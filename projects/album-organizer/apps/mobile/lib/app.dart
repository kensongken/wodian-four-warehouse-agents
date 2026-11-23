import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import 'core/auth/pending_navigation.dart';
import 'core/upload/upload_runtime.dart';
import 'features/auth/auth_controller.dart';
import 'features/auth/login_screen.dart';
import 'features/auth/otp_screen.dart';
import 'features/classification_reviews/classification_review_detail_screen.dart';
import 'features/classification_reviews/classification_review_list_screen.dart';
import 'features/tasks/task_list_screen.dart';
import 'features/tasks/import_task_screen.dart';
import 'features/tasks/task_progress_screen.dart';

final _routerRefreshProvider = Provider<_RouterRefresh>((ref) {
  final refresh = _RouterRefresh();
  ref
    ..listen<AuthState>(authControllerProvider, (_, _) => refresh.notify())
    ..onDispose(refresh.dispose);
  return refresh;
});

final _routerProvider = Provider.family.autoDispose<GoRouter, String>((
  ref,
  initialLocation,
) {
  final refresh = ref.watch(_routerRefreshProvider);
  final pendingNavigation = ref.watch(pendingNavigationProvider);
  final router = GoRouter(
    initialLocation: initialLocation,
    refreshListenable: refresh,
    onException: (_, state, router) {
      final auth = ref.read(authControllerProvider);
      if (!auth.initialized) {
        if (PendingNavigation.validate(state.uri) != null) {
          pendingNavigation.capture(state.uri);
        }
        router.go('/');
        return;
      }
      pendingNavigation.clear();
      router.go(auth.signedIn ? '/tasks' : '/login');
    },
    redirect: (context, state) {
      final auth = ref.read(authControllerProvider);
      final location = state.matchedLocation;
      final validatedDestination = PendingNavigation.validate(state.uri);
      if ((_isTaskDetailRoute(state.uri) && validatedDestination == null) ||
          (_isReviewRoute(state.uri) && !_isValidReviewRoute(state.uri))) {
        pendingNavigation.clear();
        if (!auth.initialized) {
          return '/';
        }
        return auth.signedIn ? '/tasks' : '/login';
      }
      if (!auth.initialized) {
        if (location != '/' && validatedDestination != null) {
          pendingNavigation.capture(state.uri);
        }
        return location == '/' ? null : '/';
      }
      if (auth.signedIn) {
        final destination = pendingNavigation.peek();
        if (destination != null) {
          if (pendingNavigation.consumeIfReached(state.uri)) {
            return null;
          } else if (location == '/' ||
              location == '/login' ||
              location == '/verify') {
            return destination;
          }
        }
        return location == '/' || location == '/login' || location == '/verify'
            ? '/tasks'
            : null;
      }
      if (location == '/login') {
        return null;
      }
      if (location == '/verify' && auth.phoneE164 != null) {
        return null;
      }
      return '/login';
    },
    routes: <RouteBase>[
      GoRoute(
        path: '/',
        builder: (_, _) =>
            const Scaffold(body: Center(child: Text('AI 商品相册整理助手'))),
      ),
      GoRoute(path: '/login', builder: (_, _) => const LoginScreen()),
      GoRoute(
        path: '/verify',
        builder: (_, _) {
          final phone = ref.read(authControllerProvider).phoneE164;
          return phone == null
              ? const SizedBox.shrink()
              : OtpScreen(phoneE164: phone);
        },
      ),
      GoRoute(path: '/tasks', builder: (_, _) => const TaskListScreen()),
      GoRoute(
        path: '/reviews',
        builder: (_, _) => const ClassificationReviewListScreen(),
      ),
      GoRoute(
        path: '/reviews/:opcId/:reviewId',
        builder: (_, state) => ClassificationReviewDetailScreen(
          opcId: state.pathParameters['opcId']!,
          reviewId: state.pathParameters['reviewId']!,
        ),
      ),
      GoRoute(
        path: '/tasks/:taskId/import',
        builder: (_, state) =>
            ImportTaskScreen(taskId: state.pathParameters['taskId']!),
      ),
      GoRoute(
        path: '/tasks/:taskId/progress',
        builder: (_, state) =>
            TaskProgressScreen(taskId: state.pathParameters['taskId']!),
      ),
    ],
  );
  ref.onDispose(router.dispose);
  return router;
});

final class _RouterRefresh extends ChangeNotifier {
  void notify() => notifyListeners();
}

bool _isTaskDetailRoute(Uri uri) {
  final segments = uri.pathSegments;
  return segments.length == 3 &&
      segments.first == 'tasks' &&
      (segments.last == 'import' || segments.last == 'progress');
}

final RegExp _reviewOpcId = RegExp(r'^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$');
final RegExp _reviewId = RegExp(
  r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$',
);

bool _isReviewRoute(Uri uri) => uri.pathSegments.firstOrNull == 'reviews';

bool _isValidReviewRoute(Uri uri) {
  if (uri.isAbsolute ||
      uri.hasAuthority ||
      uri.hasQuery ||
      uri.hasFragment ||
      uri.toString().contains('%')) {
    return false;
  }
  final segments = uri.pathSegments;
  if (segments.length == 1) return segments.single == 'reviews';
  return segments.length == 3 &&
      segments.first == 'reviews' &&
      _reviewOpcId.hasMatch(segments[1]) &&
      _reviewId.hasMatch(segments[2]);
}

class PhotoOrganizerApp extends ConsumerStatefulWidget {
  const PhotoOrganizerApp({this.initialLocation = '/', super.key});

  final String initialLocation;

  @override
  ConsumerState<PhotoOrganizerApp> createState() => _PhotoOrganizerAppState();
}

class _PhotoOrganizerAppState extends ConsumerState<PhotoOrganizerApp>
    with WidgetsBindingObserver {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _resumeBackgroundUploads();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _resumeBackgroundUploads();
  }

  void _resumeBackgroundUploads() {
    unawaited(
      ref
          .read(backgroundUploadRuntimeProvider)
          .resume()
          .catchError((Object _) {}),
    );
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp.router(
      title: 'AI 商品相册整理助手',
      theme: ThemeData(colorSchemeSeed: Colors.indigo, useMaterial3: true),
      routerConfig: ref.watch(_routerProvider(widget.initialLocation)),
    );
  }
}
