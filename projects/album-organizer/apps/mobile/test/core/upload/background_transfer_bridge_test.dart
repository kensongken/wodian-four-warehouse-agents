import 'dart:async';

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/models.dart';
import 'package:photo_organizer/core/media/local_asset_ref.dart';
import 'package:photo_organizer/core/media/media_library_bridge.dart';
import 'package:photo_organizer/core/upload/background_transfer_bridge.dart';
import 'package:photo_organizer/core/upload/part_transfer_client.dart';
import 'package:photo_organizer/core/upload/upload_api.dart';
import 'package:photo_organizer/core/upload/upload_coordinator.dart';
import 'package:photo_organizer/core/upload/upload_database.dart';
import 'package:photo_organizer/core/upload/upload_models.dart';
import 'package:photo_organizer/core/upload/upload_queue_repository.dart';
import 'package:photo_organizer/features/tasks/task_repository.dart';

const _taskId = '11111111-1111-4111-8111-111111111111';
const _token =
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const _asset = LocalAssetRef(
  id: 'ios:$_token',
  displayName: 'part.JPG',
  mediaType: 'image/jpeg',
  byteSize: 1024 * 1024,
  accessKind: AssetAccessKind.stagedFile,
  locator: '$_token/11111111-1111-4111-8111-111111111111/original',
);
const _asset17MiB = LocalAssetRef(
  id: 'ios:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
  displayName: 'three-parts.JPG',
  mediaType: 'image/jpeg',
  byteSize: 17 * 1024 * 1024,
  accessKind: AssetAccessKind.stagedFile,
  locator:
      'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/'
      '22222222-2222-4222-8222-222222222222/original',
);

