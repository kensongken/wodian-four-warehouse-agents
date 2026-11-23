import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import {
  chmod,
  lstat,
  mkdtemp,
  mkdir,
  open,
  rename,
  rm,
  symlink,
  truncate,
  writeFile,
} from "node:fs/promises";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";

const test = process.env.VITEST
  ? (await import("vitest")).test
  : (await import("node:test")).default;

import {
  findWorkerRuntime,
  inspectWorkerRuntime,
  MAX_WORKER_RUNTIME_PROBE_RECEIPT_BYTES,
  resolveFormalWorkerRuntimeConfig,
  workerRuntimeApprovalIssue,
} from "../studio-worker-runtime-registry.mjs";
import { normalizeWorkerRuntimeReadiness } from "../check-studio-models.mjs";
import { compareRuntimePathsByUtf8Bytes } from "../studio-runtime-canonical.mjs";
import {
  UTF8_PATH_ORDER,
  UTF8_PATH_RECORDS,
  UTF8_PATH_RECORD_TREE_SHA256,
} from "./worker-runtime-path-order-fixture.mjs";

const APPROVED_DEPENDENCY_LOCK_SHA256 =
  "774b30f7f04e3d78faf72f75fca0d0b5823f8244d751c6c10f9c97f45fd85521";
const APPROVED_WORKER_DEPENDENCY_LOCK_SHA256 =
  "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041";
const APPROVAL_PACKET_ID = `worker-runtime-bundle-v1-sha256-${"a".repeat(64)}`;
const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const FORMAL_PROMPT_VERSION = "studio-qwen3-vl-zh-json-v5";
const FORMAL_INPUT_SHA256 = "1ca0727bf07eb75b2d54a7060fb06b7e86afca54cdb763f47883d292b89b873a";

const runtimeFiles = Object.freeze([
  {
    relativePath: "bin/run-offline-worker",
    contents: "#!/bin/sh\nexit 0\n",
    executable: true,
  },
  {
    relativePath: "python/lib/runtime.txt",
    contents: "synthetic runtime bytes\n",
    executable: false,
  },
]);
const runtimeManifestFiles = Object.freeze(runtimeFiles.map((file) => Object.freeze({
  relativePath: file.relativePath,
  byteLength: Buffer.byteLength(file.contents),
  sha256: sha256(file.contents),
  type: "file",
  executable: file.executable,
})));
const runtimeTreeSha256 = (files) => {
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
};
const RUNTIME_TREE_SHA256 = runtimeTreeSha256(runtimeManifestFiles);

const packageInventoryFixture = () => ({
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
});

const sidecarContents = Object.freeze({
  manifest: JSON.stringify({
    schemaVersion: 1,
    files: runtimeManifestFiles,
    treeSha256: RUNTIME_TREE_SHA256,
  }),
  packageInventory: JSON.stringify(packageInventoryFixture()),
  derivationReceipt: JSON.stringify({
    schemaVersion: 1,
    status: "succeeded",
    materialPacketId: `worker-runtime-materials-v1-sha256-${"1".repeat(64)}`,
  }),
});

const lockedFile = (relativePath, contents) => ({
  relativePath,
  sha256: sha256(contents),
  byteLength: Buffer.byteLength(contents),
});

const runtimeFixture = (overrides = {}) => ({
  schemaVersion: 1,
  id: "synthetic-worker-runtime",
  version: "0.6.6-py312",
  pythonVersion: "3.12.13",
  platform: "darwin",
  architecture: "arm64",
  minimumOsVersion: "14.0",
  dependencyLockSha256: APPROVED_DEPENDENCY_LOCK_SHA256,
  workerDependencyLockSha256: APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
  materialInventorySha256: "1".repeat(64),
  approvalPacketId: APPROVAL_PACKET_ID,
  archive: lockedFile("runtime.tar", "synthetic archive bytes"),
  manifest: lockedFile("runtime-manifest.json", sidecarContents.manifest),
  packageInventory: lockedFile("runtime-packages.json", sidecarContents.packageInventory),
  derivationReceipt: lockedFile("derivation-receipt.json", sidecarContents.derivationReceipt),
  treeSha256: RUNTIME_TREE_SHA256,
  licenseReview: "approved",
  enabled: true,
  approved: true,
  approvedBy: "local-runtime-owner",
  approvedAt: "2026-09-23T01:00:00.000Z",
  probe: null,
  ...overrides,
});

const runtimeLock = (overrides = {}) => ({
  schemaVersion: 1,
  artifacts: [],
  workerRuntimes: [runtimeFixture(overrides)],
});

const qwenArtifactFixture = (overrides = {}) => {
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
    ...overrides,
  };
};

const probeReceipt = (overrides = {}) => ({
  schemaVersion: 1,
  runtime: {
    id: "synthetic-worker-runtime",
    version: "0.6.6-py312",
    treeSha256: RUNTIME_TREE_SHA256,
  },
  model: {
    id: "qwen3-vl-2b-instruct-4bit",
    version: "2b-4bit",
    sha256: "7".repeat(64),
  },
  promptVersion: FORMAL_PROMPT_VERSION,
  inputSha256: FORMAL_INPUT_SHA256,
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
  ...overrides,
});

