import {runtimeProfileFor,runtimeProfileInventoryMatches} from "./studio-runtime-profiles.mjs";
import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readSync,
  readdirSync,
} from "node:fs";
import { lstat, open, readdir } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";

import { datetimeRegex } from "zod";

import { artifactApprovalIssue, artifactManifest } from "./studio-model-registry.mjs";
import {
  compareRuntimePathsByUtf8Bytes,
  isValidRuntimePackageInventory,
  isPortableRuntimeRelativePath,
  parseRuntimeJsonBytes,
  runtimePathCollisionKey,
} from "./studio-runtime-canonical.mjs";

export const APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256 =
  "774b30f7f04e3d78faf72f75fca0d0b5823f8244d751c6c10f9c97f45fd85521";
export const APPROVED_WORKER_RUNTIME_WORKER_DEPENDENCY_LOCK_SHA256 =
  "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041";
export const MAX_WORKER_RUNTIME_PROBE_RECEIPT_BYTES = 64 * 1024;
export const FORMAL_QWEN_PROBE_MODEL_ID = "qwen3-vl-2b-instruct-4bit";
export const FORMAL_WORKER_RUNTIME_ID =
  "mlx-vlm-worker-runtime-0.6.6-pyscenedetect-0.7.1-py312-darwin-arm64";
export const FORMAL_QWEN_PROBE_PROMPT_VERSION = "studio-qwen3-vl-zh-json-v5";
export const FORMAL_QWEN_PROBE_INPUT_SHA256 =
  "1ca0727bf07eb75b2d54a7060fb06b7e86afca54cdb763f47883d292b89b873a";
export const FORMAL_QWEN_PROBE_OBSERVATION_KINDS = Object.freeze([
  "visible-person",
  "visible-product",
  "visible-action",
  "visible-scene",
]);

const digestPattern = /^[a-f0-9]{64}$/u;
const pythonVersionPattern = /^3\.12\.\d+$/u;
const approvalPacketIdPattern = /^worker-runtime-bundle-v1-sha256-[a-f0-9]{64}$/u;
const materialPacketIdPattern = /^worker-runtime-materials-v1-sha256-([a-f0-9]{64})$/u;
const safeIdentityPattern = /^[A-Za-z0-9._-]{1,200}$/u;
const approvalTimePattern = datetimeRegex({ precision: null, offset: false, local: false });
const stableReadChunkBytes = 64 * 1024;
const formalLauncherRelativePath = "bin/run-offline-worker";
const bundleFields = new Set([
  "schemaVersion",
  "id",
  "version",
  "pythonVersion",
  "platform",
  "architecture",
  "minimumOsVersion",
  "dependencyLockSha256",
  "workerDependencyLockSha256",
  "materialInventorySha256",
  "approvalPacketId",
  "archive",
  "manifest",
  "packageInventory",
  "derivationReceipt",
  "treeSha256",
  "licenseReview",
  "enabled",
  "approved",
  "approvedBy",
  "approvedAt",
  "probe",
]);
const sidecarFields = new Set(["relativePath", "sha256", "byteLength"]);
const probeBindingFields = new Set([
  "receiptPath",
  "receiptSha256",
  "runtimeTreeSha256",
  "modelSha256",
  "promptVersion",
  "inputSha256",
]);

function exactFields(value, expected) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).length === expected.size
    && Object.keys(value).every((key) => expected.has(key));
}

function nonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function safeIdentity(value) {
  return typeof value === "string"
    && value !== "."
    && value !== ".."
    && safeIdentityPattern.test(value);
}

function validApprovalTime(value) {
  return typeof value === "string" && approvalTimePattern.test(value);
}

function validRelativePath(value) {
  return isPortableRuntimeRelativePath(value);
}

function validSidecar(sidecar) {
  return exactFields(sidecar, sidecarFields)
    && validRelativePath(sidecar.relativePath)
    && digestPattern.test(sidecar.sha256)
    && Number.isSafeInteger(sidecar.byteLength)
    && sidecar.byteLength > 0;
}

function validProbeBinding(probe) {
  return exactFields(probe, probeBindingFields)
    && validRelativePath(probe.receiptPath)
    && digestPattern.test(probe.receiptSha256)
    && digestPattern.test(probe.runtimeTreeSha256)
    && digestPattern.test(probe.modelSha256)
    && nonEmptyString(probe.promptVersion)
    && digestPattern.test(probe.inputSha256);
}

function validBundleMetadata(bundle) {
  if (!exactFields(bundle, Object.hasOwn(bundle ?? {}, "runtimeProfileId") ? new Set([...bundleFields,"runtimeProfileId"]) : bundleFields)) return false;
  if (
    bundle.schemaVersion !== 1
    || !nonEmptyString(bundle.id)
    || !nonEmptyString(bundle.version)
    || !pythonVersionPattern.test(bundle.pythonVersion)
    || bundle.platform !== "darwin"
    || bundle.architecture !== "arm64"
    || !nonEmptyString(bundle.minimumOsVersion)
    || !digestPattern.test(bundle.dependencyLockSha256)
    || !digestPattern.test(bundle.workerDependencyLockSha256)
    || !digestPattern.test(bundle.materialInventorySha256)
    || !approvalPacketIdPattern.test(bundle.approvalPacketId)
    || !digestPattern.test(bundle.treeSha256)
    || !["pending", "approved", "rejected"].includes(bundle.licenseReview)
    || typeof bundle.enabled !== "boolean"
    || typeof bundle.approved !== "boolean"
    || !(bundle.approvedBy === null || nonEmptyString(bundle.approvedBy))
    || !(bundle.approvedAt === null || validApprovalTime(bundle.approvedAt))
    || !validSidecar(bundle.archive)
    || !validSidecar(bundle.manifest)
    || !validSidecar(bundle.packageInventory)
    || !validSidecar(bundle.derivationReceipt)
    || !(bundle.probe === null || validProbeBinding(bundle.probe))
  ) return false;
  const paths = [
    bundle.archive.relativePath,
    bundle.manifest.relativePath,
    bundle.packageInventory.relativePath,
    bundle.derivationReceipt.relativePath,
  ];
  return new Set(paths).size === paths.length;
}