void main() {
  test(
    'reconciles native success delivered while Flutter was terminated',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(queue, ownerId: 'terminated-owner');
      await queue.linkNativeJob(claim, 1, 'native:${_asset.id}:1');
      final native = _FakeBackgroundTransferBridge();
      native.jobs['native:${_asset.id}:1'] = NativeTransferJob(
        jobId: 'native:${_asset.id}:1',
        state: NativeTransferState.succeeded,
        etag: 'etag-after-termination',
      );
      final coordinator = _coordinator(queue: queue, native: native);
      addTearDown(coordinator.dispose);

      await coordinator.reconcileNativeJobs(_taskId);

      expect((await queue.find(_asset.id))!.completedParts, <int, String>{
        1: 'etag-after-termination',
      });
      expect(native.acknowledged, <String>['native:${_asset.id}:1']);
    },
  );

  test('a missing native job returns only its part to awaitingParts', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final claim = await _seedNativePart(queue, ownerId: 'missing-owner');
    await queue.linkNativeJob(claim, 1, 'native:${_asset.id}:1');
    final native = _FakeBackgroundTransferBridge();
    final coordinator = _coordinator(queue: queue, native: native);
    addTearDown(coordinator.dispose);

    await coordinator.reconcileNativeJobs(_taskId);

    final restored = (await queue.find(_asset.id))!;
    expect(restored.state, LocalUploadState.awaitingParts);
    expect(restored.completedParts, isEmpty);
    expect(restored.nativeJobs, isEmpty);
    expect(native.acknowledged, isEmpty);
  });

  test('a missing part cancels every running sibling before retry', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final claim = await _seedThreeNativeParts(queue);
    final native = _FakeBackgroundTransferBridge();
    for (var part = 1; part <= 3; part += 1) {
      final jobId = 'native:${_asset17MiB.id}:$part';
      await queue.linkNativeJob(claim, part, jobId);
      if (part > 1) {
        native.jobs[jobId] = NativeTransferJob(
          jobId: jobId,
          state: NativeTransferState.running,
        );
      }
    }
    final coordinator = _coordinator(queue: queue, native: native);
    addTearDown(coordinator.dispose);

    await coordinator.reconcileNativeJobs(_taskId);

    final retry = (await queue.find(_asset17MiB.id))!;
    expect(retry.state, LocalUploadState.awaitingParts);
    expect(retry.nativeJobs, isEmpty);
    expect(native.cancelled, <String>[
      'native:${_asset17MiB.id}:2',
      'native:${_asset17MiB.id}:3',
    ]);
  });

  test(
    'restart releases the last link even when an owner label is reused',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(queue, ownerId: 'reused-owner');
      final nativeJobId = 'native:${_asset.id}:1';
      await queue.linkNativeJob(claim, 1, nativeJobId);
      final native = _FakeBackgroundTransferBridge()
        ..jobs[nativeJobId] = NativeTransferJob(
          jobId: nativeJobId,
          state: NativeTransferState.succeeded,
          etag: 'restarted-etag',
        );
      final coordinator = _coordinator(
        queue: queue,
        native: native,
        ownerId: 'reused-owner',
      );
      addTearDown(coordinator.dispose);

      await coordinator.reconcileNativeJobs(_taskId);

      expect(
        (await queue.find(_asset.id))!.state,
        LocalUploadState.awaitingParts,
      );
    },
  );

  test('a running native job stays linked and is never acknowledged', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final claim = await _seedNativePart(queue, ownerId: 'running-owner');
    final nativeJobId = 'native:${_asset.id}:1';
    await queue.linkNativeJob(claim, 1, nativeJobId);
    final native = _FakeBackgroundTransferBridge()
      ..jobs[nativeJobId] = NativeTransferJob(
        jobId: nativeJobId,
        state: NativeTransferState.running,
      );
    final coordinator = _coordinator(queue: queue, native: native);
    addTearDown(coordinator.dispose);

    await coordinator.reconcileNativeJobs(_taskId);

    expect(
      (await queue.find(_asset.id))!.nativeJobs.values.single.nativeJobId,
      nativeJobId,
    );
    expect(native.acknowledged, isEmpty);
  });

  test(
    'a linked native job cannot be reclaimed after its Dart lease expires',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(queue, ownerId: 'expired-dart-owner');
      await queue.linkNativeJob(claim, 1, 'native:${_asset.id}:1');

      final reclaimed = await queue.nextRunnable(
        _taskId,
        ownerId: 'second-coordinator',
        now: DateTime.utc(2026, 8, 15, 1),
        leaseDuration: const Duration(minutes: 30),
      );

      expect(reclaimed, isNull);
      expect((await queue.find(_asset.id))!.retryCount, 1);
    },
  );

  test('durable native links reject control characters', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final claim = await _seedNativePart(queue, ownerId: 'invalid-link-owner');

    await expectLater(
      queue.linkNativeJob(claim, 1, 'native\njob'),
      throwsA(isA<UploadQueueException>()),
    );

    expect((await queue.find(_asset.id))!.nativeJobs, isEmpty);
  });

  test('replaying a succeeded native result is idempotent', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final claim = await _seedNativePart(queue, ownerId: 'replay-owner');
    final nativeJobId = 'native:${_asset.id}:1';
    await queue.linkNativeJob(claim, 1, nativeJobId);
    final native = _FakeBackgroundTransferBridge()
      ..jobs[nativeJobId] = NativeTransferJob(
        jobId: nativeJobId,
        state: NativeTransferState.succeeded,
        etag: 'etag-once',
      );
    final coordinator = _coordinator(queue: queue, native: native);
    addTearDown(coordinator.dispose);

    await coordinator.reconcileNativeJobs(_taskId);
    await coordinator.reconcileNativeJobs(_taskId);

    expect((await queue.find(_asset.id))!.completedParts, <int, String>{
      1: 'etag-once',
    });
    expect(native.acknowledged, <String>[nativeJobId]);
  });

  test(
    'stale terminal result is acknowledged then detached for retry',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(queue, ownerId: 'stale-policy-owner');
      final nativeJobId = 'native:${_asset.id}:1';
      await queue.linkNativeJob(claim, 1, nativeJobId);
      await queue.pauseTask(_taskId);
      await queue.resumeTask(_taskId);
      final native = _FakeBackgroundTransferBridge()
        ..jobs[nativeJobId] = NativeTransferJob(
          jobId: nativeJobId,
          state: NativeTransferState.succeeded,
          etag: 'stale-etag',
        );
      final coordinator = _coordinator(queue: queue, native: native);
      addTearDown(coordinator.dispose);

      await coordinator.reconcileNativeJobs(_taskId);

      final retried = (await queue.find(_asset.id))!;
      expect(retried.completedParts, isEmpty);
      expect(retried.nativeJobs, isEmpty);
      expect(retried.state, LocalUploadState.awaitingParts);
      expect(native.acknowledged, <String>[nativeJobId]);
    },
  );

  test('stale running job is cancelled then detached for retry', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final claim = await _seedNativePart(queue, ownerId: 'stale-running-owner');
    final nativeJobId = 'native:${_asset.id}:1';
    await queue.linkNativeJob(claim, 1, nativeJobId);
    await queue.pauseTask(_taskId);
    await queue.resumeTask(_taskId);
    final native = _FakeBackgroundTransferBridge()
      ..jobs[nativeJobId] = NativeTransferJob(
        jobId: nativeJobId,
        state: NativeTransferState.running,
      );
    final coordinator = _coordinator(queue: queue, native: native);
    addTearDown(coordinator.dispose);

    await coordinator.reconcileNativeJobs(_taskId);

    expect((await queue.find(_asset.id))!.nativeJobs, isEmpty);
    expect(native.cancelled, <String>[nativeJobId]);
  });

  test('stale terminal ack failure retains its durable link', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final claim = await _seedNativePart(queue, ownerId: 'stale-ack-owner');
    final nativeJobId = 'native:${_asset.id}:1';
    await queue.linkNativeJob(claim, 1, nativeJobId);
    await queue.pauseTask(_taskId);
    await queue.resumeTask(_taskId);
    final native = _FakeBackgroundTransferBridge()
      ..acknowledgeFailure = StateError('native ledger unavailable')
      ..jobs[nativeJobId] = NativeTransferJob(
        jobId: nativeJobId,
        state: NativeTransferState.succeeded,
        etag: 'stale-etag',
      );
    final coordinator = _coordinator(queue: queue, native: native);
    addTearDown(coordinator.dispose);

    await coordinator.reconcileNativeJobs(_taskId);

    expect((await queue.find(_asset.id))!.nativeJobs, isNotEmpty);
  });

  test(
    'event settlement failure stays durable without escaping the listener',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(queue, ownerId: 'event-ack-owner');
      final nativeJobId = 'native:${_asset.id}:1';
      await queue.linkNativeJob(claim, 1, nativeJobId);
      final native = _FakeBackgroundTransferBridge()
        ..acknowledgeFailure = StateError('native ledger unavailable');
      final coordinator = _coordinator(queue: queue, native: native);
      addTearDown(coordinator.dispose);

      native.emit(
        NativeTransferJob(
          jobId: nativeJobId,
          state: NativeTransferState.succeeded,
          etag: 'event-etag',
        ),
      );
      await Future<void>.delayed(Duration.zero);
      await Future<void>.delayed(Duration.zero);

      final item = (await queue.find(_asset.id))!;
      expect(item.completedParts, <int, String>{1: 'event-etag'});
      expect(item.nativeJobs, isNotEmpty);
    },
  );

  test('stale running cancel failure retains its durable link', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final claim = await _seedNativePart(queue, ownerId: 'stale-cancel-owner');
    final nativeJobId = 'native:${_asset.id}:1';
    await queue.linkNativeJob(claim, 1, nativeJobId);
    await queue.pauseTask(_taskId);
    await queue.resumeTask(_taskId);
    final native = _FakeBackgroundTransferBridge()
      ..cancelFailure = StateError('native scheduler unavailable')
      ..jobs[nativeJobId] = NativeTransferJob(
        jobId: nativeJobId,
        state: NativeTransferState.running,
      );
    final coordinator = _coordinator(queue: queue, native: native);
    addTearDown(coordinator.dispose);

    await coordinator.reconcileNativeJobs(_taskId);

    expect((await queue.find(_asset.id))!.nativeJobs, isNotEmpty);
  });

  test(
    'signed URL expiry becomes a durable retry without attempt two',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(queue, ownerId: 'expired-owner');
      final nativeJobId = 'native:${_asset.id}:1';
      await queue.linkNativeJob(claim, 1, nativeJobId);
      final native = _FakeBackgroundTransferBridge()
        ..jobs[nativeJobId] = NativeTransferJob(
          jobId: nativeJobId,
          state: NativeTransferState.failed,
          code: 'signed_url_expired',
          retryable: true,
        );
      final now = DateTime.utc(2026, 8, 15, 12);
      final coordinator = _coordinator(
        queue: queue,
        native: native,
        now: () => now,
      );
      addTearDown(coordinator.dispose);

      await coordinator.reconcileNativeJobs(_taskId);

      final retry = (await queue.find(_asset.id))!;
      expect(retry.state, LocalUploadState.failedRetryable);
      expect(retry.retryCount, 1);
      expect(retry.nextAttemptAt!.toUtc(), now.add(const Duration(seconds: 2)));
      expect(retry.nativeJobs, isEmpty);
      expect(native.acknowledged, <String>[nativeJobId]);
    },
  );

  test(
    'current native failure is durable but linked until ack succeeds',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(queue, ownerId: 'failure-ack-owner');
      final nativeJobId = 'native:${_asset.id}:1';
      await queue.linkNativeJob(claim, 1, nativeJobId);
      final native = _FakeBackgroundTransferBridge()
        ..acknowledgeFailure = StateError('native ledger unavailable')
        ..jobs[nativeJobId] = NativeTransferJob(
          jobId: nativeJobId,
          state: NativeTransferState.failed,
          code: 'network_unavailable',
          retryable: true,
        );
      final coordinator = _coordinator(queue: queue, native: native);
      addTearDown(coordinator.dispose);

      await coordinator.reconcileNativeJobs(_taskId);

      final retained = (await queue.find(_asset.id))!;
      expect(retained.state, LocalUploadState.failedRetryable);
      expect(retained.nativeJobs, isNotEmpty);
    },
  );

  test(
    'policy pause cannot erase a staged native failure during finalize',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(
        queue,
        ownerId: 'failure-policy-owner',
      );
      final nativeJobId = 'native:${_asset.id}:1';
      await queue.linkNativeJob(claim, 1, nativeJobId);
      final link = NativePartJobLink(
        taskId: _taskId,
        partNumber: 1,
        nativeJobId: nativeJobId,
        claim: claim,
      );
      final now = DateTime.utc(2026, 8, 15, 12);
      await queue.recordNativeJobFailed(
        link,
        now: now,
        nextAttemptAt: now.add(const Duration(seconds: 2)),
        code: 'network_unavailable',
        safeMessage: '网络暂不可用，请稍后重试',
        retryable: true,
      );
      await queue.pauseTask(_taskId);

      expect(await queue.finalizeNativeJobFailure(link), isTrue);
      await queue.resumeTask(_taskId);

      final retained = (await queue.find(_asset.id))!;
      expect(retained.state, LocalUploadState.failedRetryable);
      expect(retained.nativeJobs, isEmpty);
    },
  );

  test(
    'missing after ack preserves a staged failure across policy change',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(
        queue,
        ownerId: 'missing-policy-owner',
      );
      final nativeJobId = 'native:${_asset.id}:1';
      await queue.linkNativeJob(claim, 1, nativeJobId);
      final link = NativePartJobLink(
        taskId: _taskId,
        partNumber: 1,
        nativeJobId: nativeJobId,
        claim: claim,
      );
      final now = DateTime.utc(2026, 8, 15, 12);
      await queue.recordNativeJobFailed(
        link,
        now: now,
        nextAttemptAt: now.add(const Duration(seconds: 2)),
        code: 'network_unavailable',
        safeMessage: '网络暂不可用，请稍后重试',
        retryable: true,
      );
      await queue.pauseTask(_taskId);
      await queue.resumeTask(_taskId);

      expect(await queue.markNativeJobMissing(link), isTrue);

      final retained = (await queue.find(_asset.id))!;
      expect(retained.state, LocalUploadState.failedRetryable);
      expect(retained.nativeJobs, isEmpty);
    },
  );

  test(
    'permanent native failure survives ack outage and policy generation',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(
        queue,
        ownerId: 'permanent-policy-owner',
      );
      final nativeJobId = 'native:${_asset.id}:1';
      await queue.linkNativeJob(claim, 1, nativeJobId);
      final native = _FakeBackgroundTransferBridge()
        ..acknowledgeFailure = StateError('native ledger unavailable')
        ..jobs[nativeJobId] = NativeTransferJob(
          jobId: nativeJobId,
          state: NativeTransferState.failed,
          code: 'source_missing',
          retryable: false,
        );
      final coordinator = _coordinator(queue: queue, native: native);
      addTearDown(coordinator.dispose);

      await coordinator.reconcileNativeJobs(_taskId);
      await queue.pauseTask(_taskId);
      await queue.resumeTask(_taskId);
      native.acknowledgeFailure = null;
      await coordinator.reconcileNativeJobs(_taskId);

      final retained = (await queue.find(_asset.id))!;
      expect(retained.state, LocalUploadState.failureReportPending);
      expect(retained.nativeJobs, isEmpty);
      expect(native.enqueued, isEmpty);
    },
  );

  test(
    'terminal settlement emits durable work for queue continuation',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedNativePart(queue, ownerId: 'signal-owner');
      final nativeJobId = 'native:${_asset.id}:1';
      await queue.linkNativeJob(claim, 1, nativeJobId);
      final native = _FakeBackgroundTransferBridge()
        ..jobs[nativeJobId] = NativeTransferJob(
          jobId: nativeJobId,
          state: NativeTransferState.succeeded,
          etag: 'signal-etag',
        );
      final signal = queue.workAvailable.first;
      final coordinator = _coordinator(queue: queue, native: native);
      addTearDown(coordinator.dispose);

      await coordinator.reconcileNativeJobs(_taskId);

      expect(await signal, _taskId);
    },
  );

  test(
    'coordinator enqueues a native job and never transfers bytes in Dart',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      await _seedAwaitingParts(queue);
      final native = _FakeBackgroundTransferBridge()
        ..terminalOnEnqueue = const NativeTransferJob(
          jobId: 'replaced-by-fake',
          state: NativeTransferState.succeeded,
          etag: 'etag-native-worker',
        );
      final api = _RecordingUploadApi();
      final dartTransfer = _NoopPartTransferClient();
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: _NoopMediaLibrary(),
        api: api,
        transferClient: dartTransfer,
        backgroundTransfers: native,
        network: _WifiNetworkMonitor(),
        tasks: _SummaryTaskSyncApi(),
        now: () => DateTime.utc(2026, 8, 15, 1),
      );
      addTearDown(coordinator.dispose);

      await coordinator.runUntilIdle(_taskId);

      expect(native.enqueued, hasLength(1));
      expect(
        native.enqueued.single.request.signedUrl.host,
        'upload.example.test',
      );
      expect(native.enqueued.single.allowMobileData, isFalse);
      expect(dartTransfer.requests, isEmpty);
      expect(api.completed, <int, String>{1: 'etag-native-worker'});
      expect((await queue.find(_asset.id))!.state, LocalUploadState.uploaded);
    },
  );

  test(
    'an EventChannel result cannot release a lease still owned by its waiter',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      await _seedAwaitingParts(queue);
      final terminalWaiter = Completer<NativeTransferJob>();
      final native = _FakeBackgroundTransferBridge()
        ..terminalWaiter = terminalWaiter;
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: _NoopMediaLibrary(),
        api: _RecordingUploadApi(),
        transferClient: _NoopPartTransferClient(),
        backgroundTransfers: native,
        network: _WifiNetworkMonitor(),
        tasks: _SummaryTaskSyncApi(),
        now: () => DateTime.utc(2026, 8, 15, 1),
      );
      addTearDown(coordinator.dispose);

      final run = coordinator.runUntilIdle(_taskId);
      await native.firstEnqueue;
      final jobId = native.enqueued.single.request.jobId;
      final terminal = NativeTransferJob(
        jobId: jobId,
        state: NativeTransferState.succeeded,
        etag: 'event-race-etag',
      );
      final settled = queue.workAvailable.first;
      native.emit(terminal);
      await settled;

      expect((await queue.find(_asset.id))!.leaseOwner, isNotNull);
      terminalWaiter.complete(terminal);
      await run;

      expect((await queue.find(_asset.id))!.state, LocalUploadState.uploaded);
    },
  );

  test(
    'native scheduling is durably linked before its reply can be lost',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      await _seedAwaitingParts(queue);
      final native = _FakeBackgroundTransferBridge()
        ..enqueueFailureAfterScheduling = StateError('isolate terminated');
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: _NoopMediaLibrary(),
        api: _RecordingUploadApi(),
        transferClient: _NoopPartTransferClient(),
        backgroundTransfers: native,
        network: _WifiNetworkMonitor(),
        tasks: _SummaryTaskSyncApi(),
        now: () => DateTime.utc(2026, 8, 15, 1),
      );
      addTearDown(coordinator.dispose);

      await coordinator.runUntilIdle(_taskId);

      final item = (await queue.find(_asset.id))!;
      expect(item.nativeJobs, hasLength(1));
      expect(
        item.nativeJobs.values.single.nativeJobId,
        native.jobs.keys.single,
      );
      expect(item.state, LocalUploadState.uploading);
    },
  );

  test(
    'restart releases the item lease only after every native part settles',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedThreeNativeParts(queue);
      final native = _FakeBackgroundTransferBridge();
      for (var part = 1; part <= 3; part += 1) {
        final jobId = 'native:${_asset17MiB.id}:$part';
        await queue.linkNativeJob(claim, part, jobId);
        native.jobs[jobId] = NativeTransferJob(
          jobId: jobId,
          state: NativeTransferState.succeeded,
          etag: 'etag-$part',
        );
      }
      final coordinator = _coordinator(queue: queue, native: native);
      addTearDown(coordinator.dispose);

      await coordinator.reconcileNativeJobs(_taskId);

      final restored = (await queue.find(_asset17MiB.id))!;
      expect(restored.completedParts, <int, String>{
        1: 'etag-1',
        2: 'etag-2',
        3: 'etag-3',
      });
      expect(restored.nativeJobs, isEmpty);
      expect(restored.state, LocalUploadState.awaitingParts);
      expect(native.acknowledged, hasLength(3));
    },
  );

  test(
    'one native failure cancels sibling parts before retrying the item',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final claim = await _seedThreeNativeParts(queue);
      final native = _FakeBackgroundTransferBridge();
      for (var part = 1; part <= 3; part += 1) {
        final jobId = 'native:${_asset17MiB.id}:$part';
        await queue.linkNativeJob(claim, part, jobId);
        native.jobs[jobId] = NativeTransferJob(
          jobId: jobId,
          state: part == 1
              ? NativeTransferState.failed
              : NativeTransferState.running,
          code: part == 1 ? 'network_unavailable' : null,
          retryable: part == 1 ? true : null,
        );
      }
      final coordinator = _coordinator(queue: queue, native: native);
      addTearDown(coordinator.dispose);

      await coordinator.reconcileNativeJobs(_taskId);

      final retry = (await queue.find(_asset17MiB.id))!;
      expect(retry.state, LocalUploadState.failedRetryable);
      expect(retry.nativeJobs, isEmpty);
      expect(
        native.cancelled,
        containsAll(<String>[
          'native:${_asset17MiB.id}:2',
          'native:${_asset17MiB.id}:3',
        ]),
      );
      expect(native.acknowledged, <String>['native:${_asset17MiB.id}:1']);
    },
  );

  test(
    'live three-part failure wakes sibling waiters and releases the run',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      await _seedAwaitingThreeParts(queue);
      final native = _FakeBackgroundTransferBridge()
        ..terminalByPart[1] = const NativeTransferJob(
          jobId: 'replaced-by-fake',
          state: NativeTransferState.failed,
          code: 'network_unavailable',
          retryable: true,
        );
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: _NoopMediaLibrary(),
        api: _RecordingUploadApi(),
        transferClient: _NoopPartTransferClient(),
        backgroundTransfers: native,
        network: _WifiNetworkMonitor(),
        tasks: _SummaryTaskSyncApi(),
        now: () => DateTime.utc(2026, 8, 15, 1),
      );
      addTearDown(coordinator.dispose);

      await coordinator.runUntilIdle(_taskId);

      final retry = (await queue.find(_asset17MiB.id))!;
      expect(native.enqueued, hasLength(3));
      expect(native.cancelled, hasLength(2));
      expect(retry.state, LocalUploadState.failedRetryable);
      expect(retry.nativeJobs, isEmpty);
    },
  );
}