const canonicalJson = (value) => {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
};

async function withTemporaryRoots(count, callback) {
  const roots = await Promise.all(Array.from(
    { length: count },
    () => mkdtemp(join(tmpdir(), "studio-worker-runtime-registry-")),
  ));
  try {
    return await callback(...roots);
  } finally {
    await Promise.all(roots.map((root) => rm(root, { recursive: true, force: true })));
  }
}

async function installLockedSidecars(root, bundle) {
  const runtimeRoot = join(root, "runtime", bundle.id);
  await mkdir(runtimeRoot, { recursive: true });
  await chmod(join(root, "runtime"), 0o700);
  await chmod(runtimeRoot, 0o700);
  await Promise.all([
    writeFile(join(runtimeRoot, bundle.manifest.relativePath), sidecarContents.manifest, { mode: 0o600 }),
    writeFile(
      join(runtimeRoot, bundle.packageInventory.relativePath),
      sidecarContents.packageInventory,
      { mode: 0o600 },
    ),
    writeFile(
      join(runtimeRoot, bundle.derivationReceipt.relativePath),
      sidecarContents.derivationReceipt,
      { mode: 0o600 },
    ),
  ]);
  await Promise.all([
    chmod(join(runtimeRoot, bundle.manifest.relativePath), 0o600),
    chmod(join(runtimeRoot, bundle.packageInventory.relativePath), 0o600),
    chmod(join(runtimeRoot, bundle.derivationReceipt.relativePath), 0o600),
  ]);
}

async function installRuntimeTree(root, bundle) {
  const runtimeRoot = join(root, "runtime", bundle.id);
  for (const file of runtimeFiles) {
    const path = join(runtimeRoot, file.relativePath);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, file.contents);
    await chmod(path, file.executable ? 0o755 : 0o644);
  }
  await Promise.all([
    chmod(join(runtimeRoot, "bin"), 0o755),
    chmod(join(runtimeRoot, "python"), 0o755),
    chmod(join(runtimeRoot, "python/lib"), 0o755),
  ]);
}

async function installReadyRuntime(root, bundle) {
  await installLockedSidecars(root, bundle);
  await installRuntimeTree(root, bundle);
}

async function writeRuntimeLock(root, lock) {
  const lockPath = join(root, "models.lock.json");
  await writeFile(lockPath, JSON.stringify(lock), { mode: 0o600 });
  await chmod(lockPath, 0o600);
  return lockPath;
}

async function observeTargetBodyReads(targetPath, callback) {
  const target = await lstat(targetPath, { bigint: true });
  const probeHandle = await open(targetPath, constants.O_RDONLY);
  const fileHandlePrototype = Object.getPrototypeOf(probeHandle);
  await probeHandle.close();
  const originalRead = fileHandlePrototype.read;
  const originalReadFile = fileHandlePrototype.readFile;
  let bodyReads = 0;
  const tracksTarget = async function tracksTarget(original, arguments_) {
    const opened = await this.stat({ bigint: true });
    if (opened.dev === target.dev && opened.ino === target.ino) bodyReads += 1;
    return original.apply(this, arguments_);
  };
  fileHandlePrototype.read = async function trackedRead(...arguments_) {
    return tracksTarget.call(this, originalRead, arguments_);
  };
  fileHandlePrototype.readFile = async function trackedReadFile(...arguments_) {
    return tracksTarget.call(this, originalReadFile, arguments_);
  };
  try {
    const result = await callback();
    return { bodyReads, result };
  } finally {
    fileHandlePrototype.read = originalRead;
    fileHandlePrototype.readFile = originalReadFile;
  }
}

test("findWorkerRuntime normalizes an absent workerRuntimes field to an empty registry", () => {
  assert.throws(
    () => findWorkerRuntime({ schemaVersion: 1, artifacts: [] }, "missing-runtime"),
    /studio-worker-runtime-registry-runtime-unknown/,
  );
});

test("findWorkerRuntime resolves exactly one locked runtime", () => {
  const bundle = findWorkerRuntime(runtimeLock(), "synthetic-worker-runtime");
  assert.equal(bundle.version, "0.6.6-py312");
  assert.throws(
    () => findWorkerRuntime({
      ...runtimeLock(),
      workerRuntimes: [runtimeFixture(), runtimeFixture()],
    }, "synthetic-worker-runtime"),
    /studio-worker-runtime-registry-runtime-unknown/,
  );
});

