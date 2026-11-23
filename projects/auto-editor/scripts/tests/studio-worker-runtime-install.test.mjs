import assert from "node:assert/strict";
import { execFile as execFileCallback } from "node:child_process";
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { access, chmod, link, lstat, mkdir, mkdtemp, open, readFile, readdir, rename, rm, symlink, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, relative } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import {
  assertDeterministicRuntimeTar,
  createDeterministicRuntimeTar,
} from "../studio-runtime-tar.mjs";
import { compareRuntimePathsByUtf8Bytes } from "../studio-runtime-canonical.mjs";
import {
  JS_UTF16_PATH_ORDER,
  UTF8_PATH_ORDER,
} from "./worker-runtime-path-order-fixture.mjs";

const APPROVED_DEPENDENCY_LOCK_SHA256 =
  "774b30f7f04e3d78faf72f75fca0d0b5823f8244d751c6c10f9c97f45fd85521";
const APPROVED_WORKER_DEPENDENCY_LOCK_SHA256 =
  "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041";
const BLOCK_BYTES = 512;
const ZERO_BLOCK = Buffer.alloc(BLOCK_BYTES);
const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const execFile = promisify(execFileCallback);
const checkerPath = fileURLToPath(new URL("../check-studio-models.mjs", import.meta.url));
const installerPath = fileURLToPath(new URL("../install-studio-models.mjs", import.meta.url));

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

function treeSha256(files) {
  const digest = createHash("sha256");
  for (const file of [...files].sort((left, right) => (
    compareRuntimePathsByUtf8Bytes(left.relativePath, right.relativePath)
  ))) {
    digest.update(file.relativePath, "utf8");
    digest.update("\0");
    digest.update(file.sha256, "ascii");
    digest.update("\0");
    digest.update(String(file.byteLength), "ascii");
    digest.update("\0file\0");
    digest.update(file.executable ? "1\n" : "0\n", "ascii");
  }
  return digest.digest("hex");
}

function writeString(header, value, offset, length) {
  const bytes = Buffer.from(value, "utf8");
  if (bytes.length > length) throw new Error("test-tar-path-too-long");
  bytes.copy(header, offset);
}

function writeOctal(header, value, offset, length) {
  writeString(header, `${value.toString(8).padStart(length - 1, "0")}\0`, offset, length);
}

function tarHeader({ path, type = "0", mode = 0o644, size = 0, linkPath = "" }) {
  const header = Buffer.alloc(BLOCK_BYTES);
  writeString(header, path, 0, 100);
  writeOctal(header, mode, 100, 8);
  writeOctal(header, 0, 108, 8);
  writeOctal(header, 0, 116, 8);
  writeOctal(header, size, 124, 12);
  writeOctal(header, 0, 136, 12);
  header.fill(0x20, 148, 156);
  header[156] = type.charCodeAt(0);
  writeString(header, linkPath, 157, 100);
  writeString(header, "ustar\0", 257, 6);
  writeString(header, "00", 263, 2);
  const checksum = header.reduce((total, byte) => total + byte, 0);
  writeString(header, `${checksum.toString(8).padStart(6, "0")}\0 `, 148, 8);
  return header;
}

function maliciousTar(entries) {
  const parts = [];
  for (const entry of entries) {
    const contents = Buffer.from(entry.contents ?? "");
    const size = entry.declaredSize ?? contents.length;
    parts.push(tarHeader({ ...entry, size }));
    if (entry.type === undefined || entry.type === "0") {
      parts.push(contents);
      parts.push(Buffer.alloc((BLOCK_BYTES - (contents.length % BLOCK_BYTES)) % BLOCK_BYTES));
    }
  }
  parts.push(ZERO_BLOCK, ZERO_BLOCK);
  return Buffer.concat(parts);
}

function mutateFirstTarHeader(archive, mutate, { refreshChecksum = true } = {}) {
  const changed = Buffer.from(archive);
  const header = changed.subarray(0, BLOCK_BYTES);
  mutate(header);
  if (refreshChecksum) {
    header.fill(0x20, 148, 156);
    const checksum = header.reduce((total, byte) => total + byte, 0);
    header.fill(0, 148, 156);
    writeString(header, `${checksum.toString(8).padStart(6, "0")}\0 `, 148, 8);
  }
  return changed;
}

function manifestFile(relativePath, contents = "", executable = false, byteLength) {
  const bytes = Buffer.from(contents);
  return {
    relativePath,
    byteLength: byteLength ?? bytes.length,
    sha256: sha256(bytes),
    type: "file",
    executable,
  };
}

const defaultRuntimeFiles = Object.freeze([
  Object.freeze({
    relativePath: "bin/run-offline-worker",
    contents: "#!/bin/sh\nexit 0\n",
    executable: true,
  }),
  Object.freeze({
    relativePath: "policy/deny-network.sb",
    contents: "(version 1)\n(allow default)\n(deny network*)\n",
    executable: false,
  }),
  Object.freeze({
    relativePath: "python/bin/python3.12",
    contents: "#!/bin/sh\nprintf '%s\\n' '{\"architecture\":\"arm64\",\"pythonVersion\":\"3.12.13\",\"status\":\"ready\"}'\n",
    executable: true,
  }),
  Object.freeze({
    relativePath: "site-packages/synthetic.txt",
    contents: "synthetic runtime bytes\n",
    executable: false,
  }),
]);

function lockedFile(relativePath, bytes) {
  return { relativePath, sha256: sha256(bytes), byteLength: bytes.length };
}

async function writeRuntimeSource(root, files) {
  for (const file of files) {
    const path = join(root, file.relativePath);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, file.contents, { mode: file.executable ? 0o755 : 0o644 });
    await chmod(path, file.executable ? 0o755 : 0o644);
  }
}

function packageInventoryFixture() {
  return {
    schemaVersion: 1,
    python: {
      name: "cpython", version: "3.12.13", build: null,
      sourceMaterialId: `python-runtime:cpython:3.12.13:${"1".repeat(64)}`,
      sourceSha256: "1".repeat(64), licenseExpression: "PSF-2.0",
      licenseEvidence: ["python/LICENSE"], moduleRoots: ["python"],
    },
    packages: [{
      name: "fixture", version: "1.0.0",
      sourceMaterialId: `python-distribution:fixture:1.0.0:${"2".repeat(64)}`,
      sourceSha256: "2".repeat(64), licenseExpression: "MIT",
      licenseEvidence: ["fixture-1.0.0.dist-info/METADATA"], moduleRoots: ["fixture_pkg"],
    }],
    recordOwnership: { unownedFiles: [] },
    licenseReview: "pending",
  };
}

