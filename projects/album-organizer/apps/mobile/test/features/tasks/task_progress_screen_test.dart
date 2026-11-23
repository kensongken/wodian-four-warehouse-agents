import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/models.dart';
import 'package:photo_organizer/core/upload/upload_models.dart';
import 'package:photo_organizer/core/upload/upload_queue_repository.dart';
import 'package:photo_organizer/features/tasks/task_progress_controller.dart';
import 'package:photo_organizer/features/tasks/task_progress_screen.dart';

const _taskId = '11111111-1111-4111-8111-111111111111';

void main() {
  testWidgets(
    'shows reconciled progress and requests durable seal exactly once',
    (tester) async {
      final source = _FakeTaskProgressDataSource(
        summary: _summary(
          status: TaskStatusWire.uploading,
          total: 1000,
          uploaded: 1000,
        ),
        local: const LocalUploadProgress(
          taskId: _taskId,
          total: 1000,
          queued: 0,
          uploading: 0,
          uploaded: 1000,
          failed: 0,
        ),
        snapshot: const ImportQueueSnapshot(
          unacknowledgedAssetIds: <String>{},
          queued: 0,
          uploaded: 0,
          failed: 0,
          sealWhenIdle: true,
        ),
      );

      await tester.pumpWidget(_app(source));
      await tester.pumpAndSettle();

      expect(find.text('1000 / 1000'), findsOneWidget);
      expect(source.backgroundWorkRequests, 1);

      await tester.pump(const Duration(seconds: 5));
      await tester.pump();
      expect(source.backgroundWorkRequests, 1);
    },
  );

  testWidgets('reconciles local deltas without double counting server totals', (
    tester,
  ) async {
    final source = _FakeTaskProgressDataSource(
      summary: _summary(total: 8, uploaded: 5, failed: 1),
      local: const LocalUploadProgress(
        taskId: _taskId,
        total: 10,
        queued: 1,
        uploading: 1,
        uploaded: 7,
        failed: 1,
      ),
      snapshot: const ImportQueueSnapshot(
        unacknowledgedAssetIds: <String>{'local-a', 'local-b'},
        queued: 2,
        uploaded: 2,
        failed: 0,
      ),
    );

    await tester.pumpWidget(_app(source));
    await tester.pumpAndSettle();

    expect(find.text('7 / 10'), findsOneWidget);
    expect(find.text('上传中 1'), findsOneWidget);
    expect(find.text('失败 1'), findsOneWidget);
  });

  test('uses the exact Phase 1 stage labels without fake AI percentages', () {
    expect(
      TaskProgressState.stageLabelFor(
        serverStatus: TaskStatusWire.waitingUpload,
        waitingWifi: 0,
      ),
      '等待上传',
    );
    expect(
      TaskProgressState.stageLabelFor(
        serverStatus: TaskStatusWire.uploading,
        waitingWifi: 0,
      ),
      '上传中',
    );
    expect(
      TaskProgressState.stageLabelFor(
        serverStatus: TaskStatusWire.uploading,
        waitingWifi: 1,
      ),
      '等待 Wi‑Fi',
    );
    expect(
      TaskProgressState.stageLabelFor(
        serverStatus: TaskStatusWire.aiProcessing,
        waitingWifi: 0,
      ),
      'AI 处理中',
    );
    expect(
      TaskProgressState.stageLabelFor(
        serverStatus: TaskStatusWire.partialFailure,
        waitingWifi: 0,
      ),
      '部分失败',
    );
    expect(
      TaskProgressState.stageLabelFor(
        serverStatus: TaskStatusWire.cancelled,
        waitingWifi: 0,
      ),
      '已取消',
    );
  });
}

Widget _app(_FakeTaskProgressDataSource source) => ProviderScope(
  overrides: <Override>[
    taskProgressDataSourceProvider.overrideWithValue(source),
  ],
  child: const MaterialApp(home: TaskProgressScreen(taskId: _taskId)),
);

TaskSummary _summary({
  String status = TaskStatusWire.uploading,
  int total = 0,
  int uploaded = 0,
  int failed = 0,
}) {
  final now = DateTime.utc(2026, 8, 16);
  return TaskSummary(
    id: _taskId,
    name: '8月商品图册',
    status: status,
    totalAssets: total,
    uploadedAssets: uploaded,
    failedAssets: failed,
    unsupportedAssets: 0,
    createdAt: now,
    updatedAt: now,
  );
}

final class _FakeTaskProgressDataSource implements TaskProgressDataSource {
  _FakeTaskProgressDataSource({
    required this.summary,
    required this.local,
    required this.snapshot,
  });

  TaskSummary summary;
  LocalUploadProgress local;
  ImportQueueSnapshot snapshot;
  int backgroundWorkRequests = 0;
  @override
  Future<TaskSummary> getTask(String taskId) async => summary;

  @override
  Future<void> requestBackgroundWork(String taskId) async {
    backgroundWorkRequests += 1;
  }

  @override
  Future<ImportQueueSnapshot> snapshotTask(String taskId) async => snapshot;

  @override
  Stream<LocalUploadProgress> watchLocal(String taskId) => Stream.value(local);
}
