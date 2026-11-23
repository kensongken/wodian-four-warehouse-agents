import {runtimeProfileInventoryMatches} from "./studio-runtime-profiles.mjs";
import { execFile as execFileCallback } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import {
  chmod,
  link,
  lstat,
  mkdir,
  mkdtemp,
  open,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  unlink,
} from "node:fs/promises";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { promisify } from "node:util";

import { loadModelLock } from "./studio-model-registry.mjs";
import {
  compareRuntimePathsByUtf8Bytes,
  decodeRuntimeUtf8,
  isValidRuntimePackageInventory,
  isPortableRuntimeRelativePath,
  parseRuntimeJsonBytes,
  runtimePathCollisionKey,
} from "./studio-runtime-canonical.mjs";
import {
  findWorkerRuntime,
  inspectWorkerRuntime,
  workerRuntimeApprovalIssue,
} from "./studio-worker-runtime-registry.mjs";

const execFile = promisify(execFileCallback);
const BLOCK_BYTES = 512;
const ZERO_BLOCK = Buffer.alloc(BLOCK_BYTES);
const DIGEST = /^[a-f0-9]{64}$/u;
const APPROVAL_PACKET = /^worker-runtime-bundle-v1-sha256-[a-f0-9]{64}$/u;
const MATERIAL_PACKET = /^worker-runtime-materials-v1-sha256-([a-f0-9]{64})$/u;
const SAFE_ARTIFACT_ID = /^[a-zA-Z0-9._-]{1,200}$/u;
const MAX_ARCHIVE_BYTES = 9 * 1024 * 1024 * 1024;
const MAX_MANIFEST_BYTES = 64 * 1024 * 1024;
const MAX_PACKAGE_INVENTORY_BYTES = 16 * 1024 * 1024;
const MAX_DERIVATION_RECEIPT_BYTES = 1024 * 1024;
const READ_CHUNK_BYTES = 1024 * 1024;
const MAX_INSTALL_LOCK_BYTES = 4096;
export const INSTALL_LOCK_GRACE_MS = 30_000;
export const INSTALL_LOCK_LEASE_MS = 30_000;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const SANDBOX_EXECUTABLE = "/usr/bin/sandbox-exec";
const SUPERSEDED_BUNDLE_PACKET_IDS = new Set([
  "worker-runtime-bundle-v1-sha256-679e04e90b4644c5ce951f8a07d57427b1d8845f7434526f58d2f653232bfe6f",
  "worker-runtime-bundle-v1-sha256-08b1c52b0c7fb7feb7e97b7d53ca97c65c5d100b2e10eed09cc4b384eb96797f",
  "worker-runtime-bundle-v1-sha256-45593acffe2636c68f82233e7218e043491ea4afc731c5566430480e1ed0bfaa",
]);

export const RUNTIME_EXTRACTION_LIMITS = Object.freeze({
  maxFiles: 50_000,
  maxSingleFileBytes: 512 * 1024 * 1024,
  maxTotalBytes: 8 * 1024 * 1024 * 1024,
  maxRelativePathBytes: 1024,
});

function fail(code, detail = "") {
  throw new Error(detail ? `${code}:${detail}` : code);
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256Bytes(value) {
  return createHash("sha256").update(value).digest("hex");
}

function exactFields(value, names) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).length === names.length
    && Object.keys(value).every((key) => names.includes(key));
}

function safeRelativePath(value) {
  return isPortableRuntimeRelativePath(value, {
    maximumBytes: RUNTIME_EXTRACTION_LIMITS.maxRelativePathBytes,
  });
}

function normalizedPathKey(value) {
  return runtimePathCollisionKey(value);
}

