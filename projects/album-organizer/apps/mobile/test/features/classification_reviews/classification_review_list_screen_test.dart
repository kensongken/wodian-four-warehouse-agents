import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:photo_organizer/core/auth/session_repository.dart';
import 'package:photo_organizer/features/classification_reviews/classification_review_list_screen.dart';
import 'package:photo_organizer/features/classification_reviews/classification_review_repository.dart';

import '../../helpers/fakes.dart';

void main() {
  testWidgets('shows a safe empty state when the user has no OPC', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/me/opcs', const <String, Object?>{
        'items': <Object?>[],
      }, method: 'GET');

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();

    expect(find.text('分类审核'), findsOneWidget);
    expect(find.text('尚未配置 OPC，请联系管理员'), findsOneWidget);
    expect(find.textContaining('真伪'), findsOneWidget);
    expect(transport.calls, hasLength(1));
  });

  testWidgets('membership revocation uses a tenant-neutral message', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson(
        '/v1/me/opcs',
        const <String, Object?>{'detail': 'opc_not_found'},
        method: 'GET',
        status: 404,
      );

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();

    expect(find.text('记录不存在或已不可访问'), findsOneWidget);
    expect(find.textContaining('opc_not_found'), findsNothing);
  });

  testWidgets('single OPC auto-loads open reviews with safe product context', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/me/opcs', <String, Object?>{
        'items': <Object?>[_membership('opc-a', 'A OPC')],
      }, method: 'GET')
      ..enqueueJson('/v1/opcs/opc-a/classification-reviews', <String, Object?>{
        'items': <Object?>[
          _review(
            id: '11111111-1111-4111-8111-111111111111',
            opcId: 'opc-a',
            albumProductId: 'album-product-a',
            sourceType: 'first_sale_bead',
          ),
        ],
        'next_before': null,
      }, method: 'GET');

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();

    expect(find.text('A OPC'), findsOneWidget);
    expect(find.text('首发珠'), findsOneWidget);
    expect(find.text('Album Product：album-product-a'), findsOneWidget);
    expect(find.text('快照 v2 · 分类 v1'), findsOneWidget);
    expect(find.text('南红 · 手串 · 91%'), findsOneWidget);
    expect(find.text('候选 1 个'), findsOneWidget);
    expect(transport.calls.last.query, <String, Object?>{
      'status': 'open',
      'limit': 20,
    });
  });

  testWidgets('switching OPC discards a late result from the previous OPC', (
    tester,
  ) async {
    final aCalled = Completer<void>();
    final releaseA = Completer<void>();
    final transport = FakeTransport()
      ..enqueueJson('/v1/me/opcs', <String, Object?>{
        'items': <Object?>[
          _membership('opc-a', 'A OPC'),
          _membership('opc-b', 'B OPC'),
        ],
      }, method: 'GET')
      ..enqueueJson(
        '/v1/opcs/opc-a/classification-reviews',
        <String, Object?>{
          'items': <Object?>[
            _review(
              id: '11111111-1111-4111-8111-111111111111',
              opcId: 'opc-a',
              albumProductId: 'album-product-a',
              sourceType: 'first_sale_bead',
            ),
          ],
          'next_before': null,
        },
        method: 'GET',
        called: aCalled,
        gate: releaseA.future,
      )
      ..enqueueJson('/v1/opcs/opc-b/classification-reviews', <String, Object?>{
        'items': <Object?>[
          _review(
            id: '22222222-2222-4222-8222-222222222222',
            opcId: 'opc-b',
            albumProductId: 'album-product-b',
            sourceType: 'legacy_material',
          ),
        ],
        'next_before': null,
      }, method: 'GET');

    await tester.pumpWidget(_app(transport));
    await tester.pump();
    await aCalled.future;
    await tester.tap(find.byKey(const Key('opc-selector')));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    await tester.tap(find.text('B OPC').last);
    await tester.pumpAndSettle();

    expect(find.text('Album Product：album-product-b'), findsOneWidget);
    releaseA.complete();
    await tester.pumpAndSettle();
    expect(find.text('Album Product：album-product-a'), findsNothing);
    expect(find.text('Album Product：album-product-b'), findsOneWidget);
  });

  testWidgets('loads another page and switches to resolved reviews', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/me/opcs', <String, Object?>{
        'items': <Object?>[_membership('opc-a', 'A OPC')],
      }, method: 'GET')
      ..enqueueJson('/v1/opcs/opc-a/classification-reviews', <String, Object?>{
        'items': <Object?>[
          _review(
            id: '11111111-1111-4111-8111-111111111111',
            opcId: 'opc-a',
            albumProductId: 'album-product-first',
            sourceType: 'first_sale_bead',
          ),
        ],
        'next_before': 'next-page',
      }, method: 'GET')
      ..enqueueJson('/v1/opcs/opc-a/classification-reviews', <String, Object?>{
        'items': <Object?>[
          _review(
            id: '22222222-2222-4222-8222-222222222222',
            opcId: 'opc-a',
            albumProductId: 'album-product-second',
            sourceType: 'redesign_output',
          ),
        ],
        'next_before': null,
      }, method: 'GET')
      ..enqueueJson('/v1/opcs/opc-a/classification-reviews', <String, Object?>{
        'items': <Object?>[
          _review(
            id: '33333333-3333-4333-8333-333333333333',
            opcId: 'opc-a',
            albumProductId: 'album-product-resolved',
            sourceType: 'legacy_material',
            resolved: true,
          ),
        ],
        'next_before': null,
      }, method: 'GET');

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();
    await tester.tap(find.text('加载更多'));
    await tester.pumpAndSettle();
    expect(find.text('Album Product：album-product-first'), findsOneWidget);
    expect(find.text('Album Product：album-product-second'), findsOneWidget);
    expect(transport.calls[2].query['before'], 'next-page');

    await tester.tap(find.text('已完成'));
    await tester.pumpAndSettle();
    expect(find.text('Album Product：album-product-first'), findsNothing);
    expect(find.text('Album Product：album-product-resolved'), findsOneWidget);
    expect(transport.calls.last.query['status'], 'resolved');
  });

  testWidgets('pagination revocation clears stale OPC review cards', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/me/opcs', <String, Object?>{
        'items': <Object?>[_membership('opc-a', 'A OPC')],
      }, method: 'GET')
      ..enqueueJson('/v1/opcs/opc-a/classification-reviews', <String, Object?>{
        'items': <Object?>[
          _review(
            id: '11111111-1111-4111-8111-111111111111',
            opcId: 'opc-a',
            albumProductId: 'album-product-first',
            sourceType: 'first_sale_bead',
          ),
        ],
        'next_before': 'next-page',
      }, method: 'GET')
      ..enqueueJson(
        '/v1/opcs/opc-a/classification-reviews',
        const <String, Object?>{'detail': 'opc_not_found'},
        method: 'GET',
        status: 404,
      );

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();
    expect(find.text('Album Product：album-product-first'), findsOneWidget);

    await tester.tap(find.text('加载更多'));
    await tester.pumpAndSettle();

    expect(find.text('记录不存在或已不可访问'), findsOneWidget);
    expect(find.text('Album Product：album-product-first'), findsNothing);
    expect(find.textContaining('opc_not_found'), findsNothing);
  });

  testWidgets('returning from detail refreshes the review queue', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/me/opcs', <String, Object?>{
        'items': <Object?>[_membership('opc-a', 'A OPC')],
      }, method: 'GET')
      ..enqueueJson('/v1/opcs/opc-a/classification-reviews', <String, Object?>{
        'items': <Object?>[
          _review(
            id: '11111111-1111-4111-8111-111111111111',
            opcId: 'opc-a',
            albumProductId: 'album-product-a',
            sourceType: 'first_sale_bead',
          ),
        ],
        'next_before': null,
      }, method: 'GET')
      ..enqueueJson(
        '/v1/opcs/opc-a/classification-reviews',
        const <String, Object?>{'items': <Object?>[], 'next_before': null},
        method: 'GET',
      );
    final repository = ClassificationReviewRepository(
      SessionRepository(
        transport,
        MemoryTokenStore(
          accessToken: 'test-access-token',
          refreshToken: 'refresh-token',
        ),
      ),
    );
    final router = GoRouter(
      initialLocation: '/reviews',
      routes: <RouteBase>[
        GoRoute(
          path: '/reviews',
          builder: (_, _) => const ClassificationReviewListScreen(),
        ),
        GoRoute(
          path: '/reviews/:opcId/:reviewId',
          builder: (context, _) => Scaffold(
            body: TextButton(onPressed: context.pop, child: const Text('返回列表')),
          ),
        ),
      ],
    );
    addTearDown(router.dispose);

    await tester.pumpWidget(
      ProviderScope(
        overrides: <Override>[
          classificationReviewRepositoryProvider.overrideWithValue(repository),
        ],
        child: MaterialApp.router(routerConfig: router),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('Album Product：album-product-a'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('返回列表'));
    await tester.pumpAndSettle();

    expect(find.text('暂无待审核记录'), findsOneWidget);
    expect(transport.callsTo('/v1/opcs/opc-a/classification-reviews'), 2);
  });
}

