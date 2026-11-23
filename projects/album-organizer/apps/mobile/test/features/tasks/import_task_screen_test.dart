import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/models.dart';
import 'package:photo_organizer/core/media/local_asset_ref.dart';
import 'package:photo_organizer/core/media/media_library_bridge.dart';
import 'package:photo_organizer/core/upload/upload_models.dart';
import 'package:photo_organizer/features/tasks/import_task_screen.dart';
import 'package:photo_organizer/features/tasks/task_repository.dart';

const _taskId = '11111111-1111-4111-8111-111111111111';

void main() {
  test(
    'controller appends batches and asks only for remaining slots',
    () async {
      final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
        MediaPickResult(
          selected: <LocalAssetRef>[_asset(1), _asset(2)],
          unsupported: const <UnsupportedLocalAsset>[],
        ),
        MediaPickResult(
          selected: <LocalAssetRef>[_asset(3)],
          unsupported: const <UnsupportedLocalAsset>[],
        ),
      ]);
      final queue = _FakeImportQueue();
      final controller = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: bridge,
        unsupportedReporter: _FakeUnsupportedReporter(),
        queue: queue,
      );

      await controller.addPhotos();
      await controller.addPhotos();

      expect(bridge.requestedLimits, <int>[10000, 9998]);
      expect(controller.state.acceptedCount, 3);
      expect(controller.state.queuedCount, 3);
      expect(queue.accepted.map((asset) => asset.id), <String>[
        'asset-1',
        'asset-2',
        'asset-3',
      ]);
    },
  );

  test('9999 accepted assets leave one native picker slot', () async {
    final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
      MediaPickResult(
        selected: <LocalAssetRef>[_asset(1)],
        unsupported: const <UnsupportedLocalAsset>[],
      ),
    ]);
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: bridge,
      taskSummarySource: _FakeTaskSummarySource(totalAssets: 9999),
      unsupportedReporter: _FakeUnsupportedReporter(),
      queue: _FakeImportQueue(),
    );

    await controller.addPhotos();
    await controller.addPhotos();

    expect(bridge.requestedLimits, <int>[1]);
    expect(controller.state.acceptedCount, 10000);
  });

  testWidgets('9999 accepted assets pass one remaining slot from add photos', (
    tester,
  ) async {
    final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
      const MediaPickResult(
        selected: <LocalAssetRef>[],
        unsupported: <UnsupportedLocalAsset>[],
      ),
    ]);
    await tester.pumpWidget(
      ProviderScope(
        overrides: <Override>[
          mediaLibraryBridgeProvider.overrideWithValue(bridge),
          importTaskSummarySourceProvider.overrideWithValue(
            const _FakeTaskSummarySource(totalAssets: 9999),
          ),
          unsupportedAssetReporterProvider.overrideWithValue(
            _FakeUnsupportedReporter(),
          ),
          importQueueBoundaryProvider(_taskId)
              .overrideWithValue(_FakeImportQueue()),
        ],
        child: const MaterialApp(home: ImportTaskScreen(taskId: _taskId)),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('add-photos')));
    await tester.pumpAndSettle();

    expect(bridge.requestedLimits, <int>[1]);
  });

  testWidgets('10000 accepted assets disable add photos at task capacity', (
    tester,
  ) async {
    final bridge = _FakeMediaLibraryBridge(const <MediaPickResult>[]);
    await tester.pumpWidget(
      ProviderScope(
        overrides: <Override>[
          mediaLibraryBridgeProvider.overrideWithValue(bridge),
          importTaskSummarySourceProvider.overrideWithValue(
            const _FakeTaskSummarySource(totalAssets: 10000),
          ),
          unsupportedAssetReporterProvider.overrideWithValue(
            _FakeUnsupportedReporter(),
          ),
          importQueueBoundaryProvider(_taskId)
              .overrideWithValue(_FakeImportQueue()),
        ],
        child: const MaterialApp(home: ImportTaskScreen(taskId: _taskId)),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('已接受 10000'), findsOneWidget);
    expect(find.text('已达到 10000 张上限'), findsOneWidget);
    final button = tester.widget<FilledButton>(
      find.byKey(const Key('add-photos')),
    );
    expect(button.onPressed, isNull);
    expect(bridge.requestedLimits, isEmpty);
  });

  testWidgets('unsupported item stays visible and is reported once by id', (
    tester,
  ) async {
    const unsupported = UnsupportedLocalAsset(
      id: 'android:raw-1',
      displayName: 'RAW_1.dng',
      reason: 'unsupported_media_type',
      mediaType: 'image/x-adobe-dng',
      byteSize: 55,
    );
    final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
      const MediaPickResult(
        selected: <LocalAssetRef>[],
        unsupported: <UnsupportedLocalAsset>[unsupported],
      ),
      const MediaPickResult(
        selected: <LocalAssetRef>[],
        unsupported: <UnsupportedLocalAsset>[unsupported],
      ),
    ]);
    final reporter = _FakeUnsupportedReporter();
    await tester.pumpWidget(
      ProviderScope(
        overrides: <Override>[
          mediaLibraryBridgeProvider.overrideWithValue(bridge),
          importTaskSummarySourceProvider.overrideWithValue(
            _SequencedTaskSummarySource(<TaskSummary>[
              _taskSummary(),
              _taskSummary(unsupportedAssets: 1),
            ]),
          ),
          unsupportedAssetReporterProvider.overrideWithValue(reporter),
          importQueueBoundaryProvider(_taskId)
              .overrideWithValue(_FakeImportQueue()),
        ],
        child: const MaterialApp(home: ImportTaskScreen(taskId: _taskId)),
      ),
    );

    await tester.tap(find.byKey(const Key('add-photos')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('add-photos')));
    await tester.pumpAndSettle();

    expect(find.text('RAW_1.dng'), findsOneWidget);
    expect(find.text('原因：不支持的文件格式'), findsOneWidget);
    expect(find.text('不支持 1'), findsOneWidget);
    expect(reporter.reported.map((entry) => entry.asset.id), <String>[
      'android:raw-1',
    ]);
  });

  test('finish sets sealWhenIdle while pending assets remain queued', () async {
    final queue = _FakeImportQueue();
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: _FakeMediaLibraryBridge(<MediaPickResult>[
        MediaPickResult(
          selected: <LocalAssetRef>[_asset(1), _asset(2)],
          unsupported: const <UnsupportedLocalAsset>[],
        ),
      ]),
      unsupportedReporter: _FakeUnsupportedReporter(),
      queue: queue,
    );
    await controller.addPhotos();

    await controller.finishSelection();

    expect(controller.state.queuedCount, 2);
    expect(controller.state.sealWhenIdle, isTrue);
    expect(queue.sealWhenIdleCalls, 1);
    expect(queue.sealRequestedWithPendingCount, 2);
  });

  test('finish flushes bridge-owned cleanup before sealing', () async {
    final bridge = _FakeMediaLibraryBridge(const <MediaPickResult>[])
      ..flushError = StateError('native cleanup unavailable');
    final queue = _FakeImportQueue();
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: bridge,
      unsupportedReporter: _FakeUnsupportedReporter(),
      queue: queue,
    );

    await controller.finishSelection();

    expect(bridge.flushCalls, 1);
    expect(queue.sealWhenIdleCalls, 0);
    expect(controller.state.sealWhenIdle, isFalse);
    expect(controller.state.errorMessage, '无法完成选图，请稍后重试');

    bridge.flushError = null;
    await controller.finishSelection();

    expect(bridge.flushCalls, 2);
    expect(queue.sealWhenIdleCalls, 1);
    expect(controller.state.sealWhenIdle, isTrue);
  });

  test(
    'dispose cleanup failure is retried by replacement before seal',
    () async {
      final firstFlush = Completer<void>();
      final replacementRetry = Completer<void>();
      final bridge = _FakeMediaLibraryBridge(const <MediaPickResult>[])
        ..flushFutures.addAll(<Future<void>>[
          firstFlush.future,
          replacementRetry.future,
        ]);
      final queue = _FakeImportQueue();
      final container = ProviderContainer(
        overrides: <Override>[
          mediaLibraryBridgeProvider.overrideWithValue(bridge),
          importTaskSummarySourceProvider.overrideWithValue(
            const _FakeTaskSummarySource(),
          ),
          unsupportedAssetReporterProvider.overrideWithValue(
            _FakeUnsupportedReporter(),
          ),
          importQueueBoundaryProvider(_taskId).overrideWithValue(queue),
        ],
      );
      addTearDown(container.dispose);
      final provider = importTaskControllerProvider(_taskId);
      final subscription = container.listen(
        provider,
        (_, _) {},
        fireImmediately: true,
      );
      addTearDown(subscription.close);
      await container.read(provider.notifier).ready;

      container.invalidate(provider);
      expect(bridge.flushCalls, 1);
      final replacement = container.read(provider.notifier);
      final finish = replacement.finishSelection();
      await Future<void>.delayed(Duration.zero);

      expect(bridge.flushCalls, 1);
      expect(queue.sealWhenIdleCalls, 0);

      firstFlush.completeError(StateError('native cleanup unavailable'));
      await Future<void>.delayed(Duration.zero);
      expect(bridge.flushCalls, 2);
      expect(queue.sealWhenIdleCalls, 0);

      replacementRetry.complete();
      await finish;

      expect(bridge.flushCalls, 3);
      expect(queue.sealWhenIdleCalls, 1);
      expect(replacement.state.sealWhenIdle, isTrue);
    },
  );

  test(
    'replacement waits for a disposed picker to settle and flush before seal',
    () async {
      final cleanupRetry = Completer<void>();
      final bridge = _DelayedMediaLibraryBridge()
        ..flushFutures.add(cleanupRetry.future);
      final queue = _FakeImportQueue();
      final container = ProviderContainer(
        overrides: <Override>[
          mediaLibraryBridgeProvider.overrideWithValue(bridge),
          importTaskSummarySourceProvider.overrideWithValue(
            const _FakeTaskSummarySource(),
          ),
          unsupportedAssetReporterProvider.overrideWithValue(
            _FakeUnsupportedReporter(),
          ),
          importQueueBoundaryProvider(_taskId).overrideWithValue(queue),
        ],
      );
      addTearDown(container.dispose);
      final provider = importTaskControllerProvider(_taskId);
      final subscription = container.listen(
        provider,
        (_, _) {},
        fireImmediately: true,
      );
      addTearDown(subscription.close);
      final original = container.read(provider.notifier);
      await original.ready;
      final oldAdd = original.addPhotos();
      await bridge.called.future;

      container.invalidate(provider);
      final replacement = container.read(provider.notifier);
      final finish = replacement.finishSelection();
      await Future<void>.delayed(Duration.zero);

      expect(bridge.flushCalls, 0);
      expect(queue.sealWhenIdleCalls, 0);

      bridge.completeError(
        const MediaLibraryException(
          code: 'asset_cleanup_failed',
          userMessage: '无法安全释放照片，请稍后重试',
        ),
      );
      await oldAdd;
      await Future<void>.delayed(Duration.zero);

      expect(bridge.flushCalls, 1);
      expect(queue.sealWhenIdleCalls, 0);

      cleanupRetry.complete();
      await finish;

      expect(bridge.flushCalls, 2);
      expect(queue.sealWhenIdleCalls, 1);
      expect(replacement.state.sealWhenIdle, isTrue);
    },
  );

  test(
    'picker registered after disposal extends the shared cleanup barrier',
    () async {
      final firstFlush = Completer<void>();
      final lateCleanup = Completer<void>();
      final bridge = _DelayedMediaLibraryBridge()
        ..flushFutures.addAll(<Future<void>>[
          firstFlush.future,
          lateCleanup.future,
        ]);
      final coordinator = MediaCleanupCoordinator(bridge);

      coordinator.flushForControllerDisposal();
      var recovered = false;
      final recovery = coordinator.recoverForControllerInitialization().then(
        (_) => recovered = true,
      );
      final pick = coordinator.pickImages(remainingSlots: 1);
      await bridge.called.future;

      firstFlush.complete();
      await Future<void>.delayed(Duration.zero);

      expect(recovered, isFalse);

      bridge.completeError(
        const MediaLibraryException(
          code: 'asset_cleanup_failed',
          userMessage: '无法安全释放照片，请稍后重试',
        ),
      );
      await expectLater(pick, throwsA(isA<MediaLibraryException>()));
      await Future<void>.delayed(Duration.zero);

      expect(bridge.flushCalls, 2);
      expect(recovered, isFalse);

      lateCleanup.complete();
      await recovery;

      expect(recovered, isTrue);
    },
  );

  test(
    'deferred queue rejects enqueue for a server-accounted ID atomically',
    () async {
      final queue = DeferredImportQueueBoundary();
      await queue.recordServerDisposition(
        _taskId,
        const UnsupportedReportReceipt(
          localAssetId: 'asset-9',
          disposition: ServerAssetDisposition.accepted,
        ),
      );

      await expectLater(
        queue.enqueue(_taskId, <LocalAssetRef>[_asset(9)]),
        throwsStateError,
      );

      final snapshot = await queue.snapshot(_taskId);
      expect(snapshot.unacknowledgedAssetIds, isEmpty);
      expect(snapshot.queued, 0);
      expect(snapshot.serverAcceptedAssetIds, <String>{'asset-9'});
    },
  );

  test(
    'disposing while the native picker is open does not update dead UI state',
    () async {
      final bridge = _DelayedMediaLibraryBridge();
      final controller = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: bridge,
        unsupportedReporter: _FakeUnsupportedReporter(),
        queue: _FakeImportQueue(),
      );

      final operation = controller.addPhotos();
      await bridge.called.future;
      controller.dispose();
      bridge.complete(
        MediaPickResult(
          selected: <LocalAssetRef>[_asset(9)],
          unsupported: const <UnsupportedLocalAsset>[],
        ),
      );

      await expectLater(operation, completes);
      expect(bridge.released.map((asset) => asset.id), <String>['asset-9']);
    },
  );

  test('a failed queue handoff can accept the same asset on retry', () async {
    final queue = _FailOnceImportQueue();
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: _FakeMediaLibraryBridge(<MediaPickResult>[
        MediaPickResult(
          selected: <LocalAssetRef>[_asset(7)],
          unsupported: const <UnsupportedLocalAsset>[],
        ),
        MediaPickResult(
          selected: <LocalAssetRef>[_asset(7)],
          unsupported: const <UnsupportedLocalAsset>[],
        ),
      ]),
      unsupportedReporter: _FakeUnsupportedReporter(),
      queue: queue,
    );

    await controller.addPhotos();
    await controller.addPhotos();

    expect(controller.state.acceptedCount, 1);
    expect(controller.state.queuedCount, 1);
  });

  test('enqueue failure keeps unsupported reported and releases every selected ref once', () async {
    const unsupported = UnsupportedLocalAsset(
      id: 'android:raw-owned',
      displayName: 'RAW.dng',
      reason: 'unsupported_media_type',
    );
    final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
      MediaPickResult(
        selected: <LocalAssetRef>[_asset(1), _asset(2)],
        unsupported: const <UnsupportedLocalAsset>[unsupported],
      ),
    ]);
    final reporter = _FakeUnsupportedReporter();
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: bridge,
      unsupportedReporter: reporter,
      queue: _AlwaysFailImportQueue(),
    );

    await controller.addPhotos();

    expect(controller.state.unsupported, const <UnsupportedLocalAsset>[
      unsupported,
    ]);
    expect(reporter.reported.single.asset.id, 'android:raw-owned');
    expect(bridge.released.map((asset) => asset.id), <String>[
      'asset-1',
      'asset-2',
    ]);
    expect(controller.state.acceptedCount, 0);
    expect(controller.state.queuedCount, 0);
    expect(controller.state.errorMessage, '无法添加照片，请重新选择');
  });

  test('release failure after rejected handoff is displayed safely', () async {
    final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
      MediaPickResult(
        selected: <LocalAssetRef>[_asset(44)],
        unsupported: const <UnsupportedLocalAsset>[],
      ),
    ])..releaseError = StateError('secret locator /outside/private.jpg');
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: bridge,
      unsupportedReporter: _FakeUnsupportedReporter(),
      queue: _AlwaysFailImportQueue(),
    );

    await controller.addPhotos();

    expect(controller.state.errorMessage, '无法添加照片，且部分照片未能安全释放');
    expect(
      controller.state.errorMessage,
      isNot(contains('/outside/private.jpg')),
    );
    expect(controller.state.acceptedCount, 0);
  });

  test(
    'resolved handoff failure does not permanently prevent sealing',
    () async {
      final queue = _AlwaysFailImportQueue();
      final controller = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: _FakeMediaLibraryBridge(<MediaPickResult>[
          MediaPickResult(
            selected: <LocalAssetRef>[_asset(45)],
            unsupported: const <UnsupportedLocalAsset>[],
          ),
        ]),
        unsupportedReporter: _FakeUnsupportedReporter(),
        queue: queue,
      );

      await controller.addPhotos();
      await controller.finishSelection();

      expect(queue.sealWhenIdleCalls, 1);
      expect(controller.state.sealWhenIdle, isTrue);
    },
  );

  test(
    'cleanup ownership survives controller disposal and is retried before seal',
    () async {
      final queue = _AlwaysFailImportQueue();
      final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
        MediaPickResult(
          selected: <LocalAssetRef>[_asset(46)],
          unsupported: const <UnsupportedLocalAsset>[],
        ),
      ])..releaseError = StateError('provider temporarily unavailable');
      final first = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: bridge,
        unsupportedReporter: _FakeUnsupportedReporter(),
        queue: queue,
      );

      await first.addPhotos();
      first.dispose();
      bridge.releaseError = null;
      final resumed = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: bridge,
        unsupportedReporter: _FakeUnsupportedReporter(),
        queue: queue,
      );

      await resumed.finishSelection();

      expect(bridge.released.map((asset) => asset.id), <String>[
        'asset-46',
        'asset-46',
      ]);
      expect(queue.sealWhenIdleCalls, 1);
      expect(resumed.state.sealWhenIdle, isTrue);
    },
  );

  test('failed duplicate cleanup remains visible and prevents sealing after handoff', () async {
    final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
      MediaPickResult(
        selected: <LocalAssetRef>[_asset(7), _asset(8)],
        unsupported: const <UnsupportedLocalAsset>[],
      ),
    ])..releaseError = StateError('secret locator /outside/private.jpg');
    final queue = _FakeImportQueue(
      initialSnapshot: const ImportQueueSnapshot(
        unacknowledgedAssetIds: <String>{'asset-7'},
        queued: 1,
        uploaded: 0,
        failed: 0,
      ),
    );
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: bridge,
      unsupportedReporter: _FakeUnsupportedReporter(),
      queue: queue,
    );

    await controller.addPhotos();
    await controller.finishSelection();

    expect(queue.accepted.map((asset) => asset.id), <String>['asset-8']);
    expect(bridge.released.map((asset) => asset.id), <String>[
      'asset-7',
      'asset-7',
    ]);
    expect(controller.state.errorMessage, '部分照片未能安全释放，请稍后重试');
    expect(controller.state.errorMessage, isNot(contains('/outside')));
    expect(queue.sealWhenIdleCalls, 0);
  });

  test('duplicate cleanup can be retried before sealing', () async {
    final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
      MediaPickResult(
        selected: <LocalAssetRef>[_asset(7), _asset(8)],
        unsupported: const <UnsupportedLocalAsset>[],
      ),
    ])..releaseError = StateError('provider temporarily unavailable');
    final queue = _FakeImportQueue(
      initialSnapshot: const ImportQueueSnapshot(
        unacknowledgedAssetIds: <String>{'asset-7'},
        queued: 1,
        uploaded: 0,
        failed: 0,
      ),
    );
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: bridge,
      unsupportedReporter: _FakeUnsupportedReporter(),
      queue: queue,
    );

    await controller.addPhotos();
    bridge.releaseError = null;
    await controller.finishSelection();

    expect(bridge.released.map((asset) => asset.id), <String>[
      'asset-7',
      'asset-7',
    ]);
    expect(queue.sealWhenIdleCalls, 1);
    expect(controller.state.sealWhenIdle, isTrue);
  });

  test('picker in flight prevents finish from setting sealWhenIdle', () async {
    final bridge = _DelayedMediaLibraryBridge();
    final queue = _FakeImportQueue();
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: bridge,
      unsupportedReporter: _FakeUnsupportedReporter(),
      queue: queue,
    );
    await controller.ready;

    final selecting = controller.addPhotos();
    await bridge.called.future;
    await controller.finishSelection();

    expect(queue.sealWhenIdleCalls, 0);
    expect(controller.state.sealWhenIdle, isFalse);
    bridge.complete(const MediaPickResult(selected: [], unsupported: []));
    await selecting;
  });

  test('sealed controller rejects add without opening native picker', () async {
    final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[]);
    final queue = _FakeImportQueue();
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: bridge,
      unsupportedReporter: _FakeUnsupportedReporter(),
      queue: queue,
    );
    await controller.ready;

    await controller.finishSelection();
    await controller.addPhotos();

    expect(controller.state.sealWhenIdle, isTrue);
    expect(queue.sealWhenIdleCalls, 1);
    expect(bridge.requestedLimits, isEmpty);
  });

  test(
    'initialization reconciles server summary and durable queue snapshot',
    () async {
      final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
        MediaPickResult(
          selected: <LocalAssetRef>[_asset(7), _asset(8)],
          unsupported: const <UnsupportedLocalAsset>[
            UnsupportedLocalAsset(
              id: 'android:raw-restart',
              displayName: 'restart.dng',
              reason: 'unsupported_media_type',
            ),
          ],
        ),
      ]);
      final queue = _FakeImportQueue(
        initialSnapshot: const ImportQueueSnapshot(
          unacknowledgedAssetIds: <String>{'asset-7'},
          queued: 1,
          uploaded: 1,
          failed: 1,
        ),
      );
      final reporter = _FakeUnsupportedReporter();
      final controller = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: bridge,
        taskSummarySource: _SequencedTaskSummarySource(<TaskSummary>[
          _taskSummary(
            totalAssets: 10,
            uploadedAssets: 5,
            failedAssets: 1,
            unsupportedAssets: 2,
          ),
          _taskSummary(
            totalAssets: 10,
            uploadedAssets: 5,
            failedAssets: 1,
            unsupportedAssets: 3,
          ),
        ]),
        unsupportedReporter: reporter,
        queue: queue,
      );

      await controller.ready;

      expect(controller.state.serverAcceptedCount, 10);
      expect(controller.state.localPendingAcceptedCount, 1);
      expect(controller.state.acceptedCount, 11);
      expect(controller.state.queuedCount, 1);
      expect(controller.state.uploadedCount, 6);
      expect(controller.state.failedCount, 2);
      expect(controller.state.unsupportedCount, 2);

      await controller.addPhotos();

      expect(bridge.requestedLimits, <int>[9989]);
      expect(bridge.released.map((asset) => asset.id), <String>['asset-7']);
      expect(queue.accepted.map((asset) => asset.id), <String>['asset-8']);
      expect(controller.state.acceptedCount, 12);
      expect(controller.state.localPendingAcceptedCount, 2);
      expect(controller.state.unsupportedCount, 3);
      expect(reporter.reported.single.asset.id, 'android:raw-restart');
    },
  );

  test(
    'server-accounted accepted ID is released without increasing local delta',
    () async {
      final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
        MediaPickResult(
          selected: <LocalAssetRef>[_asset(7), _asset(9)],
          unsupported: const <UnsupportedLocalAsset>[],
        ),
      ]);
      final queue = _FakeImportQueue(
        initialSnapshot: const ImportQueueSnapshot(
          unacknowledgedAssetIds: <String>{'asset-8'},
          serverAcceptedAssetIds: <String>{'asset-7'},
          queued: 1,
          uploaded: 0,
          failed: 0,
        ),
      );
      final controller = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: bridge,
        taskSummarySource: const _FakeTaskSummarySource(totalAssets: 10),
        unsupportedReporter: _FakeUnsupportedReporter(),
        queue: queue,
      );

      await controller.addPhotos();

      expect(bridge.released.map((asset) => asset.id), <String>['asset-7']);
      expect(queue.accepted.map((asset) => asset.id), <String>['asset-9']);
      expect(controller.state.acceptedCount, 12);
      expect(controller.state.localPendingAssetIds, <String>{
        'asset-8',
        'asset-9',
      });
    },
  );

  test('fresh queue authority rejects unsupported reporting for a local accepted ID', () async {
    const collision = UnsupportedLocalAsset(
      id: 'asset-7',
      displayName: 'IMG_7.JPG',
      reason: 'unsupported_media_type',
    );
    final reporter = _FakeUnsupportedReporter();
    final queue = _FakeImportQueue();
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: _FakeMediaLibraryBridge(<MediaPickResult>[
        const MediaPickResult(
          selected: <LocalAssetRef>[],
          unsupported: <UnsupportedLocalAsset>[collision],
        ),
      ]),
      unsupportedReporter: reporter,
      queue: queue,
    );
    await controller.ready;
    queue.replaceSnapshot(
      const ImportQueueSnapshot(
        unacknowledgedAssetIds: <String>{'asset-7'},
        queued: 1,
        uploaded: 0,
        failed: 0,
      ),
    );

    await controller.addPhotos();

    expect(reporter.reported, isEmpty);
    expect(controller.state.unsupported, isEmpty);
    expect(controller.state.unsupportedCount, 0);
    expect(controller.state.acceptedCount, 1);
    expect(controller.state.errorMessage, '照片选择结果无效，请重新选择');
  });

  test('fresh queue authority rejects unsupported reporting for a server accepted ID', () async {
    const collision = UnsupportedLocalAsset(
      id: 'asset-7',
      displayName: 'IMG_7.JPG',
      reason: 'unsupported_media_type',
    );
    final reporter = _FakeUnsupportedReporter();
    final queue = _FakeImportQueue();
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: _FakeMediaLibraryBridge(<MediaPickResult>[
        const MediaPickResult(
          selected: <LocalAssetRef>[],
          unsupported: <UnsupportedLocalAsset>[collision],
        ),
      ]),
      taskSummarySource: const _FakeTaskSummarySource(totalAssets: 1),
      unsupportedReporter: reporter,
      queue: queue,
    );
    await controller.ready;
    queue.replaceSnapshot(
      const ImportQueueSnapshot(
        unacknowledgedAssetIds: <String>{},
        serverAcceptedAssetIds: <String>{'asset-7'},
        queued: 0,
        uploaded: 0,
        failed: 0,
      ),
    );

    await controller.addPhotos();

    expect(reporter.reported, isEmpty);
    expect(controller.state.unsupported, isEmpty);
    expect(controller.state.acceptedCount, 1);
    expect(controller.state.errorMessage, '照片选择结果无效，请重新选择');
  });

  test(
    'finish rechecks queue authority before retrying an unsupported report',
    () async {
      const collision = UnsupportedLocalAsset(
        id: 'asset-10',
        displayName: 'IMG_10.JPG',
        reason: 'unsupported_media_type',
      );
      final reporter = _FakeUnsupportedReporter()
        ..reportError = StateError('report temporarily unavailable');
      final queue = _FakeImportQueue();
      final controller = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: _FakeMediaLibraryBridge(<MediaPickResult>[
          const MediaPickResult(
            selected: <LocalAssetRef>[],
            unsupported: <UnsupportedLocalAsset>[collision],
          ),
        ]),
        unsupportedReporter: reporter,
        queue: queue,
      );

      await controller.addPhotos();
      expect(reporter.reported, hasLength(1));
      reporter.reportError = null;
      queue.replaceSnapshot(
        const ImportQueueSnapshot(
          unacknowledgedAssetIds: <String>{'asset-10'},
          queued: 1,
          uploaded: 0,
          failed: 0,
        ),
      );

      await controller.finishSelection();

      expect(reporter.reported, hasLength(1));
      expect(controller.state.unreportedUnsupportedAssetIds, isEmpty);
      expect(controller.state.unsupportedCount, 0);
      expect(controller.state.acceptedCount, 1);
      expect(queue.sealWhenIdleCalls, 1);
      expect(controller.state.sealWhenIdle, isTrue);
    },
  );

  test(
    'selected and unsupported ID collision is rejected before reporting',
    () async {
      const collision = UnsupportedLocalAsset(
        id: 'asset-8',
        displayName: 'IMG_8.JPG',
        reason: 'unsupported_media_type',
      );
      final bridge = _FakeMediaLibraryBridge(<MediaPickResult>[
        MediaPickResult(
          selected: <LocalAssetRef>[_asset(8)],
          unsupported: const <UnsupportedLocalAsset>[collision],
        ),
      ]);
      final reporter = _FakeUnsupportedReporter();
      final queue = _FakeImportQueue();
      final controller = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: bridge,
        unsupportedReporter: reporter,
        queue: queue,
      );

      await controller.addPhotos();

      expect(reporter.reported, isEmpty);
      expect(queue.accepted, isEmpty);
      expect(bridge.released.map((asset) => asset.id), <String>['asset-8']);
      expect(controller.state.acceptedCount, 0);
      expect(controller.state.unsupportedCount, 0);
      expect(controller.state.errorMessage, '照片选择结果无效，请重新选择');
    },
  );

  test('server-accounted unsupported ID stays visible without double count or report', () async {
    const known = UnsupportedLocalAsset(
      id: 'android:raw-known',
      displayName: 'known.dng',
      reason: 'unsupported_media_type',
    );
    final reporter = _FakeUnsupportedReporter();
    final controller = ImportTaskController(
      taskId: _taskId,
      mediaLibrary: _FakeMediaLibraryBridge(<MediaPickResult>[
        const MediaPickResult(
          selected: <LocalAssetRef>[],
          unsupported: <UnsupportedLocalAsset>[known],
        ),
      ]),
      taskSummarySource: const _FakeTaskSummarySource(unsupportedAssets: 2),
      unsupportedReporter: reporter,
      queue: _FakeImportQueue(
        initialSnapshot: const ImportQueueSnapshot(
          unacknowledgedAssetIds: <String>{},
          serverUnsupportedAssetIds: <String>{'android:raw-known'},
          queued: 0,
          uploaded: 0,
          failed: 0,
        ),
      ),
    );

    await controller.addPhotos();

    expect(
      controller.state.unsupported,
      hasLength(1),
      reason: controller.state.errorMessage,
    );
    expect(controller.state.unsupported.single.id, 'android:raw-known');
    expect(controller.state.unsupportedCount, 2);
    expect(reporter.reported, isEmpty);
  });

  test(
    'unsupported report is durably classified and rebaselined exactly once',
    () async {
      const raw = UnsupportedLocalAsset(
        id: 'android:raw-new',
        displayName: 'new.dng',
        reason: 'unsupported_media_type',
      );
      final source = _SequencedTaskSummarySource(<TaskSummary>[
        _taskSummary(unsupportedAssets: 2),
        _taskSummary(unsupportedAssets: 3),
      ]);
      final queue = _FakeImportQueue();
      final reporter = _FakeUnsupportedReporter();
      final controller = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: _FakeMediaLibraryBridge(<MediaPickResult>[
          const MediaPickResult(selected: [], unsupported: [raw]),
          const MediaPickResult(selected: [], unsupported: [raw]),
        ]),
        taskSummarySource: source,
        unsupportedReporter: reporter,
        queue: queue,
      );

      await controller.addPhotos();
      await controller.addPhotos();

      expect(source.calls, 2);
      expect(reporter.reported, hasLength(1));
      expect(controller.state.unsupportedCount, 3);
      expect(
        (await queue.snapshot(_taskId)).serverUnsupportedAssetIds,
        <String>{'android:raw-new'},
      );
    },
  );

  test(
    'unsupported endpoint accepted disposition never increments unsupported',
    () async {
      const collision = UnsupportedLocalAsset(
        id: 'android:already-accepted',
        displayName: 'collision.raw',
        reason: 'unsupported_media_type',
      );
      final source = _SequencedTaskSummarySource(<TaskSummary>[
        _taskSummary(totalAssets: 10, unsupportedAssets: 2),
        _taskSummary(totalAssets: 10, unsupportedAssets: 2),
      ]);
      final queue = _FakeImportQueue();
      final controller = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: _FakeMediaLibraryBridge(<MediaPickResult>[
          const MediaPickResult(selected: [], unsupported: [collision]),
        ]),
        taskSummarySource: source,
        unsupportedReporter: _FakeUnsupportedReporter(
          disposition: ServerAssetDisposition.accepted,
        ),
        queue: queue,
      );

      await controller.addPhotos();

      expect(controller.state.acceptedCount, 10);
      expect(controller.state.unsupportedCount, 2);
      expect((await queue.snapshot(_taskId)).serverAcceptedAssetIds, <String>{
        'android:already-accepted',
      });
    },
  );

  test(
    'finish retries an unresolved authoritative summary rebaseline',
    () async {
      const raw = UnsupportedLocalAsset(
        id: 'android:raw-eventual',
        displayName: 'eventual.dng',
        reason: 'unsupported_media_type',
      );
      final source = _FailOnceRefreshTaskSummarySource();
      final queue = _FakeImportQueue();
      final controller = ImportTaskController(
        taskId: _taskId,
        mediaLibrary: _FakeMediaLibraryBridge(<MediaPickResult>[
          const MediaPickResult(selected: [], unsupported: [raw]),
        ]),
        taskSummarySource: source,
        unsupportedReporter: _FakeUnsupportedReporter(),
        queue: queue,
      );

      await controller.addPhotos();
      expect(controller.state.unsupportedCount, 2);
      expect(controller.state.errorMessage, isNotNull);

      await controller.finishSelection();

      expect(source.calls, 3);
      expect(controller.state.unsupportedCount, 3);
      expect(queue.sealWhenIdleCalls, 1);
    },
  );

  testWidgets('selection disables both add and finish buttons', (tester) async {
    final bridge = _DelayedMediaLibraryBridge();
    await tester.pumpWidget(
      ProviderScope(
        overrides: <Override>[
          mediaLibraryBridgeProvider.overrideWithValue(bridge),
          importTaskSummarySourceProvider.overrideWithValue(
            const _FakeTaskSummarySource(),
          ),
          unsupportedAssetReporterProvider.overrideWithValue(
            _FakeUnsupportedReporter(),
          ),
          importQueueBoundaryProvider(_taskId)
              .overrideWithValue(_FakeImportQueue()),
        ],
        child: const MaterialApp(home: ImportTaskScreen(taskId: _taskId)),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('add-photos')));
    await tester.pump();

    expect(
      tester
          .widget<FilledButton>(find.byKey(const Key('add-photos')))
          .onPressed,
      isNull,
    );
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const Key('finish-selection')))
          .onPressed,
      isNull,
    );
    bridge.complete(const MediaPickResult(selected: [], unsupported: []));
    await tester.pumpAndSettle();
  });
}

