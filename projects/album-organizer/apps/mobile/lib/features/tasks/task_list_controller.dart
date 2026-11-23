import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/models.dart';
import 'task_repository.dart';

final taskListControllerProvider =
    StateNotifierProvider<TaskListController, AsyncValue<List<TaskSummary>>>(
      (ref) => TaskListController(ref.watch(taskRepositoryProvider)),
    );

final class TaskListController
    extends StateNotifier<AsyncValue<List<TaskSummary>>> {
  TaskListController(this._repository) : super(const AsyncValue.loading()) {
    Future<void>.microtask(refresh);
  }

  final TaskRepository _repository;

  Future<void> refresh() async {
    try {
      final tasks = await _repository.list();
      if (mounted) {
        state = AsyncValue.data(tasks);
      }
    } catch (error, stackTrace) {
      if (mounted) {
        state = AsyncValue.error(error, stackTrace);
      }
    }
  }

  Future<void> create(String name) async {
    final created = await _repository.create(name.trim());
    final current = state.valueOrNull ?? const <TaskSummary>[];
    state = AsyncValue.data(<TaskSummary>[created, ...current]);
  }
}
