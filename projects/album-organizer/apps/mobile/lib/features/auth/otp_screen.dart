import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_controller.dart';

final class OtpScreen extends ConsumerStatefulWidget {
  const OtpScreen({required this.phoneE164, super.key});

  final String phoneE164;

  @override
  ConsumerState<OtpScreen> createState() => _OtpScreenState();
}

final class _OtpScreenState extends ConsumerState<OtpScreen> {
  final _codeController = TextEditingController();

  @override
  void dispose() {
    _codeController.dispose();
    super.dispose();
  }

  Future<void> _verify() async {
    await ref
        .read(authControllerProvider.notifier)
        .verifyCode(phoneE164: widget.phoneE164, code: _codeController.text);
  }

  @override
  Widget build(BuildContext context) {
    final auth = ref.watch(authControllerProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('输入验证码')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(24),
          children: <Widget>[
            Text('验证码已发送至 ${widget.phoneE164}'),
            const SizedBox(height: 20),
            TextField(
              key: const Key('otp-field'),
              controller: _codeController,
              keyboardType: TextInputType.number,
              maxLength: 6,
              inputFormatters: <TextInputFormatter>[
                FilteringTextInputFormatter.digitsOnly,
                LengthLimitingTextInputFormatter(6),
              ],
              decoration: const InputDecoration(labelText: '6 位验证码'),
            ),
            if (auth.errorMessage case final message?) ...<Widget>[
              const SizedBox(height: 12),
              Text(
                message,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            ],
            const SizedBox(height: 20),
            FilledButton(
              onPressed: auth.busy ? null : _verify,
              child: auth.busy
                  ? const SizedBox.square(
                      dimension: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Text('登录'),
            ),
          ],
        ),
      ),
    );
  }
}
