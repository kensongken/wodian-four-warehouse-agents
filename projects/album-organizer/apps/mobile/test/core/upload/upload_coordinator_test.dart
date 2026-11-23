import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:drift/drift.dart'
    show OpeningDetails, QueryExecutor, QueryExecutorUser;
import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/models.dart';
import 'package:photo_organizer/core/media/local_asset_ref.dart';
import 'package:photo_organizer/core/media/media_library_bridge.dart';
import 'package:photo_organizer/core/auth/session_repository.dart';
import 'package:photo_organizer/core/upload/part_transfer_client.dart';
import 'package:photo_organizer/core/upload/upload_api.dart';
import 'package:photo_organizer/core/upload/upload_coordinator.dart';
import 'package:photo_organizer/core/upload/upload_database.dart';
import 'package:photo_organizer/core/upload/upload_models.dart';
import 'package:photo_organizer/core/upload/upload_queue_repository.dart';
import 'package:photo_organizer/features/tasks/task_repository.dart';

import '../../helpers/fakes.dart';

const _taskId = '11111111-1111-4111-8111-111111111111';
const _bindingId = '22222222-2222-4222-8222-222222222222';
const _candidateId =
    'opc-controlled-upload-candidate:'
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

void main() {
  test(
    'task-local duplicate receipt is terminal but never counted as uploaded',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'backend-duplicate-receipt',
          alreadyUploaded: true,
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[],
          disposition: InitiatedAssetDisposition.duplicate,
          canonicalAssetId: 'backend-canonical',
        );
      late final _FakeMediaLibrary media;
      media = _FakeMediaLibrary(
        onRelease: (asset) async {
          expect(
            (await queue.find(asset.id))!.state,
            LocalUploadState.duplicate,
          );
        },
      );
      final transfer = _FakePartTransferClient();
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: media,
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: _FakeTaskSyncApi(),
      );
      addTearDown(coordinator.dispose);
      await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);

      await coordinator.runUntilIdle(_taskId);

      final snapshot = await queue.snapshot(_taskId);
      final progress = await coordinator.progress(_taskId).first;
      expect(transfer.requests, isEmpty);
      expect(media.released.map((asset) => asset.id), <String>[_asset1MiB.id]);
      expect(snapshot.unacknowledgedAssetIds, isEmpty);
      expect(snapshot.uploaded, 0);
      expect(snapshot.failed, 0);
      expect(snapshot.queued, 0);
      expect(progress.duplicates, 1);
      expect(progress.uploaded, 0);
    },
  );

  test(
    'dedup skips byte transfer and releases only after local commit',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'backend-asset-1',
          alreadyUploaded: true,
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[],
        );
      late final _FakeMediaLibrary media;
      media = _FakeMediaLibrary(
        onRelease: (asset) async {
          final committed = await queue.find(asset.id);
          expect(committed!.state, LocalUploadState.uploaded);
          expect(
            (await queue.snapshot(_taskId)).cleanupRefs.map((ref) => ref.id),
            contains(asset.id),
          );
        },
      );
      final transfer = _FakePartTransferClient();
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: media,
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: _FakeTaskSyncApi(),
      );
      addTearDown(coordinator.dispose);
      await queue.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);

      await coordinator.runUntilIdle(_taskId);

      expect(api.initiatedAssets.map((asset) => asset.asset.id), <String>[
        _asset17MiB.id,
      ]);
      expect(transfer.requests, isEmpty);
      expect(media.released.map((asset) => asset.id), <String>[_asset17MiB.id]);
      expect((await queue.snapshot(_taskId)).cleanupRefs, isEmpty);
    },
  );

  test(
    'dedup accepts completed parts and commits cleanup before release',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      await queue.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'backend-dedup-with-parts',
          alreadyUploaded: true,
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[1, 2, 3],
        );
      late final _FakeMediaLibrary media;
      media = _FakeMediaLibrary(
        onRelease: (asset) async {
          final restarted = UploadQueueRepository(database);
          final committed = (await restarted.find(asset.id))!;
          expect(committed.state, LocalUploadState.uploaded);
          expect(committed.backendAssetId, 'backend-dedup-with-parts');
          expect(committed.serverConfirmed, isTrue);
          expect(
            (await restarted.snapshot(_taskId)).cleanupRefs
                .map((ref) => ref.id),
            <String>[_asset17MiB.id],
          );
        },
      );
      final transfer = _FakePartTransferClient();
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: media,
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: _FakeTaskSyncApi(),
      );
      addTearDown(coordinator.dispose);

      await coordinator.runUntilIdle(_taskId);

      expect(transfer.requests, isEmpty);
      expect(media.released.map((asset) => asset.id), <String>[_asset17MiB.id]);
    },
  );

  test('forwards persisted upload authority to initiation', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(
      _taskId,
      <LocalAssetRef>[_asset1MiB],
      authority: UploadAuthority(
        bindingId: _bindingId,
        candidateId: _candidateId,
      ),
    );
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-authorized-upload',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    expect(api.initiatedAssets, hasLength(1));
    expect(api.initiatedAssets.single.authority!.bindingId, _bindingId);
    expect(api.initiatedAssets.single.authority!.candidateId, _candidateId);
  });

  test('acknowledges server authority only after a summary refresh', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-summary-ack',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final tasks = _FakeTaskSyncApi();
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: tasks,
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    final snapshot = await queue.snapshot(_taskId);
    expect(tasks.getCalls, 1);
    expect(snapshot.unacknowledgedAssetIds, isEmpty);
    expect(snapshot.serverAcceptedAssetIds, <String>{_asset1MiB.id});
    expect(snapshot.uploaded, 0);
  });

  test('summary outage keeps confirmed upload in the local delta', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-summary-outage',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final tasks = _FakeTaskSyncApi()
      ..getFailures.add(StateError('summary unavailable'));
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: tasks,
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    final pending = await queue.snapshot(_taskId);
    expect(pending.unacknowledgedAssetIds, <String>{_asset1MiB.id});
    expect(pending.serverAcceptedAssetIds, isEmpty);
    expect(pending.uploaded, 1);

    await coordinator.runUntilIdle(_taskId);
    final acknowledged = await queue.snapshot(_taskId);
    expect(acknowledged.unacknowledgedAssetIds, isEmpty);
    expect(acknowledged.serverAcceptedAssetIds, <String>{_asset1MiB.id});
    expect(acknowledged.uploaded, 0);
  });

  test('uploads only the two remaining parts of a three-part item', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final crashed = await queue.nextRunnable(
      _taskId,
      ownerId: 'crashed-part-owner',
      now: DateTime.utc(2026, 8, 14),
      leaseDuration: Duration.zero,
    );
    await queue.markPartComplete(crashed!.claim, 1, 'etag-1');
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-asset-1',
        alreadyUploaded: false,
        providerUploadId: 'provider-upload-1',
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final transfer = _FakePartTransferClient();
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: transfer,
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    expect(api.signedPartRequests, <List<int>>[
      <int>[2, 3],
    ]);
    expect(transfer.requests.map((request) => request.partNumber), <int>[2, 3]);
    expect(transfer.requests.map((request) => request.byteOffset), <int>[
      8 * 1024 * 1024,
      16 * 1024 * 1024,
    ]);
    expect(transfer.requests.map((request) => request.byteLength), <int>[
      8 * 1024 * 1024,
      1 * 1024 * 1024,
    ]);
    expect(api.completedEtags.single, <int, String>{
      1: 'etag-1',
      2: 'etag-2',
      3: 'etag-3',
    });
  });

  test('runs at most three part transfers concurrently per device', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset25MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-asset-4-parts',
        alreadyUploaded: false,
        providerUploadId: 'provider-upload-4-parts',
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final transfer = _FakePartTransferClient(yieldBeforeComplete: true);
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: transfer,
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    expect(transfer.requests, hasLength(4));
    expect(transfer.maxInFlight, 3);
  });

  test('pauses runnable items on mobile data by default', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-mobile',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.mobile),
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    expect(
      (await queue.find(_asset17MiB.id))!.state,
      LocalUploadState.pausedWifi,
    );
    expect(api.initiatedAssets, isEmpty);
  });

  test('pauses runnable items while offline', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-offline',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.offline),
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    expect(
      (await queue.find(_asset1MiB.id))!.state,
      LocalUploadState.pausedWifi,
    );
    expect(api.initiatedAssets, isEmpty);
  });

  test('automatically resumes paused items when Wi-Fi returns', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-wifi-resume',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final released = Completer<void>();
    final network = _FakeNetworkMonitor(NetworkTransport.mobile);
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(
        onRelease: (_) async => released.complete(),
      ),
      api: api,
      transferClient: _FakePartTransferClient(),
      network: network,
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(coordinator.dispose);
    await coordinator.runUntilIdle(_taskId);

    network.emit(NetworkTransport.wifi);

    await expectLater(
      released.future.timeout(const Duration(seconds: 1)),
      completes,
    );
    expect(
      (await queue.find(_asset17MiB.id))!.state,
      LocalUploadState.uploaded,
    );
  });

  for (final disallowed in <NetworkTransport>[
    NetworkTransport.mobile,
    NetworkTransport.offline,
  ]) {
    test(
      'Wi-Fi to ${disallowed.name} fences active and waiting parts until restart',
      () async {
        final database = UploadDatabase(NativeDatabase.memory());
        addTearDown(database.close);
        final queue = UploadQueueRepository(database);
        await queue.enqueue(_taskId, <LocalAssetRef>[_asset25MiB]);
        final network = _FakeNetworkMonitor(NetworkTransport.wifi);
        final threePartsStarted = Completer<void>();
        final releaseTransfers = Completer<void>();
        late final _FakePartTransferClient transfer;
        transfer = _FakePartTransferClient()
          ..onTransfer = (_) async {
            if (transfer.requests.length == 3 &&
                !threePartsStarted.isCompleted) {
              threePartsStarted.complete();
            }
            await releaseTransfers.future;
          };
        final coordinator = UploadCoordinator(
          queue: queue,
          mediaLibrary: _FakeMediaLibrary(),
          api: _FakeUploadApi()
            ..initiation = const InitiatedAsset(
              assetId: 'backend-policy-fence',
              alreadyUploaded: false,
              providerUploadId: 'provider-policy-fence',
              partSize: 8 * 1024 * 1024,
              completedPartNumbers: <int>[],
            ),
          transferClient: transfer,
          network: network,
          tasks: _FakeTaskSyncApi(),
        );

        final run = coordinator.runUntilIdle(_taskId);
        addTearDown(() async {
          if (!releaseTransfers.isCompleted) releaseTransfers.complete();
          await run;
          await coordinator.dispose();
        });
        await threePartsStarted.future;
        network.emit(disallowed);
        await _eventually(() async {
          return (await queue.find(_asset25MiB.id))!.state ==
              LocalUploadState.pausedWifi;
        });
        releaseTransfers.complete();
        await run;

        final paused = (await queue.find(_asset25MiB.id))!;
        expect(transfer.requests, hasLength(3));
        expect(paused.state, LocalUploadState.pausedWifi);
        expect(paused.completedParts, isEmpty);
        await coordinator.dispose();

        final restartedTransfer = _FakePartTransferClient();
        final restarted = UploadCoordinator(
          queue: UploadQueueRepository(database),
          mediaLibrary: _FakeMediaLibrary(),
          api: _FakeUploadApi(),
          transferClient: restartedTransfer,
          network: _FakeNetworkMonitor(NetworkTransport.wifi),
          tasks: _FakeTaskSyncApi(),
        );
        addTearDown(restarted.dispose);
        await restarted.runUntilIdle(_taskId);

        expect(restartedTransfer.requests, hasLength(4));
        expect(
          (await queue.find(_asset25MiB.id))!.state,
          LocalUploadState.uploaded,
        );
      },
    );
  }

  test(
    'heartbeat prevents a second coordinator reclaiming a slow transfer',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
      var now = DateTime.utc(2026, 8, 15, 6);
      final firstTransferStarted = Completer<void>();
      final releaseFirstTransfer = Completer<void>();
      late final _FakePartTransferClient transfer;
      transfer = _FakePartTransferClient()
        ..onTransfer = (_) async {
          if (transfer.requests.length == 1) {
            firstTransferStarted.complete();
            await releaseFirstTransfer.future;
          }
        };
      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'backend-heartbeat',
          alreadyUploaded: false,
          providerUploadId: 'provider-heartbeat',
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[],
        );
      final first = UploadCoordinator(
        queue: queue,
        mediaLibrary: _FakeMediaLibrary(),
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: _FakeTaskSyncApi(),
        ownerId: 'slow-owner',
        leaseDuration: const Duration(seconds: 3),
        leaseHeartbeatInterval: const Duration(milliseconds: 5),
        now: () => now,
      );
      final second = UploadCoordinator(
        queue: UploadQueueRepository(database),
        mediaLibrary: _FakeMediaLibrary(),
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: _FakeTaskSyncApi(),
        ownerId: 'reclaim-owner',
        leaseDuration: const Duration(seconds: 3),
        now: () => now,
      );
      addTearDown(first.dispose);
      addTearDown(second.dispose);

      final firstRun = first.runUntilIdle(_taskId);
      await firstTransferStarted.future;
      now = now.add(const Duration(milliseconds: 3100));
      await _eventually(
        () async =>
            (await queue.find(_asset1MiB.id))!.leaseExpiresAt!.isAfter(now),
        pollInterval: const Duration(milliseconds: 1),
      );
      await second.runUntilIdle(_taskId);
      releaseFirstTransfer.complete();
      await firstRun;

      expect(transfer.requests, hasLength(1));
      expect(
        (await queue.find(_asset1MiB.id))!.state,
        LocalUploadState.uploaded,
      );
    },
  );

  test(
    'heartbeat keeps an active eighth attempt out of exhausted recovery',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
      var now = DateTime.utc(2026, 8, 15, 7);
      for (var attempt = 1; attempt <= 7; attempt += 1) {
        final claimed = await queue.nextRunnable(
          _taskId,
          ownerId: 'seed-attempt-$attempt',
          now: now,
          leaseDuration: Duration.zero,
        );
        await queue.scheduleRetry(
          claimed!.claim,
          retryCount: attempt,
          nextAttemptAt: now,
          code: 'network_unavailable',
          safeMessage: '网络不可用，请检查网络后重试',
        );
      }
      final transferStarted = Completer<void>();
      final releaseTransfer = Completer<void>();
      final transfer = _FakePartTransferClient()
        ..onTransfer = (_) async {
          transferStarted.complete();
          await releaseTransfer.future;
        };
      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'backend-eighth-heartbeat',
          alreadyUploaded: false,
          providerUploadId: 'provider-eighth-heartbeat',
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[],
        );
      final first = UploadCoordinator(
        queue: queue,
        mediaLibrary: _FakeMediaLibrary(),
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: _FakeTaskSyncApi(),
        ownerId: 'active-eighth-owner',
        leaseDuration: const Duration(seconds: 3),
        leaseHeartbeatInterval: const Duration(milliseconds: 5),
        now: () => now,
      );
      final second = UploadCoordinator(
        queue: UploadQueueRepository(database),
        mediaLibrary: _FakeMediaLibrary(),
        api: api,
        transferClient: _FakePartTransferClient(),
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: _FakeTaskSyncApi(),
        ownerId: 'exhausted-recovery-owner',
        leaseDuration: const Duration(seconds: 3),
        now: () => now,
      );
      addTearDown(first.dispose);
      addTearDown(second.dispose);

      final firstRun = first.runUntilIdle(_taskId);
      await transferStarted.future;
      now = now.add(const Duration(milliseconds: 3100));
      await _eventually(
        () async =>
            (await queue.find(_asset1MiB.id))!.leaseExpiresAt!.isAfter(now),
        pollInterval: const Duration(milliseconds: 1),
      );
      await second.runUntilIdle(_taskId);

      final stateWhileEighthActive = (await queue.find(_asset1MiB.id))!.state;
      final reportsWhileEighthActive = List.of(api.failureReports);
      releaseTransfer.complete();
      await firstRun;

      expect(stateWhileEighthActive, LocalUploadState.uploading);
      expect(reportsWhileEighthActive, isEmpty);
      expect(
        (await queue.find(_asset1MiB.id))!.state,
        LocalUploadState.uploaded,
      );
    },
  );

  test('restart restores paused retry without bypassing its backoff', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    var now = DateTime.utc(2026, 8, 15, 3);
    final claimed = await queue.nextRunnable(
      _taskId,
      ownerId: 'failed-before-pause',
      now: now,
      leaseDuration: const Duration(minutes: 30),
    );
    final dueAt = now.add(const Duration(minutes: 10));
    await queue.scheduleRetry(
      claimed!.claim,
      retryCount: claimed.retryCount,
      nextAttemptAt: dueAt,
      code: 'network_unavailable',
      safeMessage: '网络不可用，请检查网络后重试',
    );
    final mobile = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: _FakeUploadApi(),
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.mobile),
      tasks: _FakeTaskSyncApi(),
      now: () => now,
    );
    await mobile.runUntilIdle(_taskId);
    await mobile.dispose();

    final paused = (await queue.find(_asset1MiB.id))!;
    expect(paused.state, LocalUploadState.pausedWifi);
    expect(paused.pausedFromState, LocalUploadState.failedRetryable);
    expect(paused.nextAttemptAt!.toUtc(), dueAt);

    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-after-paused-backoff',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final restarted = UploadCoordinator(
      queue: UploadQueueRepository(database),
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
      now: () => now,
    );
    addTearDown(restarted.dispose);

    now = now.add(const Duration(minutes: 1));
    await restarted.runUntilIdle(_taskId);
    expect(
      (await queue.find(_asset1MiB.id))!.state,
      LocalUploadState.failedRetryable,
    );
    expect(api.initiatedAssets, isEmpty);

    now = dueAt;
    await restarted.runUntilIdle(_taskId);
    expect(api.initiatedAssets, hasLength(1));
    expect((await queue.find(_asset1MiB.id))!.state, LocalUploadState.uploaded);
  });

  test('expired signed URL is retried with a freshly signed URL', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-expired-url',
        alreadyUploaded: false,
        providerUploadId: 'provider-expired-url',
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final transfer = _FakePartTransferClient()
      ..failures.add(
        const PartTransferException(
          code: 'signed_url_expired',
          safeMessage: '上传凭证已过期',
          statusCode: 403,
        ),
      );
    var now = DateTime.utc(2026, 8, 15, 1);
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: transfer,
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
      now: () => now,
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);
    expect(
      (await queue.find(_asset1MiB.id))!.state,
      LocalUploadState.failedRetryable,
    );

    now = now.add(const Duration(seconds: 2));
    await coordinator.runUntilIdle(_taskId);

    expect(api.signedPartRequests, <List<int>>[
      <int>[1],
      <int>[1],
    ]);
    expect((await queue.find(_asset1MiB.id))!.state, LocalUploadState.uploaded);
  });

  test('eighth retryable failure becomes terminal and is reported', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-eight-failures',
        alreadyUploaded: false,
        providerUploadId: 'provider-eight-failures',
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final transfer = _FakePartTransferClient()
      ..alwaysFailure = const PartTransferException(
        code: 'network_unavailable',
        safeMessage: '网络不可用，请检查网络后重试',
      );
    final media = _FakeMediaLibrary();
    var now = DateTime.utc(2026, 8, 15, 1);
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: media,
      api: api,
      transferClient: transfer,
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
      now: () => now,
    );
    addTearDown(coordinator.dispose);

    const scheduledDelays = <Duration>[
      Duration(seconds: 2),
      Duration(seconds: 10),
      Duration(seconds: 30),
      Duration(minutes: 2),
      Duration(minutes: 5),
      Duration(minutes: 15),
      Duration(minutes: 30),
    ];
    for (var attempt = 1; attempt <= 8; attempt += 1) {
      final attemptedAt = now;
      await coordinator.runUntilIdle(_taskId);
      final item = (await queue.find(_asset1MiB.id))!;
      expect(item.retryCount, attempt);
      if (attempt < 8) {
        expect(item.state, LocalUploadState.failedRetryable);
        expect(
          item.nextAttemptAt!.toUtc().difference(attemptedAt.toUtc()),
          scheduledDelays[attempt - 1],
        );
        now = item.nextAttemptAt!;
      }
    }

    expect(
      (await queue.find(_asset1MiB.id))!.state,
      LocalUploadState.failedPermanent,
    );
    expect(transfer.requests, hasLength(8));
    expect(api.failureReports, <({String assetId, String code})>[
      (assetId: 'backend-eight-failures', code: 'network_unavailable'),
    ]);
    expect(media.released, isEmpty);
  });

  test('refreshes the shared session once after a 401', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-refreshed',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      )
      ..initiateFailures.add(
        const UploadApiException(
          code: 'unauthorized',
          safeMessage: '登录已过期，请重新登录',
          statusCode: 401,
        ),
      );
    var refreshCalls = 0;
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
      refreshSession: () async => refreshCalls += 1,
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    expect(refreshCalls, 1);
    expect(api.initiateCallCount, 2);
    expect((await queue.find(_asset1MiB.id))!.state, LocalUploadState.uploaded);
  });

  test(
    'HTTP upload API reuses SessionRepository single-refresh guard',
    () async {
      final path = '/v1/tasks/$_taskId/assets/initiate';
      final transport = FakeTransport()
        ..enqueue(method: 'POST', path: path, status: 401)
        ..enqueueJson('/v1/auth/refresh', const <String, Object>{
          'access_token': 'fresh-access',
          'refresh_token': 'fresh-refresh',
          'expires_in_seconds': 900,
        }, method: 'POST')
        ..enqueueJson(
          path,
          const <String, Object?>{
            'asset_id': '22222222-2222-4222-8222-222222222222',
            'already_uploaded': true,
            'provider_upload_id': null,
            'part_size': 8 * 1024 * 1024,
            'completed_parts': <int>[],
          },
          method: 'POST',
          status: 201,
        );
      final session = SessionRepository(
        transport,
        MemoryTokenStore(refreshToken: 'expired-refresh'),
      );
      addTearDown(session.dispose);
      final api = SessionUploadApi(session);

      final initiated = await api.initiate(
        _taskId,
        const PendingLocalAsset(asset: _asset1MiB, sha256: _sha256),
      );

      expect(initiated.alreadyUploaded, isTrue);
      expect(transport.callsTo('/v1/auth/refresh'), 1);
      expect(transport.calls.where((call) => call.path == path), hasLength(2));
      expect(
        transport.calls.last.headers['Authorization'],
        'Bearer fresh-access',
      );
      expect(transport.calls.last.data, <String, Object?>{
        'local_asset_id': _asset1MiB.id,
        'filename': _asset1MiB.displayName,
        'media_type': _asset1MiB.mediaType,
        'byte_size': _asset1MiB.byteSize,
        'sha256': _sha256,
        'captured_at': null,
        'retry': false,
      });
    },
  );

  test('HTTP upload API parses explicit duplicate provenance', () async {
    final path = '/v1/tasks/$_taskId/assets/initiate';
    final transport = FakeTransport()
      ..enqueueJson(
        path,
        const <String, Object?>{
          'asset_id': '33333333-3333-4333-8333-333333333333',
          'already_uploaded': true,
          'provider_upload_id': null,
          'part_size': 8 * 1024 * 1024,
          'completed_parts': <int>[],
          'disposition': 'duplicate',
          'canonical_asset_id': '22222222-2222-4222-8222-222222222222',
        },
        method: 'POST',
        status: 201,
      );
    final session = SessionRepository(transport, MemoryTokenStore());
    addTearDown(session.dispose);

    final initiated = await SessionUploadApi(session).initiate(
      _taskId,
      const PendingLocalAsset(asset: _asset1MiB, sha256: _sha256),
    );

    expect(initiated.disposition, InitiatedAssetDisposition.duplicate);
    expect(initiated.canonicalAssetId, '22222222-2222-4222-8222-222222222222');
    expect(initiated.alreadyUploaded, isTrue);
  });

  test('HTTP upload API sends an explicit retry receipt', () async {
    final path = '/v1/tasks/$_taskId/assets/initiate';
    final transport = FakeTransport()
      ..enqueueJson(
        path,
        const <String, Object?>{
          'asset_id': '33333333-3333-4333-8333-333333333333',
          'already_uploaded': true,
          'provider_upload_id': null,
          'part_size': 8 * 1024 * 1024,
          'completed_parts': <int>[],
        },
        method: 'POST',
        status: 201,
      );
    final session = SessionRepository(transport, MemoryTokenStore());
    addTearDown(session.dispose);
    final api = SessionUploadApi(session);

    await api.initiate(
      _taskId,
      const PendingLocalAsset(asset: _asset1MiB, sha256: _sha256),
      retry: true,
    );

    expect(transport.calls.single.data, <String, Object?>{
      'local_asset_id': _asset1MiB.id,
      'filename': _asset1MiB.displayName,
      'media_type': _asset1MiB.mediaType,
      'byte_size': _asset1MiB.byteSize,
      'sha256': _sha256,
      'captured_at': null,
      'retry': true,
    });
  });

  test('HTTP upload API sends the persisted binding authority pair', () async {
    final path = '/v1/tasks/$_taskId/assets/initiate';
    final transport = FakeTransport()
      ..enqueueJson(
        path,
        const <String, Object?>{
          'asset_id': '44444444-4444-4444-8444-444444444444',
          'already_uploaded': true,
          'provider_upload_id': null,
          'part_size': 8 * 1024 * 1024,
          'completed_parts': <int>[],
        },
        method: 'POST',
        status: 201,
      );
    final session = SessionRepository(transport, MemoryTokenStore());
    addTearDown(session.dispose);
    final api = SessionUploadApi(session);

    await api.initiate(
      _taskId,
      PendingLocalAsset(
        asset: _asset1MiB,
        sha256: _sha256,
        authority: UploadAuthority(
          bindingId: _bindingId,
          candidateId: _candidateId,
        ),
      ),
    );

    expect(transport.calls.single.data, <String, Object?>{
      'local_asset_id': _asset1MiB.id,
      'filename': _asset1MiB.displayName,
      'media_type': _asset1MiB.mediaType,
      'byte_size': _asset1MiB.byteSize,
      'sha256': _sha256,
      'captured_at': null,
      'retry': false,
      'binding_id': _bindingId,
      'candidate_id': _candidateId,
    });
  });

  test('coalesces concurrent runs so completion is issued once', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-idempotent-complete',
        alreadyUploaded: false,
        providerUploadId: 'provider-idempotent-complete',
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final transfer = _FakePartTransferClient(yieldBeforeComplete: true);
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: transfer,
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(coordinator.dispose);

    await Future.wait<void>(<Future<void>>[
      coordinator.runUntilIdle(_taskId),
      coordinator.runUntilIdle(_taskId),
    ]);

    expect(api.initiateCallCount, 1);
    expect(transfer.requests, hasLength(1));
    expect(api.completedEtags, hasLength(1));
  });

  test(
    'two coordinators cannot duplicate one claimed upload attempt',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final firstQueue = UploadQueueRepository(database);
      await firstQueue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'backend-cross-coordinator-claim',
          alreadyUploaded: false,
          providerUploadId: 'provider-cross-coordinator-claim',
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[],
        );
      final transfer = _FakePartTransferClient(yieldBeforeComplete: true);
      final tasks = _FakeTaskSyncApi();
      final first = UploadCoordinator(
        queue: firstQueue,
        mediaLibrary: _FakeMediaLibrary(),
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: tasks,
        ownerId: 'upload-owner-a',
      );
      final second = UploadCoordinator(
        queue: UploadQueueRepository(database),
        mediaLibrary: _FakeMediaLibrary(),
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: tasks,
        ownerId: 'upload-owner-b',
      );
      addTearDown(first.dispose);
      addTearDown(second.dispose);

      await Future.wait<void>(<Future<void>>[
        first.runUntilIdle(_taskId),
        second.runUntilIdle(_taskId),
      ]);

      expect(api.initiateCallCount, 1);
      expect(transfer.requests, hasLength(1));
      expect(api.completedEtags, hasLength(1));
      expect((await firstQueue.find(_asset1MiB.id))!.retryCount, 1);
    },
  );

  test('mobile-data override permits upload', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-mobile-override',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.mobile),
      tasks: _FakeTaskSyncApi(),
      allowMobileData: true,
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    expect((await queue.find(_asset1MiB.id))!.state, LocalUploadState.uploaded);
  });

  for (final contractFailure in <({int status, String code})>[
    (status: 409, code: 'task_sealed'),
    (status: 413, code: 'image_too_large'),
    (status: 415, code: 'unsupported_media_type'),
  ]) {
    test('${contractFailure.status} contract failure is not retried', () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final asset = _assetWithId('ios:permanent-${contractFailure.status}');
      await queue.enqueue(_taskId, <LocalAssetRef>[asset]);
      await queue.setSealWhenIdle(_taskId);
      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'unused',
          alreadyUploaded: true,
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[],
        )
        ..initiateFailures.add(
          UploadApiException(
            code: contractFailure.code,
            safeMessage: '无法上传该照片',
            statusCode: contractFailure.status,
          ),
        );
      late final _FakeMediaLibrary media;
      media = _FakeMediaLibrary(
        onRelease: (released) async {
          final committed = (await queue.find(released.id))!;
          expect(committed.state, LocalUploadState.failedPermanent);
          expect(committed.localRejected, isTrue);
          expect(
            (await queue.snapshot(_taskId)).cleanupRefs.map((ref) => ref.id),
            contains(released.id),
          );
        },
      );
      final tasks = _FakeTaskSyncApi();
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: media,
        api: api,
        transferClient: _FakePartTransferClient(),
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: tasks,
      );
      addTearDown(coordinator.dispose);

      await coordinator.runUntilIdle(_taskId);
      await coordinator.runUntilIdle(_taskId);

      final failed = (await queue.find(asset.id))!;
      expect(failed.state, LocalUploadState.failedPermanent);
      expect(failed.retryCount, 1);
      expect(failed.lastErrorCode, contractFailure.code);
      expect(failed.lastErrorMessage, '无法上传该照片');
      expect(api.initiateCallCount, 1);
      expect(failed.localRejected, isTrue);
      expect(media.released.map((released) => released.id), <String>[asset.id]);
      expect((await queue.snapshot(_taskId)).cleanupRefs, isEmpty);
      expect(tasks.sealCalls, 1);
    });
  }

  for (final endpoint in <String>['sign', 'complete']) {
    for (final contractFailure in <({int status, String code})>[
      (status: 409, code: 'task_sealed'),
      (status: 413, code: 'image_too_large'),
      (status: 415, code: 'unsupported_media_type'),
    ]) {
      test(
        '$endpoint ${contractFailure.status} is terminal without /fail',
        () async {
          final database = UploadDatabase(NativeDatabase.memory());
          addTearDown(database.close);
          final queue = UploadQueueRepository(database);
          final asset = _assetWithId('ios:$endpoint-${contractFailure.status}');
          await queue.enqueue(_taskId, <LocalAssetRef>[asset]);
          await queue.setSealWhenIdle(_taskId);
          final error = UploadApiException(
            code: contractFailure.code,
            safeMessage: '无法上传该照片',
            statusCode: contractFailure.status,
          );
          final api = _FakeUploadApi()
            ..initiation = const InitiatedAsset(
              assetId: 'backend-authoritative-contract',
              alreadyUploaded: false,
              providerUploadId: 'provider-authoritative-contract',
              partSize: 8 * 1024 * 1024,
              completedPartNumbers: <int>[],
            )
            ..signPartsError = endpoint == 'sign' ? error : null
            ..completeError = endpoint == 'complete' ? error : null;
          final media = _FakeMediaLibrary();
          final tasks = _FakeTaskSyncApi()
            ..summary = _taskSummary(
              _taskId,
              status: contractFailure.status == 409
                  ? 'ai_processing'
                  : 'waiting_upload',
              uploadedAssets: 0,
              failedAssets: 1,
            );
          final coordinator = UploadCoordinator(
            queue: queue,
            mediaLibrary: media,
            api: api,
            transferClient: _FakePartTransferClient(),
            network: _FakeNetworkMonitor(NetworkTransport.wifi),
            tasks: tasks,
          );
          addTearDown(coordinator.dispose);

          await coordinator.runUntilIdle(_taskId);

          final terminal = (await queue.find(asset.id))!;
          expect(terminal.state, LocalUploadState.failedPermanent);
          expect(terminal.lastErrorMessage, '无法上传该照片');
          expect(api.failureReports, isEmpty);
          if (contractFailure.status == 409) {
            expect(media.released.map((released) => released.id), <String>[
              asset.id,
            ]);
            expect((await queue.snapshot(_taskId)).serverSealed, isTrue);
            expect(tasks.getCalls, 2);
          } else {
            expect(media.released, isEmpty);
            expect((await queue.snapshot(_taskId)).serverSealed, isFalse);
          }
          expect((await queue.snapshot(_taskId)).cleanupRefs, isEmpty);
          expect(tasks.sealCalls, 0);
        },
      );
    }
  }

  test('progress stream reports durable queued and uploaded counts', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: _FakeUploadApi(),
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(coordinator.dispose);

    final queued = await coordinator.progress(_taskId).first;
    final claimed = await queue.nextRunnable(
      _taskId,
      ownerId: 'progress-test-owner',
      now: DateTime.utc(2026, 8, 15),
      leaseDuration: const Duration(minutes: 30),
    );
    await queue.markUploaded(claimed!.claim);
    final uploaded = await coordinator.progress(_taskId).first;

    expect((queued.total, queued.queued, queued.uploaded), (1, 1, 0));
    expect((uploaded.total, uploaded.queued, uploaded.uploaded), (1, 0, 1));
  });

  test(
    'HTTP upload API signs requested parts and completes in order',
    () async {
      const assetId = '22222222-2222-4222-8222-222222222222';
      final signPath = '/v1/assets/$assetId/parts/sign';
      final completePath = '/v1/assets/$assetId/complete';
      final transport = FakeTransport()
        ..enqueueJson(signPath, const <String, Object>{
          'url': 'https://uploads.invalid/part/3?signature=secret-three',
          'expires_seconds': 900,
        }, method: 'POST')
        ..enqueueJson(signPath, const <String, Object>{
          'url': 'https://uploads.invalid/part/1?signature=secret-one',
          'expires_seconds': 900,
        }, method: 'POST')
        ..enqueueJson(completePath, const <String, Object>{}, method: 'POST');
      final session = SessionRepository(
        transport,
        MemoryTokenStore(
          accessToken: 'test-access-token',
          refreshToken: 'refresh-token',
        ),
      );
      addTearDown(session.dispose);
      final api = SessionUploadApi(session);

      final signed = await api.signParts(assetId, <int>[3, 1]);
      await api.complete(assetId, <int, String>{3: 'etag-3', 1: 'etag-1'});

      expect(signed.keys, <int>[3, 1]);
      expect(
        transport.calls
            .where((call) => call.path == signPath)
            .map((call) => call.data),
        <Map<String, Object>>[
          <String, Object>{'part_number': 3},
          <String, Object>{'part_number': 1},
        ],
      );
      expect(transport.calls.last.data, <String, Object>{
        'parts': <Map<String, Object>>[
          <String, Object>{'part_number': 1, 'etag': 'etag-1'},
          <String, Object>{'part_number': 3, 'etag': 'etag-3'},
        ],
      });
    },
  );

  test('restart releases a server-confirmed durable cleanup ref', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final claimed = await queue.nextRunnable(
      _taskId,
      ownerId: 'cleanup-restart-owner',
      now: DateTime.utc(2026, 8, 15),
      leaseDuration: const Duration(minutes: 30),
    );
    await queue.markInitiated(
      claimed!.claim,
      const InitiatedAsset(
        assetId: 'backend-confirmed-before-restart',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      ),
    );
    await queue.markUploaded(claimed.claim);
    final media = _FakeMediaLibrary();
    final restarted = UploadCoordinator(
      queue: UploadQueueRepository(database),
      mediaLibrary: media,
      api: _FakeUploadApi(),
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(restarted.dispose);

    await restarted.runUntilIdle(_taskId);

    expect(media.released.map((asset) => asset.id), <String>[_asset1MiB.id]);
    expect((await queue.snapshot(_taskId)).cleanupRefs, isEmpty);
  });

  test(
    'v1 initiated row re-initiates then completes with two-phase ack',
    () async {
      final directory = await Directory.systemTemp.createTemp(
        'upload-v1-initiated-',
      );
      addTearDown(() => directory.delete(recursive: true));
      final file = File('${directory.path}/upload.sqlite');
      await _createV1UploadDatabase(file, _asset1MiB);
      final database = UploadDatabase(NativeDatabase(file));
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);

      final migrated = (await queue.find(_asset1MiB.id))!;
      expect(migrated.backendAssetId, isNull);
      expect(migrated.state, LocalUploadState.awaitingInitiation);
      expect((await queue.snapshot(_taskId)).serverAcceptedAssetIds, isEmpty);

      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'backend-after-v1-migration',
          alreadyUploaded: false,
          providerUploadId: 'provider-after-v1-migration',
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[],
        );
      final tasks = _FakeTaskSyncApi();
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: _FakeMediaLibrary(),
        api: api,
        transferClient: _FakePartTransferClient(),
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: tasks,
      );
      addTearDown(coordinator.dispose);
      await coordinator.runUntilIdle(_taskId);

      final snapshot = await queue.snapshot(_taskId);
      expect(api.initiateCallCount, 1);
      expect(
        (await queue.find(_asset1MiB.id))!.state,
        LocalUploadState.uploaded,
      );
      expect(snapshot.serverAcceptedAssetIds, <String>{_asset1MiB.id});
      expect(snapshot.uploaded, 0);
      expect(tasks.getCalls, 2);
    },
  );

  test(
    'v1 dedup crash row idempotently re-initiates without byte upload',
    () async {
      final directory = await Directory.systemTemp.createTemp(
        'upload-v1-dedup-',
      );
      addTearDown(() => directory.delete(recursive: true));
      final file = File('${directory.path}/upload.sqlite');
      await _createV1UploadDatabase(file, _asset1MiB);
      final database = UploadDatabase(NativeDatabase(file));
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final transfer = _FakePartTransferClient();
      final media = _FakeMediaLibrary();
      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'backend-v1-dedup-recovered',
          alreadyUploaded: true,
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[1],
        );
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: media,
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: _FakeTaskSyncApi(),
      );
      addTearDown(coordinator.dispose);

      await coordinator.runUntilIdle(_taskId);

      expect(api.initiateCallCount, 1);
      expect(transfer.requests, isEmpty);
      expect(media.released.map((asset) => asset.id), <String>[_asset1MiB.id]);
      expect(
        (await queue.find(_asset1MiB.id))!.state,
        LocalUploadState.uploaded,
      );
      expect((await queue.snapshot(_taskId)).uploaded, 0);
    },
  );

  test('restart seals an idle task once from durable intent', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.setSealWhenIdle(_taskId);
    final tasks = _FakeTaskSyncApi();
    final restarted = UploadCoordinator(
      queue: UploadQueueRepository(database),
      mediaLibrary: _FakeMediaLibrary(),
      api: _FakeUploadApi(),
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: tasks,
    );
    addTearDown(restarted.dispose);

    await restarted.runUntilIdle(_taskId);
    await restarted.runUntilIdle(_taskId);

    expect(tasks.sealCalls, 1);
    expect((await queue.snapshot(_taskId)).serverSealed, isTrue);
  });

  test('does not seal while a paused upload is non-terminal', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    await queue.setSealWhenIdle(_taskId);
    final tasks = _FakeTaskSyncApi();
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: _FakeUploadApi(),
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.mobile),
      tasks: tasks,
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    expect(tasks.sealCalls, 0);
    expect((await queue.snapshot(_taskId)).serverSealed, isFalse);
  });

  test('persists seal retry backoff and succeeds when due', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.setSealWhenIdle(_taskId);
    final tasks = _FakeTaskSyncApi()
      ..sealFailures.add(StateError('temporarily unavailable'));
    var now = DateTime.utc(2026, 8, 15, 4);
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: _FakeUploadApi(),
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: tasks,
      now: () => now,
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);
    now = now.add(const Duration(seconds: 1));
    await coordinator.runUntilIdle(_taskId);
    expect(tasks.sealCalls, 1);
    expect((await queue.snapshot(_taskId)).serverSealed, isFalse);

    now = now.add(const Duration(seconds: 1));
    await coordinator.runUntilIdle(_taskId);
    expect(tasks.sealCalls, 2);
    expect((await queue.snapshot(_taskId)).serverSealed, isTrue);
  });

  test('two coordinators produce one concurrent seal effect', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.setSealWhenIdle(_taskId);
    final sealStarted = Completer<void>();
    final releaseSeal = Completer<void>();
    final tasks = _FakeTaskSyncApi()
      ..sealStarted = sealStarted
      ..sealBarrier = releaseSeal.future;
    final first = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: _FakeUploadApi(),
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: tasks,
      ownerId: 'seal-owner-a',
    );
    final second = UploadCoordinator(
      queue: UploadQueueRepository(database),
      mediaLibrary: _FakeMediaLibrary(),
      api: _FakeUploadApi(),
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: tasks,
      ownerId: 'seal-owner-b',
    );
    addTearDown(first.dispose);
    addTearDown(second.dispose);

    final firstRun = first.runUntilIdle(_taskId);
    await sealStarted.future;
    await second.runUntilIdle(_taskId);
    releaseSeal.complete();
    await firstRun;

    expect(tasks.sealCalls, 1);
    expect((await queue.snapshot(_taskId)).serverSealed, isTrue);
  });

  test('release failure cannot downgrade a server-confirmed upload', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-confirmed-release-failure',
        alreadyUploaded: true,
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    final media = _FakeMediaLibrary()..releaseError = StateError('busy');
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: media,
      api: api,
      transferClient: _FakePartTransferClient(),
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    expect((await queue.find(_asset1MiB.id))!.state, LocalUploadState.uploaded);
    expect(
      (await queue.snapshot(_taskId)).cleanupRefs.map((asset) => asset.id),
      <String>[_asset1MiB.id],
    );
    expect(api.failureReports, isEmpty);
  });

  test('persists uploading state before part transfer starts', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
    final api = _FakeUploadApi()
      ..initiation = const InitiatedAsset(
        assetId: 'backend-running-state',
        alreadyUploaded: false,
        providerUploadId: 'provider-running-state',
        partSize: 8 * 1024 * 1024,
        completedPartNumbers: <int>[],
      );
    LocalUploadState? stateDuringTransfer;
    late final _FakePartTransferClient transfer;
    transfer = _FakePartTransferClient()
      ..onTransfer = (_) async {
        stateDuringTransfer = (await queue.find(_asset1MiB.id))!.state;
      };
    final coordinator = UploadCoordinator(
      queue: queue,
      mediaLibrary: _FakeMediaLibrary(),
      api: api,
      transferClient: transfer,
      network: _FakeNetworkMonitor(NetworkTransport.wifi),
      tasks: _FakeTaskSyncApi(),
    );
    addTearDown(coordinator.dispose);

    await coordinator.runUntilIdle(_taskId);

    expect(stateDuringTransfer, LocalUploadState.uploading);
    expect(transfer.requests, hasLength(1));
    expect((await queue.find(_asset1MiB.id))!.state, LocalUploadState.uploaded);
  });

  test(
    'failure-report outage stays control-only then retains and blocks seal',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
      await queue.setSealWhenIdle(_taskId);
      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'backend-report-outage',
          alreadyUploaded: false,
          providerUploadId: 'provider-report-outage',
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[],
        )
        ..failureReportError = const UploadApiException(
          code: 'network_unavailable',
          safeMessage: '网络不可用，请检查网络后重试',
        );
      final transfer = _FakePartTransferClient()
        ..alwaysFailure = const PartTransferException(
          code: 'network_unavailable',
          safeMessage: '网络不可用，请检查网络后重试',
        );
      final media = _FakeMediaLibrary();
      final tasks = _FakeTaskSyncApi()
        ..summary = _taskSummary(_taskId, uploadedAssets: 0, failedAssets: 1);
      var now = DateTime.utc(2026, 8, 15, 1);
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: media,
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: tasks,
        now: () => now,
      );
      addTearDown(coordinator.dispose);

      for (var attempt = 1; attempt <= 8; attempt += 1) {
        await coordinator.runUntilIdle(_taskId);
        final item = (await queue.find(_asset1MiB.id))!;
        if (attempt < 8) now = item.nextAttemptAt!;
      }
      final pending = (await queue.find(_asset1MiB.id))!;
      expect(pending.state, LocalUploadState.failureReportPending);
      expect(pending.retryCount, 8);
      expect(transfer.requests, hasLength(8));
      expect(media.released, isEmpty);
      expect((await queue.snapshot(_taskId)).cleanupRefs, isEmpty);
      expect(tasks.sealCalls, 0);

      await coordinator.dispose();
      api.failureReportError = null;
      now = pending.failureReportNextAttemptAt!;
      final restarted = UploadCoordinator(
        queue: UploadQueueRepository(database),
        mediaLibrary: media,
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: tasks,
        now: () => now,
      );
      addTearDown(restarted.dispose);
      await restarted.runUntilIdle(_taskId);

      final terminal = (await queue.find(_asset1MiB.id))!;
      expect(terminal.state, LocalUploadState.failedPermanent);
      expect(terminal.serverConfirmed, isTrue);
      expect(transfer.requests, hasLength(8));
      expect(api.initiateCallCount, 1);
      expect(api.completedEtags, isEmpty);
      expect(api.failureReports, hasLength(2));
      expect(media.released, isEmpty);
      expect(tasks.getCalls, 2);
      expect(tasks.sealCalls, 0);
    },
  );

  for (final controlFailure in <({int status, String code})>[
    (status: 413, code: 'invalid_failure_payload'),
    (status: 415, code: 'unsupported_failure_payload'),
  ]) {
    test(
      'pending /fail ${controlFailure.status} reconciles once and retains retry ownership',
      () async {
        final database = UploadDatabase(NativeDatabase.memory());
        addTearDown(database.close);
        final queue = UploadQueueRepository(database);
        await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
        await queue.setSealWhenIdle(_taskId);
        var now = DateTime.utc(2026, 8, 15, 8);
        final claimed = await queue.nextRunnable(
          _taskId,
          ownerId: 'pending-control-${controlFailure.status}',
          now: now,
          leaseDuration: const Duration(minutes: 30),
        );
        await queue.markHashed(claimed!.claim, _sha256);
        await queue.markInitiated(
          claimed.claim,
          const InitiatedAsset(
            assetId: 'backend-pending-control',
            alreadyUploaded: false,
            providerUploadId: 'provider-pending-control',
            partSize: 8 * 1024 * 1024,
            completedPartNumbers: <int>[],
          ),
        );
        await queue.markFailureReportPending(
          claimed.claim,
          now: now,
          code: 'network_unavailable',
          safeMessage: '网络不可用，请检查网络后重试',
        );
        now = now.add(const Duration(seconds: 2));
        final api = _FakeUploadApi()
          ..failureReportError = UploadApiException(
            code: controlFailure.code,
            safeMessage: '无法记录上传失败',
            statusCode: controlFailure.status,
          );
        final tasks = _FakeTaskSyncApi()
          ..summary = _taskSummary(_taskId, uploadedAssets: 0, failedAssets: 1);
        final media = _FakeMediaLibrary();
        final coordinator = UploadCoordinator(
          queue: queue,
          mediaLibrary: media,
          api: api,
          transferClient: _FakePartTransferClient(),
          network: _FakeNetworkMonitor(NetworkTransport.wifi),
          tasks: tasks,
          now: () => now,
        );
        addTearDown(coordinator.dispose);

        await coordinator.runUntilIdle(_taskId);
        await coordinator.runUntilIdle(_taskId);

        final reconciled = (await queue.find(_asset1MiB.id))!;
        expect(reconciled.state, LocalUploadState.failedPermanent);
        expect(reconciled.serverConfirmed, isTrue);
        expect(api.failureReports, hasLength(1));
        expect(tasks.getCalls, 2);
        expect(tasks.sealCalls, 0);
        expect(media.released, isEmpty);
        expect((await queue.snapshot(_taskId)).cleanupRefs, isEmpty);
      },
    );
  }

  for (final taskState in <({String status, bool closesUpload})>[
    (status: 'waiting_upload', closesUpload: false),
    (status: 'uploading', closesUpload: false),
    (status: 'ai_processing', closesUpload: true),
    (status: 'waiting_review', closesUpload: true),
    (status: 'completed', closesUpload: true),
    (status: 'paused', closesUpload: false),
    (status: 'partial_failure', closesUpload: true),
    (status: 'cancelled', closesUpload: true),
  ]) {
    test('task_sealed /fail reconciles ${taskState.status} as '
        '${taskState.closesUpload ? 'closed' : 'retryable'}', () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final asset = _assetWithId('ios:task-status-${taskState.status}');
      await queue.enqueue(_taskId, <LocalAssetRef>[asset]);
      await queue.setSealWhenIdle(_taskId);
      var now = DateTime.utc(2026, 8, 15, 9);
      final claimed = await queue.nextRunnable(
        _taskId,
        ownerId: 'pending-${taskState.status}-control',
        now: now,
        leaseDuration: const Duration(minutes: 30),
      );
      await queue.markHashed(claimed!.claim, _sha256);
      await queue.markInitiated(
        claimed.claim,
        InitiatedAsset(
          assetId: 'backend-pending-${taskState.status}-control',
          alreadyUploaded: false,
          providerUploadId: 'provider-pending-${taskState.status}-control',
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: const <int>[],
        ),
      );
      await queue.markFailureReportPending(
        claimed.claim,
        now: now,
        code: 'network_unavailable',
        safeMessage: '网络不可用，请检查网络后重试',
      );
      now = now.add(const Duration(seconds: 2));
      final api = _FakeUploadApi()
        ..failureReportError = const UploadApiException(
          code: 'task_sealed',
          safeMessage: '任务已封存',
          statusCode: 409,
        );
      final tasks = _FakeTaskSyncApi()
        ..summary = _taskSummary(
          _taskId,
          status: taskState.status,
          failedAssets: 1,
        );
      late final _FakeMediaLibrary media;
      media = _FakeMediaLibrary(
        onRelease: (asset) async {
          expect((await queue.snapshot(_taskId)).serverSealed, isTrue);
          expect(
            (await queue.snapshot(_taskId)).cleanupRefs.map((ref) => ref.id),
            contains(asset.id),
          );
        },
      );
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: media,
        api: api,
        transferClient: _FakePartTransferClient(),
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: tasks,
        now: () => now,
      );
      addTearDown(coordinator.dispose);

      await coordinator.runUntilIdle(_taskId);

      expect(api.failureReports, hasLength(1));
      expect(tasks.getCalls, 2);
      expect(tasks.sealCalls, 0);
      if (taskState.closesUpload) {
        expect(media.released.map((asset) => asset.id), <String>[asset.id]);
        expect((await queue.snapshot(_taskId)).cleanupRefs, isEmpty);
        expect((await queue.snapshot(_taskId)).serverSealed, isTrue);
        await expectLater(
          queue.requestFailedRetry(_taskId, asset.id),
          throwsStateError,
        );
      } else {
        expect(media.released, isEmpty);
        expect((await queue.snapshot(_taskId)).serverSealed, isFalse);
        await queue.requestFailedRetry(_taskId, asset.id);
        final retryable = (await queue.find(asset.id))!;
        expect(retryable.state, LocalUploadState.awaitingInitiation);
        expect(retryable.retryRequested, isTrue);
      }
    });
  }

  test(
    'confirmed failure blocks seal and explicit retry resets byte budget',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
      await queue.setSealWhenIdle(_taskId);
      final now = DateTime.utc(2026, 8, 15, 10);
      for (var attempt = 1; attempt <= 8; attempt += 1) {
        final claimed = await queue.nextRunnable(
          _taskId,
          ownerId: 'failed-retry-seed-$attempt',
          now: now,
          leaseDuration: Duration.zero,
        );
        if (attempt == 1) {
          await queue.markHashed(claimed!.claim, _sha256);
          await queue.markInitiated(
            claimed.claim,
            const InitiatedAsset(
              assetId: 'backend-before-explicit-retry',
              alreadyUploaded: false,
              providerUploadId: 'provider-before-explicit-retry',
              partSize: 8 * 1024 * 1024,
              completedPartNumbers: <int>[],
            ),
          );
        }
        if (attempt < 8) {
          await queue.scheduleRetry(
            claimed!.claim,
            retryCount: attempt,
            nextAttemptAt: now,
            code: 'network_unavailable',
            safeMessage: '网络不可用，请检查网络后重试',
          );
        } else {
          await queue.markFailureReported(
            claimed!.claim,
            code: 'network_unavailable',
            safeMessage: '网络不可用，请检查网络后重试',
          );
        }
      }
      final beforeRetry = (await queue.find(_asset1MiB.id))!;
      expect(beforeRetry.retryCount, 8);
      expect(beforeRetry.sha256, _sha256);

      final api = _FakeUploadApi()
        ..initiation = const InitiatedAsset(
          assetId: 'backend-after-explicit-retry',
          alreadyUploaded: true,
          partSize: 8 * 1024 * 1024,
          completedPartNumbers: <int>[],
        );
      final media = _FakeMediaLibrary();
      final tasks = _FakeTaskSyncApi();
      final transfer = _FakePartTransferClient();
      final coordinator = UploadCoordinator(
        queue: queue,
        mediaLibrary: media,
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: tasks,
        now: () => now,
      );
      addTearDown(coordinator.dispose);

      await coordinator.runUntilIdle(_taskId);
      expect(media.released, isEmpty);
      expect(tasks.sealCalls, 0);

      await coordinator.retryFailed(_taskId, _asset1MiB.id);

      final uploaded = (await queue.find(_asset1MiB.id))!;
      expect(uploaded.state, LocalUploadState.uploaded);
      expect(uploaded.retryCount, 1);
      expect(uploaded.sha256, _sha256);
      expect(api.initiatedRetryFlags, <bool>[true]);
      expect(api.initiatedAssets.single.asset.id, _asset1MiB.id);
      expect(api.initiatedAssets.single.sha256, _sha256);
      expect(transfer.requests, isEmpty);
      expect(media.released.map((asset) => asset.id), <String>[_asset1MiB.id]);
      expect(tasks.sealCalls, 1);
    },
  );

  test(
    'restart terminalizes an expired eighth-attempt lease without bytes',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      await queue.enqueue(_taskId, <LocalAssetRef>[_asset1MiB]);
      final now = DateTime.utc(2026, 8, 15, 5);
      for (var attempt = 1; attempt <= 8; attempt += 1) {
        final claimed = await queue.nextRunnable(
          _taskId,
          ownerId: 'crashed-attempt-$attempt',
          now: now,
          leaseDuration: Duration.zero,
        );
        expect(claimed!.retryCount, attempt);
        if (attempt < 8) {
          await queue.scheduleRetry(
            claimed.claim,
            retryCount: attempt,
            nextAttemptAt: now,
            code: 'network_unavailable',
            safeMessage: '网络不可用，请检查网络后重试',
          );
        }
      }
      final api = _FakeUploadApi();
      final transfer = _FakePartTransferClient();
      final restarted = UploadCoordinator(
        queue: UploadQueueRepository(database),
        mediaLibrary: _FakeMediaLibrary(),
        api: api,
        transferClient: transfer,
        network: _FakeNetworkMonitor(NetworkTransport.wifi),
        tasks: _FakeTaskSyncApi(),
        now: () => now,
      );
      addTearDown(restarted.dispose);

      await restarted.runUntilIdle(_taskId);

      final item = (await queue.find(_asset1MiB.id))!;
      expect(item.state, LocalUploadState.failedPermanent);
      expect(item.retryCount, 8);
      expect(item.lastErrorCode, 'attempt_interrupted');
      expect(api.initiateCallCount, 0);
      expect(transfer.requests, isEmpty);
    },
  );
}

