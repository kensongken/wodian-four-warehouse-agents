import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api/models.dart';
import '../../core/ingestion/ingestion_limits.dart';
import '../../core/media/local_asset_ref.dart';
import '../../core/media/media_library_bridge.dart';
import '../../core/media/method_channel_media_library.dart';
import '../../core/upload/upload_queue_repository.dart';
import 'task_repository.dart';

export '../../core/upload/upload_queue_repository.dart'
    show DeferredImportQueueBoundary, ImportQueueBoundary, ImportQueueSnapshot;

final mediaLibraryBridgeProvider = Provider<MediaLibraryBridge>(
  (_) => MethodChannelMediaLibrary(),
);

final mediaCleanupCoordinatorProvider = Provider<MediaCleanupCoordinator>(
  (ref) => MediaCleanupCoordinator(ref.watch(mediaLibraryBridgeProvider)),
);

final unsupportedAssetReporterProvider = Provider<UnsupportedAssetReporter>(
  (ref) => ref.watch(taskRepositoryProvider),
);

final importTaskSummarySourceProvider = Provider<ImportTaskSummarySource>(
  (ref) => ref.watch(taskRepositoryProvider),
);

final importQueueBoundaryProvider =
    Provider.family<ImportQueueBoundary, String>(
      (ref, _) => ref.watch(uploadQueueRepositoryProvider),
    );

final importTaskControllerProvider = StateNotifierProvider.autoDispose
    .family<ImportTaskController, ImportTaskState, String>((ref, taskId) {
      return ImportTaskController(
        taskId: taskId,
        mediaLibrary: ref.watch(mediaLibraryBridgeProvider),
        mediaCleanupCoordinator: ref.watch(mediaCleanupCoordinatorProvider),
        taskSummarySource: ref.watch(importTaskSummarySourceProvider),
        unsupportedReporter: ref.watch(unsupportedAssetReporterProvider),
        queue: ref.watch(importQueueBoundaryProvider(taskId)),
      );
    });

final class MediaCleanupCoordinator {
  MediaCleanupCoordinator(this._mediaLibrary);

  final MediaLibraryBridge _mediaLibrary;
  Future<void>? _inFlight;
  Future<void>? _controllerLifecycleFlush;
  final Set<Future<void>> _ownershipProducers = <Future<void>>{};

  Future<MediaPickResult> pickImages({required int remainingSlots}) {
    final operation = Future<MediaPickResult>.sync(
      () => _mediaLibrary.pickImages(remainingSlots: remainingSlots),
    );
    late final Future<void> settled;
    settled = operation
        .then<void>((_) {}, onError: (Object _, StackTrace _) {})
        .whenComplete(() => _ownershipProducers.remove(settled));
    _ownershipProducers.add(settled);
    _extendLifecycleForOwnershipProducer(settled);
    return operation;
  }

  Future<void> flushPendingCleanup() {
    final current = _inFlight;
    if (current != null) return current;

    late final Future<void> tracked;
    tracked = Future<void>.sync(_mediaLibrary.flushPendingCleanup)
        .whenComplete(() {
          if (identical(_inFlight, tracked)) _inFlight = null;
        });
    _inFlight = tracked;
    return tracked;
  }

  void flushForControllerDisposal() {
    final previousLifecycle = _controllerLifecycleFlush;
    final prerequisites = <Future<void>>[
      if (previousLifecycle != null) _ignoreFailure(previousLifecycle),
      ..._ownershipProducers,
    ];
    final attempt = prerequisites.isEmpty
        ? flushPendingCleanup()
        : Future.wait<void>(prerequisites)
              .then<void>((_) => flushPendingCleanup());
    _controllerLifecycleFlush = attempt;
    unawaited(attempt.catchError((Object _) {}));
  }

  Future<void> recoverForControllerInitialization() async {
    while (true) {
      var inherited = _controllerLifecycleFlush;
      if (inherited == null) return;
      try {
        await inherited;
      } catch (_) {
        if (!identical(_controllerLifecycleFlush, inherited)) continue;
        final retry = flushPendingCleanup();
        _controllerLifecycleFlush = retry;
        unawaited(retry.catchError((Object _) {}));
        inherited = retry;
        try {
          await retry;
        } catch (_) {
          if (!identical(_controllerLifecycleFlush, retry)) continue;
          rethrow;
        }
      }

      if (identical(_controllerLifecycleFlush, inherited)) {
        _controllerLifecycleFlush = null;
        return;
      }
    }
  }

