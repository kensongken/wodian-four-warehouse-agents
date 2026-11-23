export async function openAlbumProductReview(fetchImpl, product) {
  const code = encodeURIComponent(product.product_code);
  const version = Number(product.package_version);
  const response = await fetchImpl(
    `/local/api/album-review-inbox/${code}/versions/${version}/open-review`,
    { method: 'POST' },
  );
  const payload = await response.json();
  if (!response.ok) throw payload;
  return payload;
}

export function reviewIndexForProduct(items, productCode) {
  return items.findIndex(item => (item['商品编码'] || item.product_code) === productCode);
}
