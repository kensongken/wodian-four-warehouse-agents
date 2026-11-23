import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:photo_organizer/app.dart';
import 'package:photo_organizer/core/api/models.dart';
import 'package:photo_organizer/core/auth/pending_navigation.dart';
import 'package:photo_organizer/core/auth/session_repository.dart';
import 'package:photo_organizer/core/auth/token_store.dart';
import 'package:photo_organizer/core/upload/upload_runtime.dart';
import 'package:photo_organizer/features/tasks/task_repository.dart';
import 'package:photo_organizer/features/tasks/import_task_screen.dart';
import 'package:photo_organizer/features/classification_reviews/classification_review_repository.dart';

import 'helpers/fakes.dart';

void main() {
  testWidgets('unauthenticated task route redirects to login', (tester) async {
    final session = SessionRepository(FakeTransport(), MemoryTokenStore());

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
        ],
        child: const PhotoOrganizerApp(initialLocation: '/tasks'),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('手机号'), findsOneWidget);
    expect(find.text('获取验证码'), findsOneWidget);
  });

  testWidgets('authenticated login route redirects to tasks', (tester) async {
    final session = SessionRepository(
      FakeTransport(),
      MemoryTokenStore(refreshToken: 'r1'),
    );
    final tasks = FakeTaskRepository();

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
          taskRepositoryProvider.overrideWithValue(tasks),
        ],
        child: const PhotoOrganizerApp(initialLocation: '/login'),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('整理任务'), findsOneWidget);
    expect(find.text('还没有整理任务'), findsOneWidget);
  });

  testWidgets('runtime refresh invalidation redirects tasks to login', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueue(method: 'GET', path: '/v1/tasks', status: 401)
      ..enqueueJson(
        '/v1/auth/refresh',
        const <String, Object>{'detail': 'invalid_refresh_token'},
        method: 'POST',
        status: 401,
      );
    final session = SessionRepository(
      transport,
      MemoryTokenStore(refreshToken: 'expired-refresh'),
    );

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
        ],
        child: const PhotoOrganizerApp(initialLocation: '/tasks'),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('手机号'), findsOneWidget);
    expect(find.text('获取验证码'), findsOneWidget);
    expect(await session.isSignedIn, isFalse);
  });

  testWidgets('authenticated cold start preserves an import deep link', (
    tester,
  ) async {
    final session = SessionRepository(
      FakeTransport(),
      MemoryTokenStore(refreshToken: 'r1'),
    );
    const taskId = '11111111-1111-1111-1111-111111111111';

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
        ],
        child: const PhotoOrganizerApp(
          initialLocation: '/tasks/$taskId/import',
        ),
      ),
    );
    await tester.pumpAndSettle();

    final visibleText = tester
        .widgetList<Text>(find.byType(Text))
        .map((widget) => widget.data)
        .whereType<String>()
        .toList();
    expect(visibleText, contains('导入照片'));
    expect(find.textContaining(taskId), findsOneWidget);
    expect(find.byType(ImportTaskScreen), findsOneWidget);
  });

  testWidgets('login continues to the protected cold-start destination', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueue(method: 'POST', path: '/v1/auth/request-code', status: 202)
      ..enqueueJson('/v1/auth/verify-code', <String, Object>{
        'access_token': 'signed-in-access',
        'refresh_token': 'signed-in-refresh',
        'expires_in_seconds': 900,
      }, method: 'POST');
    final session = SessionRepository(
      transport,
      MemoryTokenStore(),
      createDeviceId: () => 'device-12345678',
    );
    const taskId = '22222222-2222-2222-2222-222222222222';

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
        ],
        child: const PhotoOrganizerApp(
          initialLocation: '/tasks/$taskId/import',
        ),
      ),
    );
    await tester.pumpAndSettle();
    var container = ProviderScope.containerOf(
      tester.element(find.byKey(const Key('phone-field'))),
    );
    expect(
      container.read(pendingNavigationProvider).peek(),
      '/tasks/$taskId/import',
    );
    await tester.enterText(
      find.byKey(const Key('phone-field')),
      '+8613800000000',
    );
    await tester.enterText(find.byKey(const Key('invite-field')), 'invite');
    await tester.tap(find.text('获取验证码'));
    await tester.pumpAndSettle();
    container = ProviderScope.containerOf(
      tester.element(find.byKey(const Key('otp-field'))),
    );
    expect(
      container.read(pendingNavigationProvider).peek(),
      '/tasks/$taskId/import',
    );
    await tester.enterText(find.byKey(const Key('otp-field')), '123456');
    await tester.tap(find.text('登录'));
    await tester.pumpAndSettle();

    expect(container.read(pendingNavigationProvider).peek(), isNull);
    final visibleText = tester
        .widgetList<Text>(find.byType(Text))
        .map((widget) => widget.data)
        .whereType<String>()
        .toList();
    expect(visibleText, contains('导入照片'));
    expect(find.textContaining(taskId), findsOneWidget);
    expect(transport.callsTo('/v1/tasks'), 0);
  });

  testWidgets('signed-in warm navigation rejects every invalid import URI', (
    tester,
  ) async {
    final session = SessionRepository(
      FakeTransport(),
      MemoryTokenStore(refreshToken: 'r1'),
    );
    final tasks = FakeTaskRepository();
    const taskId = '33333333-3333-4333-8333-333333333333';
    final invalidLocations = <String>[
      '/tasks/not-a-uuid/import',
      '/tasks/$taskId/import?redirect=https://evil.example',
      '/tasks/$taskId/import#fragment',
      '/tasks/%2e%2e/import',
    ];

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
          taskRepositoryProvider.overrideWithValue(tasks),
        ],
        child: const PhotoOrganizerApp(initialLocation: '/tasks'),
      ),
    );
    await tester.pumpAndSettle();

    for (final location in invalidLocations) {
      final context = tester.element(find.text('整理任务'));
      GoRouter.of(context).go(location);
      await tester.pumpAndSettle();

      expect(find.text('导入照片'), findsNothing, reason: location);
      expect(find.text('整理任务'), findsOneWidget, reason: location);
    }
  });

  testWidgets('signed-in unmatched route fails closed without an error page', (
    tester,
  ) async {
    final session = SessionRepository(
      FakeTransport(),
      MemoryTokenStore(refreshToken: 'r1'),
    );
    final tasks = FakeTaskRepository();

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
          taskRepositoryProvider.overrideWithValue(tasks),
        ],
        child: const PhotoOrganizerApp(initialLocation: '/tasks'),
      ),
    );
    await tester.pumpAndSettle();

    final context = tester.element(find.text('整理任务'));
    GoRouter.of(context).go('/unknown/internal/route');
    await tester.pumpAndSettle();

    expect(find.text('整理任务'), findsOneWidget);
    expect(find.textContaining('/unknown/internal/route'), findsNothing);
    expect(find.textContaining('GoException'), findsNothing);
  });

  testWidgets('authenticated review route opens the OPC review queue', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/me/opcs', const <String, Object?>{
        'items': <Object?>[],
      }, method: 'GET');
    final session = SessionRepository(
      transport,
      MemoryTokenStore(refreshToken: 'r1'),
    );
    final repository = ClassificationReviewRepository(session);

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
          classificationReviewRepositoryProvider.overrideWithValue(repository),
        ],
        child: const PhotoOrganizerApp(initialLocation: '/reviews'),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('分类审核'), findsOneWidget);
    expect(find.text('尚未配置 OPC，请联系管理员'), findsOneWidget);
  });

  testWidgets('login continues to the protected review list', (tester) async {
    final transport = FakeTransport()
      ..enqueue(method: 'POST', path: '/v1/auth/request-code', status: 202)
      ..enqueueJson('/v1/auth/verify-code', <String, Object>{
        'access_token': 'signed-in-access',
        'refresh_token': 'signed-in-refresh',
        'expires_in_seconds': 900,
      }, method: 'POST')
      ..enqueueJson('/v1/me/opcs', const <String, Object?>{
        'items': <Object?>[],
      }, method: 'GET');
    final session = SessionRepository(
      transport,
      MemoryTokenStore(),
      createDeviceId: () => 'device-12345678',
    );

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
        ],
        child: const PhotoOrganizerApp(initialLocation: '/reviews'),
      ),
    );
    await tester.pumpAndSettle();
    await _completeLogin(tester);

    expect(find.text('分类审核'), findsOneWidget);
    expect(find.text('尚未配置 OPC，请联系管理员'), findsOneWidget);
  });

  testWidgets('login continues to the protected review detail', (tester) async {
    const opcId = 'opc-demo';
    const reviewId = '11111111-1111-4111-8111-111111111111';
    final transport = FakeTransport()
      ..enqueue(method: 'POST', path: '/v1/auth/request-code', status: 202)
      ..enqueueJson('/v1/auth/verify-code', <String, Object>{
        'access_token': 'signed-in-access',
        'refresh_token': 'signed-in-refresh',
        'expires_in_seconds': 900,
      }, method: 'POST')
      ..enqueueJson(
        '/v1/opcs/$opcId/classification-reviews/$reviewId',
        _reviewResponse(opcId: opcId, reviewId: reviewId),
        method: 'GET',
      );
    final session = SessionRepository(
      transport,
      MemoryTokenStore(),
      createDeviceId: () => 'device-12345678',
    );

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
        ],
        child: const PhotoOrganizerApp(
          initialLocation: '/reviews/$opcId/$reviewId',
        ),
      ),
    );
    await tester.pumpAndSettle();
    await _completeLogin(tester);

    expect(find.text('审核详情'), findsOneWidget);
    expect(find.text('首发珠'), findsOneWidget);
  });

  testWidgets('task app bar action opens classification reviews', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/me/opcs', const <String, Object?>{
        'items': <Object?>[],
      }, method: 'GET');
    final session = SessionRepository(
      transport,
      MemoryTokenStore(refreshToken: 'r1'),
    );

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
          taskRepositoryProvider.overrideWithValue(FakeTaskRepository()),
        ],
        child: const PhotoOrganizerApp(initialLocation: '/tasks'),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('分类审核'));
    await tester.pumpAndSettle();

    expect(find.text('分类审核'), findsOneWidget);
    expect(find.text('尚未配置 OPC，请联系管理员'), findsOneWidget);
  });

  testWidgets('every invalid review route fails closed to tasks', (
    tester,
  ) async {
    final session = SessionRepository(
      FakeTransport(),
      MemoryTokenStore(refreshToken: 'r1'),
    );
    const reviewId = '11111111-1111-4111-8111-111111111111';
    final invalidLocations = <String>[
      '/reviews/-not-valid/$reviewId',
      '/reviews/opc-demo/not-a-uuid',
      '/reviews/opc-demo/$reviewId?redirect=elsewhere',
      '/reviews/opc-demo/$reviewId#fragment',
      '/reviews/opc-demo/$reviewId/extra',
    ];

    await tester.pumpWidget(
      _routingApp(
        overrides: <Override>[
          sessionRepositoryProvider.overrideWithValue(session),
          taskRepositoryProvider.overrideWithValue(FakeTaskRepository()),
        ],
        child: const PhotoOrganizerApp(initialLocation: '/tasks'),
      ),
    );
    await tester.pumpAndSettle();

    for (final location in invalidLocations) {
      final context = tester.element(find.text('整理任务'));
      GoRouter.of(context).go(location);
      await tester.pumpAndSettle();

      expect(find.text('整理任务'), findsOneWidget, reason: location);
      expect(find.text('审核详情'), findsNothing, reason: location);
      expect(
        find.textContaining('GoException'),
        findsNothing,
        reason: location,
      );
    }
  });

  testWidgets(
    'unmatched route during restore preserves a validated pending destination',
    (tester) async {
      final releaseRestore = Completer<void>();
      final session = SessionRepository(
        FakeTransport(),
        _DelayedTokenStore(releaseRestore.future),
      );
      const taskId = '44444444-4444-4444-8444-444444444444';
      final pending = PendingNavigation()
        ..capture(Uri.parse('/tasks/$taskId/import'));

      await tester.pumpWidget(
        _routingApp(
          overrides: <Override>[
            sessionRepositoryProvider.overrideWithValue(session),
            pendingNavigationProvider.overrideWithValue(pending),
          ],
          child: const PhotoOrganizerApp(
            initialLocation: '/unknown/during-restore',
          ),
        ),
      );
      await tester.pump();

      expect(pending.peek(), '/tasks/$taskId/import');

      releaseRestore.complete();
      await tester.pumpAndSettle();
    },
  );
}

