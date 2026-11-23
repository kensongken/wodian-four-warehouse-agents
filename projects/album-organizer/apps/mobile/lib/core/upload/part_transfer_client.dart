import '../media/local_asset_ref.dart';

abstract interface class PartTransferClient {
  Future<TransferPartResult> transfer(TransferPartRequest request);
}

final class TransferPartRequest {
  const TransferPartRequest({
    required this.jobId,
    required this.asset,
    required this.partNumber,
    required this.byteOffset,
    required this.byteLength,
    required this.signedUrl,
  });

  final String jobId;
  final LocalAssetRef asset;
  final int partNumber;
  final int byteOffset;
  final int byteLength;
  final Uri signedUrl;
}

final class TransferPartResult {
  const TransferPartResult({required this.etag});

  final String etag;
}

final class PartTransferException implements Exception {
  const PartTransferException({
    required this.code,
    required this.safeMessage,
    this.statusCode,
  });

  final String code;
  final String safeMessage;
  final int? statusCode;
}
