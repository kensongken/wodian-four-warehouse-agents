import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/models.dart';
import '../../core/upload/upload_models.dart';
import '../../core/upload/upload_queue_repository.dart';
import '../../core/upload/upload_runtime.dart';
import 'task_repository.dart';

abstract interface class TaskProgressDataSource {
  Stream<LocalUploadProgress> watchLocal(String taskId);

  Future<ImportQueueSnapshot> snapshotTask(String taskId);

  Future<TaskSummary> getTask(String taskId);

  Future<void> requestBackgroundWork(String taskId);
}

final taskProgressDataSourceProvider = Provider<TaskProgressDataSource>((ref) {
  return _DefaultTaskProgressDataSource(
    ref.watch(uploadQueueRepositoryProvider),
    ref.watch(taskRepositoryProvider),
    ref.watch(backgroundUploadRuntimeProvider),
  );
});

final taskProgressControllerProvider = StateNotifierProvider.autoDispose
    .family<TaskProgressController, TaskProgressState, String>((ref, taskId) {
      return TaskProgressController(
        taskId: taskId,
        source: ref.watch(taskProgressDataSourceProvider),
      );
    });

final class TaskProgressState {
  const TaskProgressState({
    this.summary,
    this.local,
    this.snapshot,
    this.loading = true,
    this.refreshing = false,
    this.errorMessage,
  });

  final TaskSummary? summary;
  final LocalUploadProgress? local;
  final ImportQueueSnapshot? snapshot;
  final bool loading;
  final bool refreshing;
  final String? errorMessage;

  int get accepted =>
      (summary?.totalAssets ?? 0) +
      (snapshot?.unacknowledgedAssetIds.length ?? 0);
  int get selected => local?.total ?? 0;
  int get hashed => local?.hashed ?? 0;
  int get queued => local?.queued ?? 0;
  int get uploading => local?.uploading ?? 0;
  int get waitingWifi => local?.waitingWifi ?? 0;
  int get uploaded =>
      (summary?.uploadedAssets ?? 0) + (snapshot?.uploaded ?? 0);
  int get failed => (summary?.failedAssets ?? 0) + (snapshot?.failed ?? 0);
  int get unsupported => summary?.unsupportedAssets ?? 0;
  bool get sealWhenIdle => snapshot?.sealWhenIdle ?? false;
  bool get serverSealed => snapshot?.serverSealed ?? false;

  String get stageLabel => stageLabelFor(
    serverStatus: summary?.status ?? TaskStatusWire.waitingUpload,
    waitingWifi: waitingWifi,
  );

  bool get isAiProcessing =>
      summary?.status == TaskStatusWire.aiProcessing ||
      summary?.status == TaskStatusWire.waitingReview ||
      summary?.status == TaskStatusWire.completed;

  bool get readyForDurableSeal {
    final task = summary;
    final queue = snapshot;
    final progress = local;
    if (task == null || queue == null || progress == null) return false;
    return queue.sealWhenIdle &&
        !queue.serverSealed &&
        TaskStatusWire.uploadOpen.contains(task.status) &&
        queue.queued == 0 &&
        queue.failed == 0 &&
        progress.uploading == 0 &&
        progress.waitingWifi == 0 &&
        uploaded == accepted;
  }

  static String stageLabelFor({
    required String serverStatus,
    required int waitingWifi,
  }) {
    if (serverStatus == TaskStatusWire.cancelled) return '已取消';
    if (serverStatus == TaskStatusWire.partialFailure) return '部分失败';
    if (TaskStatusWire.uploadClosed.contains(serverStatus)) return 'AI 处理中';
    if (serverStatus == TaskStatusWire.paused || waitingWifi > 0) {
      return '等待 Wi‑Fi';
    }
    if (serverStatus == TaskStatusWire.uploading) return '上传中';
    return '等待上传';
  }

  TaskProgressState copyWith({
    TaskSummary? summary,
    LocalUploadProgress? local,
    ImportQueueSnapshot? snapshot,
    bool? loading,
    bool? refreshing,
    String? errorMessage,
    bool clearError = false,
  }) {
    return TaskProgressState(
      summary: summary ?? this.summary,
      local: local ?? this.local,
      snapshot: snapshot ?? this.snapshot,
      loading: loading ?? this.loading,
      refreshing: refreshing ?? this.refreshing,
      errorMessage: clearError ? null : errorMessage ?? this.errorMessage,
    );
  }
}

