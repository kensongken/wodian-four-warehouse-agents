import '../../core/api/api_error.dart';
import '../../core/media/local_asset_ref.dart';
import '../../core/media/media_library_bridge.dart';
import '../../core/upload/upload_models.dart';
import '../../core/upload/upload_queue_repository.dart';
import 'controlled_upload_binding_repository.dart';
import 'controlled_upload_candidate.dart';

final RegExp _digest = RegExp(r'^[0-9a-f]{64}$');

final class ControlledCandidateHandoffResult {
  const ControlledCandidateHandoffResult({
    required this.candidateSha256,
    required this.bindingId,
    required this.snapshot,
  });

  final String candidateSha256;
  final String bindingId;
  final ImportQueueSnapshot snapshot;
}

final class ControlledCandidateHandoffService {
  const ControlledCandidateHandoffService({
    required MediaLibraryBridge mediaLibrary,
    required ControlledUploadBindingBoundary binding,
    required UploadQueueRepository queue,
  }) : // These public parameter names are part of the feature boundary.
       // ignore: prefer_initializing_formals
       _mediaLibrary = mediaLibrary,
       // ignore: prefer_initializing_formals
       _binding = binding,
       // ignore: prefer_initializing_formals
       _queue = queue;

  final MediaLibraryBridge _mediaLibrary;
  final ControlledUploadBindingBoundary _binding;
  final UploadQueueRepository _queue;

  Future<ControlledCandidateHandoffResult> handoff({
    required ControlledUploadCandidate candidate,
    required List<LocalAssetRef> localAssets,
    required String taskId,
    required CandidateUploadApproval approval,
  }) async {
    approval.requireCandidate(candidate.candidateId);
    final localById = <String, LocalAssetRef>{
      for (final asset in localAssets) asset.id: asset,
    };
    if (localById.length != localAssets.length ||
        localById.length != candidate.items.length ||
        !candidate.items.every(
          (item) => localById.containsKey(item.localAssetId),
        )) {
      throw _assetMismatch();
    }

    final sanitized = <LocalAssetRef>[];
    for (final item in candidate.items) {
      final local = localById[item.localAssetId]!;
      if (local.mediaType != item.mediaType ||
          local.byteSize != item.byteSize ||
          local.capturedAt?.toUtc() != item.capturedAt) {
        throw _assetMismatch();
      }
      late final String actualDigest;
      try {
        actualDigest = await _mediaLibrary.computeSha256(local);
      } on Object {
        throw _assetMismatch();
      }
      if (!_digest.hasMatch(actualDigest) || actualDigest != item.sha256) {
        throw _assetMismatch();
      }
      sanitized.add(
        LocalAssetRef(
          id: item.localAssetId,
          displayName: item.filename,
          mediaType: item.mediaType,
          byteSize: item.byteSize,
          capturedAt: item.capturedAt,
          accessKind: local.accessKind,
          locator: local.locator,
        ),
      );
    }

    final binding = await _binding.bind(
      opcId: candidate.opcId,
      taskId: taskId,
      candidate: candidate,
    );
    if (binding.opcId != candidate.opcId ||
        binding.taskId != taskId ||
        binding.candidateId != candidate.candidateId) {
      throw const ApiException(
        code: 'invalid_response',
        userMessage: '服务返回异常，请稍后重试',
      );
    }
    try {
      final snapshot = await _queue.enqueueControlledCandidate(
        taskId,
        sanitized,
        authority: binding.authority,
      );
      return ControlledCandidateHandoffResult(
        candidateSha256: candidate.candidateSha256,
        bindingId: binding.bindingId,
        snapshot: snapshot,
      );
    } on UploadQueueException catch (error) {
      if (error.code == 'candidate_queue_conflict') {
        throw const ControlledUploadException(
          code: 'candidate_queue_conflict',
          safeMessage: '候选与本地上传队列不一致，请重新核对',
        );
      }
      rethrow;
    }
  }
}

ControlledUploadException _assetMismatch() => const ControlledUploadException(
  code: 'candidate_asset_mismatch',
  safeMessage: '候选图片与本地文件不一致，请重新核对',
);
