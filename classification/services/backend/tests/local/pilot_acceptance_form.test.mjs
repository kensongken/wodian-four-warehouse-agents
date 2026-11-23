import assert from 'node:assert/strict';
import test from 'node:test';

import { createPilotAcceptanceFormState } from '../../src/product_engine/api/pilot_acceptance_form.mjs';

test('green permits all decisions and reuses request id for retry', () => {
  let sequence = 0;
  const state = createPilotAcceptanceFormState(() => `request-${++sequence}`);
  const file = { name: 'review.json' };
  state.applyInspection({ signal: 'green', code: 'verified_current' }, file);
  state.updateDraft({ decision: 'accepted', reviewerId: 'R1', note: '本地小试' });
  assert.deepEqual(state.allowedDecisions(), ['accepted', 'needs_follow_up', 'rejected']);
  assert.equal(state.submission().requestId, 'request-1');
  assert.equal(state.submission().requestId, 'request-1');
});

test('yellow excludes accepted and red disables every decision', () => {
  const state = createPilotAcceptanceFormState(() => 'request-1');
  state.applyInspection({ signal: 'yellow', code: 'verified_stale' }, {});
  assert.deepEqual(state.allowedDecisions(), ['needs_follow_up', 'rejected']);
  state.updateDraft({ decision: 'accepted', reviewerId: 'R1', note: '不应通过' });
  assert.equal(state.submission(), null);
  state.applyInspection({ signal: 'red', code: 'package_tampered' }, {});
  assert.deepEqual(state.allowedDecisions(), []);
});

test('changing normalized draft creates a new request id', () => {
  let sequence = 0;
  const state = createPilotAcceptanceFormState(() => `request-${++sequence}`);
  state.applyInspection({ signal: 'green', code: 'verified_current' }, {});
  state.updateDraft({ decision: 'accepted', reviewerId: ' R1 ', note: ' 本地小试 ' });
  assert.equal(state.submission().requestId, 'request-1');
  state.updateDraft({ decision: 'accepted', reviewerId: 'R1', note: '本地小试' });
  assert.equal(state.submission().requestId, 'request-1');
  state.updateDraft({ decision: 'rejected', reviewerId: 'R1', note: '本地小试' });
  assert.equal(state.submission().requestId, 'request-2');
});

test('new inspection resets retry identity and successful submission clears it', () => {
  let sequence = 0;
  const state = createPilotAcceptanceFormState(() => `request-${++sequence}`);
  state.applyInspection({ signal: 'green', code: 'verified_current' }, { name: 'a' });
  state.updateDraft({ decision: 'accepted', reviewerId: 'R1', note: '本地小试' });
  assert.equal(state.submission().requestId, 'request-1');
  state.markSubmitted();
  assert.equal(state.submission().requestId, 'request-2');
  state.reset();
  assert.equal(state.submission(), null);
});
