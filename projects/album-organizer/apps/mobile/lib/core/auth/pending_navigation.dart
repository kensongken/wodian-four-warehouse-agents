import 'package:flutter_riverpod/flutter_riverpod.dart';

final pendingNavigationProvider = Provider<PendingNavigation>(
  (_) => PendingNavigation(),
);

final class PendingNavigation {
  static final RegExp _taskDetailPath = RegExp(
    r'^/tasks/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/(?:import|progress)$',
  );
  static final RegExp _reviewDetailPath = RegExp(
    r'^/reviews/[A-Za-z0-9][A-Za-z0-9._:-]{2,127}/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$',
  );

  String? _destination;

  bool capture(Uri uri) {
    final validated = validate(uri);
    _destination = validated;
    return validated != null;
  }

  String? peek() => _destination;

  bool consumeIfReached(Uri current) {
    final destination = _destination;
    if (destination == null || current.toString() != destination) {
      return false;
    }
    _destination = null;
    return true;
  }

  void clear() {
    _destination = null;
  }

  static String? validate(Uri uri) {
    final serialized = uri.toString();
    if (uri.isAbsolute ||
        uri.hasAuthority ||
        serialized.startsWith('//') ||
        uri.hasQuery ||
        uri.hasFragment ||
        serialized.contains('%')) {
      return null;
    }
    if (uri.path == '/tasks' ||
        uri.path == '/reviews' ||
        _taskDetailPath.hasMatch(uri.path) ||
        _reviewDetailPath.hasMatch(uri.path)) {
      return serialized;
    }
    return null;
  }
}
