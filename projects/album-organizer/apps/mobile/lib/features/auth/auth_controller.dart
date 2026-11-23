import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_error.dart';
import '../../core/auth/pending_navigation.dart';
import '../../core/auth/session_repository.dart';

final authControllerProvider = StateNotifierProvider<AuthController, AuthState>(
  (ref) => AuthController(
    ref.watch(sessionRepositoryProvider),
    onSessionInvalidated: ref.watch(pendingNavigationProvider).clear,
  ),
);

final class AuthState {
  const AuthState({
    this.initialized = false,
    this.signedIn = false,
    this.busy = false,
    this.phoneE164,
    this.errorMessage,
  });

  final bool initialized;
  final bool signedIn;
  final bool busy;
  final String? phoneE164;
  final String? errorMessage;

  AuthState copyWith({
    bool? initialized,
    bool? signedIn,
    bool? busy,
    String? phoneE164,
    String? errorMessage,
    bool clearError = false,
  }) => AuthState(
    initialized: initialized ?? this.initialized,
    signedIn: signedIn ?? this.signedIn,
    busy: busy ?? this.busy,
    phoneE164: phoneE164 ?? this.phoneE164,
    errorMessage: clearError ? null : errorMessage ?? this.errorMessage,
  );
}

final class AuthController extends StateNotifier<AuthState> {
  AuthController(this._repository, {void Function()? onSessionInvalidated})
    : _onSessionInvalidated = onSessionInvalidated ?? _noop,
      super(const AuthState()) {
    _sessionSubscription = _repository.sessionChanges.listen(
      _handleSessionChange,
    );
    Future<void>.microtask(_restore);
  }

  final SessionRepository _repository;
  final void Function() _onSessionInvalidated;
  late final StreamSubscription<bool> _sessionSubscription;
  int _sessionEventGeneration = 0;

  void _handleSessionChange(bool signedIn) {
    _sessionEventGeneration += 1;
    if (!signedIn) {
      _onSessionInvalidated();
    }
    if (!mounted) {
      return;
    }
    state = signedIn
        ? state.copyWith(
            initialized: true,
            signedIn: true,
            busy: false,
            clearError: true,
          )
        : const AuthState(initialized: true);
  }

  Future<void> _restore() async {
    final startingGeneration = _sessionEventGeneration;
    final signedIn = await _repository.isSignedIn;
    if (mounted && startingGeneration == _sessionEventGeneration) {
      state = state.copyWith(initialized: true, signedIn: signedIn);
    }
  }

  Future<bool> requestCode({
    required String phoneE164,
    required String inviteCode,
  }) async {
    state = state.copyWith(busy: true, clearError: true);
    try {
      await _repository.requestCode(
        phoneE164: phoneE164.trim(),
        inviteCode: inviteCode,
      );
      state = state.copyWith(
        initialized: true,
        busy: false,
        phoneE164: phoneE164.trim(),
        clearError: true,
      );
      return true;
    } catch (error) {
      state = state.copyWith(
        initialized: true,
        busy: false,
        errorMessage: _userMessage(error),
      );
      return false;
    }
  }

  Future<bool> verifyCode({
    required String phoneE164,
    required String code,
  }) async {
    state = state.copyWith(busy: true, clearError: true);
    try {
      await _repository.verifyCode(phoneE164: phoneE164, code: code);
      return true;
    } catch (error) {
      state = state.copyWith(
        initialized: true,
        busy: false,
        errorMessage: _userMessage(error),
      );
      return false;
    }
  }

  Future<void> signOut() async {
    await _repository.signOut();
  }

  @override
  void dispose() {
    _sessionSubscription.cancel();
    super.dispose();
  }

  static String _userMessage(Object error) {
    if (error is ApiException) {
      return error.userMessage;
    }
    if (error is FormatException) {
      return error.message.toString();
    }
    return '服务暂时不可用，请稍后重试';
  }

  static void _noop() {}
}
