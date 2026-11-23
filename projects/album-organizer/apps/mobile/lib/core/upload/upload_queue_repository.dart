import 'dart:async';
import 'dart:convert';

import 'package:drift/drift.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/models.dart';
import '../media/local_asset_ref.dart';
import '../../features/tasks/task_repository.dart';
import 'upload_api.dart';
import 'upload_database.dart';
import 'upload_models.dart';

UploadQueueException _candidateQueueConflict() => const UploadQueueException(
  code: 'candidate_queue_conflict',
  safeMessage: '候选与本地上传队列不一致，请重新核对',
  retryable: false,
);

final uploadQueueRepositoryProvider = Provider<UploadQueueRepository>(
  (ref) => UploadQueueRepository(ref.watch(uploadDatabaseProvider)),
);

final class ImportQueueSnapshot {
  const ImportQueueSnapshot({
    required this.unacknowledgedAssetIds,
    this.serverAcceptedAssetIds = const <String>{},
    this.serverUnsupportedAssetIds = const <String>{},
    required this.queued,
    required this.uploaded,
    required this.failed,
    this.cleanupRefs = const <LocalAssetRef>[],
    this.sealWhenIdle = false,
    this.serverSealed = false,
  });

  /// Stable IDs durably owned by the local queue but not yet represented by
  /// the TaskSummary returned by the server.
  final Set<String> unacknowledgedAssetIds;

  /// Stable IDs the queue has authoritatively observed in server accepted
  /// counts. This ledger survives controller recreation.
  final Set<String> serverAcceptedAssetIds;

  /// Stable IDs the queue has authoritatively observed in server unsupported
  /// counts. These IDs are visible but never contribute a local delta.
  final Set<String> serverUnsupportedAssetIds;
  final int queued;

  /// Local completed delta not yet represented by TaskSummary.uploadedAssets.
  final int uploaded;

  /// Local failed delta not yet represented by TaskSummary.failedAssets.
  final int failed;
  final List<LocalAssetRef> cleanupRefs;
  final bool sealWhenIdle;
  final bool serverSealed;
}

abstract interface class ImportQueueBoundary {
  Future<ImportQueueSnapshot> snapshot(String taskId);

  /// Ownership transfer is all-or-nothing: a successful return means the
  /// queue owns every ref; a thrown error means it owns none of them.
  Future<ImportQueueSnapshot> enqueue(
    String taskId,
    List<LocalAssetRef> assets, {
    UploadAuthority? authority,
  });

  /// Durably records controller-owned refs before native cleanup is attempted.
  Future<ImportQueueSnapshot> retainCleanupRefs(
    String taskId,
    List<LocalAssetRef> assets,
  );

  /// Removes a retained ref only after native release has succeeded.
  Future<ImportQueueSnapshot> completeCleanupRef(
    String taskId,
    LocalAssetRef asset,
  );

  /// Persists server identity authority returned by the idempotent asset API.
  Future<ImportQueueSnapshot> recordServerDisposition(
    String taskId,
    UnsupportedReportReceipt receipt,
  );

  Future<void> setSealWhenIdle(String taskId);
}

/// Task 7's in-memory handoff boundary retained for isolated controller tests.
/// The application provider uses [UploadQueueRepository].
final class DeferredImportQueueBoundary implements ImportQueueBoundary {
  final Map<String, LocalAssetRef> _queued = <String, LocalAssetRef>{};
  final Map<String, LocalAssetRef> _cleanup = <String, LocalAssetRef>{};
  final Set<String> _serverAccepted = <String>{};
  final Set<String> _serverUnsupported = <String>{};
  bool _sealWhenIdle = false;

  @override
  Future<ImportQueueSnapshot> enqueue(
    String taskId,
    List<LocalAssetRef> assets, {
    UploadAuthority? authority,
  }) async {
    final ids = assets.map((asset) => asset.id).toSet();
    if (ids.length != assets.length ||
        ids.any(_queued.containsKey) ||
        ids.any(_serverAccepted.contains) ||
        ids.any(_serverUnsupported.contains)) {
      throw StateError('asset ownership conflicts with queue authority');
    }
    _queued.addEntries(assets.map((asset) => MapEntry(asset.id, asset)));
    return snapshot(taskId);
  }

  @override
  Future<void> setSealWhenIdle(String taskId) async {
    _sealWhenIdle = true;
  }

  @override
  Future<ImportQueueSnapshot> retainCleanupRefs(
    String taskId,
    List<LocalAssetRef> assets,
  ) async {
    _cleanup.addEntries(assets.map((asset) => MapEntry(asset.locator, asset)));
    return snapshot(taskId);
  }

  @override
  Future<ImportQueueSnapshot> completeCleanupRef(
    String taskId,
    LocalAssetRef asset,
  ) async {
    _cleanup.remove(asset.locator);
    return snapshot(taskId);
  }

  @override
  Future<ImportQueueSnapshot> recordServerDisposition(
    String taskId,
    UnsupportedReportReceipt receipt,
  ) async {
    if (_queued.containsKey(receipt.localAssetId)) {
      throw StateError('server disposition conflicts with local ownership');
    }
    switch (receipt.disposition) {
      case ServerAssetDisposition.accepted:
        _serverUnsupported.remove(receipt.localAssetId);
        _serverAccepted.add(receipt.localAssetId);
      case ServerAssetDisposition.unsupported:
        _serverAccepted.remove(receipt.localAssetId);
        _serverUnsupported.add(receipt.localAssetId);
    }
    return snapshot(taskId);
  }

  @override
  Future<ImportQueueSnapshot> snapshot(String taskId) async =>
      ImportQueueSnapshot(
        unacknowledgedAssetIds: Set<String>.unmodifiable(_queued.keys),
        serverAcceptedAssetIds: Set<String>.unmodifiable(_serverAccepted),
        serverUnsupportedAssetIds: Set<String>.unmodifiable(_serverUnsupported),
        queued: _queued.length,
        uploaded: 0,
        failed: 0,
        cleanupRefs: List<LocalAssetRef>.unmodifiable(_cleanup.values),
        sealWhenIdle: _sealWhenIdle,
      );
}

final class UploadQueueRepository implements ImportQueueBoundary {
  UploadQueueRepository(this._database);

  static const int defaultPartSize = 8 * 1024 * 1024;

  final UploadDatabase _database;
  final StreamController<String> _workAvailable =
      StreamController<String>.broadcast(sync: true);

  Stream<String> get workAvailable => _workAvailable.stream;

  @override
  Future<ImportQueueSnapshot> enqueue(
    String taskId,
    List<LocalAssetRef> assets, {
    UploadAuthority? authority,
  }) async {
    final result = await _database.transaction(() async {
      final ids = assets.map((asset) => asset.id).toSet();
      if (ids.length != assets.length) {
        throw StateError('asset ownership conflicts with queue authority');
      }
      final owned = await (_database.select(
        _database.uploadItems,
      )..where((item) => item.localAssetId.isIn(ids))).get();
      final authoritative =
          await (_database.select(_database.importQueueAuthorities)..where(
                (row) => row.taskId.equals(taskId) & row.localAssetId.isIn(ids),
              ))
              .get();
      if (owned.isNotEmpty || authoritative.isNotEmpty) {
        throw StateError('asset ownership conflicts with queue authority');
      }
      await _insertAssets(taskId, assets, authority: authority);
      return snapshot(taskId);
    });
    _workAvailable.add(taskId);
    return result;
  }

  Future<ImportQueueSnapshot> enqueueControlledCandidate(
    String taskId,
    List<LocalAssetRef> assets, {
    required UploadAuthority authority,
  }) async {
    var inserted = false;
    final result = await _database.transaction(() async {
      final ids = assets.map((asset) => asset.id).toSet();
      if (assets.isEmpty || ids.length != assets.length) {
        throw _candidateQueueConflict();
      }
      final byIds = await (_database.select(
        _database.uploadItems,
      )..where((item) => item.localAssetId.isIn(ids))).get();
      final byCandidate = await (_database.select(
        _database.uploadItems,
      )..where((item) => item.candidateId.equals(authority.candidateId))).get();
      final byBinding = await (_database.select(
        _database.uploadItems,
      )..where((item) => item.bindingId.equals(authority.bindingId))).get();
      final authoritative = await (_database.select(
        _database.importQueueAuthorities,
      )..where((row) => row.localAssetId.isIn(ids))).get();
      if (byIds.isEmpty &&
          byCandidate.isEmpty &&
          byBinding.isEmpty &&
          authoritative.isEmpty) {
        await _insertAssets(taskId, assets, authority: authority);
        inserted = true;
        return snapshot(taskId);
      }
      if (authoritative.isNotEmpty ||
          byIds.length != assets.length ||
          byCandidate.length != assets.length ||
          byBinding.length != assets.length ||
          byIds.map((row) => row.localAssetId).toSet().length != ids.length ||
          byCandidate.map((row) => row.localAssetId).toSet().length !=
              ids.length ||
          byBinding.map((row) => row.localAssetId).toSet().length !=
              ids.length ||
          !byBinding.every((row) => ids.contains(row.localAssetId)) ||
          !byCandidate.every((row) => ids.contains(row.localAssetId))) {
        throw _candidateQueueConflict();
      }
      final incoming = <String, LocalAssetRef>{
        for (final asset in assets) asset.id: asset,
      };
      for (final row in byIds) {
        final asset = incoming[row.localAssetId];
        if (asset == null ||
            row.taskId != taskId ||
            row.bindingId != authority.bindingId ||
            row.candidateId != authority.candidateId ||
            row.byteSize != asset.byteSize ||
            row.localRefJson != jsonEncode(asset.toMap())) {
          throw _candidateQueueConflict();
        }
      }
      return snapshot(taskId);
    });
    if (inserted) _workAvailable.add(taskId);
    return result;
  }

