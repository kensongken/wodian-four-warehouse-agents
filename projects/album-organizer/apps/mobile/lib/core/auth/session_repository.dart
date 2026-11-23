import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:uuid/uuid.dart';

import '../api/api_client.dart';
import '../api/api_error.dart';
import '../api/models.dart';
import 'token_store.dart';

final sessionRepositoryProvider = Provider<SessionRepository>((ref) {
  final repository = SessionRepository(
    ref.watch(apiTransportProvider),
    ref.watch(tokenStoreProvider),
  );
  ref.onDispose(repository.dispose);
  return repository;
});

final class SessionRepository {
  SessionRepository(
    this._transport,
    this._tokenStore, {
    String Function()? createDeviceId,
  }) : _createDeviceId = createDeviceId ?? const Uuid().v4;

  static final RegExp _e164 = RegExp(r'^\+[1-9]\d{1,14}$');
  static final RegExp _otp = RegExp(r'^\d{6}$');

  final ApiTransport _transport;
  final TokenStore _tokenStore;
  final String Function() _createDeviceId;
  final StreamController<bool> _sessionChanges =
      StreamController<bool>.broadcast(sync: true);
  Future<void> _mutationTail = Future<void>.value();
  ({
    _SessionSnapshot snapshot,
    int epoch,
    String? refreshToken,
    Future<TokenPair> future,
  })?
  _refreshInFlight;
  _SessionSnapshot _published = const _SessionSnapshot(epoch: 0, pair: null);
  Future<void>? _restoreInFlight;
  int _pendingMutations = 0;
  int _sessionIntentGeneration = 0;
  bool _restored = false;
  bool _disposed = false;

  Future<bool> get isSignedIn async => (await _currentSnapshot()).pair != null;
  Stream<bool> get sessionChanges => _sessionChanges.stream;

  Future<void> requestCode({
    required String phoneE164,
    required String inviteCode,
  }) async {
    _validatePhone(phoneE164);
    final normalizedInviteCode = inviteCode.trim();
    if (normalizedInviteCode.isEmpty || normalizedInviteCode.length > 128) {
      throw const FormatException('邀请码无效');
    }
    await _postSuccess(
      '/v1/auth/request-code',
      data: <String, Object>{
        'phone_e164': phoneE164,
        'invite_code': normalizedInviteCode,
      },
    );
  }

  Future<void> verifyCode({
    required String phoneE164,
    required String code,
  }) async {
    _validatePhone(phoneE164);
    if (!_otp.hasMatch(code)) {
      throw const FormatException('验证码必须为 6 位数字');
    }
    _ensureRestoreScheduled();
    final intent = ++_sessionIntentGeneration;
    final deviceId = _createDeviceId();
    late final ApiResponse response;
    try {
      response = await _postSuccess(
        '/v1/auth/verify-code',
        data: <String, Object>{
          'phone_e164': phoneE164,
          'code': code,
          'device_id': deviceId,
        },
      );
    } catch (_) {
      _ensureCurrentIntent(intent);
      rethrow;
    }
    _ensureCurrentIntent(intent);
    final pair = TokenPair.fromJson(
      _jsonObject(response.data),
      deviceId: deviceId,
    );
    _ensureCurrentIntent(intent);
    await _replaceSession(pair, expectedIntent: intent);
  }

  Future<TokenPair> refresh() async {
    final current = await _currentSnapshot();
    final context = _SessionContext(
      snapshot: current,
      intent: _sessionIntentGeneration,
    );
    _ensureCurrentContext(context);
    final existing = _refreshInFlight;
    if (existing != null &&
        identical(existing.snapshot, current) &&
        existing.epoch == current.epoch &&
        existing.refreshToken == current.pair?.refreshToken) {
      return existing.future;
    }
    late final Future<TokenPair> tracked;
    tracked = _performRefresh(context).whenComplete(() {
      if (identical(_refreshInFlight?.future, tracked)) {
        _refreshInFlight = null;
      }
    });
    _refreshInFlight = (
      snapshot: current,
      epoch: current.epoch,
      refreshToken: current.pair?.refreshToken,
      future: tracked,
    );
    return tracked;
  }

