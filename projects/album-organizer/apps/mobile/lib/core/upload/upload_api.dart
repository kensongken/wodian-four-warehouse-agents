import '../media/local_asset_ref.dart';
import '../api/api_client.dart';
import '../api/api_error.dart';
import '../auth/session_repository.dart';
import 'upload_models.dart';

abstract interface class UploadApi {
  Future<InitiatedAsset> initiate(
    String taskId,
    PendingLocalAsset asset, {
    bool retry = false,
  });

  Future<Map<int, Uri>> signParts(String assetId, List<int> partNumbers);

  Future<void> complete(String assetId, Map<int, String> etags);

  Future<void> reportFailure(String assetId, String code, String safeMessage);

  Future<void> reportUnsupported(String taskId, UnsupportedLocalAsset asset);
}

final class PendingLocalAsset {
  const PendingLocalAsset({
    required this.asset,
    required this.sha256,
    this.authority,
  });

  final LocalAssetRef asset;
  final String sha256;
  final UploadAuthority? authority;
}

enum InitiatedAssetDisposition { accepted, duplicate }

final class InitiatedAsset {
  const InitiatedAsset({
    required this.assetId,
    required this.alreadyUploaded,
    required this.partSize,
    required this.completedPartNumbers,
    this.providerUploadId,
    this.disposition = InitiatedAssetDisposition.accepted,
    this.canonicalAssetId,
  });

  final String assetId;
  final bool alreadyUploaded;
  final String? providerUploadId;
  final int partSize;
  final List<int> completedPartNumbers;
  final InitiatedAssetDisposition disposition;
  final String? canonicalAssetId;
}

final class UploadApiException implements Exception {
  const UploadApiException({
    required this.code,
    required this.safeMessage,
    this.statusCode,
    this.authorizationAlreadyRetried = false,
  });

  final String code;
  final String safeMessage;
  final int? statusCode;
  final bool authorizationAlreadyRetried;
}

final class SessionUploadApi implements UploadApi {
  SessionUploadApi(this._session);

  final SessionRepository _session;

  @override
  Future<InitiatedAsset> initiate(
    String taskId,
    PendingLocalAsset asset, {
    bool retry = false,
  }) async {
    final ref = asset.asset;
    final authority = asset.authority;
    final request = <String, Object?>{
      'local_asset_id': ref.id,
      'filename': ref.displayName,
      'media_type': ref.mediaType,
      'byte_size': ref.byteSize,
      'sha256': asset.sha256,
      'captured_at': ref.capturedAt?.toUtc().toIso8601String(),
      'retry': retry,
    };
    if (authority != null) {
      request['binding_id'] = authority.bindingId;
      request['candidate_id'] = authority.candidateId;
    }
    final response = await _post('/v1/tasks/$taskId/assets/initiate', request);
    try {
      final json = _jsonObject(response.data);
      final assetId = _requiredString(json, 'asset_id');
      final alreadyUploaded = json['already_uploaded'];
      final providerUploadId = json['provider_upload_id'];
      final partSize = json['part_size'];
      final completedParts = json['completed_parts'];
      final rawDisposition = json['disposition'];
      final canonicalAssetId = json['canonical_asset_id'];
      if (alreadyUploaded is! bool ||
          providerUploadId != null && providerUploadId is! String ||
          partSize is! int ||
          partSize <= 0 ||
          completedParts is! List<Object?>) {
        throw const FormatException('invalid initiation response');
      }
      final numbers = completedParts
          .map((value) {
            if (value is! int || value <= 0) {
              throw const FormatException('invalid completed part number');
            }
            return value;
          })
          .toList(growable: false);
      if (numbers.toSet().length != numbers.length) {
        throw const FormatException('duplicate completed part number');
      }
      final disposition = switch (rawDisposition) {
        null || 'accepted' => InitiatedAssetDisposition.accepted,
        'duplicate' => InitiatedAssetDisposition.duplicate,
        _ => throw const FormatException('invalid initiation disposition'),
      };
      if (canonicalAssetId != null &&
              (canonicalAssetId is! String || canonicalAssetId.isEmpty) ||
          disposition == InitiatedAssetDisposition.accepted &&
              canonicalAssetId != null ||
          disposition == InitiatedAssetDisposition.duplicate &&
              (alreadyUploaded != true ||
                  providerUploadId != null ||
                  numbers.isNotEmpty ||
                  canonicalAssetId == null ||
                  canonicalAssetId == assetId)) {
        throw const FormatException('invalid duplicate provenance');
      }
      return InitiatedAsset(
        assetId: assetId,
        alreadyUploaded: alreadyUploaded,
        providerUploadId: providerUploadId as String?,
        partSize: partSize,
        completedPartNumbers: List<int>.unmodifiable(numbers),
        disposition: disposition,
        canonicalAssetId: canonicalAssetId as String?,
      );
    } on FormatException {
      throw const UploadApiException(
        code: 'invalid_response',
        safeMessage: '服务返回异常，请稍后重试',
      );
    }
  }