  Future<void> _insertAssets(
    String taskId,
    List<LocalAssetRef> assets, {
    UploadAuthority? authority,
  }) async {
    for (final asset in assets) {
      await _database
          .into(_database.uploadItems)
          .insert(
            UploadItemsCompanion.insert(
              localAssetId: asset.id,
              taskId: taskId,
              bindingId: Value<String?>(authority?.bindingId),
              candidateId: Value<String?>(authority?.candidateId),
              localRefJson: jsonEncode(asset.toMap()),
              byteSize: asset.byteSize,
              partSize: defaultPartSize,
              state: LocalUploadState.awaitingHash.name,
            ),
          );
      final partCount =
          (asset.byteSize + defaultPartSize - 1) ~/ defaultPartSize;
      await _database.batch((batch) {
        batch.insertAll(_database.uploadParts, <UploadPartsCompanion>[
          for (var partNumber = 1; partNumber <= partCount; partNumber += 1)
            UploadPartsCompanion.insert(
              localAssetId: asset.id,
              partNumber: partNumber,
            ),
        ]);
      });
    }
  }

  Future<Set<String>> taskIdsWithWork() async {
    final items = await _database.select(_database.uploadItems).get();
    final tasks = await _database.select(_database.importQueueTasks).get();
    final cleanup = await _database.select(_database.importCleanupRefs).get();
    final authorities = await _database
        .select(_database.importQueueAuthorities)
        .get();
    final acceptedIds = authorities
        .where((row) => row.disposition == ServerAssetDisposition.accepted.name)
        .map((row) => row.localAssetId)
        .toSet();
    final taskIds = <String>{
      for (final item in items)
        if (_itemNeedsWork(item, acceptedIds.contains(item.localAssetId)))
          item.taskId,
      for (final task in tasks)
        if (task.sealWhenIdle && !task.serverSealed) task.taskId,
      for (final ref in cleanup) ref.taskId,
    };
    return Set<String>.unmodifiable(taskIds);
  }

  @override
  Future<ImportQueueSnapshot> recordServerDisposition(
    String taskId,
    UnsupportedReportReceipt receipt,
  ) {
    return _database.transaction(() async {
      final conflict =
          await (_database.select(_database.uploadItems)..where(
                (item) =>
                    item.taskId.equals(taskId) &
                    item.localAssetId.equals(receipt.localAssetId),
              ))
              .getSingleOrNull();
      if (conflict != null) {
        throw StateError('server disposition conflicts with local ownership');
      }
      await _database
          .into(_database.importQueueAuthorities)
          .insertOnConflictUpdate(
            ImportQueueAuthoritiesCompanion.insert(
              taskId: taskId,
              localAssetId: receipt.localAssetId,
              disposition: receipt.disposition.name,
            ),
          );
      return snapshot(taskId);
    });
  }

  @override
  Future<ImportQueueSnapshot> snapshot(String taskId) async {
    final items = await (_database.select(
      _database.uploadItems,
    )..where((item) => item.taskId.equals(taskId))).get();
    final authorities = await (_database.select(
      _database.importQueueAuthorities,
    )..where((row) => row.taskId.equals(taskId))).get();
    final cleanupRows = await (_database.select(
      _database.importCleanupRefs,
    )..where((row) => row.taskId.equals(taskId))).get();
    final task = await (_database.select(
      _database.importQueueTasks,
    )..where((row) => row.taskId.equals(taskId))).getSingleOrNull();
    final serverAcceptedIds = authorities
        .where((row) => row.disposition == ServerAssetDisposition.accepted.name)
        .map((row) => row.localAssetId)
        .toSet();
    return ImportQueueSnapshot(
      unacknowledgedAssetIds: Set<String>.unmodifiable(
        items
            .where(
              (item) =>
                  !item.localRejected &&
                  item.state != LocalUploadState.duplicate.name &&
                  !serverAcceptedIds.contains(item.localAssetId),
            )
            .map((item) => item.localAssetId),
      ),
      serverAcceptedAssetIds: Set<String>.unmodifiable(serverAcceptedIds),
      serverUnsupportedAssetIds: Set<String>.unmodifiable(
        authorities
            .where(
              (row) =>
                  row.disposition == ServerAssetDisposition.unsupported.name,
            )
            .map((row) => row.localAssetId),
      ),
      queued: items
          .where(
            (item) =>
                item.state != LocalUploadState.uploaded.name &&
                item.state != LocalUploadState.duplicate.name &&
                item.state != LocalUploadState.failedPermanent.name,
          )
          .length,
      uploaded: items
          .where(
            (item) =>
                item.state == LocalUploadState.uploaded.name &&
                !item.localRejected &&
                !item.terminalSummaryAcknowledged,
          )
          .length,
      failed: items
          .where(
            (item) =>
                item.state == LocalUploadState.failedPermanent.name &&
                !item.localRejected &&
                !item.terminalSummaryAcknowledged,
          )
          .length,
      cleanupRefs: List<LocalAssetRef>.unmodifiable(
        cleanupRows.map((row) => _decodeRef(row.localRefJson)),
      ),
      sealWhenIdle: task?.sealWhenIdle ?? false,
      serverSealed: task?.serverSealed ?? false,
    );
  }

  Future<void> markPartComplete(
    UploadClaim claim,
    int partNumber,
    String etag,
  ) async {
    _validatePartResult(partNumber, etag);
    await _database.transaction(() async {
      await _requireClaim(claim);
      final updated =
          await (_database.update(_database.uploadParts)..where(
                (part) =>
                    part.localAssetId.equals(claim.localAssetId) &
                    part.partNumber.equals(partNumber),
              ))
              .write(
                UploadPartsCompanion(
                  etag: Value<String?>(etag),
                  nativeJobId: const Value<String?>(null),
                  nativeLeaseOwner: const Value<String?>(null),
                  nativeLeaseGeneration: const Value<int?>(null),
                  nativePolicyGeneration: const Value<int?>(null),
                ),
              );
      if (updated != 1) {
        throw const UploadQueueException(
          code: 'invalid_transfer_result',
          safeMessage: '上传分片返回异常，请重试',
          retryable: true,
        );
      }
    });
  }

  Future<void> linkNativeJob(
    UploadClaim claim,
    int partNumber,
    String nativeJobId,
  ) async {
    if (partNumber <= 0 ||
        nativeJobId.isEmpty ||
        nativeJobId.length > 512 ||
        nativeJobId.runes.any(
          (codePoint) => codePoint < 0x20 || codePoint == 0x7f,
        )) {
      throw const UploadQueueException(
        code: 'invalid_native_job',
        safeMessage: '后台上传任务无效，请重试',
        retryable: true,
      );
    }
    await _database.transaction(() async {
      await _requireClaim(claim);
      final part =
          await (_database.select(_database.uploadParts)..where(
                (row) =>
                    row.localAssetId.equals(claim.localAssetId) &
                    row.partNumber.equals(partNumber),
              ))
              .getSingleOrNull();
      if (part == null ||
          part.etag != null ||
          part.nativeJobId != null && part.nativeJobId != nativeJobId) {
        throw const UploadQueueException(
          code: 'native_job_conflict',
          safeMessage: '后台上传状态冲突，请重试',
          retryable: true,
        );
      }
      await (_database.update(_database.uploadParts)..where(
            (row) =>
                row.localAssetId.equals(claim.localAssetId) &
                row.partNumber.equals(partNumber),
          ))
          .write(
            UploadPartsCompanion(
              nativeJobId: Value<String?>(nativeJobId),
              nativeLeaseOwner: Value<String?>(claim.ownerId),
              nativeLeaseGeneration: Value<int?>(claim.leaseGeneration),
              nativePolicyGeneration: Value<int?>(claim.policyGeneration),
            ),
          );
    });
  }

  Future<List<NativePartJobLink>> nativeJobLinks([String? taskId]) async {
    final parts = await (_database.select(
      _database.uploadParts,
    )..where((part) => part.nativeJobId.isNotNull())).get();
    if (parts.isEmpty) return const <NativePartJobLink>[];
    final items =
        await (_database.select(_database.uploadItems)..where(
              (item) => item.localAssetId.isIn(
                parts.map((part) => part.localAssetId).toSet(),
              ),
            ))
            .get();
    final itemsById = <String, UploadItem>{
      for (final item in items) item.localAssetId: item,
    };
    return <NativePartJobLink>[
      for (final part in parts)
        if (itemsById[part.localAssetId] case final item?)
          if ((taskId == null || item.taskId == taskId) &&
              item.state != LocalUploadState.uploaded.name &&
              item.state != LocalUploadState.duplicate.name &&
              item.state != LocalUploadState.failedPermanent.name &&
              part.nativeLeaseOwner != null &&
              part.nativeLeaseGeneration != null &&
              part.nativePolicyGeneration != null)
            NativePartJobLink(
              taskId: item.taskId,
              partNumber: part.partNumber,
              nativeJobId: part.nativeJobId!,
              claim: UploadClaim(
                localAssetId: item.localAssetId,
                ownerId: part.nativeLeaseOwner!,
                leaseGeneration: part.nativeLeaseGeneration!,
                policyGeneration: part.nativePolicyGeneration!,
              ),
            ),
    ];
  }

  Future<bool> recordNativeJobSucceeded(
    NativePartJobLink link,
    String etag,
  ) async {
    _validatePartResult(link.partNumber, etag);
    return _database.transaction(() async {
      final part = await _nativePart(link);
      if (part == null) {
        final current =
            await (_database.select(_database.uploadParts)..where(
                  (row) =>
                      row.localAssetId.equals(link.claim.localAssetId) &
                      row.partNumber.equals(link.partNumber),
                ))
                .getSingleOrNull();
        return current?.nativeJobId == null && current?.etag == etag;
      }
      await _requireClaim(link.claim);
      await (_database.update(_database.uploadParts)..where(
            (row) =>
                row.localAssetId.equals(link.claim.localAssetId) &
                row.partNumber.equals(link.partNumber) &
                row.nativeJobId.equals(link.nativeJobId) &
                row.nativeLeaseOwner.equals(link.claim.ownerId) &
                row.nativeLeaseGeneration.equals(link.claim.leaseGeneration) &
                row.nativePolicyGeneration.equals(link.claim.policyGeneration),
          ))
          .write(UploadPartsCompanion(etag: Value<String?>(etag)));
      return true;
    });
  }