test("formal config synchronously resolves only the verified installed launcher", () => withTemporaryRoots(
  1,
  async (root) => {
    const lock = runtimeLock();
    const bundle = lock.workerRuntimes[0];
    await installReadyRuntime(root, bundle);
    const lockPath = await writeRuntimeLock(root, lock);
    const launcherPath = join(root, "runtime", "synthetic-worker-runtime", "bin", "run-offline-worker");
    const launcherDetails = await lstat(launcherPath, { bigint: true });

    assert.deepEqual(resolveFormalWorkerRuntimeConfig({
      lockPath,
      modelRoot: root,
      runtimeId: bundle.id,
    }), {
      runtimeRoot: join(root, "runtime", "synthetic-worker-runtime"),
      launcherPath,
      launcherIdentity: {
        device: launcherDetails.dev.toString(),
        inode: launcherDetails.ino.toString(),
        byteLength: runtimeManifestFiles[0].byteLength,
        sha256: runtimeManifestFiles[0].sha256,
      },
      status: "runtime-installed-probe-required",
      bundle: {
        id: "synthetic-worker-runtime",
        treeSha256: RUNTIME_TREE_SHA256,
      },
    });
  },
));

test("formal config fails closed for missing, unapproved, and changed launcher identities", () => withTemporaryRoots(
  3,
  async (missingRoot, unapprovedRoot, changedRoot) => {
    const approved = runtimeLock();
    const approvedBundle = approved.workerRuntimes[0];
    const missingLock = await writeRuntimeLock(missingRoot, approved);
    assert.throws(
      () => resolveFormalWorkerRuntimeConfig({
        lockPath: missingLock,
        modelRoot: missingRoot,
        runtimeId: approvedBundle.id,
      }),
      /studio-formal-worker-runtime-unavailable/,
    );

    const unapproved = runtimeLock({
      approved: false,
      enabled: false,
      approvedBy: null,
      approvedAt: null,
    });
    await installReadyRuntime(unapprovedRoot, unapproved.workerRuntimes[0]);
    const unapprovedLock = await writeRuntimeLock(unapprovedRoot, unapproved);
    assert.throws(
      () => resolveFormalWorkerRuntimeConfig({
        lockPath: unapprovedLock,
        modelRoot: unapprovedRoot,
        runtimeId: unapproved.workerRuntimes[0].id,
      }),
      /studio-formal-worker-runtime-unapproved/,
    );

    await installReadyRuntime(changedRoot, approvedBundle);
    const changedLock = await writeRuntimeLock(changedRoot, approved);
    await writeFile(
      join(changedRoot, "runtime", approvedBundle.id, "bin", "run-offline-worker"),
      "#!/bin/sh\nexit 7\n",
    );
    await chmod(
      join(changedRoot, "runtime", approvedBundle.id, "bin", "run-offline-worker"),
      0o755,
    );
    assert.throws(
      () => resolveFormalWorkerRuntimeConfig({
        lockPath: changedLock,
        modelRoot: changedRoot,
        runtimeId: approvedBundle.id,
      }),
      /studio-formal-worker-runtime-integrity-failed/,
    );
  },
));

test("formal config rejects unsafe lock and runtime ownership or modes", () => withTemporaryRoots(
  3,
  async (lockModeRoot, runtimeModeRoot, symlinkRoot) => {
    const lock = runtimeLock();
    const bundle = lock.workerRuntimes[0];

    await installReadyRuntime(lockModeRoot, bundle);
    const lockModePath = await writeRuntimeLock(lockModeRoot, lock);
    await chmod(lockModePath, 0o666);
    assert.throws(
      () => resolveFormalWorkerRuntimeConfig({
        lockPath: lockModePath,
        modelRoot: lockModeRoot,
        runtimeId: bundle.id,
      }),
      /studio-formal-worker-runtime-integrity-failed/,
    );

    await installReadyRuntime(runtimeModeRoot, bundle);
    const runtimeModeLock = await writeRuntimeLock(runtimeModeRoot, lock);
    await chmod(join(runtimeModeRoot, "runtime", bundle.id), 0o755);
    assert.throws(
      () => resolveFormalWorkerRuntimeConfig({
        lockPath: runtimeModeLock,
        modelRoot: runtimeModeRoot,
        runtimeId: bundle.id,
      }),
      /studio-formal-worker-runtime-integrity-failed/,
    );

    await installReadyRuntime(symlinkRoot, bundle);
    const realLock = await writeRuntimeLock(symlinkRoot, lock);
    const linkedLock = join(symlinkRoot, "linked-models.lock.json");
    await symlink(realLock, linkedLock);
    assert.throws(
      () => resolveFormalWorkerRuntimeConfig({
        lockPath: linkedLock,
        modelRoot: symlinkRoot,
        runtimeId: bundle.id,
      }),
      /studio-formal-worker-runtime-integrity-failed/,
    );
  },
));

test("formal config rejects an extra empty directory outside the locked runtime topology", () => withTemporaryRoots(
  1,
  async (root) => {
    const lock = runtimeLock();
    const bundle = lock.workerRuntimes[0];
    await installReadyRuntime(root, bundle);
    const lockPath = await writeRuntimeLock(root, lock);
    await mkdir(join(root, "runtime", bundle.id, "undeclared-empty-directory"), { mode: 0o755 });

    assert.throws(
      () => resolveFormalWorkerRuntimeConfig({
        lockPath,
        modelRoot: root,
        runtimeId: bundle.id,
      }),
      /studio-formal-worker-runtime-integrity-failed/,
    );
  },
));

