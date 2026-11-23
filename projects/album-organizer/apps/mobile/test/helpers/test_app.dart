import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:photo_organizer/features/tasks/task_list_screen.dart';

Widget testApp({
  List<Override> overrides = const <Override>[],
  Widget child = const TaskListScreen(),
}) {
  return ProviderScope(
    overrides: overrides,
    child: MaterialApp(home: child),
  );
}
