import 'dart:async';
import 'dart:collection';

import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/api_client.dart';
import 'package:photo_organizer/core/api/api_error.dart';
import 'package:photo_organizer/core/api/models.dart';
import 'package:photo_organizer/core/auth/session_repository.dart';
import 'package:photo_organizer/core/auth/token_store.dart';

import '../../helpers/fakes.dart';

const tokenPairJson = <String, Object>{
  'access_token': 'a2',
  'refresh_token': 'r2',
  'expires_in_seconds': 900,
};

void main() {
  test('401 refreshes once and retries the original request', () async {
    final transport = FakeTransport()
      ..enqueue(method: 'GET', path: '/v1/tasks', status: 401)
      ..enqueueJson('/v1/auth/refresh', tokenPairJson, method: 'POST')
      ..enqueueJson('/v1/tasks', <String, Object>{
        'items': <Object>[],
      }, method: 'GET');
    final store = MemoryTokenStore(refreshToken: 'r1');
    final repository = SessionRepository(transport, store);

    final tasks = await repository.authorizedGet('/v1/tasks');

    expect(tasks.statusCode, 200);
    expect(transport.callsTo('/v1/auth/refresh'), 1);
    expect(transport.callsTo('/v1/tasks'), 2);
    expect(transport.calls.last.headers['Authorization'], 'Bearer a2');
  });

  test('concurrent 401 responses share one refresh request', () async {
    final releaseRefresh = Completer<void>();
    final transport = FakeTransport()
      ..enqueue(method: 'GET', path: '/v1/tasks', status: 401)
      ..enqueue(method: 'GET', path: '/v1/tasks', status: 401)
      ..enqueueJson(
        '/v1/auth/refresh',
        tokenPairJson,
        method: 'POST',
        gate: releaseRefresh.future,
      )
      ..enqueueJson('/v1/tasks', <String, Object>{
        'items': <Object>[],
      }, method: 'GET')
      ..enqueueJson('/v1/tasks', <String, Object>{
        'items': <Object>[],
      }, method: 'GET');
    final repository = SessionRepository(
      transport,
      MemoryTokenStore(refreshToken: 'r1'),
    );

    final first = repository.authorizedGet('/v1/tasks');
    final second = repository.authorizedGet('/v1/tasks');
    await Future<void>.delayed(Duration.zero);
    releaseRefresh.complete();

    final responses = await Future.wait(<Future<Object>>[first, second]);
    expect(responses, hasLength(2));
    expect(transport.callsTo('/v1/auth/refresh'), 1);
  });

  test(
    'an intent change does not split one rotating refresh-token flight',
    () async {
      final refreshStarted = Completer<void>();
      final releaseRefresh = Completer<void>();
      final verifyStarted = Completer<void>();
      final releaseVerify = Completer<void>();
      final transport = FakeTransport()
        ..enqueueJson(
          '/v1/auth/refresh',
          const <String, Object>{
            'access_token': 'stale-refreshed-access',
            'refresh_token': 'rotated-refresh',
            'expires_in_seconds': 900,
          },
          method: 'POST',
          called: refreshStarted,
          gate: releaseRefresh.future,
        )
        ..enqueueJson(
          '/v1/auth/verify-code',
          const <String, Object>{
            'access_token': 'user-b-access',
            'refresh_token': 'user-b-refresh',
            'expires_in_seconds': 900,
          },
          method: 'POST',
          called: verifyStarted,
          gate: releaseVerify.future,
        );
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'test-user-a-access',
          refresh: 'rotating-refresh',
          device: 'user-a-device',
        ),
      );
      final repository = SessionRepository(
        transport,
        store,
        createDeviceId: () => 'user-b-device',
      );
      expect(await repository.isSignedIn, isTrue);

      final firstRefreshError = _captureError(repository.refresh());
      await refreshStarted.future;
      final verify = repository.verifyCode(
        phoneE164: '+8613900000000',
        code: '123456',
      );
      await verifyStarted.future;
      final secondRefreshError = _captureError(repository.refresh());
      await _eventQueueCheckpoint();
      final refreshCallsBeforeRelease = transport.callsTo('/v1/auth/refresh');

      releaseRefresh.complete();
      final firstError = await firstRefreshError;
      final secondError = await secondRefreshError;
      final writesBeforeLoginPublication = List<String>.of(
        store.writeAccessTokens,
      );
      final clearsBeforeLoginPublication = store.clearCount;
      releaseVerify.complete();
      await verify;

      expect(refreshCallsBeforeRelease, 1);
      expect(firstError, _sessionChanged());
      expect(secondError, _sessionChanged());
      expect(writesBeforeLoginPublication, isEmpty);
      expect(clearsBeforeLoginPublication, 0);
      expect((await store.read())?.accessToken, 'user-b-access');
    },
  );

  test('an old refresh cleanup cannot remove a newer token flight', () async {
    final oldRefreshStarted = Completer<void>();
    final releaseOldRefresh = Completer<void>();
    final newRefreshStarted = Completer<void>();
    final releaseNewRefresh = Completer<void>();
    final transport = FakeTransport()
      ..enqueueJson(
        '/v1/auth/refresh',
        const <String, Object>{
          'access_token': 'stale-a-access',
          'refresh_token': 'stale-a-rotated',
          'expires_in_seconds': 900,
        },
        method: 'POST',
        called: oldRefreshStarted,
        gate: releaseOldRefresh.future,
      )
      ..enqueueJson('/v1/auth/verify-code', const <String, Object>{
        'access_token': 'user-b-access',
        'refresh_token': 'user-b-refresh',
        'expires_in_seconds': 900,
      }, method: 'POST')
      ..enqueueJson(
        '/v1/auth/refresh',
        const <String, Object>{
          'access_token': 'user-b-refreshed-access',
          'refresh_token': 'user-b-rotated-refresh',
          'expires_in_seconds': 900,
        },
        method: 'POST',
        called: newRefreshStarted,
        gate: releaseNewRefresh.future,
      );
    final store = _TransitionTokenStore(
      initialPair: _tokenPair(
        access: 'test-user-a-access',
        refresh: 'user-a-refresh',
        device: 'user-a-device',
      ),
    );
    final repository = SessionRepository(
      transport,
      store,
      createDeviceId: () => 'user-b-device',
    );
    expect(await repository.isSignedIn, isTrue);

    final oldRefreshError = _captureError(repository.refresh());
    await oldRefreshStarted.future;
    await repository.verifyCode(phoneE164: '+8613900000000', code: '123456');
    final firstNewRefresh = repository.refresh();
    await newRefreshStarted.future;

    releaseOldRefresh.complete();
    final oldError = await oldRefreshError;
    final secondNewRefresh = repository.refresh();
    await _eventQueueCheckpoint();
    final refreshCallsBeforeNewRelease = transport.callsTo('/v1/auth/refresh');
    releaseNewRefresh.complete();
    final refreshed = await Future.wait(<Future<TokenPair>>[
      firstNewRefresh,
      secondNewRefresh,
    ]);

    expect(oldError, _sessionChanged());
    expect(refreshCallsBeforeNewRelease, 2);
    expect(
      refreshed.map((pair) => pair.accessToken),
      everyElement('user-b-refreshed-access'),
    );
    expect((await store.read())?.refreshToken, 'user-b-rotated-refresh');
  });

  test('late 401 from an old token reuses the completed refresh', () async {
    final releaseLateUnauthorized = Completer<void>();
    final transport = FakeTransport()
      ..enqueue(method: 'GET', path: '/v1/tasks', status: 401)
      ..enqueue(
        method: 'GET',
        path: '/v1/tasks',
        status: 401,
        gate: releaseLateUnauthorized.future,
      )
      ..enqueueJson('/v1/auth/refresh', tokenPairJson, method: 'POST')
      ..enqueueJson('/v1/tasks', <String, Object>{
        'items': <Object>[],
      }, method: 'GET')
      ..enqueueJson('/v1/tasks', <String, Object>{
        'items': <Object>[],
      }, method: 'GET');
    final repository = SessionRepository(
      transport,
      MemoryTokenStore(refreshToken: 'r1'),
    );

    final first = repository.authorizedGet('/v1/tasks');
    final late = repository.authorizedGet('/v1/tasks');
    await first;
    releaseLateUnauthorized.complete();
    await late;

    expect(transport.callsTo('/v1/auth/refresh'), 1);
    expect(transport.calls.last.headers['Authorization'], 'Bearer a2');
  });

  test('sign out during refresh cannot restore the old session', () async {
    final refreshStarted = Completer<void>();
    final releaseRefresh = Completer<void>();
    final transport = FakeTransport()
      ..enqueueJson(
        '/v1/auth/refresh',
        tokenPairJson,
        method: 'POST',
        called: refreshStarted,
        gate: releaseRefresh.future,
      );
    final store = MemoryTokenStore(refreshToken: 'r1');
    final repository = SessionRepository(transport, store);

    final refresh = repository.refresh();
    await refreshStarted.future;
    await repository.signOut();
    releaseRefresh.complete();

    await expectLater(
      refresh,
      throwsA(
        isA<ApiException>().having(
          (error) => error.code,
          'code',
          'session_changed',
        ),
      ),
    );
    expect(await store.read(), isNull);
    expect(store.clearCount, 1);
  });

  test(
    'late refresh response cannot outrank sign-out while clear is running',
    () async {
      final refreshStarted = Completer<void>();
      final releaseRefreshResponse = Completer<void>();
      final refreshResponseParsed = Completer<void>();
      final clearStarted = Completer<void>();
      final releaseClear = Completer<void>();
      final transport = FakeTransport()
        ..enqueueJson(
          '/v1/auth/refresh',
          _SignalingJsonMap(const <String, Object?>{
            'access_token': 'late-a-access',
            'refresh_token': 'late-a-refresh',
            'expires_in_seconds': 900,
          }, observed: refreshResponseParsed),
          method: 'POST',
          called: refreshStarted,
          gate: releaseRefreshResponse.future,
        );
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'test-user-a-access',
          refresh: 'user-a-refresh',
          device: 'user-a-device',
        ),
        gatedClearNumber: 1,
        clearStarted: clearStarted,
        releaseClear: releaseClear,
      );
      final repository = SessionRepository(transport, store);
      expect(await repository.isSignedIn, isTrue);
      final changes = <bool>[];
      final subscription = repository.sessionChanges.listen(changes.add);
      addTearDown(subscription.cancel);

      final refresh = repository.refresh();
      final refreshExpectation = expectLater(
        refresh,
        throwsA(_sessionChanged()),
      );
      await refreshStarted.future;
      final signOutExpectation = expectLater(repository.signOut(), completes);
      await clearStarted.future;

      releaseRefreshResponse.complete();
      await refreshResponseParsed.future;
      releaseClear.complete();

      await signOutExpectation;
      await refreshExpectation;
      expect(await repository.isSignedIn, isFalse);
      expect(await store.read(), isNull);
      expect(store.clearCount, 1);
      expect(store.writeAccessTokens, isEmpty);
      expect(changes, <bool>[false]);
    },
  );

  test(
    'late refresh response cannot clear overwrite or publish over newer login',
    () async {
      final refreshStarted = Completer<void>();
      final releaseRefreshResponse = Completer<void>();
      final refreshResponseParsed = Completer<void>();
      final loginWriteStarted = Completer<void>();
      final releaseLoginWrite = Completer<void>();
      final transport = FakeTransport()
        ..enqueueJson(
          '/v1/auth/refresh',
          _SignalingJsonMap(const <String, Object?>{
            'access_token': 'late-a-access',
            'refresh_token': 'late-a-refresh',
            'expires_in_seconds': 900,
          }, observed: refreshResponseParsed),
          method: 'POST',
          called: refreshStarted,
          gate: releaseRefreshResponse.future,
        )
        ..enqueueJson('/v1/auth/verify-code', const <String, Object>{
          'access_token': 'user-b-access',
          'refresh_token': 'user-b-refresh',
          'expires_in_seconds': 900,
        }, method: 'POST')
        ..enqueue(method: 'POST', path: '/v1/tasks/new-task/seal', status: 200);
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'test-user-a-access',
          refresh: 'user-a-refresh',
          device: 'user-a-device',
        ),
        gatedWriteNumber: 1,
        writeStarted: loginWriteStarted,
        releaseWrite: releaseLoginWrite,
      );
      final repository = SessionRepository(
        transport,
        store,
        createDeviceId: () => 'user-b-device',
      );
      expect(await repository.isSignedIn, isTrue);
      final changes = <bool>[];
      final subscription = repository.sessionChanges.listen(changes.add);
      addTearDown(subscription.cancel);

      final refreshExpectation = expectLater(
        repository.refresh(),
        throwsA(_sessionChanged()),
      );
      await refreshStarted.future;
      final loginExpectation = expectLater(
        repository.verifyCode(phoneE164: '+8613900000000', code: '123456'),
        completes,
      );
      await loginWriteStarted.future;

      releaseRefreshResponse.complete();
      await refreshResponseParsed.future;
      releaseLoginWrite.complete();

      await loginExpectation;
      await refreshExpectation;
      final response = await repository.authorizedPost(
        '/v1/tasks/new-task/seal',
      );
      expect(response.statusCode, 200);
      expect(
        transport.calls.last.headers['Authorization'],
        'Bearer user-b-access',
      );
      expect((await store.read())?.accessToken, 'user-b-access');
      expect(store.writeAccessTokens, <String>['user-b-access']);
      expect(store.clearCount, 0);
      expect(changes, isEmpty);
    },
  );

  test(
    'refresh write already started is followed by authoritative sign-out clear',
    () async {
      final refreshWriteStarted = Completer<void>();
      final releaseRefreshWrite = Completer<void>();
      final transport = FakeTransport()
        ..enqueueJson('/v1/auth/refresh', const <String, Object>{
          'access_token': 'written-a-access',
          'refresh_token': 'written-a-refresh',
          'expires_in_seconds': 900,
        }, method: 'POST');
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'test-user-a-access',
          refresh: 'user-a-refresh',
          device: 'user-a-device',
        ),
        gatedWriteNumber: 1,
        writeStarted: refreshWriteStarted,
        releaseWrite: releaseRefreshWrite,
      );
      final repository = SessionRepository(transport, store);
      expect(await repository.isSignedIn, isTrue);
      final changes = <bool>[];
      final subscription = repository.sessionChanges.listen(changes.add);
      addTearDown(subscription.cancel);

      final refreshExpectation = expectLater(
        repository.refresh(),
        throwsA(_sessionChanged()),
      );
      await refreshWriteStarted.future;
      expect((await store.read())?.accessToken, 'written-a-access');
      final signOutExpectation = expectLater(repository.signOut(), completes);

      releaseRefreshWrite.complete();

      await refreshExpectation;
      await signOutExpectation;
      expect(await repository.isSignedIn, isFalse);
      expect(await store.read(), isNull);
      expect(changes, <bool>[false]);
    },
  );

  test(
    'refresh write already started is repaired before newer login persists',
    () async {
      final refreshWriteStarted = Completer<void>();
      final releaseRefreshWrite = Completer<void>();
      final loginStarted = Completer<void>();
      final releaseLoginResponse = Completer<void>();
      final loginResponseParsed = Completer<void>();
      final transport = FakeTransport()
        ..enqueueJson('/v1/auth/refresh', const <String, Object>{
          'access_token': 'written-a-access',
          'refresh_token': 'written-a-refresh',
          'expires_in_seconds': 900,
        }, method: 'POST')
        ..enqueueJson(
          '/v1/auth/verify-code',
          _SignalingJsonMap(const <String, Object?>{
            'access_token': 'user-b-access',
            'refresh_token': 'user-b-refresh',
            'expires_in_seconds': 900,
          }, observed: loginResponseParsed),
          method: 'POST',
          called: loginStarted,
          gate: releaseLoginResponse.future,
        )
        ..enqueue(method: 'POST', path: '/v1/tasks/old-task/seal', status: 200)
        ..enqueue(method: 'POST', path: '/v1/tasks/new-task/seal', status: 200);
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'test-user-a-access',
          refresh: 'user-a-refresh',
          device: 'user-a-device',
        ),
        gatedWriteNumber: 1,
        writeStarted: refreshWriteStarted,
        releaseWrite: releaseRefreshWrite,
      );
      final repository = SessionRepository(
        transport,
        store,
        createDeviceId: () => 'user-b-device',
      );
      expect(await repository.isSignedIn, isTrue);

      final refreshExpectation = expectLater(
        repository.refresh(),
        throwsA(_sessionChanged()),
      );
      await refreshWriteStarted.future;
      final loginExpectation = expectLater(
        repository.verifyCode(phoneE164: '+8613900000000', code: '123456'),
        completes,
      );
      await loginStarted.future;

      releaseRefreshWrite.complete();

      await refreshExpectation;
      final oldLineageResponse = await repository.authorizedPost(
        '/v1/tasks/old-task/seal',
      );
      expect(oldLineageResponse.statusCode, 200);
      expect(
        transport.calls.last.headers['Authorization'],
        'Bearer test-user-a-access',
      );
      releaseLoginResponse.complete();
      await loginResponseParsed.future;
      await loginExpectation;
      final response = await repository.authorizedPost(
        '/v1/tasks/new-task/seal',
      );
      expect(response.statusCode, 200);
      expect(
        transport.calls.last.headers['Authorization'],
        'Bearer user-b-access',
      );
      expect((await store.read())?.accessToken, 'user-b-access');
      expect(store.writeAccessTokens.last, 'user-b-access');
      expect(store.clearCount, 0);
    },
  );

  test(
    'failed stale refresh repair fails closed before newer login error',
    () async {
      final refreshWriteStarted = Completer<void>();
      final releaseRefreshWrite = Completer<void>();
      final loginStarted = Completer<void>();
      final releaseLoginError = Completer<void>();
      final transport = FakeTransport()
        ..enqueueJson('/v1/auth/refresh', const <String, Object>{
          'access_token': 'unrepairable-a-access',
          'refresh_token': 'unrepairable-a-refresh',
          'expires_in_seconds': 900,
        }, method: 'POST')
        ..enqueueJson(
          '/v1/auth/verify-code',
          const <String, Object>{'detail': 'service_unavailable'},
          method: 'POST',
          status: 503,
          called: loginStarted,
          gate: releaseLoginError.future,
        );
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'test-user-a-access',
          refresh: 'user-a-refresh',
          device: 'user-a-device',
        ),
        gatedWriteNumber: 1,
        failingWritesBeforeMutation: const <int>{2, 3},
        writeStarted: refreshWriteStarted,
        releaseWrite: releaseRefreshWrite,
      );
      final repository = SessionRepository(
        transport,
        store,
        createDeviceId: () => 'user-b-device',
      );
      expect(await repository.isSignedIn, isTrue);
      final changes = <bool>[];
      final subscription = repository.sessionChanges.listen(changes.add);
      addTearDown(subscription.cancel);

      final refreshExpectation = expectLater(
        repository.refresh(),
        throwsA(_sessionChanged()),
      );
      await refreshWriteStarted.future;
      final loginExpectation = expectLater(
        repository.verifyCode(phoneE164: '+8613900000000', code: '123456'),
        throwsA(isA<ApiException>()),
      );
      await loginStarted.future;

      releaseRefreshWrite.complete();

      await refreshExpectation;
      final signedInAfterRepair = await repository.isSignedIn;
      final durableAfterRepair = await store.read();
      final clearsAfterRepair = store.clearCount;
      final changesAfterRepair = List<bool>.of(changes);
      releaseLoginError.complete();
      await loginExpectation;

      expect(signedInAfterRepair, isFalse);
      expect(durableAfterRepair, isNull);
      expect(clearsAfterRepair, 1);
      expect(changesAfterRepair, <bool>[false]);
      expect(await repository.isSignedIn, isFalse);
    },
  );

  test(
    'terminal stale refresh repair failure fails closed before newer login',
    () async {
      final refreshWriteStarted = Completer<void>();
      final releaseRefreshWrite = Completer<void>();
      final loginStarted = Completer<void>();
      final releaseLogin = Completer<void>();
      final transport = FakeTransport()
        ..enqueueJson('/v1/auth/refresh', const <String, Object>{
          'access_token': 'unrepairable-a-access',
          'refresh_token': 'unrepairable-a-refresh',
          'expires_in_seconds': 900,
        }, method: 'POST')
        ..enqueueJson(
          '/v1/auth/verify-code',
          const <String, Object>{
            'access_token': 'user-b-access',
            'refresh_token': 'user-b-refresh',
            'expires_in_seconds': 900,
          },
          method: 'POST',
          called: loginStarted,
          gate: releaseLogin.future,
        );
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'test-user-a-access',
          refresh: 'user-a-refresh',
          device: 'user-a-device',
        ),
        gatedWriteNumber: 1,
        failingWritesBeforeMutation: const <int>{2, 3},
        failingClearsBeforeMutation: const <int>{1},
        writeStarted: refreshWriteStarted,
        releaseWrite: releaseRefreshWrite,
      );
      final repository = SessionRepository(
        transport,
        store,
        createDeviceId: () => 'user-b-device',
      );
      expect(await repository.isSignedIn, isTrue);
      final changes = <bool>[];
      final subscription = repository.sessionChanges.listen(changes.add);
      addTearDown(subscription.cancel);

      final refreshError = _captureError(repository.refresh());
      await refreshWriteStarted.future;
      final login = repository.verifyCode(
        phoneE164: '+8613900000000',
        code: '123456',
      );
      await loginStarted.future;

      releaseRefreshWrite.complete();
      final error = await refreshError;
      final signedInAfterFailedRepair = await repository.isSignedIn;
      final durableAfterFailedRepair = await store.read();
      final changesAfterFailedRepair = List<bool>.of(changes);
      releaseLogin.complete();
      await login;

      expect(error, _serviceUnavailable());
      expect(signedInAfterFailedRepair, isFalse);
      expect(durableAfterFailedRepair?.accessToken, 'unrepairable-a-access');
      expect(store.clearCount, 1);
      expect(changesAfterFailedRepair, <bool>[false]);
      expect((await store.read())?.accessToken, 'user-b-access');
      expect(changes, <bool>[false, true]);
    },
  );

  test('old user 401 cannot replay a POST with a new user session', () async {
    final oldRequestStarted = Completer<void>();
    final releaseOldUnauthorized = Completer<void>();
    final transport = FakeTransport()
      ..enqueue(
        method: 'POST',
        path: '/v1/tasks/old-task/seal',
        status: 401,
        called: oldRequestStarted,
        gate: releaseOldUnauthorized.future,
      )
      ..enqueueJson('/v1/auth/verify-code', <String, Object>{
        'access_token': 'user-b-access',
        'refresh_token': 'user-b-refresh',
        'expires_in_seconds': 900,
      }, method: 'POST');
    final store = MemoryTokenStore(
      accessToken: 'test-user-a-access',
      refreshToken: 'user-a-refresh',
      deviceId: 'user-a-device',
    );
    final repository = SessionRepository(
      transport,
      store,
      createDeviceId: () => 'user-b-device',
    );

    final oldPost = repository.authorizedPost(
      '/v1/tasks/old-task/seal',
      data: const <String, Object>{'confirm': true},
    );
    await oldRequestStarted.future;
    await repository.verifyCode(phoneE164: '+8613900000000', code: '123456');
    releaseOldUnauthorized.complete();

    await expectLater(
      oldPost,
      throwsA(
        isA<ApiException>().having(
          (error) => error.code,
          'code',
          'session_changed',
        ),
      ),
    );
    expect(transport.callsTo('/v1/tasks/old-task/seal'), 1);
    final current = await store.read();
    expect(current?.accessToken, 'user-b-access');
    expect(current?.deviceId, 'user-b-device');
  });

  test(
    'account switch during refresh publication cannot dispatch the old POST',
    () async {
      final refreshWriteStarted = Completer<void>();
      final releaseRefreshWrite = Completer<void>();
      final transport = FakeTransport()
        ..enqueue(method: 'POST', path: '/v1/tasks/old-task/seal', status: 401)
        ..enqueueJson('/v1/auth/refresh', tokenPairJson, method: 'POST')
        ..enqueueJson('/v1/auth/verify-code', const <String, Object>{
          'access_token': 'user-b-access',
          'refresh_token': 'user-b-refresh',
          'expires_in_seconds': 900,
        }, method: 'POST');
      final store = _TransitionTokenStore(
        initialPair: TokenPair(
          accessToken: 'test-user-a-access',
          refreshToken: 'user-a-refresh',
          expiresAt: DateTime.utc(2030),
          deviceId: 'user-a-device',
        ),
        gatedWriteNumber: 1,
        writeStarted: refreshWriteStarted,
        releaseWrite: releaseRefreshWrite,
      );
      final repository = SessionRepository(
        transport,
        store,
        createDeviceId: () => 'user-b-device',
      );

      final oldPost = repository.authorizedPost(
        '/v1/tasks/old-task/seal',
        data: const <String, Object>{'confirm': true},
      );
      final oldPostExpectation = expectLater(
        oldPost,
        throwsA(
          isA<ApiException>().having(
            (error) => error.code,
            'code',
            'session_changed',
          ),
        ),
      );
      await refreshWriteStarted.future;
      final newLogin = repository.verifyCode(
        phoneE164: '+8613900000000',
        code: '123456',
      );
      await _eventQueueCheckpoint();
      releaseRefreshWrite.complete();
      await newLogin;
      await oldPostExpectation;
      expect(transport.callsTo('/v1/tasks/old-task/seal'), 1);
      expect(
        transport.calls
            .where((call) => call.path == '/v1/tasks/old-task/seal')
            .every(
              (call) => call.headers['Authorization'] != 'Bearer user-b-access',
            ),
        isTrue,
      );
    },
  );

  test('sign out during verify prevents the late login response', () async {
    final verifyStarted = Completer<void>();
    final releaseVerify = Completer<void>();
    final transport = FakeTransport()
      ..enqueueJson(
        '/v1/auth/verify-code',
        const <String, Object>{
          'access_token': 'late-access',
          'refresh_token': 'late-refresh',
          'expires_in_seconds': 900,
        },
        method: 'POST',
        called: verifyStarted,
        gate: releaseVerify.future,
      );
    final store = MemoryTokenStore();
    final repository = SessionRepository(
      transport,
      store,
      createDeviceId: () => 'late-device',
    );
    final changes = <bool>[];
    final subscription = repository.sessionChanges.listen(changes.add);
    addTearDown(subscription.cancel);

    final verify = repository.verifyCode(
      phoneE164: '+8613900000000',
      code: '123456',
    );
    await verifyStarted.future;
    await repository.signOut();
    releaseVerify.complete();

    await expectLater(
      verify,
      throwsA(
        isA<ApiException>().having(
          (error) => error.code,
          'code',
          'session_changed',
        ),
      ),
    );
    expect(await store.read(), isNull);
    expect(changes, <bool>[false]);
  });

  test('a newer verify attempt wins over an older late response', () async {
    final oldVerifyStarted = Completer<void>();
    final releaseOldVerify = Completer<void>();
    final transport = FakeTransport()
      ..enqueueJson(
        '/v1/auth/verify-code',
        const <String, Object>{
          'access_token': 'old-access',
          'refresh_token': 'old-refresh',
          'expires_in_seconds': 900,
        },
        method: 'POST',
        called: oldVerifyStarted,
        gate: releaseOldVerify.future,
      )
      ..enqueueJson('/v1/auth/verify-code', const <String, Object>{
        'access_token': 'new-access',
        'refresh_token': 'new-refresh',
        'expires_in_seconds': 900,
      }, method: 'POST');
    final store = MemoryTokenStore();
    var deviceSequence = 0;
    final repository = SessionRepository(
      transport,
      store,
      createDeviceId: () => 'device-${++deviceSequence}',
    );

    final oldVerify = repository.verifyCode(
      phoneE164: '+8613900000000',
      code: '111111',
    );
    await oldVerifyStarted.future;
    await repository.verifyCode(phoneE164: '+8613900000000', code: '222222');
    releaseOldVerify.complete();

    await expectLater(
      oldVerify,
      throwsA(
        isA<ApiException>().having(
          (error) => error.code,
          'code',
          'session_changed',
        ),
      ),
    );
    final current = await store.read();
    expect(current?.accessToken, 'new-access');
    expect(current?.refreshToken, 'new-refresh');
    expect(current?.deviceId, 'device-2');
  });

  test(
    'stale cold restore and queued verify remain live for newest verify',
    () async {
      final restoreReadStarted = Completer<void>();
      final releaseRestoreRead = Completer<void>();
      final firstLoginResponseParsed = Completer<void>();
      final secondLoginStarted = Completer<void>();
      final releaseSecondLogin = Completer<void>();
      final transport = FakeTransport()
        ..enqueueJson(
          '/v1/auth/verify-code',
          _SignalingJsonMap(const <String, Object?>{
            'access_token': 'stale-b-access',
            'refresh_token': 'stale-b-refresh',
            'expires_in_seconds': 900,
          }, observed: firstLoginResponseParsed),
          method: 'POST',
        )
        ..enqueueJson(
          '/v1/auth/verify-code',
          const <String, Object>{
            'access_token': 'current-c-access',
            'refresh_token': 'current-c-refresh',
            'expires_in_seconds': 900,
          },
          method: 'POST',
          called: secondLoginStarted,
          gate: releaseSecondLogin.future,
        );
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'restored-a-access',
          refresh: 'restored-a-refresh',
          device: 'restored-a-device',
        ),
        gatedReadNumber: 1,
        readStarted: restoreReadStarted,
        releaseRead: releaseRestoreRead,
      );
      var deviceSequence = 0;
      final repository = SessionRepository(
        transport,
        store,
        createDeviceId: () => 'device-${++deviceSequence}',
      );
      final changes = <bool>[];
      final subscription = repository.sessionChanges.listen(changes.add);
      addTearDown(subscription.cancel);

      final initialSnapshot = repository.isSignedIn;
      await restoreReadStarted.future;
      final firstLoginExpectation = expectLater(
        repository.verifyCode(phoneE164: '+8613900000000', code: '111111'),
        throwsA(_sessionChanged()),
      );
      await firstLoginResponseParsed.future;
      final secondLogin = repository.verifyCode(
        phoneE164: '+8613900000000',
        code: '222222',
      );
      final secondLoginExpectation = expectLater(secondLogin, completes);
      await secondLoginStarted.future;
      final heartbeat = Completer<void>();
      Timer.run(heartbeat.complete);

      releaseRestoreRead.complete();

      expect(await initialSnapshot, isFalse);
      await heartbeat.future.timeout(const Duration(seconds: 2));
      await firstLoginExpectation;
      releaseSecondLogin.complete();
      await secondLoginExpectation;
      expect(await repository.isSignedIn, isTrue);
      expect((await store.read())?.accessToken, 'current-c-access');
      expect(changes, <bool>[true]);
    },
    timeout: const Timeout(Duration(seconds: 5)),
  );

  test('restore read failure establishes fail-closed authority', () async {
    final store = _TransitionTokenStore(
      initialPair: _tokenPair(
        access: 'unreadable-access',
        refresh: 'unreadable-refresh',
        device: 'unreadable-device',
      ),
      failingReadNumber: 1,
    );
    final repository = SessionRepository(FakeTransport(), store);

    expect(await repository.isSignedIn, isFalse);
    expect(await store.read(), isNull);
    expect(store.clearCount, 1);
  });

  test(
    'authorized POST waits for replacement publication before a success',
    () async {
      final writeStarted = Completer<void>();
      final releaseWrite = Completer<void>();
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'test-user-a-access',
          refresh: 'user-a-refresh',
          device: 'user-a-device',
        ),
        gatedWriteNumber: 1,
        writeStarted: writeStarted,
        releaseWrite: releaseWrite,
      );
      final transport = FakeTransport()
        ..enqueueJson('/v1/auth/verify-code', const <String, Object>{
          'access_token': 'user-b-access',
          'refresh_token': 'user-b-refresh',
          'expires_in_seconds': 900,
        }, method: 'POST')
        ..enqueue(method: 'POST', path: '/v1/tasks/new-task/seal', status: 200);
      final repository = SessionRepository(
        transport,
        store,
        createDeviceId: () => 'user-b-device',
      );
      expect(await repository.isSignedIn, isTrue);

      final verify = repository.verifyCode(
        phoneE164: '+8613900000000',
        code: '123456',
      );
      await writeStarted.future;
      var requestCompleted = false;
      final request = repository.authorizedPost('/v1/tasks/new-task/seal');
      request.then<void>(
        (_) => requestCompleted = true,
        onError: (_, _) => requestCompleted = true,
      );
      await _eventQueueCheckpoint();
      final callsBeforePublication = transport.callsTo(
        '/v1/tasks/new-task/seal',
      );
      final completedBeforePublication = requestCompleted;

      releaseWrite.complete();
      await verify;
      final response = await request;

      expect(callsBeforePublication, 0);
      expect(completedBeforePublication, isFalse);
      expect(response.statusCode, 200);
      expect(
        transport.calls.last.headers['Authorization'],
        'Bearer user-b-access',
      );
    },
  );

  test(
    '401 during replacement cannot retry an old POST with the new account',
    () async {
      final writeStarted = Completer<void>();
      final releaseWrite = Completer<void>();
      final unauthorizedDispatched = Completer<void>();
      final releaseUnauthorized = Completer<void>();
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'test-user-a-access',
          refresh: 'user-a-refresh',
          device: 'user-a-device',
        ),
        gatedWriteNumber: 1,
        writeStarted: writeStarted,
        releaseWrite: releaseWrite,
      );
      final transport = FakeTransport()
        ..enqueueJson('/v1/auth/verify-code', const <String, Object>{
          'access_token': 'user-b-access',
          'refresh_token': 'user-b-refresh',
          'expires_in_seconds': 900,
        }, method: 'POST')
        ..enqueue(
          method: 'POST',
          path: '/v1/tasks/new-task/seal',
          status: 401,
          called: unauthorizedDispatched,
          gate: releaseUnauthorized.future,
        )
        ..enqueueJson('/v1/auth/refresh', const <String, Object>{
          'access_token': 'user-b-refreshed',
          'refresh_token': 'user-b-refresh-2',
          'expires_in_seconds': 900,
        }, method: 'POST')
        ..enqueue(method: 'POST', path: '/v1/tasks/new-task/seal', status: 200);
      final repository = SessionRepository(
        transport,
        store,
        createDeviceId: () => 'user-b-device',
      );
      expect(await repository.isSignedIn, isTrue);

      final verify = repository.verifyCode(
        phoneE164: '+8613900000000',
        code: '123456',
      );
      await writeStarted.future;
      final request = repository.authorizedPost('/v1/tasks/new-task/seal');
      await _eventQueueCheckpoint();
      final callsBeforePublication = transport.callsTo(
        '/v1/tasks/new-task/seal',
      );

      releaseWrite.complete();
      await verify;
      await unauthorizedDispatched.future;
      releaseUnauthorized.complete();
      ApiResponse? response;
      Object? requestError;
      try {
        response = await request;
      } catch (error) {
        requestError = error;
      }

      expect(callsBeforePublication, 0);
      expect(requestError, isNull);
      expect(response?.statusCode, 200);
      final protectedCalls = transport.calls
          .where((call) => call.path == '/v1/tasks/new-task/seal')
          .toList(growable: false);
      expect(protectedCalls, hasLength(2));
      expect(
        protectedCalls.map((call) => call.headers['Authorization']),
        <String>['Bearer user-b-access', 'Bearer user-b-refreshed'],
      );
    },
  );

  test('replacement write failure never publishes a partial session', () async {
    final writeStarted = Completer<void>();
    final releaseWrite = Completer<void>();
    final store = _TransitionTokenStore(
      initialPair: _tokenPair(
        access: 'test-user-a-access',
        refresh: 'user-a-refresh',
        device: 'user-a-device',
      ),
      gatedWriteNumber: 1,
      failingWriteNumber: 1,
      writeStarted: writeStarted,
      releaseWrite: releaseWrite,
    );
    final transport = FakeTransport()
      ..enqueueJson('/v1/auth/verify-code', const <String, Object>{
        'access_token': 'partial-b-access',
        'refresh_token': 'partial-b-refresh',
        'expires_in_seconds': 900,
      }, method: 'POST');
    final repository = SessionRepository(
      transport,
      store,
      createDeviceId: () => 'partial-b-device',
    );
    expect(await repository.isSignedIn, isTrue);
    final changes = <bool>[];
    final subscription = repository.sessionChanges.listen(changes.add);
    addTearDown(subscription.cancel);

    final verify = repository.verifyCode(
      phoneE164: '+8613900000000',
      code: '123456',
    );
    await writeStarted.future;
    var snapshotCompleted = false;
    bool? signedInDuringTransition;
    final snapshot = repository.isSignedIn.then((value) {
      signedInDuringTransition = value;
      snapshotCompleted = true;
    });
    await _eventQueueCheckpoint();
    final completedBeforePublication = snapshotCompleted;

    releaseWrite.complete();
    await expectLater(verify, throwsA(isA<StateError>()));
    await snapshot;

    expect(completedBeforePublication, isFalse);
    expect(signedInDuringTransition, isFalse);
    expect(await repository.isSignedIn, isFalse);
    expect(await store.read(), isNull);
    expect(changes, <bool>[false]);
  });

  test(
    'terminal refresh cleanup failure still publishes fail-closed runtime',
    () async {
      final oldRequestStarted = Completer<void>();
      final releaseOldRequest = Completer<void>();
      final transport = FakeTransport()
        ..enqueue(
          method: 'GET',
          path: '/v1/tasks',
          status: 200,
          called: oldRequestStarted,
          gate: releaseOldRequest.future,
        )
        ..enqueueJson('/v1/auth/refresh', const <String, Object>{
          'access_token': 'partial-refresh-access',
          'refresh_token': 'partial-refresh-token',
          'expires_in_seconds': 900,
        }, method: 'POST');
      final store = _TransitionTokenStore(
        initialPair: _tokenPair(
          access: 'test-user-a-access',
          refresh: 'user-a-refresh',
          device: 'user-a-device',
        ),
        failingWriteNumber: 1,
        failingClearsBeforeMutation: const <int>{1, 2},
      );
      final repository = SessionRepository(transport, store);
      expect(await repository.isSignedIn, isTrue);
      final changes = <bool>[];
      final subscription = repository.sessionChanges.listen(changes.add);
      addTearDown(subscription.cancel);

      final oldRequestError = _captureError(
        repository.authorizedGet('/v1/tasks'),
      );
      await oldRequestStarted.future;
      final refreshError = await _captureError(repository.refresh());
      final signedInAfterFailure = await repository.isSignedIn;
      final durableAfterFailure = await store.read();
      final clearCountAfterFailure = store.clearCount;
      final changesAfterFailure = List<bool>.of(changes);
      releaseOldRequest.complete();
      final oldError = await oldRequestError;

      expect(refreshError, _serviceUnavailable());
      expect(signedInAfterFailure, isFalse);
      expect(durableAfterFailure?.accessToken, 'partial-refresh-access');
      expect(clearCountAfterFailure, 2);
      expect(changesAfterFailure, <bool>[false]);
      expect(oldError, _sessionChanged());
    },
  );

  test('authorized POST waits for atomic sign-out publication', () async {
    final clearStarted = Completer<void>();
    final releaseClear = Completer<void>();
    final store = _TransitionTokenStore(
      initialPair: _tokenPair(
        access: 'test-user-a-access',
        refresh: 'user-a-refresh',
        device: 'user-a-device',
      ),
      gatedClearNumber: 1,
      clearStarted: clearStarted,
      releaseClear: releaseClear,
    );
    final transport = FakeTransport()
      ..enqueue(method: 'POST', path: '/v1/tasks/old-task/seal', status: 200);
    final repository = SessionRepository(transport, store);
    expect(await repository.isSignedIn, isTrue);
    final changes = <bool>[];
    final subscription = repository.sessionChanges.listen(changes.add);
    addTearDown(subscription.cancel);

    final signOut = repository.signOut();
    await clearStarted.future;
    final request = repository.authorizedPost('/v1/tasks/old-task/seal');
    await _eventQueueCheckpoint();
    final callsBeforePublication = transport.callsTo('/v1/tasks/old-task/seal');

    releaseClear.complete();
    await signOut;
    final response = await request;

    expect(callsBeforePublication, 0);
    expect(response.statusCode, 200);
    expect(transport.calls.single.headers['Authorization'], isNull);
    expect(await repository.isSignedIn, isFalse);
    expect(changes, <bool>[false]);
  });

  test('auth routes are never automatically refreshed or retried', () async {
    final transport = FakeTransport()
      ..enqueue(method: 'POST', path: '/v1/auth/verify-code', status: 401);
    final repository = SessionRepository(
      transport,
      MemoryTokenStore(refreshToken: 'r1'),
    );

    final response = await repository.authorizedPost(
      '/v1/auth/verify-code',
      data: const <String, Object>{'code': '123456'},
    );

    expect(response.statusCode, 401);
    expect(transport.callsTo('/v1/auth/verify-code'), 1);
    expect(transport.callsTo('/v1/auth/refresh'), 0);
  });

  test('refresh failure clears the session', () async {
    final transport = FakeTransport()
      ..enqueue(method: 'GET', path: '/v1/tasks', status: 401)
      ..enqueueJson(
        '/v1/auth/refresh',
        const <String, Object>{'detail': 'invalid_refresh_token'},
        method: 'POST',
        status: 401,
      );
    final store = MemoryTokenStore(refreshToken: 'r1');
    final repository = SessionRepository(transport, store);

    await expectLater(
      repository.authorizedGet('/v1/tasks'),
      throwsA(isA<ApiException>()),
    );

    expect(await store.read(), isNull);
    expect(store.clearCount, 1);
    expect(transport.callsTo('/v1/tasks'), 1);
  });

  test('rejects non-E.164 phone numbers before transport', () async {
    final transport = FakeTransport();
    final repository = SessionRepository(transport, MemoryTokenStore());

    await expectLater(
      repository.requestCode(phoneE164: '13800138000', inviteCode: 'invite'),
      throwsA(isA<FormatException>()),
    );

    expect(transport.calls, isEmpty);
  });

  test('rejects OTP values that are not exactly six digits', () async {
    final transport = FakeTransport();
    final repository = SessionRepository(transport, MemoryTokenStore());

    await expectLater(
      repository.verifyCode(phoneE164: '+8613800000000', code: '12345'),
      throwsA(isA<FormatException>()),
    );

    expect(transport.calls, isEmpty);
  });

  test('rejects invite codes outside 1 to 128 trimmed characters', () async {
    for (final inviteCode in <String>[
      '   ',
      List<String>.filled(129, 'a').join(),
    ]) {
      final transport = FakeTransport();
      final repository = SessionRepository(transport, MemoryTokenStore());

      await expectLater(
        repository.requestCode(
          phoneE164: '+8613800000000',
          inviteCode: inviteCode,
        ),
        throwsA(
          isA<FormatException>().having(
            (error) => error.message,
            'message',
            '邀请码无效',
          ),
        ),
      );
      expect(transport.calls, isEmpty);
    }
  });

  test('accepts and trims a 128-character invite boundary', () async {
    final transport = FakeTransport()
      ..enqueue(method: 'POST', path: '/v1/auth/request-code', status: 202);
    final repository = SessionRepository(transport, MemoryTokenStore());
    final boundary = List<String>.filled(128, 'a').join();

    await repository.requestCode(
      phoneE164: '+8613800000000',
      inviteCode: ' $boundary ',
    );

    expect(transport.calls, hasLength(1));
    expect(transport.calls.single.data, <String, Object>{
      'phone_e164': '+8613800000000',
      'invite_code': boundary,
    });
  });
}