  static Future<void> _ignoreFailure(Future<void> future) async {
    try {
      await future;
    } catch (_) {
      // The next serialized flush retries bridge-owned cleanup.
    }
  }

  void _extendLifecycleForOwnershipProducer(Future<void> producer) {
    final previousLifecycle = _controllerLifecycleFlush;
    if (previousLifecycle == null) return;
    final extended = Future.wait<void>(<Future<void>>[
      _ignoreFailure(previousLifecycle),
      producer,
    ]).then<void>((_) => flushPendingCleanup());
    _controllerLifecycleFlush = extended;
    unawaited(extended.catchError((Object _) {}));
  }
}

final class ImportTaskState {
  const ImportTaskState({
    this.serverAcceptedCount = 0,
    this.localPendingAssetIds = const <String>{},
    this.serverUnsupportedCount = 0,
    this.serverUploadedCount = 0,
    this.localUploadedCount = 0,
    this.serverFailedCount = 0,
    this.localFailedCount = 0,
    this.queuedCount = 0,
    this.unsupported = const <UnsupportedLocalAsset>[],
    this.unreportedUnsupportedAssetIds = const <String>{},
    this.initializing = true,
    this.selecting = false,
    this.finishing = false,
    this.sealWhenIdle = false,
    this.errorMessage,
  });

  final int serverAcceptedCount;
  final Set<String> localPendingAssetIds;
  final int serverUnsupportedCount;
  final int serverUploadedCount;
  final int localUploadedCount;
  final int serverFailedCount;
  final int localFailedCount;
  final int queuedCount;
  final List<UnsupportedLocalAsset> unsupported;
  final Set<String> unreportedUnsupportedAssetIds;
  final bool initializing;
  final bool selecting;
  final bool finishing;
  final bool sealWhenIdle;
  final String? errorMessage;

  int get localPendingAcceptedCount => localPendingAssetIds.length;
  int get acceptedCount => serverAcceptedCount + localPendingAcceptedCount;
  int get unsupportedCount =>
      serverUnsupportedCount + unreportedUnsupportedAssetIds.length;
  int get uploadedCount => serverUploadedCount + localUploadedCount;
  int get failedCount => serverFailedCount + localFailedCount;
  int get remainingSlots => maximumAcceptedAssets - acceptedCount;
  bool get atCapacity => remainingSlots <= 0;
  bool get canAdd =>
      !initializing && !selecting && !finishing && !sealWhenIdle && !atCapacity;
  bool get canFinish =>
      !initializing && !selecting && !finishing && !sealWhenIdle;

  ImportTaskState copyWith({
    int? serverAcceptedCount,
    Set<String>? localPendingAssetIds,
    int? serverUnsupportedCount,
    int? serverUploadedCount,
    int? localUploadedCount,
    int? serverFailedCount,
    int? localFailedCount,
    int? queuedCount,
    List<UnsupportedLocalAsset>? unsupported,
    Set<String>? unreportedUnsupportedAssetIds,
    bool? initializing,
    bool? selecting,
    bool? finishing,
    bool? sealWhenIdle,
    String? errorMessage,
    bool clearError = false,
  }) => ImportTaskState(
    serverAcceptedCount: serverAcceptedCount ?? this.serverAcceptedCount,
    localPendingAssetIds: localPendingAssetIds ?? this.localPendingAssetIds,
    serverUnsupportedCount:
        serverUnsupportedCount ?? this.serverUnsupportedCount,
    serverUploadedCount: serverUploadedCount ?? this.serverUploadedCount,
    localUploadedCount: localUploadedCount ?? this.localUploadedCount,
    serverFailedCount: serverFailedCount ?? this.serverFailedCount,
    localFailedCount: localFailedCount ?? this.localFailedCount,
    queuedCount: queuedCount ?? this.queuedCount,
    unsupported: unsupported ?? this.unsupported,
    unreportedUnsupportedAssetIds:
        unreportedUnsupportedAssetIds ?? this.unreportedUnsupportedAssetIds,
    initializing: initializing ?? this.initializing,
    selecting: selecting ?? this.selecting,
    finishing: finishing ?? this.finishing,
    sealWhenIdle: sealWhenIdle ?? this.sealWhenIdle,
    errorMessage: clearError ? null : errorMessage ?? this.errorMessage,
  );
}

