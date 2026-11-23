import 'package:dio/dio.dart';

import 'api_client.dart';

final class ApiException implements Exception {
  const ApiException({
    required this.code,
    required this.userMessage,
    this.retryAfterSeconds,
  });

  factory ApiException.fromResponse(ApiResponse response) {
    final data = response.data;
    final detail = data is Map ? data['detail'] : null;
    final code = _errorCode(response.statusCode, detail);
    final retryHeader = response.headers.entries
        .where((entry) => entry.key.toLowerCase() == 'retry-after')
        .map((entry) => int.tryParse(entry.value))
        .firstOrNull;
    return ApiException(
      code: code,
      userMessage: apiErrorMessage(code, retryAfterSeconds: retryHeader),
      retryAfterSeconds: retryHeader,
    );
  }

  factory ApiException.fromTransport(Object error) {
    if (error is DioException) {
      final isNetwork = <DioExceptionType>{
        DioExceptionType.connectionError,
        DioExceptionType.connectionTimeout,
        DioExceptionType.receiveTimeout,
        DioExceptionType.sendTimeout,
      }.contains(error.type);
      if (isNetwork) {
        return const ApiException(
          code: 'network_unavailable',
          userMessage: '网络不可用，请检查网络后重试',
        );
      }
    }
    return const ApiException(
      code: 'service_unavailable',
      userMessage: '服务暂时不可用，请稍后重试',
    );
  }

  final String code;
  final String userMessage;
  final int? retryAfterSeconds;

  @override
  String toString() => 'ApiException($code)';
}

String apiErrorMessage(String code, {int? retryAfterSeconds}) {
  return switch (code) {
    'invalid_invite' => '邀请码无效',
    'invalid_otp' => '验证码已过期或不正确',
    'rate_limited' =>
      retryAfterSeconds == null
          ? '尝试次数过多，请稍后重试'
          : '尝试次数过多，请在 $retryAfterSeconds 秒后重试',
    'network_unavailable' => '网络不可用，请检查网络后重试',
    'invalid_request' => '输入信息有误，请检查后重试',
    _ => '服务暂时不可用，请稍后重试',
  };
}

String _errorCode(int statusCode, Object? detail) {
  if (detail is String) {
    return detail;
  }
  if (statusCode == 422 && detail is List<Object?>) {
    for (final item in detail) {
      if (item is! Map<Object?, Object?>) {
        continue;
      }
      final location = item['loc'];
      if (location is List<Object?> && location.contains('invite_code')) {
        return 'invalid_invite';
      }
      if (location is List<Object?> && location.contains('code')) {
        return 'invalid_otp';
      }
    }
    return 'invalid_request';
  }
  return 'service_unavailable';
}
