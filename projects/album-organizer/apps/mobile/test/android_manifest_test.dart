import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  test('release manifest requests Internet access', () async {
    final manifest = await File('android/app/src/main/AndroidManifest.xml')
        .readAsString();

    expect(
      manifest,
      contains(
        '<uses-permission android:name="android.permission.INTERNET" />',
      ),
    );
  });

  test('cleartext is enabled for debug development only', () async {
    final debugManifest = await File(
      'android/app/src/debug/AndroidManifest.xml',
    ).readAsString();
    final mainManifest = await File('android/app/src/main/AndroidManifest.xml')
        .readAsString();

    expect(debugManifest, contains('android:usesCleartextTraffic="true"'));
    expect(mainManifest, isNot(contains('android:usesCleartextTraffic')));
  });
}