final class ImportTaskController extends StateNotifier<ImportTaskState> {
  factory ImportTaskController({
    required String taskId,
    required MediaLibraryBridge mediaLibrary,
    MediaCleanupCoordinator? mediaCleanupCoordinator,
    ImportTaskSummarySource taskSummarySource = const _EmptyTaskSummarySource(),
    required UnsupportedAssetReporter unsupportedReporter,
    required ImportQueueBoundary queue,
  }) {
    final controller = ImportTaskController._(
      taskId,
      mediaLibrary,
      mediaCleanupCoordinator ?? MediaCleanupCoordinator(mediaLibrary),
      taskSummarySource,
      unsupportedReporter,
      queue,
    );
    unawaited(controller._ensureInitialized());
    return controller;
  }

  ImportTaskController._(
    this.taskId,
    this._mediaLibrary,
    this._mediaCleanupCoordinator,
    this._taskSummarySource,
    this._unsupportedReporter,
    this._queue,
  ) : super(const ImportTaskState());

  final String taskId;
  final MediaLibraryBridge _mediaLibrary;
  final MediaCleanupCoordinator _mediaCleanupCoordinator;
  final ImportTaskSummarySource _taskSummarySource;
  final UnsupportedAssetReporter _unsupportedReporter;
  final ImportQueueBoundary _queue;
  final Set<String> _sessionAcceptedIds = <String>{};
  final Set<String> _serverAcceptedIds = <String>{};
  final Set<String> _serverUnsupportedIds = <String>{};
  final Set<String> _reportedUnsupportedIds = <String>{};
  final Map<String, LocalAssetRef> _cleanupBacklog = <String, LocalAssetRef>{};
  bool _active = true;
  bool _initialized = false;
  int _serverAcceptedCount = 0;
  bool _needsSummaryRebaseline = false;
  Future<void>? _initialization;

  Future<void> get ready => _ensureInitialized();

  Future<void> _ensureInitialized() {
    if (_initialized) return Future<void>.value();
    return _initialization ??= _initialize().whenComplete(() {
      _initialization = null;
    });
  }

  Future<void> _initialize() async {
    if (_active) state = state.copyWith(initializing: true, clearError: true);
    try {
      await _mediaCleanupCoordinator.recoverForControllerInitialization();
      final summary = await _taskSummarySource.get(taskId);
      final snapshot = await _queue.snapshot(taskId);
      _validateInitialCounts(summary, snapshot);
      if (!_active) return;
      _adoptSnapshotAuthority(snapshot);
      _serverAcceptedCount = summary.totalAssets;
      _replaceCleanupBacklog(snapshot.cleanupRefs);
      _initialized = true;
      state = state.copyWith(
        serverAcceptedCount: summary.totalAssets,
        serverUnsupportedCount: summary.unsupportedAssets,
        serverUploadedCount: summary.uploadedAssets,
        serverFailedCount: summary.failedAssets,
        localPendingAssetIds: Set<String>.unmodifiable(
          snapshot.unacknowledgedAssetIds,
        ),
        queuedCount: snapshot.queued,
        localUploadedCount: snapshot.uploaded,
        localFailedCount: snapshot.failed,
        sealWhenIdle: snapshot.sealWhenIdle,
        initializing: false,
      );
    } catch (_) {
      if (_active) {
        state = state.copyWith(
          initializing: false,
          errorMessage: '无法加载导入状态，请稍后重试',
        );
      }
    }
  }

