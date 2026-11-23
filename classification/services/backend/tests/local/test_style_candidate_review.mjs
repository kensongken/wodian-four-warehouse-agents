import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {
  createCandidateReviewController,
  candidateReviewMarkup,
} from '../../src/product_engine/api/style_candidate_review.mjs';

function job(round = 1, extra = {}) {
  return {
    job_id: `job-${round}`, current_job_id: `job-${round}`, handoff_id: 'handoff-1',
    session_id: 'session-1', output_type: 'scene', status: 'awaiting_review',
    session_status: 'awaiting_review', job_status: 'awaiting_customer_review',
    regeneration_count: round - 1, source_product_ids: ['product-1'],
    source_asset_ids: ['asset-1'], synthetic_test_only: true, publishable: false,
    candidates: [1, 2, 3].map(number => ({
      candidate_id: `candidate-${round}-${number}`, sequence_number: number,
      content_url: `/local/api/style-jobs/job-${round}/candidates/candidate-${round}-${number}/image`,
      content_sha256: 'a'.repeat(64), mime_type: 'image/png', width: 24, height: 18,
      identity: 'style_candidate', synthetic_test_only: true, publishable: false,
    })), ...extra,
  };
}

function setup(overrides = {}) {
  const frames = [];
  const calls = [];
  let nextKey = 0;
  const controller = createCandidateReviewController({
    fetchJob: async id => job(Number(id.split('-')[1])),
    review: async intent => {
      calls.push(intent);
      return { job_id: intent.jobId, candidate_id: intent.candidateId, decision: intent.decision,
        status: 'approved', regeneration_count: 0, next_job_id: null,
        product_associations: [{ product_id: 'product-1', approved_image: {candidate_id: intent.candidateId}, real_photo: {asset_id: 'asset-1'} }],
        publishable: false, synthetic_test_only: true };
    },
    onRender: view => frames.push(view), newKey: () => `intent-${++nextKey}`,
    ...overrides,
  });
  return { controller, calls, last: () => frames.at(-1) };
}

test('three accessible cards toggle independently to their owned real sources', async () => {
  const {controller, last, calls} = setup();
  await controller.load('job-1');
  assert.equal(last().candidates.length, 3);
  controller.toggleReal('candidate-1-1');
  assert.deepEqual(last().candidates.map(card => card.visibleKind), ['real_photo', 'style_image', 'style_image']);
  const html = candidateReviewMarkup(last());
  assert.equal((html.match(/<article\b/g) || []).length, 3);
  assert.match(html, /alt="候选 1 对应的实拍图 1"/);
  assert.match(html, /src="\/local\/api\/style-jobs\/job-1\/assets\/asset-1\/image"/);
  assert.match(html, /查看风格图/);
  assert.match(html, /采用这张/);
  assert.match(html, /不像我的风格/);
  assert.match(html, /商品细节不准确/);
  assert.equal(calls.length, 0);
  controller.toggleReal('candidate-1-1');
  assert.equal(last().candidates[0].visibleKind, 'style_image');
});

test('delayed negative acknowledgement follows the live cursor without adopting the rejected card', async () => {
  const reads = [];
  const {controller, last} = setup({fetchJob: async id => {
    reads.push(id);
    if (id === 'job-4') return job(4, {status: 'approved', job_status: 'customer_approved',
      approved_candidate_id: 'candidate-4-2', approved_associations: [{product_id: 'product-1',
        candidate_id: 'candidate-4-2', asset_id: 'asset-1'}]});
    return job(Number(id.split('-')[1]));
  }, review: async () => ({status: 'awaiting_review', outcome: 'regenerated', regeneration_count: 1,
    next_job_id: 'job-2', current_job_id: 'job-4', current_session_status: 'approved',
    current_regeneration_count: 3, synthetic_test_only: true, publishable: false})});
  await controller.load('job-1');
  await controller.reject('candidate-1-1', 'not_my_style');
  assert.deepEqual(reads, ['job-1', 'job-4']);
  assert.equal(last().status, 'customer_approved');
  assert.equal(last().candidates.filter(card => card.adopted)[0].candidateId, 'candidate-4-2');
});

