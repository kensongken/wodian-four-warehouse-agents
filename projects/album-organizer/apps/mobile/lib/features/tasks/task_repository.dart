import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_client.dart';
import '../../core/api/api_error.dart';
import '../../core/api/models.dart';
import '../../core/auth/session_repository.dart';
import '../../core/media/local_asset_ref.dart';

final taskRepositoryProvider = Provider<TaskRepository>(
  (ref) => TaskRepository(ref.watch(sessionRepositoryProvider)),
);

abstract interface class UnsupportedAssetReporter {
  Future<UnsupportedReportReceipt> reportUnsupported(
    String taskId,
    UnsupportedLocalAsset asset,
  );
}

enum ServerAssetDisposition { accepted, unsupported }

final class UnsupportedReportReceipt {
  const UnsupportedReportReceipt({
    required this.localAssetId,
    required this.disposition,
  });

  factory UnsupportedReportReceipt.fromJson(Map<String, Object?> json) {
    final localAssetId = json['local_asset_id'];
    final status = json['status'];
    if (localAssetId is! String || localAssetId.isEmpty) {
      throw const FormatException('local_asset_id must be a non-empty string');
    }
    final disposition = switch (status) {
      'skipped_unsupported' => ServerAssetDisposition.unsupported,
      'pending' ||
      'uploading' ||
      'uploaded' ||
      'failed' => ServerAssetDisposition.accepted,
      _ => throw const FormatException('unsupported asset status'),
    };
    return UnsupportedReportReceipt(
      localAssetId: localAssetId,
      disposition: disposition,
    );
  }

  final String localAssetId;
  final ServerAssetDisposition disposition;
}

abstract interface class ImportTaskSummarySource {
  Future<TaskSummary> get(String taskId);
}

abstract interface class TaskSyncApi {
  Future<TaskSummary> get(String taskId);

  Future<TaskSummary> seal(String taskId);
}

class TaskRepository
    implements UnsupportedAssetReporter, ImportTaskSummarySource, TaskSyncApi {
  TaskRepository(this._session);

  final SessionRepository _session;

  Future<TaskSummary> create(String name) async {
    final response = await _session.authorizedPost(
      '/v1/tasks',
      data: <String, Object>{'name': _validatedName(name)},
    );
    return _taskFromResponse(response);
  }

  Future<List<TaskSummary>> list() async {
    final response = await _session.authorizedGet('/v1/tasks');
    _ensureSuccess(response);
    try {
      final json = _jsonObject(response.data);
      final items = json['items'];
      if (items is! List<Object?>) {
        throw const FormatException('items must be a list');
      }
      return items
          .map((item) => TaskSummary.fromJson(_jsonObject(item)))
          .toList(growable: false);
    } on FormatException {
      throw _invalidResponse();
    }
  }

  @override
  Future<TaskSummary> get(String taskId) async =>
      _taskFromResponse(await _session.authorizedGet('/v1/tasks/$taskId'));

  @override
  Future<TaskSummary> seal(String taskId) async => _taskFromResponse(
    await _session.authorizedPost('/v1/tasks/$taskId/seal'),
  );

  Future<TaskSummary> cancel(String taskId) async => _taskFromResponse(
    await _session.authorizedPost('/v1/tasks/$taskId/cancel'),
  );

  @override
  Future<UnsupportedReportReceipt> reportUnsupported(
    String taskId,
    UnsupportedLocalAsset asset,
  ) async {
    final response = await _session.authorizedPost(
      '/v1/tasks/$taskId/assets/unsupported',
      data: <String, Object>{
        'local_asset_id': asset.id,
        'filename': asset.displayName,
        'reason': asset.reason,
        'media_type': ?asset.mediaType,
        'byte_size': ?asset.byteSize,
      },
    );
    _ensureSuccess(response);
    try {
      return UnsupportedReportReceipt.fromJson(_jsonObject(response.data));
    } on FormatException {
      throw _invalidResponse();
    }
  }

  static TaskSummary _taskFromResponse(ApiResponse response) {
    _ensureSuccess(response);
    try {
      return TaskSummary.fromJson(_jsonObject(response.data));
    } on FormatException {
      throw _invalidResponse();
    }
  }

  static void _ensureSuccess(ApiResponse response) {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw ApiException.fromResponse(response);
    }
  }

  static String _validatedName(String value) {
    final name = value.trim();
    if (name.isEmpty || name.length > 80) {
      throw const FormatException('任务名称必须为 1 到 80 个字符');
    }
    return name;
  }

  static ApiException _invalidResponse() =>
      const ApiException(code: 'invalid_response', userMessage: '服务返回异常，请稍后重试');

  static Map<String, Object?> _jsonObject(Object? data) {
    if (data case final Map<Object?, Object?> map) {
      return map.map((key, value) => MapEntry(key.toString(), value));
    }
    throw const FormatException('API response must be a JSON object');
  }
}
