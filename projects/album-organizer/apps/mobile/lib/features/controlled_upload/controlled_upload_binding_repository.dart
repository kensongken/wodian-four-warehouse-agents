import '../../core/api/api_error.dart';
import '../../core/api/api_client.dart';
import '../../core/auth/session_repository.dart';
import '../../core/upload/upload_models.dart';
import 'controlled_upload_candidate.dart';

const _claims = <String>[
  'authenticity',
  'origin',
  'quality',
  'warehouse_acceptance',
];
const _responseKeys = <String>{
  'schema_version',
  'binding_id',
  'candidate_id',
  'source_readiness_id',
  'opc_id',
  'task_id',
  'album_product_id',
  'source_type',
  'item_count',
  'bound_by',
  'bound_at',
  'opc_authority_state',
  'task_binding_state',
  'upload_state',
  'next_action',
  'claims_not_evaluated',
  'mall_product_id',
  'bead_id',
  'publication_state',
  'auto_publish',
  'shared_market_opt_in',
};
final RegExp _canonicalUuid = RegExp(
  r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
);
final RegExp _canonicalUser = RegExp(
  r'^user:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
);
final RegExp _utcTimestamp = RegExp(
  r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$',
);

abstract interface class ControlledUploadBindingBoundary {
  Future<ControlledUploadBinding> bind({
    required String opcId,
    required String taskId,
    required ControlledUploadCandidate candidate,
  });
}

final class ControlledUploadBinding {
  const ControlledUploadBinding({
    required this.bindingId,
    required this.candidateId,
    required this.opcId,
    required this.taskId,
    required this.boundAt,
  });

  final String bindingId;
  final String candidateId;
  final String opcId;
  final String taskId;
  final DateTime boundAt;

  UploadAuthority get authority =>
      UploadAuthority(bindingId: bindingId, candidateId: candidateId);
}

final class ControlledUploadBindingRepository
    implements ControlledUploadBindingBoundary {
  const ControlledUploadBindingRepository(this._session);

  final SessionRepository _session;

  @override
  Future<ControlledUploadBinding> bind({
    required String opcId,
    required String taskId,
    required ControlledUploadCandidate candidate,
  }) async {
    if (!_canonicalUuid.hasMatch(taskId) || opcId != candidate.opcId) {
      throw _invalidResponse();
    }
    final response = await _session.authorizedPost(
      '/v1/opcs/$opcId/upload-bindings',
      data: <String, Object?>{
        'schema_version': 'opc-controlled-upload-binding-request/1.0',
        'task_id': taskId,
        'candidate': candidate.toRequestJson(),
      },
    );
    _ensureSuccess(response);
    try {
      final json = _object(response.data);
      _requireExactKeys(json, _responseKeys);
      _require(json, 'schema_version', 'opc-controlled-upload-binding/1.0');
      _require(json, 'candidate_id', candidate.candidateId);
      _require(json, 'source_readiness_id', candidate.sourceReadinessId);
      _require(json, 'opc_id', opcId);
      _require(json, 'task_id', taskId);
      _require(json, 'album_product_id', candidate.albumProductId);
      _require(json, 'source_type', 'first_sale_bead');
      final itemCount = json['item_count'];
      if (itemCount is! int || itemCount != candidate.items.length) {
        throw const FormatException('invalid binding item count');
      }
      _require(
        json,
        'opc_authority_state',
        'verified_by_authenticated_membership_and_task_binding',
      );
      _require(json, 'task_binding_state', 'bound');
      _require(json, 'upload_state', 'ready_for_queue_handoff');
      _require(json, 'next_action', 'enqueue_candidate_items');
      _require(json, 'mall_product_id', null);
      _require(json, 'bead_id', null);
      _require(json, 'publication_state', 'not_created');
      _require(json, 'auto_publish', false);
      _require(json, 'shared_market_opt_in', false);
      final claims = json['claims_not_evaluated'];
      if (claims is! List<Object?> || !_sameValues(claims, _claims)) {
        throw const FormatException('invalid claims boundary');
      }
      final bindingId = _string(json, 'binding_id');
      final boundBy = _string(json, 'bound_by');
      final boundAtWire = _string(json, 'bound_at');
      if (!_canonicalUuid.hasMatch(bindingId) ||
          !_canonicalUser.hasMatch(boundBy) ||
          !_utcTimestamp.hasMatch(boundAtWire)) {
        throw const FormatException('invalid binding identity');
      }
      final boundAt = _parseUtc(boundAtWire);
      return ControlledUploadBinding(
        bindingId: bindingId,
        candidateId: candidate.candidateId,
        opcId: opcId,
        taskId: taskId,
        boundAt: boundAt,
      );
    } on Object {
      throw _invalidResponse();
    }
  }
}

DateTime _parseUtc(String value) {
  final match = _utcTimestamp.firstMatch(value);
  if (match == null) throw const FormatException('invalid UTC timestamp');
  final parts = <int>[
    for (var index = 1; index <= 6; index += 1) int.parse(match.group(index)!),
  ];
  final fraction = match.group(7) ?? '';
  final micros = int.parse(fraction.isEmpty ? '0' : fraction.padRight(6, '0'));
  final parsed = DateTime.utc(
    parts[0],
    parts[1],
    parts[2],
    parts[3],
    parts[4],
    parts[5],
    micros ~/ 1000,
    micros % 1000,
  );
  if (parts[0] < 1 ||
      parsed.year != parts[0] ||
      parsed.month != parts[1] ||
      parsed.day != parts[2] ||
      parsed.hour != parts[3] ||
      parsed.minute != parts[4] ||
      parsed.second != parts[5] ||
      parsed.millisecond * 1000 + parsed.microsecond != micros) {
    throw const FormatException('invalid UTC timestamp');
  }
  return parsed;
}

void _ensureSuccess(ApiResponse response) {
  if (response.statusCode < 200 || response.statusCode >= 300) {
    throw ApiException.fromResponse(response);
  }
}

Map<String, Object?> _object(Object? value) {
  if (value is! Map<Object?, Object?>) {
    throw const FormatException('expected response object');
  }
  return value.map((key, item) => MapEntry(key.toString(), item));
}

String _string(Map<String, Object?> json, String key) {
  final value = json[key];
  if (value is! String || value.isEmpty) {
    throw const FormatException('missing response string');
  }
  return value;
}

void _require(Map<String, Object?> json, String key, Object? expected) {
  if (json[key] != expected) {
    throw const FormatException('response context mismatch');
  }
}

void _requireExactKeys(Map<String, Object?> json, Set<String> expected) {
  final actual = json.keys.toSet();
  if (actual.length != expected.length || !actual.containsAll(expected)) {
    throw const FormatException('unexpected response fields');
  }
}

bool _sameValues(List<Object?> actual, List<String> expected) {
  if (actual.length != expected.length) return false;
  for (var index = 0; index < expected.length; index += 1) {
    if (actual[index] != expected[index]) return false;
  }
  return true;
}

ApiException _invalidResponse() =>
    const ApiException(code: 'invalid_response', userMessage: '服务返回异常，请稍后重试');