final class _TransitionTokenStore implements TokenStore {
  _TransitionTokenStore({
    TokenPair? initialPair,
    this.gatedReadNumber,
    this.failingReadNumber,
    this.readStarted,
    this.releaseRead,
    this.gatedWriteNumber,
    this.failingWriteNumber,
    this.failingWritesBeforeMutation = const <int>{},
    this.writeStarted,
    this.releaseWrite,
    this.gatedClearNumber,
    this.failingClearsBeforeMutation = const <int>{},
    this.clearStarted,
    this.releaseClear,
  }) : _pair = initialPair;

  final int? gatedReadNumber;
  final int? failingReadNumber;
  final Completer<void>? readStarted;
  final Completer<void>? releaseRead;
  final int? gatedWriteNumber;
  final int? failingWriteNumber;
  final Set<int> failingWritesBeforeMutation;
  final Completer<void>? writeStarted;
  final Completer<void>? releaseWrite;
  final int? gatedClearNumber;
  final Set<int> failingClearsBeforeMutation;
  final Completer<void>? clearStarted;
  final Completer<void>? releaseClear;
  final List<String> writeAccessTokens = <String>[];
  TokenPair? _pair;
  int _readCount = 0;
  int _writeCount = 0;
  int _clearCount = 0;

  int get clearCount => _clearCount;