const _sha256 =
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

const _asset17MiB = LocalAssetRef(
  id: 'ios:asset-17mib',
  displayName: 'IMG_0017.JPG',
  mediaType: 'image/jpeg',
  byteSize: 17 * 1024 * 1024,
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/app-support/asset-17mib/original',
);

const _asset25MiB = LocalAssetRef(
  id: 'ios:asset-25mib',
  displayName: 'IMG_0025.JPG',
  mediaType: 'image/jpeg',
  byteSize: 25 * 1024 * 1024,
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/app-support/asset-25mib/original',
);

const _asset1MiB = LocalAssetRef(
  id: 'ios:asset-1mib',
  displayName: 'IMG_0001.JPG',
  mediaType: 'image/jpeg',
  byteSize: 1 * 1024 * 1024,
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/app-support/asset-1mib/original',
);

LocalAssetRef _assetWithId(String id) => LocalAssetRef(
  id: id,
  displayName: '$id.JPG',
  mediaType: 'image/jpeg',
  byteSize: 1024 * 1024,
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/app-support/$id/original',
);

final class _FakeMediaLibrary implements MediaLibraryBridge {
  _FakeMediaLibrary({this.onRelease});

  final Future<void> Function(LocalAssetRef asset)? onRelease;
  final List<LocalAssetRef> released = <LocalAssetRef>[];
  Object? releaseError;