  Future<bool> recordNativeJobFailed(
    NativePartJobLink link, {
    required DateTime now,
    required DateTime? nextAttemptAt,
    required String code,
    required String safeMessage,
    required bool retryable,
  }) {
    if (code.isEmpty || safeMessage.isEmpty) {
      throw const UploadQueueException(
        code: 'invalid_native_result',
        safeMessage: '后台上传结果无效，请重试',
        retryable: true,
      );
    }
    return _database.transaction(() async {
      final part = await _nativePart(link);
      if (part == null) return false;
      final item = await _requireClaim(link.claim);
      final canRetry = retryable && item.retryCount < 8;
      await _writeClaimed(
        link.claim,
        UploadItemsCompanion(
          state: Value<String>(
            canRetry
                ? LocalUploadState.failedRetryable.name
                : LocalUploadState.failureReportPending.name,
          ),
          nextAttemptAt: Value<DateTime?>(canRetry ? nextAttemptAt : null),
          lastErrorCode: Value<String?>(code),
          lastErrorMessage: Value<String?>(safeMessage),
          failureReportNextAttemptAt: Value<DateTime?>(
            canRetry ? null : now.toUtc(),
          ),
        ),
      );
      return true;
    });
  }

  Future<bool> finalizeNativeJobFailure(NativePartJobLink link) async {
    final finalized = await _database.transaction(() async {
      final part = await _nativePart(link);
      if (part == null) return true;
      final item =
          await (_database.select(_database.uploadItems)..where(
                (row) => row.localAssetId.equals(link.claim.localAssetId),
              ))
              .getSingle();
      if (!_hasStagedNativeFailure(item)) return false;
      if (!await _clearNativePart(link)) return false;
      if (_matchesClaim(item, link.claim)) {
        await _writeClaimed(
          link.claim,
          const UploadItemsCompanion(
            leaseOwner: Value<String?>(null),
            leaseExpiresAt: Value<DateTime?>(null),
          ),
        );
      }
      return true;
    });
    if (finalized) _workAvailable.add(link.taskId);
    return finalized;
  }

  Future<bool> acknowledgeNativeJob(
    NativePartJobLink link, {
    bool releaseLease = false,
  }) async {
    final acknowledged = await _database.transaction(() async {
      final part = await _nativePart(link);
      if (part == null) {
        final current =
            await (_database.select(_database.uploadParts)..where(
                  (row) =>
                      row.localAssetId.equals(link.claim.localAssetId) &
                      row.partNumber.equals(link.partNumber),
                ))
                .getSingleOrNull();
        return current?.nativeJobId == null && current?.etag != null;
      }
      if (part.etag == null) return false;
      final updated =
          await (_database.update(_database.uploadParts)..where(
                (row) =>
                    row.localAssetId.equals(link.claim.localAssetId) &
                    row.partNumber.equals(link.partNumber) &
                    row.nativeJobId.equals(link.nativeJobId) &
                    row.nativeLeaseOwner.equals(link.claim.ownerId) &
                    row.nativeLeaseGeneration.equals(
                      link.claim.leaseGeneration,
                    ) &
                    row.nativePolicyGeneration.equals(
                      link.claim.policyGeneration,
                    ),
              ))
              .write(
                const UploadPartsCompanion(
                  nativeJobId: Value<String?>(null),
                  nativeLeaseOwner: Value<String?>(null),
                  nativeLeaseGeneration: Value<int?>(null),
                  nativePolicyGeneration: Value<int?>(null),
                ),
              );
      if (updated == 1 && releaseLease) {
        final hasSibling =
            await (_database.select(_database.uploadParts)
                  ..where(
                    (row) =>
                        row.localAssetId.equals(link.claim.localAssetId) &
                        row.nativeJobId.isNotNull(),
                  )
                  ..limit(1))
                .getSingleOrNull() !=
            null;
        if (!hasSibling) {
          await (_database.update(_database.uploadItems)..where(
                (row) =>
                    row.localAssetId.equals(link.claim.localAssetId) &
                    row.leaseOwner.equals(link.claim.ownerId) &
                    row.leaseGeneration.equals(link.claim.leaseGeneration) &
                    row.policyGeneration.equals(link.claim.policyGeneration),
              ))
              .write(
                UploadItemsCompanion(
                  state: Value<String>(LocalUploadState.awaitingParts.name),
                  leaseOwner: const Value<String?>(null),
                  leaseExpiresAt: const Value<DateTime?>(null),
                ),
              );
        }
      }
      return updated == 1;
    });
    if (acknowledged) _workAvailable.add(link.taskId);
    return acknowledged;
  }

  Future<bool> detachClaimedNativeJob(NativePartJobLink link) {
    return _database.transaction(() async {
      if (await _nativePart(link) == null) return true;
      await _requireClaim(link.claim);
      return _clearNativePart(link);
    });
  }

  Future<bool> markNativeJobMissing(NativePartJobLink link) {
    return _database.transaction(() async {
      final part = await _nativePart(link);
      if (part == null) return false;
      final item =
          await (_database.select(_database.uploadItems)..where(
                (row) => row.localAssetId.equals(link.claim.localAssetId),
              ))
              .getSingle();
      final failureAlreadyStaged = _hasStagedNativeFailure(item);
      if (failureAlreadyStaged) {
        final cleared = await _clearNativePart(link);
        if (!cleared) return false;
        if (_matchesClaim(item, link.claim)) {
          await _writeClaimed(
            link.claim,
            const UploadItemsCompanion(
              leaseOwner: Value<String?>(null),
              leaseExpiresAt: Value<DateTime?>(null),
            ),
          );
        }
        return true;
      }
      if (!_matchesClaim(item, link.claim)) {
        if (item.policyGeneration != link.claim.policyGeneration) {
          return _detachStaleNativeJob(link, item);
        }
        throw const UploadLeaseLostException();
      }
      final cleared = await _clearNativePart(link);
      if (!cleared) return false;
      if (part.etag != null) {
        await (_database.update(_database.uploadItems)..where(
              (row) =>
                  row.localAssetId.equals(link.claim.localAssetId) &
                  row.leaseOwner.equals(link.claim.ownerId) &
                  row.leaseGeneration.equals(link.claim.leaseGeneration) &
                  row.policyGeneration.equals(link.claim.policyGeneration),
            ))
            .write(
              UploadItemsCompanion(
                state: Value<String>(LocalUploadState.awaitingParts.name),
                leaseOwner: const Value<String?>(null),
                leaseExpiresAt: const Value<DateTime?>(null),
              ),
            );
        return true;
      }
      await _writeClaimed(
        link.claim,
        UploadItemsCompanion(
          state: Value<String>(LocalUploadState.awaitingParts.name),
          leaseOwner: const Value<String?>(null),
          leaseExpiresAt: const Value<DateTime?>(null),
        ),
      );
      return true;
    });
  }

  Future<bool> nativeJobClaimIsCurrent(NativePartJobLink link) async {
    if (await _nativePart(link) == null) return false;
    final item =
        await (_database.select(
              _database.uploadItems,
            )..where((row) => row.localAssetId.equals(link.claim.localAssetId)))
            .getSingleOrNull();
    return item != null && _matchesClaim(item, link.claim);
  }

  Future<bool> nativeFailureIsStaged(NativePartJobLink link) async {
    if (await _nativePart(link) == null) return false;
    final item =
        await (_database.select(
              _database.uploadItems,
            )..where((row) => row.localAssetId.equals(link.claim.localAssetId)))
            .getSingleOrNull();
    return item != null && _hasStagedNativeFailure(item);
  }

  Future<bool> detachStaleNativeJob(NativePartJobLink link) {
    return _database.transaction(() async {
      final part = await _nativePart(link);
      if (part == null) return true;
      final item =
          await (_database.select(_database.uploadItems)..where(
                (row) => row.localAssetId.equals(link.claim.localAssetId),
              ))
              .getSingle();
      if (item.policyGeneration == link.claim.policyGeneration) {
        throw const UploadLeaseLostException();
      }
      return _detachStaleNativeJob(link, item);
    });
  }

  Future<bool> _detachStaleNativeJob(
    NativePartJobLink link,
    UploadItem item,
  ) async {
    if (!await _clearNativePart(link)) return false;
    final paused = item.state == LocalUploadState.pausedWifi.name;
    final updated =
        await (_database.update(_database.uploadItems)..where(
              (row) =>
                  row.localAssetId.equals(item.localAssetId) &
                  row.policyGeneration.equals(item.policyGeneration),
            ))
            .write(
              UploadItemsCompanion(
                state: Value<String>(
                  paused
                      ? LocalUploadState.pausedWifi.name
                      : LocalUploadState.awaitingParts.name,
                ),
                pausedFromState: Value<String?>(
                  paused ? LocalUploadState.awaitingParts.name : null,
                ),
                leaseOwner: const Value<String?>(null),
                leaseExpiresAt: const Value<DateTime?>(null),
              ),
            );
    if (updated != 1) throw const UploadLeaseLostException();
    return true;
  }

  static bool _matchesClaim(UploadItem item, UploadClaim claim) =>
      item.leaseOwner == claim.ownerId &&
      item.leaseGeneration == claim.leaseGeneration &&
      item.policyGeneration == claim.policyGeneration;

  static bool _hasStagedNativeFailure(UploadItem item) {
    final state = item.state;
    if (state == LocalUploadState.failedRetryable.name ||
        state == LocalUploadState.failureReportPending.name) {
      return true;
    }
    return state == LocalUploadState.pausedWifi.name &&
        (item.pausedFromState == LocalUploadState.failedRetryable.name ||
            item.pausedFromState == LocalUploadState.failureReportPending.name);
  }

