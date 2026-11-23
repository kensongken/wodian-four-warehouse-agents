import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/api_error.dart';
import 'package:photo_organizer/core/auth/session_repository.dart';
import 'package:photo_organizer/core/media/local_asset_ref.dart';
import 'package:photo_organizer/features/tasks/task_repository.dart';

import '../../helpers/fakes.dart';

const taskId = '11111111-1111-4111-8111-111111111111';
const taskJson = <String, Object>{
  'id': taskId,
  'name': '8月看货',
  'status': 'uploading',
  'total_assets': 1268,
  'uploaded_assets': 923,
  'failed_assets': 4,
  'unsupported_assets': 2,
  'created_at': '2026-08-14T01:02:03Z',
  'updated_at': '2026-08-14T04:05:06Z',
};

void main() {
  test('create sends the exact method path and trimmed body', () async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/tasks', taskJson, method: 'POST', status: 201);
    final repository = _repository(transport);

    final created = await repository.create('  8月看货  ');

    expect(created.id, taskId);
    expect(transport.calls, hasLength(1));
    expect(transport.calls.single.method, 'POST');
    expect(transport.calls.single.path, '/v1/tasks');
    expect(transport.calls.single.data, <String, Object>{'name': '8月看货'});
    expect(
      transport.calls.single.headers['Authorization'],
      'Bearer test-access-token',
    );
  });

  test('list parses the exact tenant-scoped task model', () async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/tasks', <String, Object?>{
        'items': <Object>[taskJson],
        'next_before': null,
      }, method: 'GET');
    final repository = _repository(transport);

    final tasks = await repository.list();

    expect(tasks, hasLength(1));
    final task = tasks.single;
    expect(task.id, taskId);
    expect(task.name, '8月看货');
    expect(task.status, 'uploading');
    expect(task.totalAssets, 1268);
    expect(task.uploadedAssets, 923);
    expect(task.failedAssets, 4);
    expect(task.unsupportedAssets, 2);
    expect(task.createdAt, DateTime.utc(2026, 8, 14, 1, 2, 3));
    expect(task.updatedAt, DateTime.utc(2026, 8, 14, 4, 5, 6));
    expect(transport.calls, hasLength(1));
    expect(transport.calls.single.method, 'GET');
    expect(transport.calls.single.path, '/v1/tasks');
  });

  test('get seal and cancel use exact task-owned paths', () async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/tasks/$taskId', taskJson, method: 'GET')
      ..enqueueJson('/v1/tasks/$taskId/seal', taskJson, method: 'POST')
      ..enqueueJson('/v1/tasks/$taskId/cancel', taskJson, method: 'POST');
    final repository = _repository(transport);

    await repository.get(taskId);
    await repository.seal(taskId);
    await repository.cancel(taskId);

    expect(
      transport.calls.map((call) => '${call.method} ${call.path}'),
      <String>[
        'GET /v1/tasks/$taskId',
        'POST /v1/tasks/$taskId/seal',
        'POST /v1/tasks/$taskId/cancel',
      ],
    );
    expect(transport.calls.every((call) => call.data == null), isTrue);
  });

  test(
    'reports unsupported assets with the exact idempotent API contract',
    () async {
      final transport = FakeTransport()
        ..enqueueJson(
          '/v1/tasks/$taskId/assets/unsupported',
          <String, Object?>{
            'id': '22222222-2222-4222-8222-222222222222',
            'task_id': taskId,
            'local_asset_id': 'android:raw',
            'filename': 'RAW_1.dng',
            'media_type': 'image/x-adobe-dng',
            'byte_size': 55,
            'sha256': null,
            'status': 'skipped_unsupported',
            'unsupported_reason': 'unsupported_media_type',
            'failure_code': null,
            'failure_message': null,
          },
          method: 'POST',
          status: 201,
        );
      final repository = _repository(transport);

      final receipt = await repository.reportUnsupported(
        taskId,
        const UnsupportedLocalAsset(
          id: 'android:raw',
          displayName: 'RAW_1.dng',
          reason: 'unsupported_media_type',
          mediaType: 'image/x-adobe-dng',
          byteSize: 55,
        ),
      );

      expect(transport.calls.single.method, 'POST');
      expect(receipt.localAssetId, 'android:raw');
      expect(receipt.disposition, ServerAssetDisposition.unsupported);
      expect(
        transport.calls.single.path,
        '/v1/tasks/$taskId/assets/unsupported',
      );
      expect(transport.calls.single.data, <String, Object>{
        'local_asset_id': 'android:raw',
        'filename': 'RAW_1.dng',
        'reason': 'unsupported_media_type',
        'media_type': 'image/x-adobe-dng',
        'byte_size': 55,
      });
    },
  );

  test('preserves non-success status and server error code', () async {
    final transport = FakeTransport()
      ..enqueueJson(
        '/v1/tasks/$taskId/seal',
        const <String, Object>{'detail': 'task_not_sealable'},
        method: 'POST',
        status: 409,
      );
    final repository = _repository(transport);

    await expectLater(
      repository.seal(taskId),
      throwsA(
        isA<ApiException>().having(
          (error) => error.code,
          'code',
          'task_not_sealable',
        ),
      ),
    );
  });

  test('malformed task JSON raises a stable contract error', () async {
    final transport = FakeTransport()
      ..enqueueJson('/v1/tasks', <String, Object>{
        'items': <Object>[
          const <String, Object>{'id': taskId},
        ],
      }, method: 'GET');
    final repository = _repository(transport);

    await expectLater(
      repository.list(),
      throwsA(
        isA<ApiException>().having(
          (error) => error.code,
          'code',
          'invalid_response',
        ),
      ),
    );
  });

  test('transport fixture rejects a wrong method or path mutation', () async {
    final wrongMethod = FakeTransport()
      ..enqueueJson('/v1/tasks', const <String, Object>{
        'items': <Object>[],
      }, method: 'GET');
    final wrongPath = FakeTransport()
      ..enqueueJson('/v1/tasks', const <String, Object>{
        'items': <Object>[],
      }, method: 'GET');

    await expectLater(
      wrongMethod.request('POST', '/v1/tasks'),
      throwsA(isA<StateError>()),
    );
    await expectLater(
      wrongPath.request('GET', '/v1/task'),
      throwsA(isA<StateError>()),
    );
  });
}

TaskRepository _repository(FakeTransport transport) => TaskRepository(
  SessionRepository(
    transport,
    MemoryTokenStore(
      accessToken: 'test-access-token',
      refreshToken: 'refresh-token',
    ),
  ),
);