test("same runtime metadata is rejected when the approved lock digest changes", () => {
  const bundle = runtimeFixture({ dependencyLockSha256: "8".repeat(64) });
  assert.equal(workerRuntimeApprovalIssue(bundle), "lock-mismatch");
});

test("runtime metadata requires the independently approved Worker lock digest", () => {
  assert.equal(workerRuntimeApprovalIssue(runtimeFixture({
    workerDependencyLockSha256: APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
  })), null);
  const missingWorkerLock = runtimeFixture();
  delete missingWorkerLock.workerDependencyLockSha256;
  assert.equal(workerRuntimeApprovalIssue(missingWorkerLock), "lock-invalid");
  assert.equal(workerRuntimeApprovalIssue(runtimeFixture({
    workerDependencyLockSha256: "8".repeat(64),
  })), "worker-lock-mismatch");
});

test("installed derivation receipt binds the parent material packet digest", () => withTemporaryRoots(
  1,
  async (root) => {
    const bundle = runtimeFixture({ materialInventorySha256: "6".repeat(64) });
    await installReadyRuntime(root, bundle);
    assert.equal((await inspectWorkerRuntime(bundle, root)).status, "runtime-integrity-failed");
  },
));

test("registry approval time validation has exact Zod datetime parity", () => {
  assert.equal(workerRuntimeApprovalIssue(runtimeFixture({
    approvedAt: "2026-09-23Z",
  })), "lock-invalid");
  assert.equal(workerRuntimeApprovalIssue(runtimeFixture({
    approvedAt: "2026-09-23T01:00Z",
  })), null);
});

test("runtime status is unapproved before inspecting private paths", () => withTemporaryRoots(1, async (root) => {
  const bundle = runtimeFixture({
    approved: false,
    enabled: false,
    approvedBy: null,
    approvedAt: null,
  });
  assert.deepEqual(await inspectWorkerRuntime(bundle, root), {
    id: "synthetic-worker-runtime",
    version: "0.6.6-py312",
    status: "runtime-unapproved",
    treeSha256: RUNTIME_TREE_SHA256,
  });
}));

test("runtime status distinguishes a missing install from damaged installed sidecars", () => withTemporaryRoots(1, async (root) => {
  const bundle = runtimeFixture();
  assert.equal((await inspectWorkerRuntime(bundle, root)).status, "runtime-not-installed");

  await installLockedSidecars(root, bundle);
  await writeFile(
    join(root, "runtime", bundle.id, bundle.manifest.relativePath),
    `${sidecarContents.manifest}\nchanged`,
  );
  assert.equal((await inspectWorkerRuntime(bundle, root)).status, "runtime-integrity-failed");
}));

test("locked sidecar size mismatch is rejected before its body is read", () => withTemporaryRoots(1, async (root) => {
  const bundle = runtimeFixture();
  await installReadyRuntime(root, bundle);
  const manifestPath = join(root, "runtime", bundle.id, bundle.manifest.relativePath);
  await writeFile(manifestPath, `${sidecarContents.manifest}x`);

  const observed = await observeTargetBodyReads(
    manifestPath,
    () => inspectWorkerRuntime(bundle, root),
  );
  assert.equal(observed.result.status, "runtime-integrity-failed");
  assert.equal(observed.bodyReads, 0);
}));

test("runtime inspection never follows an installed-root parent symlink", () => withTemporaryRoots(2, async (
  root,
  externalRoot,
) => {
  const bundle = runtimeFixture();
  await installReadyRuntime(externalRoot, bundle);
  await symlink(join(externalRoot, "runtime"), join(root, "runtime"));

  assert.equal((await inspectWorkerRuntime(bundle, root)).status, "runtime-integrity-failed");
}));

test("sidecars alone never count as an installed runtime tree", () => withTemporaryRoots(1, async (root) => {
  const bundle = runtimeFixture();
  await installLockedSidecars(root, bundle);
  assert.equal((await inspectWorkerRuntime(bundle, root)).status, "runtime-integrity-failed");
}));

