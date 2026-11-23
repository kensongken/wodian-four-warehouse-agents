import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/api_error.dart';
import 'package:photo_organizer/core/media/local_asset_ref.dart';
import 'package:photo_organizer/core/media/media_library_bridge.dart';
import 'package:photo_organizer/core/upload/upload_database.dart';
import 'package:photo_organizer/core/upload/upload_queue_repository.dart';
import 'package:photo_organizer/features/controlled_upload/controlled_candidate_handoff_service.dart';
import 'package:photo_organizer/features/controlled_upload/controlled_upload_binding_repository.dart';
import 'package:photo_organizer/features/controlled_upload/controlled_upload_candidate.dart';

import 'controlled_upload_fixtures.dart';

void main() {
  test(
    'human-approved synthetic candidate binds and enters the durable queue',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final media = _FakeMediaLibrary(<String, String>{
        _originalA.id:
            '1111111111111111111111111111111111111111111111111111111111111111',
        _originalB.id:
            '2222222222222222222222222222222222222222222222222222222222222222',
      });
      final binding = _FakeBinding();
      final service = ControlledCandidateHandoffService(
        mediaLibrary: media,
        binding: binding,
        queue: queue,
      );
      final candidate = ControlledUploadCandidate.fromJson(
        syntheticCandidateJson(),
      );

      final result = await service.handoff(
        candidate: candidate,
        localAssets: <LocalAssetRef>[_originalA, _originalB],
        taskId: syntheticTaskId,
        approval: CandidateUploadApproval.approved(
          candidateId: candidate.candidateId,
          confirmedAt: DateTime.utc(2026, 8, 19, 8),
        ),
      );

      expect(result.candidateSha256, candidate.candidateSha256);
      expect(binding.calls, 1);
      expect(result.snapshot.queued, 2);
      expect(
        (await queue.find(_originalA.id))!.asset.displayName,
        '${_originalA.id}.jpg',
      );
    },
  );

  test(
    'wrong human approval causes zero byte reads binding and queue writes',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final queue = UploadQueueRepository(database);
      final media = _FakeMediaLibrary(const <String, String>{});
      final binding = _FakeBinding();
      final candidate = ControlledUploadCandidate.fromJson(
        syntheticCandidateJson(),
      );

      await expectLater(
        ControlledCandidateHandoffService(
          mediaLibrary: media,
          binding: binding,
          queue: queue,
        ).handoff(
          candidate: candidate,
          localAssets: <LocalAssetRef>[_originalA, _originalB],
          taskId: syntheticTaskId,
          approval: CandidateUploadApproval.approved(
            candidateId:
                'opc-controlled-upload-candidate:'
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            confirmedAt: DateTime.utc(2026, 8, 19, 8),
          ),
        ),
        throwsA(_controlledError('human_confirmation_required')),
      );

      expect(media.hashCalls, 0);
      expect(binding.calls, 0);
      expect((await queue.snapshot(syntheticTaskId)).queued, 0);
    },
  );

  test('byte SHA drift is rejected before authenticated binding', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final binding = _FakeBinding();
    final candidate = ControlledUploadCandidate.fromJson(
      syntheticCandidateJson(),
    );
    final media = _FakeMediaLibrary(<String, String>{
      _originalA.id:
          'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
    });

    await expectLater(
      ControlledCandidateHandoffService(
        mediaLibrary: media,
        binding: binding,
        queue: queue,
      ).handoff(
        candidate: candidate,
        localAssets: <LocalAssetRef>[_originalA, _originalB],
        taskId: syntheticTaskId,
        approval: _approval(candidate),
      ),
      throwsA(_controlledError('candidate_asset_mismatch')),
    );

    expect(binding.calls, 0);
    expect((await queue.snapshot(syntheticTaskId)).queued, 0);
  });

  test('binding response from another task cannot reach the queue', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final candidate = ControlledUploadCandidate.fromJson(
      syntheticCandidateJson(),
    );
    final media = _FakeMediaLibrary(<String, String>{
      _originalA.id:
          '1111111111111111111111111111111111111111111111111111111111111111',
      _originalB.id:
          '2222222222222222222222222222222222222222222222222222222222222222',
    });

    await expectLater(
      ControlledCandidateHandoffService(
        mediaLibrary: media,
        binding: _FakeBinding(
          returnedTaskId: '77777777-7777-4777-8777-777777777777',
        ),
        queue: queue,
      ).handoff(
        candidate: candidate,
        localAssets: <LocalAssetRef>[_originalA, _originalB],
        taskId: syntheticTaskId,
        approval: _approval(candidate),
      ),
      throwsA(_apiError('invalid_response')),
    );

    expect((await queue.snapshot(syntheticTaskId)).queued, 0);
  });

  test('an exact candidate replay returns success without duplicate rows', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final queue = UploadQueueRepository(database);
    final candidate = ControlledUploadCandidate.fromJson(
      syntheticCandidateJson(),
    );
    final service = ControlledCandidateHandoffService(
      mediaLibrary: _FakeMediaLibrary(<String, String>{
        _originalA.id:
            '1111111111111111111111111111111111111111111111111111111111111111',
        _originalB.id:
            '2222222222222222222222222222222222222222222222222222222222222222',
      }),
      binding: _FakeBinding(),
      queue: queue,
    );

    await service.handoff(
      candidate: candidate,
      localAssets: <LocalAssetRef>[_originalA, _originalB],
      taskId: syntheticTaskId,
      approval: _approval(candidate),
    );
    final replay = await service.handoff(
      candidate: candidate,
      localAssets: <LocalAssetRef>[_originalB, _originalA],
      taskId: syntheticTaskId,
      approval: _approval(candidate),
    );

    expect(replay.snapshot.queued, 2);
  });
}