  Future<UploadPart?> _nativePart(NativePartJobLink link) =>
      (_database.select(_database.uploadParts)..where(
            (row) =>
                row.localAssetId.equals(link.claim.localAssetId) &
                row.partNumber.equals(link.partNumber) &
                row.nativeJobId.equals(link.nativeJobId) &
                row.nativeLeaseOwner.equals(link.claim.ownerId) &
                row.nativeLeaseGeneration.equals(link.claim.leaseGeneration) &
                row.nativePolicyGeneration.equals(link.claim.policyGeneration),
          ))
          .getSingleOrNull();

  Future<bool> _clearNativePart(NativePartJobLink link) async {
    final updated =
        await (_database.update(_database.uploadParts)..where(
              (row) =>
                  row.localAssetId.equals(link.claim.localAssetId) &
                  row.partNumber.equals(link.partNumber) &
                  row.nativeJobId.equals(link.nativeJobId) &
                  row.nativeLeaseOwner.equals(link.claim.ownerId) &
                  row.nativeLeaseGeneration.equals(link.claim.leaseGeneration) &
                  row.nativePolicyGeneration.equals(
                    link.claim.policyGeneration,
                  ),
            ))
            .write(
              const UploadPartsCompanion(
                nativeJobId: Value<String?>(null),
                nativeLeaseOwner: Value<String?>(null),
                nativeLeaseGeneration: Value<int?>(null),
                nativePolicyGeneration: Value<int?>(null),
              ),
            );
    return updated == 1;
  }

  static void _validatePartResult(int partNumber, String etag) {
    if (partNumber <= 0 || etag.trim().isEmpty || etag.length > 256) {
      throw const UploadQueueException(
        code: 'invalid_transfer_result',
        safeMessage: '上传分片返回异常，请重试',
        retryable: true,
      );
    }
  }

  Future<UploadItem> _requireClaim(UploadClaim claim) async {
    final item =
        await (_database.select(_database.uploadItems)..where(
              (row) =>
                  row.localAssetId.equals(claim.localAssetId) &
                  row.leaseOwner.equals(claim.ownerId) &
                  row.leaseGeneration.equals(claim.leaseGeneration) &
                  row.policyGeneration.equals(claim.policyGeneration),
            ))
            .getSingleOrNull();
    if (item == null) throw const UploadLeaseLostException();
    return item;
  }

  Future<void> _writeClaimed(
    UploadClaim claim,
    UploadItemsCompanion values,
  ) async {
    final updated =
        await (_database.update(_database.uploadItems)..where(
              (row) =>
                  row.localAssetId.equals(claim.localAssetId) &
                  row.leaseOwner.equals(claim.ownerId) &
                  row.leaseGeneration.equals(claim.leaseGeneration) &
                  row.policyGeneration.equals(claim.policyGeneration),
            ))
            .write(values);
    if (updated != 1) throw const UploadLeaseLostException();
  }

  Future<bool> renewLease(
    UploadClaim claim, {
    required DateTime now,
    required Duration leaseDuration,
  }) async {
    final updated =
        await (_database.update(_database.uploadItems)..where(
              (row) =>
                  row.localAssetId.equals(claim.localAssetId) &
                  row.leaseOwner.equals(claim.ownerId) &
                  row.leaseGeneration.equals(claim.leaseGeneration) &
                  row.policyGeneration.equals(claim.policyGeneration) &
                  row.state.isNotIn(<String>[
                    LocalUploadState.uploaded.name,
                    LocalUploadState.duplicate.name,
                    LocalUploadState.failedPermanent.name,
                    LocalUploadState.pausedWifi.name,
                  ]),
            ))
            .write(
              UploadItemsCompanion(
                leaseExpiresAt: Value<DateTime?>(
                  now.toUtc().add(leaseDuration),
                ),
              ),
            );
    return updated == 1;
  }

  Future<void> markUploading(UploadClaim claim) {
    return _writeClaimed(
      claim,
      UploadItemsCompanion(
        state: Value<String>(LocalUploadState.uploading.name),
      ),
    );
  }

  Future<void> markAwaitingCompletion(UploadClaim claim) {
    return _writeClaimed(
      claim,
      UploadItemsCompanion(
        state: Value<String>(LocalUploadState.awaitingCompletion.name),
      ),
    );
  }

  Future<QueuedUploadItem?> nextRunnable(
    String taskId, {
    required String ownerId,
    required DateTime now,
    required Duration leaseDuration,
  }) {
    return _database.transaction(() async {
      final effectiveNow = now.toUtc();
      final leaseExpiresAt = effectiveNow.add(leaseDuration);
      final candidates =
          await (_database.select(_database.uploadItems)
                ..where(
                  (row) =>
                      row.taskId.equals(taskId) &
                      row.retryCount.isSmallerThanValue(8) &
                      row.state.isNotIn(<String>[
                        LocalUploadState.uploaded.name,
                        LocalUploadState.duplicate.name,
                        LocalUploadState.failedPermanent.name,
                        LocalUploadState.pausedWifi.name,
                        LocalUploadState.failureReportPending.name,
                      ]) &
                      (row.nextAttemptAt.isNull() |
                          row.nextAttemptAt.isSmallerOrEqualValue(
                            effectiveNow,
                          )) &
                      (row.leaseOwner.isNull() |
                          row.leaseExpiresAt.isNull() |
                          row.leaseExpiresAt.isSmallerOrEqualValue(
                            effectiveNow,
                          )),
                )
                ..orderBy(<OrderingTerm Function(UploadItems)>[
                  (row) => OrderingTerm.asc(row.localAssetId),
                ]))
              .get();
      if (candidates.isEmpty) return null;
      for (final candidate in candidates) {
        if (await _hasLinkedNativeJob(candidate.localAssetId)) continue;
        final claimed =
            await (_database.update(_database.uploadItems)..where(
                  (row) =>
                      row.localAssetId.equals(candidate.localAssetId) &
                      row.retryCount.equals(candidate.retryCount) &
                      row.leaseGeneration.equals(candidate.leaseGeneration) &
                      row.policyGeneration.equals(candidate.policyGeneration) &
                      (row.leaseOwner.isNull() |
                          row.leaseExpiresAt.isNull() |
                          row.leaseExpiresAt.isSmallerOrEqualValue(
                            effectiveNow,
                          )),
                ))
                .write(
                  UploadItemsCompanion(
                    retryCount: Value<int>(candidate.retryCount + 1),
                    nextAttemptAt: const Value<DateTime?>(null),
                    leaseOwner: Value<String?>(ownerId),
                    leaseExpiresAt: Value<DateTime?>(leaseExpiresAt),
                    leaseGeneration: Value<int>(candidate.leaseGeneration + 1),
                  ),
                );
        if (claimed != 1) continue;
        final item =
            await (_database.select(_database.uploadItems)..where(
                  (row) => row.localAssetId.equals(candidate.localAssetId),
                ))
                .getSingle();
        return _hydrate(item);
      }
      return null;
    });
  }

  Future<QueuedUploadItem?> claimExhausted(
    String taskId, {
    required String ownerId,
    required DateTime now,
    required Duration leaseDuration,
  }) {
    return _database.transaction(() async {
      final effectiveNow = now.toUtc();
      final candidates =
          await (_database.select(_database.uploadItems)
                ..where(
                  (row) =>
                      row.taskId.equals(taskId) &
                      row.retryCount.isBiggerOrEqualValue(8) &
                      row.state.isNotIn(<String>[
                        LocalUploadState.uploaded.name,
                        LocalUploadState.duplicate.name,
                        LocalUploadState.failedPermanent.name,
                        LocalUploadState.pausedWifi.name,
                        LocalUploadState.failureReportPending.name,
                      ]) &
                      (row.nextAttemptAt.isNull() |
                          row.nextAttemptAt.isSmallerOrEqualValue(
                            effectiveNow,
                          )) &
                      (row.leaseOwner.isNull() |
                          row.leaseExpiresAt.isNull() |
                          row.leaseExpiresAt.isSmallerOrEqualValue(
                            effectiveNow,
                          )),
                )
                ..orderBy(<OrderingTerm Function(UploadItems)>[
                  (row) => OrderingTerm.asc(row.localAssetId),
                ]))
              .get();
      if (candidates.isEmpty) return null;
      for (final candidate in candidates) {
        if (await _hasLinkedNativeJob(candidate.localAssetId)) continue;
        final updated =
            await (_database.update(_database.uploadItems)..where(
                  (row) =>
                      row.localAssetId.equals(candidate.localAssetId) &
                      row.retryCount.equals(candidate.retryCount) &
                      row.leaseGeneration.equals(candidate.leaseGeneration) &
                      row.policyGeneration.equals(candidate.policyGeneration) &
                      (row.leaseOwner.isNull() |
                          row.leaseExpiresAt.isNull() |
                          row.leaseExpiresAt.isSmallerOrEqualValue(
                            effectiveNow,
                          )),
                ))
                .write(
                  UploadItemsCompanion(
                    leaseOwner: Value<String?>(ownerId),
                    leaseExpiresAt: Value<DateTime?>(
                      effectiveNow.add(leaseDuration),
                    ),
                    leaseGeneration: Value<int>(candidate.leaseGeneration + 1),
                  ),
                );
        if (updated != 1) continue;
        final claimed =
            await (_database.select(_database.uploadItems)..where(
                  (row) => row.localAssetId.equals(candidate.localAssetId),
                ))
                .getSingle();
        return _hydrate(claimed);
      }
      return null;
    });
  }

  Future<bool> _hasLinkedNativeJob(String localAssetId) async =>
      await (_database.select(_database.uploadParts)..where(
            (part) =>
                part.localAssetId.equals(localAssetId) &
                part.nativeJobId.isNotNull(),
          ))
          .getSingleOrNull() !=
      null;