Future<UploadClaim> _seedNativePart(
  UploadQueueRepository queue, {
  required String ownerId,
}) async {
  await queue.enqueue(_taskId, const <LocalAssetRef>[_asset]);
  final item = await queue.nextRunnable(
    _taskId,
    ownerId: ownerId,
    now: DateTime.utc(2026, 8, 15),
    leaseDuration: const Duration(minutes: 30),
  );
  await queue.markHashed(
    item!.claim,
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
  );
  await queue.markInitiated(
    item.claim,
    const InitiatedAsset(
      assetId: 'backend-asset',
      alreadyUploaded: false,
      providerUploadId: 'provider-upload',
      partSize: 8 * 1024 * 1024,
      completedPartNumbers: <int>[],
    ),
  );
  await queue.markUploading(item.claim);
  return item.claim;
}

Future<void> _seedAwaitingParts(UploadQueueRepository queue) async {
  await queue.enqueue(_taskId, const <LocalAssetRef>[_asset]);
  final item = await queue.nextRunnable(
    _taskId,
    ownerId: 'pre-native-owner',
    now: DateTime.utc(2026, 8, 15),
    leaseDuration: Duration.zero,
  );
  await queue.markHashed(
    item!.claim,
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
  );
  await queue.markInitiated(
    item.claim,
    const InitiatedAsset(
      assetId: 'backend-asset',
      alreadyUploaded: false,
      providerUploadId: 'provider-upload',
      partSize: 8 * 1024 * 1024,
      completedPartNumbers: <int>[],
    ),
  );
}