test("registry rejects invalid package inventory before readiness or formal launch", () => withTemporaryRoots(
  3,
  async (...roots) => {
    const inventories = [
      (() => { const value = packageInventoryFixture(); value.python.licenseEvidence = ["python/é"]; return value; })(),
      (() => { const value = packageInventoryFixture(); value.packages[0].moduleRoots = ["nested/root"]; return value; })(),
      Buffer.from([0xff]),
    ];
    for (const [index, root] of roots.entries()) {
      const bytes = Buffer.isBuffer(inventories[index])
        ? inventories[index]
        : Buffer.from(JSON.stringify(inventories[index]));
      const bundle = runtimeFixture({
        packageInventory: lockedFile("runtime-packages.json", bytes),
      });
      await installReadyRuntime(root, bundle);
      await writeFile(join(root, "runtime", bundle.id, "runtime-packages.json"), bytes, { mode: 0o600 });
      await chmod(join(root, "runtime", bundle.id, "runtime-packages.json"), 0o600);
      const lockPath = await writeRuntimeLock(root, {
        schemaVersion: 1, artifacts: [], workerRuntimes: [bundle],
      });
      assert.equal((await inspectWorkerRuntime(bundle, root)).status, "runtime-integrity-failed");
      assert.throws(
        () => resolveFormalWorkerRuntimeConfig({ lockPath, modelRoot: root, runtimeId: bundle.id }),
        /studio-formal-worker-runtime-integrity-failed/,
      );
    }
  },
));

test("registry rejects a fixed UTF-8 digest whose manifest paths are outside portable ASCII", () => withTemporaryRoots(
  1,
  async (root) => {
    const manifest = JSON.stringify({
      schemaVersion: 1,
      files: UTF8_PATH_RECORDS,
      treeSha256: UTF8_PATH_RECORD_TREE_SHA256,
    });
    const packageInventory = JSON.stringify({ schemaVersion: 1, packages: [] });
    const derivationReceipt = sidecarContents.derivationReceipt;
    const bundle = runtimeFixture({
      treeSha256: UTF8_PATH_RECORD_TREE_SHA256,
      manifest: lockedFile("runtime-manifest.json", manifest),
      packageInventory: lockedFile("runtime-packages.json", packageInventory),
      derivationReceipt: lockedFile("derivation-receipt.json", derivationReceipt),
    });
    const runtimeRoot = join(root, "runtime", bundle.id);
    await mkdir(runtimeRoot, { recursive: true });
    await chmod(join(root, "runtime"), 0o700);
    await chmod(runtimeRoot, 0o700);
    for (const relativePath of UTF8_PATH_ORDER) {
      const path = join(runtimeRoot, relativePath);
      await mkdir(dirname(path), { recursive: true });
      await writeFile(path, "", { mode: 0o644 });
      await chmod(path, 0o644);
    }
    for (const relativePath of ["runtime"]) {
      await chmod(join(runtimeRoot, relativePath), 0o755);
    }
    await Promise.all([
      writeFile(join(runtimeRoot, bundle.manifest.relativePath), manifest, { mode: 0o600 }),
      writeFile(join(runtimeRoot, bundle.packageInventory.relativePath), packageInventory, { mode: 0o600 }),
      writeFile(join(runtimeRoot, bundle.derivationReceipt.relativePath), derivationReceipt, { mode: 0o600 }),
    ]);
    await Promise.all([
      chmod(join(runtimeRoot, bundle.manifest.relativePath), 0o600),
      chmod(join(runtimeRoot, bundle.packageInventory.relativePath), 0o600),
      chmod(join(runtimeRoot, bundle.derivationReceipt.relativePath), 0o600),
    ]);

    assert.equal((await inspectWorkerRuntime(bundle, root)).status, "runtime-integrity-failed");
  },
));

test("runtime inspection rejects missing, changed, wrong-permission, and extra tree files", () => withTemporaryRoots(
  4,
  async (missingRoot, changedRoot, permissionRoot, extraRoot) => {
    const bundle = runtimeFixture();
    for (const root of [missingRoot, changedRoot, permissionRoot, extraRoot]) {
      await installReadyRuntime(root, bundle);
    }

    await rm(join(missingRoot, "runtime", bundle.id, runtimeFiles[1].relativePath));
    await writeFile(
      join(changedRoot, "runtime", bundle.id, runtimeFiles[1].relativePath),
      "changed runtime bytes\n",
    );
    await chmod(
      join(permissionRoot, "runtime", bundle.id, runtimeFiles[0].relativePath),
      0o600,
    );
    await writeFile(join(extraRoot, "runtime", bundle.id, "undeclared.txt"), "extra\n");

    for (const root of [missingRoot, changedRoot, permissionRoot, extraRoot]) {
      assert.equal((await inspectWorkerRuntime(bundle, root)).status, "runtime-integrity-failed");
    }
  },
));

