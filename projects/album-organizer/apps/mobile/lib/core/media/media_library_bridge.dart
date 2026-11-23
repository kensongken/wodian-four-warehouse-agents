import 'local_asset_ref.dart';

abstract interface class MediaLibraryBridge {
  Future<MediaPickResult> pickImages({required int remainingSlots});

  Future<void> flushPendingCleanup();

  Future<String> computeSha256(LocalAssetRef ref);

  Future<void> release(LocalAssetRef ref);
}

final class MediaLibraryException implements Exception {
  const MediaLibraryException({required this.code, required this.userMessage});

  final String code;
  final String userMessage;

  @override
  String toString() => 'MediaLibraryException($code): $userMessage';
}
