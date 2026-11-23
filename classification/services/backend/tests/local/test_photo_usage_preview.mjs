import test from 'node:test';
import assert from 'node:assert/strict';
import * as preview from '../../src/product_engine/api/photo_usage_preview.mjs';
import {
  bridgeErrorMessage,
  createBatchBridgeController,
  eligibleMainPhotoItems,
  newBatchState,
  readinessStatusMessage,
  recoveryActionNames,
  statusState,
  technicalDetails,
  visibleState,
} from '../../src/product_engine/api/photo_usage_preview.mjs';

test('keeps only customer-facing bridge states', () => {
  assert.equal(visibleState({ selectedCount: 0 }), 'awaiting_selection');
  assert.equal(visibleState({ selectedCount: 1, organizing: true }), 'organizing');
  assert.equal(visibleState({ selectedCount: 1, missingCodes: ['product_confirmation_required'] }), 'needs_confirmation');
  assert.equal(visibleState({ selectedCount: 1, submitting: true }), 'generating');
});

test('new batches reset permission and reject stale responses', () => {
  assert.deepEqual(newBatchState('B'), {
    batchId: 'B',
    permission: false,
    submitting: false,
    generation: 1,
    selectionRevision: 0,
    selectionFingerprint: '["","",false]',
  });
});

test('rapid A to B cannot render A readiness or permission into B', async () => {
  const resolvers = new Map();
  const rendered = [];
  const controller = createBatchBridgeController({
    fetchReadiness: (batchId) => new Promise(resolve => resolvers.set(batchId, resolve)),
    render: (payload, state) => rendered.push({ payload, state }),
  });
  const pendingA = controller.switchBatch('A');
  const pendingB = controller.switchBatch('B');
  resolvers.get('A')({ batch: { items: [{ item_id: 'a' }] }, permission: { transform_permission: true } });
  await pendingA;
  resolvers.get('B')({ batch: { items: [{ item_id: 'b' }] }, permission: null });
  await pendingB;
  assert.equal(rendered.some(entry => entry.payload?.batch?.items?.[0]?.item_id === 'a'), false);
  assert.equal(rendered.at(-1).payload.batch.items[0].item_id, 'b');
  assert.deepEqual(controller.snapshot(), {
    batchId: 'B',
    permission: false,
    submitting: false,
    generation: 2,
    selectionRevision: 0,
    selectionFingerprint: '["","",false]',
  });
});

test('maps durable consumer statuses to the four page states', () => {
  assert.equal(statusState('processing'), 'generating');
  assert.equal(statusState('needs_confirmation'), 'needs_confirmation');
  assert.equal(statusState('pending_real_model'), 'needs_confirmation');
  assert.equal(statusState('awaiting_review'), 'awaiting_selection');
  assert.equal(statusState('consumer_added_a_new_status'), 'needs_confirmation');
});

test('same-batch product main or permission revision aborts an old submit and poll', async () => {
  const revisedSelections = [
    { productId: 'product-b', mainPhotoId: 'main-a', permission: true },
    { productId: 'product-a', mainPhotoId: 'main-b', permission: true },
    { productId: 'product-a', mainPhotoId: 'main-a', permission: false },
  ];
  for (const revisedSelection of revisedSelections) {
    let resolveSubmit;
    let resolvePoll;
    const successes = [];
    const statuses = [];
    const controller = createBatchBridgeController({
      fetchReadiness: async batchId => ({
        can_confirm: true,
        batch: { batch_id: batchId, items: [{ item_id: 'main-a' }, { item_id: 'main-b' }] },
      }),
      render: () => {},
    });
    await controller.switchBatch('A');
    controller.updateSelection({ productId: 'product-a', mainPhotoId: 'main-a', permission: true });
    const beforeSubmit = controller.snapshot();
    const pendingSubmit = controller.runBatchAction({
      batchId: 'A',
      selectionSensitive: true,
      request: () => new Promise(resolve => { resolveSubmit = resolve; }),
      onSuccess: payload => successes.push(payload),
    });
    assert.equal(controller.snapshot().submitting, true);
    assert.equal(controller.visibleState(2), 'generating');

    const pendingPoll = controller.pollHandoff(
      'handoff-a',
      () => new Promise(resolve => { resolvePoll = resolve; }),
      (payload, state) => statuses.push({ payload, state }),
    );
    controller.updateSelection(revisedSelection);
    const afterSelection = controller.snapshot();
    resolveSubmit({ ok: true, handoff_id: 'handoff-a' });
    resolvePoll({ ok: true, handoff: { current_status: 'awaiting_review' } });

    assert.deepEqual(await pendingSubmit, { status: 'stale' });
    await pendingPoll;
    assert.equal(afterSelection.selectionRevision, beforeSubmit.selectionRevision + 1);
    assert.notEqual(afterSelection.selectionFingerprint, beforeSubmit.selectionFingerprint);
    assert.equal(afterSelection.submitting, false);
    assert.deepEqual(successes, []);
    assert.deepEqual(statuses, []);
  }
});

