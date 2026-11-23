import 'dart:collection';
import 'dart:convert';

import 'package:crypto/crypto.dart';

const _candidatePrefix = 'opc-controlled-upload-candidate:';
const _claims = <String>[
  'authenticity',
  'origin',
  'quality',
  'warehouse_acceptance',
];
const _topLevelKeys = <String>{
  'schema_version',
  'candidate_id',
  'status',
  'source_readiness_id',
  'opc_id',
  'opc_authority_state',
  'album_product_id',
  'source_type',
  'target_chain',
  'data_classification',
  'material_owner',
  'authorization_basis',
  'authorized_by',
  'authorized_at',
  'privacy_review_confirmed',
  'customer_data_present',
  'items',
  'claims_not_evaluated',
  'target_task_id',
  'task_binding_state',
  'upload_state',
  'next_action',
  'mall_product_id',
  'bead_id',
  'publication_state',
  'auto_publish',
  'shared_market_opt_in',
};
const _itemKeys = <String>{
  'local_asset_id',
  'role',
  'position',
  'filename',
  'media_type',
  'byte_size',
  'sha256',
  'captured_at',
  'human_privacy_reviewed',
  'privacy_metadata_status',
  'retry',
};

final RegExp _candidateIdPattern = RegExp(
  r'^opc-controlled-upload-candidate:[0-9a-f]{64}$',
);
final RegExp _readinessIdPattern = RegExp(
  r'^opc-real-sku-pilot-readiness:[0-9a-f]{64}$',
);
final RegExp _sha256Pattern = RegExp(r'^[0-9a-f]{64}$');
final RegExp _stableIdPattern = RegExp(r'^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$');
final RegExp _canonicalUuidPattern = RegExp(
  r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
);
final RegExp _canonicalUserPattern = RegExp(
  r'^user:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
);
final RegExp _utcTimestampPattern = RegExp(
  r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$',
);

final class ControlledUploadException implements Exception {
  const ControlledUploadException({
    required this.code,
    required this.safeMessage,
  });

  final String code;
  final String safeMessage;

  @override
  String toString() => 'ControlledUploadException($code): $safeMessage';
}

final class ControlledUploadCandidateItem {
  const ControlledUploadCandidateItem._({
    required this.localAssetId,
    required this.role,
    required this.position,
    required this.filename,
    required this.mediaType,
    required this.byteSize,
    required this.sha256,
    required this.capturedAt,
    required this.capturedAtWire,
  });

  final String localAssetId;
  final String role;
  final int position;
  final String filename;
  final String mediaType;
  final int byteSize;
  final String sha256;
  final DateTime capturedAt;
  final String capturedAtWire;
}

final class ControlledUploadCandidate {
  ControlledUploadCandidate._({
    required this.candidateId,
    required this.sourceReadinessId,
    required this.opcId,
    required this.albumProductId,
    required this.authorizedAt,
    required this.items,
    required this._requestJson,
  });

