import assert from "node:assert/strict";

import {
  partitionImageFiles,
  partitionProductFolderFiles,
} from "../../src/product_engine/api/image_file_selection.mjs";

const files = [
  { name: "副本原矿南红（测试）.xlsx", type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
  { name: "南红主图.JPG", type: "image/jpeg" },
  { name: "南红细节.heic", type: "" },
  { name: "说明.pdf", type: "application/pdf" },
];

assert.deepEqual(partitionImageFiles(files), {
  accepted: [files[1], files[2]],
  rejected: [files[0], files[3]],
});

assert.deepEqual(partitionImageFiles([{ name: "伪装图片.jpg", type: "application/pdf" }]), {
  accepted: [],
  rejected: [{ name: "伪装图片.jpg", type: "application/pdf" }],
});

assert.deepEqual(partitionProductFolderFiles(files), {
  workbook: files[0],
  images: [files[1], files[2]],
  rejected: [files[3]],
  error: null,
});

assert.deepEqual(partitionProductFolderFiles([files[0], { ...files[0], name: "另一份.xlsx" }]), {
  workbook: null,
  images: [],
  rejected: [],
  error: "资料文件夹只能包含一份 Excel 表格。",
});

console.log("image file selection tests passed");
