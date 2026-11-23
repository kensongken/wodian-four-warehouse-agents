import {writeFile} from 'node:fs/promises';
import {chromium} from '/path/to/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';

const [pageUrl, packagePath, acceptanceBatchId, evidenceDir] = process.argv.slice(2);
if (![pageUrl, packagePath, acceptanceBatchId, evidenceDir].every(Boolean)) process.exit(2);

const requests = [];
const responses = [];
const backendSteps = new Set();
const nonLoopback = [];
const providerRequests = [];
const origin = new URL(pageUrl).origin;
const browser = await chromium.launch({headless: true});
let page;
try {
  const context = await browser.newContext({viewport: {width: 1280, height: 900}});
  page = await context.newPage();
  page.on('request', request => {
    const url = request.url();
    requests.push({method: request.method(), url});
    const target = new URL(url);
    if (!['127.0.0.1', 'localhost'].includes(target.hostname)) nonLoopback.push(url);
    if (/openai|anthropic|gemini|replicate/i.test(target.hostname)) providerRequests.push(url);
  });
  page.on('response', response => {
    const url = response.url();
    responses.push({status: response.status(), url});
    if (/employee-package\/import$/.test(url)) backendSteps.add('upload');
    if (/recognitions\//.test(url)) backendSteps.add('classification');
    if (/training-results\/.+\/versions$/.test(url) || /photo-category-corrections$/.test(url)) backendSteps.add('human_correction');
    if (/products\/.+\/reviewed$/.test(url)) backendSteps.add('product_identity');
    if (/confirm-and-generate$/.test(url)) {
      backendSteps.add('product_identity');
      backendSteps.add('main_photo');
      backendSteps.add('conversion_authorization');
      backendSteps.add('handoff');
      backendSteps.add('generation');
    }
    if (/\/review$/.test(url)) backendSteps.add('customer_review');
  });

  await page.goto(pageUrl, {waitUntil: 'networkidle'});
  await page.locator('#product-archive-input').setInputFiles(packagePath);
  await page.getByText(/处理完成：Excel 1 份/).waitFor({timeout: 30000});
  await page.locator('#review-panel.visible').waitFor();
  const firstException = page.locator('#style-bridge-items .bridge-item').filter({has: page.getByRole('button', {name: '保存更正', exact: true})}).first();
  await firstException.locator('select').selectOption('real_photo');
  await Promise.all([
    page.waitForResponse(response => /photo-category-corrections$/.test(response.url()) && response.status() === 200),
    firstException.getByRole('button', {name: '保存更正', exact: true}).click(),
  ]);
  const sceneException = page.locator('#style-bridge-items .bridge-item').filter({hasText: 'SYNTHETIC-001-2.jpg'});
  await sceneException.locator('select').selectOption('scene_photo');
  await Promise.all([
    page.waitForResponse(response => /photo-category-corrections$/.test(response.url()) && response.status() === 200),
    sceneException.getByRole('button', {name: '保存更正', exact: true}).click(),
  ]);
  await page.locator('#style-bridge-main-photo option').nth(1).waitFor({state: 'attached'});
  await page.locator('#edit-result').click();
  await page.locator('[data-field="product_type"]').selectOption('first_sale_bead');
  await page.locator('[data-field="inventory_status"]').selectOption('in_stock');
  await page.locator('[data-field="main_image_selected"]').check();
  await page.locator('[data-field="material_label"]').fill('脱敏本地测试材质');
  await page.locator('[data-field="product_form"]').fill('手串');
  await page.locator('[data-field="visual_observations"]').fill('图片中可见珠串轮廓');
  await page.locator('[data-field="selling_points"]').fill('仅作脱敏本地测试');
  await Promise.all([
    page.waitForResponse(response => /training-results\/.+\/versions$/.test(response.url()) && response.status() === 201),
    page.locator('#save-standard').click(),
  ]);
  await page.waitForLoadState('networkidle');
  await page.locator('#style-bridge-product option[value="test-product-scene"]').waitFor({state: 'attached'});
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await page.locator('#style-bridge-product').selectOption('test-product-scene');
    await page.locator('#style-bridge-main-photo option').nth(1).waitFor({state: 'attached'});
    await page.locator('#style-bridge-main-photo').selectOption({index: 1});
    await page.locator('#style-bridge-permission').check();
    await page.waitForTimeout(250);
    if (await page.locator('#style-bridge-generate').isEnabled()) break;
  }
  await page.locator('#style-bridge-generate').waitFor({state: 'visible'});
  await page.locator('#style-bridge-generate').click();
  await page.locator('.candidate-card').first().waitFor({timeout: 30000});
  const candidates = await page.locator('.candidate-card').count();
  await page.locator('.candidate-card').first().getByRole('button', {name: '查看实物图', exact: true}).click();
  backendSteps.add('original_style_toggle');
  await page.locator('.candidate-card').first().getByRole('button', {name: '采用这张', exact: true}).click();
  await page.getByText('已采用', {exact: false}).first().waitFor();
  await page.screenshot({path: `${evidenceDir}/screenshots/customer-approved.png`, fullPage: true});
  await page.reload({waitUntil: 'networkidle'});
  await page.getByText('已采用', {exact: false}).first().waitFor();
  backendSteps.add('refresh_restore');
  await page.screenshot({path: `${evidenceDir}/screenshots/refresh-restored.png`, fullPage: true});

  const batchId = await page.locator('#batch-code').textContent();
  const result = {
    result: 'PASS', acceptance_batch_id: acceptanceBatchId, application_batch_id: batchId?.trim(),
    page_url: pageUrl, candidate_count: candidates,
    backend_calls_observed: [...backendSteps].sort(),
    non_loopback_requests: nonLoopback, provider_requests: providerRequests,
  };
  await writeFile(`${evidenceDir}/network.json`, JSON.stringify({requests, responses, non_loopback_requests: nonLoopback, provider_requests: providerRequests}, null, 2));
  await writeFile(`${evidenceDir}/browser.json`, JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} catch (error) {
  if (page) await page.screenshot({path: `${evidenceDir}/screenshots/failure.png`, fullPage: true}).catch(() => {});
  await writeFile(`${evidenceDir}/network.json`, JSON.stringify({requests, responses, non_loopback_requests: nonLoopback, provider_requests: providerRequests}, null, 2));
  await writeFile(`${evidenceDir}/browser-failure.json`, JSON.stringify({acceptance_batch_id: acceptanceBatchId, error: String(error), bridge_status: page ? await page.locator('#style-bridge-status').textContent().catch(() => null) : null}, null, 2));
  throw error;
} finally {
  await browser.close();
}
