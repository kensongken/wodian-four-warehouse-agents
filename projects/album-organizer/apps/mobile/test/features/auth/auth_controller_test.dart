import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/models.dart';
import 'package:photo_organizer/core/auth/session_repository.dart';
import 'package:photo_organizer/core/auth/token_store.dart';
import 'package:photo_organizer/features/auth/auth_controller.dart';

import '../../helpers/fakes.dart';

void main() {
  test('runtime session invalidation clears pending navigation', () async {
    final repository = SessionRepository(
      FakeTransport(),
      MemoryTokenStore(refreshToken: 'refresh-token'),
    );
    var pendingNavigationCleared = false;
    final controller = AuthController(
      repository,
      onSessionInvalidated: () => pendingNavigationCleared = true,
    );
    addTearDown(controller.dispose);

    await repository.signOut();

    expect(pendingNavigationCleared, isTrue);
    expect(controller.state.signedIn, isFalse);
  });

  test(
    'a late restore cannot overwrite runtime session invalidation',
    () async {
      final readStarted = Completer<void>();
      final releaseRead = Completer<void>();
      final store = _SnapshotReadTokenStore(
        readStarted: readStarted,
        releaseRead: releaseRead,
      );
      final repository = SessionRepository(FakeTransport(), store);
      final controller = AuthController(repository);
      addTearDown(controller.dispose);

      await readStarted.future;
      final signOut = repository.signOut();
      releaseRead.complete();
      await signOut;
      await Future<void>.delayed(Duration.zero);

      expect(controller.state.initialized, isTrue);
      expect(controller.state.signedIn, isFalse);
    },
  );
}

final class _SnapshotReadTokenStore implements TokenStore {
  _SnapshotReadTokenStore({
    required this.readStarted,
    required this.releaseRead,
  });

  final Completer<void> readStarted;
  final Completer<void> releaseRead;
  TokenPair? _pair = TokenPair(
    accessToken: 'test-old-access',
    refreshToken: 'old-refresh',
    expiresAt: DateTime.utc(2030),
    deviceId: 'old-device',
  );

  @override
  Future<void> clear() async {
    _pair = null;
  }

  @override
  Future<TokenPair?> read() async {
    final snapshot = _pair;
    readStarted.complete();
    await releaseRead.future;
    return snapshot;
  }

  @override
  Future<void> write(TokenPair pair) async {
    _pair = pair;
  }
}
