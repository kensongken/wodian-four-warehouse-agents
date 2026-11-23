import 'package:flutter/material.dart';

Future<String?> showCreateTaskSheet(BuildContext context, {DateTime? now}) {
  final localNow = now ?? DateTime.now();
  return showModalBottomSheet<String>(
    context: context,
    isScrollControlled: true,
    builder: (context) => _CreateTaskSheet(now: localNow),
  );
}

final class _CreateTaskSheet extends StatefulWidget {
  const _CreateTaskSheet({required this.now});

  final DateTime now;

  @override
  State<_CreateTaskSheet> createState() => _CreateTaskSheetState();
}

final class _CreateTaskSheetState extends State<_CreateTaskSheet> {
  late final TextEditingController _controller = TextEditingController(
    text: '${widget.now.month}月${widget.now.day}日整理',
  );
  String? _errorText;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _submit() {
    final value = _controller.text.trim();
    if (value.isEmpty) {
      setState(() => _errorText = '请输入任务名称');
      return;
    }
    Navigator.of(context).pop(value);
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(
        24,
        24,
        24,
        MediaQuery.viewInsetsOf(context).bottom + 24,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Text('新建整理任务', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 16),
          TextField(
            controller: _controller,
            autofocus: true,
            maxLength: 80,
            decoration: InputDecoration(
              labelText: '任务名称',
              errorText: _errorText,
            ),
            onSubmitted: (_) => _submit(),
          ),
          const SizedBox(height: 12),
          FilledButton(onPressed: _submit, child: const Text('创建')),
        ],
      ),
    );
  }
}