Widget _app(FakeTransport transport) {
  final repository = ClassificationReviewRepository(
    SessionRepository(
      transport,
      MemoryTokenStore(
        accessToken: 'test-access-token',
        refreshToken: 'refresh-token',
      ),
    ),
  );
  return ProviderScope(
    overrides: <Override>[
      classificationReviewRepositoryProvider.overrideWithValue(repository),
    ],
    child: const MaterialApp(home: ClassificationReviewListScreen()),
  );
}

Map<String, Object?> _membership(String id, String displayName) =>
    <String, Object?>{
      'id': id,
      'display_name': displayName,
      'role': 'reviewer',
    };

Map<String, Object?> _review({
  required String id,
  required String opcId,
  required String albumProductId,
  required String sourceType,
  bool resolved = false,
}) => <String, Object?>{
  'id': id,
  'opc_id': opcId,
  'classification_id': '44444444-4444-4444-8444-444444444444',
  'album_product_id': albumProductId,
  'source_type': sourceType,
  'input_snapshot_version': 2,
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
  'status': resolved ? 'resolved' : 'open',
  'confirmed_level_1': resolved ? '南红' : null,
  'confirmed_level_2': resolved ? '手串' : null,
  'confirmed_by': resolved ? 'user:55555555-5555-4555-8555-555555555555' : null,
  'decided_at': resolved ? '2026-08-17T05:00:00Z' : null,
  'created_at': '2026-08-17T01:02:03Z',
  'updated_at': '2026-08-17T04:05:06Z',
  'claims_not_evaluated': <Object?>[
    'authenticity',
    'origin',
    'quality',
    'warehouse_acceptance',
  ],
  'mall_product_id': null,
  'bead_id': null,
};
