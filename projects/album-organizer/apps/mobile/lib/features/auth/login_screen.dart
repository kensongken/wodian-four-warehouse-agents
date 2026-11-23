import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import 'auth_controller.dart';

final class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});

  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

final class _LoginScreenState extends ConsumerState<LoginScreen> {
  final _phoneController = TextEditingController();
  final _inviteController = TextEditingController();

  @override
  void dispose() {
    _phoneController.dispose();
    _inviteController.dispose();
    super.dispose();
  }

  Future<void> _requestCode() async {
    final succeeded = await ref
        .read(authControllerProvider.notifier)
        .requestCode(
          phoneE164: _phoneController.text,
          inviteCode: _inviteController.text,
        );
    if (succeeded && mounted) {
      context.go('/verify');
    }
  }

  @override
  Widget build(BuildContext context) {
    final auth = ref.watch(authControllerProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('登录')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(24),
          children: <Widget>[
            Text(
              'AI 商品相册整理助手',
              style: Theme.of(context).textTheme.headlineSmall,
            ),
            const SizedBox(height: 28),
            TextField(
              key: const Key('phone-field'),
              controller: _phoneController,
              keyboardType: TextInputType.phone,
              decoration: const InputDecoration(
                labelText: '手机号',
                hintText: '+8613800000000',
              ),
            ),
            const SizedBox(height: 16),
            TextField(
              key: const Key('invite-field'),
              controller: _inviteController,
              decoration: const InputDecoration(labelText: '邀请码'),
            ),
            if (auth.errorMessage case final message?) ...<Widget>[
              const SizedBox(height: 12),
              Text(
                message,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            ],
            const SizedBox(height: 24),
            FilledButton(
              onPressed: auth.busy ? null : _requestCode,
              child: auth.busy
                  ? const SizedBox.square(
                      dimension: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Text('获取验证码'),
            ),
          ],
        ),
      ),
    );
  }
}
