import 'part_transfer_client.dart';

enum NativeTransferState { queued, running, progress, succeeded, failed }

final class NativeTransferJob {
  const NativeTransferJob({
    required this.jobId,
    required this.state,
    this.progress,
    this.etag,
    this.code,
    this.retryable,
  });

  factory NativeTransferJob.fromMap(Map<String, Object?> map) {
    final jobId = map['jobId'];
    final rawState = map['state'];
    final progress = map['progress'];
    final etag = map['etag'];
    final code = map['code'];
    final retryable = map['retryable'];
    if (jobId is! String || !_isSafeNativeJobId(jobId)) {
      throw const FormatException('jobId must be a non-empty string');
    }
    final state = switch (rawState) {
      'queued' => NativeTransferState.queued,
      'running' => NativeTransferState.running,
      'progress' => NativeTransferState.progress,
      'succeeded' => NativeTransferState.succeeded,
      'failed' => NativeTransferState.failed,
      _ => throw const FormatException('unsupported native transfer state'),
    };
    if (progress != null &&
        (progress is! int || progress < 0 || progress > 100)) {
      throw const FormatException('progress must be between 0 and 100');
    }
    if (etag != null && etag is! String) {
      throw const FormatException('etag must be a string');
    }
    if (code != null && code is! String) {
      throw const FormatException('code must be a string');
    }
    if (retryable != null && retryable is! bool) {
      throw const FormatException('retryable must be a boolean');
    }
    final validEtag = etag as String?;
    final validCode = code as String?;
    if (state == NativeTransferState.succeeded &&
        (validEtag == null ||
            validEtag.trim().isEmpty ||
            validEtag.length > 256)) {
      throw const FormatException('succeeded transfer requires an etag');
    }
    if (state == NativeTransferState.failed &&
        (validCode == null ||
            validCode.trim().isEmpty ||
            validCode.length > 128 ||
            retryable == null)) {
      throw const FormatException('failed transfer requires error metadata');
    }
    if (state == NativeTransferState.progress && progress == null) {
      throw const FormatException('progress transfer requires progress');
    }
    return NativeTransferJob(
      jobId: jobId,
      state: state,
      progress: progress as int?,
      etag: validEtag,
      code: validCode,
      retryable: retryable as bool?,
    );
  }

  final String jobId;
  final NativeTransferState state;
  final int? progress;
  final String? etag;
  final String? code;
  final bool? retryable;

  bool get isTerminal =>
      state == NativeTransferState.succeeded ||
      state == NativeTransferState.failed;
}

bool _isSafeNativeJobId(String value) =>
    value.isNotEmpty &&
    value.length <= 512 &&
    !value.runes.any((codePoint) => codePoint < 0x20 || codePoint == 0x7f);

abstract interface class BackgroundTransferBridge {
  Stream<NativeTransferJob> get events;

  Future<String> enqueuePart(
    TransferPartRequest request, {
    required bool allowMobileData,
  });

  /// Returns only jobs still durably known by the native scheduler/ledger.
  /// An omitted requested ID is treated as a missing native job.
  Future<Map<String, NativeTransferJob>> queryJobs(
    Iterable<String> nativeJobIds,
  );

  Future<NativeTransferJob> waitForTerminal(String nativeJobId);

  Future<void> acknowledge(String nativeJobId);

  Future<void> cancel(String nativeJobId);
}
