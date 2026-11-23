import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api/models.dart';
import 'create_task_sheet.dart';
import 'task_list_controller.dart';

final class TaskListScreen extends ConsumerStatefulWidget {
  const TaskListScreen({super.key});

  @override
  ConsumerState<TaskListScreen> createState() => _TaskListScreenState();
}

final class _TaskListScreenState extends ConsumerState<TaskListScreen>
    with WidgetsBindingObserver {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      ref.read(taskListControllerProvider.notifier).refresh();
    }
  }

  Future<void> _createTask() async {
    final name = await showCreateTaskSheet(context);
    if (name == null || !mounted) {
      return;
    }
    try {
      await ref.read(taskListControllerProvider.notifier).create(name);
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('创建失败，请稍后重试')));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final tasks = ref.watch(taskListControllerProvider);
    return Scaffold(
      appBar: AppBar(
        title: const Text('整理任务'),
        actions: <Widget>[
          IconButton(
            tooltip: '分类审核',
            onPressed: () => context.push('/reviews'),
            icon: const Icon(Icons.fact_check_outlined),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: ref.read(taskListControllerProvider.notifier).refresh,
        child: tasks.when(
          loading: () => ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            children: const <Widget>[
              SizedBox(height: 240),
              Center(child: CircularProgressIndicator()),
            ],
          ),
          error: (_, _) => ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            children: <Widget>[
              const SizedBox(height: 200),
              const Center(child: Text('加载失败，下拉重试')),
              Center(
                child: TextButton(
                  onPressed: ref
                      .read(taskListControllerProvider.notifier)
                      .refresh,
                  child: const Text('重试'),
                ),
              ),
            ],
          ),
          data: (items) => items.isEmpty
              ? ListView(
                  physics: const AlwaysScrollableScrollPhysics(),
                  children: const <Widget>[
                    SizedBox(height: 220),
                    Center(child: Text('还没有整理任务')),
                  ],
                )
              : ListView.separated(
                  physics: const AlwaysScrollableScrollPhysics(),
                  padding: const EdgeInsets.all(16),
                  itemCount: items.length,
                  separatorBuilder: (_, _) => const SizedBox(height: 12),
                  itemBuilder: (context, index) =>
                      _TaskCard(task: items[index]),
                ),
        ),
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _createTask,
        icon: const Icon(Icons.add),
        label: const Text('新建整理任务'),
      ),
    );
  }
}

final class _TaskCard extends StatelessWidget {
  const _TaskCard({required this.task});

  final TaskSummary task;

  @override
  Widget build(BuildContext context) {
    final action = _nextAction(task.status);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Expanded(
                  child: Text(
                    task.name,
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                Text(task.status),
              ],
            ),
            const SizedBox(height: 12),
            Text('${task.uploadedAssets} / ${task.totalAssets}'),
            const SizedBox(height: 4),
            Text('失败 ${task.failedAssets} · 不支持 ${task.unsupportedAssets}'),
            const SizedBox(height: 8),
            Align(
              alignment: Alignment.centerRight,
              child: TextButton(
                onPressed: action.opensImport
                    ? () => context.push('/tasks/${task.id}/import')
                    : action.opensReviews
                    ? () => context.push('/reviews')
                    : null,
                child: Text(action.label),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

({String label, bool opensImport, bool opensReviews}) _nextAction(
  String status,
) {
  return switch (status) {
    'waiting_upload' => (label: '添加照片', opensImport: true, opensReviews: false),
    'uploading' || 'paused' || 'partial_failure' => (
      label: '继续上传',
      opensImport: true,
      opensReviews: false,
    ),
    'ai_processing' => (
      label: 'AI 处理中',
      opensImport: false,
      opensReviews: false,
    ),
    'waiting_review' => (label: '开始审核', opensImport: false, opensReviews: true),
    'completed' => (label: '已完成', opensImport: false, opensReviews: false),
    'cancelled' => (label: '已取消', opensImport: false, opensReviews: false),
    _ => (label: status, opensImport: false, opensReviews: false),
  };
}