async function fixture(t, options = {}) {
  const temporaryRoot = await mkdtemp(join(tmpdir(), "studio-worker-runtime-install-"));
  t.after(() => rm(temporaryRoot, { recursive: true, force: true }));
  const modelRoot = join(temporaryRoot, "models");
  const candidateRoot = join(temporaryRoot, "candidate");
  const sourceRoot = join(temporaryRoot, "source-runtime");
  await mkdir(candidateRoot, { mode: 0o700 });
  const runtimeFiles = options.runtimeFiles ?? defaultRuntimeFiles;
  const manifestFiles = options.manifestFiles ?? runtimeFiles.map((file) => (
    manifestFile(file.relativePath, file.contents, file.executable)
  ));
  const computedTreeSha256 = options.treeSha256 ?? treeSha256(manifestFiles);
  const manifest = options.manifest ?? {
    schemaVersion: 1,
    files: manifestFiles,
    treeSha256: computedTreeSha256,
  };
  const manifestBytes = options.manifestBytes ?? Buffer.from(`${JSON.stringify(manifest)}\n`);
  const packageInventory = options.packageInventory ?? packageInventoryFixture();
  const packagesBytes = options.packageInventoryBytes ?? Buffer.from(`${JSON.stringify(packageInventory)}\n`);
  let archiveBytes = options.archiveBytes;
  if (archiveBytes === undefined) {
    await writeRuntimeSource(sourceRoot, runtimeFiles);
    const archivePath = join(candidateRoot, "runtime.tar");
    await createDeterministicRuntimeTar(sourceRoot, archivePath);
    archiveBytes = await readFile(archivePath);
  }
  const derivationReceipt = {
    schemaVersion: 1,
    status: "succeeded",
    materialPacketId: "worker-runtime-materials-v1-sha256-" + "1".repeat(64),
    supplementalMaterialPacketId: "worker-runtime-build-materials-v1-sha256-" + "2".repeat(64),
    builderToolPacketId: "worker-runtime-builder-tool-v1-sha256-" + "3".repeat(64),
    builderToolVerifiedReceiptSha256: "4".repeat(64),
    builderToolExecutionSecurityBoundary: {
      inodePin: "opened-o_nofollow-handle-private-hardlink-dev-ino-sha256-pre-post",
      sameLoginAccountCompromise: "out-of-scope-local-mvp",
    },
    builderToolExecutable: {
      byteLength: 40_152_144,
      sha256: "5".repeat(64),
    },
    dependencyLockSha256: APPROVED_DEPENDENCY_LOCK_SHA256,
    workerDependencyLockSha256: APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
    target: {
      pythonVersionRange: ">=3.12.0 <3.13",
      platform: "darwin",
      architecture: "arm64",
    },
    promotionVerification: {
      relocationImportSelfCheck: "passed-before-promotion",
    },
    archiveSha256: sha256(archiveBytes),
    treeSha256: computedTreeSha256,
    ...options.derivationReceipt,
  };
  const receiptBytes = Buffer.from(`${JSON.stringify(derivationReceipt)}\n`);
  const sidecars = {
    archive: lockedFile("runtime.tar", archiveBytes),
    manifest: lockedFile("runtime-manifest.json", manifestBytes),
    packageInventory: lockedFile("runtime-packages.json", packagesBytes),
    derivationReceipt: lockedFile("derivation-receipt.json", receiptBytes),
  };
  const packetCore = {
    schemaVersion: 1,
    status: "awaiting-exact-bundle-and-license-approval",
    approved: false,
    enabled: false,
    licenseReview: "pending",
    materialPacketId: derivationReceipt.materialPacketId,
    supplementalMaterialPacketId: derivationReceipt.supplementalMaterialPacketId,
    builderToolPacketId: derivationReceipt.builderToolPacketId,
    builderToolExecutableSha256: derivationReceipt.builderToolExecutable.sha256,
    builderToolExecutableBytes: derivationReceipt.builderToolExecutable.byteLength,
    builderToolVerifiedReceiptSha256: derivationReceipt.builderToolVerifiedReceiptSha256,
    builderToolExecutionSecurityBoundary: derivationReceipt.builderToolExecutionSecurityBoundary,
    dependencyLockSha256: APPROVED_DEPENDENCY_LOCK_SHA256,
    workerDependencyLockSha256: APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
    pythonVersion: "3.12.13",
    platform: "darwin",
    architecture: "arm64",
    minimumOsVersion: "14.0",
    treeSha256: computedTreeSha256,
    totalInstalledBytes: manifestFiles.reduce((total, file) => total + file.byteLength, 0),
    ...sidecars,
    ...(options.extendedBundleCore ? {
      licenseInventorySummary: {
        sha256: sidecars.packageInventory.sha256,
        installedDistributionCount: packageInventory.packages.length,
        unknownLicenseNames: packageInventory.packages
          .filter((item) => item.licenseExpression === "UNKNOWN")
          .map((item) => item.name),
        scenePackages: packageInventory.packages
          .filter((item) => ["platformdirs", "scenedetect"].includes(item.name))
          .map((item) => ({ name: item.name, version: item.version, licenseExpression: item.licenseExpression })),
      },
      noIllusion: {
        retrieved: true,
        built: true,
        deterministic: true,
        installed: false,
        qwenProbeRun: false,
        customerContentUsed: false,
        productionReady: false,
      },
    } : {}),
  };
  const approvalPacketId = `worker-runtime-bundle-v1-sha256-${sha256(canonicalJson(packetCore))}`;
  const bundle = {
    schemaVersion: 1,
    id: options.id ?? "synthetic-worker-runtime",
    version: "0.6.6-py312",
    pythonVersion: "3.12.13",
    platform: "darwin",
    architecture: "arm64",
    minimumOsVersion: "14.0",
    dependencyLockSha256: APPROVED_DEPENDENCY_LOCK_SHA256,
    workerDependencyLockSha256: APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
    materialInventorySha256: "1".repeat(64),
    approvalPacketId,
    ...sidecars,
    treeSha256: computedTreeSha256,
    licenseReview: "approved",
    enabled: true,
    approved: true,
    approvedBy: "synthetic-runtime-owner",
    approvedAt: "2026-09-27T01:00:00.000Z",
    probe: null,
    ...options.bundle,
  };
  const lockPath = join(temporaryRoot, "models.lock.json");
  await Promise.all([
    writeFile(join(candidateRoot, "runtime.tar"), archiveBytes),
    writeFile(join(candidateRoot, "runtime-manifest.json"), manifestBytes),
    writeFile(join(candidateRoot, "runtime-packages.json"), packagesBytes),
    writeFile(join(candidateRoot, "derivation-receipt.json"), receiptBytes),
    writeFile(lockPath, JSON.stringify({ schemaVersion: 1, artifacts: [], workerRuntimes: [bundle] })),
  ]);
  return {
    approvalPacketId,
    bundle,
    bundleId: bundle.id,
    candidateRoot,
    finalRoot: join(modelRoot, "runtime", bundle.id),
    lockPath,
    modelRoot,
    temporaryRoot,
  };
}

async function installer() {
  return import("../studio-worker-runtime-installer.mjs");
}

test("installer rejects a derivation receipt bound to a different Worker lock", async (t) => {
  const input = await fixture(t, {
    derivationReceipt: { workerDependencyLockSha256: "e".repeat(64) },
  });
  const { installWorkerRuntime } = await installer();

  await assert.rejects(
    installWorkerRuntime(input),
    /studio-runtime-tree-mismatch/,
  );
});

test("installer accepts the approved extended bundle identity without weakening candidate verification", async (t) => {
  const input = await fixture(t, { extendedBundleCore: true });
  const { installWorkerRuntime } = await installer();

  assert.deepEqual(await installWorkerRuntime(input), {
    artifact: input.bundle.id,
    status: "ready",
    reused: false,
  });
});

function qwenArtifactFixture() {
  const revision = "b".repeat(40);
  const sourceUrl = "https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct";
  return {
    id: "qwen3-vl-2b-instruct-4bit",
    version: "2b-4bit",
    artifactKind: "checkpoint",
    sources: [{
      id: "qwen-official-weights",
      kind: "weights",
      sourceUrl,
      sourceRevision: revision,
      inventoryVerified: true,
      files: [{
        relativePath: "inputs/model.safetensors",
        sourceUrl: `${sourceUrl}/resolve/${revision}/model.safetensors`,
        required: true,
        executable: false,
        sha256: "6".repeat(64),
        byteLength: 1,
      }],
    }],
    runtimeFiles: [{
      relativePath: "qwen3-vl-2b-instruct-4bit/model.safetensors",
      candidateRelativePath: "qwen3-vl-2b-instruct-4bit/model.safetensors",
      required: true,
      executable: false,
      sourceKind: "weights",
      sha256: "7".repeat(64),
      byteLength: 1,
    }],
    derivationReceipt: {
      schemaVersion: 1,
      recipeId: "synthetic-qwen-recipe",
      inputSourceIds: ["qwen-official-weights"],
      outputTreeSha256: "5".repeat(64),
      probeReportSha256: "4".repeat(64),
      status: "succeeded",
      syntheticInput: true,
      realModel: true,
    },
    licenseEvidence: {
      code: {
        applies: false,
        sourceId: null,
        sourceRevision: null,
        spdxExpression: null,
        evidenceUrl: null,
      },
      weights: {
        applies: true,
        sourceId: "qwen-official-weights",
        sourceRevision: revision,
        spdxExpression: "Apache-2.0",
        evidenceUrl: `${sourceUrl}/raw/${revision}/LICENSE`,
      },
    },
    licenseReview: "approved",
    runtimeBackend: "mlx-vlm-metal",
    enabled: true,
    approved: true,
    approvedBy: "local-model-owner",
    approvedAt: "2026-09-23T01:00:00.000Z",
  };
}

async function absent(path) {
  await assert.rejects(access(path));
}

async function prepareInstallParents(input) {
  await mkdir(join(input.modelRoot, "runtime"), { recursive: true, mode: 0o700 });
  await chmod(input.modelRoot, 0o700);
  await chmod(join(input.modelRoot, "runtime"), 0o700);
  const lockParent = join(input.modelRoot, ".runtime-staging", ".locks");
  await mkdir(lockParent, { recursive: true, mode: 0o700 });
  await chmod(join(input.modelRoot, ".runtime-staging"), 0o700);
  await chmod(lockParent, 0o700);
  return lockParent;
}

function canonicalInstallLock(input, {
  createdAt = "2026-09-27T01:00:00.000Z",
  heartbeatAt = createdAt,
  ownerToken = "00000000-0000-4000-8000-000000000001",
  pid = 2_147_483_647,
  processInstanceSha256 = "a".repeat(64),
} = {}) {
  return `${canonicalJson({
    bundleId: input.bundle.id,
    createdAt,
    heartbeatAt,
    ownerToken,
    pid,
    processInstanceSha256,
  })}\n`;
}

function canonicalLegacyInstallLock(input, {
  createdAt = "2026-09-27T01:00:00.000Z",
  heartbeatAt = createdAt,
  ownerToken = "00000000-0000-4000-8000-000000000001",
  pid = process.pid,
} = {}) {
  return `${canonicalJson({
    bundleId: input.bundle.id,
    createdAt,
    heartbeatAt,
    ownerToken,
    pid,
  })}\n`;
}

function canonicalInstanceBoundInstallLock(input, {
  createdAt = "2026-09-27T01:00:00.000Z",
  heartbeatAt = createdAt,
  ownerToken = "00000000-0000-4000-8000-000000000001",
  pid = process.pid,
  processInstanceSha256 = "a".repeat(64),
} = {}) {
  return canonicalInstallLock(input, {
    createdAt,
    heartbeatAt,
    ownerToken,
    pid,
    processInstanceSha256,
  });
}

async function writeInstallLock(input, contents, { old = false } = {}) {
  const lockParent = await prepareInstallParents(input);
  const path = join(lockParent, `${input.bundle.id}.lock`);
  await writeFile(path, contents, { mode: 0o600 });
  await chmod(path, 0o600);
  if (old) {
    const timestamp = new Date(Date.now() - 60_000);
    await utimes(path, timestamp, timestamp);
  }
  return path;
}