  Future<TokenPair> _performRefresh(_SessionContext expected) async {
    try {
      _ensureCurrentContext(expected);
      final current = expected.snapshot.pair;
      if (current == null) {
        throw const ApiException(
          code: 'invalid_refresh_token',
          userMessage: '登录已过期，请重新登录',
        );
      }
      final response = await _transport.request(
        'POST',
        '/v1/auth/refresh',
        data: <String, Object>{
          'refresh_token': current.refreshToken,
          'device_id': current.deviceId,
        },
      );
      if (!_isSuccess(response.statusCode)) {
        throw ApiException.fromResponse(response);
      }
      final refreshed = TokenPair.fromJson(
        _jsonObject(response.data),
        deviceId: current.deviceId,
      );
      _ensureCurrentContext(expected);
      await _persistRefresh(expected, refreshed);
      return refreshed;
    } catch (error) {
      if (error case final _RefreshPersistenceFailure failure) {
        throw ApiException.fromTransport(failure.cause);
      }
      if (error case final _DurableReconciliationFailure failure) {
        throw ApiException.fromTransport(failure.cause);
      }
      if (_isStaleSession(error) || !_isCurrentContext(expected)) {
        throw _staleSession();
      }
      await _invalidateIfCurrent(expected);
      if (error is ApiException || error is FormatException) {
        rethrow;
      }
      throw ApiException.fromTransport(error);
    }
  }

  Future<void> signOut() {
    _ensureRestoreScheduled();
    final intent = ++_sessionIntentGeneration;
    return _invalidateCurrentSession(expectedIntent: intent);
  }

  Future<ApiResponse> authorizedGet(
    String path, {
    Map<String, Object?> query = const <String, Object?>{},
  }) => _authorizedRequest('GET', path, query: query);

  Future<ApiResponse> authorizedPost(String path, {Object? data}) =>
      _authorizedRequest('POST', path, data: data);

  Future<ApiResponse> _authorizedRequest(
    String method,
    String path, {
    Object? data,
    Map<String, Object?> query = const <String, Object?>{},
  }) async {
    final initialSnapshot = await _currentSnapshot();
    final requestEpoch = initialSnapshot.epoch;
    final initial = await _requestWithSnapshot(
      method,
      path,
      initialSnapshot,
      data: data,
      query: query,
    );
    _ensureCurrentEpoch(requestEpoch);
    if (initial.statusCode != 401 || _isAuthRoute(path)) {
      return initial;
    }
    final currentSnapshot = await _currentSnapshot();
    _ensureSameEpoch(currentSnapshot, requestEpoch);
    if (currentSnapshot.pair == null ||
        currentSnapshot.pair?.accessToken ==
            initialSnapshot.pair?.accessToken) {
      await refresh();
    }
    final retrySnapshot = await _currentSnapshot();
    _ensureSameEpoch(retrySnapshot, requestEpoch);
    final retryContext = _SessionContext(
      snapshot: retrySnapshot,
      intent: _sessionIntentGeneration,
    );
    _ensureCurrentContext(retryContext);
    final retried = await _requestWithSnapshot(
      method,
      path,
      retrySnapshot,
      data: data,
      query: query,
    );
    _ensureCurrentEpoch(requestEpoch);
    if (retried.statusCode == 401) {
      _ensureCurrentSnapshot(retrySnapshot);
      await _invalidateIfCurrent(retryContext);
      throw ApiException.fromResponse(retried);
    }
    return retried;
  }

  Future<_SessionSnapshot> _currentSnapshot() async {
    while (true) {
      _ensureRestoreScheduled();
      final barrier = _mutationTail;
      await barrier;
      if (!identical(barrier, _mutationTail) || _pendingMutations != 0) {
        continue;
      }
      if (_restored) {
        return _published;
      }
      final restore = _restoreInFlight;
      if (restore != null) {
        await restore;
      } else {
        _ensureRestoreScheduled();
      }
    }
  }