export function findWorkerRuntime(lock, runtimeId) {
  const runtimes = lock?.workerRuntimes ?? [];
  if (!Array.isArray(runtimes)) {
    throw new Error("studio-worker-runtime-registry-lock-invalid");
  }
  const matches = runtimes.filter((runtime) => runtime?.id === runtimeId);
  if (matches.length !== 1) {
    throw new Error("studio-worker-runtime-registry-runtime-unknown");
  }
  return matches[0];
}

export function workerRuntimeApprovalIssue(bundle) {
  if (!validBundleMetadata(bundle)) return "lock-invalid";
  const profile=runtimeProfileFor(bundle, APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256, APPROVED_WORKER_RUNTIME_WORKER_DEPENDENCY_LOCK_SHA256);
  if (profile === null) return "profile-mismatch";
  if (bundle.dependencyLockSha256 !== profile.dependencyLockSha256) {
    return "lock-mismatch";
  }
  if (
    bundle.workerDependencyLockSha256
    !== profile.workerDependencyLockSha256
  ) {
    return "worker-lock-mismatch";
  }
  if (bundle.probe !== null && bundle.probe.runtimeTreeSha256 !== bundle.treeSha256) {
    return "probe-binding-mismatch";
  }
  if (
    bundle.approved !== true
    || bundle.enabled !== true
    || bundle.licenseReview !== "approved"
    || !nonEmptyString(bundle.approvedBy)
    || !validApprovalTime(bundle.approvedAt)
  ) return "runtime-unapproved";
  return null;
}

function safePath(root, relativePath) {
  const resolvedRoot = resolve(root);
  const path = resolve(resolvedRoot, relativePath);
  const fromRoot = relative(resolvedRoot, path);
  if (fromRoot === ".." || fromRoot.startsWith(`..${sep}`) || isAbsolute(fromRoot)) {
    throw new Error("studio-worker-runtime-registry-path-unsafe");
  }
  return path;
}

