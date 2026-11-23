enum OpcRole { owner, reviewer }

enum ProductSourceType {
  firstSaleBead('first_sale_bead', '首发珠'),
  legacyMaterial('legacy_material', '旧珠入件'),
  redesignOutput('redesign_output', '改款成品');

  const ProductSourceType(this.wireValue, this.label);

  final String wireValue;
  final String label;
}

enum ReviewStatus {
  open('open'),
  resolved('resolved');

  const ReviewStatus(this.wireValue);

  final String wireValue;
}

const level1Categories = <String>{
  '南红',
  '沉香',
  '翡翠',
  '和田玉',
  '蜜蜡',
  '珍珠',
  '水晶',
  '彩宝',
  '贵金属',
  '其他',
  '无法判断',
};

const level2Categories = <String>{
  '手串',
  '项链',
  '吊坠',
  '挂件',
  '戒指',
  '耳饰',
  '手镯',
  '珠子/散珠',
  '摆件',
  '配件',
  '其他',
  '无法判断',
};

const _reasonCodes = <String>{
  'seed_match',
  'visual_pattern',
  'shape_pattern',
  'insufficient_evidence',
};

const _claimBoundary = <String>[
  'authenticity',
  'origin',
  'quality',
  'warehouse_acceptance',
];

final RegExp _stableId = RegExp(r'^[A-Za-z0-9][A-Za-z0-9._:-]+$');
final RegExp _uuid = RegExp(
  r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
  caseSensitive: false,
);
final RegExp _timezoneSuffix = RegExp(r'(?:[zZ]|[+-]\d{2}:\d{2})$');

final class OpcMembershipSummary {
  const OpcMembershipSummary({
    required this.id,
    required this.displayName,
    required this.role,
  });

  factory OpcMembershipSummary.fromJson(Map<String, Object?> json) {
    final displayName = _requiredString(json, 'display_name');
    if (displayName.trim().isEmpty || displayName.length > 128) {
      throw const FormatException('invalid display_name');
    }
    return OpcMembershipSummary(
      id: _requiredStableId(json, 'id'),
      displayName: displayName,
      role: switch (_requiredString(json, 'role')) {
        'owner' => OpcRole.owner,
        'reviewer' => OpcRole.reviewer,
        _ => throw const FormatException('invalid OPC role'),
      },
    );
  }

  final String id;
  final String displayName;
  final OpcRole role;
}

final class ClassificationCandidate {
  const ClassificationCandidate({
    required this.rank,
    required this.level1,
    required this.level2,
    required this.confidence,
    required this.reasonCodes,
  });

  factory ClassificationCandidate.fromJson(Map<String, Object?> json) {
    final rank = _requiredInt(json, 'rank');
    final level1 = _requiredString(json, 'level_1');
    final level2 = _requiredString(json, 'level_2');
    final confidenceValue = json['confidence'];
    final reasonValues = json['reason_codes'];
    if (rank < 1 ||
        rank > 3 ||
        !level1Categories.contains(level1) ||
        !level2Categories.contains(level2) ||
        confidenceValue is! num ||
        !confidenceValue.isFinite ||
        confidenceValue < 0 ||
        confidenceValue > 1 ||
        reasonValues is! List<Object?> ||
        reasonValues.isEmpty ||
        reasonValues.length > 4) {
      throw const FormatException('invalid classification candidate');
    }
    final reasonCodes = reasonValues
        .map((value) {
          if (value is! String || !_reasonCodes.contains(value)) {
            throw const FormatException('invalid reason code');
          }
          return value;
        })
        .toList(growable: false);
    if (reasonCodes.toSet().length != reasonCodes.length) {
      throw const FormatException('duplicate reason code');
    }
    return ClassificationCandidate(
      rank: rank,
      level1: level1,
      level2: level2,
      confidence: confidenceValue.toDouble(),
      reasonCodes: reasonCodes,
    );
  }

  final int rank;
  final String level1;
  final String level2;
  final double confidence;
  final List<String> reasonCodes;
}

final class ClassificationReview {
  const ClassificationReview({
    required this.id,
    required this.opcId,
    required this.classificationId,
    required this.albumProductId,
    required this.sourceType,
    required this.inputSnapshotVersion,
    required this.classificationVersion,
    required this.candidates,
    required this.status,
    required this.confirmedLevel1,
    required this.confirmedLevel2,
    required this.confirmedBy,
    required this.decidedAt,
    required this.createdAt,
    required this.updatedAt,
  });