test('failed generation reload is a final empty manual view with no retry or write actions', async () => {
  const failed = job(2, {status: 'manual_attention_required', job_status: 'generation_failed', candidates: []});
  let writes = 0;
  const {controller, last} = setup({fetchJob: async id => id === 'job-1' ? job() : failed,
    review: async () => {writes++; return {status: 'manual_attention_required',
      outcome: 'manual_generation_unavailable', regeneration_count: 1, next_job_id: 'job-2',
      current_job_id: 'job-2', current_session_status: 'manual_attention_required',
      current_regeneration_count: 1, synthetic_test_only: true, publishable: false};}});
  await controller.load('job-1');
  await controller.reject('candidate-1-1', 'not_my_style');
  await controller.retry();
  await controller.load('job-2');
  assert.equal(last().status, 'manual_attention_required');
  assert.equal(last().candidates.length, 0);
  assert.equal(last().canRetry, false);
  assert.equal(last().canReview, false);
  assert.match(candidateReviewMarkup(last()), /本轮未生成可用图片，已转人工处理/);
  assert.doesNotMatch(candidateReviewMarkup(last()), /data-action="(?:approved|not_my_style|retry)"/);
  assert.equal(writes, 1);
});

test('real dual-backend generation-failure responses reach the controller and survive Chromium reload', {
  skip: !process.env.PLAYWRIGHT_MODULE_PATH || !process.env.REVIEW_ACCEPTANCE_MANIFEST,
}, async () => {
  const manifest = JSON.parse(await readFile(process.env.REVIEW_ACCEPTANCE_MANIFEST, 'utf8'));
  assert.equal(manifest.result, 'PASS');
  const fixture = manifest.review_edge_cases.failure_ui_fixture;
  assert.equal(fixture.origin, 'real_consumer_client_proxy_projection');
  let written = false;
  let writes = 0;
  const frames = [];
  const controller = createCandidateReviewController({
    fetchJob: async id => id === fixture.failed.job_id ? fixture.failed : written ? fixture.restored : fixture.initial,
    review: async () => {written = true; writes++; return fixture.decision;},
    onRender: frame => frames.push(frame), newKey: () => 'recorded-backend-failure',
  });
  await controller.load(fixture.initial.job_id);
  await controller.reject(fixture.initial.candidates[0].candidate_id, 'not_my_style');
  await controller.load(fixture.initial.job_id);
  assert.equal(frames.at(-1).status, 'manual_attention_required');
  assert.equal(frames.at(-1).canRetry, false);
  assert.equal(frames.at(-1).canReview, false);
  assert.equal(writes, 1);

  written = false;
  writes = 0;
  const {chromium} = await import(process.env.PLAYWRIGHT_MODULE_PATH);
  const module = await readFile(new URL('../../src/product_engine/api/style_candidate_review.mjs', import.meta.url), 'utf8');
  const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=', 'base64');
  const server = createServer(async (request, response) => {
    const send = body => {response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(body));};
    if (request.url === '/') {
      response.setHeader('Content-Type', 'text/html; charset=utf-8');
      response.end(`<main id="review"></main><script type="module">import {mountCandidateReview} from '/review.mjs'; mountCandidateReview(document.querySelector('#review')).load('${fixture.initial.job_id}');</script>`);
    } else if (request.url === '/review.mjs') {
      response.setHeader('Content-Type', 'text/javascript'); response.end(module);
    } else if (request.url.endsWith('/image')) {
      response.setHeader('Content-Type', 'image/png'); response.end(pixel);
    } else if (request.method === 'POST') {
      let body = '';
      for await (const chunk of request) body += chunk;
      assert.equal(JSON.parse(body).decision, 'not_my_style');
      assert.equal(request.url, `/local/api/style-jobs/${fixture.initial.job_id}/candidates/${fixture.initial.candidates[0].candidate_id}/review`);
      written = true; writes++; send(fixture.decision);
    } else if (request.url === `/local/api/style-jobs/${fixture.failed.job_id}`) send(fixture.failed);
    else if (request.url === `/local/api/style-jobs/${fixture.initial.job_id}`) send(written ? fixture.restored : fixture.initial);
    else {response.statusCode = 404; send({ok: false});}
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true});
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.getByRole('button', {name: '不像我的风格', exact: true}).click();
    await page.getByText('本轮未生成可用图片，已转人工处理。', {exact: true}).waitFor();
    await page.reload();
    await page.getByText('本轮未生成可用图片，已转人工处理。', {exact: true}).waitFor();
    assert.equal(await page.locator('.candidate-card').count(), 0);
    assert.equal(await page.getByRole('button').count(), 0);
    assert.equal(writes, 1);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
});