test('same selection keeps its revision and submitting state authoritative', async () => {
  let resolveSubmit;
  const controller = createBatchBridgeController({
    fetchReadiness: async batchId => ({
      can_confirm: true,
      batch: { batch_id: batchId, items: [{ item_id: 'main-a' }] },
    }),
    render: () => {},
  });
  await controller.switchBatch('A');
  const selection = { productId: 'product-a', mainPhotoId: 'main-a', permission: true };
  controller.updateSelection(selection);
  const revision = controller.snapshot().selectionRevision;
  const pending = controller.runBatchAction({
    batchId: 'A',
    selectionSensitive: true,
    request: () => new Promise(resolve => { resolveSubmit = resolve; }),
  });

  controller.updateSelection(selection);
  assert.equal(controller.snapshot().selectionRevision, revision);
  assert.equal(controller.snapshot().submitting, true);
  assert.equal(controller.visibleState(1), 'generating');
  resolveSubmit({ ok: true });
  assert.deepEqual(await pending, { status: 'success' });
  assert.equal(controller.snapshot().submitting, false);
});

test('delayed correction delete move reupload and submit actions from A cannot refresh B', async () => {
  for (const actionName of ['correction', 'delete', 'move', 'reupload', 'submit']) {
    let resolveAction;
    const readinessRequests = [];
    const actionEffects = [];
    const controller = createBatchBridgeController({
      fetchReadiness: async batchId => {
        readinessRequests.push(batchId);
        return { batch: { batch_id: batchId, items: [{ item_id: batchId.toLowerCase() }] } };
      },
      render: () => {},
    });
    await controller.switchBatch('A');
    const pendingAction = controller.runBatchAction({
      batchId: 'A',
      request: () => new Promise(resolve => { resolveAction = resolve; }),
      refresh: true,
      onSuccess: () => actionEffects.push(`${actionName}:success`),
      onError: message => actionEffects.push(`${actionName}:${message}`),
    });
    await controller.switchBatch('B');
    resolveAction({ ok: true, batch: { batch_id: 'A' } });

    assert.deepEqual(await pendingAction, { status: 'stale' }, actionName);
    assert.deepEqual(readinessRequests, ['A', 'B'], actionName);
    assert.deepEqual(actionEffects, [], actionName);
    assert.equal(controller.snapshot().batchId, 'B', actionName);
  }
});

test('a delayed A poll cannot render a status after switching to B', async () => {
  let resolvePoll;
  const statuses = [];
  const errors = [];
  const controller = createBatchBridgeController({
    fetchReadiness: async batchId => ({ batch: { batch_id: batchId, items: [] } }),
    render: () => {},
  });
  await controller.switchBatch('A');
  const pendingPoll = controller.pollHandoff(
    'handoff-a',
    () => new Promise(resolve => { resolvePoll = resolve; }),
    (payload, state) => statuses.push({ payload, state }),
    message => errors.push(message),
  );
  await controller.switchBatch('B');
  resolvePoll({ ok: true, handoff: { current_status: 'awaiting_review' } });
  await pendingPoll;

  assert.deepEqual(statuses, []);
  assert.deepEqual(errors, []);
  assert.equal(controller.snapshot().batchId, 'B');
});

test('non-ok action and poll responses show Chinese without a false refresh', async () => {
  const readinessRequests = [];
  const successes = [];
  const errors = [];
  const statuses = [];
  const controller = createBatchBridgeController({
    fetchReadiness: async batchId => {
      readinessRequests.push(batchId);
      return { batch: { batch_id: batchId, items: [] } };
    },
    render: () => {},
  });
  await controller.switchBatch('A');

  const actionResult = await controller.runBatchAction({
    batchId: 'A',
    request: async () => ({ ok: false, error_code: 'position_conflict', message_zh: '目标位置已有图片。' }),
    refresh: true,
    onSuccess: payload => successes.push(payload),
    onError: message => errors.push(message),
  });
  await controller.pollHandoff(
    'missing-handoff',
    async () => ({ ok: false, error_code: 'style_handoff_not_found' }),
    (payload, state) => statuses.push({ payload, state }),
    message => errors.push(message),
  );

  assert.deepEqual(actionResult, { status: 'error' });
  assert.deepEqual(readinessRequests, ['A']);
  assert.deepEqual(successes, []);
  assert.deepEqual(statuses, []);
  assert.deepEqual(errors, ['目标位置已有图片。', '找不到这个本地风格图任务。']);
});

