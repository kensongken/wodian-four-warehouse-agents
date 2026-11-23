import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/models.dart';
import 'package:photo_organizer/features/tasks/task_repository.dart';

import '../../helpers/fakes.dart';
import '../../helpers/test_app.dart';

final uploadingTask = TaskSummary(
  id: '11111111-1111-1111-1111-111111111111',
  name: '8月看货',
  status: 'uploading',
  totalAssets: 1268,
  uploadedAssets: 923,
  failedAssets: 4,
  unsupportedAssets: 2,
  createdAt: DateTime.utc(2026, 8, 14),
  updatedAt: DateTime.utc(2026, 8, 14),
);

final waitingReviewTask = TaskSummary(
  id: '22222222-2222-4222-8222-222222222222',
  name: '待审核图册',
  status: 'waiting_review',
  totalAssets: 10,
  uploadedAssets: 10,
  failedAssets: 0,
  unsupportedAssets: 0,
  createdAt: DateTime.utc(2026, 8, 17),
  updatedAt: DateTime.utc(2026, 8, 17),
);

void main() {
  testWidgets('shows exact progress counts, server state, and next action', (
    tester,
  ) async {
    final repo = FakeTaskRepository(items: <TaskSummary>[uploadingTask]);

    await tester.pumpWidget(
      testApp(
        overrides: <Override>[taskRepositoryProvider.overrideWithValue(repo)],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('uploading'), findsOneWidget);
    expect(find.text('923 / 1268'), findsOneWidget);
    expect(find.text('失败 4 · 不支持 2'), findsOneWidget);
    expect(find.text('继续上传'), findsOneWidget);
  });

  testWidgets('creates a task with trimmed input', (tester) async {
    final repo = FakeTaskRepository(items: <TaskSummary>[uploadingTask]);
    await tester.pumpWidget(
      testApp(
        overrides: <Override>[taskRepositoryProvider.overrideWithValue(repo)],
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.text('新建整理任务'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), '  8月看货  ');
    await tester.tap(find.text('创建'));
    await tester.pumpAndSettle();

    expect(repo.createdNames, <String>['8月看货']);
  });

  testWidgets('new task defaults to the local date and limits input to 80', (
    tester,
  ) async {
    final repo = FakeTaskRepository();
    final now = DateTime.now();
    final expectedName = '${now.month}月${now.day}日整理';
    await tester.pumpWidget(
      testApp(
        overrides: <Override>[taskRepositoryProvider.overrideWithValue(repo)],
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.text('新建整理任务'));
    await tester.pumpAndSettle();

    final field = tester.widget<TextField>(find.byType(TextField));
    expect(field.controller?.text, expectedName);
    expect(field.maxLength, 80);
  });

  testWidgets('an 81-character paste never creates a name over 80', (
    tester,
  ) async {
    final repo = FakeTaskRepository();
    await tester.pumpWidget(
      testApp(
        overrides: <Override>[taskRepositoryProvider.overrideWithValue(repo)],
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('新建整理任务'));
    await tester.pumpAndSettle();

    final pastedName = List<String>.filled(81, '名').join();
    await tester.enterText(find.byType(TextField), pastedName);
    await tester.tap(find.text('创建'));
    await tester.pumpAndSettle();

    expect(repo.createdNames, hasLength(1));
    expect(repo.createdNames.single.length, 80);
    expect(repo.createdNames.single, List<String>.filled(80, '名').join());
  });

  testWidgets('pull-to-refresh and app resume reload tasks', (tester) async {
    final repo = FakeTaskRepository(items: <TaskSummary>[uploadingTask]);
    await tester.pumpWidget(
      testApp(
        overrides: <Override>[taskRepositoryProvider.overrideWithValue(repo)],
      ),
    );
    await tester.pumpAndSettle();
    expect(repo.listCalls, 1);

    await tester.drag(find.byType(RefreshIndicator), const Offset(0, 500));
    await tester.pumpAndSettle();
    expect(repo.listCalls, 2);

    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pumpAndSettle();
    expect(repo.listCalls, 3);
  });

  testWidgets('waiting review exposes an enabled classification action', (
    tester,
  ) async {
    final repo = FakeTaskRepository(items: <TaskSummary>[waitingReviewTask]);
    await tester.pumpWidget(
      testApp(
        overrides: <Override>[taskRepositoryProvider.overrideWithValue(repo)],
      ),
    );
    await tester.pumpAndSettle();

    final action = tester.widget<TextButton>(
      find.widgetWithText(TextButton, '开始审核'),
    );
    expect(action.onPressed, isNotNull);
  });
}