  Future<QueuedUploadItem?> claimFailureReport(
    String taskId, {
    required String ownerId,
    required DateTime now,
    required Duration leaseDuration,
  }) {
    return _database.transaction(() async {
      final effectiveNow = now.toUtc();
      final candidate =
          await (_database.select(_database.uploadItems)
                ..where(
                  (row) =>
                      row.taskId.equals(taskId) &
                      row.state.equals(
                        LocalUploadState.failureReportPending.name,
                      ) &
                      (row.failureReportNextAttemptAt.isNull() |
                          row.failureReportNextAttemptAt.isSmallerOrEqualValue(
                            effectiveNow,
                          )) &
                      (row.leaseOwner.isNull() |
                          row.leaseExpiresAt.isNull() |
                          row.leaseExpiresAt.isSmallerOrEqualValue(
                            effectiveNow,
                          )),
                )
                ..orderBy(<OrderingTerm Function(UploadItems)>[
                  (row) => OrderingTerm.asc(row.localAssetId),
                ])
                ..limit(1))
              .getSingleOrNull();
      if (candidate == null) return null;
      final updated =
          await (_database.update(_database.uploadItems)..where(
                (row) =>
                    row.localAssetId.equals(candidate.localAssetId) &
                    row.state.equals(
                      LocalUploadState.failureReportPending.name,
                    ) &
                    row.leaseGeneration.equals(candidate.leaseGeneration) &
                    row.policyGeneration.equals(candidate.policyGeneration) &
                    (row.leaseOwner.isNull() |
                        row.leaseExpiresAt.isNull() |
                        row.leaseExpiresAt.isSmallerOrEqualValue(effectiveNow)),
              ))
              .write(
                UploadItemsCompanion(
                  leaseOwner: Value<String?>(ownerId),
                  leaseExpiresAt: Value<DateTime?>(
                    effectiveNow.add(leaseDuration),
                  ),
                  leaseGeneration: Value<int>(candidate.leaseGeneration + 1),
                ),
              );
      if (updated != 1) return null;
      final claimed =
          await (_database.select(_database.uploadItems)..where(
                (row) => row.localAssetId.equals(candidate.localAssetId),
              ))
              .getSingle();
      return _hydrate(claimed);
    });
  }

  Future<QueuedUploadItem?> find(String localAssetId) async {
    final item = await (_database.select(
      _database.uploadItems,
    )..where((row) => row.localAssetId.equals(localAssetId))).getSingleOrNull();
    if (item == null) return null;
    return _hydrate(item);
  }

  Future<QueuedUploadItem> _hydrate(UploadItem item) async {
    final parts =
        await (_database.select(_database.uploadParts)
              ..where((part) => part.localAssetId.equals(item.localAssetId))
              ..orderBy(<OrderingTerm Function(UploadParts)>[
                (part) => OrderingTerm.asc(part.partNumber),
              ]))
            .get();
    return QueuedUploadItem(
      taskId: item.taskId,
      asset: _decodeRef(item.localRefJson),
      authority: _decodeAuthority(item),
      partSize: item.partSize,
      completedParts: <int, String>{
        for (final part in parts) part.partNumber: ?part.etag,
      },
      nativeJobs: <int, NativePartJobLink>{
        for (final part in parts)
          if (part.nativeJobId != null &&
              part.nativeLeaseOwner != null &&
              part.nativeLeaseGeneration != null &&
              part.nativePolicyGeneration != null)
            part.partNumber: NativePartJobLink(
              taskId: item.taskId,
              partNumber: part.partNumber,
              nativeJobId: part.nativeJobId!,
              claim: UploadClaim(
                localAssetId: item.localAssetId,
                ownerId: part.nativeLeaseOwner!,
                leaseGeneration: part.nativeLeaseGeneration!,
                policyGeneration: part.nativePolicyGeneration!,
              ),
            ),
      },
      state: LocalUploadState.values.byName(item.state),
      sha256: item.sha256,
      backendAssetId: item.backendAssetId,
      providerUploadId: item.providerUploadId,
      retryCount: item.retryCount,
      nextAttemptAt: item.nextAttemptAt,
      lastErrorCode: item.lastErrorCode,
      lastErrorMessage: item.lastErrorMessage,
      leaseOwner: item.leaseOwner,
      leaseExpiresAt: item.leaseExpiresAt,
      leaseGeneration: item.leaseGeneration,
      policyGeneration: item.policyGeneration,
      serverConfirmed: item.serverConfirmed,
      terminalSummaryAcknowledged: item.terminalSummaryAcknowledged,
      retryRequested: item.retryRequested,
      localRejected: item.localRejected,
      pausedFromState: item.pausedFromState == null
          ? null
          : LocalUploadState.values.byName(item.pausedFromState!),
      failureReportRetryCount: item.failureReportRetryCount,
      failureReportNextAttemptAt: item.failureReportNextAttemptAt,
    );
  }

  static UploadAuthority? _decodeAuthority(UploadItem item) {
    final bindingId = item.bindingId;
    final candidateId = item.candidateId;
    if (bindingId == null && candidateId == null) return null;
    if (bindingId == null || candidateId == null) {
      throw const UploadQueueException(
        code: 'invalid_upload_authority',
        safeMessage: '上传授权记录损坏，请重新导入',
        retryable: false,
      );
    }
    try {
      return UploadAuthority(bindingId: bindingId, candidateId: candidateId);
    } on FormatException {
      throw const UploadQueueException(
        code: 'invalid_upload_authority',
        safeMessage: '上传授权记录损坏，请重新导入',
        retryable: false,
      );
    }
  }

  Future<void> markHashed(UploadClaim claim, String sha256) {
    return _writeClaimed(
      claim,
      UploadItemsCompanion(
        sha256: Value<String?>(sha256),
        state: Value<String>(LocalUploadState.awaitingInitiation.name),
      ),
    );
  }

  Future<void> pause(String localAssetId) {
    return _database.transaction(() async {
      final item = await (_database.select(
        _database.uploadItems,
      )..where((row) => row.localAssetId.equals(localAssetId))).getSingle();
      await _pauseItem(item);
    });
  }

  Future<void> pauseTask(String taskId) {
    return _database.transaction(() async {
      final items = await (_database.select(
        _database.uploadItems,
      )..where((item) => item.taskId.equals(taskId))).get();
      for (final item in items) {
        await _pauseItem(item);
      }
    });
  }

  Future<void> _pauseItem(UploadItem item) async {
    if (item.state == LocalUploadState.uploaded.name ||
        item.state == LocalUploadState.duplicate.name ||
        item.state == LocalUploadState.failedPermanent.name ||
        item.state == LocalUploadState.pausedWifi.name) {
      return;
    }
    await (_database.update(
      _database.uploadItems,
    )..where((row) => row.localAssetId.equals(item.localAssetId))).write(
      UploadItemsCompanion(
        state: Value<String>(LocalUploadState.pausedWifi.name),
        pausedFromState: Value<String?>(item.state),
        policyGeneration: Value<int>(item.policyGeneration + 1),
        leaseOwner: const Value<String?>(null),
        leaseExpiresAt: const Value<DateTime?>(null),
      ),
    );
  }

  Future<void> resume(String localAssetId) {
    return _database.transaction(() async {
      final item = await (_database.select(
        _database.uploadItems,
      )..where((row) => row.localAssetId.equals(localAssetId))).getSingle();
      if (item.state != LocalUploadState.pausedWifi.name) return;
      await _writeResumedState(item);
    });
  }

  Future<void> resumeTask(String taskId) {
    return _database.transaction(() async {
      final items =
          await (_database.select(_database.uploadItems)..where(
                (item) =>
                    item.taskId.equals(taskId) &
                    item.state.equals(LocalUploadState.pausedWifi.name),
              ))
              .get();
      for (final item in items) {
        await _writeResumedState(item);
      }
    });
  }

  Future<void> _writeResumedState(UploadItem item) async {
    final LocalUploadState state;
    if (item.pausedFromState case final previous?) {
      final restored = LocalUploadState.values.byName(previous);
      state = restored == LocalUploadState.pausedWifi
          ? LocalUploadState.awaitingHash
          : restored;
    } else if (item.sha256 == null) {
      state = LocalUploadState.awaitingHash;
    } else if (item.backendAssetId == null) {
      state = LocalUploadState.awaitingInitiation;
    } else {
      final incompletePart =
          await (_database.select(_database.uploadParts)
                ..where(
                  (part) =>
                      part.localAssetId.equals(item.localAssetId) &
                      part.etag.isNull(),
                )
                ..limit(1))
              .getSingleOrNull();
      state = incompletePart == null
          ? LocalUploadState.awaitingCompletion
          : LocalUploadState.awaitingParts;
    }
    await (_database.update(
      _database.uploadItems,
    )..where((row) => row.localAssetId.equals(item.localAssetId))).write(
      UploadItemsCompanion(
        state: Value<String>(state.name),
        pausedFromState: const Value<String?>(null),
      ),
    );
  }