  @override
  Future<String> computeSha256(LocalAssetRef ref) async => 'a' * 64;

  @override
  Future<void> flushPendingCleanup() async {}

  @override
  Future<MediaPickResult> pickImages({required int remainingSlots}) =>
      throw UnimplementedError();

  @override
  Future<void> release(LocalAssetRef ref) async {
    await onRelease?.call(ref);
    if (releaseError case final error?) throw error;
    released.add(ref);
  }
}

final class _FakeUploadApi implements UploadApi {
  late InitiatedAsset initiation;
  final List<PendingLocalAsset> initiatedAssets = <PendingLocalAsset>[];
  final List<bool> initiatedRetryFlags = <bool>[];
  final List<Object> initiateFailures = <Object>[];
  int initiateCallCount = 0;
  final List<List<int>> signedPartRequests = <List<int>>[];
  final List<Map<int, String>> completedEtags = <Map<int, String>>[];
  final List<({String assetId, String code})> failureReports =
      <({String assetId, String code})>[];
  Object? failureReportError;
  Object? signPartsError;
  Object? completeError;

  @override
  Future<void> complete(String assetId, Map<int, String> etags) async {
    if (completeError case final error?) throw error;
    completedEtags.add(Map<int, String>.of(etags));
  }

  @override
  Future<InitiatedAsset> initiate(
    String taskId,
    PendingLocalAsset asset, {
    bool retry = false,
  }) async {
    initiateCallCount += 1;
    if (initiateFailures.isNotEmpty) throw initiateFailures.removeAt(0);
    initiatedAssets.add(asset);
    initiatedRetryFlags.add(retry);
    return initiation;
  }

