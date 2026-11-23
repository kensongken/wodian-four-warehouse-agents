import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const testApi = process.env.VITEST ? await import("vitest") : await import("node:test");
const test = testApi.test ?? testApi.default;

import * as canonical from "../studio-runtime-canonical.mjs";
import {
  JS_UTF16_PATH_ORDER,
  UTF8_PATH_ORDER,
  UTF8_PATH_RECORD_TREE_SHA256,
} from "./worker-runtime-path-order-fixture.mjs";

const ordinaryPathOrder = (left, right) => left < right ? -1 : left > right ? 1 : 0;
const packageInventoryScalarVectors = JSON.parse(await readFile(
  new URL("./worker-runtime-package-inventory-scalar-vectors.json", import.meta.url),
  "utf8",
));

function setNestedValue(value, target, replacement) {
  const parts = target.split(".");
  const last = parts.pop();
  let parent = value;
  for (const part of parts) parent = parent[Number.isNaN(Number(part)) ? part : Number(part)];
  parent[last] = replacement;
}

test("unsigned UTF-8 comparison remains a fixed standalone total-order primitive", () => {
  assert.deepEqual(
    [...UTF8_PATH_ORDER].sort(canonical.compareRuntimePathsByUtf8Bytes),
    UTF8_PATH_ORDER,
  );
  assert.deepEqual([...UTF8_PATH_ORDER].sort(ordinaryPathOrder), JS_UTF16_PATH_ORDER);
  assert.equal(
    UTF8_PATH_RECORD_TREE_SHA256,
    "ce65004fb2ff951ea50a7ca4922e6e65f6aaa677ecb0a2a58517a55023459428",
  );
});

test("UTF-8 comparison rejects malformed strings and non-string inputs", () => {
  for (const malformed of ["\ud800", "\ud801", "\udc00"]) {
    assert.throws(
      () => canonical.compareRuntimePathsByUtf8Bytes(malformed, "valid"),
      /studio-runtime-path-comparator-input-invalid/,
    );
    assert.throws(
      () => canonical.compareRuntimePathsByUtf8Bytes("valid", malformed),
      /studio-runtime-path-comparator-input-invalid/,
    );
  }
  for (const value of [undefined, null, 1, Buffer.from("valid")]) {
    assert.throws(
      () => canonical.compareRuntimePathsByUtf8Bytes(value, "valid"),
      /studio-runtime-path-comparator-input-invalid/,
    );
    assert.throws(
      () => canonical.compareRuntimePathsByUtf8Bytes("valid", value),
      /studio-runtime-path-comparator-input-invalid/,
    );
  }
});

test("runtime paths accept printable portable ASCII and reject Unicode or malformed strings", () => {
  assert.equal(typeof canonical.isPortableRuntimeRelativePath, "function");
  for (const path of [
    "bin/run-offline-worker",
    "site-packages/scipy/io/tests/data/Transparent Busy.ani",
    "site-packages/pkg-1.0.dist-info/METADATA",
  ]) assert.equal(canonical.isPortableRuntimeRelativePath(path), true, path);

  for (const path of [
    "runtime/e\u0301-combining.txt",
    "runtime/\u00f1-bmp.txt",
    "runtime/\ue000-private.txt",
    "runtime/\u{10000}-supplementary.txt",
    "runtime/\u00df.txt",
    "runtime/\ud800.txt",
    "runtime/\udc00.txt",
    "runtime/bad\\path",
    "runtime/../escape",
    "runtime/control\n.txt",
  ]) assert.equal(canonical.isPortableRuntimeRelativePath(path), false, JSON.stringify(path));
});

test("runtime path batches reject every member before returning canonical order", () => {
  assert.deepEqual(
    canonical.sortPortableRuntimePaths(["Python.h", "abstract.h", "A/record", "a/license"]),
    ["A/record", "Python.h", "a/license", "abstract.h"],
  );
  for (const invalid of ["pkg/é.py", "pkg/e\u0301.py", "pkg/\u{10000}.py", "pkg/\ud800.py"]) {
    assert.throws(
      () => canonical.sortPortableRuntimePaths(["valid/path", invalid]),
      /studio-runtime-path-invalid/,
    );
  }
});