Future<void> _seedAwaitingThreeParts(UploadQueueRepository queue) async {
  await queue.enqueue(_taskId, const <LocalAssetRef>[_asset17MiB]);
  final item = await queue.nextRunnable(
    _taskId,
    ownerId: 'pre-native-three-part-owner',
    now: DateTime.utc(2026, 8, 15),
    leaseDuration: Duration.zero,
  );
  await queue.markHashed(
    item!.claim,
    'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
  );
  await queue.markInitiated(
    item.claim,
    const InitiatedAsset(
      assetId: 'backend-three-part-asset',
      alreadyUploaded: false,
      providerUploadId: 'provider-three-part-upload',
      partSize: 8 * 1024 * 1024,
      completedPartNumbers: <int>[],
    ),
  );
}

Future<UploadClaim> _seedThreeNativeParts(UploadQueueRepository queue) async {
  await queue.enqueue(_taskId, const <LocalAssetRef>[_asset17MiB]);
  final item = await queue.nextRunnable(
    _taskId,
    ownerId: 'terminated-three-part-owner',
    now: DateTime.utc(2026, 8, 15),
    leaseDuration: const Duration(minutes: 30),
  );
  await queue.markHashed(
    item!.claim,
    'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
  );
  await queue.markInitiated(
    item.claim,
    const InitiatedAsset(
      assetId: 'backend-three-part-asset',
      alreadyUploaded: false,
      providerUploadId: 'provider-three-part-upload',
      partSize: 8 * 1024 * 1024,
      completedPartNumbers: <int>[],
    ),
  );
  await queue.markUploading(item.claim);
  return item.claim;
}

