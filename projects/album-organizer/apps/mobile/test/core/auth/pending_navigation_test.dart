import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/auth/pending_navigation.dart';

void main() {
  const taskId = '11111111-1111-4111-8111-111111111111';

  test('accepts only task roots and UUID import or progress routes', () {
    final pending = PendingNavigation();

    expect(pending.capture(Uri.parse('/tasks')), isTrue);
    expect(pending.peek(), '/tasks');
    expect(pending.capture(Uri.parse('/tasks/$taskId/import')), isTrue);
    expect(pending.peek(), '/tasks/$taskId/import');
    expect(pending.capture(Uri.parse('/tasks/$taskId/progress')), isTrue);
    expect(pending.peek(), '/tasks/$taskId/progress');
  });

  test('validation is side-effect free and shared with routing', () {
    final pending = PendingNavigation()
      ..capture(Uri.parse('/tasks/$taskId/import'));

    expect(PendingNavigation.validate(Uri.parse('/tasks')), '/tasks');
    expect(
      PendingNavigation.validate(Uri.parse('/tasks/$taskId/import')),
      '/tasks/$taskId/import',
    );
    expect(
      PendingNavigation.validate(Uri.parse('/tasks/$taskId/progress')),
      '/tasks/$taskId/progress',
    );
    expect(
      PendingNavigation.validate(
        Uri.parse('/tasks/$taskId/import?redirect=https://evil.example'),
      ),
      isNull,
    );
    expect(pending.peek(), '/tasks/$taskId/import');
  });

  test('rejects external, encoded, queried, and non-task destinations', () {
    final pending = PendingNavigation();
    final rejected = <String>[
      'https://evil.example/tasks',
      '//evil.example/tasks',
      '/tasks/not-a-uuid/import',
      '/tasks/not-a-uuid/progress',
      '/tasks/$taskId/import?redirect=https://evil.example',
      '/tasks/$taskId/progress?redirect=https://evil.example',
      '/tasks/%2e%2e/login',
      '/tasks/$taskId/import#fragment',
      '/login',
    ];

    for (final value in rejected) {
      expect(pending.capture(Uri.parse(value)), isFalse, reason: value);
      expect(pending.peek(), isNull, reason: value);
    }
  });

  test('consumes a destination once only after it is reached', () {
    final pending = PendingNavigation()
      ..capture(Uri.parse('/tasks/$taskId/import'));

    expect(pending.consumeIfReached(Uri.parse('/tasks')), isFalse);
    expect(pending.peek(), '/tasks/$taskId/import');
    expect(
      pending.consumeIfReached(Uri.parse('/tasks/$taskId/import')),
      isTrue,
    );
    expect(pending.peek(), isNull);
    expect(
      pending.consumeIfReached(Uri.parse('/tasks/$taskId/import')),
      isFalse,
    );
  });
}