  @override
  Future<void> reportFailure(
    String assetId,
    String code,
    String safeMessage,
  ) async {
    failureReports.add((assetId: assetId, code: code));
    if (failureReportError case final error?) throw error;
  }

  @override
  Future<void> reportUnsupported(
    String taskId,
    UnsupportedLocalAsset asset,
  ) async {}

  @override
  Future<Map<int, Uri>> signParts(String assetId, List<int> partNumbers) async {
    if (signPartsError case final error?) throw error;
    signedPartRequests.add(List<int>.of(partNumbers));
    return <int, Uri>{
      for (final partNumber in partNumbers)
        partNumber: Uri.parse('https://uploads.invalid/part/$partNumber'),
    };
  }
}

final class _FakePartTransferClient implements PartTransferClient {
  _FakePartTransferClient({this.yieldBeforeComplete = false});

  final bool yieldBeforeComplete;
  final List<TransferPartRequest> requests = <TransferPartRequest>[];
  final List<Object> failures = <Object>[];
  Object? alwaysFailure;
  Future<void> Function(TransferPartRequest request)? onTransfer;
  int _inFlight = 0;
  int maxInFlight = 0;

  @override
  Future<TransferPartResult> transfer(TransferPartRequest request) async {
    requests.add(request);
    _inFlight += 1;
    if (_inFlight > maxInFlight) maxInFlight = _inFlight;
    try {
      if (yieldBeforeComplete) await Future<void>.delayed(Duration.zero);
      await onTransfer?.call(request);
      if (failures.isNotEmpty) throw failures.removeAt(0);
      if (alwaysFailure case final failure?) throw failure;
      return TransferPartResult(etag: 'etag-${request.partNumber}');
    } finally {
      _inFlight -= 1;
    }
  }
}