  factory ControlledUploadCandidate.fromJson(Map<String, Object?> input) {
    try {
      _requireExactKeys(input, _topLevelKeys);
      _requireEquals(
        input,
        'schema_version',
        'opc-controlled-upload-candidate/1.0',
      );
      _requireEquals(input, 'status', 'ready_for_authenticated_task_binding');
      _requireEquals(
        input,
        'opc_authority_state',
        'requires_authenticated_revalidation',
      );
      _requireEquals(input, 'source_type', 'first_sale_bead');
      _requireEquals(input, 'target_chain', 'sellable_product_material');
      _requireEquals(
        input,
        'data_classification',
        'opc_owned_product_material_no_customer_data',
      );
      _requireEquals(input, 'material_owner', 'opc');
      _requireEquals(
        input,
        'authorization_basis',
        'opc_self_owned_product_material',
      );
      _requireEquals(input, 'privacy_review_confirmed', true);
      _requireEquals(input, 'customer_data_present', false);
      _requireEquals(input, 'target_task_id', null);
      _requireEquals(input, 'task_binding_state', 'not_bound');
      _requireEquals(input, 'upload_state', 'not_started');
      _requireEquals(input, 'next_action', 'bind_to_verified_opc_upload_task');
      _requireEquals(input, 'mall_product_id', null);
      _requireEquals(input, 'bead_id', null);
      _requireEquals(input, 'publication_state', 'not_created');
      _requireEquals(input, 'auto_publish', false);
      _requireEquals(input, 'shared_market_opt_in', false);

      final candidateId = _requiredString(input, 'candidate_id');
      final sourceReadinessId = _requiredString(input, 'source_readiness_id');
      final opcId = _requiredStableId(input, 'opc_id');
      final albumProductId = _requiredStableId(input, 'album_product_id');
      final authorizedBy = _requiredString(input, 'authorized_by');
      final authorizedAtWire = _requiredString(input, 'authorized_at');
      if (!_candidateIdPattern.hasMatch(candidateId) ||
          !_readinessIdPattern.hasMatch(sourceReadinessId) ||
          !_canonicalUserPattern.hasMatch(authorizedBy)) {
        throw const FormatException('invalid controlled identity');
      }
      final authorizedAt = _parseUtc(authorizedAtWire);

      final rawClaims = input['claims_not_evaluated'];
      if (rawClaims is! List<Object?> ||
          rawClaims.length != _claims.length ||
          !_sameOrderedValues(rawClaims, _claims)) {
        throw const FormatException('invalid claims boundary');
      }

      final rawItems = input['items'];
      if (rawItems is! List<Object?> ||
          rawItems.length < 2 ||
          rawItems.length > 4) {
        throw const FormatException('invalid candidate items');
      }
      final items = <ControlledUploadCandidateItem>[];
      for (var index = 0; index < rawItems.length; index += 1) {
        final raw = _object(rawItems[index]);
        _requireExactKeys(raw, _itemKeys);
        final item = _parseItem(raw, index);
        items.add(item);
      }
      if (items.map((item) => item.localAssetId).toSet().length !=
          items.length) {
        throw const FormatException('duplicate candidate item');
      }

      final normalized = _deepCopy(input);
      normalized['authorized_at'] = _formatUtc(authorizedAt);
      final normalizedItems = normalized['items']! as List<Object?>;
      for (var index = 0; index < items.length; index += 1) {
        (normalizedItems[index]! as Map<String, Object?>)['captured_at'] =
            items[index].capturedAtWire;
      }
      if (_candidateIdentity(normalized) != candidateId) {
        throw const FormatException('candidate identity mismatch');
      }
      return ControlledUploadCandidate._(
        candidateId: candidateId,
        sourceReadinessId: sourceReadinessId,
        opcId: opcId,
        albumProductId: albumProductId,
        authorizedAt: authorizedAt,
        items: List<ControlledUploadCandidateItem>.unmodifiable(items),
        requestJson: normalized,
      );
    } on ControlledUploadException {
      rethrow;
    } on Object {
      throw const ControlledUploadException(
        code: 'invalid_candidate',
        safeMessage: '候选资料无效，请重新生成',
      );
    }
  }

  final String candidateId;
  final String sourceReadinessId;
  final String opcId;
  final String albumProductId;
  final DateTime authorizedAt;
  final List<ControlledUploadCandidateItem> items;
  final Map<String, Object?> _requestJson;

  String get candidateSha256 => candidateId.substring(_candidatePrefix.length);

  Set<String> get claimsNotEvaluated => Set<String>.unmodifiable(_claims);

  Map<String, Object?> toRequestJson() => _deepCopy(_requestJson);
}

final class CandidateUploadApproval {
  factory CandidateUploadApproval.approved({
    required String candidateId,
    required DateTime confirmedAt,
  }) {
    if (!_candidateIdPattern.hasMatch(candidateId) || !confirmedAt.isUtc) {
      throw const ControlledUploadException(
        code: 'human_confirmation_required',
        safeMessage: '请先人工确认候选图片',
      );
    }
    return CandidateUploadApproval._(
      candidateId: candidateId,
      confirmedAt: confirmedAt,
    );
  }

  const CandidateUploadApproval._({
    required this.candidateId,
    required this.confirmedAt,
  });

  final String candidateId;
  final DateTime confirmedAt;
  String get decision => 'approved_for_upload_queue';

  void requireCandidate(String expectedCandidateId) {
    if (candidateId != expectedCandidateId) {
      throw const ControlledUploadException(
        code: 'human_confirmation_required',
        safeMessage: '请先人工确认候选图片',
      );
    }
  }
}

ControlledUploadCandidateItem _parseItem(
  Map<String, Object?> input,
  int index,
) {
  final localAssetId = _requiredString(input, 'local_asset_id');
  final role = _requiredString(input, 'role');
  final position = input['position'];
  final filename = _requiredString(input, 'filename');
  final mediaType = _requiredString(input, 'media_type');
  final byteSize = input['byte_size'];
  final digest = _requiredString(input, 'sha256');
  final capturedAtWire = _requiredString(input, 'captured_at');
  if (!_canonicalUuidPattern.hasMatch(localAssetId) ||
      position is! int ||
      position != index + 1 ||
      role != (index == 0 ? 'primary' : 'detail') ||
      !const <String>{
        'image/jpeg',
        'image/png',
        'image/heic',
      }.contains(mediaType) ||
      byteSize is! int ||
      byteSize <= 0 ||
      byteSize > 104857600 ||
      !_sha256Pattern.hasMatch(digest)) {
    throw const FormatException('invalid candidate item facts');
  }
  _requireEquals(input, 'human_privacy_reviewed', true);
  _requireEquals(
    input,
    'privacy_metadata_status',
    'no_sensitive_metadata_detected',
  );
  _requireEquals(input, 'retry', false);
  final extension = switch (mediaType) {
    'image/jpeg' => 'jpg',
    'image/png' => 'png',
    'image/heic' => 'heic',
    _ => throw const FormatException('invalid media type'),
  };
  if (filename != '$localAssetId.$extension') {
    throw const FormatException('unsafe filename');
  }
  final capturedAt = _parseUtc(capturedAtWire);
  return ControlledUploadCandidateItem._(
    localAssetId: localAssetId,
    role: role,
    position: position,
    filename: filename,
    mediaType: mediaType,
    byteSize: byteSize,
    sha256: digest,
    capturedAt: capturedAt,
    capturedAtWire: _formatUtc(capturedAt),
  );
}

