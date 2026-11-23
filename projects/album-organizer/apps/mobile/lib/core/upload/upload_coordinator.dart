import 'dart:async';

import 'package:uuid/uuid.dart';

import '../api/models.dart';
import '../media/local_asset_ref.dart';
import '../media/media_library_bridge.dart';
import '../../features/tasks/task_repository.dart';
import 'background_transfer_bridge.dart';
import 'part_transfer_client.dart';
import 'upload_api.dart';
import 'upload_models.dart';
import 'upload_queue_repository.dart';

enum NetworkTransport { offline, wifi, mobile }

abstract interface class NetworkMonitor {
  Future<NetworkTransport> current();

  Stream<NetworkTransport> get changes;
}

final class UploadCoordinator {
  factory UploadCoordinator({
    required UploadQueueRepository queue,
    required MediaLibraryBridge mediaLibrary,
    required UploadApi api,
    required PartTransferClient transferClient,
    BackgroundTransferBridge? backgroundTransfers,
    required NetworkMonitor network,
    required TaskSyncApi tasks,
    bool allowMobileData = false,
    DateTime Function()? now,
    Future<void> Function()? refreshSession,
    String? ownerId,
    Duration leaseDuration = const Duration(minutes: 30),
    Duration? leaseHeartbeatInterval,
  }) => UploadCoordinator._(
    queue,
    mediaLibrary,
    api,
    transferClient,
    backgroundTransfers,
    network,
    tasks,
    allowMobileData,
    now ?? _utcNow,
    refreshSession,
    ownerId ?? 'upload-coordinator-${const Uuid().v4()}',
    leaseDuration,
    leaseHeartbeatInterval,
  );

  UploadCoordinator._(
    this._queue,
    this._mediaLibrary,
    this._api,
    this._transferClient,
    this._backgroundTransfers,
    this._network,
    this._tasks,
    this.allowMobileData,
    this._now,
    this._refreshSession,
    this._ownerId,
    this._leaseDuration,
    this._leaseHeartbeatInterval,
  ) {
    _networkSubscription = _network.changes.listen(
      (transport) {
        final allowed =
            transport == NetworkTransport.wifi ||
            (transport == NetworkTransport.mobile && allowMobileData);
        final taskIds = allowed
            ? List<String>.of(_pausedTaskIds)
            : List<String>.of(_knownTaskIds);
        for (final taskId in taskIds) {
          _scheduleNetworkTransition(taskId, allowed: allowed);
        }
      },
      onError: (Object _) {
        // Explicit runs always re-read the current transport; the durable
        // queue remains safe if a platform connectivity stream is interrupted.
      },
    );
    _backgroundSubscription = _backgroundTransfers?.events.listen(
      (event) => unawaited(
        _applyNativeEvent(event).catchError((Object _) {
          // The native link remains durable; startup/resume reconciliation
          // retries settlement without surfacing an isolate-level error.
        }),
      ),
      onError: (Object _) {
        // Startup/resume reconciliation is the durable source of truth when
        // the transient EventChannel is interrupted.
      },
    );
  }

  final UploadQueueRepository _queue;
  final MediaLibraryBridge _mediaLibrary;
  final UploadApi _api;
  final PartTransferClient _transferClient;
  final BackgroundTransferBridge? _backgroundTransfers;
  final NetworkMonitor _network;
  final TaskSyncApi _tasks;
  final DateTime Function() _now;
  final Future<void> Function()? _refreshSession;
  final String _ownerId;
  final Duration _leaseDuration;
  final Duration? _leaseHeartbeatInterval;
  final bool allowMobileData;
  static final _partTransferGate = _AsyncSemaphore(3);
  final Set<String> _knownTaskIds = <String>{};
  final Set<String> _pausedTaskIds = <String>{};
  final Map<String, Future<void>> _taskRuns = <String, Future<void>>{};
  final Map<String, Future<void>> _networkTransitions =
      <String, Future<void>>{};
  final Map<String, Future<void>> _nativeSettlements = <String, Future<void>>{};
  final Set<String> _activelyAwaitedNativeJobs = <String>{};
  late final StreamSubscription<NetworkTransport> _networkSubscription;
  StreamSubscription<NativeTransferJob>? _backgroundSubscription;