LocalAssetRef _asset(int index) => LocalAssetRef(
  id: 'asset-$index',
  displayName: 'IMG_$index.JPG',
  mediaType: 'image/jpeg',
  byteSize: 42,
  accessKind: AssetAccessKind.stagedFile,
  locator:
      '00000000-0000-4000-8000-${index.toString().padLeft(12, '0')}/original',
);

final class _FakeMediaLibraryBridge implements MediaLibraryBridge {
  _FakeMediaLibraryBridge(this._results);

  final List<MediaPickResult> _results;
  final List<int> requestedLimits = <int>[];
  final List<LocalAssetRef> released = <LocalAssetRef>[];
  final List<Future<void>> flushFutures = <Future<void>>[];
  Object? releaseError;
  Object? flushError;
  var flushCalls = 0;

  @override
  Future<String> computeSha256(LocalAssetRef ref) => throw UnimplementedError();

  @override
  Future<void> flushPendingCleanup() async {
    flushCalls += 1;
    if (flushFutures.isNotEmpty) await flushFutures.removeAt(0);
    if (flushError case final error?) throw error;
  }

  @override
  Future<MediaPickResult> pickImages({required int remainingSlots}) async {
    requestedLimits.add(remainingSlots);
    return _results.removeAt(0);
  }

  @override
  Future<void> release(LocalAssetRef ref) async {
    released.add(ref);
    if (releaseError case final error?) throw error;
  }
}