test('approval records the returned association and resets the adopted card to style view', async () => {
  const {controller, last, calls} = setup();
  await controller.load('job-1');
  controller.toggleReal('candidate-1-2');
  await controller.approve('candidate-1-2');
  assert.equal(last().status, 'customer_approved');
  assert.equal(last().productAssociations[0].approved_image.candidate_id, 'candidate-1-2');
  assert.equal(last().candidates[1].visibleKind, 'style_image');
  assert.match(candidateReviewMarkup(last()), /实拍图与风格图已关联/);
  assert.doesNotMatch(candidateReviewMarkup(last()), /data-action="approved"/);
  controller.toggleReal('candidate-1-2');
  assert.equal(last().candidates[1].visibleKind, 'real_photo');
  await controller.reject('candidate-1-1', 'not_my_style');
  assert.equal(calls.length, 1);
});

test('three negative responses advance automatically and fourth becomes manual without further submission', async () => {
  const submitted = [];
  const {controller, last} = setup({review: async intent => {
    submitted.push(intent);
    const round = Number(intent.jobId.split('-')[1]);
    return {status: round === 4 ? 'manual_attention_required' : 'awaiting_review',
      next_job_id: round === 4 ? null : `job-${round + 1}`, regeneration_count: Math.min(round, 3),
      product_associations: [], publishable: false, synthetic_test_only: true};
  }});
  await controller.load('job-1');
  for (let round = 1; round <= 4; round++) {
    await controller.reject(`candidate-${round}-1`, round === 2 ? 'product_detail_inaccurate' : 'not_my_style');
    assert.equal(last().roundNumber, Math.min(round + 1, 4));
  }
  assert.equal(last().status, 'manual_attention_required');
  assert.match(candidateReviewMarkup(last()), /已转人工处理/);
  assert.doesNotMatch(candidateReviewMarkup(last()), /data-action="not_my_style"/);
  await controller.reject('candidate-4-1', 'not_my_style');
  assert.equal(submitted.length, 4);
  assert.equal(submitted[1].decision, 'product_detail_inaccurate');
});

test('lost write response retains original intent key and retry cannot repeat a new decision', async () => {
  const sent = [];
  const {controller, last} = setup({review: async intent => {
    sent.push({...intent});
    if (sent.length === 1) throw new TypeError('network lost');
    return {status: 'awaiting_review', next_job_id: 'job-2', regeneration_count: 1,
      product_associations: [], publishable: false, synthetic_test_only: true};
  }});
  await controller.load('job-1');
  await controller.reject('candidate-1-1', 'not_my_style');
  assert.equal(last().busy, false);
  assert.equal(last().canRetry, true);
  await controller.approve('candidate-1-2');
  assert.equal(sent.length, 1);
  await controller.retry();
  assert.deepEqual(sent[1], sent[0]);
  assert.equal(sent[1].idempotencyKey, 'intent-1');
  assert.equal(last().roundNumber, 2);
  await controller.reject('candidate-2-1', 'not_my_style');
  assert.equal(sent[2].idempotencyKey, 'intent-2');
});

test('double click sends once and real toggle stays usable while write is pending', async () => {
  let finish;
  const sent = [];
  const {controller, last} = setup({review: intent => {sent.push(intent); return new Promise(resolve => {finish = resolve;});}});
  await controller.load('job-1');
  const first = controller.approve('candidate-1-1');
  await controller.approve('candidate-1-1');
  assert.equal(sent.length, 1);
  assert.equal(last().busy, true);
  controller.toggleReal('candidate-1-1');
  assert.equal(last().candidates[0].visibleKind, 'real_photo');
  const html = candidateReviewMarkup(last());
  assert.match(html, /data-action="approved"[^>]*disabled/);
  assert.doesNotMatch(html, /data-action="toggle"[^>]*disabled/);
  finish({status: 'approved', regeneration_count: 0, next_job_id: null, product_associations: [], publishable: false, synthetic_test_only: true});
  await first;
});