DateTime _parseUtc(String value) {
  final match = _utcTimestampPattern.firstMatch(value);
  if (match == null) {
    throw const FormatException('timestamp must use UTC Z form');
  }
  final year = int.parse(match.group(1)!);
  final month = int.parse(match.group(2)!);
  final day = int.parse(match.group(3)!);
  final hour = int.parse(match.group(4)!);
  final minute = int.parse(match.group(5)!);
  final second = int.parse(match.group(6)!);
  final fraction = match.group(7);
  final micros = fraction == null ? 0 : int.parse(fraction.padRight(6, '0'));
  final parsed = DateTime.utc(
    year,
    month,
    day,
    hour,
    minute,
    second,
    micros ~/ 1000,
    micros % 1000,
  );
  if (year < 1 ||
      parsed.year != year ||
      parsed.month != month ||
      parsed.day != day ||
      parsed.hour != hour ||
      parsed.minute != minute ||
      parsed.second != second ||
      parsed.millisecond * 1000 + parsed.microsecond != micros) {
    throw const FormatException('invalid UTC timestamp');
  }
  return parsed;
}

String _formatUtc(DateTime value) {
  final micros = value.millisecond * 1000 + value.microsecond;
  final base =
      '${value.year.toString().padLeft(4, '0')}-'
      '${value.month.toString().padLeft(2, '0')}-'
      '${value.day.toString().padLeft(2, '0')}T'
      '${value.hour.toString().padLeft(2, '0')}:'
      '${value.minute.toString().padLeft(2, '0')}:'
      '${value.second.toString().padLeft(2, '0')}';
  return micros == 0
      ? '${base}Z'
      : '$base.${micros.toString().padLeft(6, '0')}Z';
}

String _requiredStableId(Map<String, Object?> input, String key) {
  final value = _requiredString(input, key);
  if (!_stableIdPattern.hasMatch(value)) {
    throw const FormatException('invalid stable ID');
  }
  return value;
}

String _requiredString(Map<String, Object?> input, String key) {
  final value = input[key];
  if (value is! String || value.isEmpty) {
    throw const FormatException('missing string');
  }
  return value;
}

void _requireEquals(Map<String, Object?> input, String key, Object? expected) {
  if (input[key] != expected) {
    throw const FormatException('unexpected fixed value');
  }
}

void _requireExactKeys(Map<String, Object?> input, Set<String> expected) {
  if (input.keys.toSet().length != expected.length ||
      !input.keys.toSet().containsAll(expected)) {
    throw const FormatException('unexpected candidate fields');
  }
}

bool _sameOrderedValues(List<Object?> actual, List<String> expected) {
  for (var index = 0; index < expected.length; index += 1) {
    if (actual[index] != expected[index]) return false;
  }
  return true;
}

Map<String, Object?> _object(Object? value) {
  if (value is! Map<Object?, Object?>) {
    throw const FormatException('expected object');
  }
  return value.map((key, item) => MapEntry(key.toString(), item));
}

Map<String, Object?> _deepCopy(Map<String, Object?> input) =>
    _object(jsonDecode(jsonEncode(input)));

String _candidateIdentity(Map<String, Object?> normalized) {
  final body = Map<String, Object?>.of(normalized)..remove('candidate_id');
  final bytes = utf8.encode(jsonEncode(_canonicalValue(body)));
  return '$_candidatePrefix${sha256.convert(bytes)}';
}

Object? _canonicalValue(Object? value) => switch (value) {
  Map<Object?, Object?> map => _canonicalMap(map),
  List<Object?> list => list.map(_canonicalValue).toList(growable: false),
  _ => value,
};

SplayTreeMap<String, Object?> _canonicalMap(Map<Object?, Object?> input) =>
    SplayTreeMap<String, Object?>()..addEntries(
      input.entries.map(
        (entry) => MapEntry(entry.key.toString(), _canonicalValue(entry.value)),
      ),
    );