test("collision keys fold ASCII A-Z only after path validation", () => {
  assert.equal(typeof canonical.runtimePathCollisionKey, "function");
  assert.equal(canonical.runtimePathCollisionKey("Pkg/Case.PY"), "pkg/case.py");
  assert.equal(canonical.runtimePathCollisionKey("runtime/SS.txt"), "runtime/ss.txt");
  assert.equal(canonical.isPortableRuntimeRelativePath("runtime/ß.txt"), false);
  assert.equal(canonical.isPortableRuntimeRelativePath("runtime/ss.txt"), true);
});

test("runtime JSON bytes require fatal UTF-8 decoding and reject decoded lone surrogates as paths", () => {
  assert.equal(typeof canonical.parseRuntimeJsonBytes, "function");
  assert.throws(() => canonical.parseRuntimeJsonBytes(Buffer.from([0x7b, 0x22, 0x78, 0x22, 0x3a, 0xff, 0x7d])));
  assert.deepEqual(canonical.parseRuntimeJsonBytes(Buffer.from('{"x":"ascii"}', "ascii")), { x: "ascii" });
  const parsed = canonical.parseRuntimeJsonBytes(Buffer.from('{"path":"\\ud800"}', "ascii"));
  assert.equal(canonical.isPortableRuntimeRelativePath(parsed.path), false);
});

const validPackageInventory = () => ({
  schemaVersion: 1,
  python: {
    name: "cpython",
    version: "3.12.14",
    build: "20260901",
    sourceMaterialId: `python-runtime:cpython:3.12.14:${"1".repeat(64)}`,
    sourceSha256: "1".repeat(64),
    licenseExpression: "PSF-2.0",
    licenseEvidence: ["python/LICENSE"],
    moduleRoots: ["python"],
  },
  packages: [{
    name: "fixture-package",
    version: "1.0.0",
    sourceMaterialId: `python-distribution:fixture-package:1.0.0:${"2".repeat(64)}`,
    sourceSha256: "2".repeat(64),
    licenseExpression: "MIT",
    licenseEvidence: ["fixture_package-1.0.0.dist-info/METADATA"],
    moduleRoots: ["fixture_package"],
  }],
  recordOwnership: { unownedFiles: [] },
  licenseReview: "pending",
});

test("package inventory paths share one portable-ASCII contract", () => {
  assert.equal(canonical.isValidRuntimePackageInventory(validPackageInventory(), {
    pythonVersion: "3.12.14",
  }), true);

  const mutations = [
    (value) => { value.schemaVersion = true; },
    (value) => { value.python.sourceSha256 = null; },
    (value) => { value.python.licenseEvidence = "python/LICENSE"; },
    (value) => { value.packages[0].moduleRoots = "fixture_package"; },
    (value) => { value.python.licenseEvidence = ["python/é-LICENSE"]; },
    (value) => { value.packages[0].licenseEvidence = ["pkg/é-LICENSE"]; },
    (value) => { value.python.moduleRoots = ["é-python"]; },
    (value) => { value.packages[0].moduleRoots = ["é-package"]; },
    (value) => { value.packages[0].moduleRoots = ["nested/root"]; },
    (value) => { value.packages[0].moduleRoots = [""]; },
    (value) => { value.packages[0].moduleRoots = ["."]; },
    (value) => { value.packages[0].moduleRoots = [".."]; },
    (value) => { value.packages[0].licenseEvidence = ["LICENSE", "license"]; },
    (value) => { value.packages[0].moduleRoots = ["Fixture", "fixture"]; },
    (value) => { value.packages.push({ ...value.packages[0], name: "fixture_package" }); },
  ];
  for (const mutate of mutations) {
    const value = validPackageInventory();
    mutate(value);
    assert.equal(canonical.isValidRuntimePackageInventory(value, {
      pythonVersion: "3.12.14",
    }), false);
  }
});

test("package inventory scalar metadata matches the shared cross-language vectors", () => {
  for (const vector of packageInventoryScalarVectors.schemaVersions) {
    const value = validPackageInventory();
    value.schemaVersion = JSON.parse(vector.valueJson);
    assert.equal(
      canonical.isValidRuntimePackageInventory(value, { pythonVersion: "3.12.14" }),
      vector.valid,
      vector.label,
    );
  }
  for (const vector of packageInventoryScalarVectors.metadataScalars) {
    const value = validPackageInventory();
    const replacement = JSON.parse(vector.valueJson);
    setNestedValue(value, vector.target, replacement);
    assert.equal(
      canonical.isValidRuntimePackageInventory(value, {
        pythonVersion: vector.target === "python.version" ? replacement : "3.12.14",
      }),
      vector.valid,
      vector.label,
    );
  }
});
