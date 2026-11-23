final class TokenPair {
  const TokenPair({
    required this.accessToken,
    required this.refreshToken,
    required this.expiresAt,
    required this.deviceId,
  });

  factory TokenPair.fromJson(
    Map<String, Object?> json, {
    required String deviceId,
    DateTime? now,
  }) {
    final expiresInSeconds = json['expires_in_seconds'];
    if (expiresInSeconds is! int) {
      throw const FormatException('expires_in_seconds must be an integer');
    }
    return TokenPair(
      accessToken: _requiredString(json, 'access_token'),
      refreshToken: _requiredString(json, 'refresh_token'),
      expiresAt: (now ?? DateTime.now()).toUtc().add(
        Duration(seconds: expiresInSeconds),
      ),
      deviceId: deviceId,
    );
  }

  final String accessToken;
  final String refreshToken;
  final DateTime expiresAt;
  final String deviceId;
}

abstract final class TaskStatusWire {
  static const waitingUpload = 'waiting_upload';
  static const uploading = 'uploading';
  static const aiProcessing = 'ai_processing';
  static const waitingReview = 'waiting_review';
  static const completed = 'completed';
  static const paused = 'paused';
  static const partialFailure = 'partial_failure';
  static const cancelled = 'cancelled';

  static const all = <String>{
    waitingUpload,
    uploading,
    aiProcessing,
    waitingReview,
    completed,
    paused,
    partialFailure,
    cancelled,
  };

  static const uploadOpen = <String>{waitingUpload, uploading};
  static const uploadPaused = <String>{paused};
  static const uploadClosed = <String>{
    aiProcessing,
    waitingReview,
    completed,
    partialFailure,
    cancelled,
  };
}

final class TaskSummary {
  const TaskSummary({
    required this.id,
    required this.name,
    required this.status,
    required this.totalAssets,
    required this.uploadedAssets,
    required this.failedAssets,
    required this.unsupportedAssets,
    required this.createdAt,
    required this.updatedAt,
  });

  factory TaskSummary.fromJson(Map<String, Object?> json) => TaskSummary(
    id: _requiredString(json, 'id'),
    name: _requiredString(json, 'name'),
    status: _requiredString(json, 'status'),
    totalAssets: _requiredInt(json, 'total_assets'),
    uploadedAssets: _requiredInt(json, 'uploaded_assets'),
    failedAssets: _requiredInt(json, 'failed_assets'),
    unsupportedAssets: _requiredInt(json, 'unsupported_assets'),
    createdAt: DateTime.parse(_requiredString(json, 'created_at')),
    updatedAt: DateTime.parse(_requiredString(json, 'updated_at')),
  );

  final String id;
  final String name;
  final String status;
  final int totalAssets;
  final int uploadedAssets;
  final int failedAssets;
  final int unsupportedAssets;
  final DateTime createdAt;
  final DateTime updatedAt;
}

String _requiredString(Map<String, Object?> json, String key) {
  final value = json[key];
  if (value is! String || value.isEmpty) {
    throw FormatException('$key must be a non-empty string');
  }
  return value;
}

int _requiredInt(Map<String, Object?> json, String key) {
  final value = json[key];
  if (value is! int) {
    throw FormatException('$key must be an integer');
  }
  return value;
}