  Future<void> runUntilIdle(String taskId) {
    _knownTaskIds.add(taskId);
    final existing = _taskRuns[taskId];
    if (existing != null) return existing;
    late final Future<void> tracked;
    tracked = _runUntilIdle(taskId).whenComplete(() {
      if (identical(_taskRuns[taskId], tracked)) _taskRuns.remove(taskId);
    });
    _taskRuns[taskId] = tracked;
    return tracked;
  }

  Future<void> _runUntilIdle(String taskId) async {
    await _releasePendingCleanup(taskId);
    final network = await _network.current();
    final disallowed =
        network == NetworkTransport.offline ||
        (network == NetworkTransport.mobile && !allowMobileData);
    if (disallowed) {
      await _queue.pauseTask(taskId);
      _pausedTaskIds.add(taskId);
      return;
    }
    await _queue.resumeTask(taskId);
    _pausedTaskIds.remove(taskId);
    await reconcileNativeJobs(taskId);
    await _refreshServerAuthority(taskId);
    while (true) {
      final failureReport = await _queue.claimFailureReport(
        taskId,
        ownerId: _ownerId,
        now: _now(),
        leaseDuration: _leaseDuration,
      );
      if (failureReport != null) {
        await _processFailureReport(failureReport);
        continue;
      }
      final item = await _queue.nextRunnable(
        taskId,
        ownerId: _ownerId,
        now: _now(),
        leaseDuration: _leaseDuration,
      );
      if (item == null) {
        final exhausted = await _queue.claimExhausted(
          taskId,
          ownerId: _ownerId,
          now: _now(),
          leaseDuration: _leaseDuration,
        );
        if (exhausted != null) {
          await _recordFailure(
            exhausted.claim,
            const UploadQueueException(
              code: 'attempt_interrupted',
              safeMessage: '上传未能安全恢复，请重新添加该照片',
              retryable: false,
            ),
          );
          continue;
        }
        if (await _refreshServerAuthority(taskId)) {
          await _sealIfIdle(taskId);
        }
        return;
      }
      try {
        final heartbeat = _UploadLeaseHeartbeat(
          queue: _queue,
          claim: item.claim,
          now: _now,
          leaseDuration: _leaseDuration,
          heartbeatInterval: _leaseHeartbeatInterval,
        );
        await heartbeat.run(() => _processItem(taskId, item, heartbeat));
      } on UploadLeaseLostException {
        return;
      } catch (error) {
        await _recordFailure(item.claim, error);
      }
    }
  }

  Future<void> _sealIfIdle(String taskId) async {
    final claimed = await _queue.claimSeal(
      taskId,
      ownerId: _ownerId,
      now: _now(),
      leaseDuration: _leaseDuration,
    );
    if (!claimed) return;
    try {
      final sealed = await _tasks.seal(taskId);
      if (sealed.id != taskId) {
        throw const UploadQueueException(
          code: 'invalid_response',
          safeMessage: '服务返回异常，请稍后重试',
          retryable: true,
        );
      }
      await _queue.markServerSealed(taskId, _ownerId, now: _now());
    } catch (error) {
      final failure = _UploadFailure.from(error);
      await _queue.scheduleSealRetry(
        taskId,
        _ownerId,
        now: _now(),
        code: failure.code,
      );
    }
  }

  Future<bool> _refreshServerAuthority(String taskId) async {
    final fence = await _queue.captureServerSummaryFence(taskId);
    if (fence.isEmpty) return true;
    try {
      final summary = await _tasks.get(taskId);
      if (summary.id != taskId) return false;
      await _queue.acknowledgeServerSummary(fence, summary);
      return !await _queue.hasPendingServerAcknowledgement(taskId);
    } catch (_) {
      // Until a fresh server summary is observed, the durable snapshot keeps
      // the item in its local count delta and retries on a later run.
      return false;
    }
  }

  Future<void> _releasePendingCleanup(String taskId) async {
    final pending = (await _queue.snapshot(taskId)).cleanupRefs;
    for (final asset in pending) {
      try {
        await _mediaLibrary.release(asset);
        await _queue.completeCleanupRef(taskId, asset);
      } catch (_) {
        // The durable cleanup row remains available to the controller or the
        // next coordinator run.
      }
    }
  }

