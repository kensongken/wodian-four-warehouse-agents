import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/features/controlled_upload/controlled_upload_candidate.dart';

import 'controlled_upload_fixtures.dart';

void main() {
  test('parses a strict candidate and verifies its canonical SHA', () {
    final candidate = ControlledUploadCandidate.fromJson(
      syntheticCandidateJson(),
    );

    expect(candidate.candidateId, syntheticCandidateId);
    expect(
      candidate.candidateSha256,
      'f737916fb538786c4e325fe05a71e92084b00c67c70a4338aa123a989b5739b4',
    );
    expect(candidate.items.map((item) => item.role), <String>[
      'primary',
      'detail',
    ]);
    expect(candidate.claimsNotEvaluated, <String>{
      'authenticity',
      'origin',
      'quality',
      'warehouse_acceptance',
    });
  });

  test('rejects candidate content with a stale candidate SHA', () {
    final json = syntheticCandidateJson()..['album_product_id'] = 'changed';

    expect(
      () => ControlledUploadCandidate.fromJson(json),
      throwsA(_controlledError('invalid_candidate')),
    );
  });

  test('rejects extra fields instead of accepting a URL or token channel', () {
    final json = syntheticCandidateJson()
      ..['download_url'] = 'https://invalid.test/?token=synthetic-secret';

    expect(
      () => ControlledUploadCandidate.fromJson(json),
      throwsA(_controlledError('invalid_candidate')),
    );
  });

  test('rejects evaluated claims and publication boundary drift', () {
    final json = syntheticCandidateJson()
      ..['claims_not_evaluated'] = <Object?>['authenticity']
      ..['auto_publish'] = true;

    expect(
      () => ControlledUploadCandidate.fromJson(json),
      throwsA(_controlledError('invalid_candidate')),
    );
  });

  test('rejects timestamps that are not explicitly UTC', () {
    final json = syntheticCandidateJson()
      ..['authorized_at'] = '2026-08-19T16:00:00+08:00';

    expect(
      () => ControlledUploadCandidate.fromJson(json),
      throwsA(_controlledError('invalid_candidate')),
    );
  });

  test('normalizes accepted UTC fractions exactly like the backend hash', () {
    final json = syntheticCandidateJson()
      ..['authorized_at'] = '2026-08-19T08:00:00.000Z';
    final items = json['items']! as List<Object?>;
    (items.first! as Map<String, Object?>)['captured_at'] =
        '2026-08-19T07:00:00.000Z';

    final candidate = ControlledUploadCandidate.fromJson(json);

    expect(candidate.candidateId, syntheticCandidateId);
    expect(candidate.toRequestJson()['authorized_at'], '2026-08-19T08:00:00Z');
    expect(
      ((candidate.toRequestJson()['items']! as List<Object?>).first!
          as Map<String, Object?>)['captured_at'],
      '2026-08-19T07:00:00Z',
    );
  });

  test(
    'rejects year zero because the backend datetime contract rejects it',
    () {
      final json = syntheticCandidateJson()
        ..['authorized_at'] = '0000-08-19T08:00:00Z';

      expect(
        () => ControlledUploadCandidate.fromJson(json),
        throwsA(_controlledError('invalid_candidate')),
      );
    },
  );

  test('approval is bound to the exact candidate and queue-only decision', () {
    final approval = CandidateUploadApproval.approved(
      candidateId:
          'opc-controlled-upload-candidate:'
          'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      confirmedAt: DateTime.utc(2026, 8, 19, 8),
    );

    expect(approval.decision, 'approved_for_upload_queue');
    expect(
      () => approval.requireCandidate(syntheticCandidateId),
      throwsA(_controlledError('human_confirmation_required')),
    );
  });
}

Matcher _controlledError(String code) => isA<ControlledUploadException>()
    .having((error) => error.code, 'code', code);
