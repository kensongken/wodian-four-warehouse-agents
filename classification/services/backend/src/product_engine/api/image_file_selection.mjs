const IMAGE_MIME_TYPES = new Set([
  'image/jpeg',
  'image/png',
  'image/heic',
  'image/heif',
]);

const IMAGE_SUFFIXES = new Set(['.jpg', '.jpeg', '.png', '.heic', '.heif']);

export function partitionImageFiles(files) {
  const accepted = [];
  const rejected = [];
  for (const file of Array.from(files)) {
    const declaredType = (file.type || '').toLowerCase();
    const name = (file.name || '').toLowerCase();
    const suffix = name.includes('.') ? name.slice(name.lastIndexOf('.')) : '';
    const isImage = declaredType
      ? IMAGE_MIME_TYPES.has(declaredType)
      : IMAGE_SUFFIXES.has(suffix);
    (isImage ? accepted : rejected).push(file);
  }
  return { accepted, rejected };
}

export function partitionProductFolderFiles(files) {
  const allFiles = Array.from(files);
  const workbooks = allFiles.filter(file => (file.name || '').toLowerCase().endsWith('.xlsx'));
  if (workbooks.length !== 1) {
    return {
      workbook: null,
      images: [],
      rejected: [],
      error: workbooks.length
        ? '资料文件夹只能包含一份 Excel 表格。'
        : '资料文件夹里需要放一份 Excel 表格。',
    };
  }
  const remaining = allFiles.filter(file => file !== workbooks[0]);
  const { accepted: images, rejected } = partitionImageFiles(remaining);
  if (!images.length) {
    return { workbook: null, images: [], rejected, error: '资料文件夹里没有可上传的商品图片。' };
  }
  return { workbook: workbooks[0], images, rejected, error: null };
}