  Future<void> _processItem(
    String taskId,
    QueuedUploadItem initial,
    _UploadLeaseHeartbeat heartbeat,
  ) async {
    var item = initial;
    if (item.sha256 == null) {
      final digest = await _mediaLibrary.computeSha256(item.asset);
      await heartbeat.ensureActive();
      await _queue.markHashed(item.claim, digest);
      item = (await _queue.find(item.asset.id))!;
    }
    if (item.backendAssetId == null || item.retryRequested) {
      final initiated = await _withAuthorizationRefresh(
        () => _api.initiate(
          taskId,
          PendingLocalAsset(
            asset: item.asset,
            sha256: item.sha256!,
            authority: item.authority,
          ),
          retry: item.retryRequested,
        ),
      );
      await heartbeat.ensureActive();
      if (initiated.disposition == InitiatedAssetDisposition.duplicate) {
        item = await _queue.markDuplicate(item.claim, initiated);
        await _releaseCommittedCleanup(taskId, item.asset);
        return;
      }
      if (initiated.alreadyUploaded) {
        item = await _queue.markAlreadyUploaded(item.claim, initiated);
        await _releaseCommittedCleanup(taskId, item.asset);
        return;
      }
      await _queue.markInitiated(item.claim, initiated);
      item = (await _queue.find(item.asset.id))!;
      await _refreshServerAuthority(taskId);
    }
    final backendAssetId = item.backendAssetId!;
    final remaining = item.remainingPartNumbers;
    if (remaining.isNotEmpty) {
      final activeItem = item;
      await _queue.markUploading(item.claim);
      await _ensurePolicyAllowed(taskId, item.claim, heartbeat);
      final signedUrls = await _withAuthorizationRefresh(
        () => _api.signParts(backendAssetId, remaining),
      );
      await heartbeat.ensureActive();
      final native = _backgroundTransfers;
      if (native != null) {
        for (var start = 0; start < remaining.length; start += 3) {
          final requests = <TransferPartRequest>[
            for (final partNumber in remaining.skip(start).take(3))
              _partRequest(
                taskId,
                backendAssetId,
                activeItem,
                partNumber,
                signedUrls[partNumber],
              ),
          ];
          for (final request in requests) {
            await _queue.linkNativeJob(
              activeItem.claim,
              request.partNumber,
              request.jobId,
            );
          }
          for (final request in requests) {
            _activelyAwaitedNativeJobs.add(request.jobId);
          }
          try {
            // Register every native job in this three-part batch before any
            // waiter can observe a fast failure and cancel its siblings.
            final scheduled = await Future.wait<bool>(
              requests.map(
                (request) => _scheduleBackgroundPart(
                  taskId,
                  activeItem,
                  request,
                  heartbeat,
                  native,
                ),
              ),
            );
            if (scheduled.any((accepted) => !accepted)) return;
            final completed = await Future.wait<bool>(
              requests.map(
                (request) => _awaitBackgroundPart(
                  taskId,
                  activeItem,
                  request,
                  heartbeat,
                  native,
                ),
              ),
            );
            if (completed.any((part) => !part)) return;
          } finally {
            for (final request in requests) {
              _activelyAwaitedNativeJobs.remove(request.jobId);
            }
          }
        }
      } else {
        final completed = await Future.wait<bool>(
          remaining.map((partNumber) async {
            final request = _partRequest(
              taskId,
              backendAssetId,
              activeItem,
              partNumber,
              signedUrls[partNumber],
            );
            final result = await _partTransferGate.withPermit(() async {
              await _ensurePolicyAllowed(taskId, activeItem.claim, heartbeat);
              return _transferClient.transfer(request);
            });
            await heartbeat.ensureActive();
            await _queue.markPartComplete(
              activeItem.claim,
              partNumber,
              result.etag,
            );
            return true;
          }),
        );
        if (completed.any((part) => !part)) return;
      }
      item = (await _queue.find(item.asset.id))!;
    }
    await _ensurePolicyAllowed(taskId, item.claim, heartbeat);
    await _queue.markAwaitingCompletion(item.claim);
    await _withAuthorizationRefresh(
      () => _api.complete(backendAssetId, item.completedParts),
    );
    await heartbeat.ensureActive();
    await _commitAndRelease(taskId, item);
  }

