import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/api_client.dart';

void main() {
  group('release build environment', () {
    test('rejects a missing APP_ENV instead of defaulting to development', () {
      expect(
        () => AppEnvironment.fromValues(name: '', isReleaseMode: true),
        throwsA(isA<StateError>()),
      );
    });

    test('rejects an explicit development environment', () {
      expect(
        () => AppEnvironment.fromValues(
          name: 'development',
          apiBaseUrl: 'https://api.example.com',
          isReleaseMode: true,
        ),
        throwsA(isA<StateError>()),
      );
    });

    test('rejects production without an explicit base URL', () {
      expect(
        () =>
            AppEnvironment.fromValues(name: 'production', isReleaseMode: true),
        throwsA(isA<StateError>()),
      );
    });

    test('rejects a cleartext production base URL', () {
      expect(
        () => AppEnvironment.fromValues(
          name: 'production',
          apiBaseUrl: 'http://api.example.com',
          isReleaseMode: true,
        ),
        throwsA(isA<StateError>()),
      );
    });

    test('accepts only production with an explicit HTTPS base URL', () {
      final environment = AppEnvironment.fromValues(
        name: 'production',
        apiBaseUrl: 'https://api.example.com',
        isReleaseMode: true,
      );

      expect(environment.name, 'production');
      expect(environment.apiBaseUrl, 'https://api.example.com');
    });
  });

  group('production API environment', () {
    test('rejects a missing base URL', () {
      expect(
        () => AppEnvironment.fromValues(name: 'production'),
        throwsA(isA<StateError>()),
      );
    });

    test('rejects a cleartext base URL', () {
      expect(
        () => AppEnvironment.fromValues(
          name: 'production',
          apiBaseUrl: 'http://api.example.com',
        ),
        throwsA(isA<StateError>()),
      );
    });

    test('accepts an explicit HTTPS base URL', () {
      final environment = AppEnvironment.fromValues(
        name: 'production',
        apiBaseUrl: 'https://api.example.com',
      );

      expect(environment.name, 'production');
      expect(environment.apiBaseUrl, 'https://api.example.com');
    });
  });

  test('development defaults are adapted to the target platform', () {
    expect(
      AppEnvironment.fromValues(
        name: 'development',
        platform: TargetPlatform.android,
      ).apiBaseUrl,
      'http://10.0.2.2:8000',
    );
    expect(
      AppEnvironment.fromValues(
        name: 'development',
        platform: TargetPlatform.iOS,
      ).apiBaseUrl,
      'http://127.0.0.1:8000',
    );
  });

  test('non-release missing APP_ENV uses a platform development default', () {
    final environment = AppEnvironment.fromValues(
      name: '',
      platform: TargetPlatform.android,
      isReleaseMode: false,
    );

    expect(environment.name, 'development');
    expect(environment.apiBaseUrl, 'http://10.0.2.2:8000');
  });
}
