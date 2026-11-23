import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/api_client.dart';
import 'package:photo_organizer/core/api/api_error.dart';

void main() {
  test('maps authentication API errors to Chinese messages', () {
    expect(apiErrorMessage('invalid_invite'), '邀请码无效');
    expect(apiErrorMessage('invalid_otp'), '验证码已过期或不正确');
    expect(
      apiErrorMessage('rate_limited', retryAfterSeconds: 45),
      '尝试次数过多，请在 45 秒后重试',
    );
  });

  test('maps network and service failures to Chinese messages', () {
    expect(apiErrorMessage('network_unavailable'), '网络不可用，请检查网络后重试');
    expect(apiErrorMessage('otp_delivery_unavailable'), '服务暂时不可用，请稍后重试');
  });

  test('reads a rate-limit countdown case-insensitively', () {
    final error = ApiException.fromResponse(
      const ApiResponse(
        statusCode: 429,
        data: <String, Object>{'detail': 'rate_limited'},
        headers: <String, String>{'Retry-After': '30'},
      ),
    );

    expect(error.retryAfterSeconds, 30);
    expect(error.userMessage, '尝试次数过多，请在 30 秒后重试');
  });

  test('maps FastAPI validation detail for invite code without echoing it', () {
    final error = ApiException.fromResponse(
      const ApiResponse(
        statusCode: 422,
        data: <String, Object>{
          'detail': <Object>[
            <String, Object>{
              'type': 'string_too_long',
              'loc': <Object>['body', 'invite_code'],
              'msg': 'String should have at most 128 characters',
              'input': 'must-not-be-returned',
            },
          ],
        },
      ),
    );

    expect(error.code, 'invalid_invite');
    expect(error.userMessage, '邀请码无效');
    expect(error.toString(), isNot(contains('must-not-be-returned')));
  });
}
