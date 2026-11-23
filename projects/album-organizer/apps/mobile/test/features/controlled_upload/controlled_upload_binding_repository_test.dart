import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/api_error.dart';
import 'package:photo_organizer/core/auth/session_repository.dart';
import 'package:photo_organizer/features/controlled_upload/controlled_upload_binding_repository.dart';
import 'package:photo_organizer/features/controlled_upload/controlled_upload_candidate.dart';

import '../../helpers/fakes.dart';
import 'controlled_upload_fixtures.dart';

void main() {
  test(
    'posts the strict candidate to the authenticated OPC binding route',
    () async {
      final transport = FakeTransport()
        ..enqueueJson(
          '/v1/opcs/opc-synthetic/upload-bindings',
          syntheticBindingJson(),
          method: 'POST',
        );
      final repository = ControlledUploadBindingRepository(
        SessionRepository(
          transport,
          MemoryTokenStore(
            accessToken: 'test-synthetic-access',
            refreshToken: 'synthetic-refresh',
          ),
        ),
      );
      final candidate = ControlledUploadCandidate.fromJson(
        syntheticCandidateJson(),
      );

      final binding = await repository.bind(
        opcId: 'opc-synthetic',
        taskId: syntheticTaskId,
        candidate: candidate,
      );

      expect(binding.bindingId, syntheticBindingId);
      expect(transport.calls.single.method, 'POST');
      expect(
        transport.calls.single.path,
        '/v1/opcs/opc-synthetic/upload-bindings',
      );
      expect(transport.calls.single.data, <String, Object?>{
        'schema_version': 'opc-controlled-upload-binding-request/1.0',
        'task_id': syntheticTaskId,
        'candidate': syntheticCandidateJson(),
      });
    },
  );

  test(
    'rejects a binding response from another task before authority exists',
    () async {
      final response = syntheticBindingJson()
        ..['task_id'] = '77777777-7777-4777-8777-777777777777';
      final transport = FakeTransport()
        ..enqueueJson(
          '/v1/opcs/opc-synthetic/upload-bindings',
          response,
          method: 'POST',
        );
      final repository = _repository(transport);

      await expectLater(
        repository.bind(
          opcId: 'opc-synthetic',
          taskId: syntheticTaskId,
          candidate: ControlledUploadCandidate.fromJson(
            syntheticCandidateJson(),
          ),
        ),
        throwsA(_apiError('invalid_response')),
      );
    },
  );

  test('rejects response safety drift and unknown fields', () async {
    final response = syntheticBindingJson()
      ..['auto_publish'] = true
      ..['signed_url'] = 'https://invalid.test/?token=synthetic-secret';
    final transport = FakeTransport()
      ..enqueueJson(
        '/v1/opcs/opc-synthetic/upload-bindings',
        response,
        method: 'POST',
      );

    await expectLater(
      _repository(transport).bind(
        opcId: 'opc-synthetic',
        taskId: syntheticTaskId,
        candidate: ControlledUploadCandidate.fromJson(syntheticCandidateJson()),
      ),
      throwsA(_apiError('invalid_response')),
    );
  });

  test('rejects an impossible calendar date in binding response', () async {
    final response = syntheticBindingJson()
      ..['bound_at'] = '2026-02-30T08:05:00Z';
    final transport = FakeTransport()
      ..enqueueJson(
        '/v1/opcs/opc-synthetic/upload-bindings',
        response,
        method: 'POST',
      );

    await expectLater(
      _repository(transport).bind(
        opcId: 'opc-synthetic',
        taskId: syntheticTaskId,
        candidate: ControlledUploadCandidate.fromJson(syntheticCandidateJson()),
      ),
      throwsA(_apiError('invalid_response')),
    );
  });

  test('rejects year zero in binding response', () async {
    final response = syntheticBindingJson()
      ..['bound_at'] = '0000-08-19T08:05:00Z';
    final transport = FakeTransport()
      ..enqueueJson(
        '/v1/opcs/opc-synthetic/upload-bindings',
        response,
        method: 'POST',
      );

    await expectLater(
      _repository(transport).bind(
        opcId: 'opc-synthetic',
        taskId: syntheticTaskId,
        candidate: ControlledUploadCandidate.fromJson(syntheticCandidateJson()),
      ),
      throwsA(_apiError('invalid_response')),
    );
  });

  test('rejects a non-integer binding item count', () async {
    final response = syntheticBindingJson()..['item_count'] = 2.0;
    final transport = FakeTransport()
      ..enqueueJson(
        '/v1/opcs/opc-synthetic/upload-bindings',
        response,
        method: 'POST',
      );

    await expectLater(
      _repository(transport).bind(
        opcId: 'opc-synthetic',
        taskId: syntheticTaskId,
        candidate: ControlledUploadCandidate.fromJson(syntheticCandidateJson()),
      ),
      throwsA(_apiError('invalid_response')),
    );
  });

  test(
    'preserves a stable server conflict without parsing a binding',
    () async {
      final transport = FakeTransport()
        ..enqueueJson(
          '/v1/opcs/opc-synthetic/upload-bindings',
          const <String, Object?>{'detail': 'task_not_open'},
          method: 'POST',
          status: 409,
        );

      await expectLater(
        _repository(transport).bind(
          opcId: 'opc-synthetic',
          taskId: syntheticTaskId,
          candidate: ControlledUploadCandidate.fromJson(
            syntheticCandidateJson(),
          ),
        ),
        throwsA(_apiError('task_not_open')),
      );
    },
  );
}

ControlledUploadBindingRepository _repository(FakeTransport transport) =>
    ControlledUploadBindingRepository(
      SessionRepository(
        transport,
        MemoryTokenStore(
          accessToken: 'test-synthetic-access',
          refreshToken: 'synthetic-refresh',
        ),
      ),
    );

Matcher _apiError(String code) =>
    isA<ApiException>().having((error) => error.code, 'code', code);