CandidateUploadApproval _approval(ControlledUploadCandidate candidate) =>
    CandidateUploadApproval.approved(
      candidateId: candidate.candidateId,
      confirmedAt: DateTime.utc(2026, 8, 19, 8),
    );

Matcher _controlledError(String code) => isA<ControlledUploadException>()
    .having((error) => error.code, 'code', code);

Matcher _apiError(String code) =>
    isA<ApiException>().having((error) => error.code, 'code', code);

final _originalA = LocalAssetRef(
  id: '11111111-1111-4111-8111-111111111111',
  displayName: 'private-original-name-a.jpg',
  mediaType: 'image/jpeg',
  byteSize: 123,
  capturedAt: DateTime.utc(2026, 8, 19, 7),
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/synthetic/source-a',
);

final _originalB = LocalAssetRef(
  id: '22222222-2222-4222-8222-222222222222',
  displayName: 'private-original-name-b.png',
  mediaType: 'image/png',
  byteSize: 456,
  capturedAt: DateTime.utc(2026, 8, 19, 7, 5),
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/synthetic/source-b',
);

final class _FakeBinding implements ControlledUploadBindingBoundary {
  _FakeBinding({this.returnedTaskId = syntheticTaskId});

  final String returnedTaskId;
  int calls = 0;

  @override
  Future<ControlledUploadBinding> bind({
    required String opcId,
    required String taskId,
    required ControlledUploadCandidate candidate,
  }) async {
    calls += 1;
    return ControlledUploadBinding(
      bindingId: syntheticBindingId,
      candidateId: candidate.candidateId,
      opcId: opcId,
      taskId: returnedTaskId,
      boundAt: DateTime.utc(2026, 8, 19, 8, 5),
    );
  }
}

final class _FakeMediaLibrary implements MediaLibraryBridge {
  _FakeMediaLibrary(this.digests);

  final Map<String, String> digests;
  int hashCalls = 0;

  @override
  Future<String> computeSha256(LocalAssetRef ref) async {
    hashCalls += 1;
    return digests[ref.id]!;
  }

  @override
  Future<void> flushPendingCleanup() async {}

  @override
  Future<MediaPickResult> pickImages({required int remainingSlots}) =>
      throw UnimplementedError();

  @override
  Future<void> release(LocalAssetRef ref) async {}
}