  TransferPartRequest _partRequest(
    String taskId,
    String backendAssetId,
    QueuedUploadItem item,
    int partNumber,
    Uri? signedUrl,
  ) {
    if (signedUrl == null) {
      throw const UploadApiException(
        code: 'invalid_response',
        safeMessage: '服务返回异常，请稍后重试',
      );
    }
    final byteOffset = (partNumber - 1) * item.partSize;
    return TransferPartRequest(
      jobId:
          '$taskId:$backendAssetId:$partNumber:${item.claim.leaseGeneration}',
      asset: item.asset,
      partNumber: partNumber,
      byteOffset: byteOffset,
      byteLength: (item.asset.byteSize - byteOffset).clamp(0, item.partSize),
      signedUrl: signedUrl,
    );
  }

  Future<bool> _scheduleBackgroundPart(
    String taskId,
    QueuedUploadItem item,
    TransferPartRequest request,
    _UploadLeaseHeartbeat heartbeat,
    BackgroundTransferBridge native,
  ) {
    return _partTransferGate.withPermit(() async {
      await _ensurePolicyAllowed(taskId, item.claim, heartbeat);
      final nativeJobId = request.jobId;
      try {
        final scheduledJobId = await native.enqueuePart(
          request,
          allowMobileData: allowMobileData,
        );
        if (scheduledJobId == nativeJobId) return true;
        try {
          await native.cancel(scheduledJobId);
        } catch (_) {
          // Keep the expected stable link for startup reconciliation.
        }
        return false;
      } catch (_) {
        // Native scheduling may have succeeded before the channel reply was
        // interrupted. The pre-written link is the recovery authority.
        return false;
      }
    });
  }

  Future<bool> _awaitBackgroundPart(
    String taskId,
    QueuedUploadItem item,
    TransferPartRequest request,
    _UploadLeaseHeartbeat heartbeat,
    BackgroundTransferBridge native,
  ) async {
    final nativeJobId = request.jobId;
    final link = NativePartJobLink(
      taskId: taskId,
      partNumber: request.partNumber,
      nativeJobId: nativeJobId,
      claim: item.claim,
    );
    final NativeTransferJob terminal;
    try {
      terminal = await native.waitForTerminal(nativeJobId);
    } catch (_) {
      // Native cancellation or event-stream interruption is reconciled from
      // the durable link, but this in-process waiter must always settle.
      return false;
    }
    if (terminal.jobId != nativeJobId || !terminal.isTerminal) return false;
    await _settleNativeResult(link, terminal);
    if (terminal.state != NativeTransferState.succeeded) return false;
    await heartbeat.ensureActive();
    final refreshed = await _queue.find(item.asset.id);
    return refreshed?.completedParts[request.partNumber] == terminal.etag;
  }

  Future<void> _ensurePolicyAllowed(
    String taskId,
    UploadClaim claim,
    _UploadLeaseHeartbeat heartbeat,
  ) async {
    final transport = await _network.current();
    final allowed =
        transport == NetworkTransport.wifi ||
        (transport == NetworkTransport.mobile && allowMobileData);
    if (!allowed) {
      await _queue.pauseTask(taskId);
      _pausedTaskIds.add(taskId);
      throw const UploadLeaseLostException();
    }
    await heartbeat.ensureActive();
  }

  Future<void> _recordFailure(UploadClaim claim, Object error) async {
    final item = await _queue.find(claim.localAssetId);
    if (item == null) return;
    final failure = _UploadFailure.from(error);
    final attemptCount = item.retryCount;
    if (failure.retryable && attemptCount < 8) {
      await _queue.scheduleRetry(
        claim,
        retryCount: attemptCount,
        nextAttemptAt: _now().add(_retryDelays[attemptCount - 1]),
        code: failure.code,
        safeMessage: failure.safeMessage,
      );
      return;
    }
    final backendAssetId = item.backendAssetId;
    if (backendAssetId == null) {
      if (failure.releaseNativeRef) {
        await _queue.markLocalRejected(
          claim,
          retryCount: attemptCount,
          code: failure.code,
          safeMessage: failure.safeMessage,
        );
        await _releaseCommittedCleanup(item.taskId, item.asset);
        return;
      }
      await _queue.markFailed(
        claim,
        retryCount: attemptCount,
        code: failure.code,
        safeMessage: failure.safeMessage,
      );
      return;
    }
    if (failure.releaseNativeRef) {
      if (failure.code == 'task_sealed' &&
          await _reconcileSealedBackendFailure(item, claim, failure)) {
        return;
      }
      await _queue.markFailed(
        claim,
        retryCount: attemptCount,
        code: failure.code,
        safeMessage: failure.safeMessage,
        serverConfirmed: true,
      );
      return;
    }
    try {
      await _withAuthorizationRefresh(
        () => _api.reportFailure(
          backendAssetId,
          failure.code,
          failure.safeMessage,
        ),
      );
    } catch (_) {
      await _queue.markFailureReportPending(
        claim,
        now: _now(),
        code: failure.code,
        safeMessage: failure.safeMessage,
      );
      return;
    }
    await _queue.markFailureReported(
      claim,
      code: failure.code,
      safeMessage: failure.safeMessage,
    );
  }

