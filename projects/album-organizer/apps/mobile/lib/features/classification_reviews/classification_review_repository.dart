import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_client.dart';
import '../../core/api/api_error.dart';
import '../../core/auth/session_repository.dart';
import 'classification_review_models.dart';

final classificationReviewRepositoryProvider =
    Provider<ClassificationReviewRepository>(
      (ref) =>
          ClassificationReviewRepository(ref.watch(sessionRepositoryProvider)),
    );

final class ClassificationReviewRepository {
  const ClassificationReviewRepository(this._session);

  final SessionRepository _session;

  Future<List<OpcMembershipSummary>> listMyOpcMemberships() async {
    final response = await _session.authorizedGet('/v1/me/opcs');
    _ensureSuccess(response);
    try {
      final json = reviewJsonObject(response.data);
      final values = json['items'];
      if (values is! List<Object?>) {
        throw const FormatException('items must be a list');
      }
      return values
          .map(
            (value) => OpcMembershipSummary.fromJson(reviewJsonObject(value)),
          )
          .toList(growable: false);
    } on FormatException {
      throw _invalidResponse();
    }
  }

  Future<ClassificationReviewPage> listReviews(
    String opcId,
    ReviewStatus status, {
    String? before,
  }) async {
    final response = await _session.authorizedGet(
      '/v1/opcs/$opcId/classification-reviews',
      query: <String, Object?>{
        'status': status.wireValue,
        'limit': 20,
        'before': ?before,
      },
    );
    _ensureSuccess(response);
    try {
      final json = reviewJsonObject(response.data);
      final values = json['items'];
      final cursor = json['next_before'];
      if (values is! List<Object?> ||
          (cursor != null && (cursor is! String || cursor.isEmpty))) {
        throw const FormatException('invalid review page');
      }
      final items = values
          .map(
            (value) => ClassificationReview.fromJson(reviewJsonObject(value)),
          )
          .toList(growable: false);
      if (items.any((item) => item.opcId != opcId || item.status != status)) {
        throw const FormatException('review page does not match request');
      }
      return ClassificationReviewPage(
        items: items,
        nextBefore: cursor as String?,
      );
    } on FormatException {
      throw _invalidResponse();
    }
  }

  Future<ClassificationReview> getReview(String opcId, String reviewId) async {
    final response = await _session.authorizedGet(
      '/v1/opcs/$opcId/classification-reviews/$reviewId',
    );
    return _reviewFromResponse(
      response,
      expectedOpcId: opcId,
      expectedReviewId: reviewId,
    );
  }

  Future<ClassificationReview> confirm(
    String opcId,
    String reviewId, {
    required String level1,
    required String level2,
  }) async {
    final response = await _session.authorizedPost(
      '/v1/opcs/$opcId/classification-reviews/$reviewId/confirm',
      data: <String, Object?>{'level_1': level1, 'level_2': level2},
    );
    return _reviewFromResponse(
      response,
      expectedOpcId: opcId,
      expectedReviewId: reviewId,
    );
  }

  static ClassificationReview _reviewFromResponse(
    ApiResponse response, {
    required String expectedOpcId,
    required String expectedReviewId,
  }) {
    _ensureSuccess(response);
    try {
      final review = ClassificationReview.fromJson(
        reviewJsonObject(response.data),
      );
      if (review.opcId != expectedOpcId || review.id != expectedReviewId) {
        throw const FormatException('review does not match request');
      }
      return review;
    } on FormatException {
      throw _invalidResponse();
    }
  }

  static void _ensureSuccess(ApiResponse response) {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw ApiException.fromResponse(response);
    }
  }

  static ApiException _invalidResponse() =>
      const ApiException(code: 'invalid_response', userMessage: '服务返回异常，请稍后重试');
}