final class _FakeNetworkMonitor implements NetworkMonitor {
  _FakeNetworkMonitor(this.value);

  NetworkTransport value;
  final StreamController<NetworkTransport> _changes =
      StreamController<NetworkTransport>.broadcast();

  @override
  Stream<NetworkTransport> get changes => _changes.stream;

  @override
  Future<NetworkTransport> current() async => value;

  void emit(NetworkTransport next) {
    value = next;
    _changes.add(next);
  }
}

final class _FakeTaskSyncApi implements TaskSyncApi {
  int getCalls = 0;
  int sealCalls = 0;
  final List<Object> getFailures = <Object>[];
  final List<Object> sealFailures = <Object>[];
  Completer<void>? sealStarted;
  Future<void>? sealBarrier;
  TaskSummary? summary;

  @override
  Future<TaskSummary> get(String taskId) async {
    getCalls += 1;
    if (getFailures.isNotEmpty) throw getFailures.removeAt(0);
    return summary ?? _taskSummary(taskId);
  }

  @override
  Future<TaskSummary> seal(String taskId) async {
    sealCalls += 1;
    final started = sealStarted;
    if (started != null && !started.isCompleted) started.complete();
    await sealBarrier;
    if (sealFailures.isNotEmpty) throw sealFailures.removeAt(0);
    return _taskSummary(taskId, status: 'ai_processing');
  }
}

