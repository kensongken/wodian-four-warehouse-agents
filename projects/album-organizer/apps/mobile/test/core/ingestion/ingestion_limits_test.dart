import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/ingestion/ingestion_limits.dart';

void main() {
  test(
    'shared limits provide ten thousand task slots and a 100 MiB image cap',
    () {
      expect(maximumAcceptedAssets, 10000);
      expect(maximumImageBytes, 100 * 1024 * 1024);
    },
  );
}