  Future<void> markInitiated(UploadClaim claim, InitiatedAsset initiated) {
    return _database.transaction(() async {
      final item = await _requireClaim(claim);
      final localAssetId = claim.localAssetId;
      if (!initiated.alreadyUploaded &&
          (initiated.providerUploadId == null ||
              initiated.providerUploadId!.isEmpty)) {
        throw const UploadQueueException(
          code: 'upload_initialization_pending',
          safeMessage: '上传正在准备，请稍后重试',
          retryable: true,
        );
      }
      var parts = await (_database.select(
        _database.uploadParts,
      )..where((part) => part.localAssetId.equals(localAssetId))).get();
      if (item.partSize != initiated.partSize) {
        if (parts.any((part) => part.etag != null)) {
          throw const UploadQueueException(
            code: 'part_size_changed',
            safeMessage: '上传分片规格已变更，请重试',
            retryable: true,
          );
        }
        await (_database.delete(
          _database.uploadParts,
        )..where((part) => part.localAssetId.equals(localAssetId))).go();
        final partCount =
            (item.byteSize + initiated.partSize - 1) ~/ initiated.partSize;
        await _database.batch((batch) {
          batch.insertAll(_database.uploadParts, <UploadPartsCompanion>[
            for (var number = 1; number <= partCount; number += 1)
              UploadPartsCompanion.insert(
                localAssetId: localAssetId,
                partNumber: number,
              ),
          ]);
        });
        parts = await (_database.select(
          _database.uploadParts,
        )..where((part) => part.localAssetId.equals(localAssetId))).get();
      }
      final completedNumbers = initiated.completedPartNumbers;
      if (completedNumbers.toSet().length != completedNumbers.length) {
        throw const UploadQueueException(
          code: 'invalid_response',
          safeMessage: '服务返回异常，请稍后重试',
          retryable: true,
        );
      }
      final localParts = <int, UploadPart>{
        for (final part in parts) part.partNumber: part,
      };
      for (final partNumber in completedNumbers) {
        final localPart = localParts[partNumber];
        if (localPart == null) {
          throw const UploadQueueException(
            code: 'invalid_response',
            safeMessage: '服务返回异常，请稍后重试',
            retryable: true,
          );
        }
        if (localPart.etag == null) {
          throw const UploadQueueException(
            code: 'server_part_etag_missing',
            safeMessage: '无法安全恢复上传分片，请重试',
            retryable: true,
          );
        }
      }
      await _writeClaimed(
        claim,
        UploadItemsCompanion(
          backendAssetId: Value<String?>(initiated.assetId),
          providerUploadId: Value<String?>(initiated.providerUploadId),
          partSize: Value<int>(initiated.partSize),
          state: Value<String>(LocalUploadState.awaitingParts.name),
          retryRequested: const Value<bool>(false),
          localRejected: const Value<bool>(false),
        ),
      );
    });
  }

  Future<QueuedUploadItem> markAlreadyUploaded(
    UploadClaim claim,
    InitiatedAsset initiated,
  ) {
    return _database.transaction(() async {
      if (!initiated.alreadyUploaded ||
          initiated.assetId.isEmpty ||
          initiated.partSize <= 0) {
        throw const UploadQueueException(
          code: 'invalid_response',
          safeMessage: '服务返回异常，请稍后重试',
          retryable: true,
        );
      }
      final item = await _requireClaim(claim);
      await _writeClaimed(
        claim,
        UploadItemsCompanion(
          backendAssetId: Value<String?>(initiated.assetId),
          providerUploadId: Value<String?>(initiated.providerUploadId),
          partSize: Value<int>(initiated.partSize),
          state: Value<String>(LocalUploadState.uploaded.name),
          nextAttemptAt: const Value<DateTime?>(null),
          lastErrorCode: const Value<String?>(null),
          lastErrorMessage: const Value<String?>(null),
          leaseOwner: const Value<String?>(null),
          leaseExpiresAt: const Value<DateTime?>(null),
          serverConfirmed: const Value<bool>(true),
          retryRequested: const Value<bool>(false),
          localRejected: const Value<bool>(false),
        ),
      );
      await _database
          .into(_database.importCleanupRefs)
          .insertOnConflictUpdate(
            ImportCleanupRefsCompanion.insert(
              taskId: item.taskId,
              locator: _decodeRef(item.localRefJson).locator,
              localAssetId: item.localAssetId,
              localRefJson: item.localRefJson,
            ),
          );
      final committed =
          await (_database.select(_database.uploadItems)
                ..where((row) => row.localAssetId.equals(claim.localAssetId)))
              .getSingle();
      return _hydrate(committed);
    });
  }

  Future<QueuedUploadItem> markDuplicate(
    UploadClaim claim,
    InitiatedAsset initiated,
  ) {
    return _database.transaction(() async {
      if (initiated.disposition != InitiatedAssetDisposition.duplicate ||
          !initiated.alreadyUploaded ||
          initiated.assetId.isEmpty ||
          initiated.canonicalAssetId == null ||
          initiated.canonicalAssetId!.isEmpty ||
          initiated.canonicalAssetId == initiated.assetId ||
          initiated.providerUploadId != null ||
          initiated.completedPartNumbers.isNotEmpty ||
          initiated.partSize <= 0) {
        throw const UploadQueueException(
          code: 'invalid_response',
          safeMessage: '服务返回异常，请稍后重试',
          retryable: true,
        );
      }
      final item = await _requireClaim(claim);
      await (_database.delete(
        _database.uploadParts,
      )..where((part) => part.localAssetId.equals(item.localAssetId))).go();
      await _writeClaimed(
        claim,
        UploadItemsCompanion(
          backendAssetId: Value<String?>(initiated.assetId),
          providerUploadId: const Value<String?>(null),
          partSize: Value<int>(initiated.partSize),
          state: Value<String>(LocalUploadState.duplicate.name),
          nextAttemptAt: const Value<DateTime?>(null),
          lastErrorCode: const Value<String?>(null),
          lastErrorMessage: const Value<String?>(null),
          leaseOwner: const Value<String?>(null),
          leaseExpiresAt: const Value<DateTime?>(null),
          serverConfirmed: const Value<bool>(true),
          terminalSummaryAcknowledged: const Value<bool>(true),
          retryRequested: const Value<bool>(false),
          localRejected: const Value<bool>(false),
        ),
      );
      await _database
          .into(_database.importCleanupRefs)
          .insertOnConflictUpdate(
            ImportCleanupRefsCompanion.insert(
              taskId: item.taskId,
              locator: _decodeRef(item.localRefJson).locator,
              localAssetId: item.localAssetId,
              localRefJson: item.localRefJson,
            ),
          );
      final committed =
          await (_database.select(_database.uploadItems)
                ..where((row) => row.localAssetId.equals(claim.localAssetId)))
              .getSingle();
      return _hydrate(committed);
    });
  }

  Future<ServerSummaryFence> captureServerSummaryFence(String taskId) async {
    final items = await (_database.select(
      _database.uploadItems,
    )..where((item) => item.taskId.equals(taskId))).get();
    final accepted =
        await (_database.select(_database.importQueueAuthorities)..where(
              (row) =>
                  row.taskId.equals(taskId) &
                  row.disposition.equals(ServerAssetDisposition.accepted.name),
            ))
            .get();
    final acceptedIds = accepted.map((row) => row.localAssetId).toSet();

    ServerSummaryReceipt receipt(UploadItem item) => ServerSummaryReceipt(
      localAssetId: item.localAssetId,
      backendAssetId: item.backendAssetId!,
      leaseGeneration: item.leaseGeneration,
      policyGeneration: item.policyGeneration,
      state: LocalUploadState.values.byName(item.state),
    );

    return ServerSummaryFence(
      taskId: taskId,
      acceptanceReceipts: List<ServerSummaryReceipt>.unmodifiable(
        items
            .where(
              (item) =>
                  item.backendAssetId != null &&
                  item.state != LocalUploadState.duplicate.name &&
                  !acceptedIds.contains(item.localAssetId),
            )
            .map(receipt),
      ),
      terminalReceipts: List<ServerSummaryReceipt>.unmodifiable(
        items
            .where(
              (item) =>
                  item.backendAssetId != null &&
                  item.serverConfirmed &&
                  !item.terminalSummaryAcknowledged &&
                  (item.state == LocalUploadState.uploaded.name ||
                      item.state == LocalUploadState.failedPermanent.name),
            )
            .map(receipt),
      ),
    );
  }

  Future<void> acknowledgeServerSummary(
    ServerSummaryFence fence,
    TaskSummary summary,
  ) {
    return _database.transaction(() async {
      if (summary.id != fence.taskId ||
          summary.totalAssets < 0 ||
          summary.uploadedAssets < 0 ||
          summary.failedAssets < 0) {
        throw const FormatException('invalid task summary');
      }
      final items = await (_database.select(
        _database.uploadItems,
      )..where((item) => item.taskId.equals(fence.taskId))).get();
      final itemsById = <String, UploadItem>{
        for (final item in items) item.localAssetId: item,
      };
      final authorities = await (_database.select(
        _database.importQueueAuthorities,
      )..where((row) => row.taskId.equals(fence.taskId))).get();
      final acceptedIds = authorities
          .where(
            (row) => row.disposition == ServerAssetDisposition.accepted.name,
          )
          .map((row) => row.localAssetId)
          .toSet();
      final pendingAcceptance = fence.acceptanceReceipts
          .where((receipt) {
            final item = itemsById[receipt.localAssetId];
            return item != null &&
                _matchesAcceptanceReceipt(item, receipt) &&
                !acceptedIds.contains(item.localAssetId);
          })
          .toList(growable: false);
      if (summary.totalAssets >=
          acceptedIds.length + pendingAcceptance.length) {
        for (final receipt in pendingAcceptance) {
          await _database
              .into(_database.importQueueAuthorities)
              .insertOnConflictUpdate(
                ImportQueueAuthoritiesCompanion.insert(
                  taskId: fence.taskId,
                  localAssetId: receipt.localAssetId,
                  disposition: ServerAssetDisposition.accepted.name,
                ),
              );
          acceptedIds.add(receipt.localAssetId);
        }
      }

      for (final receipt in fence.terminalReceipts) {
        final item = itemsById[receipt.localAssetId];
        if (item == null ||
            !_matchesReceipt(item, receipt) ||
            !item.serverConfirmed ||
            item.terminalSummaryAcknowledged ||
            !acceptedIds.contains(item.localAssetId)) {
          continue;
        }
        await (_database.update(_database.uploadItems)..where(
              (row) =>
                  row.localAssetId.equals(receipt.localAssetId) &
                  row.backendAssetId.equals(receipt.backendAssetId) &
                  row.leaseGeneration.equals(receipt.leaseGeneration) &
                  row.policyGeneration.equals(receipt.policyGeneration) &
                  row.state.equals(receipt.state.name) &
                  row.serverConfirmed.equals(true) &
                  row.terminalSummaryAcknowledged.equals(false),
            ))
            .write(
              const UploadItemsCompanion(
                terminalSummaryAcknowledged: Value<bool>(true),
              ),
            );
      }
    });
  }

