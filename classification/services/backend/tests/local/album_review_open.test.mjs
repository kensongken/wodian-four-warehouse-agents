import assert from 'node:assert/strict';
import test from 'node:test';

import {
  openAlbumProductReview,
  reviewIndexForProduct,
} from '../../src/product_engine/api/album_review_open.mjs';

test('opens the exact album product version without re-uploading in the browser', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      async json() {
        return { ok: true, replayed: false, batch: { batch_id: 'BATCH-ALBUM-1' } };
      },
    };
  };

  const payload = await openAlbumProductReview(fetchImpl, {
    product_code: 'SYNTHETIC / 001',
    package_version: 3,
  });

  assert.deepEqual(calls, [{
    url: '/local/api/album-review-inbox/SYNTHETIC%20%2F%20001/versions/3/open-review',
    options: { method: 'POST' },
  }]);
  assert.equal(payload.batch.batch_id, 'BATCH-ALBUM-1');
});

test('surfaces the server error instead of pretending review opened', async () => {
  const failure = { error_code: 'album_product_version_not_found', message_zh: '版本已更新' };
  const fetchImpl = async () => ({ ok: false, json: async () => failure });

  await assert.rejects(
    openAlbumProductReview(fetchImpl, { product_code: 'A', package_version: 1 }),
    error => error === failure,
  );
});

test('selects the requested product inside a shared employee batch', () => {
  const items = [
    { item_id: '914-main', product_code: 'BCNHDIY914' },
    { item_id: '914-gallery', product_code: 'BCNHDIY914' },
    { item_id: '1047-main', product_code: 'BCNHDIY1047' },
    { item_id: '1047-gallery', product_code: 'BCNHDIY1047' },
  ];

  assert.equal(reviewIndexForProduct(items, 'BCNHDIY1047'), 2);
  assert.equal(reviewIndexForProduct(items, 'MISSING'), -1);
});
