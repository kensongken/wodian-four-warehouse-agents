import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/auth/session_repository.dart';
import 'package:photo_organizer/features/classification_reviews/classification_review_detail_screen.dart';
import 'package:photo_organizer/features/classification_reviews/classification_review_repository.dart';

import '../../helpers/fakes.dart';

const opcId = 'opc-demo';
const reviewId = '11111111-1111-4111-8111-111111111111';
const detailPath = '/v1/opcs/$opcId/classification-reviews/$reviewId';

void main() {
  testWidgets('shows candidates and requires an explicit complete selection', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson(detailPath, _review(), method: 'GET');

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();

    expect(find.text('仅确认图片分类，不判断真伪、产地、品质或仓验。'), findsOneWidget);
    expect(find.text('首发珠'), findsOneWidget);
    expect(find.text('南红 · 手串 · 91%'), findsOneWidget);
    expect(find.text('其他 · 珠子/散珠 · 42%'), findsOneWidget);
    expect(find.byType(Image), findsNothing);
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(FilledButton, '确认分类'))
          .onPressed,
      isNull,
    );

    await tester.tap(find.byKey(const Key('candidate-0')));
    await tester.pump();

    expect(
      tester
          .widget<DropdownButtonFormField<String>>(
            find.byKey(const Key('level-1-selector')),
          )
          .initialValue,
      '南红',
    );
    expect(
      tester
          .widget<DropdownButtonFormField<String>>(
            find.byKey(const Key('level-2-selector')),
          )
          .initialValue,
      '手串',
    );
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(FilledButton, '确认分类'))
          .onPressed,
      isNotNull,
    );
  });

  testWidgets('confirms the selected category and becomes read only', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson(detailPath, _review(), method: 'GET')
      ..enqueueJson(
        '$detailPath/confirm',
        _review(resolved: true),
        method: 'POST',
      );

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('candidate-0')));
    await tester.pump();
    await tester.tap(find.text('确认分类'));
    await tester.pumpAndSettle();
    expect(find.text('确认后当前流程不可再次修改。'), findsOneWidget);
    await tester.tap(find.text('确认'));
    await tester.pumpAndSettle();

    expect(find.text('已确认：南红 · 手串'), findsOneWidget);
    expect(find.text('确认分类'), findsNothing);
    expect(transport.calls.last.data, <String, Object?>{
      'level_1': '南红',
      'level_2': '手串',
    });
  });

  testWidgets('a resolved conflict refreshes detail with safe copy', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson(detailPath, _review(), method: 'GET')
      ..enqueueJson(
        '$detailPath/confirm',
        const <String, Object?>{'detail': 'review_already_resolved'},
        method: 'POST',
        status: 409,
      )
      ..enqueueJson(detailPath, _review(resolved: true), method: 'GET');

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('candidate-0')));
    await tester.pump();
    await tester.tap(find.text('确认分类'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('确认'));
    await tester.pumpAndSettle();

    expect(find.text('该记录已处理，已刷新'), findsOneWidget);
    expect(find.text('已确认：南红 · 手串'), findsOneWidget);
    expect(transport.callsTo(detailPath), 2);
  });

  testWidgets('submitting state prevents a second confirm request', (
    tester,
  ) async {
    final postCalled = Completer<void>();
    final releasePost = Completer<void>();
    final transport = FakeTransport()
      ..enqueueJson(detailPath, _review(), method: 'GET')
      ..enqueueJson(
        '$detailPath/confirm',
        _review(resolved: true),
        method: 'POST',
        called: postCalled,
        gate: releasePost.future,
      );

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('candidate-0')));
    await tester.pump();
    await tester.tap(find.text('确认分类'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('确认'));
    await tester.pump();
    await postCalled.future;

    final submit = tester.widget<FilledButton>(
      find.widgetWithText(FilledButton, '确认中…'),
    );
    expect(submit.onPressed, isNull);
    expect(transport.callsTo('$detailPath/confirm'), 1);

    releasePost.complete();
    await tester.pumpAndSettle();
    expect(transport.callsTo('$detailPath/confirm'), 1);
  });

  testWidgets('missing review uses a tenant-safe unavailable message', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson(
        detailPath,
        const <String, Object?>{'detail': 'review_not_found'},
        method: 'GET',
        status: 404,
      );

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();

    expect(find.text('记录不存在或已不可访问'), findsOneWidget);
    expect(find.textContaining('review_not_found'), findsNothing);
  });

  testWidgets('missing review during confirmation uses the same safe message', (
    tester,
  ) async {
    final transport = FakeTransport()
      ..enqueueJson(detailPath, _review(), method: 'GET')
      ..enqueueJson(
        '$detailPath/confirm',
        const <String, Object?>{'detail': 'review_not_found'},
        method: 'POST',
        status: 404,
      );

    await tester.pumpWidget(_app(transport));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('candidate-0')));
    await tester.pump();
    await tester.tap(find.text('确认分类'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('确认'));
    await tester.pumpAndSettle();

    expect(find.text('记录不存在或已不可访问'), findsOneWidget);
    expect(find.textContaining('review_not_found'), findsNothing);
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
    child: const MaterialApp(
      home: ClassificationReviewDetailScreen(opcId: opcId, reviewId: reviewId),
    ),
  );
}

Map<String, Object?> _review({bool resolved = false}) => <String, Object?>{
  'id': reviewId,
  'opc_id': opcId,
  'classification_id': '22222222-2222-4222-8222-222222222222',
  'album_product_id': 'album-product-demo-001',
  'source_type': 'first_sale_bead',
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
    <String, Object?>{
      'rank': 2,
      'level_1': '其他',
      'level_2': '珠子/散珠',
      'confidence': 0.42,
      'reason_codes': <Object?>['visual_pattern'],
    },
  ],
  'status': resolved ? 'resolved' : 'open',
  'confirmed_level_1': resolved ? '南红' : null,
  'confirmed_level_2': resolved ? '手串' : null,
  'confirmed_by': resolved ? 'user:33333333-3333-4333-8333-333333333333' : null,
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