test("checker preserves runtime integrity failures even when the lock declares a probe", () => withTemporaryRoots(
  3,
  async (launcherRoot, sidecarRoot, modeRoot) => {
    const probe = {
      receiptPath: "runtime/probes/synthetic-worker-runtime.json",
      receiptSha256: "9".repeat(64),
      runtimeTreeSha256: RUNTIME_TREE_SHA256,
      modelSha256: "7".repeat(64),
      promptVersion: FORMAL_PROMPT_VERSION,
      inputSha256: FORMAL_INPUT_SHA256,
    };
    const bundle = runtimeFixture({ probe });
    const lock = {
      schemaVersion: 1,
      artifacts: [qwenArtifactFixture()],
      workerRuntimes: [bundle],
    };
    for (const root of [launcherRoot, sidecarRoot, modeRoot]) {
      await installReadyRuntime(root, bundle);
    }
    await writeFile(
      join(launcherRoot, "runtime", bundle.id, "bin", "run-offline-worker"),
      "#!/bin/sh\nexit 9\n",
    );
    await writeFile(
      join(sidecarRoot, "runtime", bundle.id, bundle.manifest.relativePath),
      `${sidecarContents.manifest}\nchanged`,
    );
    await chmod(join(modeRoot, "runtime", bundle.id, "bin", "run-offline-worker"), 0o600);

    const statuses = [];
    for (const root of [launcherRoot, sidecarRoot, modeRoot]) {
      const inspected = await inspectWorkerRuntime(bundle, root, lock);
      statuses.push({
        inspected: inspected.status,
        public: normalizeWorkerRuntimeReadiness(bundle, inspected),
      });
    }
    assert.deepEqual(statuses, [
      { inspected: "runtime-integrity-failed", public: "runtime-integrity-failed" },
      { inspected: "runtime-integrity-failed", public: "runtime-integrity-failed" },
      { inspected: "runtime-integrity-failed", public: "runtime-integrity-failed" },
    ]);
  },
));

test("declared runtime-file size mismatch is rejected before its body is read", () => withTemporaryRoots(
  1,
  async (root) => {
    const bundle = runtimeFixture();
    await installReadyRuntime(root, bundle);
    const runtimeFilePath = join(root, "runtime", bundle.id, runtimeFiles[0].relativePath);
    await writeFile(runtimeFilePath, `${runtimeFiles[0].contents}x`);

    const observed = await observeTargetBodyReads(
      runtimeFilePath,
      () => inspectWorkerRuntime(bundle, root),
    );
    assert.equal(observed.result.status, "runtime-integrity-failed");
    assert.equal(observed.bodyReads, 0);
  },
));

test("runtime status is probe-required until the bound real probe exists", () => withTemporaryRoots(1, async (root) => {
  const lock = runtimeLock({ approved: true, probe: null });
  await installReadyRuntime(root, lock.workerRuntimes[0]);
  const status = await inspectWorkerRuntime(lock.workerRuntimes[0], root);
  assert.equal(status.status, "runtime-installed-probe-required");
}));

test("formal probe readiness accepts only the canonical CLI receipt bound to locked Qwen", () => withTemporaryRoots(
  11,
  async (...roots) => {
    const model = qwenArtifactFixture();
    const baseReceipt = probeReceipt({
      promptVersion: FORMAL_PROMPT_VERSION,
      inputSha256: FORMAL_INPUT_SHA256,
    });
    const cases = [
      { name: "canonical", receipt: baseReceipt, canonical: true, expected: "runtime-real-probe-succeeded" },
      { name: "noncanonical", receipt: baseReceipt, canonical: false, expected: "runtime-real-probe-failed" },
      {
        name: "wrong-path",
        receipt: baseReceipt,
        canonical: true,
        receiptPath: "runtime/probes/other.json",
        expected: "runtime-real-probe-failed",
      },
      {
        name: "wrong-prompt",
        receipt: { ...baseReceipt, promptVersion: "other-prompt" },
        canonical: true,
        expected: "runtime-real-probe-failed",
      },
      {
        name: "wrong-model-version",
        receipt: { ...baseReceipt, model: { ...baseReceipt.model, version: "other-version" } },
        canonical: true,
        expected: "runtime-real-probe-failed",
      },
      {
        name: "wrong-fixed-input",
        receipt: { ...baseReceipt, inputSha256: "8".repeat(64) },
        canonical: true,
        expected: "runtime-real-probe-failed",
      },
      {
        name: "unknown-kind",
        receipt: { ...baseReceipt, observationKinds: ["identity"] },
        canonical: true,
        expected: "runtime-real-probe-failed",
      },
      {
        name: "duplicate-kind",
        receipt: {
          ...baseReceipt,
          observationCount: 2,
          observationKinds: ["visible-product", "visible-product"],
        },
        canonical: true,
        expected: "runtime-real-probe-failed",
      },
      {
        name: "noncanonical-kind-order",
        receipt: {
          ...baseReceipt,
          observationCount: 2,
          observationKinds: ["visible-product", "visible-person"],
        },
        canonical: true,
        expected: "runtime-real-probe-failed",
      },
      {
        name: "raw-field",
        receipt: { ...baseReceipt, rawOutput: "private model output" },
        canonical: true,
        expected: "runtime-real-probe-failed",
      },
      {
        name: "non-private-probe-directory",
        receipt: baseReceipt,
        canonical: true,
        probeDirectoryMode: 0o755,
        expected: "runtime-real-probe-failed",
      },
    ];
    const observed = [];
    for (const [index, scenario] of cases.entries()) {
      const root = roots[index];
      const receiptPath = scenario.receiptPath
        ?? "runtime/probes/synthetic-worker-runtime.json";
      const bytes = scenario.canonical
        ? `${canonicalJson(scenario.receipt)}\n`
        : JSON.stringify(scenario.receipt);
      const bundle = runtimeFixture({
        probe: {
          receiptPath,
          receiptSha256: sha256(bytes),
          runtimeTreeSha256: RUNTIME_TREE_SHA256,
          modelSha256: scenario.receipt.model.sha256,
          promptVersion: scenario.receipt.promptVersion,
          inputSha256: scenario.receipt.inputSha256,
        },
      });
      const lock = { schemaVersion: 1, artifacts: [model], workerRuntimes: [bundle] };
      await installReadyRuntime(root, bundle);
      await mkdir(dirname(join(root, receiptPath)), { recursive: true, mode: 0o700 });
      await chmod(
        join(root, "runtime", "probes"),
        scenario.probeDirectoryMode ?? 0o700,
      ).catch(() => undefined);
      await writeFile(join(root, receiptPath), bytes, { mode: 0o600 });
      await chmod(join(root, receiptPath), 0o600);
      observed.push({
        name: scenario.name,
        status: (await inspectWorkerRuntime(bundle, root, lock)).status,
      });
    }
    assert.deepEqual(observed, cases.map(({ name, expected: status }) => ({ name, status })));
  },
));