async function lstatIfPresent(path) {
  try {
    return await lstat(path);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function lstatBigIntIfPresent(path) {
  try {
    return await lstat(path, { bigint: true });
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

function sameIdentity(left, right) {
  return left !== null
    && right !== null
    && left.dev === right.dev
    && left.ino === right.ino;
}

function sameStableFile(left, right) {
  return sameIdentity(left, right)
    && left.mode === right.mode
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs
    && left.ctimeNs === right.ctimeNs;
}

function sameStableDirectory(left, right) {
  return sameIdentity(left, right)
    && left.mode === right.mode
    && left.mtimeNs === right.mtimeNs
    && left.ctimeNs === right.ctimeNs;
}

function currentUserId() {
  const uid = typeof process.geteuid === "function" ? process.geteuid() : process.getuid?.();
  if (!Number.isSafeInteger(uid) || uid < 0) {
    throw new Error("studio-formal-worker-runtime-integrity-failed");
  }
  return BigInt(uid);
}

function lstatBigIntSyncIfPresent(path) {
  try {
    return lstatSync(path, { bigint: true });
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

function readExactBytesSync(fileDescriptor, byteLength) {
  const bytes = Buffer.allocUnsafe(byteLength);
  let offset = 0;
  while (offset < byteLength) {
    const length = Math.min(byteLength - offset, stableReadChunkBytes);
    const bytesRead = readSync(fileDescriptor, bytes, offset, length, offset);
    if (bytesRead === 0) throw new Error("studio-formal-worker-runtime-integrity-failed");
    offset += bytesRead;
  }
  return bytes;
}

function readStableAbsoluteFileSync(path, limits, options = {}) {
  const parentPath = dirname(path);
  const parentBefore = lstatBigIntSyncIfPresent(parentPath);
  const before = lstatBigIntSyncIfPresent(path);
  if (
    parentBefore === null
    || parentBefore.isSymbolicLink()
    || !parentBefore.isDirectory()
    || before === null
    || before.isSymbolicLink()
    || !before.isFile()
    || before.nlink !== 1n
  ) throw new Error(options.errorCode ?? "studio-formal-worker-runtime-integrity-failed");
  const uid = currentUserId();
  if (before.uid !== uid || (options.expectedMode !== undefined
    && (before.mode & 0o7777n) !== BigInt(options.expectedMode))) {
    throw new Error(options.errorCode ?? "studio-formal-worker-runtime-integrity-failed");
  }

  let fileDescriptor;
  try {
    fileDescriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const opened = fstatSync(fileDescriptor, { bigint: true });
    if (
      !opened.isFile()
      || opened.nlink !== 1n
      || opened.uid !== uid
      || !sameStableFile(before, opened)
    ) throw new Error(options.errorCode ?? "studio-formal-worker-runtime-integrity-failed");
    const byteLength = validatedReadLength(opened, limits);
    const bytes = readExactBytesSync(fileDescriptor, byteLength);
    const afterRead = fstatSync(fileDescriptor, { bigint: true });
    const afterPath = lstatBigIntSyncIfPresent(path);
    const parentAfter = lstatBigIntSyncIfPresent(parentPath);
    if (
      !sameStableFile(opened, afterRead)
      || afterPath === null
      || afterPath.isSymbolicLink()
      || !sameStableFile(opened, afterPath)
      || parentAfter === null
      || parentAfter.isSymbolicLink()
      || !sameStableDirectory(parentBefore, parentAfter)
    ) throw new Error(options.errorCode ?? "studio-formal-worker-runtime-integrity-failed");
    return { bytes, details: opened };
  } finally {
    if (fileDescriptor !== undefined) closeSync(fileDescriptor);
  }
}

function readFormalLockSync(lockPath) {
  let inspected;
  try {
    inspected = readStableAbsoluteFileSync(resolve(lockPath), {
      maximumByteLength: 16 * 1024 * 1024,
    }, { errorCode: "studio-formal-worker-runtime-lock-unsafe" });
  } catch (error) {
    if (error?.message === "studio-formal-worker-runtime-lock-unsafe") throw error;
    throw new Error("studio-formal-worker-runtime-lock-invalid");
  }
  const mode = inspected.details.mode & 0o7777n;
  if (mode !== 0o600n && mode !== 0o644n) {
    throw new Error("studio-formal-worker-runtime-lock-unsafe");
  }
  let lock;
  try {
    lock = parseRuntimeJsonBytes(inspected.bytes);
  } catch {
    throw new Error("studio-formal-worker-runtime-lock-invalid");
  }
  if (
    lock === null
    || typeof lock !== "object"
    || Array.isArray(lock)
    || !Object.keys(lock).every((key) => ["schemaVersion", "artifacts", "workerRuntimes"].includes(key))
    || lock.schemaVersion !== 1
    || !Array.isArray(lock.artifacts)
    || !(lock.workerRuntimes === undefined || Array.isArray(lock.workerRuntimes))
  ) throw new Error("studio-formal-worker-runtime-lock-invalid");
  return lock;
}

async function snapshotParentChain(root, relativePath) {
  const resolvedRoot = resolve(root);
  const destination = safePath(resolvedRoot, relativePath);
  const parent = dirname(destination);
  const parentParts = relative(resolvedRoot, parent).split(sep).filter(Boolean);
  const snapshots = [];
  let current = resolvedRoot;
  for (const part of [null, ...parentParts]) {
    if (part !== null) current = resolve(current, part);
    const details = await lstatBigIntIfPresent(current);
    if (details === null) return { destination, missing: true, snapshots };
    if (details.isSymbolicLink() || !details.isDirectory()) {
      throw new Error("studio-worker-runtime-registry-path-unsafe");
    }
    snapshots.push({ path: current, details });
  }
  return { destination, missing: false, snapshots };
}

async function revalidateDirectorySnapshots(snapshots) {
  for (const snapshot of snapshots) {
    const current = await lstatBigIntIfPresent(snapshot.path);
    if (
      current === null
      || current.isSymbolicLink()
      || !current.isDirectory()
      || !sameStableDirectory(snapshot.details, current)
    ) return false;
  }
  return true;
}

function validatedReadLength(opened, { expectedByteLength, maximumByteLength }) {
  if (expectedByteLength !== undefined) {
    if (!Number.isSafeInteger(expectedByteLength) || expectedByteLength < 0) {
      throw new Error("studio-worker-runtime-registry-read-limit-invalid");
    }
    if (opened.size !== BigInt(expectedByteLength)) {
      throw new Error("studio-worker-runtime-registry-size-mismatch");
    }
    return expectedByteLength;
  }
  if (!Number.isSafeInteger(maximumByteLength) || maximumByteLength < 0) {
    throw new Error("studio-worker-runtime-registry-read-limit-invalid");
  }
  if (opened.size > BigInt(maximumByteLength)) {
    throw new Error("studio-worker-runtime-registry-size-limit-exceeded");
  }
  return Number(opened.size);
}

async function readExactBytes(handle, byteLength) {
  const bytes = Buffer.allocUnsafe(byteLength);
  let offset = 0;
  while (offset < byteLength) {
    const length = Math.min(byteLength - offset, stableReadChunkBytes);
    const result = await handle.read(bytes, offset, length, offset);
    if (result.bytesRead === 0) {
      throw new Error("studio-worker-runtime-registry-file-truncated");
    }
    offset += result.bytesRead;
  }
  return bytes;
}

async function readStableFile(root, relativePath, limits) {
  const parents = await snapshotParentChain(root, relativePath);
  if (parents.missing) return null;
  const before = await lstatBigIntIfPresent(parents.destination);
  if (before === null) return null;
  if (before.isSymbolicLink() || !before.isFile() || before.nlink !== 1n) {
    throw new Error("studio-worker-runtime-registry-path-unsafe");
  }

  let handle;
  try {
    handle = await open(parents.destination, constants.O_RDONLY | constants.O_NOFOLLOW);
    const opened = await handle.stat({ bigint: true });
    if (!opened.isFile() || opened.nlink !== 1n || !sameStableFile(before, opened)) {
      throw new Error("studio-worker-runtime-registry-path-unsafe");
    }
    const byteLength = validatedReadLength(opened, limits);
    const bytes = await readExactBytes(handle, byteLength);
    const afterRead = await handle.stat({ bigint: true });
    if (!sameStableFile(opened, afterRead)) {
      throw new Error("studio-worker-runtime-registry-path-unsafe");
    }
    const afterPath = await lstatBigIntIfPresent(parents.destination);
    if (
      afterPath === null
      || afterPath.isSymbolicLink()
      || !afterPath.isFile()
      || afterPath.nlink !== 1n
      || !sameStableFile(opened, afterPath)
      || !await revalidateDirectorySnapshots(parents.snapshots)
    ) throw new Error("studio-worker-runtime-registry-path-unsafe");
    return { bytes, details: opened };
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

async function inspectContainedPath(root, relativePath) {
  const resolvedRoot = resolve(root);
  const destination = safePath(resolvedRoot, relativePath);
  let current = resolvedRoot;
  let details = await lstatIfPresent(current);
  if (details === null) return { destination, details: null };
  if (details.isSymbolicLink() || !details.isDirectory()) {
    throw new Error("studio-worker-runtime-registry-path-unsafe");
  }
  const pathParts = relative(resolvedRoot, destination).split(sep).filter(Boolean);
  for (const [index, part] of pathParts.entries()) {
    current = resolve(current, part);
    details = await lstatIfPresent(current);
    if (details === null) return { destination, details: null };
    if (details.isSymbolicLink()) {
      throw new Error("studio-worker-runtime-registry-path-unsafe");
    }
    if (index < pathParts.length - 1 && !details.isDirectory()) {
      throw new Error("studio-worker-runtime-registry-path-unsafe");
    }
  }
  return { destination, details };
}

async function readLockedFile(root, lock, expectedMode) {
  let inspected;
  try {
    inspected = await readStableFile(root, lock.relativePath, {
      expectedByteLength: lock.byteLength,
    });
  } catch {
    return null;
  }
  if (
    inspected === null
    || inspected.details.size !== BigInt(lock.byteLength)
    || (expectedMode !== undefined && (inspected.details.mode & 0o7777n) !== BigInt(expectedMode))
  ) return null;
  if (createHash("sha256").update(inspected.bytes).digest("hex") !== lock.sha256) return null;
  return inspected;
}

const runtimeManifestFields = new Set(["schemaVersion", "files", "treeSha256"]);
const runtimeManifestFileFields = new Set([
  "relativePath", "byteLength", "sha256", "type", "executable",
]);

function computeRuntimeTreeSha256(files) {
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

function parseRuntimeManifest(bytes, bundle) {
  let manifest;
  try {
    manifest = parseRuntimeJsonBytes(bytes);
  } catch {
    return null;
  }
  if (
    !exactFields(manifest, runtimeManifestFields)
    || manifest.schemaVersion !== 1
    || !Array.isArray(manifest.files)
    || manifest.files.length === 0
    || !digestPattern.test(manifest.treeSha256)
  ) return null;
  const sidecarPaths = new Set([
    bundle.manifest.relativePath,
    bundle.packageInventory.relativePath,
    bundle.derivationReceipt.relativePath,
  ]);
  const collisionPaths = new Set();
  const directoryPaths = new Map();
  for (const file of manifest.files) {
    if (
      !exactFields(file, runtimeManifestFileFields)
      || !validRelativePath(file.relativePath)
      || sidecarPaths.has(file.relativePath)
      || !Number.isSafeInteger(file.byteLength)
      || file.byteLength < 0
      || !digestPattern.test(file.sha256)
      || file.type !== "file"
      || typeof file.executable !== "boolean"
    ) return null;
    const collisionPath = runtimePathCollisionKey(file.relativePath);
    if (collisionPaths.has(collisionPath)) return null;
    collisionPaths.add(collisionPath);
    const parts = file.relativePath.split("/");
    for (let index = 1; index < parts.length; index += 1) {
      const directoryPath = parts.slice(0, index).join("/");
      const directoryCollisionPath = runtimePathCollisionKey(directoryPath);
      const existingDirectory = directoryPaths.get(directoryCollisionPath);
      if (existingDirectory !== undefined && existingDirectory !== directoryPath) return null;
      directoryPaths.set(directoryCollisionPath, directoryPath);
    }
  }
  if ([...directoryPaths.keys()].some((path) => collisionPaths.has(path))) return null;
  if (
    manifest.treeSha256 !== bundle.treeSha256
    || computeRuntimeTreeSha256(manifest.files) !== bundle.treeSha256
  ) return null;
  return manifest;
}

function derivationReceiptMatchesMaterialInventory(bytes, bundle) {
  let receipt;
  try {
    receipt = parseRuntimeJsonBytes(bytes);
  } catch {
    return false;
  }
  const match = typeof receipt?.materialPacketId === "string"
    ? materialPacketIdPattern.exec(receipt.materialPacketId)
    : null;
  return match !== null && match[1] === bundle.materialInventorySha256;
}

function validRuntimePackageInventoryBytes(bytes, bundle) {
  try {
    const inventory=parseRuntimeJsonBytes(bytes);
    return isValidRuntimePackageInventory(inventory, {
      pythonVersion: bundle.pythonVersion,
    }) && runtimeProfileInventoryMatches(bundle,inventory);
  } catch {
    return false;
  }
}

async function walkRuntimeFiles(runtimeRoot) {
  const files = [];
  const directories = [];
  const visit = async (directoryPath, directoryRelativePath) => {
    const before = await lstatBigIntIfPresent(directoryPath);
    if (before === null || before.isSymbolicLink() || !before.isDirectory()) {
      throw new Error("studio-worker-runtime-registry-path-unsafe");
    }
    if (before.nlink < 1n) throw new Error("studio-worker-runtime-registry-path-unsafe");
    directories.push({
      path: directoryPath,
      relativePath: directoryRelativePath,
      details: before,
    });
    const entries = await readdir(directoryPath, { withFileTypes: true });
    for (const entry of entries) {
      const entryRelativePath = directoryRelativePath === ""
        ? entry.name
        : `${directoryRelativePath}/${entry.name}`;
      if (!validRelativePath(entryRelativePath)) {
        throw new Error("studio-worker-runtime-registry-path-unsafe");
      }
    }
    entries.sort((left, right) => compareRuntimePathsByUtf8Bytes(left.name, right.name));
    for (const entry of entries) {
      const entryRelativePath = directoryRelativePath === ""
        ? entry.name
        : `${directoryRelativePath}/${entry.name}`;
      if (!validRelativePath(entryRelativePath)) {
        throw new Error("studio-worker-runtime-registry-path-unsafe");
      }
      const entryPath = resolve(directoryPath, entry.name);
      const details = await lstatBigIntIfPresent(entryPath);
      if (details === null || details.isSymbolicLink()) {
        throw new Error("studio-worker-runtime-registry-path-unsafe");
      }
      if (details.isDirectory()) await visit(entryPath, entryRelativePath);
      else if (details.isFile()) files.push(entryRelativePath);
      else throw new Error("studio-worker-runtime-registry-path-unsafe");
    }
    const after = await lstatBigIntIfPresent(directoryPath);
    if (after === null || !sameStableDirectory(before, after)) {
      throw new Error("studio-worker-runtime-registry-path-unsafe");
    }
  };
  await visit(runtimeRoot, "");
  return { files, directories };
}

async function verifyRuntimeTree(runtimeRoot, bundle, manifest) {
  const sidecarPaths = [
    bundle.manifest.relativePath,
    bundle.packageInventory.relativePath,
    bundle.derivationReceipt.relativePath,
  ];
  let walked;
  try {
    walked = await walkRuntimeFiles(runtimeRoot);
  } catch {
    return false;
  }
  const expectedPaths = [...manifest.files.map((file) => file.relativePath), ...sidecarPaths]
    .sort(compareRuntimePathsByUtf8Bytes);
  const actualPaths = walked.files
    .sort(compareRuntimePathsByUtf8Bytes);
  if (
    actualPaths.length !== expectedPaths.length
    || actualPaths.some((path, index) => path !== expectedPaths[index])
  ) return false;

  const expectedDirectories = new Set([""]);
  for (const path of expectedPaths) {
    const parts = path.split("/");
    for (let index = 1; index < parts.length; index += 1) {
      expectedDirectories.add(parts.slice(0, index).join("/"));
    }
  }
  if (
    walked.directories.length !== expectedDirectories.size
    || walked.directories.some(({ relativePath, details }) => (
      !expectedDirectories.has(relativePath)
      || (details.mode & 0o7777n) !== BigInt(relativePath === "" ? 0o700 : 0o755)
    ))
  ) return false;

  const observedFiles = [];
  for (const file of manifest.files) {
    let inspected;
    try {
      inspected = await readStableFile(runtimeRoot, file.relativePath, {
        expectedByteLength: file.byteLength,
      });
    } catch {
      return false;
    }
    if (inspected === null) return false;
    const actualDigest = createHash("sha256").update(inspected.bytes).digest("hex");
    const executable = (inspected.details.mode & 0o111n) !== 0n;
    const expectedMode = BigInt(file.executable ? 0o755 : 0o644);
    if (
      inspected.details.size !== BigInt(file.byteLength)
      || actualDigest !== file.sha256
      || executable !== file.executable
      || (inspected.details.mode & 0o7777n) !== expectedMode
    ) return false;
    observedFiles.push({
      relativePath: file.relativePath,
      byteLength: Number(inspected.details.size),
      sha256: actualDigest,
      type: "file",
      executable,
    });
  }
  return computeRuntimeTreeSha256(observedFiles) === bundle.treeSha256
    && await revalidateDirectorySnapshots(walked.directories);
}

function inspectPrivateRuntimeRootSync(root, relativePath) {
  const resolvedRoot = resolve(root);
  const destination = safePath(resolvedRoot, relativePath);
  const uid = currentUserId();
  let current = resolvedRoot;
  let details = lstatBigIntSyncIfPresent(current);
  if (details === null) return { destination, details: null };
  const parts = relative(resolvedRoot, destination).split(sep).filter(Boolean);
  for (const [index, part] of [null, ...parts].entries()) {
    if (part !== null) {
      current = resolve(current, part);
      details = lstatBigIntSyncIfPresent(current);
    }
    if (details === null) return { destination, details: null };
    const expectedMode = index <= 2 ? 0o700n : 0o755n;
    if (
      details.isSymbolicLink()
      || !details.isDirectory()
      || details.uid !== uid
      || (details.mode & 0o7777n) !== expectedMode
    ) throw new Error("studio-formal-worker-runtime-integrity-failed");
  }
  return { destination, details };
}

function inspectContainedPathSync(root, relativePath) {
  const resolvedRoot = resolve(root);
  const destination = safePath(resolvedRoot, relativePath);
  let current = resolvedRoot;
  let details = lstatBigIntSyncIfPresent(current);
  if (details === null) return { destination, details: null };
  if (details.isSymbolicLink() || !details.isDirectory()) {
    throw new Error("studio-formal-worker-runtime-integrity-failed");
  }
  const parts = relative(resolvedRoot, destination).split(sep).filter(Boolean);
  for (const [index, part] of parts.entries()) {
    current = resolve(current, part);
    details = lstatBigIntSyncIfPresent(current);
    if (details === null) return { destination, details: null };
    if (
      details.isSymbolicLink()
      || (index < parts.length - 1 && !details.isDirectory())
    ) throw new Error("studio-formal-worker-runtime-integrity-failed");
  }
  return { destination, details };
}

function readLockedFileSync(root, lock, expectedMode) {
  try {
    const inspected = readStableAbsoluteFileSync(
      safePath(root, lock.relativePath),
      { expectedByteLength: lock.byteLength },
      { expectedMode },
    );
    if (createHash("sha256").update(inspected.bytes).digest("hex") !== lock.sha256) return null;
    return inspected;
  } catch {
    return null;
  }
}

function walkRuntimeFilesSync(runtimeRoot) {
  const files = [];
  const directories = [];
  const uid = currentUserId();
  const visit = (directoryPath, directoryRelativePath) => {
    const before = lstatBigIntSyncIfPresent(directoryPath);
    const expectedMode = directoryRelativePath === "" ? 0o700n : 0o755n;
    if (
      before === null
      || before.isSymbolicLink()
      || !before.isDirectory()
      || before.uid !== uid
      || (before.mode & 0o7777n) !== expectedMode
    ) throw new Error("studio-formal-worker-runtime-integrity-failed");
    directories.push({ path: directoryPath, relativePath: directoryRelativePath, details: before });
    const entries = readdirSync(directoryPath, { withFileTypes: true });
    for (const entry of entries) {
      const entryRelativePath = directoryRelativePath === ""
        ? entry.name
        : `${directoryRelativePath}/${entry.name}`;
      if (!validRelativePath(entryRelativePath)) {
        throw new Error("studio-formal-worker-runtime-integrity-failed");
      }
    }
    entries.sort((left, right) => compareRuntimePathsByUtf8Bytes(left.name, right.name));
    for (const entry of entries) {
      const entryRelativePath = directoryRelativePath === ""
        ? entry.name
        : `${directoryRelativePath}/${entry.name}`;
      if (!validRelativePath(entryRelativePath)) {
        throw new Error("studio-formal-worker-runtime-integrity-failed");
      }
      const entryPath = resolve(directoryPath, entry.name);
      const details = lstatBigIntSyncIfPresent(entryPath);
      if (details === null || details.isSymbolicLink() || details.uid !== uid) {
        throw new Error("studio-formal-worker-runtime-integrity-failed");
      }
      if (details.isDirectory()) visit(entryPath, entryRelativePath);
      else if (details.isFile()) files.push(entryRelativePath);
      else throw new Error("studio-formal-worker-runtime-integrity-failed");
    }
    const after = lstatBigIntSyncIfPresent(directoryPath);
    if (after === null || !sameStableDirectory(before, after)) {
      throw new Error("studio-formal-worker-runtime-integrity-failed");
    }
  };
  visit(runtimeRoot, "");
  return { files, directories };
}

function verifyRuntimeTreeSync(runtimeRoot, bundle, manifest) {
  const sidecarPaths = [
    bundle.manifest.relativePath,
    bundle.packageInventory.relativePath,
    bundle.derivationReceipt.relativePath,
  ];
  let walked;
  try {
    walked = walkRuntimeFilesSync(runtimeRoot);
  } catch {
    return false;
  }
  const expectedPaths = [...manifest.files.map((file) => file.relativePath), ...sidecarPaths]
    .sort(compareRuntimePathsByUtf8Bytes);
  const actualPaths = walked.files.sort(compareRuntimePathsByUtf8Bytes);
  if (
    actualPaths.length !== expectedPaths.length
    || actualPaths.some((path, index) => path !== expectedPaths[index])
  ) return false;

  const expectedDirectories = new Set([""]);
  for (const path of expectedPaths) {
    const parts = path.split("/");
    for (let index = 1; index < parts.length; index += 1) {
      expectedDirectories.add(parts.slice(0, index).join("/"));
    }
  }
  if (
    walked.directories.length !== expectedDirectories.size
    || walked.directories.some(({ relativePath }) => !expectedDirectories.has(relativePath))
  ) return false;

  const observedFiles = [];
  for (const file of manifest.files) {
    let inspected;
    try {
      inspected = readStableAbsoluteFileSync(
        safePath(runtimeRoot, file.relativePath),
        { expectedByteLength: file.byteLength },
        { expectedMode: file.executable ? 0o755 : 0o644 },
      );
    } catch {
      return false;
    }
    const actualDigest = createHash("sha256").update(inspected.bytes).digest("hex");
    if (actualDigest !== file.sha256) return false;
    observedFiles.push({
      relativePath: file.relativePath,
      byteLength: Number(inspected.details.size),
      sha256: actualDigest,
      type: "file",
      executable: file.executable,
    });
  }
  for (const { path, details } of walked.directories) {
    const current = lstatBigIntSyncIfPresent(path);
    if (current === null || !sameStableDirectory(details, current)) return false;
  }
  return computeRuntimeTreeSha256(observedFiles) === bundle.treeSha256;
}

const probeReceiptFields = new Set([
  "schemaVersion", "runtime", "model", "promptVersion", "inputSha256", "outputSha256",
  "observationCount", "observationKinds", "runtimeMs", "maximumResidentSetBytes",
  "syntheticInput", "customerContentUsed", "realModel", "formalWorkerPath",
  "networkDenied", "providerMode", "publishedClaimed",
]);
const probeReceiptRuntimeFields = new Set(["id", "version", "treeSha256"]);
const probeReceiptModelFields = new Set(["id", "version", "sha256"]);

export function canonicalWorkerRuntimeProbeJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalWorkerRuntimeProbeJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalWorkerRuntimeProbeJson(value[key])}`
    )).join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new Error("studio-worker-runtime-probe-canonical-json-invalid");
  return encoded;
}

export function formalQwenProbeModelFromLock(lock) {
  const matches = Array.isArray(lock?.artifacts)
    ? lock.artifacts.filter((artifact) => artifact?.id === FORMAL_QWEN_PROBE_MODEL_ID)
    : [];
  if (matches.length !== 1) return null;
  const artifact = matches[0];
  if (artifactApprovalIssue(artifact) !== null || !safeIdentity(artifact.version)) return null;
  let files;
  try {
    files = artifactManifest(artifact).filter((file) => (
      file.relativePath === `${FORMAL_QWEN_PROBE_MODEL_ID}/model.safetensors`
    ));
  } catch {
    return null;
  }
  if (files.length !== 1 || !digestPattern.test(files[0].sha256)) return null;
  return Object.freeze({
    id: FORMAL_QWEN_PROBE_MODEL_ID,
    version: artifact.version,
    sha256: files[0].sha256,
  });
}

export function parseFormalWorkerRuntimeProbeReceipt(receipt, expected = {}) {
  if (
    !exactFields(receipt, probeReceiptFields)
    || receipt.schemaVersion !== 1
    || !exactFields(receipt.runtime, probeReceiptRuntimeFields)
    || !safeIdentity(receipt.runtime.id)
    || !safeIdentity(receipt.runtime.version)
    || !digestPattern.test(receipt.runtime.treeSha256)
    || !exactFields(receipt.model, probeReceiptModelFields)
    || receipt.model.id !== FORMAL_QWEN_PROBE_MODEL_ID
    || !safeIdentity(receipt.model.version)
    || !digestPattern.test(receipt.model.sha256)
    || receipt.promptVersion !== FORMAL_QWEN_PROBE_PROMPT_VERSION
    || receipt.inputSha256 !== FORMAL_QWEN_PROBE_INPUT_SHA256
    || !digestPattern.test(receipt.outputSha256)
    || !Number.isSafeInteger(receipt.observationCount)
    || receipt.observationCount <= 0
    || !Array.isArray(receipt.observationKinds)
    || receipt.observationKinds.length === 0
    || receipt.observationKinds.length > receipt.observationCount
    || !Number.isSafeInteger(receipt.runtimeMs)
    || receipt.runtimeMs < 0
    || !Number.isSafeInteger(receipt.maximumResidentSetBytes)
    || receipt.maximumResidentSetBytes < 0
    || receipt.syntheticInput !== true
    || receipt.customerContentUsed !== false
    || receipt.realModel !== true
    || receipt.formalWorkerPath !== true
    || receipt.networkDenied !== true
    || receipt.providerMode !== "local"
    || receipt.publishedClaimed !== false
  ) return null;
  let previousKind = -1;
  for (const kind of receipt.observationKinds) {
    const index = FORMAL_QWEN_PROBE_OBSERVATION_KINDS.indexOf(kind);
    if (index <= previousKind) return null;
    previousKind = index;
  }
  const runtime = expected.runtime;
  const model = expected.model;
  if (
    runtime !== undefined
    && (
      receipt.runtime.id !== runtime.id
      || receipt.runtime.version !== runtime.version
      || receipt.runtime.treeSha256 !== runtime.treeSha256
    )
  ) return null;
  if (
    model !== undefined
    && (
      receipt.model.id !== model.id
      || receipt.model.version !== model.version
      || receipt.model.sha256 !== model.sha256
    )
  ) return null;
  return receipt;
}

export function parseCanonicalFormalWorkerRuntimeProbeReceipt(bytes, expected = {}) {
  if (!(bytes instanceof Uint8Array) || bytes.byteLength === 0) return null;
  let parsed;
  try {
    parsed = parseRuntimeJsonBytes(Buffer.from(bytes));
  } catch {
    return null;
  }
  const receipt = parseFormalWorkerRuntimeProbeReceipt(parsed, expected);
  if (receipt === null) return null;
  const canonical = Buffer.from(`${canonicalWorkerRuntimeProbeJson(receipt)}\n`);
  return canonical.equals(Buffer.from(bytes)) ? receipt : null;
}

function safeStatus(bundle, status) {
  return Object.freeze({
    id: bundle?.id,
    version: bundle?.version,
    ...(bundle?.runtimeProfileId ? {runtimeProfileId:bundle.runtimeProfileId} : {}),
    status,
    treeSha256: bundle?.treeSha256,
  });
}

async function privateProbeDirectoryReady(root) {
  const uid = currentUserId();
  try {
    for (const relativePath of [".", "runtime", "runtime/probes"]) {
      const { details } = await inspectContainedPath(root, relativePath);
      if (
        details === null
        || details.isSymbolicLink()
        || !details.isDirectory()
        || BigInt(details.uid) !== uid
        || BigInt(details.mode & 0o7777) !== 0o700n
      ) return false;
    }
    return true;
  } catch {
    return false;
  }
}

function privateProbeDirectoryReadySync(root) {
  const uid = currentUserId();
  try {
    for (const relativePath of [".", "runtime", "runtime/probes"]) {
      const { details } = inspectContainedPathSync(root, relativePath);
      if (
        details === null
        || details.isSymbolicLink()
        || !details.isDirectory()
        || details.uid !== uid
        || (details.mode & 0o7777n) !== 0o700n
      ) return false;
    }
    return true;
  } catch {
    return false;
  }
}

async function inspectProbe(bundle, root, lock) {
  if (bundle.probe === null) return "runtime-installed-probe-required";
  if (bundle.probe.receiptPath !== `runtime/probes/${bundle.id}.json`) {
    return "runtime-real-probe-failed";
  }
  let inspected;
  try {
    inspected = await readStableFile(root, bundle.probe.receiptPath, {
      maximumByteLength: MAX_WORKER_RUNTIME_PROBE_RECEIPT_BYTES,
    });
  } catch {
    return "runtime-real-probe-failed";
  }
  if (inspected === null) return "runtime-installed-probe-required";
  if (!await privateProbeDirectoryReady(root)) return "runtime-real-probe-failed";
  const model = formalQwenProbeModelFromLock(lock);
  if (model === null) return "runtime-real-probe-failed";
  if (
    inspected.details.uid !== currentUserId()
    || (inspected.details.mode & 0o7777n) !== 0o600n
  ) return "runtime-real-probe-failed";
  const { bytes } = inspected;
  if (createHash("sha256").update(bytes).digest("hex") !== bundle.probe.receiptSha256) {
    return "runtime-real-probe-failed";
  }
  const receipt = parseCanonicalFormalWorkerRuntimeProbeReceipt(bytes, {
    runtime: bundle,
    model,
  });
  if (receipt === null) return "runtime-real-probe-failed";
  if (
    receipt.runtime.treeSha256 !== bundle.probe.runtimeTreeSha256
    || receipt.model.sha256 !== bundle.probe.modelSha256
    || receipt.promptVersion !== bundle.probe.promptVersion
    || receipt.inputSha256 !== bundle.probe.inputSha256
  ) return "runtime-real-probe-failed";
  return "runtime-real-probe-succeeded";
}

function inspectProbeSync(bundle, root, lock) {
  if (bundle.probe === null) return "runtime-installed-probe-required";
  if (bundle.probe.receiptPath !== `runtime/probes/${bundle.id}.json`) {
    return "runtime-real-probe-failed";
  }
  let receiptPath;
  try {
    receiptPath = inspectContainedPathSync(root, bundle.probe.receiptPath);
  } catch {
    return "runtime-real-probe-failed";
  }
  if (receiptPath.details === null) return "runtime-installed-probe-required";
  if (!privateProbeDirectoryReadySync(root)) return "runtime-real-probe-failed";
  const model = formalQwenProbeModelFromLock(lock);
  if (model === null) return "runtime-real-probe-failed";
  let inspected;
  try {
    inspected = readStableAbsoluteFileSync(
      receiptPath.destination,
      { maximumByteLength: MAX_WORKER_RUNTIME_PROBE_RECEIPT_BYTES },
      { expectedMode: 0o600 },
    );
  } catch {
    return "runtime-real-probe-failed";
  }
  if (createHash("sha256").update(inspected.bytes).digest("hex") !== bundle.probe.receiptSha256) {
    return "runtime-real-probe-failed";
  }
  const receipt = parseCanonicalFormalWorkerRuntimeProbeReceipt(inspected.bytes, {
    runtime: bundle,
    model,
  });
  if (receipt === null) return "runtime-real-probe-failed";
  if (
    receipt.runtime.treeSha256 !== bundle.probe.runtimeTreeSha256
    || receipt.model.sha256 !== bundle.probe.modelSha256
    || receipt.promptVersion !== bundle.probe.promptVersion
    || receipt.inputSha256 !== bundle.probe.inputSha256
  ) return "runtime-real-probe-failed";
  return "runtime-real-probe-succeeded";
}

export function resolveFormalWorkerRuntimeConfig({ lockPath, modelRoot, runtimeId, runtimeProfileId }) {
  if (
    !nonEmptyString(lockPath)
    || !isAbsolute(lockPath)
    || !nonEmptyString(modelRoot)
    || !isAbsolute(modelRoot)
    || !nonEmptyString(runtimeId)
  ) throw new Error("studio-formal-worker-runtime-config-invalid");

  let lock;
  try {
    lock = readFormalLockSync(lockPath);
  } catch (error) {
    if (
      error?.message === "studio-formal-worker-runtime-lock-unsafe"
      || error?.message === "studio-formal-worker-runtime-lock-invalid"
    ) throw new Error("studio-formal-worker-runtime-integrity-failed");
    throw error;
  }
  let bundle;
  try {
    bundle = findWorkerRuntime(lock, runtimeId);
  } catch {
    throw new Error("studio-formal-worker-runtime-unavailable");
  }
  if ((bundle.runtimeProfileId !== undefined && runtimeProfileId !== bundle.runtimeProfileId) || (bundle.runtimeProfileId === undefined && runtimeProfileId !== undefined && runtimeProfileId !== "legacy")) throw new Error("studio-formal-worker-runtime-profile-mismatch");
  const approvalIssue = workerRuntimeApprovalIssue(bundle);
  if (approvalIssue === "runtime-unapproved") {
    throw new Error("studio-formal-worker-runtime-unapproved");
  }
  if (approvalIssue !== null) {
    throw new Error("studio-formal-worker-runtime-integrity-failed");
  }

  let inspectedRuntime;
  try {
    inspectedRuntime = inspectPrivateRuntimeRootSync(modelRoot, `runtime/${bundle.id}`);
  } catch {
    throw new Error("studio-formal-worker-runtime-integrity-failed");
  }
  if (inspectedRuntime.details === null) {
    throw new Error("studio-formal-worker-runtime-unavailable");
  }
  const runtimeRoot = inspectedRuntime.destination;
  const manifestSnapshot = readLockedFileSync(runtimeRoot, bundle.manifest, 0o600);
  const packageInventorySnapshot = readLockedFileSync(runtimeRoot, bundle.packageInventory, 0o600);
  const derivationReceiptSnapshot = readLockedFileSync(runtimeRoot, bundle.derivationReceipt, 0o600);
  if (
    manifestSnapshot === null
    || packageInventorySnapshot === null
    || derivationReceiptSnapshot === null
    || !validRuntimePackageInventoryBytes(packageInventorySnapshot.bytes, bundle)
    || !derivationReceiptMatchesMaterialInventory(derivationReceiptSnapshot.bytes, bundle)
  ) throw new Error("studio-formal-worker-runtime-integrity-failed");
  const manifest = parseRuntimeManifest(manifestSnapshot.bytes, bundle);
  const launcher = manifest?.files.find((file) => file.relativePath === formalLauncherRelativePath);
  if (
    manifest === null
    || launcher === undefined
    || launcher.executable !== true
    || !verifyRuntimeTreeSync(runtimeRoot, bundle, manifest)
  ) throw new Error("studio-formal-worker-runtime-integrity-failed");
  const launcherPath = safePath(runtimeRoot, formalLauncherRelativePath);
  let launcherSnapshot;
  try {
    launcherSnapshot = readStableAbsoluteFileSync(
      launcherPath,
      { expectedByteLength: launcher.byteLength },
      { expectedMode: 0o755 },
    );
  } catch {
    throw new Error("studio-formal-worker-runtime-integrity-failed");
  }
  if (createHash("sha256").update(launcherSnapshot.bytes).digest("hex") !== launcher.sha256) {
    throw new Error("studio-formal-worker-runtime-integrity-failed");
  }
  const status = inspectProbeSync(bundle, modelRoot, lock);
  let launcherAfterProbe;
  try {
    launcherAfterProbe = readStableAbsoluteFileSync(
      launcherPath,
      { expectedByteLength: launcher.byteLength },
      { expectedMode: 0o755 },
    );
  } catch {
    throw new Error("studio-formal-worker-runtime-integrity-failed");
  }
  if (
    !sameStableFile(launcherSnapshot.details, launcherAfterProbe.details)
    || createHash("sha256").update(launcherAfterProbe.bytes).digest("hex") !== launcher.sha256
  ) throw new Error("studio-formal-worker-runtime-integrity-failed");
  return Object.freeze({
    runtimeRoot,
    launcherPath,
    launcherIdentity: Object.freeze({
      device: launcherSnapshot.details.dev.toString(),
      inode: launcherSnapshot.details.ino.toString(),
      byteLength: launcher.byteLength,
      sha256: launcher.sha256,
    }),
    status,
    bundle: Object.freeze({ id: bundle.id, treeSha256: bundle.treeSha256 }),
  });
}

export async function inspectWorkerRuntime(bundle, root, lock) {
  const issue = workerRuntimeApprovalIssue(bundle);
  if (issue === "runtime-unapproved") return safeStatus(bundle, "runtime-unapproved");
  if (issue !== null) return safeStatus(bundle, "runtime-integrity-failed");

  let inspectedRuntime;
  try {
    inspectedRuntime = await inspectContainedPath(root, `runtime/${bundle.id}`);
  } catch {
    return safeStatus(bundle, "runtime-integrity-failed");
  }
  const { destination: runtimeRoot, details: runtimeDetails } = inspectedRuntime;
  if (runtimeDetails === null) return safeStatus(bundle, "runtime-not-installed");
  if (!runtimeDetails.isDirectory() || runtimeDetails.isSymbolicLink()) {
    return safeStatus(bundle, "runtime-integrity-failed");
  }

  const manifestSnapshot = await readLockedFile(runtimeRoot, bundle.manifest, 0o600);
  if (manifestSnapshot === null) return safeStatus(bundle, "runtime-integrity-failed");
  const packageInventorySnapshot = await readLockedFile(runtimeRoot, bundle.packageInventory, 0o600);
  const derivationReceiptSnapshot = await readLockedFile(runtimeRoot, bundle.derivationReceipt, 0o600);
  if (
    packageInventorySnapshot === null
    || derivationReceiptSnapshot === null
    || !validRuntimePackageInventoryBytes(packageInventorySnapshot.bytes, bundle)
    || !derivationReceiptMatchesMaterialInventory(derivationReceiptSnapshot.bytes, bundle)
  ) return safeStatus(bundle, "runtime-integrity-failed");
  const manifest = parseRuntimeManifest(manifestSnapshot.bytes, bundle);
  if (manifest === null) return safeStatus(bundle, "runtime-integrity-failed");
  const probeStatus = await inspectProbe(bundle, root, lock);
  if (!await verifyRuntimeTree(runtimeRoot, bundle, manifest)) {
    return safeStatus(bundle, "runtime-integrity-failed");
  }
  return safeStatus(bundle, probeStatus);
}