async function observeTargetBodyReads(targetPath, callback) {
  const target = await lstat(targetPath, { bigint: true });
  const probeHandle = await open(targetPath, constants.O_RDONLY);
  const fileHandlePrototype = Object.getPrototypeOf(probeHandle);
  await probeHandle.close();
  const originalRead = fileHandlePrototype.read;
  let bodyReads = 0;
  fileHandlePrototype.read = async function trackedRead(...arguments_) {
    const opened = await this.stat({ bigint: true });
    if (opened.dev === target.dev && opened.ino === target.ino) bodyReads += 1;
    return originalRead.apply(this, arguments_);
  };
  try {
    const result = await callback();
    return { bodyReads, result };
  } finally {
    fileHandlePrototype.read = originalRead;
  }
}

test("requires an exact approved packet identity before model-root mutation", async (t) => {
  const input = await fixture(t, { bundle: { approvalPacketId: `worker-runtime-bundle-v1-sha256-${"f".repeat(64)}` } });
  const { installWorkerRuntime } = await installer();
  await assert.rejects(installWorkerRuntime(input), /studio-runtime-artifact-unapproved/);
  await absent(input.modelRoot);
});

test("rejects absent, forged, or over-authorized approval records before model-root mutation", async (t) => {
  const { installWorkerRuntime } = await installer();
  for (const [name, bundle] of [
    ["absent", { approved: false, enabled: false, approvedBy: null, approvedAt: null }],
    ["forged", { approvedBy: null }],
    ["over-authorized", { installAuthorized: true }],
  ]) {
    await t.test(name, async (subtest) => {
      const input = await fixture(subtest, { bundle });
      await assert.rejects(installWorkerRuntime(input), /studio-runtime-artifact-unapproved/);
      await absent(input.modelRoot);
    });
  }
});

test("rejects archive adversaries before destination residue", async (t) => {
  const cases = [
    ["parent traversal", [{ path: "../escape", contents: "x" }]],
    ["absolute path", [{ path: "/absolute", contents: "x" }]],
    ["symlink", [{ path: "linked", type: "2", linkPath: "target" }]],
    ["hardlink", [{ path: "linked", type: "1", linkPath: "target" }]],
    ["device", [{ path: "device", type: "3" }]],
    ["FIFO", [{ path: "fifo", type: "6" }]],
    ["newline path", [{ path: "bad\npath", contents: "x" }]],
  ];
  const { installWorkerRuntime } = await installer();
  for (const [name, entries] of cases) {
    await t.test(name, async (subtest) => {
      const input = await fixture(subtest, {
        archiveBytes: maliciousTar(entries),
        manifestFiles: [manifestFile(entries[0].path, entries[0].contents ?? "")],
      });
      await assert.rejects(installWorkerRuntime(input), /studio-runtime-unsafe-archive-entry/);
      await absent(input.modelRoot);
    });
  }
});

test("rejects ASCII case collisions and every non-ASCII path before writing", async (t) => {
  const { installWorkerRuntime } = await installer();
  for (const [name, paths] of [
    ["case", ["Case.txt", "case.txt"]],
    ["directory case", ["Pkg/a.txt", "pkg/b.txt"]],
    ["combining", ["e\u0301.txt"]],
    ["BMP", ["\ue000.txt"]],
    ["supplementary", ["\u{10000}.txt"]],
    ["sharp-s versus ss", ["ß.txt", "ss.txt"]],
    ["lone high surrogate", ["\ud800.txt"]],
    ["lone low surrogate", ["\udc00.txt"]],
  ]) {
    await t.test(name, async (subtest) => {
      const files = paths.map((path) => manifestFile(path, path));
      const input = await fixture(subtest, {
        archiveBytes: maliciousTar(paths.map((path) => ({ path, contents: path }))),
        manifestFiles: files,
      });
      await assert.rejects(installWorkerRuntime(input), /studio-runtime-unsafe-archive-entry/);
      await absent(input.modelRoot);
    });
  }
});

test("rejects a raw invalid UTF-8 manifest before writing", async (t) => {
  const invalidManifestBytes = Buffer.concat([
    Buffer.from('{"schemaVersion":1,"files":[],"treeSha256":"', "ascii"),
    Buffer.from([0xff]),
    Buffer.from('"}\n', "ascii"),
  ]);
  const input = await fixture(t, { manifestBytes: invalidManifestBytes });
  const { installWorkerRuntime } = await installer();
  await assert.rejects(installWorkerRuntime(input), /studio-runtime-manifest-digest-mismatch/);
  await absent(input.modelRoot);
});

test("rejects invalid package-inventory paths before candidate extraction", async (t) => {
  const invalidInventories = [
    { python: { licenseEvidence: ["python/é-LICENSE"] } },
    { python: { moduleRoots: ["é-python"] } },
    { package: { licenseEvidence: ["pkg/é-LICENSE"] } },
    { package: { moduleRoots: ["é-package"] } },
    { package: { moduleRoots: ["nested/root"] } },
    { package: { moduleRoots: [""] } },
    { package: { moduleRoots: ["."] } },
    { package: { moduleRoots: [".."] } },
  ];
  for (const [index, mutation] of invalidInventories.entries()) {
    await t.test(String(index), async (subtest) => {
      const packageInventory = packageInventoryFixture();
      if (mutation.python) Object.assign(packageInventory.python, mutation.python);
      if (mutation.package) Object.assign(packageInventory.packages[0], mutation.package);
      const input = await fixture(subtest, { packageInventory });
      const { installWorkerRuntime } = await installer();
      await assert.rejects(installWorkerRuntime(input), /studio-runtime-tree-mismatch/);
      await absent(input.modelRoot);
    });
  }
});

test("rejects raw malformed package-inventory JSON before candidate extraction", async (t) => {
  const input = await fixture(t, { packageInventoryBytes: Buffer.from([0xff]) });
  const { installWorkerRuntime } = await installer();
  await assert.rejects(installWorkerRuntime(input), /studio-runtime-tree-mismatch/);
  await absent(input.modelRoot);
});

test("rejects noncanonical USTAR numeric fields and nonzero reserved bytes", async (t) => {
  const base = maliciousTar([{ path: "declared", contents: "x" }]);
  const cases = [
    ["internal NUL in numeric field", mutateFirstTarHeader(base, (header) => {
      Buffer.from([0x30, 0x30, 0x00, 0x30, 0x36, 0x34, 0x34, 0x00]).copy(header, 100);
    })],
    ["nonzero devmajor", mutateFirstTarHeader(base, (header) => { header[329] = 1; })],
    ["nonzero reserved byte", mutateFirstTarHeader(base, (header) => { header[500] = 1; })],
  ];
  const { installWorkerRuntime } = await installer();
  for (const [name, archiveBytes] of cases) {
    await t.test(name, async (subtest) => {
      const input = await fixture(subtest, {
        archiveBytes,
        manifestFiles: [manifestFile("declared", "x")],
      });
      await assert.rejects(installWorkerRuntime(input), /studio-runtime-unsafe-archive-entry/);
      await absent(input.modelRoot);
    });
  }
});

test("rejects checksum, truncation, padding, duplicate, and backslash USTAR adversaries", async (t) => {
  const base = maliciousTar([{ path: "declared", contents: "x" }]);
  const cases = [
    ["checksum mismatch", mutateFirstTarHeader(base, (header) => { header[0] ^= 1; }, { refreshChecksum: false }), [manifestFile("declared", "x")]],
    ["truncated archive", base.subarray(0, base.length - 1), [manifestFile("declared", "x")]],
    ["nonzero padding", (() => { const changed = Buffer.from(base); changed[513] = 1; return changed; })(), [manifestFile("declared", "x")]],
    ["exact duplicate", maliciousTar([{ path: "declared", contents: "x" }, { path: "declared", contents: "x" }]), [manifestFile("declared", "x")]],
    ["backslash path", maliciousTar([{ path: "bad\\path", contents: "x" }]), [manifestFile("bad\\path", "x")]],
  ];
  const { installWorkerRuntime } = await installer();
  for (const [name, archiveBytes, manifestFiles] of cases) {
    await t.test(name, async (subtest) => {
      const input = await fixture(subtest, { archiveBytes, manifestFiles });
      await assert.rejects(installWorkerRuntime(input), /studio-runtime-/);
      await absent(input.modelRoot);
    });
  }
});

test("enforces file-count, single-file, total-size, and relative-path caps", async (t) => {
  const { installWorkerRuntime } = await installer();
  const limitCases = [
    {
      name: "too many files",
      files: Array.from({ length: 50_001 }, (_, index) => manifestFile(`f/${index}`, "")),
      archive: maliciousTar(Array.from({ length: 50_001 }, (_, index) => ({ path: `f/${index}` }))),
    },
    {
      name: "oversized file",
      files: [manifestFile("large", "", false, 512 * 1024 * 1024 + 1)],
      archive: maliciousTar([{ path: "large", declaredSize: 512 * 1024 * 1024 + 1 }]),
    },
    {
      name: "oversized total",
      files: Array.from({ length: 17 }, (_, index) => manifestFile(`large-${index}`, "", false, 512 * 1024 * 1024)),
      archive: maliciousTar(Array.from({ length: 17 }, (_, index) => ({ path: `large-${index}`, declaredSize: 512 * 1024 * 1024 }))),
    },
    {
      name: "oversized relative path",
      files: [manifestFile("p".repeat(1_025), "")],
      archive: maliciousTar([{ path: "short" }]),
    },
  ];
  for (const current of limitCases) {
    await t.test(current.name, async (subtest) => {
      const input = await fixture(subtest, {
        archiveBytes: current.archive,
        manifestFiles: current.files,
      });
      await assert.rejects(installWorkerRuntime(input), /studio-runtime-extraction-limit-exceeded/);
      await absent(input.modelRoot);
    });
  }
});