  Future<bool> _reconcileSealedBackendFailure(
    QueuedUploadItem item,
    UploadClaim claim,
    _UploadFailure failure,
  ) async {
    final TaskSummary summary;
    try {
      summary = await _tasks.get(item.taskId);
    } catch (_) {
      return false;
    }
    if (summary.id != item.taskId ||
        !TaskStatusWire.uploadClosed.contains(summary.status)) {
      return false;
    }
    await _queue.markFailureResolvedBySealedTask(
      claim,
      code: failure.code,
      safeMessage: failure.safeMessage,
    );
    await _releaseCommittedCleanup(item.taskId, item.asset);
    return true;
  }

  Future<void> _processFailureReport(QueuedUploadItem item) async {
    final backendAssetId = item.backendAssetId;
    final code = item.lastErrorCode;
    final safeMessage = item.lastErrorMessage;
    if (backendAssetId == null || code == null || safeMessage == null) {
      await _queue.scheduleFailureReportRetry(item.claim, now: _now());
      return;
    }
    final heartbeat = _UploadLeaseHeartbeat(
      queue: _queue,
      claim: item.claim,
      now: _now,
      leaseDuration: _leaseDuration,
      heartbeatInterval: _leaseHeartbeatInterval,
    );
    try {
      await heartbeat.run(() async {
        await _withAuthorizationRefresh(
          () => _api.reportFailure(backendAssetId, code, safeMessage),
        );
        await heartbeat.ensureActive();
        await _queue.markFailureReported(
          item.claim,
          code: code,
          safeMessage: safeMessage,
        );
      });
    } on UploadLeaseLostException {
      return;
    } catch (error) {
      if (await _reconcileAuthoritativeFailureReport(item, error)) return;
      await _queue.scheduleFailureReportRetry(item.claim, now: _now());
    }
  }

  Future<bool> _reconcileAuthoritativeFailureReport(
    QueuedUploadItem item,
    Object error,
  ) async {
    if (error is! UploadApiException ||
        !const <int>{409, 413, 415}.contains(error.statusCode)) {
      return false;
    }
    final TaskSummary summary;
    try {
      summary = await _tasks.get(item.taskId);
    } catch (_) {
      return false;
    }
    if (summary.id != item.taskId) return false;
    final code = item.lastErrorCode ?? 'upload_failed';
    final safeMessage = item.lastErrorMessage ?? '上传失败，请重试';
    if (TaskStatusWire.uploadClosed.contains(summary.status)) {
      await _queue.markFailureResolvedBySealedTask(
        item.claim,
        code: code,
        safeMessage: safeMessage,
      );
      await _releaseCommittedCleanup(item.taskId, item.asset);
    } else {
      await _queue.markFailureReportRejected(
        item.claim,
        code: code,
        safeMessage: safeMessage,
      );
    }
    return true;
  }

  Future<void> _commitAndRelease(String taskId, QueuedUploadItem item) async {
    await _queue.markUploaded(item.claim);
    await _releaseCommittedCleanup(taskId, item.asset);
  }

  Future<void> _releaseCommittedCleanup(
    String taskId,
    LocalAssetRef asset,
  ) async {
    try {
      await _mediaLibrary.release(asset);
      await _queue.completeCleanupRef(taskId, asset);
    } catch (_) {
      // Server confirmation and the terminal local transaction already won.
      // The cleanup row remains durable for a later retry.
    }
  }

  Future<T> _withAuthorizationRefresh<T>(Future<T> Function() operation) async {
    try {
      return await operation();
    } on UploadApiException catch (error) {
      final refresh = _refreshSession;
      if (error.statusCode != 401 ||
          error.authorizationAlreadyRetried ||
          refresh == null) {
        rethrow;
      }
      await refresh();
      return operation();
    }
  }