test('retryable poll keeps the handoff and retry is invalidated by a batch switch', async () => {
  const statuses = [];
  const errors = [];
  const retries = [];
  let fetchCount = 0;
  const controller = createBatchBridgeController({
    fetchReadiness: async batchId => ({ batch: { batch_id: batchId, items: [] } }),
    render: () => {},
  });
  await controller.switchBatch('A');

  const fetchHandoff = async handoffId => {
    fetchCount += 1;
    assert.equal(handoffId, 'handoff-a');
    if (fetchCount === 1) {
      return {
        ok: false,
        retryable: true,
        error_code: 'classification_handoff_cleanup_pending',
        message_zh: '暂时无法读取风格图任务，请稍后重试。',
      };
    }
    return { ok: true, handoff: { current_status: 'awaiting_review' } };
  };
  await controller.pollHandoff(
    'handoff-a',
    fetchHandoff,
    (payload, state) => statuses.push({ payload, state }),
    message => errors.push(message),
    retry => retries.push(retry),
  );

  assert.equal(fetchCount, 1);
  assert.deepEqual(errors, ['暂时无法读取风格图任务，请稍后重试。']);
  assert.equal(retries.length, 1);
  await retries[0]();
  assert.equal(fetchCount, 2);
  assert.equal(statuses.at(-1).state, 'awaiting_selection');

  await controller.pollHandoff(
    'handoff-a',
    async () => ({ ok: false, retryable: true, message_zh: '请重试。' }),
    () => {},
    () => {},
    retry => retries.push(retry),
  );
  await controller.switchBatch('B');
  assert.deepEqual(await retries.at(-1)(), { status: 'stale' });
  assert.equal(fetchCount, 2);
});

test('red recovery main-photo and raw-code presentation contracts stay separate', () => {
  assert.deepEqual(recoveryActionNames({ state: 'failed' }), ['reupload', 'delete', 'move']);
  assert.deepEqual(recoveryActionNames({ state: 'duplicate' }), ['reupload', 'delete', 'move']);
  assert.deepEqual(recoveryActionNames({ state: 'accepted' }), []);
  assert.deepEqual(
    recoveryActionNames({ state: 'accepted', recovery_actions: ['delete', 'move'] }),
    ['delete', 'move'],
  );

  const items = [
    { state: 'failed', final_category: 'real_photo', item: { item_id: 'failed' } },
    { state: 'duplicate', final_category: 'real_photo', item: { item_id: 'duplicate' } },
    { state: 'accepted', final_category: 'real_photo', item: { item_id: 'accepted' } },
    { state: 'exception', final_category: 'real_photo', item: { item_id: 'correctable' } },
    { state: 'accepted', final_category: 'scene_photo', item: { item_id: 'scene' } },
  ];
  assert.deepEqual(eligibleMainPhotoItems(items).map(view => view.item.item_id), ['accepted', 'correctable']);

  const codes = ['product_confirmation_required', 'style_service_unavailable', 'future_code'];
  const mainMessage = readinessStatusMessage(codes);
  assert.match(mainMessage, /请选择脱敏测试商品/);
  assert.match(mainMessage, /本地风格测试服务/);
  for (const code of codes) assert.equal(mainMessage.includes(code), false);
  assert.equal(bridgeErrorMessage({ error_code: 'style_handoff_not_found' }), '找不到这个本地风格图任务。');

  const details = technicalDetails({
    missing_codes: codes,
    technical_details: { classifier_version: 'local-photo-usage-rules-v1' },
    items: [{ state: 'failed', reason_code: 'photo_decode_failed', item: { item_id: 'failed' } }],
  });
  const serialized = JSON.stringify(details);
  assert.match(serialized, /product_confirmation_required/);
  assert.match(serialized, /photo_decode_failed/);
});

test('multiple eligible real photos have one deterministic recommendation that stays changeable', () => {
  const choices = eligibleMainPhotoItems([
    { state: 'accepted', final_category: 'real_photo', item: { item_id: 'later', position: 4 } },
    { state: 'failed', final_category: 'real_photo', item: { item_id: 'red', position: 0 } },
    { state: 'exception', final_category: 'real_photo', item: { item_id: 'first', position: 1 } },
    { state: 'duplicate', final_category: 'real_photo', item: { item_id: 'duplicate', position: 2 } },
  ]);

  assert.deepEqual(choices.map(view => view.item.item_id), ['first', 'later']);
  assert.deepEqual(choices.map(view => view.recommended), [true, false]);
  assert.equal(preview.selectMainPhotoId(choices, ''), 'first');
  assert.equal(preview.selectMainPhotoId(choices, 'later'), 'later');
  assert.equal(preview.selectMainPhotoId(choices, 'red'), 'first');
});

test('an enabled confirmation never shows the contradictory cannot-generate copy', () => {
  const message = readinessStatusMessage(
    ['product_confirmation_required', 'main_photo_required', 'transform_permission_required'],
    { canConfirm: true, selectionComplete: true },
  );

  assert.equal(message, '已准备好，请确认商品并生成风格图。');
  assert.equal(message.includes('还不能生成'), false);
});

test('target batch directory reloads persisted choices and excludes the active batch', async () => {
  let calls = 0;
  let targets = [
    { batch_id: 'A', product_id: 'product-a', display_label: '商品 A' },
  ];
  const directory = preview.createTargetBatchDirectory(async () => {
    calls += 1;
    return { ok: true, targets };
  });

  assert.deepEqual(await directory.refresh('A'), []);
  targets = [
    ...targets,
    { batch_id: 'B', product_id: 'product-b', display_label: '商品 B' },
  ];
  assert.deepEqual(await directory.refresh('A'), [
    { batchId: 'B', productId: 'product-b', label: '商品 B' },
  ]);
  assert.equal(calls, 2);
});