final class _DelayedMediaLibraryBridge implements MediaLibraryBridge {
  final Completer<MediaPickResult> _pick = Completer<MediaPickResult>();
  final Completer<void> called = Completer<void>();
  final List<LocalAssetRef> released = <LocalAssetRef>[];
  final List<Future<void>> flushFutures = <Future<void>>[];
  var flushCalls = 0;

  void complete(MediaPickResult result) => _pick.complete(result);

  void completeError(Object error) => _pick.completeError(error);

  @override
  Future<String> computeSha256(LocalAssetRef ref) => throw UnimplementedError();

  @override
  Future<void> flushPendingCleanup() async {
    flushCalls += 1;
    if (flushFutures.isNotEmpty) await flushFutures.removeAt(0);
  }

  @override
  Future<MediaPickResult> pickImages({required int remainingSlots}) {
    called.complete();
    return _pick.future;
  }

  @override
  Future<void> release(LocalAssetRef ref) async {
    released.add(ref);
  }
}

final class _FakeUnsupportedReporter implements UnsupportedAssetReporter {
  _FakeUnsupportedReporter({
    this.disposition = ServerAssetDisposition.unsupported,
  });

  final ServerAssetDisposition disposition;
  final List<({String taskId, UnsupportedLocalAsset asset})> reported =
      <({String taskId, UnsupportedLocalAsset asset})>[];
  Object? reportError;