UploadCoordinator _coordinator({
  required UploadQueueRepository queue,
  required BackgroundTransferBridge native,
  DateTime Function()? now,
  String? ownerId,
}) => UploadCoordinator(
  queue: queue,
  mediaLibrary: _NoopMediaLibrary(),
  api: _NoopUploadApi(),
  transferClient: _NoopPartTransferClient(),
  backgroundTransfers: native,
  network: _WifiNetworkMonitor(),
  tasks: _NoopTaskSyncApi(),
  now: now,
  ownerId: ownerId,
);

final class _FakeBackgroundTransferBridge implements BackgroundTransferBridge {
  final StreamController<NativeTransferJob> _events =
      StreamController<NativeTransferJob>.broadcast(sync: true);
  final Completer<void> _firstEnqueue = Completer<void>();
  final Map<String, NativeTransferJob> jobs = <String, NativeTransferJob>{};
  final Map<int, NativeTransferJob> terminalByPart = <int, NativeTransferJob>{};
  final Map<String, Completer<NativeTransferJob>> _terminalWaiters =
      <String, Completer<NativeTransferJob>>{};
  final Set<String> _cancelledWaiters = <String>{};
  final List<String> acknowledged = <String>[];
  final List<String> cancelled = <String>[];
  final List<({TransferPartRequest request, bool allowMobileData})> enqueued =
      <({TransferPartRequest request, bool allowMobileData})>[];
  NativeTransferJob? terminalOnEnqueue;
  Object? enqueueFailureAfterScheduling;
  Object? acknowledgeFailure;
  Object? cancelFailure;
  Completer<NativeTransferJob>? terminalWaiter;