function computeTreeSha256(files) {
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

function parseJson(bytes, code) {
  try {
    return parseRuntimeJsonBytes(bytes);
  } catch {
    fail(code);
  }
}

export function parseRuntimeManifest(bytes, bundle) {
  const manifest = parseJson(bytes, "studio-runtime-manifest-digest-mismatch");
  if (
    !exactFields(manifest, ["schemaVersion", "files", "treeSha256"])
    || manifest.schemaVersion !== 1
    || !Array.isArray(manifest.files)
    || manifest.files.length === 0
    || !DIGEST.test(manifest.treeSha256)
  ) fail("studio-runtime-tree-mismatch");
  if (manifest.files.length > RUNTIME_EXTRACTION_LIMITS.maxFiles) {
    fail("studio-runtime-extraction-limit-exceeded");
  }
  const exactPaths = new Set();
  const normalizedPaths = new Set();
  const directoryPaths = new Set();
  const normalizedDirectoryPaths = new Map();
  let totalBytes = 0;
  for (const file of manifest.files) {
    if (typeof file?.relativePath === "string" && !safeRelativePath(file.relativePath)) {
      if (Buffer.byteLength(file.relativePath, "utf8") > RUNTIME_EXTRACTION_LIMITS.maxRelativePathBytes) {
        fail("studio-runtime-extraction-limit-exceeded");
      }
      fail("studio-runtime-unsafe-archive-entry");
    }
    if (
      !exactFields(file, ["relativePath", "byteLength", "sha256", "type", "executable"])
      || !Number.isSafeInteger(file.byteLength)
      || file.byteLength < 0
      || !DIGEST.test(file.sha256)
      || file.type !== "file"
      || typeof file.executable !== "boolean"
    ) {
      fail("studio-runtime-tree-mismatch");
    }
    if (file.byteLength > RUNTIME_EXTRACTION_LIMITS.maxSingleFileBytes) {
      fail("studio-runtime-extraction-limit-exceeded");
    }
    totalBytes += file.byteLength;
    if (!Number.isSafeInteger(totalBytes) || totalBytes > RUNTIME_EXTRACTION_LIMITS.maxTotalBytes) {
      fail("studio-runtime-extraction-limit-exceeded");
    }
    const normalized = normalizedPathKey(file.relativePath);
    if (exactPaths.has(file.relativePath) || normalizedPaths.has(normalized)) {
      fail("studio-runtime-unsafe-archive-entry");
    }
    exactPaths.add(file.relativePath);
    normalizedPaths.add(normalized);
    const parts = file.relativePath.split("/");
    for (let index = 1; index < parts.length; index += 1) {
      const directoryPath = parts.slice(0, index).join("/");
      const normalizedDirectory = normalizedPathKey(directoryPath);
      const existingDirectory = normalizedDirectoryPaths.get(normalizedDirectory);
      if (existingDirectory !== undefined && existingDirectory !== directoryPath) {
        fail("studio-runtime-unsafe-archive-entry");
      }
      if (normalizedPaths.has(normalizedDirectory) && !directoryPaths.has(directoryPath)) {
        fail("studio-runtime-unsafe-archive-entry");
      }
      directoryPaths.add(directoryPath);
      normalizedDirectoryPaths.set(normalizedDirectory, directoryPath);
    }
  }
  for (const directoryPath of directoryPaths) {
    const normalized = normalizedPathKey(directoryPath);
    if ([...exactPaths].some((path) => normalizedPathKey(path) === normalized)) {
      fail("studio-runtime-unsafe-archive-entry");
    }
  }
  if (manifest.treeSha256 !== bundle.treeSha256 || computeTreeSha256(manifest.files) !== bundle.treeSha256) {
    fail("studio-runtime-tree-mismatch");
  }
  return {
    ...manifest,
    filesByPath: new Map(manifest.files.map((file) => [file.relativePath, file])),
    directoryPaths,
    totalBytes,
  };
}

export function validatePackageInventory(bytes, bundle) {
  const inventory = parseJson(bytes, "studio-runtime-tree-mismatch");
  if (!isValidRuntimePackageInventory(inventory, { pythonVersion: bundle.pythonVersion }) || !runtimeProfileInventoryMatches(bundle,inventory)) {
    fail("studio-runtime-tree-mismatch");
  }
  return inventory;
}

export function validateDerivationReceipt(bytes, bundle, manifest, packageInventory) {
  const receipt = parseJson(bytes, "studio-runtime-tree-mismatch");
  const materialPacketMatch = typeof receipt?.materialPacketId === "string"
    ? MATERIAL_PACKET.exec(receipt.materialPacketId)
    : null;
  if (
    receipt?.schemaVersion !== 1
    || receipt.status !== "succeeded"
    || typeof receipt.materialPacketId !== "string"
    || materialPacketMatch === null
    || materialPacketMatch[1] !== bundle.materialInventorySha256
    || typeof receipt.supplementalMaterialPacketId !== "string"
    || typeof receipt.builderToolPacketId !== "string"
    || !DIGEST.test(receipt.builderToolVerifiedReceiptSha256)
    || receipt.builderToolExecutionSecurityBoundary === null
    || typeof receipt.builderToolExecutionSecurityBoundary !== "object"
    || !Number.isSafeInteger(receipt.builderToolExecutable?.byteLength)
    || receipt.builderToolExecutable.byteLength <= 0
    || !DIGEST.test(receipt.builderToolExecutable?.sha256)
    || receipt.dependencyLockSha256 !== bundle.dependencyLockSha256
    || receipt.workerDependencyLockSha256 !== bundle.workerDependencyLockSha256
    || receipt.target?.platform !== bundle.platform
    || receipt.target?.architecture !== bundle.architecture
    || receipt.archiveSha256 !== bundle.archive.sha256
    || receipt.treeSha256 !== bundle.treeSha256
    || receipt.promotionVerification?.relocationImportSelfCheck !== "passed-before-promotion"
  ) fail("studio-runtime-tree-mismatch");
  const packetCore = {
    schemaVersion: 1,
    status: "awaiting-exact-bundle-and-license-approval",
    approved: false,
    enabled: false,
    licenseReview: "pending",
    materialPacketId: receipt.materialPacketId,
    supplementalMaterialPacketId: receipt.supplementalMaterialPacketId,
    builderToolPacketId: receipt.builderToolPacketId,
    builderToolExecutableSha256: receipt.builderToolExecutable.sha256,
    builderToolExecutableBytes: receipt.builderToolExecutable.byteLength,
    builderToolVerifiedReceiptSha256: receipt.builderToolVerifiedReceiptSha256,
    builderToolExecutionSecurityBoundary: receipt.builderToolExecutionSecurityBoundary,
    dependencyLockSha256: bundle.dependencyLockSha256,
    workerDependencyLockSha256: bundle.workerDependencyLockSha256,
    pythonVersion: bundle.pythonVersion,
    platform: bundle.platform,
    architecture: bundle.architecture,
    minimumOsVersion: bundle.minimumOsVersion,
    treeSha256: bundle.treeSha256,
    totalInstalledBytes: manifest.totalBytes,
    archive: bundle.archive,
    manifest: bundle.manifest,
    packageInventory: bundle.packageInventory,
    derivationReceipt: bundle.derivationReceipt,
  };
  if (bundle.runtimeProfileId !== undefined) packetCore.runtimeProfileId=bundle.runtimeProfileId;
  const extendedPacketCore = {
    ...packetCore,
    licenseInventorySummary: {
      sha256: bundle.packageInventory.sha256,
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
  };
  const legacyComputed = `worker-runtime-bundle-v1-sha256-${sha256Bytes(canonicalJson(packetCore))}`;
  const extendedComputed = `worker-runtime-bundle-v1-sha256-${sha256Bytes(canonicalJson(extendedPacketCore))}`;
  if (
    !APPROVAL_PACKET.test(bundle.approvalPacketId)
    || ![legacyComputed, extendedComputed].includes(bundle.approvalPacketId)
    || SUPERSEDED_BUNDLE_PACKET_IDS.has(bundle.approvalPacketId)
  ) fail("studio-runtime-artifact-unapproved");
  return receipt;
}

function safeContainedPath(root, relativePath, code) {
  if (!safeRelativePath(relativePath)) fail(code);
  const destination = resolve(root, relativePath);
  const fromRoot = relative(resolve(root), destination);
  if (fromRoot === ".." || fromRoot.startsWith(`..${sep}`) || isAbsolute(fromRoot)) fail(code);
  return destination;
}

async function lstatIfPresent(path, options) {
  try {
    return await lstat(path, options);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

function sameIdentity(left, right) {
  return left !== null && right !== null && left.dev === right.dev && left.ino === right.ino;
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

async function snapshotParentChain(root, relativePath, code) {
  const destination = safeContainedPath(root, relativePath, code);
  const parentParts = relative(resolve(root), dirname(destination)).split(sep).filter(Boolean);
  const snapshots = [];
  let current = resolve(root);
  for (const part of [null, ...parentParts]) {
    if (part !== null) current = join(current, part);
    const details = await lstatIfPresent(current, { bigint: true });
    if (details === null || details.isSymbolicLink() || !details.isDirectory()) fail(code);
    snapshots.push({ path: current, details });
  }
  return { destination, snapshots };
}

async function revalidateDirectories(snapshots) {
  for (const snapshot of snapshots) {
    const current = await lstatIfPresent(snapshot.path, { bigint: true });
    if (
      current === null
      || current.isSymbolicLink()
      || !current.isDirectory()
      || !sameStableDirectory(snapshot.details, current)
    ) return false;
  }
  return true;
}

async function revalidateDirectoryIdentities(snapshots) {
  for (const snapshot of snapshots) {
    const current = await lstatIfPresent(snapshot.path, { bigint: true });
    if (
      current === null
      || current.isSymbolicLink()
      || !current.isDirectory()
      || !sameIdentity(snapshot.details, current)
      || snapshot.details.mode !== current.mode
    ) return false;
  }
  return true;
}

async function readExact(handle, byteLength, position = 0) {
  const bytes = Buffer.allocUnsafe(byteLength);
  let offset = 0;
  while (offset < byteLength) {
    const result = await handle.read(
      bytes,
      offset,
      Math.min(READ_CHUNK_BYTES, byteLength - offset),
      position + offset,
    );
    if (result.bytesRead === 0) fail("studio-runtime-candidate-path-unsafe");
    offset += result.bytesRead;
  }
  return bytes;
}

async function openCandidateFile(candidateRoot, sidecar, maximumBytes) {
  if (!Number.isSafeInteger(sidecar.byteLength) || sidecar.byteLength <= 0 || sidecar.byteLength > maximumBytes) {
    fail("studio-runtime-candidate-digest-mismatch");
  }
  const parents = await snapshotParentChain(
    candidateRoot,
    sidecar.relativePath,
    "studio-runtime-candidate-path-unsafe",
  );
  const before = await lstatIfPresent(parents.destination, { bigint: true });
  if (before === null || before.isSymbolicLink() || !before.isFile() || before.nlink !== 1n) {
    fail("studio-runtime-candidate-path-unsafe");
  }
  let handle;
  try {
    handle = await open(parents.destination, constants.O_RDONLY | constants.O_NOFOLLOW);
    const opened = await handle.stat({ bigint: true });
    if (!opened.isFile() || opened.nlink !== 1n || !sameStableFile(before, opened)) {
      fail("studio-runtime-candidate-path-unsafe");
    }
    if (opened.size !== BigInt(sidecar.byteLength)) fail("studio-runtime-candidate-digest-mismatch");
    return { handle, opened, parents, path: parents.destination };
  } catch (error) {
    if (handle) await handle.close().catch(() => {});
    if (error instanceof Error && error.message.startsWith("studio-runtime-")) throw error;
    fail("studio-runtime-candidate-path-unsafe");
  }
}

async function hashOpenedFile(opened) {
  const digest = createHash("sha256");
  let position = 0;
  while (position < Number(opened.opened.size)) {
    const length = Math.min(READ_CHUNK_BYTES, Number(opened.opened.size) - position);
    const bytes = await readExact(opened.handle, length, position);
    digest.update(bytes);
    position += bytes.length;
  }
  const after = await opened.handle.stat({ bigint: true });
  if (!sameStableFile(opened.opened, after) || !await revalidateDirectories(opened.parents.snapshots)) {
    fail("studio-runtime-candidate-path-unsafe");
  }
  return digest.digest("hex");
}

async function readOpenedSidecar(opened, locked) {
  const bytes = await readExact(opened.handle, locked.byteLength);
  if (sha256Bytes(bytes) !== locked.sha256) fail("studio-runtime-candidate-digest-mismatch");
  const after = await opened.handle.stat({ bigint: true });
  if (!sameStableFile(opened.opened, after) || !await revalidateDirectories(opened.parents.snapshots)) {
    fail("studio-runtime-candidate-path-unsafe");
  }
  return bytes;
}

function decodeTarString(block, offset, length) {
  const field = block.subarray(offset, offset + length);
  const end = field.indexOf(0);
  const significant = field.subarray(0, end === -1 ? field.length : end);
  if (end !== -1 && field.subarray(end + 1).some((byte) => byte !== 0)) {
    fail("studio-runtime-unsafe-archive-entry");
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(significant);
  } catch {
    fail("studio-runtime-unsafe-archive-entry");
  }
}

function readTarOctal(block, offset, length) {
  const field = block.subarray(offset, offset + length);
  if (
    field.length !== length
    || field.at(-1) !== 0
    || field.subarray(0, -1).some((byte) => byte < 0x30 || byte > 0x37)
  ) fail("studio-runtime-unsafe-archive-entry");
  const parsed = Number.parseInt(field.subarray(0, -1).toString("ascii"), 8);
  if (!Number.isSafeInteger(parsed)) fail("studio-runtime-extraction-limit-exceeded");
  return parsed;
}

function readTarChecksum(block) {
  const field = block.subarray(148, 156);
  if (
    field.length !== 8
    || field[6] !== 0
    || field[7] !== 0x20
    || field.subarray(0, 6).some((byte) => byte < 0x30 || byte > 0x37)
  ) fail("studio-runtime-unsafe-archive-entry");
  return Number.parseInt(field.subarray(0, 6).toString("ascii"), 8);
}

function tarPath(block, type) {
  const name = decodeTarString(block, 0, 100);
  const prefix = decodeTarString(block, 345, 155);
  const path = prefix ? `${prefix}/${name}` : name;
  if (!safeRelativePath(path)) {
    if (Buffer.byteLength(path, "utf8") > RUNTIME_EXTRACTION_LIMITS.maxRelativePathBytes) {
      fail("studio-runtime-extraction-limit-exceeded");
    }
    fail("studio-runtime-unsafe-archive-entry");
  }
  return type === "5" ? path.replace(/\/$/u, "") : path;
}

function validateTarHeader(block) {
  const storedChecksum = readTarChecksum(block);
  const checksumBlock = Buffer.from(block);
  checksumBlock.fill(0x20, 148, 156);
  const computedChecksum = checksumBlock.reduce((total, byte) => total + byte, 0);
  const type = String.fromCharCode(block[156]);
  if (!["0", "5"].includes(type)) fail("studio-runtime-unsafe-archive-entry");
  const mode = readTarOctal(block, 100, 8);
  const size = readTarOctal(block, 124, 12);
  if (
    storedChecksum !== computedChecksum
    || readTarOctal(block, 108, 8) !== 0
    || readTarOctal(block, 116, 8) !== 0
    || readTarOctal(block, 136, 12) !== 0
    || decodeTarString(block, 157, 100) !== ""
    || decodeTarString(block, 257, 6) !== "ustar"
    || decodeTarString(block, 263, 2) !== "00"
    || decodeTarString(block, 265, 32) !== ""
    || decodeTarString(block, 297, 32) !== ""
    || block.subarray(329, 345).some((byte) => byte !== 0)
    || block.subarray(500, 512).some((byte) => byte !== 0)
    || (type === "5" && (mode !== 0o755 || size !== 0))
    || (type === "0" && ![0o644, 0o755].includes(mode))
  ) fail("studio-runtime-unsafe-archive-entry");
  return { type, mode, size, path: tarPath(block, type) };
}

export async function preflightRuntimeArchive(archive, manifest) {
  const entries = [];
  const paths = new Set();
  const normalizedPaths = new Set();
  const seenFiles = new Set();
  const seenDirectories = new Set();
  let position = 0;
  let zeroBlocks = 0;
  let fileCount = 0;
  let totalBytes = 0;
  while (position < Number(archive.opened.size)) {
    const block = await readExact(archive.handle, BLOCK_BYTES, position);
    position += BLOCK_BYTES;
    if (block.equals(ZERO_BLOCK)) {
      zeroBlocks += 1;
      continue;
    }
    if (zeroBlocks !== 0) fail("studio-runtime-unsafe-archive-entry");
    const entry = validateTarHeader(block);
    const normalized = normalizedPathKey(entry.path);
    if (paths.has(entry.path) || normalizedPaths.has(normalized)) {
      fail("studio-runtime-unsafe-archive-entry");
    }
    paths.add(entry.path);
    normalizedPaths.add(normalized);
    if (entry.type === "5") {
      if (!manifest.directoryPaths.has(entry.path)) fail("studio-runtime-tree-mismatch");
      seenDirectories.add(entry.path);
    } else {
      fileCount += 1;
      if (fileCount > RUNTIME_EXTRACTION_LIMITS.maxFiles
        || entry.size > RUNTIME_EXTRACTION_LIMITS.maxSingleFileBytes) {
        fail("studio-runtime-extraction-limit-exceeded");
      }
      totalBytes += entry.size;
      if (!Number.isSafeInteger(totalBytes) || totalBytes > RUNTIME_EXTRACTION_LIMITS.maxTotalBytes) {
        fail("studio-runtime-extraction-limit-exceeded");
      }
      const expected = manifest.filesByPath.get(entry.path);
      if (expected === undefined || expected.byteLength !== entry.size) fail("studio-runtime-tree-mismatch");
      if ((entry.mode === 0o755) !== expected.executable) fail("studio-runtime-tree-mismatch");
      seenFiles.add(entry.path);
      entries.push({ ...entry, dataOffset: position, expected });
    }
    const paddedBytes = Math.ceil(entry.size / BLOCK_BYTES) * BLOCK_BYTES;
    const nextPosition = position + paddedBytes;
    if (!Number.isSafeInteger(nextPosition) || nextPosition > Number(archive.opened.size)) {
      fail("studio-runtime-unsafe-archive-entry");
    }
    if (entry.size % BLOCK_BYTES !== 0 && entry.size > 0) {
      const paddingStart = position + entry.size;
      const padding = await readExact(archive.handle, nextPosition - paddingStart, paddingStart);
      if (padding.some((byte) => byte !== 0)) fail("studio-runtime-unsafe-archive-entry");
    }
    position = nextPosition;
  }
  if (zeroBlocks !== 2 || seenFiles.size !== manifest.files.length) fail("studio-runtime-tree-mismatch");
  if (
    seenDirectories.size !== manifest.directoryPaths.size
    || [...manifest.directoryPaths].some((path) => !seenDirectories.has(path))
  ) fail("studio-runtime-tree-mismatch");
  return entries;
}

async function physicalRoot(path, code) {
  const requested = resolve(path);
  const requestedParent = dirname(requested);
  const parentDetails = await lstatIfPresent(requestedParent);
  if (parentDetails === null || parentDetails.isSymbolicLink() || !parentDetails.isDirectory()) {
    fail(code);
  }
  const requestedDetails = await lstatIfPresent(requested);
  if (requestedDetails?.isSymbolicLink()) fail(code);
  const parent = await realpath(requestedParent).catch(() => fail(code));
  const physical = join(parent, basename(requested));
  const details = await lstatIfPresent(physical);
  if (details !== null && (details.isSymbolicLink() || !details.isDirectory())) fail(code);
  return physical;
}

async function ensureDirectDirectory(path, mode, code) {
  const current = await lstatIfPresent(path, { bigint: true });
  if (current !== null) {
    if (current.isSymbolicLink() || !current.isDirectory()) fail(code);
    return current;
  }
  try {
    await mkdir(path, { mode });
  } catch (error) {
    if (error?.code !== "EEXIST") throw error;
  }
  const created = await lstatIfPresent(path, { bigint: true });
  if (created === null || created.isSymbolicLink() || !created.isDirectory()) fail(code);
  await chmod(path, mode);
  return created;
}

function effectiveUid() {
  const uid = typeof process.geteuid === "function" ? process.geteuid() : process.getuid?.();
  if (!Number.isSafeInteger(uid) || uid < 0) fail("studio-runtime-staging-path-unsafe");
  return uid;
}

function privateOwnedDirectory(details, currentUid) {
  return details !== null
    && details.isDirectory()
    && !details.isSymbolicLink()
    && details.nlink >= 1n
    && details.uid === BigInt(currentUid)
    && (details.mode & 0o7777n) === 0o700n;
}

async function ensurePrivateOwnedDirectory(path, currentUid, code) {
  const parent = dirname(path);
  const parentBefore = await lstatIfPresent(parent, { bigint: true });
  if (parentBefore === null || parentBefore.isSymbolicLink() || !parentBefore.isDirectory()) fail(code);
  let details = await lstatIfPresent(path, { bigint: true });
  if (details === null) {
    try {
      await mkdir(path, { mode: 0o700 });
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
    }
    details = await lstatIfPresent(path, { bigint: true });
  }
  const parentAfter = await lstatIfPresent(parent, { bigint: true });
  if (
    !privateOwnedDirectory(details, currentUid)
    || parentAfter === null
    || parentAfter.isSymbolicLink()
    || !parentAfter.isDirectory()
    || !sameIdentity(parentBefore, parentAfter)
    || parentBefore.mode !== parentAfter.mode
  ) fail(code);
  return { path, details };
}

async function assertPrivateOwnedDirectory(snapshot, currentUid, code) {
  const current = await lstatIfPresent(snapshot.path, { bigint: true });
  if (
    !privateOwnedDirectory(current, currentUid)
    || !sameIdentity(snapshot.details, current)
  ) fail(code);
}

async function createInstallInfrastructure(modelRoot, currentUid) {
  const modelRootSnapshot = await ensurePrivateOwnedDirectory(
    modelRoot,
    currentUid,
    "studio-runtime-staging-path-unsafe",
  );
  const runtimeParentSnapshot = await ensurePrivateOwnedDirectory(
    join(modelRoot, "runtime"),
    currentUid,
    "studio-runtime-staging-path-unsafe",
  );
  const stagingParent = join(modelRoot, ".runtime-staging");
  await ensurePrivateOwnedDirectory(
    stagingParent,
    currentUid,
    "studio-runtime-staging-path-unsafe",
  );
  const lockParent = join(stagingParent, ".locks");
  const lockParentSnapshot = await ensurePrivateOwnedDirectory(
    lockParent,
    currentUid,
    "studio-runtime-staging-path-unsafe",
  );
  const stagingParentSnapshot = {
    path: stagingParent,
    details: await lstat(stagingParent, { bigint: true }),
  };
  await assertPrivateOwnedDirectory(stagingParentSnapshot, currentUid, "studio-runtime-staging-path-unsafe");
  await assertPrivateOwnedDirectory(modelRootSnapshot, currentUid, "studio-runtime-staging-path-unsafe");
  await assertPrivateOwnedDirectory(runtimeParentSnapshot, currentUid, "studio-runtime-staging-path-unsafe");
  return {
    lockParent: lockParentSnapshot,
    modelRoot: modelRootSnapshot,
    runtimeParent: runtimeParentSnapshot,
    stagingParent: stagingParentSnapshot,
  };
}

async function assertInstallInfrastructure(infrastructure, currentUid, code) {
  await assertPrivateOwnedDirectory(infrastructure.modelRoot, currentUid, code);
  await assertPrivateOwnedDirectory(infrastructure.runtimeParent, currentUid, code);
  await assertPrivateOwnedDirectory(infrastructure.stagingParent, currentUid, code);
  await assertPrivateOwnedDirectory(infrastructure.lockParent, currentUid, code);
}

function installLockRecord(bundleId, ownerToken, pid, processInstanceSha256, timestamp) {
  const iso = new Date(timestamp).toISOString();
  return {
    bundleId,
    createdAt: iso,
    heartbeatAt: iso,
    ownerToken,
    pid,
    processInstanceSha256,
  };
}

function parseInstallLock(bytes, bundleId) {
  let parsed;
  try {
    parsed = parseRuntimeJsonBytes(bytes);
  } catch {
    return null;
  }
  const legacyFields = ["bundleId", "createdAt", "heartbeatAt", "ownerToken", "pid"];
  const identityBoundFields = [...legacyFields, "processInstanceSha256"];
  const identityBound = exactFields(parsed, identityBoundFields);
  if (
    (!identityBound && !exactFields(parsed, legacyFields))
    || parsed.bundleId !== bundleId
    || !UUID_V4.test(parsed.ownerToken)
    || !Number.isSafeInteger(parsed.pid)
    || parsed.pid <= 0
    || parsed.pid > 2_147_483_647
    || (identityBound && !DIGEST.test(parsed.processInstanceSha256))
    || typeof parsed.createdAt !== "string"
    || typeof parsed.heartbeatAt !== "string"
  ) return null;
  const createdAt = Date.parse(parsed.createdAt);
  const heartbeatAt = Date.parse(parsed.heartbeatAt);
  if (
    !Number.isFinite(createdAt)
    || !Number.isFinite(heartbeatAt)
    || new Date(createdAt).toISOString() !== parsed.createdAt
    || new Date(heartbeatAt).toISOString() !== parsed.heartbeatAt
    || heartbeatAt < createdAt
    || decodeRuntimeUtf8(bytes) !== `${canonicalJson(parsed)}\n`
  ) return null;
  return parsed;
}

function processIsLive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "EPERM") return true;
    if (error?.code === "ESRCH") return false;
    return true;
  }
}

export async function resolveProcessInstanceIdentity(pid, dependencies = {}) {
  const platform = dependencies.platform ?? process.platform;
  const execute = dependencies.execute ?? execFile;
  const readText = dependencies.readText ?? readFile;
  if (!Number.isSafeInteger(pid) || pid <= 0 || pid > 2_147_483_647) return null;
  if (platform === "linux") {
    try {
      const statPath = `/proc/${pid}/stat`;
      const before = await readText(statPath, "utf8");
      const bootId = (await readText("/proc/sys/kernel/random/boot_id", "utf8")).trim();
      const after = await readText(statPath, "utf8");
      const startTicks = (value) => {
        const openingParenthesis = value.indexOf(" (");
        const closingParenthesis = value.lastIndexOf(")");
        const recordedPid = Number(value.slice(0, openingParenthesis));
        const fields = closingParenthesis === -1
          ? []
          : value.slice(closingParenthesis + 1).trim().split(/\s+/u);
        return openingParenthesis > 0
          && recordedPid === pid
          && fields.length >= 20
          && /^[0-9]+$/u.test(fields[19])
          ? fields[19]
          : null;
      };
      const beforeStartTicks = startTicks(before);
      const afterStartTicks = startTicks(after);
      if (
        beforeStartTicks === null
        || beforeStartTicks !== afterStartTicks
        || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(bootId)
      ) return null;
      return sha256Bytes(`linux-proc-start-v1\0${pid}\0${bootId}\0${beforeStartTicks}`);
    } catch {
      return null;
    }
  }
  if (platform === "darwin") {
    const commandOptions = {
      env: { PATH: "/usr/bin:/bin:/usr/sbin", LANG: "C", LC_ALL: "C", TZ: "UTC" },
      maxBuffer: 1024,
      timeout: 2_000,
    };
    const readCommand = async (executable, arguments_) => {
      try {
        const result = await execute(executable, arguments_, commandOptions);
        return typeof result?.stdout === "string" ? result.stdout.trim() : null;
      } catch {
        return null;
      }
    };
    const normalizeStart = (value) => {
      const start = value?.replace(/\s+/gu, " ");
      return start && start.length <= 128 ? start : null;
    };
    const normalizeBootSession = (value) => (
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu.test(value ?? "")
        ? value.toLowerCase()
        : null
    );
    const bootBefore = normalizeBootSession(await readCommand(
      "/usr/sbin/sysctl",
      ["-n", "kern.bootsessionuuid"],
    ));
    const startBefore = normalizeStart(await readCommand(
      "/bin/ps",
      ["-o", "lstart=", "-p", String(pid)],
    ));
    const startAfter = normalizeStart(await readCommand(
      "/bin/ps",
      ["-o", "lstart=", "-p", String(pid)],
    ));
    const bootAfter = normalizeBootSession(await readCommand(
      "/usr/sbin/sysctl",
      ["-n", "kern.bootsessionuuid"],
    ));
    if (
      bootBefore === null
      || bootBefore !== bootAfter
      || startBefore === null
      || startBefore !== startAfter
    ) return null;
    return sha256Bytes(`darwin-process-instance-v2\0${pid}\0${bootBefore}\0${startBefore}`);
  }
  return null;
}

async function processInstanceStatus(
  pid,
  expectedIdentity,
  timestamp,
  now,
  processLiveness,
  processIdentity,
) {
  if (!await processLiveness(pid)) return "stale";
  if (expectedIdentity === null) return "active";
  const currentIdentity = await processIdentity(pid);
  if (currentIdentity === null || currentIdentity === expectedIdentity) return "active";
  return now - timestamp < INSTALL_LOCK_LEASE_MS ? "active" : "stale";
}

async function inspectExistingInstallLock(
  path,
  bundleId,
  currentUid,
  now,
  processLiveness,
  processIdentity,
) {
  const before = await lstatIfPresent(path, { bigint: true });
  if (before === null) return { status: "retry" };
  if (
    before.isSymbolicLink()
    || !before.isFile()
    || before.nlink < 1n
    || before.uid !== BigInt(currentUid)
    || (before.mode & 0o7777n) !== 0o600n
  ) fail("studio-runtime-staging-path-unsafe");
  let handle;
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const opened = await handle.stat({ bigint: true });
    if (!sameStableFile(before, opened)) return { status: "busy" };
    let bytes = null;
    if (opened.size <= BigInt(MAX_INSTALL_LOCK_BYTES)) {
      bytes = await readExact(handle, Number(opened.size));
    }
    const after = await handle.stat({ bigint: true });
    const pathAfter = await lstatIfPresent(path, { bigint: true });
    if (!sameStableFile(opened, after) || !sameStableFile(after, pathAfter)) {
      return { status: "busy" };
    }
    const record = bytes === null ? null : parseInstallLock(bytes, bundleId);
    if (record !== null) {
      let status;
      if (Object.hasOwn(record, "processInstanceSha256")) {
        status = await processInstanceStatus(
          record.pid,
          record.processInstanceSha256,
          Date.parse(record.heartbeatAt),
          now,
          processLiveness,
          processIdentity,
        );
      } else {
        status = await processLiveness(record.pid) ? "active" : "stale";
      }
      return status === "active"
        ? { status: "busy" }
        : { details: after, record, status: "stale" };
    }
    const ageMilliseconds = now - Number(after.mtimeNs / 1_000_000n);
    return ageMilliseconds < INSTALL_LOCK_GRACE_MS
      ? { status: "busy" }
      : { details: after, record: null, status: "stale" };
  } catch (error) {
    if (error?.code === "ENOENT") return { status: "retry" };
    throw error;
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

async function cleanupRecoveryClaim(path, expected) {
  const current = await lstatIfPresent(path, { bigint: true }).catch(() => null);
  if (
    current !== null
    && !current.isSymbolicLink()
    && current.isFile()
    && sameIdentity(expected, current)
  ) await unlink(path).catch(() => {});
}

function parseRecoveryClaimName(name, lockName, bundleId) {
  const prefixes = [
    `${lockName}.recover-`,
    `worker-runtime-recovery-${sha256Bytes(bundleId)}-`,
  ];
  const prefix = prefixes.find((candidate) => name.startsWith(candidate));
  if (prefix === undefined) return null;
  const suffix = name.slice(prefix.length);
  const current = /^([1-9][0-9]{0,9})-([0-9]{1,16})-([a-f0-9]{64})-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/u.exec(suffix);
  if (current !== null) {
    const pid = Number(current[1]);
    const timestamp = Number(current[2]);
    if (!Number.isSafeInteger(pid) || pid > 2_147_483_647 || !Number.isSafeInteger(timestamp)) return null;
    return { pid, processInstanceSha256: current[3], timestamp };
  }
  const legacy = /^([1-9][0-9]{0,9})-([0-9]{1,16})-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/u.exec(suffix);
  if (legacy === null) return null;
  const pid = Number(legacy[1]);
  const timestamp = Number(legacy[2]);
  if (!Number.isSafeInteger(pid) || pid > 2_147_483_647 || !Number.isSafeInteger(timestamp)) return null;
  return { pid, processInstanceSha256: null, timestamp };
}

async function snapshotRecoveryClaims(
  path,
  stale,
  bundleId,
  claimPath,
  leaderPath,
  currentUid,
  now,
  processLiveness,
  processIdentity,
) {
  const lockParent = dirname(path);
  const lockName = basename(path);
  const claims = [];
  const unknown = [];
  for (const name of await readdir(lockParent)) {
    const candidatePath = join(lockParent, name);
    const details = await lstatIfPresent(candidatePath, { bigint: true });
    if (details === null || !sameIdentity(stale.details, details)) continue;
    if (
      details.isSymbolicLink()
      || !details.isFile()
      || details.uid !== BigInt(currentUid)
      || (details.mode & 0o7777n) !== 0o600n
    ) return { status: "busy" };
    if (candidatePath === path || candidatePath === leaderPath) continue;
    const parsed = parseRecoveryClaimName(name, lockName, bundleId);
    if (parsed === null) {
      unknown.push({ details, path: candidatePath });
      continue;
    }
    const status = candidatePath === claimPath
      ? "active"
      : await processInstanceStatus(
        parsed.pid,
        parsed.processInstanceSha256,
        parsed.timestamp,
        now,
        processLiveness,
        processIdentity,
      );
    claims.push({ details, path: candidatePath, status });
  }
  const original = await lstatIfPresent(path, { bigint: true });
  const leader = await lstatIfPresent(leaderPath, { bigint: true });
  const own = claims.find((claim) => claim.path === claimPath);
  if (
    original === null
    || !sameIdentity(stale.details, original)
    || leader === null
    || leader.isSymbolicLink()
    || !leader.isFile()
    || leader.uid !== BigInt(currentUid)
    || (leader.mode & 0o7777n) !== 0o600n
    || !sameIdentity(stale.details, leader)
    || original.nlink !== BigInt(claims.length + unknown.length + 2)
  ) return { status: "busy" };
  if (own === undefined) return { status: "claim-replaced" };
  const active = claims
    .filter((claim) => claim.status === "active")
    .sort((left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0));
  if (active[0]?.path !== claimPath) return { status: "busy" };
  const removable = [
    ...claims.filter((claim) => claim.status === "stale"),
    ...unknown,
  ];
  return {
    claims,
    leader,
    original,
    own,
    removable,
    status: active.length > 1 ? "wait" : removable.length > 0 ? "cleanup" : "ready",
  };
}

async function removeRecoveryEvidence(evidence, stale, currentUid) {
  let handle;
  try {
    handle = await open(evidence.path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const opened = await handle.stat({ bigint: true });
    const current = await lstatIfPresent(evidence.path, { bigint: true });
    if (
      opened.isSymbolicLink()
      || !opened.isFile()
      || opened.uid !== BigInt(currentUid)
      || (opened.mode & 0o7777n) !== 0o600n
      || !sameIdentity(stale.details, opened)
      || !sameIdentity(opened, current)
    ) return false;
    await unlink(evidence.path);
    return true;
  } catch (error) {
    if (error?.code === "ENOENT") return true;
    return false;
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

async function ensureRecoveryLeader(path, leaderPath, stale, currentUid) {
  try {
    await link(path, leaderPath);
  } catch (error) {
    if (error?.code !== "EEXIST") {
      if (error?.code === "ENOENT") return false;
      fail("studio-runtime-staging-path-unsafe");
    }
  }
  const [original, leader] = await Promise.all([
    lstatIfPresent(path, { bigint: true }),
    lstatIfPresent(leaderPath, { bigint: true }),
  ]);
  return original !== null
    && leader !== null
    && !leader.isSymbolicLink()
    && leader.isFile()
    && leader.uid === BigInt(currentUid)
    && (leader.mode & 0o7777n) === 0o600n
    && sameIdentity(stale.details, original)
    && sameIdentity(original, leader);
}

async function restoreRecoveryLeader(path, leaderPath, currentUid) {
  const leader = await lstatIfPresent(leaderPath, { bigint: true });
  if (leader === null) return;
  if (
    leader.isSymbolicLink()
    || !leader.isFile()
    || leader.nlink < 1n
    || leader.uid !== BigInt(currentUid)
    || (leader.mode & 0o7777n) !== 0o600n
  ) fail("studio-runtime-staging-path-unsafe");
  const original = await lstatIfPresent(path, { bigint: true });
  if (original !== null) {
    return;
  }
  try {
    await link(leaderPath, path);
  } catch (error) {
    if (error?.code === "ENOENT") return;
    if (error?.code !== "EEXIST") fail("studio-runtime-staging-path-unsafe");
  }
  const restored = await lstatIfPresent(path, { bigint: true });
  if (restored === null || !sameIdentity(leader, restored)) fail("studio-runtime-install-busy");
}

async function reconcileRecoveryLeader(
  path,
  leaderPath,
  stale,
  bundleId,
  currentUid,
  now,
  processLiveness,
  processIdentity,
) {
  const leader = await lstatIfPresent(leaderPath, { bigint: true });
  if (leader === null || sameIdentity(stale.details, leader)) return true;
  const inspected = await inspectExistingInstallLock(
    leaderPath,
    bundleId,
    currentUid,
    now,
    processLiveness,
    processIdentity,
  );
  if (inspected.status !== "stale") return false;
  const [mainCurrent, leaderCurrent] = await Promise.all([
    lstatIfPresent(path, { bigint: true }),
    lstatIfPresent(leaderPath, { bigint: true }),
  ]);
  if (
    mainCurrent === null
    || leaderCurrent === null
    || !sameIdentity(stale.details, mainCurrent)
    || !sameIdentity(inspected.details, leaderCurrent)
  ) return false;
  try {
    await unlink(leaderPath);
    return true;
  } catch {
    return false;
  }
}

async function recoverStaleInstallLock(
  path,
  stale,
  bundleId,
  currentUid,
  now,
  pid,
  processInstanceSha256,
  processLiveness,
  processIdentity,
  beforeRecoveryUnlink,
) {
  const claimPath = join(
    dirname(path),
    `worker-runtime-recovery-${sha256Bytes(bundleId)}-${pid}-${now}-${processInstanceSha256}-${randomUUID()}`,
  );
  const leaderPath = `${path}.recovery-leader`;
  try {
    await link(path, claimPath);
  } catch (error) {
    if (error?.code === "ENOENT" || error?.code === "EEXIST") return false;
    fail("studio-runtime-staging-path-unsafe");
  }
  let claim;
  try {
    claim = await lstatIfPresent(claimPath, { bigint: true });
    const original = await lstatIfPresent(path, { bigint: true });
    if (
      claim === null
      || original === null
      || claim.isSymbolicLink()
      || !claim.isFile()
      || claim.uid !== BigInt(currentUid)
      || (claim.mode & 0o7777n) !== 0o600n
      || !sameIdentity(stale.details, claim)
      || !sameIdentity(claim, original)
    ) return false;
    const confirmed = await inspectExistingInstallLock(
      claimPath,
      bundleId,
      currentUid,
      now,
      processLiveness,
      processIdentity,
    );
    if (confirmed.status !== "stale") return false;
    if (!await ensureRecoveryLeader(path, leaderPath, stale, currentUid)) return false;
    if (beforeRecoveryUnlink) await beforeRecoveryUnlink({ claimPath, lockPath: path });
    let snapshot;
    for (let waitAttempt = 0; waitAttempt < 16; waitAttempt += 1) {
      snapshot = await snapshotRecoveryClaims(
        path,
        stale,
        bundleId,
        claimPath,
        leaderPath,
        currentUid,
        now,
        processLiveness,
        processIdentity,
      );
      if (snapshot.status === "ready") break;
      if (snapshot.status === "claim-replaced") fail("studio-runtime-install-busy");
      if (snapshot.status === "busy") return false;
      if (snapshot.status === "cleanup") {
        let cleaned = true;
        for (const evidence of snapshot.removable) {
          if (!await removeRecoveryEvidence(evidence, stale, currentUid)) cleaned = false;
        }
        if (!cleaned) return false;
        continue;
      }
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 0));
    }
    if (snapshot?.status !== "ready") return false;
    await unlink(path);
    const claimAfter = await lstatIfPresent(claimPath, { bigint: true });
    if (claimAfter === null || !sameIdentity(stale.details, claimAfter) || claimAfter.nlink < 1n) {
      fail("studio-runtime-promotion-recovery-required");
    }
    const leaderAfter = await lstatIfPresent(leaderPath, { bigint: true });
    if (leaderAfter === null || !sameIdentity(snapshot.leader, leaderAfter)) {
      fail("studio-runtime-promotion-recovery-required");
    }
    await unlink(leaderPath);
    await unlink(claimPath);
    return true;
  } finally {
    if (claim !== undefined && claim !== null) await cleanupRecoveryClaim(claimPath, claim);
  }
}

async function acquireInstallLock(infrastructure, bundleId, currentUid, dependencies) {
  const now = dependencies.now ?? Date.now;
  const pid = dependencies.pid ?? process.pid;
  const processIdentity = dependencies.processIdentity ?? resolveProcessInstanceIdentity;
  const processLiveness = dependencies.processLiveness ?? processIsLive;
  const openLock = dependencies.openInstallLock ?? open;
  const processInstanceSha256 = await processIdentity(pid);
  if (!DIGEST.test(processInstanceSha256 ?? "")) fail("studio-runtime-staging-path-unsafe");
  await assertInstallInfrastructure(infrastructure, currentUid, "studio-runtime-staging-path-unsafe");
  const path = join(infrastructure.lockParent.path, `${bundleId}.lock`);
  const leaderPath = `${path}.recovery-leader`;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    await restoreRecoveryLeader(path, leaderPath, currentUid);
    const ownerToken = randomUUID();
    const record = installLockRecord(
      bundleId,
      ownerToken,
      pid,
      processInstanceSha256,
      now(),
    );
    const recordBytes = Buffer.from(`${canonicalJson(record)}\n`, "utf8");
    let handle;
    let details;
    try {
      handle = await openLock(
        path,
        constants.O_RDWR | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
        0o600,
      );
    } catch (error) {
      if (error?.code !== "EEXIST") fail("studio-runtime-staging-path-unsafe");
      const inspected = await inspectExistingInstallLock(
        path,
        bundleId,
        currentUid,
        now(),
        processLiveness,
        processIdentity,
      );
      if (inspected.status === "retry") continue;
      if (inspected.status === "busy") fail("studio-runtime-install-busy");
      if (!await reconcileRecoveryLeader(
        path,
        leaderPath,
        inspected,
        bundleId,
        currentUid,
        now(),
        processLiveness,
        processIdentity,
      )) fail("studio-runtime-install-busy");
      if (await recoverStaleInstallLock(
        path,
        inspected,
        bundleId,
        currentUid,
        now(),
        pid,
        processInstanceSha256,
        processLiveness,
        processIdentity,
        dependencies.beforeRecoveryUnlink,
      )) continue;
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 0));
      continue;
    }
    try {
      await handle.writeFile(recordBytes);
      await handle.sync();
      details = await handle.stat({ bigint: true });
      if (
        !details.isFile()
        || details.nlink !== 1n
        || details.uid !== BigInt(currentUid)
        || (details.mode & 0o7777n) !== 0o600n
        || details.size !== BigInt(recordBytes.length)
      ) fail("studio-runtime-staging-path-unsafe");
      const pathDetails = await lstatIfPresent(path, { bigint: true });
      if (!sameStableFile(details, pathDetails)) fail("studio-runtime-staging-path-unsafe");
      await assertInstallInfrastructure(infrastructure, currentUid, "studio-runtime-staging-path-unsafe");
      return {
        currentUid,
        details,
        handle,
        infrastructure,
        ownerToken,
        path,
        unlinkPath: dependencies.unlinkInstallLock ?? unlink,
        record,
        recordBytes,
      };
    } catch (error) {
      if (details !== undefined) {
        const current = await lstatIfPresent(path, { bigint: true }).catch(() => null);
        if (sameStableFile(details, current)) await unlink(path).catch(() => {});
      }
      await handle.close().catch(() => {});
      throw error;
    }
  }
  fail("studio-runtime-install-busy");
}

async function installLockIntact(lock) {
  try {
    await assertInstallInfrastructure(
      lock.infrastructure,
      lock.currentUid,
      "studio-runtime-promotion-recovery-required",
    );
    const [opened, pathDetails] = await Promise.all([
      lock.handle.stat({ bigint: true }),
      lstatIfPresent(lock.path, { bigint: true }),
    ]);
    if (
      !opened.isFile()
      || opened.nlink !== 1n
      || opened.uid !== BigInt(lock.currentUid)
      || (opened.mode & 0o7777n) !== 0o600n
      || pathDetails === null
      || pathDetails.isSymbolicLink()
      || !pathDetails.isFile()
      || pathDetails.nlink !== 1n
      || !sameStableFile(lock.details, opened)
      || !sameStableFile(opened, pathDetails)
    ) return false;
    const bytes = await readExact(lock.handle, lock.recordBytes.length);
    const after = await lock.handle.stat({ bigint: true });
    const parsed = parseInstallLock(bytes, lock.record.bundleId);
    return parsed !== null
      && parsed.ownerToken === lock.ownerToken
      && bytes.equals(lock.recordBytes)
      && sameStableFile(opened, after);
  } catch {
    return false;
  }
}

async function refreshInstallLock(lock, now) {
  if (!await installLockIntact(lock)) return false;
  try {
    const heartbeatMilliseconds = Math.max(now(), Date.parse(lock.record.heartbeatAt));
    const record = {
      ...lock.record,
      heartbeatAt: new Date(heartbeatMilliseconds).toISOString(),
    };
    const recordBytes = Buffer.from(`${canonicalJson(record)}\n`, "utf8");
    const written = await lock.handle.write(recordBytes, 0, recordBytes.length, 0);
    if (written.bytesWritten !== recordBytes.length) return false;
    await lock.handle.truncate(recordBytes.length);
    await lock.handle.sync();
    const [opened, pathDetails] = await Promise.all([
      lock.handle.stat({ bigint: true }),
      lstatIfPresent(lock.path, { bigint: true }),
    ]);
    if (
      !opened.isFile()
      || opened.nlink !== 1n
      || opened.uid !== BigInt(lock.currentUid)
      || (opened.mode & 0o7777n) !== 0o600n
      || !sameStableFile(opened, pathDetails)
      || parseInstallLock(recordBytes, record.bundleId)?.ownerToken !== lock.ownerToken
    ) return false;
    lock.details = opened;
    lock.record = record;
    lock.recordBytes = recordBytes;
    return true;
  } catch {
    return false;
  }
}

async function releaseInstallLock(lock) {
  if (!await installLockIntact(lock)) {
    await lock.handle.close().catch(() => {});
    fail("studio-runtime-promotion-recovery-required");
  }
  try {
    await lock.unlinkPath(lock.path);
  } finally {
    await lock.handle.close();
  }
  await assertInstallInfrastructure(
    lock.infrastructure,
    lock.currentUid,
    "studio-runtime-promotion-recovery-required",
  );
}

async function createPrivateStaging(infrastructure, currentUid) {
  await assertInstallInfrastructure(infrastructure, currentUid, "studio-runtime-staging-path-unsafe");
  const stagingParent = infrastructure.stagingParent.path;
  const parentBefore = await lstat(stagingParent, { bigint: true });
  const root = await mkdtemp(join(stagingParent, ".install-"));
  await chmod(root, 0o700);
  const details = await lstat(root, { bigint: true });
  if (!privateOwnedDirectory(details, currentUid)) fail("studio-runtime-staging-path-unsafe");
  const ownerToken = randomUUID();
  const marker = join(root, ".installer-owner");
  const handle = await open(marker, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    await handle.writeFile(ownerToken, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
  const parentAfter = await lstat(stagingParent, { bigint: true });
  if (
    !sameIdentity(parentBefore, parentAfter)
    || parentBefore.mode !== parentAfter.mode
    || !privateOwnedDirectory(parentAfter, currentUid)
  ) {
    fail("studio-runtime-staging-path-unsafe");
  }
  await assertInstallInfrastructure(infrastructure, currentUid, "studio-runtime-staging-path-unsafe");
  return { root, details, marker, ownerToken, currentUid };
}

async function proveOwnedStaging(staging) {
  const current = await lstatIfPresent(staging.root, { bigint: true });
  if (current === null) return false;
  if (
    !privateOwnedDirectory(current, staging.currentUid)
    || !sameIdentity(staging.details, current)
  ) return false;
  const markerDetails = await lstatIfPresent(staging.marker, { bigint: true });
  if (
    markerDetails === null
    || markerDetails.isSymbolicLink()
    || !markerDetails.isFile()
    || markerDetails.nlink !== 1n
    || markerDetails.uid !== BigInt(staging.currentUid)
    || (markerDetails.mode & 0o7777n) !== 0o600n
  ) return false;
  let handle;
  try {
    handle = await open(staging.marker, constants.O_RDONLY | constants.O_NOFOLLOW);
    const opened = await handle.stat({ bigint: true });
    if (!sameStableFile(markerDetails, opened)) return false;
    const bytes = await readExact(handle, Buffer.byteLength(staging.ownerToken));
    return bytes.toString("utf8") === staging.ownerToken;
  } catch {
    return false;
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

async function cleanupOwnedStaging(staging) {
  if (await lstatIfPresent(staging.root) === null) return;
  if (!await proveOwnedStaging(staging)) fail("studio-runtime-promotion-recovery-required");
  await rm(staging.root, { recursive: true, force: false });
}

async function createRuntimeDirectories(runtimeRoot, directoryPaths) {
  await mkdir(runtimeRoot, { mode: 0o700 });
  await chmod(runtimeRoot, 0o700);
  const sorted = [...directoryPaths].sort((left, right) => (
    left.split("/").length - right.split("/").length
    || compareRuntimePathsByUtf8Bytes(left, right)
  ));
  for (const relativePath of sorted) {
    const parents = await snapshotParentChain(
      runtimeRoot,
      relativePath,
      "studio-runtime-staging-path-unsafe",
    );
    const destination = safeContainedPath(runtimeRoot, relativePath, "studio-runtime-staging-path-unsafe");
    const parent = dirname(destination);
    const parentDetails = await lstatIfPresent(parent, { bigint: true });
    if (parentDetails === null || parentDetails.isSymbolicLink() || !parentDetails.isDirectory()) {
      fail("studio-runtime-staging-path-unsafe");
    }
    try {
      await mkdir(destination, { mode: 0o755 });
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      fail("studio-runtime-staging-path-unsafe");
    }
    const created = await lstatIfPresent(destination, { bigint: true });
    const parentAfter = await lstatIfPresent(parent, { bigint: true });
    if (
      created === null || created.isSymbolicLink() || !created.isDirectory()
      || parentAfter === null || !sameIdentity(parentDetails, parentAfter)
      || !await revalidateDirectoryIdentities(parents.snapshots)
    ) fail("studio-runtime-staging-path-unsafe");
    await chmod(destination, 0o755);
  }
}

async function writeOpenedFile(destinationRoot, relativePath, mode, writeBody) {
  const parents = await snapshotParentChain(
    destinationRoot,
    relativePath,
    "studio-runtime-staging-path-unsafe",
  );
  let handle;
  try {
    handle = await open(
      parents.destination,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      mode,
    );
    const opened = await handle.stat({ bigint: true });
    if (!opened.isFile() || opened.nlink !== 1n) fail("studio-runtime-staging-path-unsafe");
    await writeBody(handle);
    await handle.sync();
    await handle.chmod(mode);
    const after = await handle.stat({ bigint: true });
    if (!sameIdentity(opened, after) || !await revalidateDirectoryIdentities(parents.snapshots)) {
      fail("studio-runtime-staging-path-unsafe");
    }
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

async function extractRuntime(archive, entries, runtimeRoot, manifest) {
  await createRuntimeDirectories(runtimeRoot, manifest.directoryPaths);
  for (const entry of entries) {
    await writeOpenedFile(runtimeRoot, entry.path, entry.mode, async (destination) => {
      const digest = createHash("sha256");
      let copied = 0;
      while (copied < entry.size) {
        const length = Math.min(READ_CHUNK_BYTES, entry.size - copied);
        const bytes = await readExact(archive.handle, length, entry.dataOffset + copied);
        digest.update(bytes);
        await destination.write(bytes);
        copied += bytes.length;
      }
      if (digest.digest("hex") !== entry.expected.sha256) fail("studio-runtime-tree-mismatch");
    });
  }
  const after = await archive.handle.stat({ bigint: true });
  if (!sameStableFile(archive.opened, after) || !await revalidateDirectories(archive.parents.snapshots)) {
    fail("studio-runtime-candidate-path-unsafe");
  }
}

async function readInstalledFile(runtimeRoot, file, expectedMode = file.executable ? 0o755 : 0o644) {
  const parents = await snapshotParentChain(runtimeRoot, file.relativePath, "studio-runtime-tree-mismatch");
  const before = await lstatIfPresent(parents.destination, { bigint: true });
  if (before === null || before.isSymbolicLink() || !before.isFile() || before.nlink !== 1n) {
    fail("studio-runtime-tree-mismatch");
  }
  if (before.size !== BigInt(file.byteLength)) fail("studio-runtime-tree-mismatch");
  let handle;
  try {
    handle = await open(parents.destination, constants.O_RDONLY | constants.O_NOFOLLOW);
    const opened = await handle.stat({ bigint: true });
    if (!sameStableFile(before, opened)) fail("studio-runtime-tree-mismatch");
    const bytes = await readExact(handle, file.byteLength);
    const after = await handle.stat({ bigint: true });
    if (
      !sameStableFile(opened, after)
      || !await revalidateDirectories(parents.snapshots)
      || sha256Bytes(bytes) !== file.sha256
      || (opened.mode & 0o7777n) !== BigInt(expectedMode)
    ) fail("studio-runtime-tree-mismatch");
    return bytes;
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

async function walkRuntimeTree(root, current = root, prefix = "") {
  const files = [];
  const directories = [];
  const before = await lstat(current, { bigint: true });
  if (before.isSymbolicLink() || !before.isDirectory()) fail("studio-runtime-tree-mismatch");
  directories.push({ relativePath: prefix, details: before });
  const entries = await readdir(current, { withFileTypes: true });
  for (const entry of entries) {
    const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (!safeRelativePath(relativePath)) fail("studio-runtime-tree-mismatch");
  }
  entries.sort((left, right) => compareRuntimePathsByUtf8Bytes(left.name, right.name));
  for (const entry of entries) {
    const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (!safeRelativePath(relativePath)) fail("studio-runtime-tree-mismatch");
    const path = join(current, entry.name);
    const details = await lstat(path, { bigint: true });
    if (details.isSymbolicLink()) fail("studio-runtime-tree-mismatch");
    if (details.isDirectory()) {
      const nested = await walkRuntimeTree(root, path, relativePath);
      files.push(...nested.files);
      directories.push(...nested.directories);
    } else if (details.isFile()) files.push(relativePath);
    else fail("studio-runtime-tree-mismatch");
  }
  const after = await lstat(current, { bigint: true });
  if (!sameStableDirectory(before, after)) fail("studio-runtime-tree-mismatch");
  return { files, directories };
}

async function verifyRuntimeTree(runtimeRoot, manifest, allowedSidecars = []) {
  const expected = new Set([...manifest.files.map((file) => file.relativePath), ...allowedSidecars]);
  const actual = await walkRuntimeTree(runtimeRoot);
  if (actual.files.length !== expected.size || actual.files.some((path) => !expected.has(path))) {
    fail("studio-runtime-tree-mismatch");
  }
  const expectedDirectories = new Set([""]);
  for (const relativePath of expected) {
    const parts = relativePath.split("/");
    for (let index = 1; index < parts.length; index += 1) {
      expectedDirectories.add(parts.slice(0, index).join("/"));
    }
  }
  if (
    actual.directories.length !== expectedDirectories.size
    || actual.directories.some(({ relativePath, details }) => (
      !expectedDirectories.has(relativePath)
      || details.nlink < 1n
      || (details.mode & 0o7777n) !== BigInt(relativePath === "" ? 0o700 : 0o755)
    ))
  ) fail("studio-runtime-tree-mismatch");
  for (const file of manifest.files) await readInstalledFile(runtimeRoot, file);
}

async function runTechnicalSelfCheck(runtimeRoot, bundle) {
  const sandbox = await lstatIfPresent(SANDBOX_EXECUTABLE);
  if (sandbox === null || sandbox.isSymbolicLink() || !sandbox.isFile()) {
    fail("studio-runtime-python-mismatch");
  }
  const python = join(runtimeRoot, "python", "bin", "python3.12");
  const policy = join(runtimeRoot, "policy", "deny-network.sb");
  const source = `import json, pathlib, platform, sys\nroot=pathlib.Path(${JSON.stringify(runtimeRoot)}).resolve()\nexecutable=pathlib.Path(sys.executable).resolve()\nif root not in executable.parents: raise SystemExit("python-origin-invalid")\nprint(json.dumps({"status":"ready","pythonVersion":".".join(map(str,sys.version_info[:3])),"architecture":platform.machine()}))\n`;
  let result;
  try {
    result = await execFile(SANDBOX_EXECUTABLE, [
      "-f", policy, python, "-I", "-S", "-B", "-c", source,
    ], {
      cwd: runtimeRoot,
      env: {
        PATH: "/usr/bin:/bin",
        HOME: dirname(runtimeRoot),
        TMPDIR: dirname(runtimeRoot),
        LANG: "C",
        LC_ALL: "C",
        PYTHONNOUSERSITE: "1",
        PYTHONSAFEPATH: "1",
        PYTHONDONTWRITEBYTECODE: "1",
        HF_HUB_OFFLINE: "1",
        TRANSFORMERS_OFFLINE: "1",
      },
      timeout: 30_000,
      maxBuffer: 64 * 1024,
    });
  } catch {
    fail("studio-runtime-python-mismatch");
  }
  const line = result.stdout.trim().split("\n").filter(Boolean).at(-1);
  const checked = parseJson(Buffer.from(line ?? ""), "studio-runtime-python-mismatch");
  if (
    checked.status !== "ready"
    || checked.pythonVersion !== bundle.pythonVersion
    || checked.architecture !== bundle.architecture
  ) fail("studio-runtime-python-mismatch");
}

async function writeSidecars(runtimeRoot, bundle, bytesByName) {
  for (const [name, sidecar] of [
    ["manifest", bundle.manifest],
    ["packageInventory", bundle.packageInventory],
    ["derivationReceipt", bundle.derivationReceipt],
  ]) {
    const bytes = bytesByName[name];
    await writeOpenedFile(runtimeRoot, sidecar.relativePath, 0o600, async (handle) => {
      await handle.write(bytes);
    });
  }
}

async function verifyInstalledSidecars(runtimeRoot, bundle) {
  for (const sidecar of [bundle.manifest, bundle.packageInventory, bundle.derivationReceipt]) {
    await readInstalledFile(runtimeRoot, { ...sidecar, executable: false }, 0o600);
  }
}

async function loadApprovedBundle(lockPath, bundleId) {
  let lock;
  try {
    lock = await loadModelLock(lockPath);
  } catch {
    fail("studio-runtime-artifact-unapproved");
  }
  let bundle;
  try {
    bundle = findWorkerRuntime(lock, bundleId);
  } catch {
    fail("studio-runtime-artifact-unapproved");
  }
  const approvalIssue = workerRuntimeApprovalIssue(bundle);
  if (
    !SAFE_ARTIFACT_ID.test(bundle.id)
    || bundle.id === "."
    || bundle.id === ".."
    || approvalIssue !== null
  ) {
    fail("studio-runtime-artifact-unapproved", approvalIssue ?? "artifact-id-invalid");
  }
  const requiredPaths = {
    archive: "runtime.tar",
    manifest: "runtime-manifest.json",
    packageInventory: "runtime-packages.json",
    derivationReceipt: "derivation-receipt.json",
  };
  for (const [name, requiredPath] of Object.entries(requiredPaths)) {
    if (bundle[name]?.relativePath !== requiredPath) fail("studio-runtime-artifact-unapproved");
  }
  return { bundle, lock };
}

async function snapshotExactCandidateInventory(root, bundle) {
  const before = await lstat(root, { bigint: true });
  if (before.isSymbolicLink() || !before.isDirectory()) {
    fail("studio-runtime-candidate-path-unsafe");
  }
  const expected = new Set([
    bundle.archive.relativePath,
    bundle.manifest.relativePath,
    bundle.packageInventory.relativePath,
    bundle.derivationReceipt.relativePath,
  ]);
  const entries = await readdir(root, { withFileTypes: true });
  if (
    expected.size !== 4
    || entries.length !== expected.size
    || entries.some((entry) => !expected.has(entry.name) || !entry.isFile() || entry.isSymbolicLink())
  ) fail("studio-runtime-candidate-path-unsafe");
  const after = await lstat(root, { bigint: true });
  if (!sameStableDirectory(before, after)) fail("studio-runtime-candidate-path-unsafe");
  return after;
}

export async function verifyCandidate(bundle, candidateRoot) {
  const root = await physicalRoot(candidateRoot, "studio-runtime-candidate-path-unsafe");
  const rootDetails = await snapshotExactCandidateInventory(root, bundle);
  const archive = await openCandidateFile(root, bundle.archive, MAX_ARCHIVE_BYTES);
  const openedSidecars = [];
  try {
    if (await hashOpenedFile(archive) !== bundle.archive.sha256) {
      fail("studio-runtime-candidate-digest-mismatch");
    }
    const bytesByName = {};
    for (const [name, sidecar, maximum] of [
      ["manifest", bundle.manifest, MAX_MANIFEST_BYTES],
      ["packageInventory", bundle.packageInventory, MAX_PACKAGE_INVENTORY_BYTES],
      ["derivationReceipt", bundle.derivationReceipt, MAX_DERIVATION_RECEIPT_BYTES],
    ]) {
      const opened = await openCandidateFile(root, sidecar, maximum);
      openedSidecars.push(opened);
      bytesByName[name] = await readOpenedSidecar(opened, sidecar);
    }
    const manifest = parseRuntimeManifest(bytesByName.manifest, bundle);
    const packageInventory = validatePackageInventory(bytesByName.packageInventory, bundle);
    validateDerivationReceipt(bytesByName.derivationReceipt, bundle, manifest, packageInventory);
    const entries = await preflightRuntimeArchive(archive, manifest);
    const rootAfter = await lstat(root, { bigint: true });
    if (!sameStableDirectory(rootDetails, rootAfter)) fail("studio-runtime-candidate-path-unsafe");
    return { archive, bytesByName, entries, manifest };
  } catch (error) {
    await archive.handle.close().catch(() => {});
    throw error;
  } finally {
    await Promise.all(openedSidecars.map((opened) => opened.handle.close().catch(() => {})));
  }
}

async function exactInstalled(bundle, modelRoot, lock) {
  const inspected = await inspectWorkerRuntime(bundle, modelRoot, lock);
  return [
    "runtime-installed-probe-required",
    "runtime-real-probe-failed",
    "runtime-real-probe-succeeded",
  ].includes(inspected.status);
}

export async function installWorkerRuntime(input, dependencies = {}) {
  const promote = dependencies.promote ?? rename;
  const currentUid = dependencies.currentUid ?? effectiveUid();
  const now = dependencies.now ?? Date.now;
  const { bundle, lock } = await loadApprovedBundle(input.lockPath, input.bundleId);
  const candidate = await verifyCandidate(bundle, input.candidateRoot);
  let staging;
  let installLock;
  let retainStaging = false;
  try {
    const modelRoot = await physicalRoot(input.modelRoot, "studio-runtime-staging-path-unsafe");
    const infrastructure = await createInstallInfrastructure(modelRoot, currentUid);
    installLock = await acquireInstallLock(infrastructure, bundle.id, currentUid, {
      ...dependencies,
      now,
    });
    const requireInstallLock = async () => {
      if (!await refreshInstallLock(installLock, now)) {
        if (staging) retainStaging = true;
        fail("studio-runtime-promotion-recovery-required");
      }
    };
    await requireInstallLock();
    const existingRoot = join(modelRoot, "runtime", bundle.id);
    const existing = await lstatIfPresent(existingRoot);
    if (existing !== null) {
      if (await exactInstalled(bundle, modelRoot, lock)) {
        return { artifact: bundle.id, status: "ready", reused: true };
      }
      fail("studio-runtime-existing-runtime-differs");
    }

    staging = await createPrivateStaging(infrastructure, currentUid);
    const stagedRuntime = join(staging.root, "runtime");
    await extractRuntime(candidate.archive, candidate.entries, stagedRuntime, candidate.manifest);
    await requireInstallLock();
    await verifyRuntimeTree(stagedRuntime, candidate.manifest);
    await runTechnicalSelfCheck(stagedRuntime, bundle);
    await requireInstallLock();
    await verifyRuntimeTree(stagedRuntime, candidate.manifest);
    await writeSidecars(stagedRuntime, bundle, candidate.bytesByName);
    const installedSidecars = [
      bundle.manifest.relativePath,
      bundle.packageInventory.relativePath,
      bundle.derivationReceipt.relativePath,
    ];
    await verifyRuntimeTree(stagedRuntime, candidate.manifest, installedSidecars);
    await verifyInstalledSidecars(stagedRuntime, bundle);
    await requireInstallLock();

    const runtimeParent = join(modelRoot, "runtime");
    await ensureDirectDirectory(runtimeParent, 0o700, "studio-runtime-staging-path-unsafe");
    await requireInstallLock();
    if (await lstatIfPresent(existingRoot) !== null) {
      if (await exactInstalled(bundle, modelRoot, lock)) {
        return { artifact: bundle.id, status: "ready", reused: true };
      }
      fail("studio-runtime-existing-runtime-differs");
    }
    try {
      await promote(stagedRuntime, existingRoot);
    } catch (error) {
      await requireInstallLock();
      if (await exactInstalled(bundle, modelRoot, lock)) {
        return { artifact: bundle.id, status: "ready", reused: false };
      }
      const stagedStillOwned = await lstatIfPresent(stagedRuntime) !== null && await proveOwnedStaging(staging);
      if (await lstatIfPresent(existingRoot) !== null || !stagedStillOwned) {
        retainStaging = true;
        fail("studio-runtime-promotion-recovery-required");
      }
      if (error instanceof Error && error.message.startsWith("studio-runtime-")) throw error;
      fail("studio-runtime-promotion-failed");
    }
    await requireInstallLock();
    if (!await exactInstalled(bundle, modelRoot, lock)) {
      retainStaging = true;
      fail("studio-runtime-promotion-recovery-required");
    }
    return { artifact: bundle.id, status: "ready", reused: false };
  } finally {
    await candidate.archive.handle.close().catch(() => {});
    try {
      if (staging && !retainStaging) await cleanupOwnedStaging(staging);
    } finally {
      if (installLock) await releaseInstallLock(installLock);
    }
  }
}