  Future<bool> hasPendingServerAcknowledgement(String taskId) async {
    final items = await (_database.select(
      _database.uploadItems,
    )..where((item) => item.taskId.equals(taskId))).get();
    if (items.isEmpty) return false;
    final accepted =
        await (_database.select(_database.importQueueAuthorities)..where(
              (row) =>
                  row.taskId.equals(taskId) &
                  row.disposition.equals(ServerAssetDisposition.accepted.name),
            ))
            .get();
    final acceptedIds = accepted.map((row) => row.localAssetId).toSet();
    return items.any(
      (item) =>
          item.state != LocalUploadState.duplicate.name &&
              item.backendAssetId != null &&
              !acceptedIds.contains(item.localAssetId) ||
          item.serverConfirmed && !item.terminalSummaryAcknowledged,
    );
  }

  Future<bool> claimSeal(
    String taskId, {
    required String ownerId,
    required DateTime now,
    required Duration leaseDuration,
  }) {
    return _database.transaction(() async {
      final effectiveNow = now.toUtc();
      final task = await (_database.select(
        _database.importQueueTasks,
      )..where((row) => row.taskId.equals(taskId))).getSingleOrNull();
      if (task == null ||
          !task.sealWhenIdle ||
          task.serverSealed ||
          task.sealNextAttemptAt?.toUtc().isAfter(effectiveNow) == true ||
          (task.sealLeaseOwner != null &&
              task.sealLeaseExpiresAt?.toUtc().isAfter(effectiveNow) == true)) {
        return false;
      }
      final nonTerminal = await (_database.select(
        _database.uploadItems,
      )..where((item) => item.taskId.equals(taskId))).get();
      if (nonTerminal.any((item) {
        final isLocalRejection =
            item.state == LocalUploadState.failedPermanent.name &&
            item.localRejected;
        return item.state != LocalUploadState.uploaded.name &&
            item.state != LocalUploadState.duplicate.name &&
            !isLocalRejection;
      })) {
        return false;
      }
      final updated =
          await (_database.update(_database.importQueueTasks)..where(
                (row) =>
                    row.taskId.equals(taskId) &
                    row.serverSealed.equals(false) &
                    (row.sealLeaseOwner.isNull() |
                        row.sealLeaseExpiresAt.isNull() |
                        row.sealLeaseExpiresAt.isSmallerOrEqualValue(
                          effectiveNow,
                        )),
              ))
              .write(
                ImportQueueTasksCompanion(
                  sealLeaseOwner: Value<String?>(ownerId),
                  sealLeaseExpiresAt: Value<DateTime?>(
                    effectiveNow.add(leaseDuration),
                  ),
                ),
              );
      return updated == 1;
    });
  }

  Future<void> markServerSealed(
    String taskId,
    String ownerId, {
    DateTime? now,
  }) async {
    final updated =
        await (_database.update(_database.importQueueTasks)..where(
              (row) =>
                  row.taskId.equals(taskId) &
                  row.sealLeaseOwner.equals(ownerId),
            ))
            .write(
              ImportQueueTasksCompanion(
                serverSealed: const Value<bool>(true),
                sealConfirmedAt: Value<DateTime?>(
                  (now ?? DateTime.now()).toUtc(),
                ),
                sealLeaseOwner: const Value<String?>(null),
                sealLeaseExpiresAt: const Value<DateTime?>(null),
                sealNextAttemptAt: const Value<DateTime?>(null),
                sealLastErrorCode: const Value<String?>(null),
              ),
            );
    if (updated != 1) {
      throw StateError('task seal lease was lost');
    }
  }

  Future<void> scheduleSealRetry(
    String taskId,
    String ownerId, {
    required DateTime now,
    required String code,
  }) {
    return _database.transaction(() async {
      final task = await (_database.select(
        _database.importQueueTasks,
      )..where((row) => row.taskId.equals(taskId))).getSingle();
      if (task.sealLeaseOwner != ownerId) return;
      final retryCount = task.sealRetryCount + 1;
      final delay =
          _sealRetryDelays[(retryCount - 1).clamp(
            0,
            _sealRetryDelays.length - 1,
          )];
      await (_database.update(
        _database.importQueueTasks,
      )..where((row) => row.taskId.equals(taskId))).write(
        ImportQueueTasksCompanion(
          sealRetryCount: Value<int>(retryCount),
          sealNextAttemptAt: Value<DateTime?>(now.toUtc().add(delay)),
          sealLastErrorCode: Value<String?>(code),
          sealLeaseOwner: const Value<String?>(null),
          sealLeaseExpiresAt: const Value<DateTime?>(null),
        ),
      );
    });
  }

  Future<void> scheduleRetry(
    UploadClaim claim, {
    required int retryCount,
    required DateTime nextAttemptAt,
    required String code,
    required String safeMessage,
  }) {
    return _writeClaimed(
      claim,
      UploadItemsCompanion(
        retryCount: Value<int>(retryCount),
        nextAttemptAt: Value<DateTime?>(nextAttemptAt.toUtc()),
        state: Value<String>(LocalUploadState.failedRetryable.name),
        lastErrorCode: Value<String?>(code),
        lastErrorMessage: Value<String?>(safeMessage),
        leaseOwner: const Value<String?>(null),
        leaseExpiresAt: const Value<DateTime?>(null),
      ),
    );
  }

  Future<void> markFailureReportPending(
    UploadClaim claim, {
    required DateTime now,
    required String code,
    required String safeMessage,
  }) {
    return _writeClaimed(
      claim,
      UploadItemsCompanion(
        state: Value<String>(LocalUploadState.failureReportPending.name),
        nextAttemptAt: const Value<DateTime?>(null),
        lastErrorCode: Value<String?>(code),
        lastErrorMessage: Value<String?>(safeMessage),
        failureReportRetryCount: const Value<int>(1),
        failureReportNextAttemptAt: Value<DateTime?>(
          now.toUtc().add(_failureReportRetryDelays.first),
        ),
        leaseOwner: const Value<String?>(null),
        leaseExpiresAt: const Value<DateTime?>(null),
      ),
    );
  }

  Future<void> scheduleFailureReportRetry(
    UploadClaim claim, {
    required DateTime now,
  }) {
    return _database.transaction(() async {
      final item = await _requireClaim(claim);
      final retryCount = item.failureReportRetryCount + 1;
      final delay =
          _failureReportRetryDelays[(retryCount - 1).clamp(
            0,
            _failureReportRetryDelays.length - 1,
          )];
      await _writeClaimed(
        claim,
        UploadItemsCompanion(
          state: Value<String>(LocalUploadState.failureReportPending.name),
          failureReportRetryCount: Value<int>(retryCount),
          failureReportNextAttemptAt: Value<DateTime?>(now.toUtc().add(delay)),
          leaseOwner: const Value<String?>(null),
          leaseExpiresAt: const Value<DateTime?>(null),
        ),
      );
    });
  }

  Future<void> markFailureReported(
    UploadClaim claim, {
    required String code,
    required String safeMessage,
  }) {
    return _database.transaction(() async {
      await _requireClaim(claim);
      await _writeClaimed(
        claim,
        UploadItemsCompanion(
          state: Value<String>(LocalUploadState.failedPermanent.name),
          nextAttemptAt: const Value<DateTime?>(null),
          lastErrorCode: Value<String?>(code),
          lastErrorMessage: Value<String?>(safeMessage),
          failureReportNextAttemptAt: const Value<DateTime?>(null),
          leaseOwner: const Value<String?>(null),
          leaseExpiresAt: const Value<DateTime?>(null),
          serverConfirmed: const Value<bool>(true),
          terminalSummaryAcknowledged: const Value<bool>(false),
          localRejected: const Value<bool>(false),
        ),
      );
    });
  }

  Future<void> markFailureReportRejected(
    UploadClaim claim, {
    required String code,
    required String safeMessage,
  }) {
    return _database.transaction(() async {
      await _requireClaim(claim);
      await _writeClaimed(
        claim,
        UploadItemsCompanion(
          state: Value<String>(LocalUploadState.failedPermanent.name),
          nextAttemptAt: const Value<DateTime?>(null),
          lastErrorCode: Value<String?>(code),
          lastErrorMessage: Value<String?>(safeMessage),
          failureReportNextAttemptAt: const Value<DateTime?>(null),
          leaseOwner: const Value<String?>(null),
          leaseExpiresAt: const Value<DateTime?>(null),
          serverConfirmed: const Value<bool>(true),
          terminalSummaryAcknowledged: const Value<bool>(true),
          localRejected: const Value<bool>(false),
        ),
      );
    });
  }

  Future<void> markFailureResolvedBySealedTask(
    UploadClaim claim, {
    required String code,
    required String safeMessage,
  }) {
    return _database.transaction(() async {
      final item = await _requireClaim(claim);
      await _writeClaimed(
        claim,
        UploadItemsCompanion(
          state: Value<String>(LocalUploadState.failedPermanent.name),
          nextAttemptAt: const Value<DateTime?>(null),
          lastErrorCode: Value<String?>(code),
          lastErrorMessage: Value<String?>(safeMessage),
          failureReportNextAttemptAt: const Value<DateTime?>(null),
          leaseOwner: const Value<String?>(null),
          leaseExpiresAt: const Value<DateTime?>(null),
          serverConfirmed: const Value<bool>(true),
          terminalSummaryAcknowledged: const Value<bool>(true),
          localRejected: const Value<bool>(false),
        ),
      );
      await _database
          .into(_database.importQueueTasks)
          .insertOnConflictUpdate(
            ImportQueueTasksCompanion.insert(
              taskId: item.taskId,
              serverSealed: const Value<bool>(true),
              sealLeaseOwner: const Value<String?>(null),
              sealLeaseExpiresAt: const Value<DateTime?>(null),
              sealNextAttemptAt: const Value<DateTime?>(null),
              sealLastErrorCode: const Value<String?>(null),
            ),
          );
      await _database
          .into(_database.importCleanupRefs)
          .insertOnConflictUpdate(
            ImportCleanupRefsCompanion.insert(
              taskId: item.taskId,
              locator: _decodeRef(item.localRefJson).locator,
              localAssetId: item.localAssetId,
              localRefJson: item.localRefJson,
            ),
          );
    });
  }