  Stream<LocalUploadProgress> progress(String taskId) =>
      _queue.progress(taskId);

  Future<void> resumeBackgroundWork() async {
    await reconcileNativeJobs();
    final taskIds = await _queue.taskIdsWithWork();
    for (final taskId in taskIds) {
      await runUntilIdle(taskId);
    }
  }

  Future<void> reconcileNativeJobs([String? taskId]) async {
    final native = _backgroundTransfers;
    if (native == null) return;
    final links = await _queue.nativeJobLinks(taskId);
    if (links.isEmpty) return;
    final jobs = await native.queryJobs(links.map((link) => link.nativeJobId));
    final settledAttempts = <String>{};
    for (final link in links) {
      if (settledAttempts.contains(link.claim.localAssetId)) continue;
      final job = jobs[link.nativeJobId];
      if (job == null || job.jobId != link.nativeJobId) {
        if (!await _cancelSiblingNativeJobs(link, native)) continue;
        await _queue.markNativeJobMissing(link);
        settledAttempts.add(link.claim.localAssetId);
        continue;
      }
      await _settleNativeResult(
        link,
        job,
        releaseLeaseOnLast: !_activelyAwaitedNativeJobs.contains(job.jobId),
      );
      if (job.state == NativeTransferState.failed) {
        settledAttempts.add(link.claim.localAssetId);
      }
    }
  }

  Future<void> _applyNativeEvent(NativeTransferJob event) async {
    final links = await _queue.nativeJobLinks();
    for (final link in links) {
      if (link.nativeJobId != event.jobId) continue;
      await _settleNativeResult(
        link,
        event,
        releaseLeaseOnLast: !_activelyAwaitedNativeJobs.contains(event.jobId),
      );
      return;
    }
  }

  Future<void> _settleNativeResult(
    NativePartJobLink link,
    NativeTransferJob job, {
    bool releaseLeaseOnLast = false,
  }) {
    final localAssetId = link.claim.localAssetId;
    final previous = _nativeSettlements[localAssetId] ?? Future<void>.value();
    late final Future<void> tracked;
    tracked = previous
        .catchError((Object _) {})
        .then<void>(
          (_) => _applyNativeResult(
            link,
            job,
            releaseLeaseOnLast: releaseLeaseOnLast,
          ),
        )
        .whenComplete(() {
          if (identical(_nativeSettlements[localAssetId], tracked)) {
            _nativeSettlements.remove(localAssetId);
          }
        });
    _nativeSettlements[localAssetId] = tracked;
    return tracked;
  }

  Future<void> _applyNativeResult(
    NativePartJobLink link,
    NativeTransferJob job, {
    required bool releaseLeaseOnLast,
  }) async {
    final native = _backgroundTransfers;
    if (native == null) return;
    if (!await _queue.nativeJobClaimIsCurrent(link)) {
      if (job.state == NativeTransferState.failed &&
          await _queue.nativeFailureIsStaged(link)) {
        try {
          await native.acknowledge(link.nativeJobId);
          await _queue.finalizeNativeJobFailure(link);
        } catch (_) {
          // The staged failure and exact native link remain retryable.
        }
        return;
      }
      try {
        if (job.isTerminal) {
          await native.acknowledge(link.nativeJobId);
        } else {
          await native.cancel(link.nativeJobId);
        }
        await _queue.detachStaleNativeJob(link);
      } catch (_) {
        // Keep the durable link until native cleanup and local detachment can
        // both be retried without losing a terminal result.
      }
      return;
    }
    if (!job.isTerminal) return;
    if (job.state == NativeTransferState.succeeded) {
      try {
        final persisted = await _queue.recordNativeJobSucceeded(
          link,
          job.etag!,
        );
        if (!persisted) return;
        await native.acknowledge(link.nativeJobId);
        await _queue.acknowledgeNativeJob(
          link,
          releaseLease: releaseLeaseOnLast,
        );
      } on UploadLeaseLostException {
        return;
      }
      return;
    }
    if (job.state == NativeTransferState.failed) {
      final failure = _UploadFailure.fromNative(job);
      try {
        final item = await _queue.find(link.claim.localAssetId);
        if (item == null) return;
        if (!await _cancelSiblingNativeJobs(link, native)) return;
        final now = _now();
        final canRetry = failure.retryable && item.retryCount < 8;
        final persisted = await _queue.recordNativeJobFailed(
          link,
          now: now,
          nextAttemptAt: canRetry
              ? now.add(_retryDelays[item.retryCount - 1])
              : null,
          code: failure.code,
          safeMessage: failure.safeMessage,
          retryable: failure.retryable,
        );
        if (!persisted) return;
        await native.acknowledge(link.nativeJobId);
        await _queue.finalizeNativeJobFailure(link);
      } on UploadLeaseLostException {
        return;
      } catch (_) {
        // A failed native acknowledgement leaves the durable link intact so
        // the same terminal result can be reconciled later.
        return;
      }
    }
  }