  Future<ApiResponse> _requestWithSnapshot(
    String method,
    String path,
    _SessionSnapshot snapshot, {
    Object? data,
    Map<String, Object?> query = const <String, Object?>{},
  }) async {
    _ensureCurrentSnapshot(snapshot);
    return _transport.request(
      method,
      path,
      data: data,
      query: query,
      headers: snapshot.pair == null
          ? const <String, String>{}
          : <String, String>{
              'Authorization': 'Bearer ${snapshot.pair!.accessToken}',
            },
    );
  }

  Future<ApiResponse> _postSuccess(String path, {Object? data}) async {
    try {
      final response = await _transport.request('POST', path, data: data);
      if (!_isSuccess(response.statusCode)) {
        throw ApiException.fromResponse(response);
      }
      return response;
    } catch (error) {
      if (error is ApiException) {
        rethrow;
      }
      throw ApiException.fromTransport(error);
    }
  }

  static bool _isSuccess(int statusCode) =>
      statusCode >= 200 && statusCode < 300;

  static bool _isAuthRoute(String path) => path.startsWith('/v1/auth/');

  void _ensureRestoreScheduled() {
    if (_restored || _restoreInFlight != null) {
      return;
    }
    final restoreIntent = _sessionIntentGeneration;
    late final Future<void> tracked;
    tracked =
        _serializeMutation(() async {
          if (_restored) {
            return;
          }
          TokenPair? restored;
          var readSucceeded = false;
          try {
            restored = await _tokenStore.read();
            readSucceeded = true;
          } catch (_) {
            // The terminal fail-closed publication below owns recovery.
          }
          if (_restored) {
            return;
          }
          final canRestore =
              readSucceeded && restoreIntent == _sessionIntentGeneration;
          if (!canRestore) {
            await _clearPersistentCopy();
          }
          if (!_restored) {
            _publish(
              pair: canRestore ? restored : null,
              advanceEpoch: canRestore ? restored != null : true,
              emitChange: false,
            );
          }
        }).whenComplete(() {
          if (identical(_restoreInFlight, tracked)) {
            _restoreInFlight = null;
          }
        });
    _restoreInFlight = tracked;
  }

  Future<void> _replaceSession(
    TokenPair pair, {
    required int expectedIntent,
  }) async {
    _ensureCurrentIntent(expectedIntent);
    await _serializeMutation(() async {
      _ensureCurrentIntent(expectedIntent);
      Object? writeError;
      StackTrace? writeStack;
      try {
        await _tokenStore.write(pair);
      } catch (error, stackTrace) {
        writeError = error;
        writeStack = stackTrace;
      }
      if (!_isCurrentIntent(expectedIntent)) {
        await _reconcilePersistentCopy();
        throw _staleSession();
      }
      if (writeError != null) {
        await _clearPersistentCopy();
        if (!_isCurrentIntent(expectedIntent)) {
          await _reconcilePersistentCopy();
          throw _staleSession();
        }
        _publish(pair: null, advanceEpoch: true);
        Error.throwWithStackTrace(writeError, writeStack!);
      }
      _publish(pair: pair, advanceEpoch: true);
    });
  }

  Future<void> _invalidateCurrentSession({
    required int expectedIntent,
    _SessionSnapshot? expectedSnapshot,
  }) async {
    _ensureMutationAuthority(expectedIntent, expectedSnapshot);
    await _serializeMutation(() async {
      _ensureMutationAuthority(expectedIntent, expectedSnapshot);
      Object? clearError;
      StackTrace? clearStack;
      try {
        await _tokenStore.clear();
      } catch (error, stackTrace) {
        clearError = error;
        clearStack = stackTrace;
      }
      if (!_hasMutationAuthority(expectedIntent, expectedSnapshot)) {
        await _reconcilePersistentCopy();
        throw _staleSession();
      }
      if (clearError != null) {
        await _clearPersistentCopy();
        if (!_hasMutationAuthority(expectedIntent, expectedSnapshot)) {
          await _reconcilePersistentCopy();
          throw _staleSession();
        }
      }
      _publish(pair: null, advanceEpoch: true, forceChange: true);
      if (clearError != null) {
        Error.throwWithStackTrace(clearError, clearStack!);
      }
    });
  }