test("rejects changed candidate sidecars before model-root mutation", async (t) => {
  const input = await fixture(t);
  await writeFile(join(input.candidateRoot, "runtime-packages.json"), "changed\n");
  const { installWorkerRuntime } = await installer();
  await assert.rejects(installWorkerRuntime(input), /studio-runtime-candidate-digest-mismatch/);
  await absent(input.modelRoot);
});

test("oversized locked sidecar is rejected before its body is read", async (t) => {
  const input = await fixture(t);
  input.bundle.packageInventory.byteLength = 16 * 1024 * 1024 + 1;
  await writeFile(input.lockPath, JSON.stringify({
    schemaVersion: 1,
    artifacts: [],
    workerRuntimes: [input.bundle],
  }));
  const packagesPath = join(input.candidateRoot, "runtime-packages.json");
  const { installWorkerRuntime } = await installer();
  const observed = await observeTargetBodyReads(
    packagesPath,
    async () => {
      await assert.rejects(
        installWorkerRuntime(input),
        /studio-runtime-candidate-digest-mismatch/,
      );
    },
  );
  assert.equal(observed.bodyReads, 0);
  await absent(input.modelRoot);
});

test("requires exactly the four locked candidate files before model-root mutation", async (t) => {
  const input = await fixture(t);
  await writeFile(join(input.candidateRoot, "unapproved-fifth-file"), "not approved\n");
  const { installWorkerRuntime } = await installer();
  await assert.rejects(installWorkerRuntime(input), /studio-runtime-candidate-path-unsafe/);
  await absent(input.modelRoot);
});

test("deterministic tar writer and validator reject non-ASCII paths and invalid UTF-8 headers", async (t) => {
  const temporaryRoot = await mkdtemp(join(tmpdir(), "studio-runtime-tar-utf8-order-"));
  t.after(() => rm(temporaryRoot, { recursive: true, force: true }));
  const sourceRoot = join(temporaryRoot, "source");
  const archivePath = join(temporaryRoot, "runtime.tar");
  await writeRuntimeSource(sourceRoot, UTF8_PATH_ORDER.map((relativePath) => ({
    relativePath,
    contents: "",
    executable: false,
  })));
  await assert.rejects(createDeterministicRuntimeTar(sourceRoot, archivePath), /studio-runtime-tar-path-invalid/);
  await assert.rejects(lstat(archivePath), { code: "ENOENT" });

  const utf8OrderedArchive = maliciousTar(UTF8_PATH_ORDER.map((path) => ({ path })));
  const utf8ArchivePath = join(temporaryRoot, "utf8.tar");
  await writeFile(utf8ArchivePath, utf8OrderedArchive);
  await assert.rejects(assertDeterministicRuntimeTar(utf8ArchivePath), /studio-runtime-tar-metadata-invalid/);

  const utf16OrderedArchive = maliciousTar(JS_UTF16_PATH_ORDER.map((path) => ({ path })));
  const utf16ArchivePath = join(temporaryRoot, "utf16.tar");
  await writeFile(utf16ArchivePath, utf16OrderedArchive);
  await assert.rejects(
    assertDeterministicRuntimeTar(utf16ArchivePath),
    /studio-runtime-tar-metadata-invalid/,
  );

  const rawInvalidArchive = maliciousTar([{ path: "ascii.txt" }]);
  rawInvalidArchive[0] = 0xff;
  const refreshed = mutateFirstTarHeader(rawInvalidArchive, () => {});
  const rawInvalidArchivePath = join(temporaryRoot, "raw-invalid.tar");
  await writeFile(rawInvalidArchivePath, refreshed);
  await assert.rejects(
    assertDeterministicRuntimeTar(rawInvalidArchivePath),
    /studio-runtime-tar-metadata-invalid/,
  );
});

test("installer rejects a fixed UTF-8 digest whose manifest paths are outside portable ASCII", async (t) => {
  const runtimeFiles = [
    ...defaultRuntimeFiles,
    ...UTF8_PATH_ORDER.map((relativePath) => ({ relativePath, contents: "", executable: false })),
  ];
  const input = await fixture(t, {
    runtimeFiles,
    archiveBytes: maliciousTar(runtimeFiles.map((file) => ({
      path: file.relativePath,
      contents: file.contents,
      mode: file.executable ? 0o755 : 0o644,
    }))),
    treeSha256: "115d7580e84fa13dee62c8865c0f6423ef60a6d868d9d00ece774f21e006581b",
  });
  const { installWorkerRuntime } = await installer();

  await assert.rejects(installWorkerRuntime(input), /studio-runtime-unsafe-archive-entry/);
  await absent(input.modelRoot);
});

test("binds material inventory to the exact parent material packet digest", async (t) => {
  const input = await fixture(t, {
    bundle: { materialInventorySha256: "6".repeat(64) },
  });
  const { installWorkerRuntime } = await installer();
  await assert.rejects(installWorkerRuntime(input), /studio-runtime-tree-mismatch/);
  await absent(input.modelRoot);
});

test("rejects archive-manifest disagreement, missing or extra files, and permission mismatch", async (t) => {
  const { installWorkerRuntime } = await installer();
  const cases = [
    {
      name: "manifest archive path disagreement",
      files: [manifestFile("declared", "x")],
      archive: maliciousTar([{ path: "different", contents: "x" }]),
    },
    {
      name: "undeclared extracted file",
      files: [manifestFile("declared", "x")],
      archive: maliciousTar([{ path: "declared", contents: "x" }, { path: "extra", contents: "y" }]),
    },
    {
      name: "missing extracted file",
      files: [manifestFile("declared", "x"), manifestFile("missing", "y")],
      archive: maliciousTar([{ path: "declared", contents: "x" }]),
    },
    {
      name: "permission mismatch",
      files: [manifestFile("declared", "x", true)],
      archive: maliciousTar([{ path: "declared", contents: "x", mode: 0o644 }]),
    },
  ];
  for (const current of cases) {
    await t.test(current.name, async (subtest) => {
      const input = await fixture(subtest, {
        archiveBytes: current.archive,
        manifestFiles: current.files,
      });
      await assert.rejects(installWorkerRuntime(input), /studio-runtime-tree-mismatch/);
      await absent(input.modelRoot);
    });
  }
});

test("installs atomically, reuses exact bytes, and preserves older ready runtimes", async (t) => {
  const first = await fixture(t, { id: "older-runtime" });
  const { installWorkerRuntime } = await installer();
  const installed = await installWorkerRuntime(first);
  assert.deepEqual(installed, { artifact: "older-runtime", status: "ready", reused: false });
  assert.equal((await readFile(join(first.finalRoot, "site-packages/synthetic.txt"), "utf8")), "synthetic runtime bytes\n");

  const reused = await installWorkerRuntime(first);
  assert.deepEqual(reused, { artifact: "older-runtime", status: "ready", reused: true });

  const second = await fixture(t, { id: "newer-runtime" });
  second.modelRoot = first.modelRoot;
  second.finalRoot = join(first.modelRoot, "runtime", second.bundle.id);
  await installWorkerRuntime(second);
  assert.equal((await readFile(join(first.finalRoot, "site-packages/synthetic.txt"), "utf8")), "synthetic runtime bytes\n");
  await access(second.finalRoot);
});

test("exact runtime reuse remains idempotent with a canonical bound probe and rejects tree damage", async (t) => {
  const input = await fixture(t);
  const model = qwenArtifactFixture();
  const receipt = {
    schemaVersion: 1,
    runtime: {
      id: input.bundle.id,
      version: input.bundle.version,
      treeSha256: input.bundle.treeSha256,
    },
    model: {
      id: model.id,
      version: model.version,
      sha256: model.runtimeFiles[0].sha256,
    },
    promptVersion: "studio-qwen3-vl-zh-json-v5",
    inputSha256: "1ca0727bf07eb75b2d54a7060fb06b7e86afca54cdb763f47883d292b89b873a",
    outputSha256: "9".repeat(64),
    observationCount: 1,
    observationKinds: ["visible-product"],
    runtimeMs: 1_250,
    maximumResidentSetBytes: 32_000_000,
    syntheticInput: true,
    customerContentUsed: false,
    realModel: true,
    formalWorkerPath: true,
    networkDenied: true,
    providerMode: "local",
    publishedClaimed: false,
  };
  const receiptBytes = Buffer.from(`${canonicalJson(receipt)}\n`);
  input.bundle.probe = {
    receiptPath: `runtime/probes/${input.bundle.id}.json`,
    receiptSha256: sha256(receiptBytes),
    runtimeTreeSha256: input.bundle.treeSha256,
    modelSha256: model.runtimeFiles[0].sha256,
    promptVersion: receipt.promptVersion,
    inputSha256: receipt.inputSha256,
  };
  await writeFile(input.lockPath, JSON.stringify({
    schemaVersion: 1,
    artifacts: [model],
    workerRuntimes: [input.bundle],
  }));

  const { installWorkerRuntime } = await installer();
  assert.deepEqual(await installWorkerRuntime(input), {
    artifact: input.bundle.id,
    status: "ready",
    reused: false,
  });
  const probes = join(input.modelRoot, "runtime", "probes");
  await mkdir(probes, { mode: 0o700 });
  await writeFile(join(probes, `${input.bundle.id}.json`), receiptBytes, { mode: 0o600 });

  assert.deepEqual(await installWorkerRuntime(input), {
    artifact: input.bundle.id,
    status: "ready",
    reused: true,
  });
  await writeFile(join(input.finalRoot, "site-packages/synthetic.txt"), "damaged runtime\n");
  await assert.rejects(
    installWorkerRuntime(input),
    /studio-runtime-existing-runtime-differs/,
  );
});

