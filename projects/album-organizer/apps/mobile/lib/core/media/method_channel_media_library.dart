import 'package:flutter/services.dart';

import '../ingestion/ingestion_limits.dart';
import 'local_asset_ref.dart';
import 'media_library_bridge.dart';

final class MethodChannelMediaLibrary implements MediaLibraryBridge {
  MethodChannelMediaLibrary([
    this._channel = const MethodChannel(_channelName),
  ]);

  static const String _channelName = 'photo_organizer/media_library';
  static final RegExp _sha256 = RegExp(r'^[0-9a-f]{64}$');

  final MethodChannel _channel;
  final Map<String, _NativeOwnershipHandle> _cleanupLedger =
      <String, _NativeOwnershipHandle>{};

  @override
  Future<MediaPickResult> pickImages({required int remainingSlots}) async {
    if (remainingSlots <= 0 || remainingSlots > maximumAcceptedAssets) {
      throw ArgumentError.value(
        remainingSlots,
        'remainingSlots',
        'must be between 1 and $maximumAcceptedAssets',
      );
    }
    await flushPendingCleanup();

    final Object? raw;
    try {
      raw = await _channel.invokeMethod<Object?>('pickImages', <String, Object>{
        'remainingSlots': remainingSlots,
      });
    } on PlatformException catch (error) {
      throw MediaLibraryException(
        code: error.code,
        userMessage: '无法读取所选照片，请重试',
      );
    }

    final batchHandles = <_NativeOwnershipHandle>[];
    try {
      final payload = _stringMap(raw);
      final selectedValues = _objectList(payload['selected'], 'selected');
      final selectedMaps = <Map<String, Object?>>[];
      FormatException? ownershipScanError;
      for (final value in selectedValues) {
        try {
          final map = _stringMap(value);
          final handle = _NativeOwnershipHandle.fromMap(map);
          batchHandles.add(handle);
          _cleanupLedger[handle.key] = handle;
          selectedMaps.add(map);
        } on FormatException catch (error) {
          ownershipScanError ??= error;
        }
      }
      if (ownershipScanError case final error?) throw error;
      final unsupportedValues = _objectList(
        payload['unsupported'],
        'unsupported',
      );
      final selected = <LocalAssetRef>[];
      final unsupported = unsupportedValues
          .map((value) => UnsupportedLocalAsset.fromMap(_stringMap(value)))
          .toList(growable: true);
      for (var index = 0; index < selectedMaps.length; index += 1) {
        final ref = LocalAssetRef.fromMap(selectedMaps[index]);
        final handle = batchHandles[index];
        if (ref.byteSize > maximumImageBytes) {
          final released = await _releaseNative(handle);
          if (released) _cleanupLedger.remove(handle.key);
          if (!released) throw const _CleanupFailure();
          unsupported.add(
            UnsupportedLocalAsset(
              id: ref.id,
              displayName: ref.displayName,
              reason: 'image_too_large',
              mediaType: ref.mediaType,
              byteSize: ref.byteSize,
            ),
          );
        } else {
          selected.add(ref);
        }
      }
      if (selected.length > remainingSlots) {
        throw const FormatException('native selection exceeds remainingSlots');
      }
      for (final ref in selected) {
        _cleanupLedger.remove(_NativeOwnershipHandle.fromRef(ref).key);
      }
      return MediaPickResult(
        selected: List<LocalAssetRef>.unmodifiable(selected),
        unsupported: List<UnsupportedLocalAsset>.unmodifiable(unsupported),
      );
    } on _CleanupFailure {
      await _releaseBatch(batchHandles);
      throw const MediaLibraryException(
        code: 'asset_cleanup_failed',
        userMessage: '无法安全释放照片，请稍后重试',
      );
    } on FormatException {
      final cleaned = await _releaseBatch(batchHandles);
      if (!cleaned) {
        throw const MediaLibraryException(
          code: 'asset_cleanup_failed',
          userMessage: '无法安全释放照片，请稍后重试',
        );
      }
      throw const MediaLibraryException(
        code: 'invalid_native_response',
        userMessage: '照片选择结果无效，请重新选择',
      );
    }
  }

