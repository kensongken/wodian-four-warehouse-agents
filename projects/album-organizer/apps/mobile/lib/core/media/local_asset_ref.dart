enum AssetAccessKind { stagedFile, contentUri }

final class LocalAssetRef {
  const LocalAssetRef({
    required this.id,
    required this.displayName,
    required this.mediaType,
    required this.byteSize,
    required this.accessKind,
    required this.locator,
    this.capturedAt,
  });

  factory LocalAssetRef.fromMap(Map<String, Object?> map) {
    final capturedAtValue = map['capturedAt'];
    DateTime? capturedAt;
    if (capturedAtValue != null) {
      if (capturedAtValue is! String) {
        throw const FormatException('capturedAt must be an ISO-8601 string');
      }
      capturedAt = DateTime.tryParse(capturedAtValue)?.toUtc();
      if (capturedAt == null) {
        throw const FormatException('capturedAt must be an ISO-8601 string');
      }
    }
    final accessKindValue = _requiredString(map, 'accessKind');
    final accessKind = switch (accessKindValue) {
      'stagedFile' => AssetAccessKind.stagedFile,
      'contentUri' => AssetAccessKind.contentUri,
      _ => throw const FormatException('unsupported accessKind'),
    };
    final mediaType = _requiredString(map, 'mediaType');
    if (!const <String>{
      'image/heic',
      'image/jpeg',
      'image/png',
    }.contains(mediaType)) {
      throw const FormatException('unsupported selected mediaType');
    }
    final byteSize = _requiredInt(map, 'byteSize');
    if (byteSize <= 0) {
      throw const FormatException('byteSize must be positive');
    }
    return LocalAssetRef(
      id: _requiredString(map, 'id'),
      displayName: _requiredString(map, 'displayName'),
      mediaType: mediaType,
      byteSize: byteSize,
      capturedAt: capturedAt,
      accessKind: accessKind,
      locator: _requiredString(map, 'locator'),
    );
  }

  final String id;
  final String displayName;
  final String mediaType;
  final int byteSize;
  final DateTime? capturedAt;
  final AssetAccessKind accessKind;
  final String locator;

  Map<String, Object?> toMap() => <String, Object?>{
    'id': id,
    'displayName': displayName,
    'mediaType': mediaType,
    'byteSize': byteSize,
    'capturedAt': capturedAt?.toUtc().toIso8601String(),
    'accessKind': switch (accessKind) {
      AssetAccessKind.stagedFile => 'stagedFile',
      AssetAccessKind.contentUri => 'contentUri',
    },
    'locator': locator,
  };
}

final class UnsupportedLocalAsset {
  const UnsupportedLocalAsset({
    required this.id,
    required this.displayName,
    required this.reason,
    this.mediaType,
    this.byteSize,
  });

  factory UnsupportedLocalAsset.fromMap(Map<String, Object?> map) {
    final mediaType = map['mediaType'];
    final byteSize = map['byteSize'];
    if (mediaType != null && mediaType is! String) {
      throw const FormatException('mediaType must be a string');
    }
    if (byteSize != null && (byteSize is! int || byteSize < 0)) {
      throw const FormatException('byteSize must be a non-negative integer');
    }
    final reason = _requiredString(map, 'reason');
    if (!const <String>{
      'unsupported_media_type',
      'image_too_large',
      'read_failure',
    }.contains(reason)) {
      throw const FormatException('unsupported reason');
    }
    return UnsupportedLocalAsset(
      id: _requiredString(map, 'id'),
      displayName: _requiredStringEither(map, 'displayName', 'name'),
      reason: reason,
      mediaType: mediaType as String?,
      byteSize: byteSize as int?,
    );
  }

  final String id;
  final String displayName;
  final String reason;
  final String? mediaType;
  final int? byteSize;

  Map<String, Object?> toReportMap() => <String, Object?>{
    'local_asset_id': id,
    'filename': displayName,
    'media_type': mediaType,
    'byte_size': byteSize,
    'reason': reason,
  };
}

final class MediaPickResult {
  const MediaPickResult({required this.selected, required this.unsupported});

  final List<LocalAssetRef> selected;
  final List<UnsupportedLocalAsset> unsupported;
}

String _requiredString(Map<String, Object?> map, String key) {
  final value = map[key];
  if (value is! String || value.isEmpty) {
    throw FormatException('$key must be a non-empty string');
  }
  return value;
}

String _requiredStringEither(
  Map<String, Object?> map,
  String first,
  String second,
) {
  final value = map[first] ?? map[second];
  if (value is! String || value.isEmpty) {
    throw FormatException('$first must be a non-empty string');
  }
  return value;
}

int _requiredInt(Map<String, Object?> map, String key) {
  final value = map[key];
  if (value is! int) {
    throw FormatException('$key must be an integer');
  }
  return value;
}