  @override
  Future<Map<int, Uri>> signParts(String assetId, List<int> partNumbers) async {
    final entries = await Future.wait<MapEntry<int, Uri>>(
      partNumbers.map((partNumber) async {
        final response = await _post(
          '/v1/assets/$assetId/parts/sign',
          <String, Object>{'part_number': partNumber},
        );
        try {
          final json = _jsonObject(response.data);
          final rawUrl = _requiredString(json, 'url');
          final uri = Uri.tryParse(rawUrl);
          final expiresSeconds = json['expires_seconds'];
          if (uri == null ||
              uri.scheme != 'https' ||
              uri.host.isEmpty ||
              expiresSeconds is! int ||
              expiresSeconds <= 0) {
            throw const FormatException('invalid signed part response');
          }
          return MapEntry<int, Uri>(partNumber, uri);
        } on FormatException {
          throw const UploadApiException(
            code: 'invalid_response',
            safeMessage: '服务返回异常，请稍后重试',
          );
        }
      }),
    );
    return Map<int, Uri>.unmodifiable(Map<int, Uri>.fromEntries(entries));
  }

  @override
  Future<void> complete(String assetId, Map<int, String> etags) async {
    final parts = etags.entries.toList()
      ..sort((first, second) => first.key.compareTo(second.key));
    await _post('/v1/assets/$assetId/complete', <String, Object>{
      'parts': <Map<String, Object>>[
        for (final entry in parts)
          <String, Object>{'part_number': entry.key, 'etag': entry.value},
      ],
    });
  }

  @override
  Future<void> reportFailure(
    String assetId,
    String code,
    String safeMessage,
  ) async {
    await _post('/v1/assets/$assetId/fail', <String, Object>{
      'code': code,
      'message': safeMessage,
    });
  }

  @override
  Future<void> reportUnsupported(
    String taskId,
    UnsupportedLocalAsset asset,
  ) async {
    await _post('/v1/tasks/$taskId/assets/unsupported', asset.toReportMap());
  }

  Future<ApiResponse> _post(String path, Object data) async {
    try {
      final response = await _session.authorizedPost(path, data: data);
      if (response.statusCode >= 200 && response.statusCode < 300) {
        return response;
      }
      final error = ApiException.fromResponse(response);
      throw UploadApiException(
        code: error.code,
        safeMessage: error.userMessage,
        statusCode: response.statusCode,
      );
    } on UploadApiException {
      rethrow;
    } on ApiException catch (error) {
      throw UploadApiException(
        code: error.code,
        safeMessage: error.userMessage,
        statusCode: error.code == 'invalid_refresh_token' ? 401 : null,
        authorizationAlreadyRetried: true,
      );
    } catch (error) {
      final normalized = ApiException.fromTransport(error);
      throw UploadApiException(
        code: normalized.code,
        safeMessage: normalized.userMessage,
      );
    }
  }
}

Map<String, Object?> _jsonObject(Object? value) {
  if (value case final Map<Object?, Object?> map) {
    return map.map((key, value) => MapEntry(key.toString(), value));
  }
  throw const FormatException('response must be a JSON object');
}

String _requiredString(Map<String, Object?> json, String key) {
  final value = json[key];
  if (value is! String || value.isEmpty) {
    throw FormatException('$key must be a non-empty string');
  }
  return value;
}