  @override
  Future<UnsupportedReportReceipt> reportUnsupported(
    String taskId,
    UnsupportedLocalAsset asset,
  ) async {
    reported.add((taskId: taskId, asset: asset));
    if (reportError case final error?) throw error;
    return UnsupportedReportReceipt(
      localAssetId: asset.id,
      disposition: disposition,
    );
  }
}

final class _FakeTaskSummarySource implements ImportTaskSummarySource {
  const _FakeTaskSummarySource({
    this.totalAssets = 0,
    this.unsupportedAssets = 0,
  });

  final int totalAssets;
  final int unsupportedAssets;

  @override
  Future<TaskSummary> get(String taskId) async => TaskSummary(
    id: taskId,
    name: 'Import',
    status: 'waiting_upload',
    totalAssets: totalAssets,
    uploadedAssets: 0,
    failedAssets: 0,
    unsupportedAssets: unsupportedAssets,
    createdAt: DateTime.utc(2026, 8, 15),
    updatedAt: DateTime.utc(2026, 8, 15),
  );
}

final class _SequencedTaskSummarySource implements ImportTaskSummarySource {
  _SequencedTaskSummarySource(this._items);

  final List<TaskSummary> _items;
  var calls = 0;

  @override
  Future<TaskSummary> get(String taskId) async {
    final index = calls < _items.length ? calls : _items.length - 1;
    calls += 1;
    return _items[index];
  }
}