  Future<void> get firstEnqueue => _firstEnqueue.future;

  @override
  Stream<NativeTransferJob> get events => _events.stream;

  void emit(NativeTransferJob job) => _events.add(job);

  @override
  Future<void> acknowledge(String nativeJobId) async {
    if (acknowledgeFailure case final error?) throw error;
    acknowledged.add(nativeJobId);
  }

  @override
  Future<void> cancel(String nativeJobId) async {
    if (cancelFailure case final error?) throw error;
    cancelled.add(nativeJobId);
    jobs.remove(nativeJobId);
    _cancelledWaiters.add(nativeJobId);
    final waiter = _terminalWaiters[nativeJobId];
    if (waiter != null && !waiter.isCompleted) {
      waiter.completeError(StateError('native transfer cancelled'));
    }
  }

  @override
  Future<String> enqueuePart(
    TransferPartRequest request, {
    required bool allowMobileData,
  }) async {
    enqueued.add((request: request, allowMobileData: allowMobileData));
    if (!_firstEnqueue.isCompleted) _firstEnqueue.complete();
    final nativeJobId = request.jobId;
    final terminal = terminalByPart[request.partNumber] ?? terminalOnEnqueue;
    if (terminal != null) {
      jobs[nativeJobId] = NativeTransferJob(
        jobId: nativeJobId,
        state: terminal.state,
        progress: terminal.progress,
        etag: terminal.etag,
        code: terminal.code,
        retryable: terminal.retryable,
      );
    } else {
      jobs[nativeJobId] = NativeTransferJob(
        jobId: nativeJobId,
        state: NativeTransferState.running,
      );
    }
    if (enqueueFailureAfterScheduling case final error?) throw error;
    return nativeJobId;
  }

