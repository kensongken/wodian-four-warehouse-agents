import test from 'node:test';
import assert from 'node:assert/strict';
import { createPilotReviewRequestGuard } from '../../src/product_engine/api/pilot_review_request_guard.mjs';

test('starting a new product immediately resets download and rejects the older response', () => {
  const resets = [];
  const guard = createPilotReviewRequestGuard(templateCode => resets.push(templateCode));

  const greenProductRequest = guard.begin('GREEN-A');
  const yellowProductRequest = guard.begin('YELLOW-B');

  assert.deepEqual(resets, ['GREEN-A', 'YELLOW-B']);
  assert.equal(guard.isCurrent(greenProductRequest, 'YELLOW-B'), false);
  assert.equal(guard.isCurrent(yellowProductRequest, 'YELLOW-B'), true);
});

test('a later request for the same product supersedes its older response', () => {
  const guard = createPilotReviewRequestGuard(() => {});
  const beforeConfirmation = guard.begin('PRODUCT-A');
  const afterConfirmation = guard.begin('PRODUCT-A');

  assert.equal(guard.isCurrent(beforeConfirmation, 'PRODUCT-A'), false);
  assert.equal(guard.isCurrent(afterConfirmation, 'PRODUCT-A'), true);
});
