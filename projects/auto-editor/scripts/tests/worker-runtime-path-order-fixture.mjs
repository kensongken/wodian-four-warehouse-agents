export const UTF8_PATH_ORDER = Object.freeze([
  "runtime/A-upper.txt",
  "runtime/a-lower.txt",
  "runtime/e\u0301-combining.txt",
  "runtime/ñ-non-ascii.txt",
  "runtime/\uE000-private.txt",
  "runtime/\u{10000}-supplementary.txt",
]);

export const JS_UTF16_PATH_ORDER = Object.freeze([
  "runtime/A-upper.txt",
  "runtime/a-lower.txt",
  "runtime/e\u0301-combining.txt",
  "runtime/ñ-non-ascii.txt",
  "runtime/\u{10000}-supplementary.txt",
  "runtime/\uE000-private.txt",
]);

export const EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
export const UTF8_PATH_RECORD_TREE_SHA256 =
  "ce65004fb2ff951ea50a7ca4922e6e65f6aaa677ecb0a2a58517a55023459428";

export const UTF8_PATH_RECORDS = Object.freeze(UTF8_PATH_ORDER.map((relativePath) => Object.freeze({
  relativePath,
  byteLength: 0,
  sha256: EMPTY_SHA256,
  type: "file",
  executable: false,
})));