Widget _routingApp({
  required List<Override> overrides,
  required PhotoOrganizerApp child,
}) => ProviderScope(
  overrides: <Override>[
    ...overrides,
    backgroundUploadRuntimeProvider.overrideWithValue(
      const _NoopBackgroundUploadRuntime(),
    ),
  ],
  child: child,
);

final class _NoopBackgroundUploadRuntime implements BackgroundUploadRuntime {
  const _NoopBackgroundUploadRuntime();

  @override
  Future<void> resume() async {}
}

Future<void> _completeLogin(WidgetTester tester) async {
  await tester.enterText(
    find.byKey(const Key('phone-field')),
    '+8613800000000',
  );
  await tester.enterText(find.byKey(const Key('invite-field')), 'invite');
  await tester.tap(find.text('获取验证码'));
  await tester.pumpAndSettle();
  await tester.enterText(find.byKey(const Key('otp-field')), '123456');
  await tester.tap(find.text('登录'));
  await tester.pumpAndSettle();
}

Map<String, Object?> _reviewResponse({
  required String opcId,
  required String reviewId,
}) => <String, Object?>{
  'id': reviewId,
  'opc_id': opcId,
  'classification_id': '22222222-2222-4222-8222-222222222222',
  'album_product_id': 'album-product-demo-001',
  'source_type': 'first_sale_bead',
  'input_snapshot_version': 1,
  'classification_version': 1,
  'candidates': <Object?>[
    <String, Object?>{
      'rank': 1,
      'level_1': '南红',
      'level_2': '手串',
      'confidence': 0.91,
      'reason_codes': <Object?>['seed_match'],
    },
  ],
  'status': 'open',
  'confirmed_level_1': null,
  'confirmed_level_2': null,
  'confirmed_by': null,
  'decided_at': null,
  'created_at': '2026-08-17T01:02:03Z',
  'updated_at': '2026-08-17T01:02:03Z',
  'claims_not_evaluated': <Object?>[
    'authenticity',
    'origin',
    'quality',
    'warehouse_acceptance',
  ],
  'mall_product_id': null,
  'bead_id': null,
};

final class _DelayedTokenStore implements TokenStore {
  const _DelayedTokenStore(this.releaseRead);

  final Future<void> releaseRead;

  @override
  Future<void> clear() async {}

  @override
  Future<TokenPair?> read() async {
    await releaseRead;
    return null;
  }

  @override
  Future<void> write(TokenPair pair) async {}
}
