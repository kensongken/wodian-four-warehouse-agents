import '../../core/api/api_error.dart';

const classificationReviewUnavailableMessage = '记录不存在或已不可访问';

bool isClassificationReviewUnavailable(Object error) =>
    error is ApiException &&
    const <String>{'opc_not_found', 'review_not_found'}.contains(error.code);

String classificationReviewSafeMessage(
  Object error, {
  required String fallback,
}) {
  if (isClassificationReviewUnavailable(error)) {
    return classificationReviewUnavailableMessage;
  }
  return fallback;
}