  @override
  Future<Map<String, NativeTransferJob>> queryJobs(
    Iterable<String> nativeJobIds,
  ) async {
    final result = <String, NativeTransferJob>{};
    for (final id in nativeJobIds) {
      final job = jobs[id];
      if (job != null) result[id] = job;
    }
    return result;
  }

  @override
  Future<NativeTransferJob> waitForTerminal(String nativeJobId) async {
    final waiter = terminalWaiter;
    if (waiter != null) return waiter.future;
    if (_cancelledWaiters.remove(nativeJobId)) {
      throw StateError('native transfer cancelled');
    }
    final current = jobs[nativeJobId];
    if (current != null && current.isTerminal) return current;
    final terminal = Completer<NativeTransferJob>();
    _terminalWaiters[nativeJobId] = terminal;
    try {
      return await terminal.future;
    } finally {
      _terminalWaiters.remove(nativeJobId);
    }
  }
}

final class _NoopPartTransferClient implements PartTransferClient {
  final List<TransferPartRequest> requests = <TransferPartRequest>[];

  @override
  Future<TransferPartResult> transfer(TransferPartRequest request) {
    requests.add(request);
    throw UnimplementedError();
  }
}

final class _RecordingUploadApi implements UploadApi {
  Map<int, String>? completed;