  Future<void> addPhotos() async {
    if (state.selecting || state.finishing || state.sealWhenIdle) return;
    await _ensureInitialized();
    if (!_active || !_initialized || !state.canAdd) return;
    state = state.copyWith(selecting: true, clearError: true);
    try {
      if (!await _retryCleanupBacklog()) {
        if (_active) {
          state = state.copyWith(
            selecting: false,
            errorMessage: '部分照片未能安全释放，请稍后重试',
          );
        }
        return;
      }
      if (!_active) return;
      final result = await _mediaCleanupCoordinator.pickImages(
        remainingSlots: state.remainingSlots,
      );
      if (!_active) {
        await _releaseAssets(result.selected);
        return;
      }

      final ImportQueueSnapshot authority;
      try {
        authority = await _queue.snapshot(taskId);
        _validateQueueSnapshot(authority, state.serverAcceptedCount);
        _adoptSnapshotAuthority(authority);
      } catch (_) {
        final released = await _releaseAssets(result.selected);
        if (_active) {
          state = state.copyWith(
            selecting: false,
            errorMessage: released ? '无法验证导入状态，请重试' : '无法验证导入状态，且部分照片未能安全释放',
          );
        }
        return;
      }
      if (!_active) {
        await _releaseAssets(result.selected);
        return;
      }
      state = state.copyWith(
        localPendingAssetIds: Set<String>.unmodifiable(
          authority.unacknowledgedAssetIds,
        ),
        queuedCount: authority.queued,
        localUploadedCount: authority.uploaded,
        localFailedCount: authority.failed,
      );
      final selectedIds = result.selected.map((asset) => asset.id).toSet();
      final unsupportedIds = result.unsupported
          .map((asset) => asset.id)
          .toSet();
      final acceptedAuthority = <String>{
        ...authority.unacknowledgedAssetIds,
        ...authority.serverAcceptedAssetIds,
      };
      final hasAuthorityConflict = unsupportedIds.any(
        acceptedAuthority.contains,
      );
      final hasBatchConflict = selectedIds.any(unsupportedIds.contains);
      if (hasAuthorityConflict || hasBatchConflict) {
        final released = await _releaseAssets(result.selected);
        if (_active) {
          state = state.copyWith(
            selecting: false,
            errorMessage: released ? '照片选择结果无效，请重新选择' : '照片选择结果无效，且部分照片未能安全释放',
          );
        }
        return;
      }

      final unsupportedById = <String, UnsupportedLocalAsset>{
        for (final asset in state.unsupported) asset.id: asset,
        for (final asset in result.unsupported) asset.id: asset,
      };
      final unreportedUnsupportedIds = <String>{
        ...state.unreportedUnsupportedAssetIds,
        for (final asset in result.unsupported)
          if (!_serverAcceptedIds.contains(asset.id) &&
              !_serverUnsupportedIds.contains(asset.id) &&
              !_reportedUnsupportedIds.contains(asset.id))
            asset.id,
      };
      state = state.copyWith(
        unsupported: List<UnsupportedLocalAsset>.unmodifiable(
          unsupportedById.values,
        ),
        unreportedUnsupportedAssetIds: Set<String>.unmodifiable(
          unreportedUnsupportedIds,
        ),
      );
      await _reportUnsupportedItems();
      if (!_active) {
        await _releaseAssets(result.selected);
        return;
      }

      final newlyAccepted = <LocalAssetRef>[];
      final duplicateAcquisitions = <LocalAssetRef>[];
      for (final asset in result.selected) {
        if (_sessionAcceptedIds.contains(asset.id)) {
          duplicateAcquisitions.add(asset);
        } else {
          newlyAccepted.add(asset);
        }
      }
      if (state.acceptedCount + newlyAccepted.length > maximumAcceptedAssets) {
        await _releaseAssets(result.selected);
        throw const MediaLibraryException(
          code: 'invalid_native_response',
          userMessage: '照片选择结果超出任务上限，请重新选择',
        );
      }
      final duplicatesReleased = await _releaseAssets(duplicateAcquisitions);
      final duplicateReleaseError = duplicatesReleased ? null : '部分重复照片未能安全释放';
      var snapshot = ImportQueueSnapshot(
        unacknowledgedAssetIds: state.localPendingAssetIds,
        queued: state.queuedCount,
        uploaded: state.localUploadedCount,
        failed: state.localFailedCount,
        sealWhenIdle: state.sealWhenIdle,
      );
      if (newlyAccepted.isNotEmpty) {
        try {
          snapshot = await _queue.enqueue(
            taskId,
            List<LocalAssetRef>.unmodifiable(newlyAccepted),
          );
        } catch (_) {
          final released = await _releaseAssets(newlyAccepted);
          if (_active) {
            state = state.copyWith(
              selecting: false,
              errorMessage: released ? '无法添加照片，请重新选择' : '无法添加照片，且部分照片未能安全释放',
            );
          }
          return;
        }
        _validateQueueSnapshot(snapshot, state.serverAcceptedCount);
        _adoptSnapshotAuthority(snapshot);
      }
      if (!_active) return;
      state = state.copyWith(
        localPendingAssetIds: Set<String>.unmodifiable(
          snapshot.unacknowledgedAssetIds,
        ),
        queuedCount: snapshot.queued,
        localUploadedCount: snapshot.uploaded,
        localFailedCount: snapshot.failed,
        selecting: false,
        errorMessage: duplicateReleaseError,
      );
    } on MediaLibraryException catch (error) {
      if (_active) {
        state = state.copyWith(
          selecting: false,
          errorMessage: error.userMessage,
        );
      }
    } catch (_) {
      if (_active) {
        state = state.copyWith(selecting: false, errorMessage: '无法添加照片，请稍后重试');
      }
    }
  }

