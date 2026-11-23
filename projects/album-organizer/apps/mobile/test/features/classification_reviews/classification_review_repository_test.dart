import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/api_error.dart';
import 'package:photo_organizer/core/auth/session_repository.dart';
import 'package:photo_organizer/features/classification_reviews/classification_review_models.dart';
import 'package:photo_organizer/features/classification_reviews/classification_review_repository.dart';

import '../../helpers/fakes.dart';

const opcId = 'opc-demo';
const reviewId = '11111111-1111-4111-8111-111111111111';
const reviewJson = <String, Object?>{
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
  'status': 'open',
  'confirmed_level_1': null,
  'confirmed_level_2': null,
  'confirmed_by': null,
  'decided_at': null,
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

void main() {
  test('discovers current OPC memberships with the exact contract', () async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/me/opcs', <String, Object?>{
        'items': <Object?>[
          <String, Object?>{
            'id': opcId,
            'display_name': '示例 OPC',
            'role': 'reviewer',
          },
        ],
      }, method: 'GET');
    final repository = _repository(transport);

    final memberships = await repository.listMyOpcMemberships();

    expect(memberships, hasLength(1));
    expect(memberships.single.id, opcId);
    expect(memberships.single.displayName, '示例 OPC');
    expect(memberships.single.role, OpcRole.reviewer);
    expect(transport.calls.single.query, isEmpty);
  });

  test(
    'lists reviews with exact tenant status limit and cursor query',
    () async {
      final transport = FakeTransport()
        ..enqueueJson(
          '/v1/opcs/$opcId/classification-reviews',
          <String, Object?>{
            'items': <Object?>[reviewJson],
            'next_before': '2026-08-17T01:02:03Z,$reviewId',
          },
          method: 'GET',
        );
      final repository = _repository(transport);

      final page = await repository.listReviews(
        opcId,
        ReviewStatus.open,
        before: 'cursor-1',
      );

      expect(page.items, hasLength(1));
      expect(page.items.single.sourceType, ProductSourceType.firstSaleBead);
      expect(page.items.single.candidates, hasLength(2));
      expect(page.items.single.candidates.first.level1, '南红');
      expect(page.nextBefore, '2026-08-17T01:02:03Z,$reviewId');
      expect(transport.calls.single.query, <String, Object?>{
        'status': 'open',
        'limit': 20,
        'before': 'cursor-1',
      });
    },
  );

  test('omits a null cursor and rejects a response from another OPC', () async {
    final wrongOpc = Map<String, Object?>.of(reviewJson)
      ..['opc_id'] = 'opc-other';
    final transport = FakeTransport()
      ..enqueueJson('/v1/opcs/$opcId/classification-reviews', <String, Object?>{
        'items': <Object?>[wrongOpc],
        'next_before': null,
      }, method: 'GET');
    final repository = _repository(transport);

    await expectLater(
      repository.listReviews(opcId, ReviewStatus.open),
      throwsA(_invalidResponse),
    );
    expect(transport.calls.single.query, <String, Object?>{
      'status': 'open',
      'limit': 20,
    });
  });

  test(
    'gets detail and confirms with the exact server-owned actor contract',
    () async {
      final resolved = Map<String, Object?>.of(reviewJson)
        ..['status'] = 'resolved'
        ..['confirmed_level_1'] = '南红'
        ..['confirmed_level_2'] = '手串'
        ..['confirmed_by'] = 'user:33333333-3333-4333-8333-333333333333'
        ..['decided_at'] = '2026-08-17T05:00:00Z';
      final path = '/v1/opcs/$opcId/classification-reviews/$reviewId';
      final transport = FakeTransport()
        ..enqueueJson(path, reviewJson, method: 'GET')
        ..enqueueJson('$path/confirm', resolved, method: 'POST');
      final repository = _repository(transport);

      final detail = await repository.getReview(opcId, reviewId);
      final confirmed = await repository.confirm(
        opcId,
        reviewId,
        level1: '南红',
        level2: '手串',
      );

      expect(detail.id, reviewId);
      expect(confirmed.status, ReviewStatus.resolved);
      expect(transport.calls.last.data, <String, Object?>{
        'level_1': '南红',
        'level_2': '手串',
      });
      expect(
        (transport.calls.last.data as Map).containsKey('confirmed_by'),
        isFalse,
      );
    },
  );

  for (final mutation in <String, Map<String, Object?> Function()>{
    'unknown source type': () =>
        Map<String, Object?>.of(reviewJson)..['source_type'] = 'unknown',
    'missing claim boundary': () =>
        Map<String, Object?>.of(reviewJson)
          ..['claims_not_evaluated'] = <Object?>['authenticity'],
    'non-null mall product': () =>
        Map<String, Object?>.of(reviewJson)..['mall_product_id'] = 'mall-1',
    'non-null bead': () =>
        Map<String, Object?>.of(reviewJson)..['bead_id'] = 'bead-1',
    'empty candidates': () =>
        Map<String, Object?>.of(reviewJson)..['candidates'] = <Object?>[],
    'zero snapshot version': () =>
        Map<String, Object?>.of(reviewJson)..['input_snapshot_version'] = 0,
  }.entries) {
    test('${mutation.key} is a stable invalid response', () async {
      final transport = FakeTransport()
        ..enqueueJson(
          '/v1/opcs/$opcId/classification-reviews/$reviewId',
          mutation.value(),
          method: 'GET',
        );

      await expectLater(
        _repository(transport).getReview(opcId, reviewId),
        throwsA(_invalidResponse),
      );
    });
  }

  test('preserves safe backend conflict codes', () async {
    final path = '/v1/opcs/$opcId/classification-reviews/$reviewId/confirm';
    final transport = FakeTransport()
      ..enqueueJson(
        path,
        const <String, Object?>{'detail': 'review_already_resolved'},
        method: 'POST',
        status: 409,
      );

    await expectLater(
      _repository(transport)
          .confirm(opcId, reviewId, level1: '南红', level2: '手串'),
      throwsA(
        isA<ApiException>().having(
          (error) => error.code,
          'code',
          'review_already_resolved',
        ),
      ),
    );
  });
}

final _invalidResponse = isA<ApiException>().having(
  (error) => error.code,
  'code',
  'invalid_response',
);

ClassificationReviewRepository _repository(FakeTransport transport) =>
    ClassificationReviewRepository(
      SessionRepository(
        transport,
        MemoryTokenStore(
          accessToken: 'test-access-token',
          refreshToken: 'refresh-token',
        ),
      ),
    );