  Future<void> _persistRefresh(
    _SessionContext expected,
    TokenPair refreshed,
  ) async {
    _ensureCurrentContext(expected);
    await _serializeMutation(() async {
      _ensureCurrentContext(expected);
      Object? writeError;
      StackTrace? writeStack;
      try {
        await _tokenStore.write(refreshed);
      } catch (error, stackTrace) {
        writeError = error;
        writeStack = stackTrace;
      }
      if (!_isCurrentContext(expected)) {
        await _reconcilePersistentCopy();
        throw _staleSession();
      }
      if (writeError != null) {
        try {
          await _clearPersistentCopyOrThrow();
        } on _DurableReconciliationFailure {
          _failClosedIfSameAuthority(expected.snapshot);
          rethrow;
        }
        if (!_isCurrentContext(expected)) {
          await _reconcilePersistentCopy();
          throw _staleSession();
        }
        _publish(pair: null, advanceEpoch: true, forceChange: true);
        throw _RefreshPersistenceFailure(writeError, writeStack!);
      }
      _publish(pair: refreshed, advanceEpoch: false, emitChange: false);
    });
  }

  Future<void> _invalidateIfCurrent(_SessionContext expected) async {
    if (!_isCurrentContext(expected)) {
      return;
    }
    await _invalidateCurrentSession(
      expectedIntent: expected.intent,
      expectedSnapshot: expected.snapshot,
    );
  }

  Future<void> _serializeMutation(Future<void> Function() action) {
    _pendingMutations += 1;
    final completer = Completer<void>();
    _mutationTail = _mutationTail.then<void>((_) async {
      try {
        await action();
        completer.complete();
      } catch (error, stackTrace) {
        completer.completeError(error, stackTrace);
      } finally {
        _pendingMutations -= 1;
      }
    });
    return completer.future;
  }

  void _ensureCurrentEpoch(int expectedEpoch) {
    if (_pendingMutations != 0 || _published.epoch != expectedEpoch) {
      throw _staleSession();
    }
  }

  void _ensureSameEpoch(_SessionSnapshot snapshot, int expectedEpoch) {
    if (snapshot.epoch != expectedEpoch) {
      throw _staleSession();
    }
  }

  void _ensureCurrentSnapshot(_SessionSnapshot expected) {
    if (!_isCurrentSnapshot(expected)) {
      throw _staleSession();
    }
  }

  bool _isCurrentSnapshot(_SessionSnapshot expected) =>
      _pendingMutations == 0 && identical(_published, expected);

  void _ensureCurrentIntent(int expectedIntent) {
    if (!_isCurrentIntent(expectedIntent)) {
      throw _staleSession();
    }
  }

  bool _isCurrentIntent(int expectedIntent) =>
      _sessionIntentGeneration == expectedIntent;

  void _ensureCurrentContext(_SessionContext expected) {
    if (!_isCurrentContext(expected)) {
      throw _staleSession();
    }
  }

  bool _isCurrentContext(_SessionContext expected) =>
      _isCurrentIntent(expected.intent) &&
      identical(_published, expected.snapshot);

  void _ensureMutationAuthority(
    int expectedIntent,
    _SessionSnapshot? expectedSnapshot,
  ) {
    if (!_hasMutationAuthority(expectedIntent, expectedSnapshot)) {
      throw _staleSession();
    }
  }

  bool _hasMutationAuthority(
    int expectedIntent,
    _SessionSnapshot? expectedSnapshot,
  ) =>
      _isCurrentIntent(expectedIntent) &&
      (expectedSnapshot == null || identical(_published, expectedSnapshot));

