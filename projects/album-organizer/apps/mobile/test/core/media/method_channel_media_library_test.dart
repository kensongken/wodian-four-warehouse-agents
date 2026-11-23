import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/media/local_asset_ref.dart';
import 'package:photo_organizer/core/media/media_library_bridge.dart';
import 'package:photo_organizer/core/media/method_channel_media_library.dart';

const _channel = MethodChannel('photo_organizer/media_library');
const _jpegAssetMap = <String, Object?>{
  'id': 'ios:stable-jpeg',
  'displayName': 'IMG_0001.JPG',
  'mediaType': 'image/jpeg',
  'byteSize': 42,
  'capturedAt': '2026-08-13T02:03:04.000Z',
  'accessKind': 'stagedFile',
  'locator': 'stable-jpeg/original',
};

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

  tearDown(() {
    messenger.setMockMethodCallHandler(_channel, null);
  });

  test(
    'decodes selected and unsupported assets without dropping either',
    () async {
      MethodCall? received;
      messenger.setMockMethodCallHandler(_channel, (call) async {
        received = call;
        return <String, Object?>{
          'selected': <Object?>[_jpegAssetMap],
          'unsupported': <Object?>[
            <String, Object?>{
              'id': 'ios:clip',
              'name': 'clip.mov',
              'reason': 'unsupported_media_type',
              'mediaType': 'video/quicktime',
              'byteSize': 84,
            },
          ],
        };
      });

      final result = await MethodChannelMediaLibrary().pickImages(
        remainingSlots: 3000,
      );

      expect(received?.method, 'pickImages');
      expect(received?.arguments, <String, Object>{'remainingSlots': 3000});
      expect(result.selected.single.mediaType, 'image/jpeg');
      expect(
        result.selected.single.capturedAt,
        DateTime.utc(2026, 8, 13, 2, 3, 4),
      );
      expect(result.unsupported.single.displayName, 'clip.mov');
      expect(result.unsupported.single.id, 'ios:clip');
    },
  );

  test('accepts 10000 remaining slots and rejects a larger request', () async {
    var nativeCalls = 0;
    messenger.setMockMethodCallHandler(_channel, (call) async {
      nativeCalls += 1;
      return <String, Object?>{
        'selected': <Object?>[],
        'unsupported': <Object?>[],
      };
    });

    await MethodChannelMediaLibrary().pickImages(remainingSlots: 10000);

    await expectLater(
      MethodChannelMediaLibrary().pickImages(remainingSlots: 10001),
      throwsA(isA<ArgumentError>()),
    );
    expect(nativeCalls, 1);
  });

  test('rejects malformed native payloads at the platform boundary', () async {
    messenger.setMockMethodCallHandler(_channel, (_) async {
      return <String, Object?>{
        'selected': <Object?>[
          <String, Object?>{..._jpegAssetMap, 'byteSize': '42'},
        ],
        'unsupported': <Object?>[],
      };
    });

    await expectLater(
      MethodChannelMediaLibrary().pickImages(remainingSlots: 1),
      throwsA(
        isA<MediaLibraryException>().having(
          (error) => error.code,
          'code',
          'invalid_native_response',
        ),
      ),
    );
  });

  test(
    'moves assets over 100 MiB to unsupported before queue insertion',
    () async {
      messenger.setMockMethodCallHandler(_channel, (_) async {
        return <String, Object?>{
          'selected': <Object?>[
            <String, Object?>{
              ..._jpegAssetMap,
              'id': 'android:too-big',
              'byteSize': 104857601,
              'accessKind': 'contentUri',
              'locator': 'content://media/external/images/1',
            },
          ],
          'unsupported': <Object?>[],
        };
      });

      final result = await MethodChannelMediaLibrary().pickImages(
        remainingSlots: 1,
      );

      expect(result.selected, isEmpty);
      expect(result.unsupported.single.id, 'android:too-big');
      expect(result.unsupported.single.reason, 'image_too_large');
      expect(result.unsupported.single.byteSize, 104857601);
    },
  );

  test('releases an oversized native reference exactly once', () async {
    final released = <Object?>[];
    messenger.setMockMethodCallHandler(_channel, (call) async {
      if (call.method == 'release') {
        released.add(call.arguments);
        return null;
      }
      return <String, Object?>{
        'selected': <Object?>[
          <String, Object?>{
            ..._jpegAssetMap,
            'id': 'android:too-big',
            'byteSize': 104857601,
            'accessKind': 'contentUri',
            'locator': 'content://media/owned/lease-1',
          },
        ],
        'unsupported': <Object?>[],
      };
    });

    final result = await MethodChannelMediaLibrary().pickImages(
      remainingSlots: 1,
    );

    expect(result.selected, isEmpty);
    expect(result.unsupported.single.reason, 'image_too_large');
    expect(released, hasLength(1));
    expect(
      (released.single! as Map<Object?, Object?>)['locator'],
      'content://media/owned/lease-1',
    );
  });

  test(
    'extracts every ownership handle before a later DTO is malformed',
    () async {
      final releasedIds = <String>[];
      messenger.setMockMethodCallHandler(_channel, (call) async {
        if (call.method == 'release') {
          final arguments = call.arguments! as Map<Object?, Object?>;
          releasedIds.add(arguments['id']! as String);
          return null;
        }
        return <String, Object?>{
          'selected': <Object?>[
            _jpegAssetMap,
            <String, Object?>{
              ..._jpegAssetMap,
              'id': 'broken',
              'byteSize': '42',
            },
          ],
          'unsupported': <Object?>[],
        };
      });

      await expectLater(
        MethodChannelMediaLibrary().pickImages(remainingSlots: 2),
        throwsA(
          isA<MediaLibraryException>().having(
            (error) => error.code,
            'code',
            'invalid_native_response',
          ),
        ),
      );
      expect(releasedIds, <String>['ios:stable-jpeg', 'broken']);
    },
  );

  test(
    'retains earlier ownership when a later selected value is not a map',
    () async {
      final releasedIds = <String>[];
      messenger.setMockMethodCallHandler(_channel, (call) async {
        if (call.method == 'release') {
          final arguments = call.arguments! as Map<Object?, Object?>;
          releasedIds.add(arguments['id']! as String);
          return null;
        }
        return <String, Object?>{
          'selected': <Object?>[_jpegAssetMap, 'not-a-map'],
          'unsupported': <Object?>[],
        };
      });

      await expectLater(
        MethodChannelMediaLibrary().pickImages(remainingSlots: 2),
        throwsA(
          isA<MediaLibraryException>().having(
            (error) => error.code,
            'code',
            'invalid_native_response',
          ),
        ),
      );
      expect(releasedIds, <String>['ios:stable-jpeg']);
    },
  );

  test(
    'scans past an earlier malformed handle to release later ownership',
    () async {
      final releasedIds = <String>[];
      messenger.setMockMethodCallHandler(_channel, (call) async {
        if (call.method == 'release') {
          final arguments = call.arguments! as Map<Object?, Object?>;
          releasedIds.add(arguments['id']! as String);
          return null;
        }
        return <String, Object?>{
          'selected': <Object?>[
            <String, Object?>{
              ..._jpegAssetMap,
              'id': 'bad-handle',
              'accessKind': 'unknown',
              'locator': 'bad/lease/original',
            },
            <String, Object?>{
              ..._jpegAssetMap,
              'id': 'ios:later-owned',
              'locator': 'later/lease/original',
            },
          ],
          'unsupported': <Object?>[],
        };
      });

      await expectLater(
        MethodChannelMediaLibrary().pickImages(remainingSlots: 2),
        throwsA(
          isA<MediaLibraryException>().having(
            (error) => error.code,
            'code',
            'invalid_native_response',
          ),
        ),
      );
      expect(releasedIds, <String>['ios:later-owned']);
    },
  );

  test(
    'extracts selected ownership before parsing malformed unsupported data',
    () async {
      final releasedIds = <String>[];
      messenger.setMockMethodCallHandler(_channel, (call) async {
        if (call.method == 'release') {
          final arguments = call.arguments! as Map<Object?, Object?>;
          releasedIds.add(arguments['id']! as String);
          return null;
        }
        return <String, Object?>{
          'selected': <Object?>[_jpegAssetMap],
          'unsupported': <Object?>[
            <String, Object?>{
              'id': 'ios:bad-unsupported',
              'name': 'bad.raw',
              'reason': 'not-a-server-reason',
            },
          ],
        };
      });

      await expectLater(
        MethodChannelMediaLibrary().pickImages(remainingSlots: 1),
        throwsA(
          isA<MediaLibraryException>().having(
            (error) => error.code,
            'code',
            'invalid_native_response',
          ),
        ),
      );
      expect(releasedIds, <String>['ios:stable-jpeg']);
    },
  );

  test(
    'retains failed cleanup for retry before opening picker again',
    () async {
      var releaseAttempts = 0;
      var pickerCalls = 0;
      messenger.setMockMethodCallHandler(_channel, (call) async {
        if (call.method == 'release') {
          releaseAttempts += 1;
          if (releaseAttempts <= 2) {
            throw PlatformException(code: 'release_failed');
          }
          return null;
        }
        pickerCalls += 1;
        return <String, Object?>{
          'selected': <Object?>[
            <String, Object?>{
              ..._jpegAssetMap,
              'byteSize': 104857601,
              'locator': 'stable-jpeg/lease-retry/original',
            },
          ],
          'unsupported': <Object?>[],
        };
      });
      final library = MethodChannelMediaLibrary();

      await expectLater(
        library.pickImages(remainingSlots: 1),
        throwsA(
          isA<MediaLibraryException>().having(
            (error) => error.code,
            'code',
            'asset_cleanup_failed',
          ),
        ),
      );
      expect(releaseAttempts, 2);
      expect(pickerCalls, 1);

      final result = await library.pickImages(remainingSlots: 1);

      expect(releaseAttempts, 4);
      expect(pickerCalls, 2);
      expect(result.unsupported.single.reason, 'image_too_large');
    },
  );

  test('flushes retained cleanup without opening the picker again', () async {
    var releaseAttempts = 0;
    var pickerCalls = 0;
    messenger.setMockMethodCallHandler(_channel, (call) async {
      if (call.method == 'release') {
        releaseAttempts += 1;
        if (releaseAttempts <= 2) {
          throw PlatformException(code: 'release_failed');
        }
        return null;
      }
      pickerCalls += 1;
      return <String, Object?>{
        'selected': <Object?>[
          <String, Object?>{
            ..._jpegAssetMap,
            'byteSize': 104857601,
            'locator': 'stable-jpeg/flush-lease/original',
          },
        ],
        'unsupported': <Object?>[],
      };
    });
    final library = MethodChannelMediaLibrary();
    await expectLater(
      library.pickImages(remainingSlots: 1),
      throwsA(
        isA<MediaLibraryException>().having(
          (error) => error.code,
          'code',
          'asset_cleanup_failed',
        ),
      ),
    );

    await library.flushPendingCleanup();

    expect(releaseAttempts, 3);
    expect(pickerCalls, 1);
  });

  test('maps cleanup failure safely without exposing a locator', () async {
    messenger.setMockMethodCallHandler(_channel, (call) async {
      if (call.method == 'release') {
        throw PlatformException(
          code: 'release_failed',
          message: 'content://secret/owned-lease',
        );
      }
      return <String, Object?>{
        'selected': <Object?>[
          <String, Object?>{
            ..._jpegAssetMap,
            'byteSize': 104857601,
            'locator': 'content://secret/owned-lease',
          },
        ],
        'unsupported': <Object?>[],
      };
    });

    try {
      await MethodChannelMediaLibrary().pickImages(remainingSlots: 1);
      fail('cleanup should fail');
    } on MediaLibraryException catch (error) {
      expect(error.code, 'asset_cleanup_failed');
      expect(error.userMessage, '无法安全释放照片，请稍后重试');
      expect(error.toString(), isNot(contains('content://secret/owned-lease')));
    }
  });

  test(
    'oversize cleanup failure still releases earlier decoded refs',
    () async {
      final releasedIds = <String>[];
      messenger.setMockMethodCallHandler(_channel, (call) async {
        if (call.method == 'release') {
          final arguments = call.arguments! as Map<Object?, Object?>;
          releasedIds.add(arguments['id']! as String);
          if (arguments['id'] == 'android:too-big') {
            throw PlatformException(code: 'release_failed');
          }
          return null;
        }
        return <String, Object?>{
          'selected': <Object?>[
            _jpegAssetMap,
            <String, Object?>{
              ..._jpegAssetMap,
              'id': 'android:too-big',
              'byteSize': 104857601,
              'locator': 'content://secret/owned-lease',
            },
          ],
          'unsupported': <Object?>[],
        };
      });

      await expectLater(
        MethodChannelMediaLibrary().pickImages(remainingSlots: 2),
        throwsA(
          isA<MediaLibraryException>().having(
            (error) => error.code,
            'code',
            'asset_cleanup_failed',
          ),
        ),
      );
      expect(releasedIds, <String>[
        'android:too-big',
        'ios:stable-jpeg',
        'android:too-big',
      ]);
    },
  );

  test('requires a 64-character lowercase native SHA-256 digest', () async {
    messenger.setMockMethodCallHandler(_channel, (call) async {
      expect(call.method, 'computeSha256');
      return 'ABCDEF';
    });

    await expectLater(
      MethodChannelMediaLibrary().computeSha256(
        LocalAssetRef.fromMap(_jpegAssetMap),
      ),
      throwsA(
        isA<MediaLibraryException>().having(
          (error) => error.code,
          'code',
          'invalid_native_response',
        ),
      ),
    );
  });

  test('maps release platform failures without exposing the locator', () async {
    messenger.setMockMethodCallHandler(_channel, (_) async {
      throw PlatformException(
        code: 'invalid_asset_ref',
        message: 'content://secret/token',
      );
    });

    try {
      await MethodChannelMediaLibrary().release(
        LocalAssetRef.fromMap(_jpegAssetMap),
      );
      fail('release should fail');
    } on MediaLibraryException catch (error) {
      expect(error.code, 'invalid_asset_ref');
      expect(error.userMessage, '无法释放所选照片，请稍后重试');
      expect(error.toString(), isNot(contains('content://secret/token')));
    }
  });

  test(
    'rejects non-positive selection limits before opening native UI',
    () async {
      var nativeCalls = 0;
      messenger.setMockMethodCallHandler(_channel, (_) async {
        nativeCalls += 1;
        return null;
      });

      await expectLater(
        MethodChannelMediaLibrary().pickImages(remainingSlots: 0),
        throwsA(isA<ArgumentError>()),
      );
      expect(nativeCalls, 0);
    },
  );
}