  factory ClassificationReview.fromJson(Map<String, Object?> json) {
    _requireNullIdentity(json, 'mall_product_id');
    _requireNullIdentity(json, 'bead_id');
    _validateClaimBoundary(json['claims_not_evaluated']);
    final candidateValues = json['candidates'];
    if (candidateValues is! List<Object?> ||
        candidateValues.isEmpty ||
        candidateValues.length > 3) {
      throw const FormatException('invalid candidate count');
    }
    final candidates = candidateValues
        .map((value) => ClassificationCandidate.fromJson(_jsonObject(value)))
        .toList(growable: false);
    for (var index = 0; index < candidates.length; index += 1) {
      if (candidates[index].rank != index + 1) {
        throw const FormatException('candidate ranks must be ordered');
      }
    }
    final inputSnapshotVersion = _requiredInt(json, 'input_snapshot_version');
    final classificationVersion = _requiredInt(json, 'classification_version');
    if (inputSnapshotVersion < 1 || classificationVersion < 1) {
      throw const FormatException('classification versions must be positive');
    }
    final status = switch (_requiredString(json, 'status')) {
      'open' => ReviewStatus.open,
      'resolved' => ReviewStatus.resolved,
      _ => throw const FormatException('invalid review status'),
    };
    final confirmedLevel1 = _nullableCategory(
      json,
      'confirmed_level_1',
      level1Categories,
    );
    final confirmedLevel2 = _nullableCategory(
      json,
      'confirmed_level_2',
      level2Categories,
    );
    final confirmedBy = _nullableStableId(json, 'confirmed_by');
    final decidedAt = _nullableDateTime(json, 'decided_at');
    final hasDecision =
        confirmedLevel1 != null &&
        confirmedLevel2 != null &&
        confirmedBy != null &&
        decidedAt != null;
    if ((status == ReviewStatus.open &&
            (confirmedLevel1 != null ||
                confirmedLevel2 != null ||
                confirmedBy != null ||
                decidedAt != null)) ||
        (status == ReviewStatus.resolved && !hasDecision)) {
      throw const FormatException('review decision does not match status');
    }
    return ClassificationReview(
      id: _requiredUuid(json, 'id'),
      opcId: _requiredStableId(json, 'opc_id'),
      classificationId: _requiredUuid(json, 'classification_id'),
      albumProductId: _requiredStableId(json, 'album_product_id'),
      sourceType: switch (_requiredString(json, 'source_type')) {
        'first_sale_bead' => ProductSourceType.firstSaleBead,
        'legacy_material' => ProductSourceType.legacyMaterial,
        'redesign_output' => ProductSourceType.redesignOutput,
        _ => throw const FormatException('invalid source type'),
      },
      inputSnapshotVersion: inputSnapshotVersion,
      classificationVersion: classificationVersion,
      candidates: candidates,
      status: status,
      confirmedLevel1: confirmedLevel1,
      confirmedLevel2: confirmedLevel2,
      confirmedBy: confirmedBy,
      decidedAt: decidedAt,
      createdAt: _requiredDateTime(json, 'created_at'),
      updatedAt: _requiredDateTime(json, 'updated_at'),
    );
  }

  final String id;
  final String opcId;
  final String classificationId;
  final String albumProductId;
  final ProductSourceType sourceType;
  final int inputSnapshotVersion;
  final int classificationVersion;
  final List<ClassificationCandidate> candidates;
  final ReviewStatus status;
  final String? confirmedLevel1;
  final String? confirmedLevel2;
  final String? confirmedBy;
  final DateTime? decidedAt;
  final DateTime createdAt;
  final DateTime updatedAt;
}

final class ClassificationReviewPage {
  const ClassificationReviewPage({required this.items, this.nextBefore});

  final List<ClassificationReview> items;
  final String? nextBefore;
}

Map<String, Object?> reviewJsonObject(Object? value) => _jsonObject(value);

String _requiredString(Map<String, Object?> json, String key) {
  final value = json[key];
  if (value is! String || value.isEmpty) {
    throw FormatException('$key must be a non-empty string');
  }
  return value;
}

int _requiredInt(Map<String, Object?> json, String key) {
  final value = json[key];
  if (value is! int) throw FormatException('$key must be an integer');
  return value;
}

String _requiredStableId(Map<String, Object?> json, String key) {
  final value = _requiredString(json, key);
  if (value.length < 3 || value.length > 128 || !_stableId.hasMatch(value)) {
    throw FormatException('$key must be a stable id');
  }
  return value;
}

String _requiredUuid(Map<String, Object?> json, String key) {
  final value = _requiredString(json, key);
  if (!_uuid.hasMatch(value)) throw FormatException('$key must be a UUID');
  return value;
}

String? _nullableStableId(Map<String, Object?> json, String key) {
  if (!json.containsKey(key) || json[key] == null) return null;
  return _requiredStableId(json, key);
}

String? _nullableCategory(
  Map<String, Object?> json,
  String key,
  Set<String> allowed,
) {
  if (!json.containsKey(key) || json[key] == null) return null;
  final value = _requiredString(json, key);
  if (!allowed.contains(value)) throw FormatException('invalid $key');
  return value;
}

DateTime _requiredDateTime(Map<String, Object?> json, String key) {
  final value = _requiredString(json, key);
  final parsed = DateTime.tryParse(value);
  if (parsed == null || !_timezoneSuffix.hasMatch(value)) {
    throw FormatException('$key must include a timezone');
  }
  return parsed.toUtc();
}

DateTime? _nullableDateTime(Map<String, Object?> json, String key) {
  if (!json.containsKey(key) || json[key] == null) return null;
  return _requiredDateTime(json, key);
}

void _requireNullIdentity(Map<String, Object?> json, String key) {
  if (!json.containsKey(key) || json[key] != null) {
    throw FormatException('$key must be explicitly null');
  }
}

void _validateClaimBoundary(Object? value) {
  if (value is! List<Object?> || value.length != _claimBoundary.length) {
    throw const FormatException('invalid claim boundary');
  }
  for (var index = 0; index < _claimBoundary.length; index += 1) {
    if (value[index] != _claimBoundary[index]) {
      throw const FormatException('invalid claim boundary');
    }
  }
}

Map<String, Object?> _jsonObject(Object? value) {
  if (value case final Map<Object?, Object?> map) {
    return map.map((key, item) => MapEntry(key.toString(), item));
  }
  throw const FormatException('response must be a JSON object');
}