test('failed next-round read retries only reading, never resubmits the accepted feedback', async () => {
  let reads = 0;
  let writes = 0;
  const {controller, last} = setup({
    fetchJob: async id => { if (id === 'job-2' && ++reads === 1) throw new Error('unavailable'); return job(Number(id.split('-')[1])); },
    review: async () => { writes++; return {status: 'awaiting_review', next_job_id: 'job-2', regeneration_count: 1, product_associations: [], publishable: false, synthetic_test_only: true}; },
  });
  await controller.load('job-1');
  await controller.reject('candidate-1-1', 'not_my_style');
  assert.equal(last().canRetry, true);
  await controller.retry();
  assert.equal(writes, 1);
  assert.equal(last().jobId, 'job-2');
});

test('a definitive stale-round rejection offers read recovery instead of replaying a forbidden write', async () => {
  let submitted = 0;
  const {controller, last} = setup({
    fetchJob: async id => submitted && id === 'job-1' ? job(1, {current_job_id: 'job-2'}) : job(Number(id.split('-')[1])),
    review: async () => {submitted++; throw {retryable: false, error_code: 'review_candidate_not_current', message_zh: '请刷新后重新选择。'};},
  });
  await controller.load('job-1');
  await controller.approve('candidate-1-1');
  await controller.retry();
  assert.equal(submitted, 1);
  assert.equal(last().jobId, 'job-2');
  assert.equal(last().canReview, true);
});

test('restored old jobs follow consumer current job and stale reads cannot cross batches', async () => {
  let finishOld;
  const {controller, last} = setup({fetchJob: async id => {
    if (id === 'slow') return new Promise(resolve => {finishOld = resolve;});
    if (id === 'job-1') return job(1, {current_job_id: 'job-2', regeneration_count: 1});
    return job(2);
  }});
  const old = controller.load('slow');
  await controller.load('job-1');
  finishOld(job(1));
  await old;
  assert.equal(last().jobId, 'job-2');
  assert.equal(last().roundNumber, 2);
  controller.reset();
  assert.equal(last().candidates.length, 0);
});

test('approved reload uses this job’s authoritative adopted card and associations', async () => {
  const {controller, last, calls} = setup({fetchJob: async () => job(1, {
    status: 'approved', job_status: 'customer_approved', approved_candidate_id: 'candidate-1-2',
    approved_associations: [{product_id: 'product-1', candidate_id: 'candidate-1-2', asset_id: 'asset-1'}],
  })});
  await controller.load('job-1');
  assert.equal(last().candidates[1].adopted, true);
  assert.equal(last().productAssociations[0].real_photo.asset_id, 'asset-1');
  controller.toggleReal('candidate-1-2');
  assert.equal(last().candidates[1].visibleKind, 'real_photo');
  await controller.approve('candidate-1-1');
  assert.equal(calls.length, 0);
});

test('manual reload and stale recovery retain three rejected candidates read-only', async () => {
  for (const stale of [false, true]) {
    let writes = 0;
    const {controller, last} = setup({
      fetchJob: async id => !stale || writes ? (id === 'job-4' ? job(4, {
        status: 'manual_attention_required', job_status: 'customer_rejected',
      }) : job(1, {current_job_id: 'job-4'})) : job(1),
      review: async () => {writes++; throw {retryable: false, message_zh: '当前轮次已结束'};},
    });
    await controller.load(stale ? 'job-1' : 'job-4');
    if (stale) {await controller.reject('candidate-1-1', 'not_my_style'); await controller.retry();}
    assert.equal(last().candidates.length, 3);
    assert.equal(last().status, 'manual_attention_required');
    controller.toggleReal('candidate-4-2');
    assert.equal(last().candidates[1].visibleKind, 'real_photo');
    await controller.approve('candidate-4-1');
    assert.equal(writes, stale ? 1 : 0);
  }
});

test('incomplete, unchecked and unsafe candidates never offer review actions', async () => {
  for (const payload of [job(1, {candidates: job().candidates.slice(0, 2)}),
    job(1, {job_status: 'authenticity_failed'}), job(1, {publishable: true})]) {
    const {controller, last, calls} = setup({fetchJob: async () => payload});
    await controller.load('job-1');
    await controller.approve('candidate-1-1');
    assert.equal(calls.length, 0);
    assert.doesNotMatch(candidateReviewMarkup(last()), /data-action="approved"/);
  }
});