final class TaskProgressController extends StateNotifier<TaskProgressState> {
  factory TaskProgressController({
    required String taskId,
    required TaskProgressDataSource source,
    Duration foregroundRefresh = const Duration(seconds: 5),
    Duration backgroundRefresh = const Duration(seconds: 30),
  }) => TaskProgressController._(
    taskId: taskId,
    source: source,
    foregroundRefresh: foregroundRefresh,
    backgroundRefresh: backgroundRefresh,
  );

  TaskProgressController._({
    required this.taskId,
    required this._source,
    required this._foregroundRefresh,
    required this._backgroundRefresh,
  }) : super(const TaskProgressState()) {
    _localSubscription = _source
        .watchLocal(taskId)
        .listen(
          (local) {
            if (!_active) return;
            state = state.copyWith(local: local);
            _requestDurableSealIfReady();
          },
          onError: (Object _) {
            if (_active) {
              state = state.copyWith(errorMessage: '无法读取本地上传进度，请稍后重试');
            }
          },
        );
    unawaited(refresh());
    _scheduleRefresh();
  }

  final String taskId;
  final TaskProgressDataSource _source;
  final Duration _foregroundRefresh;
  final Duration _backgroundRefresh;
  late final StreamSubscription<LocalUploadProgress> _localSubscription;
  Timer? _refreshTimer;
  bool _foreground = true;
  bool _sealWorkRequested = false;
  bool _active = true;
  Future<void>? _refreshInFlight;

  Future<void> refresh() {
    return _refreshInFlight ??= _refresh().whenComplete(() {
      _refreshInFlight = null;
    });
  }

  Future<void> _refresh() async {
    if (!_active) return;
    state = state.copyWith(refreshing: true, clearError: true);
    try {
      final results = await Future.wait<Object>(<Future<Object>>[
        _source.getTask(taskId),
        _source.snapshotTask(taskId),
      ]);
      if (!_active) return;
      final summary = results[0] as TaskSummary;
      final snapshot = results[1] as ImportQueueSnapshot;
      _validate(summary, snapshot);
      state = state.copyWith(
        summary: summary,
        snapshot: snapshot,
        loading: false,
        refreshing: false,
      );
      if (snapshot.serverSealed) _sealWorkRequested = true;
      _requestDurableSealIfReady();
    } catch (_) {
      if (_active) {
        state = state.copyWith(
          loading: false,
          refreshing: false,
          errorMessage: '无法同步任务进度，请稍后重试',
        );
      }
    }
  }

  void setForeground(bool foreground) {
    if (_foreground == foreground) {
      if (foreground) unawaited(refresh());
      return;
    }
    _foreground = foreground;
    _scheduleRefresh();
    if (foreground) unawaited(refresh());
  }

  void _requestDurableSealIfReady() {
    if (_sealWorkRequested || !state.readyForDurableSeal) return;
    _sealWorkRequested = true;
    unawaited(
      _source.requestBackgroundWork(taskId).catchError((Object _) {
        if (_active && !state.serverSealed) _sealWorkRequested = false;
      }),
    );
  }

  void _scheduleRefresh() {
    _refreshTimer?.cancel();
    final interval = _foreground ? _foregroundRefresh : _backgroundRefresh;
    _refreshTimer = Timer.periodic(interval, (_) => unawaited(refresh()));
  }

  static void _validate(TaskSummary summary, ImportQueueSnapshot snapshot) {
    if (!TaskStatusWire.all.contains(summary.status) ||
        summary.totalAssets < 0 ||
        summary.uploadedAssets < 0 ||
        summary.failedAssets < 0 ||
        summary.unsupportedAssets < 0 ||
        snapshot.queued < 0 ||
        snapshot.uploaded < 0 ||
        snapshot.failed < 0) {
      throw const FormatException('invalid task progress');
    }
  }

  @override
  void dispose() {
    _active = false;
    _refreshTimer?.cancel();
    unawaited(_localSubscription.cancel());
    super.dispose();
  }
}

final class _DefaultTaskProgressDataSource implements TaskProgressDataSource {
  const _DefaultTaskProgressDataSource(this._queue, this._tasks, this._runtime);

  final UploadQueueRepository _queue;
  final TaskRepository _tasks;
  final BackgroundUploadRuntime _runtime;

  @override
  Future<TaskSummary> getTask(String taskId) => _tasks.get(taskId);

  @override
  Future<void> requestBackgroundWork(String taskId) => _runtime.resume();

  @override
  Future<ImportQueueSnapshot> snapshotTask(String taskId) =>
      _queue.snapshot(taskId);

  @override
  Stream<LocalUploadProgress> watchLocal(String taskId) =>
      _queue.progress(taskId);
}