  Future<void> requestFailedRetry(String taskId, String localAssetId) {
    return _database.transaction(() async {
      final item =
          await (_database.select(_database.uploadItems)..where(
                (row) =>
                    row.taskId.equals(taskId) &
                    row.localAssetId.equals(localAssetId) &
                    row.state.equals(LocalUploadState.failedPermanent.name) &
                    row.localRejected.equals(false),
              ))
              .getSingleOrNull();
      final task = await (_database.select(
        _database.importQueueTasks,
      )..where((row) => row.taskId.equals(taskId))).getSingleOrNull();
      if (item == null || task?.serverSealed == true) {
        throw StateError('failed upload is not retryable');
      }
      final updated =
          await (_database.update(_database.uploadItems)..where(
                (row) =>
                    row.localAssetId.equals(localAssetId) &
                    row.state.equals(LocalUploadState.failedPermanent.name) &
                    row.leaseGeneration.equals(item.leaseGeneration) &
                    row.policyGeneration.equals(item.policyGeneration),
              ))
              .write(
                UploadItemsCompanion(
                  backendAssetId: const Value<String?>(null),
                  providerUploadId: const Value<String?>(null),
                  retryCount: const Value<int>(0),
                  nextAttemptAt: const Value<DateTime?>(null),
                  state: Value<String>(
                    (item.sha256 == null
                            ? LocalUploadState.awaitingHash
                            : LocalUploadState.awaitingInitiation)
                        .name,
                  ),
                  lastErrorCode: const Value<String?>(null),
                  lastErrorMessage: const Value<String?>(null),
                  leaseOwner: const Value<String?>(null),
                  leaseExpiresAt: const Value<DateTime?>(null),
                  leaseGeneration: Value<int>(item.leaseGeneration + 1),
                  policyGeneration: Value<int>(item.policyGeneration + 1),
                  serverConfirmed: const Value<bool>(false),
                  terminalSummaryAcknowledged: const Value<bool>(false),
                  retryRequested: const Value<bool>(true),
                  localRejected: const Value<bool>(false),
                  pausedFromState: const Value<String?>(null),
                  failureReportRetryCount: const Value<int>(0),
                  failureReportNextAttemptAt: const Value<DateTime?>(null),
                ),
              );
      if (updated != 1) throw const UploadLeaseLostException();
      await (_database.update(_database.uploadParts)
            ..where((part) => part.localAssetId.equals(localAssetId)))
          .write(const UploadPartsCompanion(etag: Value<String?>(null)));
      await (_database.delete(_database.importCleanupRefs)..where(
            (row) =>
                row.taskId.equals(taskId) &
                row.localAssetId.equals(localAssetId),
          ))
          .go();
    });
  }

  Future<void> markLocalRejected(
    UploadClaim claim, {
    required int retryCount,
    required String code,
    required String safeMessage,
  }) {
    return _database.transaction(() async {
      final item = await _requireClaim(claim);
      await _writeClaimed(
        claim,
        UploadItemsCompanion(
          retryCount: Value<int>(retryCount),
          nextAttemptAt: const Value<DateTime?>(null),
          state: Value<String>(LocalUploadState.failedPermanent.name),
          lastErrorCode: Value<String?>(code),
          lastErrorMessage: Value<String?>(safeMessage),
          leaseOwner: const Value<String?>(null),
          leaseExpiresAt: const Value<DateTime?>(null),
          localRejected: const Value<bool>(true),
        ),
      );
      await _database
          .into(_database.importCleanupRefs)
          .insertOnConflictUpdate(
            ImportCleanupRefsCompanion.insert(
              taskId: item.taskId,
              locator: _decodeRef(item.localRefJson).locator,
              localAssetId: item.localAssetId,
              localRefJson: item.localRefJson,
            ),
          );
    });
  }

  Future<void> markFailed(
    UploadClaim claim, {
    required int retryCount,
    required String code,
    required String safeMessage,
    bool serverConfirmed = false,
  }) {
    return _database.transaction(() async {
      await _requireClaim(claim);
      await _writeClaimed(
        claim,
        UploadItemsCompanion(
          retryCount: Value<int>(retryCount),
          nextAttemptAt: const Value<DateTime?>(null),
          state: Value<String>(LocalUploadState.failedPermanent.name),
          lastErrorCode: Value<String?>(code),
          lastErrorMessage: Value<String?>(safeMessage),
          leaseOwner: const Value<String?>(null),
          leaseExpiresAt: const Value<DateTime?>(null),
          serverConfirmed: Value<bool>(serverConfirmed),
          localRejected: const Value<bool>(false),
        ),
      );
    });
  }

  Future<void> markUploaded(UploadClaim claim) {
    return _database.transaction(() async {
      final item = await _requireClaim(claim);
      await _writeClaimed(
        claim,
        UploadItemsCompanion(
          state: Value<String>(LocalUploadState.uploaded.name),
          nextAttemptAt: const Value<DateTime?>(null),
          lastErrorCode: const Value<String?>(null),
          lastErrorMessage: const Value<String?>(null),
          leaseOwner: const Value<String?>(null),
          leaseExpiresAt: const Value<DateTime?>(null),
          serverConfirmed: const Value<bool>(true),
          localRejected: const Value<bool>(false),
        ),
      );
      await _database
          .into(_database.importCleanupRefs)
          .insertOnConflictUpdate(
            ImportCleanupRefsCompanion.insert(
              taskId: item.taskId,
              locator: _decodeRef(item.localRefJson).locator,
              localAssetId: item.localAssetId,
              localRefJson: item.localRefJson,
            ),
          );
    });
  }

  Stream<LocalUploadProgress> progress(String taskId) =>
      (_database.select(
        _database.uploadItems,
      )..where((item) => item.taskId.equals(taskId))).watch().map((items) {
        int count(LocalUploadState state) =>
            items.where((item) => item.state == state.name).length;
        final uploaded = count(LocalUploadState.uploaded);
        final failed = count(LocalUploadState.failedPermanent);
        final uploading = count(LocalUploadState.uploading);
        final duplicates = count(LocalUploadState.duplicate);
        return LocalUploadProgress(
          taskId: taskId,
          total: items.length,
          queued: items.length - uploaded - failed - uploading - duplicates,
          uploading: uploading,
          uploaded: uploaded,
          failed: failed,
          hashed: items.length - count(LocalUploadState.awaitingHash),
          waitingWifi: count(LocalUploadState.pausedWifi),
          duplicates: duplicates,
        );
      });

  @override
  Future<ImportQueueSnapshot> retainCleanupRefs(
    String taskId,
    List<LocalAssetRef> assets,
  ) {
    return _database.transaction(() async {
      for (final asset in assets) {
        await _database
            .into(_database.importCleanupRefs)
            .insertOnConflictUpdate(
              ImportCleanupRefsCompanion.insert(
                taskId: taskId,
                locator: asset.locator,
                localAssetId: asset.id,
                localRefJson: jsonEncode(asset.toMap()),
              ),
            );
      }
      return snapshot(taskId);
    });
  }

  @override
  Future<ImportQueueSnapshot> completeCleanupRef(
    String taskId,
    LocalAssetRef asset,
  ) {
    return _database.transaction(() async {
      await (_database.delete(_database.importCleanupRefs)..where(
            (row) =>
                row.taskId.equals(taskId) & row.locator.equals(asset.locator),
          ))
          .go();
      return snapshot(taskId);
    });
  }

  @override
  Future<void> setSealWhenIdle(String taskId) async {
    await _database.transaction(() async {
      final existing = await (_database.select(
        _database.importQueueTasks,
      )..where((row) => row.taskId.equals(taskId))).getSingleOrNull();
      await _database
          .into(_database.importQueueTasks)
          .insertOnConflictUpdate(
            ImportQueueTasksCompanion.insert(
              taskId: taskId,
              sealWhenIdle: const Value<bool>(true),
              sealRequestedAt: Value<DateTime?>(
                existing?.sealRequestedAt ?? DateTime.now().toUtc(),
              ),
            ),
          );
    });
    _workAvailable.add(taskId);
  }
}

bool _itemNeedsWork(UploadItem item, bool serverAccepted) {
  if (item.state == LocalUploadState.duplicate.name) return false;
  final terminal =
      item.state == LocalUploadState.uploaded.name ||
      item.state == LocalUploadState.failedPermanent.name;
  if (!terminal) return true;
  if (item.localRejected || item.backendAssetId == null) return false;
  if (!serverAccepted) return true;
  return item.serverConfirmed && !item.terminalSummaryAcknowledged;
}

LocalAssetRef _decodeRef(String json) => LocalAssetRef.fromMap(
  (jsonDecode(json) as Map<Object?, Object?>).map(
    (key, value) => MapEntry(key.toString(), value),
  ),
);

bool _matchesAcceptanceReceipt(UploadItem item, ServerSummaryReceipt receipt) =>
    item.localAssetId == receipt.localAssetId &&
    item.backendAssetId == receipt.backendAssetId &&
    item.leaseGeneration == receipt.leaseGeneration &&
    item.policyGeneration == receipt.policyGeneration;

bool _matchesReceipt(UploadItem item, ServerSummaryReceipt receipt) =>
    _matchesAcceptanceReceipt(item, receipt) &&
    item.state == receipt.state.name;

const _sealRetryDelays = <Duration>[
  Duration(seconds: 2),
  Duration(seconds: 10),
  Duration(seconds: 30),
  Duration(minutes: 2),
  Duration(minutes: 5),
  Duration(minutes: 15),
  Duration(minutes: 30),
  Duration(minutes: 60),
];

const _failureReportRetryDelays = <Duration>[
  Duration(seconds: 2),
  Duration(seconds: 10),
  Duration(seconds: 30),
  Duration(minutes: 2),
  Duration(minutes: 5),
  Duration(minutes: 15),
  Duration(minutes: 30),
  Duration(minutes: 60),
];