  @override
  Future<void> clear() async {
    _clearCount += 1;
    if (failingClearsBeforeMutation.contains(_clearCount)) {
      throw StateError('secure-store clear failed before mutation');
    }
    _pair = null;
    if (_clearCount == gatedClearNumber) {
      clearStarted?.complete();
      await releaseClear?.future;
    }
  }

  @override
  Future<TokenPair?> read() async {
    _readCount += 1;
    final snapshot = _pair;
    if (_readCount == gatedReadNumber) {
      readStarted?.complete();
      await releaseRead?.future;
    }
    if (_readCount == failingReadNumber) {
      throw StateError('secure-store read failed');
    }
    return snapshot;
  }

  @override
  Future<void> write(TokenPair pair) async {
    _writeCount += 1;
    if (failingWritesBeforeMutation.contains(_writeCount)) {
      throw StateError('secure-store write failed before mutation');
    }
    writeAccessTokens.add(pair.accessToken);
    _pair = pair;
    if (_writeCount == gatedWriteNumber) {
      writeStarted?.complete();
      await releaseWrite?.future;
    }
    if (_writeCount == failingWriteNumber) {
      throw StateError('partial secure-store write');
    }
  }
}

TokenPair _tokenPair({
  required String access,
  required String refresh,
  required String device,
}) => TokenPair(
  accessToken: access,
  refreshToken: refresh,
  expiresAt: DateTime.utc(2030),
  deviceId: device,
);

Future<void> _eventQueueCheckpoint() => Future<void>(() {});

Future<Object?> _captureError(Future<Object?> future) async {
  try {
    await future;
    return null;
  } catch (error) {
    return error;
  }
}

Matcher _sessionChanged() => isA<ApiException>().having(
  (error) => error.code,
  'code',
  'session_changed',
);

Matcher _serviceUnavailable() => isA<ApiException>().having(
  (error) => error.code,
  'code',
  'service_unavailable',
);

final class _SignalingJsonMap extends MapBase<String, Object?> {
  _SignalingJsonMap(Map<String, Object?> values, {required this.observed})
    : _values = Map<String, Object?>.of(values);

  final Map<String, Object?> _values;
  final Completer<void> observed;

  @override
  Object? operator [](Object? key) {
    if (key == 'expires_in_seconds' && !observed.isCompleted) {
      observed.complete();
    }
    return _values[key];
  }

  @override
  void operator []=(String key, Object? value) {
    _values[key] = value;
  }

  @override
  void clear() => _values.clear();

  @override
  Iterable<String> get keys => _values.keys;

  @override
  Object? remove(Object? key) => _values.remove(key);
}