final class _FailOnceRefreshTaskSummarySource
    implements ImportTaskSummarySource {
  var calls = 0;

  @override
  Future<TaskSummary> get(String taskId) async {
    calls += 1;
    if (calls == 2) throw StateError('summary temporarily unavailable');
    return _taskSummary(unsupportedAssets: calls == 1 ? 2 : 3);
  }
}

TaskSummary _taskSummary({
  int totalAssets = 0,
  int uploadedAssets = 0,
  int failedAssets = 0,
  int unsupportedAssets = 0,
}) => TaskSummary(
  id: _taskId,
  name: 'Import',
  status: 'waiting_upload',
  totalAssets: totalAssets,
  uploadedAssets: uploadedAssets,
  failedAssets: failedAssets,
  unsupportedAssets: unsupportedAssets,
  createdAt: DateTime.utc(2026, 8, 15),
  updatedAt: DateTime.utc(2026, 8, 15),
);

final class _FakeImportQueue implements ImportQueueBoundary {
  _FakeImportQueue({
    ImportQueueSnapshot initialSnapshot = const ImportQueueSnapshot(
      unacknowledgedAssetIds: <String>{},
      queued: 0,
      uploaded: 0,
      failed: 0,
    ),
  }) : _snapshot = initialSnapshot;

  final List<LocalAssetRef> accepted = <LocalAssetRef>[];
  ImportQueueSnapshot _snapshot;
  var sealWhenIdleCalls = 0;
  var sealRequestedWithPendingCount = -1;