  Future<bool> _cancelSiblingNativeJobs(
    NativePartJobLink failed,
    BackgroundTransferBridge native,
  ) async {
    final siblings = (await _queue.nativeJobLinks(failed.taskId)).where(
      (link) =>
          link.claim.localAssetId == failed.claim.localAssetId &&
          link.nativeJobId != failed.nativeJobId,
    );
    for (final sibling in siblings) {
      try {
        await native.cancel(sibling.nativeJobId);
        if (!await _queue.detachClaimedNativeJob(sibling)) return false;
      } catch (_) {
        // Keep the failed job linked until every sibling has a durable,
        // idempotent cancellation outcome.
        return false;
      }
    }
    return true;
  }

  Future<void> retryFailed(String taskId, String localAssetId) async {
    await _queue.requestFailedRetry(taskId, localAssetId);
    await runUntilIdle(taskId);
  }

  Future<void> _resumeAfterNetworkChange(String taskId) async {
    try {
      await _queue.resumeTask(taskId);
      await runUntilIdle(taskId);
      _pausedTaskIds.remove(taskId);
    } catch (_) {
      // The durable queue retains the item for the next explicit/network run.
    }
  }

  Future<void> _pauseAfterNetworkChange(String taskId) async {
    try {
      await _queue.pauseTask(taskId);
      _pausedTaskIds.add(taskId);
    } catch (_) {
      // A later explicit run re-evaluates the persisted network policy.
    }
  }

  void _scheduleNetworkTransition(String taskId, {required bool allowed}) {
    final previous = _networkTransitions[taskId] ?? Future<void>.value();
    late final Future<void> tracked;
    tracked = previous
        .then<void>((_) {
          return allowed
              ? _resumeAfterNetworkChange(taskId)
              : _pauseAfterNetworkChange(taskId);
        })
        .whenComplete(() {
          if (identical(_networkTransitions[taskId], tracked)) {
            _networkTransitions.remove(taskId);
          }
        });
    _networkTransitions[taskId] = tracked;
  }

  Future<void> dispose() async {
    await _backgroundSubscription?.cancel();
    await _networkSubscription.cancel();
    await Future.wait<void>(List<Future<void>>.of(_networkTransitions.values));
    await Future.wait<void>(List<Future<void>>.of(_nativeSettlements.values));
  }
}

const List<Duration> _retryDelays = <Duration>[
  Duration(seconds: 2),
  Duration(seconds: 10),
  Duration(seconds: 30),
  Duration(minutes: 2),
  Duration(minutes: 5),
  Duration(minutes: 15),
  Duration(minutes: 30),
  // Kept as the documented next backoff if the attempt cap changes. With the
  // current hard cap of eight attempts, only the first seven delays run.
  Duration(minutes: 60),
];

DateTime _utcNow() => DateTime.now().toUtc();

final class _UploadFailure {
  const _UploadFailure({
    required this.code,
    required this.safeMessage,
    required this.retryable,
    required this.releaseNativeRef,
  });

  factory _UploadFailure.from(Object error) {
    final String code;
    final String safeMessage;
    final int? statusCode;
    if (error case final PartTransferException failure) {
      code = failure.code;
      safeMessage = failure.safeMessage;
      statusCode = failure.statusCode;
    } else if (error case final UploadApiException failure) {
      code = failure.code;
      safeMessage = failure.safeMessage;
      statusCode = failure.statusCode;
    } else if (error case final MediaLibraryException failure) {
      code = failure.code;
      safeMessage = failure.userMessage;
      statusCode = null;
    } else if (error case final UploadQueueException failure) {
      return _UploadFailure(
        code: failure.code,
        safeMessage: failure.safeMessage,
        retryable: failure.retryable,
        releaseNativeRef: false,
      );
    } else {
      code = 'service_unavailable';
      safeMessage = '服务暂时不可用，请稍后重试';
      statusCode = null;
    }
    final retryable =
        code == 'network_unavailable' ||
        code == 'service_unavailable' ||
        code == 'signed_url_expired' ||
        (statusCode != null && statusCode >= 500);
    return _UploadFailure(
      code: code,
      safeMessage: safeMessage,
      retryable: retryable,
      releaseNativeRef:
          statusCode == 409 || statusCode == 413 || statusCode == 415,
    );
  }

