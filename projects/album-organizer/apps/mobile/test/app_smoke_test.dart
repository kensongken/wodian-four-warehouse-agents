import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/app.dart';
import 'package:photo_organizer/core/upload/upload_runtime.dart';

void main() {
  testWidgets('shows the product name', (tester) async {
    await tester.pumpWidget(_app(_FakeBackgroundUploadRuntime()));
    expect(find.text('AI 商品相册整理助手'), findsOneWidget);
  });

  testWidgets('uses its caller provider scope', (tester) async {
    await tester.pumpWidget(_app(_FakeBackgroundUploadRuntime()));

    expect(find.byType(ProviderScope), findsOneWidget);
  });

  testWidgets('reconciles background jobs on startup and resume', (
    tester,
  ) async {
    final runtime = _FakeBackgroundUploadRuntime();
    await tester.pumpWidget(_app(runtime));
    await tester.pump();
    expect(runtime.resumeCalls, 1);

    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pump();

    expect(runtime.resumeCalls, 2);
  });
}

ProviderScope _app(BackgroundUploadRuntime runtime) => ProviderScope(
  overrides: <Override>[
    backgroundUploadRuntimeProvider.overrideWithValue(runtime),
  ],
  child: const PhotoOrganizerApp(),
);

final class _FakeBackgroundUploadRuntime implements BackgroundUploadRuntime {
  int resumeCalls = 0;

  @override
  Future<void> resume() async {
    resumeCalls += 1;
  }
}