  void _publish({
    required TokenPair? pair,
    required bool advanceEpoch,
    bool emitChange = true,
    bool forceChange = false,
  }) {
    final wasSignedIn = _published.pair != null;
    _published = _SessionSnapshot(
      epoch: advanceEpoch ? _published.epoch + 1 : _published.epoch,
      pair: pair,
    );
    _restored = true;
    final signedIn = pair != null;
    if (emitChange && (forceChange || signedIn != wasSignedIn)) {
      _emitSessionChange(signedIn);
    }
  }

  Future<void> _clearPersistentCopy() async {
    try {
      await _tokenStore.clear();
    } catch (_) {
      // Runtime session remains fail-closed even when secure cleanup fails.
    }
  }

  Future<void> _reconcilePersistentCopy() async {
    final authoritativeSnapshot = _published;
    final authoritative = _published.pair;
    Object? repairError;
    StackTrace? repairStack;
    for (var attempt = 0; attempt < 2; attempt += 1) {
      try {
        if (authoritative == null) {
          await _tokenStore.clear();
        } else {
          await _tokenStore.write(authoritative);
        }
        return;
      } catch (error, stackTrace) {
        repairError = error;
        repairStack = stackTrace;
      }
    }
    try {
      await _tokenStore.clear();
    } catch (error, stackTrace) {
      _failClosedIfSameAuthority(authoritativeSnapshot);
      throw _DurableReconciliationFailure(
        repairError ?? error,
        repairStack ?? stackTrace,
      );
    }
    if (authoritative != null && identical(_published, authoritativeSnapshot)) {
      _publish(pair: null, advanceEpoch: true);
    }
  }

  void _failClosedIfSameAuthority(_SessionSnapshot expected) {
    if (expected.pair != null && identical(_published, expected)) {
      _publish(pair: null, advanceEpoch: true);
    }
  }

  Future<void> _clearPersistentCopyOrThrow() async {
    Object? clearError;
    StackTrace? clearStack;
    for (var attempt = 0; attempt < 2; attempt += 1) {
      try {
        await _tokenStore.clear();
        return;
      } catch (error, stackTrace) {
        clearError = error;
        clearStack = stackTrace;
      }
    }
    throw _DurableReconciliationFailure(clearError!, clearStack!);
  }

  static ApiException _staleSession() =>
      const ApiException(code: 'session_changed', userMessage: '登录状态已变化，请重试');

  static bool _isStaleSession(Object error) =>
      error is ApiException && error.code == 'session_changed';

  void _emitSessionChange(bool signedIn) {
    if (!_disposed) {
      _sessionChanges.add(signedIn);
    }
  }

  Future<void> dispose() async {
    _disposed = true;
    await _sessionChanges.close();
  }

  static void _validatePhone(String phoneE164) {
    if (!_e164.hasMatch(phoneE164)) {
      throw const FormatException('请输入 E.164 格式手机号');
    }
  }

  static Map<String, Object?> _jsonObject(Object? data) {
    if (data case final Map<Object?, Object?> map) {
      return map.map((key, value) => MapEntry(key.toString(), value));
    }
    throw const FormatException('API response must be a JSON object');
  }
}

final class _SessionSnapshot {
  const _SessionSnapshot({required this.epoch, required this.pair});

  final int epoch;
  final TokenPair? pair;
}

final class _SessionContext {
  const _SessionContext({required this.snapshot, required this.intent});

  final _SessionSnapshot snapshot;
  final int intent;
}

final class _RefreshPersistenceFailure implements Exception {
  const _RefreshPersistenceFailure(this.cause, this.stackTrace);

  final Object cause;
  final StackTrace stackTrace;
}

final class _DurableReconciliationFailure implements Exception {
  const _DurableReconciliationFailure(this.cause, this.stackTrace);

  final Object cause;
  final StackTrace stackTrace;
}
