import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/auth/session_repository.dart';
import 'package:photo_organizer/features/auth/login_screen.dart';
import 'package:photo_organizer/features/auth/otp_screen.dart';

import '../../helpers/fakes.dart';
import '../../helpers/test_app.dart';

void main() {
  testWidgets('invalid phone is blocked locally with a Chinese error', (
    tester,
  ) async {
    final transport = FakeTransport();
    final session = SessionRepository(transport, MemoryTokenStore());
    await tester.pumpWidget(
      testApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
        ],
        child: const LoginScreen(),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('phone-field')), '13800138000');
    await tester.enterText(find.byKey(const Key('invite-field')), 'invite');
    await tester.tap(find.text('获取验证码'));
    await tester.pumpAndSettle();

    expect(find.text('请输入 E.164 格式手机号'), findsOneWidget);
    expect(transport.calls, isEmpty);
  });

  testWidgets('OTP field only accepts six digits', (tester) async {
    final session = SessionRepository(FakeTransport(), MemoryTokenStore());
    await tester.pumpWidget(
      testApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
        ],
        child: const OtpScreen(phoneE164: '+8613800000000'),
      ),
    );
    await tester.pumpAndSettle();

    final field = tester.widget<TextField>(find.byKey(const Key('otp-field')));
    expect(field.maxLength, 6);
    await tester.enterText(find.byKey(const Key('otp-field')), '12a34567');
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('otp-field')))
          .controller
          ?.text,
      '123456',
    );
  });
}