test("same runtime ID with non-identical bytes refuses replacement", async (t) => {
  const first = await fixture(t);
  const { installWorkerRuntime } = await installer();
  await installWorkerRuntime(first);
  const second = await fixture(t, {
    runtimeFiles: defaultRuntimeFiles.map((file) => (
      file.relativePath === "site-packages/synthetic.txt"
        ? { ...file, contents: "different bytes\n" }
        : file
    )),
  });
  second.modelRoot = first.modelRoot;
  second.finalRoot = first.finalRoot;
  await assert.rejects(installWorkerRuntime(second), /studio-runtime-existing-runtime-differs/);
  assert.equal((await readFile(join(first.finalRoot, "site-packages/synthetic.txt"), "utf8")), "synthetic runtime bytes\n");
});

test("exact reuse rejects mode, hardlink, empty-directory, and sidecar boundary changes", async (t) => {
  const { installWorkerRuntime } = await installer();
  for (const [name, mutate] of [
    ["04775 runtime file", async (input) => {
      await chmod(join(input.finalRoot, "bin/run-offline-worker"), 0o4775);
    }],
    ["hardlinked runtime file", async (input) => {
      await link(
        join(input.finalRoot, "site-packages/synthetic.txt"),
        join(input.modelRoot, "runtime-file-hardlink"),
      );
    }],
    ["extra empty directory", async (input) => {
      await mkdir(join(input.finalRoot, "unapproved-empty-directory"));
    }],
    ["0777 runtime directory", async (input) => {
      await chmod(join(input.finalRoot, "site-packages"), 0o777);
    }],
    ["0644 sidecar", async (input) => {
      await chmod(join(input.finalRoot, "runtime-manifest.json"), 0o644);
    }],
    ["hardlinked sidecar", async (input) => {
      await link(
        join(input.finalRoot, "runtime-manifest.json"),
        join(input.modelRoot, "sidecar-hardlink"),
      );
    }],
  ]) {
    await t.test(name, async (subtest) => {
      const input = await fixture(subtest);
      await installWorkerRuntime(input);
      await mutate(input);
      await assert.rejects(
        installWorkerRuntime(input),
        /studio-runtime-existing-runtime-differs/,
      );
    });
  }
});

test("a rename interruption reconciles an exact promoted runtime and cleans only owned staging", async (t) => {
  const input = await fixture(t);
  const userOwned = join(input.modelRoot, ".runtime-staging", "user-owned");
  await mkdir(userOwned, { recursive: true });
  await chmod(input.modelRoot, 0o700);
  await chmod(join(input.modelRoot, ".runtime-staging"), 0o700);
  await writeFile(join(userOwned, "sentinel"), "preserve");
  const { installWorkerRuntime } = await installer();
  const result = await installWorkerRuntime(input, {
    promote: async (source, destination) => {
      await rename(source, destination);
      throw new Error("synthetic-interruption-after-rename");
    },
  });
  assert.deepEqual(result, { artifact: input.bundle.id, status: "ready", reused: false });
  assert.equal(await readFile(join(userOwned, "sentinel"), "utf8"), "preserve");
  assert.deepEqual(
    (await readdir(join(input.modelRoot, ".runtime-staging"))).sort(),
    [".locks", "user-owned"],
  );
});

test("a pre-rename interruption removes only the current owned staging", async (t) => {
  const input = await fixture(t);
  const userOwned = join(input.modelRoot, ".runtime-staging", "user-owned");
  await mkdir(userOwned, { recursive: true });
  await chmod(input.modelRoot, 0o700);
  await chmod(join(input.modelRoot, ".runtime-staging"), 0o700);
  await writeFile(join(userOwned, "sentinel"), "preserve");
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input, {
      promote: async () => { throw new Error("synthetic-interruption-before-rename"); },
    }),
    /studio-runtime-promotion-failed/,
  );
  await absent(input.finalRoot);
  assert.equal(await readFile(join(userOwned, "sentinel"), "utf8"), "preserve");
  assert.deepEqual(
    (await readdir(join(input.modelRoot, ".runtime-staging"))).sort(),
    [".locks", "user-owned"],
  );
});

test("pre-existing staging infrastructure must be current-owner mode 0700", async (t) => {
  const { installWorkerRuntime } = await installer();
  await t.test("world-writable staging parent", async (subtest) => {
    const input = await fixture(subtest);
    await mkdir(join(input.modelRoot, ".runtime-staging"), { recursive: true });
    await chmod(join(input.modelRoot, ".runtime-staging"), 0o777);
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-staging-path-unsafe/);
    await absent(input.finalRoot);
  });
  await t.test("wrong-owner staging parent", async (subtest) => {
    const input = await fixture(subtest);
    await mkdir(join(input.modelRoot, ".runtime-staging"), { recursive: true, mode: 0o700 });
    await chmod(join(input.modelRoot, ".runtime-staging"), 0o700);
    const actualUid = typeof process.geteuid === "function" ? process.geteuid() : process.getuid();
    await assert.rejects(
      installWorkerRuntime(input, { currentUid: actualUid + 1 }),
      /studio-runtime-staging-path-unsafe/,
    );
    await absent(input.finalRoot);
  });
  await t.test("world-writable install-lock parent", async (subtest) => {
    const input = await fixture(subtest);
    const lockParent = join(input.modelRoot, ".runtime-staging", ".locks");
    await mkdir(lockParent, { recursive: true });
    await chmod(join(input.modelRoot, ".runtime-staging"), 0o700);
    await chmod(lockParent, 0o777);
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-staging-path-unsafe/);
    await absent(input.finalRoot);
  });
});

test("staging-parent replacement during promotion is recovery-required", async (t) => {
  const input = await fixture(t);
  const stagingParent = join(input.modelRoot, ".runtime-staging");
  const retainedParent = join(input.modelRoot, ".runtime-staging-retained-evidence");
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input, {
      promote: async (source, destination) => {
        const sourceRelativePath = relative(stagingParent, source);
        await rename(stagingParent, retainedParent);
        await mkdir(stagingParent, { mode: 0o700 });
        await rename(join(retainedParent, sourceRelativePath), destination);
      },
    }),
    /studio-runtime-promotion-recovery-required/,
  );
  await access(retainedParent);
});

test("cooperative concurrent installers cannot race past the final-root precheck", async (t) => {
  const input = await fixture(t);
  const { installWorkerRuntime } = await installer();
  let releasePromotion;
  let enteredPromotion;
  const entered = new Promise((resolve) => { enteredPromotion = resolve; });
  const release = new Promise((resolve) => { releasePromotion = resolve; });
  const first = installWorkerRuntime(input, {
    promote: async (source, destination) => {
      enteredPromotion();
      await release;
      await rename(source, destination);
    },
  });
  await entered;
  try {
    await assert.rejects(
      installWorkerRuntime(input),
      /studio-runtime-install-busy/,
    );
  } finally {
    releasePromotion();
    await first;
  }
  assert.equal((await installWorkerRuntime(input)).reused, true);
});

test("the owned install lock refreshes its canonical heartbeat while work advances", async (t) => {
  const input = await fixture(t);
  const baseTime = Date.parse("2026-09-27T01:00:00.000Z");
  let tick = 0;
  let observed;
  const { installWorkerRuntime } = await installer();
  await installWorkerRuntime(input, {
    now: () => baseTime + (tick += 1_000),
    promote: async (source, destination) => {
      const lockPath = join(
        input.modelRoot,
        ".runtime-staging",
        ".locks",
        `${input.bundle.id}.lock`,
      );
      const bytes = await readFile(lockPath, "utf8");
      observed = JSON.parse(bytes);
      assert.equal(bytes, `${canonicalJson(observed)}\n`);
      await rename(source, destination);
    },
  });
  assert.equal(observed.bundleId, input.bundle.id);
  assert.match(observed.processInstanceSha256, /^[a-f0-9]{64}$/u);
  assert.ok(Date.parse(observed.heartbeatAt) > Date.parse(observed.createdAt));
});

test("stale install locks recover without treating fresh partial or live-owner locks as stale", async (t) => {
  const { installWorkerRuntime } = await installer();
  await t.test("dead pid", async (subtest) => {
    const input = await fixture(subtest);
    await writeInstallLock(input, canonicalInstallLock(input));
    const result = await installWorkerRuntime(input);
    assert.deepEqual(result, { artifact: input.bundle.id, status: "ready", reused: false });
  });
  await t.test("old zero-byte lock", async (subtest) => {
    const input = await fixture(subtest);
    await writeInstallLock(input, "", { old: true });
    const result = await installWorkerRuntime(input);
    assert.deepEqual(result, { artifact: input.bundle.id, status: "ready", reused: false });
  });
  await t.test("fresh partial lock", async (subtest) => {
    const input = await fixture(subtest);
    const lockPath = await writeInstallLock(input, '{"bundleId":');
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-install-busy/);
    assert.equal(await readFile(lockPath, "utf8"), '{"bundleId":');
    await absent(input.finalRoot);
  });
  await t.test("live pid", async (subtest) => {
    const input = await fixture(subtest);
    const livePid = process.pid;
    const now = new Date().toISOString();
    await writeInstallLock(input, canonicalInstallLock(input, {
      createdAt: now,
      heartbeatAt: now,
      pid: livePid,
    }));
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-install-busy/);
    await absent(input.finalRoot);
  });
});