  void replaceSnapshot(ImportQueueSnapshot snapshot) {
    _snapshot = snapshot;
  }

  @override
  Future<ImportQueueSnapshot> enqueue(
    String taskId,
    List<LocalAssetRef> assets, {
    UploadAuthority? authority,
  }) async {
    accepted.addAll(assets);
    _snapshot = ImportQueueSnapshot(
      unacknowledgedAssetIds: <String>{
        ..._snapshot.unacknowledgedAssetIds,
        ...assets.map((asset) => asset.id),
      },
      serverAcceptedAssetIds: _snapshot.serverAcceptedAssetIds,
      serverUnsupportedAssetIds: _snapshot.serverUnsupportedAssetIds,
      queued: _snapshot.queued + assets.length,
      uploaded: _snapshot.uploaded,
      failed: _snapshot.failed,
      cleanupRefs: _snapshot.cleanupRefs,
      sealWhenIdle: _snapshot.sealWhenIdle,
    );
    return _snapshot;
  }

  @override
  Future<ImportQueueSnapshot> snapshot(String taskId) async => _snapshot;

  @override
  Future<ImportQueueSnapshot> retainCleanupRefs(
    String taskId,
    List<LocalAssetRef> assets,
  ) async {
    final byLocator = <String, LocalAssetRef>{
      for (final asset in _snapshot.cleanupRefs) asset.locator: asset,
      for (final asset in assets) asset.locator: asset,
    };
    _snapshot = _copySnapshot(
      _snapshot,
      cleanupRefs: List<LocalAssetRef>.unmodifiable(byLocator.values),
    );
    return _snapshot;
  }

