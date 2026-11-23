import 'dart:async';

import 'package:photo_organizer/core/api/api_client.dart';
import 'package:photo_organizer/core/api/models.dart';
import 'package:photo_organizer/core/auth/token_store.dart';
import 'package:photo_organizer/core/media/local_asset_ref.dart';
import 'package:photo_organizer/features/tasks/task_repository.dart';

final class RecordedCall {
  const RecordedCall(
    this.method,
    this.path,
    this.data,
    this.headers,
    this.query,
  );

  final String method;
  final String path;
  final Object? data;
  final Map<String, String> headers;
  final Map<String, Object?> query;
}

final class _QueuedResponse {
  const _QueuedResponse({
    required this.expectedMethod,
    required this.expectedPath,
    required this.response,
    this.gate,
    this.called,
  });

  final String expectedMethod;
  final String expectedPath;
  final ApiResponse response;
  final Future<void>? gate;
  final Completer<void>? called;
}

final class FakeTransport implements ApiTransport {
  final List<_QueuedResponse> _responses = <_QueuedResponse>[];
  final List<RecordedCall> calls = <RecordedCall>[];

  void enqueue({
    required String method,
    required String path,
    required int status,
    Future<void>? gate,
    Completer<void>? called,
  }) {
    _responses.add(
      _QueuedResponse(
        expectedMethod: method,
        expectedPath: path,
        response: ApiResponse(statusCode: status),
        gate: gate,
        called: called,
      ),
    );
  }

  void enqueueJson(
    String path,
    Object json, {
    required String method,
    int status = 200,
    Future<void>? gate,
    Completer<void>? called,
    Map<String, String> headers = const <String, String>{},
  }) {
    _responses.add(
      _QueuedResponse(
        expectedMethod: method,
        expectedPath: path,
        response: ApiResponse(statusCode: status, data: json, headers: headers),
        gate: gate,
        called: called,
      ),
    );
  }

  int callsTo(String path) => calls.where((call) => call.path == path).length;

  @override
  Future<ApiResponse> request(
    String method,
    String path, {
    Object? data,
    Map<String, String> headers = const <String, String>{},
    Map<String, Object?> query = const <String, Object?>{},
  }) async {
    calls.add(
      RecordedCall(
        method,
        path,
        data,
        Map<String, String>.of(headers),
        Map<String, Object?>.of(query),
      ),
    );
    if (_responses.isEmpty) {
      throw StateError('No response queued for $method $path');
    }
    final queued = _responses.first;
    if (queued.expectedMethod != method || queued.expectedPath != path) {
      throw StateError(
        'Expected ${queued.expectedMethod} ${queued.expectedPath}, '
        'received $method $path',
      );
    }
    _responses.removeAt(0);
    queued.called?.complete();
    await queued.gate;
    return queued.response;
  }
}

final class MemoryTokenStore implements TokenStore {
  MemoryTokenStore({
    String? accessToken,
    String? refreshToken,
    String deviceId = 'device-12345678',
  }) : _pair = refreshToken == null
           ? null
           : TokenPair(
               accessToken: accessToken ?? 'expired-access',
               refreshToken: refreshToken,
               expiresAt: DateTime.utc(2030),
               deviceId: deviceId,
             );

  TokenPair? _pair;
  int clearCount = 0;

  @override
  Future<void> clear() async {
    clearCount += 1;
    _pair = null;
  }

  @override
  Future<TokenPair?> read() async => _pair;

  @override
  Future<void> write(TokenPair pair) async {
    _pair = pair;
  }
}

final class FakeTaskRepository implements TaskRepository {
  FakeTaskRepository({List<TaskSummary> items = const <TaskSummary>[]})
    : _items = List<TaskSummary>.of(items);

  final List<TaskSummary> _items;
  final List<String> createdNames = <String>[];
  int listCalls = 0;

  @override
  Future<TaskSummary> cancel(String taskId) async => get(taskId);

  @override
  Future<TaskSummary> create(String name) async {
    createdNames.add(name);
    final now = DateTime.utc(2026, 8, 14);
    final task = TaskSummary(
      id: 'created-${createdNames.length}',
      name: name,
      status: 'waiting_upload',
      totalAssets: 0,
      uploadedAssets: 0,
      failedAssets: 0,
      unsupportedAssets: 0,
      createdAt: now,
      updatedAt: now,
    );
    _items.insert(0, task);
    return task;
  }

  @override
  Future<TaskSummary> get(String taskId) async =>
      _items.singleWhere((task) => task.id == taskId);

  @override
  Future<UnsupportedReportReceipt> reportUnsupported(
    String taskId,
    UnsupportedLocalAsset asset,
  ) async => UnsupportedReportReceipt(
    localAssetId: asset.id,
    disposition: ServerAssetDisposition.unsupported,
  );

  @override
  Future<List<TaskSummary>> list() async {
    listCalls += 1;
    return List<TaskSummary>.unmodifiable(_items);
  }

  @override
  Future<TaskSummary> seal(String taskId) async => get(taskId);
}