  @override
  Future<void> complete(String assetId, Map<int, String> etags) async {
    completed = Map<int, String>.of(etags);
  }

  @override
  Future<InitiatedAsset> initiate(
    String taskId,
    PendingLocalAsset asset, {
    bool retry = false,
  }) => throw UnimplementedError();

  @override
  Future<void> reportFailure(String assetId, String code, String safeMessage) =>
      throw UnimplementedError();

  @override
  Future<void> reportUnsupported(String taskId, UnsupportedLocalAsset asset) =>
      throw UnimplementedError();

  @override
  Future<Map<int, Uri>> signParts(
    String assetId,
    List<int> partNumbers,
  ) async => <int, Uri>{
    for (final part in partNumbers)
      part: Uri.parse('https://upload.example.test/$part?signature=secret'),
  };
}

final class _NoopMediaLibrary implements MediaLibraryBridge {
  @override
  Future<String> computeSha256(LocalAssetRef ref) => throw UnimplementedError();

  @override
  Future<void> flushPendingCleanup() async {}

  @override
  Future<MediaPickResult> pickImages({required int remainingSlots}) =>
      throw UnimplementedError();

  @override
  Future<void> release(LocalAssetRef ref) async {}
}

final class _NoopUploadApi implements UploadApi {
  @override
  Future<void> complete(String assetId, Map<int, String> etags) =>
      throw UnimplementedError();

  @override
  Future<InitiatedAsset> initiate(
    String taskId,
    PendingLocalAsset asset, {
    bool retry = false,
  }) => throw UnimplementedError();

  @override
  Future<void> reportFailure(String assetId, String code, String safeMessage) =>
      throw UnimplementedError();

  @override
  Future<void> reportUnsupported(String taskId, UnsupportedLocalAsset asset) =>
      throw UnimplementedError();

  @override
  Future<Map<int, Uri>> signParts(String assetId, List<int> partNumbers) =>
      throw UnimplementedError();
}

final class _WifiNetworkMonitor implements NetworkMonitor {
  @override
  Stream<NetworkTransport> get changes =>
      const Stream<NetworkTransport>.empty();

  @override
  Future<NetworkTransport> current() async => NetworkTransport.wifi;
}

final class _NoopTaskSyncApi implements TaskSyncApi {
  @override
  Future<TaskSummary> get(String taskId) => throw UnimplementedError();

  @override
  Future<TaskSummary> seal(String taskId) => throw UnimplementedError();
}

final class _SummaryTaskSyncApi implements TaskSyncApi {
  @override
  Future<TaskSummary> get(String taskId) async => _summary();

  @override
  Future<TaskSummary> seal(String taskId) async => _summary();
}

TaskSummary _summary() => TaskSummary(
  id: _taskId,
  name: 'Background test',
  status: TaskStatusWire.uploading,
  totalAssets: 1,
  uploadedAssets: 1,
  failedAssets: 0,
  unsupportedAssets: 0,
  createdAt: DateTime.utc(2026, 8, 15),
  updatedAt: DateTime.utc(2026, 8, 15),
);