  @override
  Future<ImportQueueSnapshot> completeCleanupRef(
    String taskId,
    LocalAssetRef asset,
  ) async {
    _snapshot = _copySnapshot(
      _snapshot,
      cleanupRefs: <LocalAssetRef>[
        for (final pending in _snapshot.cleanupRefs)
          if (pending.locator != asset.locator) pending,
      ],
    );
    return _snapshot;
  }

  @override
  Future<ImportQueueSnapshot> recordServerDisposition(
    String taskId,
    UnsupportedReportReceipt receipt,
  ) async {
    final accepted = <String>{..._snapshot.serverAcceptedAssetIds};
    final unsupported = <String>{..._snapshot.serverUnsupportedAssetIds};
    switch (receipt.disposition) {
      case ServerAssetDisposition.accepted:
        unsupported.remove(receipt.localAssetId);
        accepted.add(receipt.localAssetId);
      case ServerAssetDisposition.unsupported:
        accepted.remove(receipt.localAssetId);
        unsupported.add(receipt.localAssetId);
    }
    _snapshot = ImportQueueSnapshot(
      unacknowledgedAssetIds: _snapshot.unacknowledgedAssetIds,
      serverAcceptedAssetIds: Set<String>.unmodifiable(accepted),
      serverUnsupportedAssetIds: Set<String>.unmodifiable(unsupported),
      queued: _snapshot.queued,
      uploaded: _snapshot.uploaded,
      failed: _snapshot.failed,
      cleanupRefs: _snapshot.cleanupRefs,
      sealWhenIdle: _snapshot.sealWhenIdle,
    );
    return _snapshot;
  }