test("an expired lock is recoverable when its pid belongs to a different process instance", async (t) => {
  const input = await fixture(t);
  const baseTime = Date.parse("2026-09-27T01:00:00.000Z");
  await writeInstallLock(input, canonicalInstanceBoundInstallLock(input));
  const { installWorkerRuntime } = await installer();
  const result = await installWorkerRuntime(input, {
    now: () => baseTime + 60_000,
    processIdentity: async () => "b".repeat(64),
    processLiveness: () => true,
  });
  assert.deepEqual(result, { artifact: input.bundle.id, status: "ready", reused: false });
});

test("the actual lock-owning process instance stays busy beyond the heartbeat lease", async (t) => {
  const input = await fixture(t);
  const baseTime = Date.parse("2026-09-27T01:00:00.000Z");
  await writeInstallLock(input, canonicalInstanceBoundInstallLock(input, {
    processInstanceSha256: "b".repeat(64),
  }));
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input, {
      now: () => baseTime + 60_000,
      processIdentity: async () => "b".repeat(64),
      processLiveness: () => true,
    }),
    /studio-runtime-install-busy/,
  );
  await absent(input.finalRoot);
});

test("a live legacy five-field lock remains busy after its timestamp and mtime are old", async (t) => {
  const input = await fixture(t);
  const baseTime = Date.now() - 60_000;
  const legacyBytes = canonicalLegacyInstallLock(input, {
    createdAt: new Date(baseTime).toISOString(),
  });
  const lockPath = await writeInstallLock(input, legacyBytes, { old: true });
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input, {
      now: () => baseTime + 60_000,
      processIdentity: async () => "b".repeat(64),
      processLiveness: (pid) => pid === process.pid,
    }),
    /studio-runtime-install-busy/,
  );
  assert.equal(await readFile(lockPath, "utf8"), legacyBytes);
  await absent(input.finalRoot);
});

test("a live legacy recovery claim remains busy after its lease is old", async (t) => {
  const input = await fixture(t);
  const lockPath = await writeInstallLock(input, canonicalInstallLock(input));
  const liveClaim = `${lockPath}.recover-${process.pid}-${Date.now() - 60_000}-00000000-0000-4000-8000-000000000007`;
  await link(lockPath, liveClaim);
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input, {
      processIdentity: async () => "b".repeat(64),
      processLiveness: (pid) => pid === process.pid,
    }),
    /studio-runtime-install-busy/,
  );
  await access(liveClaim);
  await access(lockPath);
  await absent(input.finalRoot);
});

test("artifact IDs reject dot segments while preserving the one-to-200 character contract", async (t) => {
  const { installWorkerRuntime } = await installer();
  for (const artifactId of [".", "..", "x".repeat(201)]) {
    await t.test(`reject ${artifactId.length}-character ${artifactId.slice(0, 2)}`, async (subtest) => {
      const input = await fixture(subtest, { id: artifactId });
      await assert.rejects(
        installWorkerRuntime(input),
        /studio-runtime-artifact-unapproved/,
      );
      await absent(input.modelRoot);
    });
  }
  await t.test("accept one character", async (subtest) => {
    const input = await fixture(subtest, { id: "x" });
    const installed = await installWorkerRuntime(input);
    assert.deepEqual(installed, { artifact: "x", status: "ready", reused: false });
  });
});

test("a 200-character artifact uses bounded hashed recovery basenames", async (t) => {
  const artifactId = "x".repeat(200);
  const input = await fixture(t, { id: artifactId });
  const expectedLockName = `${artifactId}.lock`;
  const expectedClaimPrefix = `worker-runtime-recovery-${sha256(artifactId)}-`;
  const staleLockPath = await writeInstallLock(input, canonicalInstallLock(input));
  assert.equal(basename(staleLockPath), expectedLockName);
  let observed;
  let stagingBasename;
  const { installWorkerRuntime } = await installer();
  const installed = await installWorkerRuntime(input, {
    beforeRecoveryUnlink: async ({ claimPath, lockPath }) => {
      observed = { claimPath, lockPath };
    },
    promote: async (source, destination) => {
      stagingBasename = basename(dirname(source));
      await rename(source, destination);
    },
  });
  assert.deepEqual(installed, { artifact: artifactId, status: "ready", reused: false });
  assert.ok(observed, "hashed stale lock must enter recovery");
  assert.equal(basename(observed.lockPath), expectedLockName);
  assert.equal(basename(observed.claimPath).startsWith(expectedClaimPrefix), true);
  assert.equal(basename(observed.claimPath).includes(artifactId), false);
  for (const derivedBasename of [
    basename(observed.lockPath),
    basename(observed.claimPath),
    `${expectedLockName}.recovery-leader`,
    stagingBasename,
    basename(input.finalRoot),
  ]) {
    assert.ok(Buffer.byteLength(derivedBasename, "utf8") <= 255, derivedBasename);
  }
  assert.equal(basename(input.finalRoot), artifactId);
});

test("Darwin process identity binds a stable boot session to a same-second process start", async () => {
  const loaded = await installer();
  assert.equal(typeof loaded.resolveProcessInstanceIdentity, "function");
  if (typeof loaded.resolveProcessInstanceIdentity !== "function") return;
  const identityFor = async (bootSessionUuid) => {
    const responses = {
      "/bin/ps": ["Sun Sep 27 01:00:00 2026\n", "Sun Sep 27 01:00:00 2026\n"],
      "/usr/sbin/sysctl": [`${bootSessionUuid}\n`, `${bootSessionUuid}\n`],
    };
    return loaded.resolveProcessInstanceIdentity(4242, {
      platform: "darwin",
      execute: async (executable, arguments_, options) => {
        assert.deepEqual(options.env, {
          PATH: "/usr/bin:/bin:/usr/sbin",
          LANG: "C",
          LC_ALL: "C",
          TZ: "UTC",
        });
        if (executable === "/bin/ps") assert.deepEqual(arguments_, ["-o", "lstart=", "-p", "4242"]);
        if (executable === "/usr/sbin/sysctl") assert.deepEqual(arguments_, ["-n", "kern.bootsessionuuid"]);
        return { stdout: responses[executable].shift() };
      },
    });
  };
  const beforeReboot = await identityFor("00000000-0000-4000-8000-000000000010");
  const afterReboot = await identityFor("00000000-0000-4000-8000-000000000011");
  assert.match(beforeReboot, /^[a-f0-9]{64}$/u);
  assert.match(afterReboot, /^[a-f0-9]{64}$/u);
  assert.notEqual(beforeReboot, afterReboot);
});

test("Darwin process identity fails closed when boot or process-start readings change", async (t) => {
  const loaded = await installer();
  assert.equal(typeof loaded.resolveProcessInstanceIdentity, "function");
  if (typeof loaded.resolveProcessInstanceIdentity !== "function") return;
  for (const [name, bootResponses, startResponses] of [
    [
      "boot session changed",
      ["00000000-0000-4000-8000-000000000010\n", "00000000-0000-4000-8000-000000000011\n"],
      ["Sun Sep 27 01:00:00 2026\n", "Sun Sep 27 01:00:00 2026\n"],
    ],
    [
      "process start changed",
      ["00000000-0000-4000-8000-000000000010\n", "00000000-0000-4000-8000-000000000010\n"],
      ["Sun Sep 27 01:00:00 2026\n", "Sun Sep 27 01:00:01 2026\n"],
    ],
  ]) {
    await t.test(name, async () => {
      const responses = {
        "/bin/ps": [...startResponses],
        "/usr/sbin/sysctl": [...bootResponses],
      };
      const identity = await loaded.resolveProcessInstanceIdentity(4242, {
        platform: "darwin",
        execute: async (executable) => ({ stdout: responses[executable].shift() }),
      });
      assert.equal(identity, null);
    });
  }
});

test("a fresh pid-instance mismatch and an unverifiable live pid both fail closed as busy", async (t) => {
  const { installWorkerRuntime } = await installer();
  for (const [name, elapsed, identity] of [
    ["fresh pid reuse", 1_000, "b".repeat(64)],
    ["identity unavailable", 60_000, null],
  ]) {
    await t.test(name, async (subtest) => {
      const input = await fixture(subtest);
      const baseTime = Date.parse("2026-09-27T01:00:00.000Z");
      const lockPid = process.pid === 1 ? 2 : 1;
      await writeInstallLock(input, canonicalInstanceBoundInstallLock(input, { pid: lockPid }));
      await assert.rejects(
        installWorkerRuntime(input, {
          now: () => baseTime + elapsed,
          processIdentity: async (pid) => (pid === process.pid ? "b".repeat(64) : identity),
          processLiveness: () => true,
        }),
        /studio-runtime-install-busy/,
      );
      await absent(input.finalRoot);
    });
  }
});

test("dead legacy and identity-bound recovery claims are removed without blocking install", async (t) => {
  const { installWorkerRuntime } = await installer();
  for (const [name, claimSuffix] of [
    ["legacy claim", `2147483647-${Date.now() - 60_000}-00000000-0000-4000-8000-000000000003`],
    ["identity-bound claim", `2147483647-${Date.now() - 60_000}-${"a".repeat(64)}-00000000-0000-4000-8000-000000000004`],
  ]) {
    await t.test(name, async (subtest) => {
      const input = await fixture(subtest);
      const lockPath = await writeInstallLock(input, canonicalInstallLock(input));
      const crashedClaim = `${lockPath}.recover-${claimSuffix}`;
      await link(lockPath, crashedClaim);
      const installed = await installWorkerRuntime(input);
      assert.deepEqual(installed, { artifact: input.bundle.id, status: "ready", reused: false });
      await absent(crashedClaim);
      assert.equal((await installWorkerRuntime(input)).reused, true);
    });
  }
});