test("synchronous and asynchronous probe inspection agree on missing, malformed, and mismatched receipts", () => withTemporaryRoots(
  3,
  async (missingRoot, malformedRoot, mismatchedRoot) => {
    const receiptPath = "runtime/probes/synthetic-worker-runtime.json";
    const baseProbe = {
      receiptPath,
      receiptSha256: "0".repeat(64),
      runtimeTreeSha256: RUNTIME_TREE_SHA256,
      modelSha256: "7".repeat(64),
      promptVersion: FORMAL_PROMPT_VERSION,
      inputSha256: FORMAL_INPUT_SHA256,
    };
    const observedSyncStatus = (lockPath, root, bundle) => {
      try {
        return resolveFormalWorkerRuntimeConfig({
          lockPath,
          modelRoot: root,
          runtimeId: bundle.id,
        }).status;
      } catch (error) {
        assert.equal(error?.message, "studio-formal-worker-runtime-integrity-failed");
        return "runtime-integrity-failed";
      }
    };

    const missingBundle = runtimeFixture({ probe: baseProbe });
    await installReadyRuntime(missingRoot, missingBundle);
    const missingLockPath = await writeRuntimeLock(missingRoot, {
      schemaVersion: 1,
      artifacts: [qwenArtifactFixture()],
      workerRuntimes: [missingBundle],
    });
    assert.equal((await inspectWorkerRuntime(missingBundle, missingRoot)).status, "runtime-installed-probe-required");
    assert.equal(
      observedSyncStatus(missingLockPath, missingRoot, missingBundle),
      "runtime-installed-probe-required",
    );

    const malformedReceipt = "{}";
    const malformedBundle = runtimeFixture({
      probe: { ...baseProbe, receiptSha256: sha256(malformedReceipt) },
    });
    await installReadyRuntime(malformedRoot, malformedBundle);
    await mkdir(join(malformedRoot, "runtime", "probes"), { recursive: true });
    await writeFile(join(malformedRoot, receiptPath), malformedReceipt, { mode: 0o600 });
    const malformedLockPath = await writeRuntimeLock(malformedRoot, {
      schemaVersion: 1,
      artifacts: [qwenArtifactFixture()],
      workerRuntimes: [malformedBundle],
    });
    assert.equal(
      (await inspectWorkerRuntime(malformedBundle, malformedRoot, {
        schemaVersion: 1,
        artifacts: [qwenArtifactFixture()],
      })).status,
      "runtime-real-probe-failed",
    );
    assert.equal(observedSyncStatus(malformedLockPath, malformedRoot, malformedBundle), "runtime-real-probe-failed");

    const mismatchedReceipt = `${canonicalJson(probeReceipt({ promptVersion: "wrong-prompt" }))}\n`;
    const mismatchedBundle = runtimeFixture({
      probe: { ...baseProbe, receiptSha256: sha256(mismatchedReceipt) },
    });
    await installReadyRuntime(mismatchedRoot, mismatchedBundle);
    await mkdir(join(mismatchedRoot, "runtime", "probes"), { recursive: true });
    await writeFile(join(mismatchedRoot, receiptPath), mismatchedReceipt, { mode: 0o600 });
    const mismatchedLockPath = await writeRuntimeLock(mismatchedRoot, {
      schemaVersion: 1,
      artifacts: [qwenArtifactFixture()],
      workerRuntimes: [mismatchedBundle],
    });
    assert.equal(
      (await inspectWorkerRuntime(mismatchedBundle, mismatchedRoot, {
        schemaVersion: 1,
        artifacts: [qwenArtifactFixture()],
      })).status,
      "runtime-real-probe-failed",
    );
    assert.equal(observedSyncStatus(mismatchedLockPath, mismatchedRoot, mismatchedBundle), "runtime-real-probe-failed");
  },
));