  @override
  Future<void> setSealWhenIdle(String taskId) async {
    sealWhenIdleCalls += 1;
    sealRequestedWithPendingCount = accepted.length;
    _snapshot = ImportQueueSnapshot(
      unacknowledgedAssetIds: _snapshot.unacknowledgedAssetIds,
      serverAcceptedAssetIds: _snapshot.serverAcceptedAssetIds,
      serverUnsupportedAssetIds: _snapshot.serverUnsupportedAssetIds,
      queued: _snapshot.queued,
      uploaded: _snapshot.uploaded,
      failed: _snapshot.failed,
      cleanupRefs: _snapshot.cleanupRefs,
      sealWhenIdle: true,
    );
  }
}

final class _FailOnceImportQueue implements ImportQueueBoundary {
  var _failed = false;
  ImportQueueSnapshot _snapshot = const ImportQueueSnapshot(
    unacknowledgedAssetIds: <String>{},
    queued: 0,
    uploaded: 0,
    failed: 0,
  );

  @override
  Future<ImportQueueSnapshot> enqueue(
    String taskId,
    List<LocalAssetRef> assets, {
    UploadAuthority? authority,
  }) async {
    if (!_failed) {
      _failed = true;
      throw StateError('handoff unavailable');
    }
    _snapshot = ImportQueueSnapshot(
      unacknowledgedAssetIds: assets.map((asset) => asset.id).toSet(),
      serverAcceptedAssetIds: _snapshot.serverAcceptedAssetIds,
      serverUnsupportedAssetIds: _snapshot.serverUnsupportedAssetIds,
      queued: assets.length,
      uploaded: 0,
      failed: 0,
      cleanupRefs: _snapshot.cleanupRefs,
    );
    return _snapshot;
  }

  @override
  Future<ImportQueueSnapshot> snapshot(String taskId) async => _snapshot;

  @override
  Future<ImportQueueSnapshot> retainCleanupRefs(
    String taskId,
    List<LocalAssetRef> assets,
  ) async {
    _snapshot = _copySnapshot(_snapshot, cleanupRefs: assets);
    return _snapshot;
  }

  @override
  Future<ImportQueueSnapshot> completeCleanupRef(
    String taskId,
    LocalAssetRef asset,
  ) async {
    _snapshot = _copySnapshot(_snapshot, cleanupRefs: const <LocalAssetRef>[]);
    return _snapshot;
  }

  @override
  Future<ImportQueueSnapshot> recordServerDisposition(
    String taskId,
    UnsupportedReportReceipt receipt,
  ) async => _snapshot;

  @override
  Future<void> setSealWhenIdle(String taskId) async {}
}

final class _AlwaysFailImportQueue implements ImportQueueBoundary {
  var sealWhenIdleCalls = 0;
  ImportQueueSnapshot _snapshot = const ImportQueueSnapshot(
    unacknowledgedAssetIds: <String>{},
    queued: 0,
    uploaded: 0,
    failed: 0,
  );

  @override
  Future<ImportQueueSnapshot> enqueue(
    String taskId,
    List<LocalAssetRef> assets, {
    UploadAuthority? authority,
  }) => Future<ImportQueueSnapshot>.error(StateError('handoff unavailable'));

  @override
  Future<void> setSealWhenIdle(String taskId) async {
    sealWhenIdleCalls += 1;
    _snapshot = _copySnapshot(_snapshot, sealWhenIdle: true);
  }

  @override
  Future<ImportQueueSnapshot> snapshot(String taskId) async => _snapshot;

  @override
  Future<ImportQueueSnapshot> retainCleanupRefs(
    String taskId,
    List<LocalAssetRef> assets,
  ) async {
    final byLocator = <String, LocalAssetRef>{
      for (final asset in _snapshot.cleanupRefs) asset.locator: asset,
      for (final asset in assets) asset.locator: asset,
    };
    _snapshot = _copySnapshot(
      _snapshot,
      cleanupRefs: List<LocalAssetRef>.unmodifiable(byLocator.values),
    );
    return _snapshot;
  }

  @override
  Future<ImportQueueSnapshot> completeCleanupRef(
    String taskId,
    LocalAssetRef asset,
  ) async {
    _snapshot = _copySnapshot(
      _snapshot,
      cleanupRefs: <LocalAssetRef>[
        for (final pending in _snapshot.cleanupRefs)
          if (pending.locator != asset.locator) pending,
      ],
    );
    return _snapshot;
  }

  @override
  Future<ImportQueueSnapshot> recordServerDisposition(
    String taskId,
    UnsupportedReportReceipt receipt,
  ) async => _snapshot;
}

ImportQueueSnapshot _copySnapshot(
  ImportQueueSnapshot snapshot, {
  List<LocalAssetRef>? cleanupRefs,
  bool? sealWhenIdle,
}) => ImportQueueSnapshot(
  unacknowledgedAssetIds: snapshot.unacknowledgedAssetIds,
  serverAcceptedAssetIds: snapshot.serverAcceptedAssetIds,
  serverUnsupportedAssetIds: snapshot.serverUnsupportedAssetIds,
  queued: snapshot.queued,
  uploaded: snapshot.uploaded,
  failed: snapshot.failed,
  cleanupRefs: cleanupRefs ?? snapshot.cleanupRefs,
  sealWhenIdle: sealWhenIdle ?? snapshot.sealWhenIdle,
);