TaskSummary _taskSummary(
  String taskId, {
  String status = 'waiting_upload',
  int totalAssets = 1,
  int uploadedAssets = 1,
  int failedAssets = 0,
}) {
  final now = DateTime.utc(2026, 8, 15);
  return TaskSummary(
    id: taskId,
    name: 'Import',
    status: status,
    totalAssets: totalAssets,
    uploadedAssets: uploadedAssets,
    failedAssets: failedAssets,
    unsupportedAssets: 0,
    createdAt: now,
    updatedAt: now,
  );
}

Future<void> _eventually(
  Future<bool> Function() condition, {
  Duration pollInterval = Duration.zero,
}) async {
  for (var turn = 0; turn < 100; turn += 1) {
    if (await condition()) return;
    await Future<void>.delayed(pollInterval);
  }
  fail('condition did not become true');
}

Future<void> _createV1UploadDatabase(File file, LocalAssetRef asset) async {
  final legacy = NativeDatabase(file);
  await legacy.ensureOpen(const _SchemaV1ExecutorUser());
  await legacy.runCustom('''
    CREATE TABLE upload_items (
      local_asset_id TEXT NOT NULL PRIMARY KEY,
      task_id TEXT NOT NULL,
      local_ref_json TEXT NOT NULL,
      byte_size INTEGER NOT NULL,
      sha256 TEXT,
      backend_asset_id TEXT,
      provider_upload_id TEXT,
      part_size INTEGER NOT NULL,
      retry_count INTEGER NOT NULL DEFAULT 0,
      next_attempt_at INTEGER,
      state TEXT NOT NULL,
      last_error_code TEXT,
      last_error_message TEXT
    )
  ''');
  await legacy.runCustom('''
    CREATE TABLE upload_parts (
      local_asset_id TEXT NOT NULL,
      part_number INTEGER NOT NULL,
      etag TEXT,
      PRIMARY KEY (local_asset_id, part_number),
      FOREIGN KEY (local_asset_id) REFERENCES upload_items(local_asset_id)
        ON DELETE CASCADE
    )
  ''');
  await legacy.runCustom('''
    CREATE TABLE import_queue_tasks (
      task_id TEXT NOT NULL PRIMARY KEY,
      seal_when_idle INTEGER NOT NULL DEFAULT 0
    )
  ''');
  await legacy.runCustom('''
    CREATE TABLE import_queue_authorities (
      task_id TEXT NOT NULL,
      local_asset_id TEXT NOT NULL,
      disposition TEXT NOT NULL,
      PRIMARY KEY (task_id, local_asset_id)
    )
  ''');
  await legacy.runCustom('''
    CREATE TABLE import_cleanup_refs (
      task_id TEXT NOT NULL,
      locator TEXT NOT NULL,
      local_asset_id TEXT NOT NULL,
      local_ref_json TEXT NOT NULL,
      PRIMARY KEY (task_id, locator)
    )
  ''');
  await legacy.runCustom(
    'INSERT INTO upload_items '
    '(local_asset_id, task_id, local_ref_json, byte_size, sha256, '
    'backend_asset_id, provider_upload_id, part_size, state) '
    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
    <Object?>[
      asset.id,
      _taskId,
      jsonEncode(asset.toMap()),
      asset.byteSize,
      _sha256,
      'legacy-backend-asset',
      'legacy-provider-upload',
      8 * 1024 * 1024,
      LocalUploadState.awaitingParts.name,
    ],
  );
  await legacy.runCustom(
    'INSERT INTO upload_parts (local_asset_id, part_number) VALUES (?, 1)',
    <Object?>[asset.id],
  );
  await legacy.runCustom(
    'INSERT INTO import_queue_authorities '
    '(task_id, local_asset_id, disposition) VALUES (?, ?, ?)',
    <Object?>[_taskId, asset.id, ServerAssetDisposition.accepted.name],
  );
  await legacy.runCustom('PRAGMA user_version = 1');
  await legacy.close();
}

final class _SchemaV1ExecutorUser implements QueryExecutorUser {
  const _SchemaV1ExecutorUser();

  @override
  int get schemaVersion => 1;

  @override
  Future<void> beforeOpen(
    QueryExecutor executor,
    OpeningDetails details,
  ) async {}
}