  Future<void> _reportUnsupportedItems() async {
    var hadFailure = false;
    for (final asset in state.unsupported) {
      if (!_active) return;
      if (_reportedUnsupportedIds.contains(asset.id)) continue;
      try {
        final authority = await _queue.snapshot(taskId);
        _validateQueueSnapshot(authority, state.serverAcceptedCount);
        _adoptSnapshotAuthority(authority);
        if (!_active) return;
        state = state.copyWith(
          localPendingAssetIds: Set<String>.unmodifiable(
            authority.unacknowledgedAssetIds,
          ),
          queuedCount: authority.queued,
          localUploadedCount: authority.uploaded,
          localFailedCount: authority.failed,
        );
        final alreadyAccounted =
            authority.unacknowledgedAssetIds.contains(asset.id) ||
            authority.serverAcceptedAssetIds.contains(asset.id) ||
            authority.serverUnsupportedAssetIds.contains(asset.id);
        if (alreadyAccounted) {
          final pending = <String>{...state.unreportedUnsupportedAssetIds}
            ..remove(asset.id);
          state = state.copyWith(
            unreportedUnsupportedAssetIds: Set<String>.unmodifiable(pending),
          );
          continue;
        }
        final receipt = await _unsupportedReporter.reportUnsupported(
          taskId,
          asset,
        );
        if (receipt.localAssetId != asset.id) {
          throw const FormatException('unsupported report ID mismatch');
        }
        final snapshot = await _queue.recordServerDisposition(taskId, receipt);
        _validateQueueSnapshot(snapshot, state.serverAcceptedCount);
        _adoptSnapshotAuthority(snapshot);
        _reportedUnsupportedIds.add(asset.id);
        final pending = <String>{...state.unreportedUnsupportedAssetIds}
          ..remove(asset.id);
        if (_active) {
          state = state.copyWith(
            unreportedUnsupportedAssetIds: Set<String>.unmodifiable(pending),
          );
        }
        _needsSummaryRebaseline = true;
      } catch (_) {
        hadFailure = true;
      }
    }
    if (_needsSummaryRebaseline && _active) {
      try {
        final summary = await _taskSummarySource.get(taskId);
        final snapshot = await _queue.snapshot(taskId);
        _validateInitialCounts(summary, snapshot);
        _adoptSnapshotAuthority(snapshot);
        _serverAcceptedCount = summary.totalAssets;
        _needsSummaryRebaseline = false;
        state = state.copyWith(
          serverAcceptedCount: summary.totalAssets,
          serverUnsupportedCount: summary.unsupportedAssets,
          serverUploadedCount: summary.uploadedAssets,
          serverFailedCount: summary.failedAssets,
          localPendingAssetIds: Set<String>.unmodifiable(
            snapshot.unacknowledgedAssetIds,
          ),
          queuedCount: snapshot.queued,
          localUploadedCount: snapshot.uploaded,
          localFailedCount: snapshot.failed,
        );
      } catch (_) {
        hadFailure = true;
      }
    }
    if (hadFailure && _active) {
      state = state.copyWith(errorMessage: '部分不支持的文件尚未上报，将在下次添加时重试');
    }
  }

