import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

final appEnvironmentProvider = Provider<AppEnvironment>(
  (_) => AppEnvironment.fromDartDefines(),
);

final class AppEnvironment {
  const AppEnvironment._({required this.name, required this.apiBaseUrl});

  factory AppEnvironment.fromDartDefines({
    TargetPlatform? platform,
    bool? isReleaseMode,
  }) => AppEnvironment.fromValues(
    name: const String.fromEnvironment('APP_ENV'),
    apiBaseUrl: const String.fromEnvironment('API_BASE_URL'),
    platform: platform,
    isReleaseMode: isReleaseMode ?? kReleaseMode,
  );

  factory AppEnvironment.fromValues({
    required String name,
    String? apiBaseUrl,
    TargetPlatform? platform,
    bool isReleaseMode = false,
  }) {
    final configuredName = name.trim().toLowerCase();
    if (isReleaseMode && configuredName != 'production') {
      throw StateError('Release builds require APP_ENV=production');
    }
    final normalizedName = configuredName.isEmpty && !isReleaseMode
        ? 'development'
        : configuredName;
    if (!const <String>{
      'development',
      'test',
      'production',
    }.contains(normalizedName)) {
      throw StateError('Unsupported APP_ENV');
    }
    final configuredUrl = apiBaseUrl?.trim() ?? '';
    if (normalizedName == 'production' && configuredUrl.isEmpty) {
      throw StateError('Production API_BASE_URL must be explicit');
    }
    final resolvedUrl = configuredUrl.isEmpty
        ? _developmentBaseUrl(platform ?? defaultTargetPlatform)
        : configuredUrl;
    final uri = Uri.tryParse(resolvedUrl);
    if (uri == null ||
        !uri.hasScheme ||
        uri.host.isEmpty ||
        (uri.scheme != 'http' && uri.scheme != 'https') ||
        uri.userInfo.isNotEmpty) {
      throw StateError(
        'API_BASE_URL must be an HTTP(S) origin without credentials',
      );
    }
    if (normalizedName == 'production' && uri.scheme != 'https') {
      throw StateError('Production API_BASE_URL must use HTTPS');
    }
    return AppEnvironment._(name: normalizedName, apiBaseUrl: resolvedUrl);
  }

  final String name;
  final String apiBaseUrl;

  static String _developmentBaseUrl(TargetPlatform platform) =>
      platform == TargetPlatform.android
      ? 'http://10.0.2.2:8000'
      : 'http://127.0.0.1:8000';
}

final apiTransportProvider = Provider<ApiTransport>(
  (ref) =>
      DioApiTransport(baseUrl: ref.watch(appEnvironmentProvider).apiBaseUrl),
);

final class ApiResponse {
  const ApiResponse({
    required this.statusCode,
    this.data,
    this.headers = const <String, String>{},
  });

  final int statusCode;
  final Object? data;
  final Map<String, String> headers;
}

abstract interface class ApiTransport {
  Future<ApiResponse> request(
    String method,
    String path, {
    Object? data,
    Map<String, String> headers = const <String, String>{},
    Map<String, Object?> query = const <String, Object?>{},
  });
}

final class DioApiTransport implements ApiTransport {
  DioApiTransport({required String baseUrl, Dio? dio})
    : _dio =
          dio ??
          Dio(
            BaseOptions(
              baseUrl: baseUrl,
              connectTimeout: const Duration(seconds: 15),
              receiveTimeout: const Duration(seconds: 30),
              validateStatus: (_) => true,
            ),
          );

  final Dio _dio;

  @override
  Future<ApiResponse> request(
    String method,
    String path, {
    Object? data,
    Map<String, String> headers = const <String, String>{},
    Map<String, Object?> query = const <String, Object?>{},
  }) async {
    final response = await _dio.request<Object?>(
      path,
      data: data,
      queryParameters: query,
      options: Options(method: method, headers: headers),
    );
    return ApiResponse(
      statusCode: response.statusCode ?? 0,
      data: response.data,
      headers: <String, String>{
        for (final entry in response.headers.map.entries)
          entry.key: entry.value.join(','),
      },
    );
  }
}