  @override
  Future<void> flushPendingCleanup() async {
    if (!await _retryCleanupLedger()) {
      throw const MediaLibraryException(
        code: 'asset_cleanup_failed',
        userMessage: '无法安全释放照片，请稍后重试',
      );
    }
  }

  @override
  Future<String> computeSha256(LocalAssetRef ref) async {
    try {
      final digest = await _channel.invokeMethod<Object?>(
        'computeSha256',
        ref.toMap(),
      );
      if (digest is! String || !_sha256.hasMatch(digest)) {
        throw const FormatException('invalid SHA-256 digest');
      }
      return digest;
    } on PlatformException catch (error) {
      throw MediaLibraryException(
        code: error.code,
        userMessage: '无法读取照片内容，请重试',
      );
    } on FormatException {
      throw const MediaLibraryException(
        code: 'invalid_native_response',
        userMessage: '照片校验结果无效，请重新选择',
      );
    }
  }

  @override
  Future<void> release(LocalAssetRef ref) async {
    try {
      await _channel.invokeMethod<void>('release', ref.toMap());
    } on PlatformException catch (error) {
      throw MediaLibraryException(
        code: error.code,
        userMessage: '无法释放所选照片，请稍后重试',
      );
    }
  }

  Future<bool> _releaseNative(_NativeOwnershipHandle ref) async {
    try {
      await _channel.invokeMethod<void>('release', ref.toMap());
      return true;
    } catch (_) {
      return false;
    }
  }

  Future<bool> _releaseBatch(Iterable<_NativeOwnershipHandle> refs) async {
    var cleaned = true;
    for (final ref in refs) {
      if (!_cleanupLedger.containsKey(ref.key)) continue;
      if (await _releaseNative(ref)) {
        _cleanupLedger.remove(ref.key);
      } else {
        cleaned = false;
      }
    }
    return cleaned;
  }

  Future<bool> _retryCleanupLedger() =>
      _releaseBatch(List<_NativeOwnershipHandle>.of(_cleanupLedger.values));
}

final class _CleanupFailure implements Exception {
  const _CleanupFailure();
}

final class _NativeOwnershipHandle {
  const _NativeOwnershipHandle({
    required this.id,
    required this.accessKind,
    required this.locator,
  });

  factory _NativeOwnershipHandle.fromMap(Map<String, Object?> map) {
    final id = map['id'];
    final accessKind = switch (map['accessKind']) {
      'stagedFile' => 'stagedFile',
      'contentUri' => 'contentUri',
      _ => throw const FormatException('unsupported accessKind'),
    };
    final locator = map['locator'];
    if (id is! String || id.isEmpty) {
      throw const FormatException('id must be a non-empty string');
    }
    if (locator is! String || locator.isEmpty) {
      throw const FormatException('locator must be a non-empty string');
    }
    return _NativeOwnershipHandle(
      id: id,
      accessKind: accessKind,
      locator: locator,
    );
  }

  factory _NativeOwnershipHandle.fromRef(LocalAssetRef ref) =>
      _NativeOwnershipHandle(
        id: ref.id,
        accessKind: switch (ref.accessKind) {
          AssetAccessKind.stagedFile => 'stagedFile',
          AssetAccessKind.contentUri => 'contentUri',
        },
        locator: ref.locator,
      );

  final String id;
  final String accessKind;
  final String locator;

  String get key => '$id\u0000$accessKind\u0000$locator';

  Map<String, Object> toMap() => <String, Object>{
    'id': id,
    'accessKind': accessKind,
    'locator': locator,
  };
}

Map<String, Object?> _stringMap(Object? value) {
  if (value case final Map<Object?, Object?> map) {
    return map.map((key, value) => MapEntry(key.toString(), value));
  }
  throw const FormatException('value must be a map');
}

List<Object?> _objectList(Object? value, String name) {
  if (value is List<Object?>) {
    return value;
  }
  throw FormatException('$name must be a list');
}
