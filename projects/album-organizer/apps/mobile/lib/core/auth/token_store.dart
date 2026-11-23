import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/models.dart';

abstract interface class TokenStore {
  Future<TokenPair?> read();
  Future<void> write(TokenPair pair);
  Future<void> clear();
}

final tokenStoreProvider = Provider<TokenStore>((_) => SecureTokenStore());

final class SecureTokenStore implements TokenStore {
  SecureTokenStore({FlutterSecureStorage? storage})
    : _storage = storage ?? const FlutterSecureStorage();

  static const _accessTokenKey = 'session.access_token';
  static const _refreshTokenKey = 'session.refresh_token';
  static const _expiresAtKey = 'session.expires_at';
  static const _deviceIdKey = 'session.device_id';

  final FlutterSecureStorage _storage;

  @override
  Future<TokenPair?> read() async {
    final values = await Future.wait<String?>(<Future<String?>>[
      _storage.read(key: _accessTokenKey),
      _storage.read(key: _refreshTokenKey),
      _storage.read(key: _expiresAtKey),
      _storage.read(key: _deviceIdKey),
    ]);
    if (values.any((value) => value == null)) {
      return null;
    }
    final expiresAt = DateTime.tryParse(values[2]!);
    if (expiresAt == null) {
      await clear();
      return null;
    }
    return TokenPair(
      accessToken: values[0]!,
      refreshToken: values[1]!,
      expiresAt: expiresAt,
      deviceId: values[3]!,
    );
  }

  @override
  Future<void> write(TokenPair pair) async {
    await Future.wait<void>(<Future<void>>[
      _storage.write(key: _accessTokenKey, value: pair.accessToken),
      _storage.write(key: _refreshTokenKey, value: pair.refreshToken),
      _storage.write(
        key: _expiresAtKey,
        value: pair.expiresAt.toIso8601String(),
      ),
      _storage.write(key: _deviceIdKey, value: pair.deviceId),
    ]);
  }

  @override
  Future<void> clear() async {
    await Future.wait<void>(<Future<void>>[
      _storage.delete(key: _accessTokenKey),
      _storage.delete(key: _refreshTokenKey),
      _storage.delete(key: _expiresAtKey),
      _storage.delete(key: _deviceIdKey),
    ]);
  }
}
