import 'dart:async';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/media/local_asset_ref.dart';
import 'package:photo_organizer/core/upload/background_transfer_bridge.dart';
import 'package:photo_organizer/core/upload/method_channel_background_transfer.dart';
import 'package:photo_organizer/core/upload/part_transfer_client.dart';

const _methodChannel = MethodChannel('photo_organizer/background_transfer');
const _eventChannel = MethodChannel(
  'photo_organizer/background_transfer_events',
);
const _codec = StandardMethodCodec();
const _asset = LocalAssetRef(
  id: 'ios:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  displayName: 'part.jpg',
  mediaType: 'image/jpeg',
  byteSize: 1024,
  accessKind: AssetAccessKind.stagedFile,
  locator:
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/'
      '11111111-1111-4111-8111-111111111111/original',
);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  tearDown(() async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(_methodChannel, null);
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(_eventChannel, null);
  });

  test(
    'enqueue serializes the durable ref and transient transfer fields',
    () async {
      MethodCall? captured;
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(_methodChannel, (call) async {
            captured = call;
            return 'stable-job-1';
          });
      final bridge = MethodChannelBackgroundTransferBridge();

      final nativeJobId = await bridge.enqueuePart(
        TransferPartRequest(
          jobId: 'stable-job-1',
          asset: _asset,
          partNumber: 2,
          byteOffset: 512,
          byteLength: 512,
          signedUrl: Uri.parse(
            'https://upload.example.test/part?signature=transient-secret',
          ),
        ),
        allowMobileData: false,
      );

      expect(nativeJobId, 'stable-job-1');
      expect(captured!.method, 'enqueuePart');
      expect(captured!.arguments, <String, Object?>{
        'jobId': 'stable-job-1',
        'asset': _asset.toMap(),
        'partNumber': 2,
        'byteOffset': 512,
        'byteLength': 512,
        'signedUrl':
            'https://upload.example.test/part?signature=transient-secret',
        'allowMobileData': false,
      });
    },
  );

  test('query decodes only requested durable native jobs', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(_methodChannel, (call) async {
          expect(call.method, 'queryJobs');
          expect(call.arguments, <String, Object>{
            'jobIds': <String>['job-running', 'job-done'],
          });
          return <Object?>[
            <String, Object?>{'jobId': 'job-running', 'state': 'running'},
            <String, Object?>{
              'jobId': 'job-done',
              'state': 'succeeded',
              'etag': 'etag-durable',
            },
          ];
        });
    final bridge = MethodChannelBackgroundTransferBridge();

    final jobs = await bridge.queryJobs(<String>['job-running', 'job-done']);

    expect(jobs.keys, <String>['job-running', 'job-done']);
    expect(jobs['job-done']!.etag, 'etag-durable');
  });

  test('event channel delivers a terminal result', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(_eventChannel, (_) async => null);
    final bridge = MethodChannelBackgroundTransferBridge();
    final terminal = bridge.events.first;
    await Future<void>.delayed(Duration.zero);

    await _sendEvent(<String, Object?>{
      'jobId': 'job-event',
      'state': 'succeeded',
      'etag': 'etag-from-event',
    });

    expect((await terminal).etag, 'etag-from-event');
  });

  test('rejects a native success with an invalid ETag', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(
          _methodChannel,
          (_) async => <Object?>[
            <String, Object?>{
              'jobId': 'job-invalid-etag',
              'state': 'succeeded',
              'etag': 'x' * 257,
            },
          ],
        );
    final bridge = MethodChannelBackgroundTransferBridge();

    await expectLater(
      bridge.queryJobs(<String>['job-invalid-etag']),
      throwsFormatException,
    );
  });

  test('rejects a native scheduler that changes the stable job id', () async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(_methodChannel, (_) async => 'other-job');
    final bridge = MethodChannelBackgroundTransferBridge();

    await expectLater(
      bridge.enqueuePart(
        TransferPartRequest(
          jobId: 'stable-job',
          asset: _asset,
          partNumber: 1,
          byteOffset: 0,
          byteLength: 512,
          signedUrl: Uri.parse('https://upload.example.test/part'),
        ),
        allowMobileData: false,
      ),
      throwsFormatException,
    );
  });

  test('rejects control characters in native job identifiers', () {
    expect(
      () => NativeTransferJob.fromMap(<String, Object?>{
        'jobId': 'job\u0000hidden',
        'state': 'running',
      }),
      throwsFormatException,
    );
  });

  test(
    'cancel wakes a locally enqueued waiter without a native event',
    () async {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(_methodChannel, (call) async {
            switch (call.method) {
              case 'enqueuePart':
                return 'cancelled-job';
              case 'cancel':
                return null;
              case 'queryJobs':
                return <Object?>[
                  <String, Object?>{
                    'jobId': 'cancelled-job',
                    'state': 'running',
                  },
                ];
            }
            fail('unexpected method ${call.method}');
          });
      final bridge = MethodChannelBackgroundTransferBridge();
      await bridge.enqueuePart(
        TransferPartRequest(
          jobId: 'cancelled-job',
          asset: _asset,
          partNumber: 1,
          byteOffset: 0,
          byteLength: 512,
          signedUrl: Uri.parse('https://upload.example.test/part'),
        ),
        allowMobileData: false,
      );

      await bridge.cancel('cancelled-job');

      await expectLater(
        bridge.waitForTerminal('cancelled-job'),
        throwsStateError,
      );
    },
  );

  test(
    'terminal event before wait is replayed after native ack removes query',
    () async {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(_methodChannel, (call) async {
            switch (call.method) {
              case 'enqueuePart':
                return 'early-terminal-job';
              case 'acknowledge':
                return null;
              case 'queryJobs':
                return <Object?>[];
            }
            fail('unexpected method ${call.method}');
          });
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(_eventChannel, (_) async => null);
      final bridge = MethodChannelBackgroundTransferBridge();
      final observed = bridge.events.first;
      await bridge.enqueuePart(
        TransferPartRequest(
          jobId: 'early-terminal-job',
          asset: _asset,
          partNumber: 1,
          byteOffset: 0,
          byteLength: 512,
          signedUrl: Uri.parse('https://upload.example.test/part'),
        ),
        allowMobileData: false,
      );
      await Future<void>.delayed(Duration.zero);
      await _sendEvent(<String, Object?>{
        'jobId': 'early-terminal-job',
        'state': 'succeeded',
        'etag': 'early-etag',
      });
      await observed;
      await bridge.acknowledge('early-terminal-job');

      final replayed = await bridge.waitForTerminal('early-terminal-job');

      expect(replayed.etag, 'early-etag');
    },
  );
}

Future<void> _sendEvent(Map<String, Object?> event) {
  final completer = Completer<void>();
  TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
      .handlePlatformMessage(
        'photo_organizer/background_transfer_events',
        _codec.encodeSuccessEnvelope(event),
        (_) => completer.complete(),
      );
  return completer.future;
}