test("runtime status succeeds only for the exact locked probe bytes and bindings", () => withTemporaryRoots(1, async (root) => {
  const receipt = `${canonicalJson(probeReceipt())}\n`;
  const receiptPath = "runtime/probes/synthetic-worker-runtime.json";
  const probe = {
    receiptPath,
    receiptSha256: sha256(receipt),
    runtimeTreeSha256: RUNTIME_TREE_SHA256,
    modelSha256: "7".repeat(64),
    promptVersion: FORMAL_PROMPT_VERSION,
    inputSha256: FORMAL_INPUT_SHA256,
  };
  const bundle = runtimeFixture({ probe });
  await installReadyRuntime(root, bundle);
  await mkdir(join(root, "runtime", "probes"), { recursive: true, mode: 0o700 });
  await chmod(join(root, "runtime", "probes"), 0o700);
  await writeFile(join(root, receiptPath), receipt, { mode: 0o600 });
  await chmod(join(root, receiptPath), 0o600);

  const lock = { schemaVersion: 1, artifacts: [qwenArtifactFixture()], workerRuntimes: [bundle] };
  const status = await inspectWorkerRuntime(bundle, root, lock);
  assert.deepEqual(status, {
    id: "synthetic-worker-runtime",
    version: "0.6.6-py312",
    status: "runtime-real-probe-succeeded",
    treeSha256: RUNTIME_TREE_SHA256,
  });
  assert.deepEqual(Object.keys(status).sort(), ["id", "status", "treeSha256", "version"]);
  assert.equal(JSON.stringify(status).includes(root), false);
  assert.equal(JSON.stringify(status).includes(bundle.approvedBy), false);

  const staleReceipt = `${canonicalJson(probeReceipt({ promptVersion: "stale-prompt" }))}\n`;
  await writeFile(join(root, receiptPath), staleReceipt);
  const staleBundle = runtimeFixture({
    probe: { ...probe, receiptSha256: sha256(staleReceipt) },
  });
  assert.equal(
    (await inspectWorkerRuntime(staleBundle, root, {
      ...lock,
      workerRuntimes: [staleBundle],
    })).status,
    "runtime-real-probe-failed",
  );
}));

test("probe receipt without a declared length is capped before its body is read", () => withTemporaryRoots(
  1,
  async (root) => {
    const receiptPath = "runtime/probes/oversized.json";
    const bundle = runtimeFixture({
      probe: {
        receiptPath,
        receiptSha256: "a".repeat(64),
        runtimeTreeSha256: RUNTIME_TREE_SHA256,
        modelSha256: "7".repeat(64),
        promptVersion: FORMAL_PROMPT_VERSION,
        inputSha256: FORMAL_INPUT_SHA256,
      },
    });
    await installReadyRuntime(root, bundle);
    const receiptAbsolutePath = join(root, receiptPath);
    await mkdir(dirname(receiptAbsolutePath), { recursive: true });
    await writeFile(receiptAbsolutePath, "{}");
    await truncate(receiptAbsolutePath, MAX_WORKER_RUNTIME_PROBE_RECEIPT_BYTES + 1);

    const observed = await observeTargetBodyReads(
      receiptAbsolutePath,
      () => inspectWorkerRuntime(bundle, root),
    );
    assert.equal(observed.result.status, "runtime-real-probe-failed");
    assert.equal(observed.bodyReads, 0);
  },
));

test("runtime inspection fails closed when its parent path is swapped at the read boundary", () => withTemporaryRoots(
  2,
  async (root, externalRoot) => {
    const bundle = runtimeFixture();
    await installReadyRuntime(root, bundle);
    await installReadyRuntime(externalRoot, bundle);
    const runtimeRoot = join(root, "runtime", bundle.id);
    const originalRuntimeRoot = join(root, "runtime", `${bundle.id}-original`);
    const externalRuntimeRoot = join(externalRoot, "runtime", bundle.id);

    const probeHandle = await open(
      join(runtimeRoot, bundle.manifest.relativePath),
      constants.O_RDONLY,
    );
    const fileHandlePrototype = Object.getPrototypeOf(probeHandle);
    await probeHandle.close();
    const originalRead = fileHandlePrototype.read;
    let swapped = false;
    fileHandlePrototype.read = async function stableReadBoundaryAttack(...arguments_) {
      if (!swapped) {
        swapped = true;
        await rename(runtimeRoot, originalRuntimeRoot);
        await symlink(externalRuntimeRoot, runtimeRoot);
      }
      return originalRead.apply(this, arguments_);
    };
    try {
      assert.equal((await inspectWorkerRuntime(bundle, root)).status, "runtime-integrity-failed");
      assert.equal(swapped, true);
    } finally {
      fileHandlePrototype.read = originalRead;
    }
  },
));