  Future<void> finishSelection() async {
    if (state.selecting || state.finishing || state.sealWhenIdle) return;
    await _ensureInitialized();
    if (!_active || !_initialized || !state.canFinish) return;
    state = state.copyWith(finishing: true, clearError: true);
    try {
      await _mediaCleanupCoordinator.recoverForControllerInitialization();
      await _mediaCleanupCoordinator.flushPendingCleanup();
      if (!await _retryCleanupBacklog()) {
        if (_active) {
          state = state.copyWith(
            finishing: false,
            errorMessage: '部分照片未能安全释放，请稍后重试',
          );
        }
        return;
      }
      await _reportUnsupportedItems();
      if (_needsSummaryRebaseline ||
          state.unreportedUnsupportedAssetIds.isNotEmpty) {
        throw const _PendingUnsupportedReports();
      }
      await _queue.setSealWhenIdle(taskId);
      if (_active) {
        state = state.copyWith(finishing: false, sealWhenIdle: true);
      }
    } catch (_) {
      if (_active) {
        state = state.copyWith(
          finishing: false,
          sealWhenIdle: false,
          errorMessage: '无法完成选图，请稍后重试',
        );
      }
    }
  }

  Future<bool> _releaseAssets(Iterable<LocalAssetRef> assets) async {
    final pending = <String, LocalAssetRef>{
      for (final asset in assets) asset.locator: asset,
    };
    if (pending.isEmpty) return true;
    try {
      final snapshot = await _queue.retainCleanupRefs(
        taskId,
        List<LocalAssetRef>.unmodifiable(pending.values),
      );
      _validateQueueSnapshot(snapshot, _serverAcceptedCount);
      _replaceCleanupBacklog(snapshot.cleanupRefs);
    } catch (_) {
      _cleanupBacklog.addAll(pending);
      return false;
    }
    var released = true;
    for (final asset in pending.values) {
      try {
        await _mediaLibrary.release(asset);
        final snapshot = await _queue.completeCleanupRef(taskId, asset);
        _validateQueueSnapshot(snapshot, _serverAcceptedCount);
        _replaceCleanupBacklog(snapshot.cleanupRefs);
      } catch (_) {
        _cleanupBacklog[asset.locator] = asset;
        released = false;
      }
    }
    return released;
  }

  Future<bool> _retryCleanupBacklog() =>
      _releaseAssets(List<LocalAssetRef>.of(_cleanupBacklog.values));

  void _replaceCleanupBacklog(Iterable<LocalAssetRef> refs) {
    _cleanupBacklog
      ..clear()
      ..addEntries(refs.map((ref) => MapEntry(ref.locator, ref)));
  }

  void _adoptSnapshotAuthority(ImportQueueSnapshot snapshot) {
    _serverAcceptedIds
      ..clear()
      ..addAll(snapshot.serverAcceptedAssetIds);
    _serverUnsupportedIds
      ..clear()
      ..addAll(snapshot.serverUnsupportedAssetIds);
    _sessionAcceptedIds
      ..clear()
      ..addAll(snapshot.unacknowledgedAssetIds)
      ..addAll(snapshot.serverAcceptedAssetIds)
      ..addAll(snapshot.serverUnsupportedAssetIds);
  }

  static void _validateInitialCounts(
    TaskSummary summary,
    ImportQueueSnapshot snapshot,
  ) {
    if (summary.totalAssets < 0 ||
        summary.totalAssets > maximumAcceptedAssets ||
        summary.uploadedAssets < 0 ||
        summary.failedAssets < 0 ||
        summary.unsupportedAssets < 0) {
      throw const FormatException('invalid task summary counts');
    }
    _validateQueueSnapshot(snapshot, summary.totalAssets);
    if (snapshot.serverAcceptedAssetIds.length > summary.totalAssets ||
        snapshot.serverUnsupportedAssetIds.length > summary.unsupportedAssets) {
      throw const FormatException('server identity ledger exceeds summary');
    }
  }

  static void _validateQueueSnapshot(
    ImportQueueSnapshot snapshot,
    int serverAcceptedCount,
  ) {
    final allKnownIds = <String>{
      ...snapshot.unacknowledgedAssetIds,
      ...snapshot.serverAcceptedAssetIds,
      ...snapshot.serverUnsupportedAssetIds,
    };
    final expectedKnownCount =
        snapshot.unacknowledgedAssetIds.length +
        snapshot.serverAcceptedAssetIds.length +
        snapshot.serverUnsupportedAssetIds.length;
    if (snapshot.queued < 0 ||
        snapshot.uploaded < 0 ||
        snapshot.failed < 0 ||
        serverAcceptedCount + snapshot.unacknowledgedAssetIds.length >
            maximumAcceptedAssets ||
        allKnownIds.length != expectedKnownCount ||
        allKnownIds.any((id) => id.isEmpty) ||
        snapshot.cleanupRefs.any(
          (ref) => ref.id.isEmpty || ref.locator.isEmpty,
        )) {
      throw const FormatException('invalid import queue snapshot');
    }
  }