test("an unknown same-inode lock-parent hardlink is cleaned without permanent busy", async (t) => {
  const input = await fixture(t);
  const lockPath = await writeInstallLock(input, canonicalInstallLock(input));
  const unknownLink = join(dirname(lockPath), `${input.bundle.id}.unknown-stale-link`);
  await link(lockPath, unknownLink);
  const { installWorkerRuntime } = await installer();
  const installed = await installWorkerRuntime(input);
  assert.deepEqual(installed, { artifact: input.bundle.id, status: "ready", reused: false });
  await absent(unknownLink);
});

test("a crash after unlinking the stale main lock is recovered through the leader inode", async (t) => {
  const input = await fixture(t);
  const lockPath = await writeInstallLock(input, canonicalInstallLock(input));
  const crashedClaim = `${lockPath}.recover-2147483647-${Date.now() - 60_000}-00000000-0000-4000-8000-000000000004`;
  const leaderPath = `${lockPath}.recovery-leader`;
  await link(lockPath, crashedClaim);
  await link(lockPath, leaderPath);
  await rm(lockPath);
  const { installWorkerRuntime } = await installer();
  const installed = await installWorkerRuntime(input);
  assert.deepEqual(installed, { artifact: input.bundle.id, status: "ready", reused: false });
  await absent(leaderPath);
  assert.equal((await installWorkerRuntime(input)).reused, true);
});

test("claim replacement is preserved on the raced attempt and recoverable on the next attempt", async (t) => {
  const input = await fixture(t);
  const lockPath = await writeInstallLock(input, canonicalInstallLock(input));
  const retainedClaim = join(dirname(lockPath), `${input.bundle.id}.retained-recovery-claim`);
  const replacementContents = "replacement-claim-evidence\n";
  let replaced = false;
  let replacementClaimPath;
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input, {
      beforeRecoveryUnlink: async ({ claimPath }) => {
        if (replaced) return;
        replaced = true;
        replacementClaimPath = claimPath;
        await rename(claimPath, retainedClaim);
        await writeFile(claimPath, replacementContents, { mode: 0o600 });
        await chmod(claimPath, 0o600);
      },
    }),
    /studio-runtime-install-busy/,
  );
  assert.equal(await readFile(lockPath, "utf8"), canonicalInstallLock(input));
  assert.equal(await readFile(replacementClaimPath, "utf8"), replacementContents);
  assert.equal(await readFile(retainedClaim, "utf8"), canonicalInstallLock(input));
  await absent(input.finalRoot);
  const installed = await installWorkerRuntime(input);
  assert.deepEqual(installed, { artifact: input.bundle.id, status: "ready", reused: false });
  await absent(retainedClaim);
});

test("a stale leader residue for a different inode does not block recovery of the current stale lock", async (t) => {
  const input = await fixture(t);
  const firstLockPath = await writeInstallLock(input, canonicalInstallLock(input));
  const leaderPath = `${firstLockPath}.recovery-leader`;
  await link(firstLockPath, leaderPath);
  await rm(firstLockPath);
  const currentLockPath = await writeInstallLock(input, canonicalInstallLock(input, {
    ownerToken: "00000000-0000-4000-8000-000000000005",
  }));
  const currentBefore = await lstat(currentLockPath, { bigint: true });
  const leaderBefore = await lstat(leaderPath, { bigint: true });
  assert.notEqual(currentBefore.ino, leaderBefore.ino);
  const { installWorkerRuntime } = await installer();
  const installed = await installWorkerRuntime(input);
  assert.deepEqual(installed, { artifact: input.bundle.id, status: "ready", reused: false });
  await absent(leaderPath);
});

test("an active identity-bound recovery claimant is never cleaned as stale debris", async (t) => {
  const input = await fixture(t);
  const lockPath = await writeInstallLock(input, canonicalInstallLock(input));
  const identity = "a".repeat(64);
  const activeClaim = `${lockPath}.recover-${process.pid}-${Date.now() - 60_000}-${identity}-00000000-0000-4000-8000-000000000006`;
  await link(lockPath, activeClaim);
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input, {
      processIdentity: async () => identity,
      processLiveness: (pid) => pid === process.pid,
    }),
    /studio-runtime-install-busy/,
  );
  await access(activeClaim);
  await access(lockPath);
  await absent(input.finalRoot);
});

test("two stale-lock recoverers never remove a new owner lock", async (t) => {
  const input = await fixture(t);
  await writeInstallLock(input, canonicalInstallLock(input));
  const { installWorkerRuntime } = await installer();
  const results = await Promise.allSettled([
    installWorkerRuntime(input),
    installWorkerRuntime(input),
  ]);
  assert.ok(results.some((result) => result.status === "fulfilled"));
  for (const result of results) {
    if (result.status === "rejected") assert.match(result.reason.stack, /studio-runtime-install-busy/);
  }
  assert.equal((await installWorkerRuntime(input)).reused, true);
  assert.deepEqual(
    (await readdir(join(input.modelRoot, ".runtime-staging", ".locks"))).sort(),
    [],
  );
});

test("an exact promoted runtime is reusable after its installer leaves a dead lock", async (t) => {
  const input = await fixture(t);
  const { installWorkerRuntime } = await installer();
  assert.equal((await installWorkerRuntime(input)).reused, false);
  await writeInstallLock(input, canonicalInstallLock(input));
  assert.equal((await installWorkerRuntime(input)).reused, true);
});

test("final write parents must be current-owner mode 0700", async (t) => {
  const { installWorkerRuntime } = await installer();
  await t.test("world-writable model root", async (subtest) => {
    const input = await fixture(subtest);
    await mkdir(input.modelRoot, { mode: 0o700 });
    await chmod(input.modelRoot, 0o777);
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-staging-path-unsafe/);
    await absent(input.finalRoot);
  });
  await t.test("world-writable runtime parent", async (subtest) => {
    const input = await fixture(subtest);
    await mkdir(join(input.modelRoot, "runtime"), { recursive: true, mode: 0o700 });
    await chmod(input.modelRoot, 0o700);
    await chmod(join(input.modelRoot, "runtime"), 0o777);
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-staging-path-unsafe/);
    await absent(input.finalRoot);
  });
  await t.test("wrong-owner expectation", async (subtest) => {
    const input = await fixture(subtest);
    await prepareInstallParents(input);
    const actualUid = typeof process.geteuid === "function" ? process.geteuid() : process.getuid();
    await assert.rejects(
      installWorkerRuntime(input, { currentUid: actualUid + 1 }),
      /studio-runtime-staging-path-unsafe/,
    );
    await absent(input.finalRoot);
  });
});

test("final runtime-parent replacement during promotion is recovery-required", async (t) => {
  const input = await fixture(t);
  await prepareInstallParents(input);
  const runtimeParent = join(input.modelRoot, "runtime");
  const retainedParent = join(input.modelRoot, "runtime-retained-evidence");
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input, {
      promote: async (source, destination) => {
        await rename(runtimeParent, retainedParent);
        await mkdir(runtimeParent, { mode: 0o700 });
        await rename(source, destination);
      },
    }),
    /studio-runtime-promotion-recovery-required/,
  );
  await access(retainedParent);
  await access(input.finalRoot);
});

test("lock replacement before release is retained as recovery evidence", async (t) => {
  const input = await fixture(t);
  const lockParent = await prepareInstallParents(input);
  const lockPath = join(lockParent, `${input.bundle.id}.lock`);
  const retainedOwnerLock = join(lockParent, `${input.bundle.id}.original-owner-lock`);
  const replacementContents = canonicalInstallLock(input, {
    createdAt: new Date().toISOString(),
    heartbeatAt: new Date().toISOString(),
    ownerToken: "00000000-0000-4000-8000-000000000002",
    pid: process.pid,
  });
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input, {
      promote: async (source, destination) => {
        await rename(source, destination);
        await rename(lockPath, retainedOwnerLock);
        await writeFile(lockPath, replacementContents, { mode: 0o600 });
        await chmod(lockPath, 0o600);
      },
    }),
    /studio-runtime-promotion-recovery-required/,
  );
  assert.equal(await readFile(lockPath, "utf8"), replacementContents);
  await access(retainedOwnerLock);
  await access(input.finalRoot);
});

test("release closes the owned lock descriptor when lock unlink fails", async (t) => {
  const input = await fixture(t);
  const lockPath = join(
    input.modelRoot,
    ".runtime-staging",
    ".locks",
    `${input.bundle.id}.lock`,
  );
  let openedLockHandle;
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input, {
      openInstallLock: async (...arguments_) => {
        openedLockHandle = await open(...arguments_);
        return openedLockHandle;
      },
      unlinkInstallLock: async () => {
        throw new Error("synthetic-lock-unlink-failure");
      },
    }),
    /synthetic-lock-unlink-failure/,
  );
  assert.ok(openedLockHandle);
  await assert.rejects(openedLockHandle.stat(), /file closed|EBADF/iu);
  await access(lockPath);
});

test("lost staging ownership returns recovery-required and preserves evidence", async (t) => {
  const input = await fixture(t, {
    runtimeFiles: defaultRuntimeFiles.map((file) => (
      file.relativePath === "python/bin/python3.12"
        ? {
          ...file,
          contents: "#!/bin/sh\nrm ../.installer-owner\nexit 1\n",
        }
        : file
    )),
  });
  const { installWorkerRuntime } = await installer();
  await assert.rejects(
    installWorkerRuntime(input),
    /studio-runtime-promotion-recovery-required/,
  );
  const retained = (await readdir(join(input.modelRoot, ".runtime-staging")))
    .filter((name) => name !== ".locks");
  assert.equal(retained.length, 1);
  await access(join(input.modelRoot, ".runtime-staging", retained[0], "runtime"));
});

