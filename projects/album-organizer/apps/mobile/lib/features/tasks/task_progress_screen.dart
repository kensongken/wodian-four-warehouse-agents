import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'task_progress_controller.dart';

class TaskProgressScreen extends ConsumerStatefulWidget {
  const TaskProgressScreen({required this.taskId, super.key});

  final String taskId;

  @override
  ConsumerState<TaskProgressScreen> createState() => _TaskProgressScreenState();
}

class _TaskProgressScreenState extends ConsumerState<TaskProgressScreen>
    with WidgetsBindingObserver {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    ref
        .read(taskProgressControllerProvider(widget.taskId).notifier)
        .setForeground(state == AppLifecycleState.resumed);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(taskProgressControllerProvider(widget.taskId));
    final controller = ref.read(
      taskProgressControllerProvider(widget.taskId).notifier,
    );
    return Scaffold(
      appBar: AppBar(title: const Text('上传进度')),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: controller.refresh,
          child: ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.all(16),
            children: <Widget>[
              if (state.loading)
                const Center(child: CircularProgressIndicator())
              else ...<Widget>[
                Text(
                  state.summary?.name ?? '商品图册',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
                const SizedBox(height: 8),
                Text(
                  state.stageLabel,
                  key: const Key('task-stage'),
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 16),
                Text(
                  '${state.uploaded} / ${state.accepted}',
                  style: Theme.of(context).textTheme.headlineMedium,
                ),
                const SizedBox(height: 8),
                LinearProgressIndicator(
                  value: state.accepted == 0
                      ? 0
                      : (state.uploaded / state.accepted).clamp(0, 1),
                ),
                const SizedBox(height: 16),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: <Widget>[
                    _CountChip(label: '已选择', value: state.selected),
                    _CountChip(label: '已哈希', value: state.hashed),
                    _CountChip(label: '排队中', value: state.queued),
                    _CountChip(label: '上传中', value: state.uploading),
                    _CountChip(label: '失败', value: state.failed),
                    _CountChip(label: '不支持', value: state.unsupported),
                  ],
                ),
                if (state.isAiProcessing) ...<Widget>[
                  const SizedBox(height: 20),
                  const Text('图片导入已经完成，AI 整理将在下一阶段继续；当前不会展示虚构的 AI 百分比。'),
                ],
                if (state.sealWhenIdle && !state.serverSealed) ...<Widget>[
                  const SizedBox(height: 12),
                  const Text('全部已接受图片处理完成后，任务会自动结束。'),
                ],
              ],
              if (state.errorMessage case final error?) ...<Widget>[
                const SizedBox(height: 16),
                Text(
                  error,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
                TextButton(
                  onPressed: controller.refresh,
                  child: const Text('重试'),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

final class _CountChip extends StatelessWidget {
  const _CountChip({required this.label, required this.value});

  final String label;
  final int value;

  @override
  Widget build(BuildContext context) => Chip(label: Text('$label $value'));
}