  @override
  void dispose() {
    _active = false;
    _mediaCleanupCoordinator.flushForControllerDisposal();
    super.dispose();
  }
}

final class _EmptyTaskSummarySource implements ImportTaskSummarySource {
  const _EmptyTaskSummarySource();

  @override
  Future<TaskSummary> get(String taskId) async {
    final now = DateTime.fromMillisecondsSinceEpoch(0, isUtc: true);
    return TaskSummary(
      id: taskId,
      name: 'Import',
      status: 'waiting_upload',
      totalAssets: 0,
      uploadedAssets: 0,
      failedAssets: 0,
      unsupportedAssets: 0,
      createdAt: now,
      updatedAt: now,
    );
  }
}

final class _PendingUnsupportedReports implements Exception {
  const _PendingUnsupportedReports();
}

class ImportTaskScreen extends ConsumerWidget {
  const ImportTaskScreen({required this.taskId, super.key});

  final String taskId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(importTaskControllerProvider(taskId));
    final controller = ref.read(importTaskControllerProvider(taskId).notifier);
    return Scaffold(
      appBar: AppBar(title: const Text('导入照片')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: <Widget>[
            Text('任务 $taskId', style: Theme.of(context).textTheme.bodySmall),
            const SizedBox(height: 16),
            Wrap(
              spacing: 12,
              runSpacing: 8,
              children: <Widget>[
                _CountLabel(label: '已接受', value: state.acceptedCount),
                _CountLabel(label: '不支持', value: state.unsupportedCount),
                _CountLabel(label: '排队中', value: state.queuedCount),
                _CountLabel(label: '已上传', value: state.uploadedCount),
                _CountLabel(label: '失败', value: state.failedCount),
              ],
            ),
            const SizedBox(height: 20),
            FilledButton.icon(
              key: const Key('add-photos'),
              onPressed: state.canAdd ? controller.addPhotos : null,
              icon: const Icon(Icons.add_photo_alternate_outlined),
              label: Text(
                state.atCapacity ? '已达到 $maximumAcceptedAssets 张上限' : '添加照片',
              ),
            ),
            const SizedBox(height: 8),
            OutlinedButton(
              key: const Key('finish-selection'),
              onPressed: state.canFinish
                  ? () async {
                      await controller.finishSelection();
                      if (!context.mounted) return;
                      final latest = ref.read(
                        importTaskControllerProvider(taskId),
                      );
                      if (latest.sealWhenIdle) {
                        context.go('/tasks/$taskId/progress');
                      }
                    }
                  : null,
              child: const Text('完成选图并上传'),
            ),
            if (state.sealWhenIdle) ...<Widget>[
              const SizedBox(height: 8),
              const Text('已设置：所有排队照片处理完成后自动结束任务'),
            ],
            if (state.errorMessage case final error?) ...<Widget>[
              const SizedBox(height: 8),
              Text(
                error,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            ],
            if (state.unsupported.isNotEmpty) ...<Widget>[
              const SizedBox(height: 24),
              Text('无法导入的文件', style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 8),
              for (final asset in state.unsupported)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text(asset.displayName),
                  subtitle: Text('原因：${_reasonLabel(asset.reason)}'),
                  leading: const Icon(Icons.warning_amber_outlined),
                ),
            ],
          ],
        ),
      ),
    );
  }
}

final class _CountLabel extends StatelessWidget {
  const _CountLabel({required this.label, required this.value});

  final String label;
  final int value;

  @override
  Widget build(BuildContext context) => Chip(label: Text('$label $value'));
}

String _reasonLabel(String reason) => switch (reason) {
  'unsupported_media_type' => '不支持的文件格式',
  'image_too_large' => '文件超过 100 MiB',
  'read_failure' => '无法读取文件',
  _ => reason,
};