test("candidate symlinks and pre-existing staging symlinks are never followed", async (t) => {
  const { installWorkerRuntime } = await installer();
  await t.test("candidate root", async (subtest) => {
    const input = await fixture(subtest);
    const original = `${input.candidateRoot}-original`;
    await rename(input.candidateRoot, original);
    await symlink(original, input.candidateRoot);
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-candidate-path-unsafe/);
    await absent(input.modelRoot);
  });
  await t.test("candidate sidecar", async (subtest) => {
    const input = await fixture(subtest);
    const packagePath = join(input.candidateRoot, "runtime-packages.json");
    const external = join(input.temporaryRoot, "external-packages.json");
    await writeFile(external, await readFile(packagePath));
    await rm(packagePath);
    await symlink(external, packagePath);
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-candidate-path-unsafe/);
    await absent(input.modelRoot);
  });
  await t.test("candidate sidecar hardlink", async (subtest) => {
    const input = await fixture(subtest);
    const packagePath = join(input.candidateRoot, "runtime-packages.json");
    const external = join(input.temporaryRoot, "external-packages-hardlink.json");
    await rename(packagePath, external);
    await link(external, packagePath);
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-candidate-path-unsafe/);
    await absent(input.modelRoot);
  });
  await t.test("candidate fifth directory", async (subtest) => {
    const input = await fixture(subtest);
    await mkdir(join(input.candidateRoot, "unapproved-directory"));
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-candidate-path-unsafe/);
    await absent(input.modelRoot);
  });
  await t.test("staging parent", async (subtest) => {
    const input = await fixture(subtest);
    const external = join(input.temporaryRoot, "external-stage");
    await mkdir(input.modelRoot, { recursive: true });
    await mkdir(external);
    await symlink(external, join(input.modelRoot, ".runtime-staging"));
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-staging-path-unsafe/);
    assert.deepEqual(await readdir(external), []);
  });
  await t.test("model-root direct parent", async (subtest) => {
    const input = await fixture(subtest);
    const external = join(input.temporaryRoot, "external-model-parent");
    const linkedParent = join(input.temporaryRoot, "linked-model-parent");
    await mkdir(external);
    await symlink(external, linkedParent);
    input.modelRoot = join(linkedParent, "models");
    input.finalRoot = join(input.modelRoot, "runtime", input.bundle.id);
    await assert.rejects(installWorkerRuntime(input), /studio-runtime-staging-path-unsafe/);
    assert.deepEqual(await readdir(external), []);
  });
});

test("the full-test runner excludes and then executes the worker runtime Node suite", async (t) => {
  const syntheticRoot = await mkdtemp(join(tmpdir(), "studio-full-runner-"));
  t.after(() => rm(syntheticRoot, { recursive: true, force: true }));
  await mkdir(join(syntheticRoot, "scripts", "tests"), { recursive: true });
  await mkdir(join(syntheticRoot, "packages", "review-app", "tests"), { recursive: true });
  await mkdir(join(syntheticRoot, "node_modules", "vitest"), { recursive: true });
  await writeFile(
    join(syntheticRoot, "scripts", "run-full-tests.mjs"),
    await readFile(fileURLToPath(new URL("../run-full-tests.mjs", import.meta.url))),
  );
  const passingNodeTest = `import test from "node:test";\ntest("synthetic", () => {});\n`;
  for (const relativePath of [
    "scripts/tests/studio-model-install.test.mjs",
    "scripts/tests/studio-model-approval.test.mjs",
    "scripts/tests/local-persona-model-probe.test.mjs",
  ]) {
    await writeFile(join(syntheticRoot, relativePath), passingNodeTest);
  }
  await writeFile(
    join(syntheticRoot, "scripts", "tests", "studio-worker-runtime-install.test.mjs"),
    `import test from "node:test";\nimport { writeFile } from "node:fs/promises";\ntest("worker", async () => writeFile(new URL("../../worker-runtime-node-suite-ran", import.meta.url), "ran\\n"));\n`,
  );
  await writeFile(join(syntheticRoot, "node_modules", "vitest", "vitest.mjs"), `
const args = process.argv.slice(2);
const creative = "packages/review-app/tests/creative-api.test.ts";
const browser = "packages/review-app/tests/studio-model-browser-pilot.test.ts";
const worker = "scripts/tests/studio-worker-runtime-install.test.mjs";
if (args[0] === "run" && args[1] !== creative && args[1] !== browser) {
  const excluded = args.some((value, index) => value === "--exclude" && args[index + 1] === worker);
  process.exit(excluded ? 0 : 23);
}
if (args[0] === "list" && args[1] === creative) {
  process.stdout.write(creative + " > synthetic\\n");
  process.exit(0);
}
if (args[0] === "run" && args[1] === creative) {
  const first = process.env.CREATIVE_API_SHARD === "1/4";
  process.stdout.write(first ? "Tests  1 passed (1)\\n" : "Tests  0 passed | 1 skipped (1)\\n");
  process.exit(0);
}
if (args[0] === "run" && args[1] === browser) process.exit(0);
process.exit(24);
`);
  const runnerEnvironment = { ...process.env };
  delete runnerEnvironment.NODE_TEST_CONTEXT;
  await execFile(
    process.execPath,
    [join(syntheticRoot, "scripts", "run-full-tests.mjs")],
    {
      cwd: syntheticRoot,
      env: runnerEnvironment,
      timeout: 20_000,
      maxBuffer: 1024 * 1024,
    },
  );
  assert.equal(await readFile(join(syntheticRoot, "worker-runtime-node-suite-ran"), "utf8"), "ran\n");
});

test("worker-runtime CLI is explicit, mutually exclusive, and installs only a synthetic root", async (t) => {
  for (const arguments_ of [
    ["--worker-runtime", "--artifact", "x"],
    ["--worker-runtime", "--artifact", "x", "--candidate-root", "x", "--persona"],
    ["--worker-runtime", "--artifact", "x", "--candidate-root", "x", "--vision"],
    ["--artifact", "x", "--candidate-root", "x"],
  ]) {
    await assert.rejects(
      execFile(process.execPath, [installerPath, ...arguments_]),
      (error) => error?.stderr?.includes("studio-model-installer-arguments-invalid"),
    );
  }

  const input = await fixture(t);
  const { stdout } = await execFile(process.execPath, [
    installerPath,
    "--worker-runtime",
    "--artifact", input.bundle.id,
    "--candidate-root", input.candidateRoot,
    "--lock", input.lockPath,
    "--root", input.modelRoot,
  ]);
  assert.deepEqual(JSON.parse(stdout), {
    artifact: input.bundle.id,
    status: "ready",
    reused: false,
  });
});

test("checker returns only browser-safe runtime states without private paths or approvals", async (t) => {
  const input = await fixture(t);
  const before = await execFile(process.execPath, [
    checkerPath,
    "--json",
    "--artifact", input.bundle.id,
    "--lock", input.lockPath,
    "--root", input.modelRoot,
  ]);
  const beforeReport = JSON.parse(before.stdout);
  assert.deepEqual(beforeReport, {
    schemaVersion: 1,
    workerRuntimes: [{
      id: input.bundle.id,
      version: input.bundle.version,
      status: "runtime-not-installed",
      treeSha256: input.bundle.treeSha256,
    }],
  });
  assert.equal(before.stdout.includes(input.modelRoot), false);
  assert.equal(before.stdout.includes(input.candidateRoot), false);
  assert.equal(before.stdout.includes(input.bundle.approvedBy), false);

  const { installWorkerRuntime } = await installer();
  await installWorkerRuntime(input);
  const after = await execFile(process.execPath, [
    checkerPath,
    "--json",
    "--artifact", input.bundle.id,
    "--lock", input.lockPath,
    "--root", input.modelRoot,
  ]);
  assert.equal(JSON.parse(after.stdout).workerRuntimes[0].status, "runtime-installed-probe-required");
});

test("checker maps unapproved runtime metadata to an allowed safe failure state", async (t) => {
  const input = await fixture(t, {
    bundle: { approved: false, enabled: false, approvedBy: null, approvedAt: null },
  });
  const checked = await execFile(process.execPath, [
    checkerPath,
    "--json",
    "--artifact", input.bundle.id,
    "--lock", input.lockPath,
    "--root", input.modelRoot,
  ]);
  assert.equal(
    JSON.parse(checked.stdout).workerRuntimes[0].status,
    "runtime-integrity-failed",
  );
  assert.equal(checked.stdout.includes("runtime-unapproved"), false);
});

test("checker never echoes a path-like or URL-like runtime identity", async (t) => {
  const privateIdentity = "https://private.invalid/Users/test-user/runtime";
  const input = await fixture(t, { id: privateIdentity });
  const checked = await execFile(process.execPath, [
    checkerPath,
    "--json",
    "--artifact", privateIdentity,
    "--lock", input.lockPath,
    "--root", input.modelRoot,
  ]);
  assert.deepEqual(JSON.parse(checked.stdout), {
    schemaVersion: 1,
    workerRuntimes: [{ status: "runtime-integrity-failed" }],
  });
  assert.equal(checked.stdout.includes(privateIdentity), false);
  assert.equal(checked.stdout.includes("private.invalid"), false);
});