test('broken media removes approval until a successful read retry and preserves all real sources', async () => {
  const {controller, last, calls} = setup({fetchJob: async () => job(1, {source_asset_ids: ['asset-1', 'asset-2']})});
  await controller.load('job-1');
  controller.toggleReal('candidate-1-1');
  assert.match(candidateReviewMarkup(last()), /assets\/asset-2\/image/);
  controller.mediaFailed('candidate-1-1');
  await controller.approve('candidate-1-2');
  assert.equal(calls.length, 0);
  assert.doesNotMatch(candidateReviewMarkup(last()), /data-action="approved"/);
  await controller.retry();
  assert.equal(last().canReview, true);
});

test('unsafe labels are escaped and server URLs cannot become external media', async () => {
  const payload = job(1, {source_product_ids: ['<script>alert(1)</script>']});
  payload.candidates[0].content_url = 'https://external.invalid/customer-photo';
  const {controller, last} = setup({fetchJob: async () => payload});
  await controller.load('job-1');
  const html = candidateReviewMarkup(last());
  assert.doesNotMatch(html, /<script>|https:\/\/external/);
  assert.match(html, /&lt;script&gt;/);
});

test('actual page wires generation, media toggle, lost-response retry and approval on narrow screens', {
  skip: !process.env.PLAYWRIGHT_MODULE_PATH,
}, async () => {
  const {chromium} = await import(process.env.PLAYWRIGHT_MODULE_PATH);
  const backendRoot = fileURLToPath(new URL('../..', import.meta.url));
  const pythonExecutable = process.env.PYTHON_EXECUTABLE || `${backendRoot}/.venv/bin/python`;
  const panel = spawnSync(pythonExecutable, ['-c',
    'import re; from product_engine.api.local_app import _style_bridge_panel, PAGE_PATH; print("".join(re.findall(r"<style>.*?</style>", PAGE_PATH.read_text(), re.S)) + _style_bridge_panel())'],
  {cwd: backendRoot, env: {...process.env, PYTHONPATH: `${backendRoot}/src`}, encoding: 'utf8'});
  assert.equal(panel.status, 0, panel.stderr);
  const modules = new Map(await Promise.all(['photo_usage_preview', 'style_candidate_review'].map(async name => [
    `/local/assets/${name.replaceAll('_', '-')}.mjs`,
    await readFile(new URL(`../../src/product_engine/api/${name}.mjs`, import.meta.url), 'utf8'),
  ])));
  const writes = [];
  const reviews = new Map();
  let handoffExists = false;
  let latestHandoffBatchId = 'batch-1';
  let terminalMode = '';
  let realReviewReads = 0;
  const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=', 'base64');
  const server = createServer(async (request, response) => {
    const path = request.url;
    if (modules.has(path)) {response.setHeader('Content-Type', 'text/javascript'); response.end(modules.get(path)); return;}
    if (path.endsWith('/image')) {response.setHeader('Content-Type', 'image/png'); response.end(pixel); return;}
    response.setHeader('Content-Type', 'application/json');
    const send = payload => response.end(JSON.stringify(payload));
    if (path === '/') {response.setHeader('Content-Type', 'text/html; charset=utf-8'); response.end(`<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head><body>${panel.stdout}</body></html>`); return;}
    if (path === '/local/api/style-test-products') {send({products: [{product_id: 'product-1', customer_test_id: 'customer-test', display_label: '测试商品'}]}); return;}
    if (path === '/local/api/style-test-target-batches') {send({ok: true, targets: [{batch_id: 'batch-1', product_id: 'product-1', display_label: '测试商品'}]}); return;}
    if (path.endsWith('/style-readiness')) {send({ok: true, can_confirm: true,
      batch: {batch_id: 'batch-1', items: [{item_id: 'asset-1'}]}, missing_codes: [],
      items: [{state: 'accepted', automatic_category: 'real_photo', final_category: 'real_photo',
        item: {item_id: 'asset-1', filename: '测试实拍.png', position: 0}}]}); return;}
    if (path.endsWith('/confirm-and-generate')) {handoffExists = true; send({ok: true, handoff_id: 'handoff-1'}); return;}
    if (path === '/local/api/latest-style-handoff') {
      if (!handoffExists) {response.statusCode = 404; send({ok: false}); return;}
      send({ok: true, handoff: {batch_id: latestHandoffBatchId, current_status: 'awaiting_review',
        output_job_ids: ['job-real', 'job-failed', 'job-1'], output_jobs: [
          {job_id: 'job-real', output_type: 'real_photo', status: 'awaiting_customer_review'},
          {job_id: 'job-failed', output_type: 'scene', status: 'authenticity_failed'},
          {job_id: 'job-1', output_type: 'scene', status: 'awaiting_customer_review'},
        ]}}); return;
    }
    if (path === '/local/api/style-handoffs/handoff-1') {send({ok: true, handoff: {
      current_status: 'awaiting_review', output_job_ids: ['job-real', 'job-failed', 'job-1'], output_jobs: [
        {job_id: 'job-real', output_type: 'real_photo', status: 'awaiting_customer_review'},
        {job_id: 'job-failed', output_type: 'scene', status: 'authenticity_failed'},
        {job_id: 'job-1', output_type: 'scene', status: 'awaiting_customer_review'},
      ]}}); return;}
    if (path === '/local/api/style-jobs/job-real') {realReviewReads++; response.statusCode = 503; send({ok: false}); return;}
    if (path === '/local/api/style-jobs/job-1') {send(terminalMode ? job(1, {
      current_job_id: terminalMode === 'approved' ? 'job-2' : 'job-4',
      status: terminalMode === 'approved' ? 'approved' : 'manual_attention_required',
      job_status: 'customer_rejected',
    }) : job()); return;}
    if (path === '/local/api/style-jobs/job-2') {send(terminalMode === 'approved' ? job(2, {
      status: 'approved', job_status: 'customer_approved', approved_candidate_id: 'candidate-2-2',
      approved_associations: [{product_id: 'product-1', candidate_id: 'candidate-2-2', asset_id: 'asset-1'}],
    }) : job(2)); return;}
    if (path === '/local/api/style-jobs/job-4') {send(job(4, {status: 'manual_attention_required', job_status: 'customer_rejected'})); return;}
    if (path.endsWith('/review')) {
      let body = '';
      for await (const chunk of request) body += chunk;
      const intent = JSON.parse(body);
      writes.push({path, ...intent});
      if (terminalMode === 'manual') {response.statusCode = 409; send({ok: false, retryable: false, message_zh: '当前轮次已结束，请刷新。'}); return;}
      if (reviews.has(intent.idempotency_key)) {send(reviews.get(intent.idempotency_key)); return;}
      const result = {status: intent.decision === 'approved' ? 'approved' : 'awaiting_review',
        next_job_id: intent.decision === 'approved' ? null : 'job-2', regeneration_count: 1,
        synthetic_test_only: true, publishable: false,
        product_associations: intent.decision === 'approved' ? [{product_id: 'product-1',
          approved_image: {candidate_id: 'candidate-2-2'}, real_photo: {asset_id: 'asset-1'}}] : []};
      reviews.set(intent.idempotency_key, result);
      if (intent.decision === 'approved') terminalMode = 'approved';
      if (writes.length === 1) {response.statusCode = 503; send({ok: false, retryable: true, message_zh: '暂时没有收到审核结果，请重试。'}); return;}
      send(result); return;
    }
    response.statusCode = 404; send({ok: false});
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true});
    const page = await browser.newPage({viewport: {width: 320, height: 800}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => localStorage.setItem('latestUploadBatchId', 'batch-1'));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    async function openHandoff() {
      await page.locator('#style-bridge-product option[value="product-1"]').waitFor({state: 'attached'});
      await page.locator('#style-bridge-main-photo option[value="asset-1"]').waitFor({state: 'attached'});
      await page.locator('#style-bridge-product').selectOption('product-1');
      await page.locator('#style-bridge-permission').check();
      await page.locator('#style-bridge-generate').click();
    }
    await openHandoff();
    await page.getByRole('article', {name: '候选 3', exact: true}).waitFor();
    await page.getByText('实拍图已整理，作为实物原图保留。', {exact: true}).waitFor();
    await page.getByText('风格图尚未通过检查，暂时不能审核。', {exact: true}).waitFor();
    assert.equal(await page.getByRole('button', {name: '重试刚才的操作', exact: true}).count(), 0);
    assert.equal(await page.locator('.candidate-card').count(), 3);
    await page.locator('.candidate-card').first().getByRole('button', {name: '查看实物图', exact: true}).click();
    assert.equal(await page.getByAltText('候选 1 对应的实拍图 1', {exact: true}).getAttribute('src'), '/local/api/style-jobs/job-1/assets/asset-1/image');
    const boxes = await page.locator('.candidate-card').evaluateAll(cards => cards.map(card => ({x: card.getBoundingClientRect().x, y: card.getBoundingClientRect().y})));
    assert.equal(boxes[0].x, boxes[1].x);
    assert.ok(boxes[1].y > boxes[0].y);
    const overflow = await page.evaluate(() => [...document.querySelectorAll('body *')]
      .filter(element => element.getBoundingClientRect().right > window.innerWidth || element.scrollWidth > element.clientWidth)
      .map(element => ({tag: element.tagName, class: element.className, id: element.id, width: element.getBoundingClientRect().width, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth})));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, JSON.stringify(overflow));
    await page.getByRole('button', {name: '不像我的风格', exact: true}).click();
    await page.getByRole('button', {name: '重试刚才的操作', exact: true}).click();
    await page.getByRole('heading', {name: '场景图 · 第 2 轮 / 最多 4 轮', exact: true}).waitFor();
    assert.equal(writes.length, 2);
    assert.deepEqual(writes[1], writes[0]);
    await page.locator('.candidate-card').nth(1).getByRole('button', {name: '采用这张', exact: true}).click();
    await page.getByText('已采用，实拍图与风格图已关联。', {exact: true}).waitFor();
    assert.equal(await page.getByRole('button', {name: '采用这张', exact: true}).count(), 0);
    await page.locator('.candidate-card').nth(1).getByRole('button', {name: '查看实物图', exact: true}).click();
    await page.getByAltText('候选 2 对应的实拍图 1', {exact: true}).waitFor();
    assert.equal(writes[2].decision, 'approved');
    assert.equal(writes[2].path, '/local/api/style-jobs/job-2/candidates/candidate-2-2/review');
    await page.reload();
    await page.getByRole('heading', {name: '候选 2 · 已采用', exact: true}).waitFor();
    await page.getByText('已关联 1 件商品；默认展示风格图，查看实物时展示原图。', {exact: true}).waitFor();
    assert.equal(await page.getByRole('button', {name: '采用这张', exact: true}).count(), 0);
    assert.equal(writes.length, 3);
    terminalMode = 'manual';
    await page.reload();
    await openHandoff();
    await page.getByText('已转人工处理，保留本轮图片供核对。', {exact: true}).waitFor();
    assert.equal(await page.locator('.candidate-card').count(), 3);
    await page.locator('.candidate-card').nth(1).getByRole('button', {name: '查看实物图', exact: true}).click();
    assert.equal(await page.getByAltText('候选 2 对应的实拍图 1', {exact: true}).getAttribute('src'), '/local/api/style-jobs/job-4/assets/asset-1/image');
    assert.equal(await page.getByRole('button', {name: '采用这张', exact: true}).count(), 0);
    assert.equal(writes.length, 3);
    terminalMode = '';
    await page.reload();
    await openHandoff();
    await page.locator('.candidate-card').first().getByRole('button', {name: '采用这张', exact: true}).waitFor();
    terminalMode = 'manual';
    await page.locator('.candidate-card').first().getByRole('button', {name: '采用这张', exact: true}).click();
    await page.getByRole('button', {name: '重试刚才的操作', exact: true}).click();
    await page.getByText('已转人工处理，保留本轮图片供核对。', {exact: true}).waitFor();
    assert.equal(await page.locator('.candidate-card').count(), 3);
    assert.equal(await page.getByRole('button', {name: '采用这张', exact: true}).count(), 0);
    assert.equal(writes.length, 4);
    assert.equal(realReviewReads, 0);
    latestHandoffBatchId = 'batch-2';
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/local/api/latest-style-handoff')),
      page.reload(),
    ]);
    assert.equal(await page.locator('.candidate-card').count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
});
