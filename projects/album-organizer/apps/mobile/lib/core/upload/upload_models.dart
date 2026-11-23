import '../media/local_asset_ref.dart';

final RegExp _canonicalUploadBindingId = RegExp(
  r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
);
final RegExp _controlledUploadCandidateId = RegExp(
  r'^opc-controlled-upload-candidate:[0-9a-f]{64}$',
);

final class UploadAuthority {
  factory UploadAuthority({
    required String bindingId,
    required String candidateId,
  }) {
    if (!_canonicalUploadBindingId.hasMatch(bindingId) ||
        !_controlledUploadCandidateId.hasMatch(candidateId)) {
      throw const FormatException('invalid upload authority');
    }
    return UploadAuthority._(bindingId: bindingId, candidateId: candidateId);
  }

  const UploadAuthority._({required this.bindingId, required this.candidateId});

  final String bindingId;
  final String candidateId;
}

enum LocalUploadState {
  awaitingHash,
  awaitingInitiation,
  awaitingParts,
  uploading,
  awaitingCompletion,
  uploaded,
  duplicate,
  pausedWifi,
  failedRetryable,
  failureReportPending,
  failedPermanent,
}

final class UploadClaim {
  const UploadClaim({
    required this.localAssetId,
    required this.ownerId,
    required this.leaseGeneration,
    required this.policyGeneration,
  });

  final String localAssetId;
  final String ownerId;
  final int leaseGeneration;
  final int policyGeneration;
}

final class UploadLeaseLostException implements Exception {
  const UploadLeaseLostException();
}

final class NativePartJobLink {
  const NativePartJobLink({
    required this.taskId,
    required this.partNumber,
    required this.nativeJobId,
    required this.claim,
  });

  final String taskId;
  final int partNumber;
  final String nativeJobId;
  final UploadClaim claim;
}

final class ServerSummaryReceipt {
  const ServerSummaryReceipt({
    required this.localAssetId,
    required this.backendAssetId,
    required this.leaseGeneration,
    required this.policyGeneration,
    required this.state,
  });

  final String localAssetId;
  final String backendAssetId;
  final int leaseGeneration;
  final int policyGeneration;
  final LocalUploadState state;
}

final class ServerSummaryFence {
  const ServerSummaryFence({
    required this.taskId,
    required this.acceptanceReceipts,
    required this.terminalReceipts,
  });

  final String taskId;
  final List<ServerSummaryReceipt> acceptanceReceipts;
  final List<ServerSummaryReceipt> terminalReceipts;

  bool get isEmpty => acceptanceReceipts.isEmpty && terminalReceipts.isEmpty;
}

final class UploadQueueException implements Exception {
  const UploadQueueException({
    required this.code,
    required this.safeMessage,
    required this.retryable,
  });

  final String code;
  final String safeMessage;
  final bool retryable;
}

final class QueuedUploadItem {
  const QueuedUploadItem({
    required this.taskId,
    required this.asset,
    this.authority,
    required this.partSize,
    required this.completedParts,
    this.nativeJobs = const <int, NativePartJobLink>{},
    required this.state,
    this.sha256,
    this.backendAssetId,
    this.providerUploadId,
    this.retryCount = 0,
    this.nextAttemptAt,
    this.lastErrorCode,
    this.lastErrorMessage,
    this.leaseOwner,
    this.leaseExpiresAt,
    this.leaseGeneration = 0,
    this.policyGeneration = 0,
    this.serverConfirmed = false,
    this.terminalSummaryAcknowledged = false,
    this.retryRequested = false,
    this.localRejected = false,
    this.pausedFromState,
    this.failureReportRetryCount = 0,
    this.failureReportNextAttemptAt,
  });

  final String taskId;
  final LocalAssetRef asset;
  final UploadAuthority? authority;
  final int partSize;
  final Map<int, String> completedParts;
  final Map<int, NativePartJobLink> nativeJobs;
  final LocalUploadState state;
  final String? sha256;
  final String? backendAssetId;
  final String? providerUploadId;
  final int retryCount;
  final DateTime? nextAttemptAt;
  final String? lastErrorCode;
  final String? lastErrorMessage;
  final String? leaseOwner;
  final DateTime? leaseExpiresAt;
  final int leaseGeneration;
  final int policyGeneration;
  final bool serverConfirmed;
  final bool terminalSummaryAcknowledged;
  final bool retryRequested;
  final bool localRejected;
  final LocalUploadState? pausedFromState;
  final int failureReportRetryCount;
  final DateTime? failureReportNextAttemptAt;

  UploadClaim get claim {
    final ownerId = leaseOwner;
    if (ownerId == null) throw const UploadLeaseLostException();
    return UploadClaim(
      localAssetId: asset.id,
      ownerId: ownerId,
      leaseGeneration: leaseGeneration,
      policyGeneration: policyGeneration,
    );
  }

  int get partCount => (asset.byteSize + partSize - 1) ~/ partSize;

  List<int> get remainingPartNumbers => <int>[
    for (var number = 1; number <= partCount; number += 1)
      if (!completedParts.containsKey(number)) number,
  ];
}

final class LocalUploadProgress {
  const LocalUploadProgress({
    required this.taskId,
    required this.total,
    required this.queued,
    required this.uploading,
    required this.uploaded,
    required this.failed,
    this.hashed = 0,
    this.waitingWifi = 0,
    this.duplicates = 0,
  });

  final String taskId;
  final int total;
  final int queued;
  final int uploading;
  final int uploaded;
  final int failed;
  final int hashed;
  final int waitingWifi;
  final int duplicates;
}