  factory _UploadFailure.fromNative(NativeTransferJob job) {
    final (code, safeMessage, mayRetry) = switch (job.code) {
      'signed_url_expired' => ('signed_url_expired', '上传链接已过期，正在重试', true),
      'network_unavailable' => ('network_unavailable', '网络暂时不可用，请稍后重试', true),
      'background_expired' => ('service_unavailable', '后台上传时间已用尽，请稍后重试', true),
      'source_missing' ||
      'source_read_failed' => ('media_unavailable', '无法读取照片，请重新添加', false),
      _ => ('service_unavailable', '服务暂时不可用，请稍后重试', true),
    };
    return _UploadFailure(
      code: code,
      safeMessage: safeMessage,
      retryable: job.retryable == true && mayRetry,
      releaseNativeRef: false,
    );
  }

  final String code;
  final String safeMessage;
  final bool retryable;
  final bool releaseNativeRef;
}

final class _UploadLeaseHeartbeat {
  _UploadLeaseHeartbeat({
    required UploadQueueRepository queue,
    required UploadClaim claim,
    required DateTime Function() now,
    required Duration leaseDuration,
    Duration? heartbeatInterval,
  }) : this._(queue, claim, now, leaseDuration, heartbeatInterval);

  _UploadLeaseHeartbeat._(
    this._queue,
    this._claim,
    this._now,
    this._leaseDuration,
    this._heartbeatInterval,
  );

  final UploadQueueRepository _queue;
  final UploadClaim _claim;
  final DateTime Function() _now;
  final Duration _leaseDuration;
  final Duration? _heartbeatInterval;
  Timer? _timer;
  Future<void>? _renewal;
  bool _lost = false;

  Future<T> run<T>(Future<T> Function() operation) async {
    await ensureActive();
    final interval =
        _heartbeatInterval ??
        Duration(
          microseconds: (_leaseDuration.inMicroseconds ~/ 3).clamp(
            1000,
            const Duration(minutes: 1).inMicroseconds,
          ),
        );
    if (_leaseDuration > Duration.zero) {
      _timer = Timer.periodic(interval, (_) => _scheduleRenewal());
    }
    try {
      return await operation();
    } finally {
      _timer?.cancel();
      await _renewal;
    }
  }

  void _scheduleRenewal() {
    if (_lost || _renewal != null) return;
    late final Future<void> renewal;
    renewal = _renew()
        .onError((_, _) {
          _lost = true;
        })
        .whenComplete(() {
          if (identical(_renewal, renewal)) _renewal = null;
        });
    _renewal = renewal;
  }

  Future<void> ensureActive() async {
    if (_lost) throw const UploadLeaseLostException();
    final pending = _renewal;
    if (pending != null) await pending;
    if (_lost) throw const UploadLeaseLostException();
    await _renew();
  }

  Future<void> _renew() async {
    final renewed = await _queue.renewLease(
      _claim,
      now: _now(),
      leaseDuration: _leaseDuration,
    );
    if (!renewed) {
      _lost = true;
      throw const UploadLeaseLostException();
    }
  }
}

final class _AsyncSemaphore {
  _AsyncSemaphore(this._maximum);

  final int _maximum;
  final List<Completer<void>> _waiters = <Completer<void>>[];
  int _active = 0;

  Future<T> withPermit<T>(Future<T> Function() operation) async {
    await _acquire();
    try {
      return await operation();
    } finally {
      _release();
    }
  }

  Future<void> _acquire() {
    if (_active < _maximum) {
      _active += 1;
      return Future<void>.value();
    }
    final waiter = Completer<void>();
    _waiters.add(waiter);
    return waiter.future;
  }

  void _release() {
    if (_waiters.isNotEmpty) {
      _waiters.removeAt(0).complete();
      return;
    }
    _active -= 1;
  }
}
