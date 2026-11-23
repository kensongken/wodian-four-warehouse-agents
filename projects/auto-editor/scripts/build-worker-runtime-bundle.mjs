#!/usr/bin/env node

import { createHash } from "node:crypto";
import { constants, createReadStream, readFileSync } from "node:fs";
import { phoneRuntimeProfileDefinition } from "./studio-runtime-profiles.mjs";
import { execFile as execFileCallback, spawn } from "node:child_process";
import {
  access,
  chmod,
  link,
  lstat,
  mkdir,
  mkdtemp,
  open,
  readlink,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  stat,
  statfs,
  writeFile,
} from "node:fs/promises";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";
import { createGunzip } from "node:zlib";

import {
  APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256,
  APPROVED_WORKER_RUNTIME_WORKER_DEPENDENCY_LOCK_SHA256,
} from "./studio-worker-runtime-registry.mjs";
import {
  assertDeterministicRuntimeTar,
  createDeterministicRuntimeTar,
} from "./studio-runtime-tar.mjs";
import {
  compareRuntimePathsByUtf8Bytes,
  decodeRuntimeUtf8,
  isPortableRuntimeRelativePath,
  parseRuntimeJsonBytes,
  runtimePathCollisionKey,
  sortPortableRuntimePaths,
} from "./studio-runtime-canonical.mjs";

export {
  APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256,
  APPROVED_WORKER_RUNTIME_WORKER_DEPENDENCY_LOCK_SHA256,
  assertDeterministicRuntimeTar,
};

const execFile = promisify(execFileCallback);
const DIGEST = /^[a-f0-9]{64}$/u;
const PACKET_PREFIX = "worker-runtime-materials-v1-sha256-";
const SUPPLEMENTAL_PACKET_PREFIX = "worker-runtime-build-materials-v1-sha256-";
const BUILDER_TOOL_PACKET_PREFIX = "worker-runtime-builder-tool-v1-sha256-";
const APPROVED_BASE_MATERIAL_PACKET_ID =
  "worker-runtime-materials-v1-sha256-8147590c4832c5ad9ac04b2b9a370dc5b55b61832334a745ada538c2b8de4895";
const APPROVED_MATERIAL_PACKET_ID =
  "worker-runtime-materials-v1-sha256-505b5f9ad6b9d009bea1c5f4ab8d5b7b5fb6513277a11985d114fcbd2669730f";
const APPROVED_SUPPLEMENTAL_PACKET_ID =
  "worker-runtime-build-materials-v1-sha256-0805bab07fa760a6edb7d78f703a1930896faca3a76965f68666620a07ddd693";
const PROPOSED_BUILDER_TOOL_PACKET_ID =
  "worker-runtime-builder-tool-v1-sha256-2fce99df62d12abd29243f2cdefc82a3f88dd42f777dbe6b16bf59df1ebdd11f";
const APPROVED_GITHUB_REDIRECT_POLICY_ID =
  "github-release-one-hop-v1-sha256-c1a15a5f32b42b20bf8d331b457c95f6c67e4527d051b4608497782bcd9cd920";
const APPROVED_GITHUB_ORIGINAL_URL =
  "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-aarch64-apple-darwin-install_only.tar.gz";
const APPROVED_GITHUB_REDIRECT_HOST = "release-assets.githubusercontent.com";
const APPROVED_GITHUB_REDIRECT_PATH =
  "/github-production-release-asset/162334160/0748cc26-9857-48e2-b4f4-9a76653fb5a1";
const APPROVED_PYTHON_SHA256 =
  "3ee3ee547cedfeb7c2b16b2b7156039f7b470bb8f857e226fd3d2eb11db83c76";
const APPROVED_PYTHON_BYTES = 25_135_464;
const RECEIPT_FIELDS = new Set([
  "schemaVersion",
  "packetId",
  "decision",
  "approvedBy",
  "approvedAt",
  "retrievalAuthorized",
  "buildCandidateAuthorized",
  "installAuthorized",
  "modelExecutionAuthorized",
]);
const REDIRECT_POLICY_FIELDS = new Set([
  "schemaVersion",
  "packetId",
  "material",
  "redirect",
  "authorization",
  "policyId",
  "status",
]);
const REDIRECT_APPROVAL_FIELDS = new Set([
  "schemaVersion",
  "policyId",
  "packetId",
  "decision",
  "approvedBy",
  "approvedAt",
  "retrievalAuthorized",
  "buildCandidateAuthorized",
  "installAuthorized",
  "modelExecutionAuthorized",
]);
const REQUIRED_UV_VERSION = "0.12.3";
const BUILDER_TOOL_EXECUTION_SECURITY_BOUNDARY = Object.freeze({
  inodePin: "opened-o_nofollow-handle-private-hardlink-dev-ino-sha256-pre-post",
  sameLoginAccountCompromise: "out-of-scope-local-mvp",
});
const APPROVED_BUILDER_TOOL_ARCHIVE_URL =
  "https://api.github.com/repos/astral-sh/uv/releases/assets/505387707";
const APPROVED_BUILDER_TOOL_ARCHIVE_FILENAME = "uv-aarch64-apple-darwin.tar.gz";
const APPROVED_BUILDER_TOOL_MEMBER_PATH = "uv-aarch64-apple-darwin/uv";
const APPROVED_BUILDER_TOOL_UNSELECTED_SIBLING_PATH = "uv-aarch64-apple-darwin/uvx";
const APPROVED_BUILDER_TOOL_REDIRECT_HOST = "release-assets.githubusercontent.com";
const APPROVED_BUILDER_TOOL_REDIRECT_PATH =
  "/github-production-release-asset/699532645/3433a4a2-db55-4d29-8976-5438ac5ee0e0";
const SANDBOX_EXECUTABLE = "/usr/bin/sandbox-exec";
const COMMAND_TIMEOUT_MS = 10 * 60 * 1000;
const TAR_TIMEOUT_MS = 2 * 60 * 1000;
const MAX_ARCHIVE_ENTRIES = 100_000;
const MAX_EXPANDED_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024;
const MAX_TEXT_SCAN_BYTES = 2 * 1024 * 1024;
const FIXED_TIME = "1970-01-01T00:00:00.000Z";
const SCENE_RUNTIME_MATERIALS = Object.freeze([Object.freeze({
  kind: "python-distribution",
  name: "platformdirs",
  version: "4.11.8",
  filename: "platformdirs-4.11.8-py3-none-any.whl",
  url: "https://files.pythonhosted.org/packages/f4/e1/5b7b8bbb55084d1425bcb9bc823ff519e1b2be05f6ebb0089e2eacc38413/platformdirs-4.11.8-py3-none-any.whl",
  sha256: "52f2f181bbfde907966932cc8312d967d02976422d66d537ea16092b8e291081",
  byteLength: 24_027,
  pythonTag: "py3",
  abiTag: "none",
  platformTag: "any",
  minimumOsVersion: null,
}), Object.freeze({
  kind: "python-distribution",
  name: "scenedetect",
  version: "0.7.1",
  filename: "scenedetect-0.7.1-py3-none-any.whl",
  url: "https://files.pythonhosted.org/packages/b8/a2/938555bc170ee504ecf33f328dc838b40c3b887d58bf4ee8beefb6c33fd8/scenedetect-0.7.1-py3-none-any.whl",
  sha256: "91b67902275b2e0d29a12f6d70435f04e0bd7852a6dc8a69c901c6069328dffa",
  byteLength: 146_194,
  pythonTag: "py3",
  abiTag: "none",
  platformTag: "any",
  minimumOsVersion: null,
})]);
const SCENE_RUNTIME_FIXED_VERSIONS = Object.freeze({
  click: "8.3.1",
  numpy: "2.4.1",
  "opencv-python": "4.13.0.90",
  platformdirs: "4.11.8",
  scenedetect: "0.7.1",
  tqdm: "4.67.1",
});
const SCENE_RUNTIME_REQUIRED_METADATA_NAMES = Object.freeze([
  "click",
  "numpy",
  "opencv-python",
  "platformdirs",
  "tqdm",
]);
const verifiedBuilderToolCapabilities = new WeakSet();

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

function sha256Bytes(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

async function sha256File(path) {
  const digest = createHash("sha256");
  for await (const chunk of createReadStream(path)) digest.update(chunk);
  return digest.digest("hex");
}

async function writeAll(handle, bytes) {
  let offset = 0;
  while (offset < bytes.length) {
    const { bytesWritten } = await handle.write(bytes, offset, bytes.length - offset, null);
    if (bytesWritten <= 0) fail("studio-runtime-file-write-failed");
    offset += bytesWritten;
  }
}

export function computeMaterialPacketId(packetOrCore) {
  const core = { ...packetOrCore };
  delete core.packetId;
  return `${PACKET_PREFIX}${sha256Bytes(canonicalJson(core))}`;
}

export function computeSupplementalMaterialPacketId(packetOrCore) {
  const core = { ...packetOrCore };
  delete core.packetId;
  return `${SUPPLEMENTAL_PACKET_PREFIX}${sha256Bytes(canonicalJson(core))}`;
}

export function computeBuilderToolPacketId(packetOrCore) {
  const core = { ...packetOrCore };
  delete core.packetId;
  return `${BUILDER_TOOL_PACKET_PREFIX}${sha256Bytes(canonicalJson(core))}`;
}

function exactFields(value, fields) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).length === fields.size
    && Object.keys(value).every((key) => fields.has(key));
}

function safeFilename(value) {
  return typeof value === "string"
    && value.length > 0
    && value === basename(value)
    && value !== "."
    && value !== ".."
    && !value.startsWith(".")
    && !/[\u0000-\u001f\u007f\\/]/u.test(value);
}

function releaseParts(value) {
  if (typeof value !== "string" || !/^\d+(?:\.\d+)*$/u.test(value)) return null;
  return value.split(".").map((part) => Number.parseInt(part, 10));
}

function compareRelease(left, right) {
  for (let index = 0; index < Math.max(left.length, right.length); index += 1) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference !== 0) return difference < 0 ? -1 : 1;
  }
  return 0;
}

function requirementAllowsVersion(version, specifier) {
  if (!specifier) return true;
  const actual = releaseParts(version);
  if (!actual) return false;
  return specifier.split(",").every((rawClause) => {
    const clause = rawClause.trim();
    const match = /^(~=|==|!=|<=|>=|<|>)\s*(\d+(?:\.\d+)*)$/u.exec(clause);
    if (!match) fail("studio-runtime-scene-requirement-malformed", clause);
    const [, operator, expectedText] = match;
    const expected = releaseParts(expectedText);
    const comparison = compareRelease(actual, expected);
    if (operator === "==") return comparison === 0;
    if (operator === "!=") return comparison !== 0;
    if (operator === ">=") return comparison >= 0;
    if (operator === ">") return comparison > 0;
    if (operator === "<=") return comparison <= 0;
    if (operator === "<") return comparison < 0;
    const upper = expected.slice(0, Math.max(1, expected.length - 1));
    upper[upper.length - 1] += 1;
    return comparison >= 0 && compareRelease(actual, upper) < 0;
  });
}

function metadataRequirements(text) {
  if (typeof text !== "string" || text.length === 0 || text.includes("\0")) {
    fail("studio-runtime-scene-metadata-missing");
  }
  const logicalLines = [];
  for (const line of text.replaceAll("\r\n", "\n").split("\n")) {
    if (/^[ \t]/u.test(line)) {
      if (logicalLines.length === 0) fail("studio-runtime-scene-requirement-malformed");
      logicalLines[logicalLines.length - 1] += line.trim();
    } else {
      logicalLines.push(line);
    }
  }
  const requirements = [];
  for (const line of logicalLines) {
    if (/^Requires-Dist/iu.test(line) && !/^Requires-Dist:\s*\S/iu.test(line)) {
      fail("studio-runtime-scene-requirement-malformed");
    }
    const value = /^Requires-Dist:\s*(.+)$/iu.exec(line)?.[1]?.trim();
    if (!value) continue;
    const separator = value.indexOf(";");
    const requirementText = (separator === -1 ? value : value.slice(0, separator)).trim();
    const markerText = separator === -1 ? null : value.slice(separator + 1).trim();
    if (markerText !== null) {
      const marker = /^extra\s*==\s*(["'])([A-Za-z0-9][A-Za-z0-9._-]*)\1$/u.exec(markerText);
      if (!marker) fail("studio-runtime-scene-requirement-malformed", value);
      continue;
    }
    const match = /^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9._,-]+\])?\s*(?:\(([^)]+)\)|((?:~=|==|!=|<=|>=|<|>).+))?$/u.exec(requirementText);
    if (!match) fail("studio-runtime-scene-requirement-malformed", value);
    requirements.push({
      name: normalizedName(match[1]),
      specifier: (match[2] ?? match[3] ?? "").trim(),
    });
  }
  return requirements;
}

export function validateSceneRuntimeMetadata(installedPackages, sceneMetadata, profilePacket) {
  if (!Array.isArray(installedPackages)) fail("studio-runtime-scene-inventory-invalid");
  const installed = new Map();
  for (const item of installedPackages) {
    const key = typeof item?.name === "string" ? normalizedName(item.name) : "";
    if (!key || typeof item?.version !== "string" || !item.version) {
      fail("studio-runtime-scene-inventory-invalid");
    }
    if (installed.has(key)) fail("studio-runtime-scene-package-ambiguous", key);
    installed.set(key, item.version);
  }
  let fixed = SCENE_RUNTIME_FIXED_VERSIONS;
  let headless = false;
  if (profilePacket?.runtimeProfileId !== undefined) {
    const profile = phoneRuntimeProfileDefinition();
    if (!profile || profilePacket.runtimeProfileId !== profile.runtimeProfileId
      || profilePacket.workerDependencyLockSha256 !== profile.workerDependencyLockSha256
      || profilePacket.dependencyLockSha256 !== profile.dependencyLockSha256) {
      fail("studio-runtime-scene-profile-invalid");
    }
    const graphBytes = readFileSync(new URL("./runtime-profiles/phone-editorial-v1/dependency.lock.json", import.meta.url));
    if (createHash("sha256").update(graphBytes).digest("hex") !== profile.dependencyLockSha256) {
      fail("studio-runtime-scene-profile-invalid");
    }
    const graph = JSON.parse(graphBytes);
    fixed = Object.fromEntries(graph.packages.map(item => [normalizedName(item.name), item.version]));
    if (installed.size !== Object.keys(fixed).length || installed.has("opencv-python")
      || !installed.has("opencv-python-headless")) fail("studio-runtime-scene-profile-inventory-invalid");
    headless = true;
  } else if (profilePacket?.workerDependencyLockSha256 !== undefined
    && profilePacket.workerDependencyLockSha256 !== APPROVED_WORKER_RUNTIME_WORKER_DEPENDENCY_LOCK_SHA256) {
    fail("studio-runtime-scene-profile-invalid");
  }
  for (const [name, expectedVersion] of Object.entries(fixed)) {
    if (installed.get(name) !== expectedVersion) {
      fail("studio-runtime-scene-dependency-incompatible", name);
    }
  }
  const requirements = metadataRequirements(sceneMetadata);
  const seen = new Set();
  for (const requirement of requirements) {
    if (seen.has(requirement.name)) {
      fail("studio-runtime-scene-requirement-ambiguous", requirement.name);
    }
    seen.add(requirement.name);
    // This explicit substitution is limited to the SHA-bound phone graph.
    // The actual distribution name remains headless in the inventory.
    const installedVersion = installed.get(headless && requirement.name === "opencv-python"
      ? "opencv-python-headless" : requirement.name);
    if (!installedVersion || !requirementAllowsVersion(installedVersion, requirement.specifier)) {
      fail("studio-runtime-scene-dependency-incompatible", requirement.name);
    }
  }
  for (const name of SCENE_RUNTIME_REQUIRED_METADATA_NAMES) {
    if (!seen.has(name)) fail("studio-runtime-scene-requirement-missing", name);
  }
  return Object.fromEntries(Object.entries(fixed));
}

export function validateSceneMaterialExtension(packet) {
  if (!packet || typeof packet !== "object" || Array.isArray(packet)) {
    fail("studio-runtime-scene-material-packet-invalid");
  }
  if (computeMaterialPacketId(packet) !== packet.packetId) {
    fail("studio-runtime-material-packet-id-mismatch");
  }
  if (
    packet.workerDependencyLockSha256
      !== APPROVED_WORKER_RUNTIME_WORKER_DEPENDENCY_LOCK_SHA256
  ) fail("studio-runtime-worker-lock-mismatch");
  if (
    !Number.isSafeInteger(packet.baseMaterialCount)
    || packet.baseMaterialCount <= 0
    || !Array.isArray(packet.materials)
    || packet.materials.length !== packet.baseMaterialCount + SCENE_RUNTIME_MATERIALS.length
  ) fail("studio-runtime-scene-delta-invalid");
  const prefix = packet.materials.slice(0, packet.baseMaterialCount);
  if (prefix.some((item) => ["platformdirs", "scenedetect"].includes(normalizedName(item?.name ?? "")))) {
    fail("studio-runtime-scene-delta-invalid");
  }
  const delta = packet.materials.slice(packet.baseMaterialCount);
  if (canonicalJson(delta) !== canonicalJson(SCENE_RUNTIME_MATERIALS)) {
    fail("studio-runtime-scene-delta-invalid");
  }
  const {
    packetId: _packetId,
    parentMaterialPacketId,
    baseMaterialCount: _baseMaterialCount,
    workerDependencyLockSha256: _workerDependencyLockSha256,
    ...inherited
  } = packet;
  const baseCore = {
    ...inherited,
    expectedTotalDownloadBytes: prefix.reduce((total, item) => total + item.byteLength, 0),
    materials: prefix,
  };
  if (computeMaterialPacketId(baseCore) !== parentMaterialPacketId) {
    fail("studio-runtime-parent-material-mismatch");
  }
  return packet;
}

export function runtimeDependencyLockBindings(packet) {
  if (packet?.dependencyLockSha256 !== APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256) {
    fail("studio-runtime-lock-mismatch");
  }
  const hasSceneExtension = [
    "parentMaterialPacketId",
    "baseMaterialCount",
    "workerDependencyLockSha256",
  ].some((field) => Object.hasOwn(packet, field));
  if (hasSceneExtension) validateSceneMaterialExtension(packet);
  return {
    dependencyLockSha256: packet.dependencyLockSha256,
    workerDependencyLockSha256: packet.workerDependencyLockSha256 ?? null,
  };
}

function validatePacket(packet) {
  if (!packet || typeof packet !== "object" || Array.isArray(packet)) {
    fail("studio-runtime-material-packet-invalid");
  }
  if (computeMaterialPacketId(packet) !== packet.packetId) {
    fail("studio-runtime-material-packet-id-mismatch");
  }
  if (
    packet.schemaVersion !== 1
    || packet.status !== "awaiting-exact-retrieval-approval"
    || packet.approved !== false
    || packet.installAuthorized !== false
    || packet.target?.pythonVersionRange !== ">=3.12.0 <3.13"
    || packet.target?.platform !== "darwin"
    || packet.target?.architecture !== "arm64"
    || !Array.isArray(packet.materials)
    || packet.materials.length === 0
  ) fail("studio-runtime-material-packet-invalid");
  if (packet.dependencyLockSha256 !== APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256) {
    fail("studio-runtime-lock-mismatch");
  }
  const filenames = new Set();
  const urls = new Set();
  let expectedTotal = 0;
  for (const item of packet.materials) {
    if (
      !["python-runtime", "source-package", "python-distribution"].includes(item?.kind)
      || typeof item.name !== "string"
      || !item.name
      || typeof item.version !== "string"
      || !item.version
      || !safeFilename(item.filename)
      || !DIGEST.test(item.sha256 ?? "")
      || !Number.isSafeInteger(item.byteLength)
      || item.byteLength <= 0
    ) fail("studio-runtime-material-packet-invalid");
    let parsed;
    try {
      parsed = new URL(item.url);
    } catch {
      fail("studio-runtime-material-packet-invalid");
    }
    if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.hash) {
      fail("studio-runtime-material-packet-invalid");
    }
    if (filenames.has(item.filename) || urls.has(parsed.href)) {
      fail("studio-runtime-material-packet-invalid");
    }
    filenames.add(item.filename);
    urls.add(parsed.href);
    expectedTotal += item.byteLength;
  }
  if (packet.expectedTotalDownloadBytes !== expectedTotal) {
    fail("studio-runtime-material-packet-invalid");
  }
  if (packet.materials.filter((item) => item.kind === "python-runtime").length !== 1) {
    fail("studio-runtime-material-packet-invalid");
  }
  if (packet.materials.filter((item) => item.kind === "source-package").length !== 1) {
    fail("studio-runtime-material-packet-invalid");
  }
  const sceneExtensionFields = [
    "parentMaterialPacketId",
    "baseMaterialCount",
    "workerDependencyLockSha256",
  ];
  if (sceneExtensionFields.some((field) => Object.hasOwn(packet, field))) {
    validateSceneMaterialExtension(packet);
  }
  return packet;
}

function validateApprovalReceipt(packet, receipt) {
  if (
    !exactFields(receipt, RECEIPT_FIELDS)
    || receipt.schemaVersion !== 1
    || receipt.packetId !== packet.packetId
    || receipt.decision !== "approve-exact-retrieval"
    || receipt.approvedBy !== "human-user"
    || typeof receipt.approvedAt !== "string"
    || !Number.isFinite(Date.parse(receipt.approvedAt))
    || receipt.retrievalAuthorized !== true
    || receipt.buildCandidateAuthorized !== true
    || receipt.installAuthorized !== false
    || receipt.modelExecutionAuthorized !== false
  ) fail("studio-runtime-material-approval-invalid");
  return receipt;
}

function validateAuthorizedInputs(packet, approvalReceipt) {
  const validated = validatePacket(packet);
  validateApprovalReceipt(validated, approvalReceipt);
  return validated;
}

function validateSupplementalPacket(packet) {
  if (!packet || typeof packet !== "object" || Array.isArray(packet)) {
    fail("studio-runtime-supplemental-packet-invalid");
  }
  if (
    packet.packetId !== APPROVED_SUPPLEMENTAL_PACKET_ID
    || computeSupplementalMaterialPacketId(packet) !== packet.packetId
    || packet.schemaVersion !== 1
    || packet.status !== "awaiting-exact-supplemental-retrieval-approval"
    || packet.approved !== false
    || packet.retrievalAuthorized !== false
    || packet.installAuthorized !== false
    || packet.modelExecutionAuthorized !== false
    || packet.parentMaterialPacketId !== APPROVED_BASE_MATERIAL_PACKET_ID
    || packet.approvedDependencyLockSha256 !== APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256
    || packet.target?.pythonVersion !== "3.12.14"
    || packet.target?.platform !== "darwin"
    || packet.target?.architecture !== "arm64"
    || !Array.isArray(packet.materials)
    || packet.materials.length !== 2
  ) fail("studio-runtime-supplemental-packet-invalid");
  const expectedNames = new Set(["setuptools", "wheel"]);
  const filenames = new Set();
  let expectedTotal = 0;
  for (const material of packet.materials) {
    let parsed;
    try { parsed = new URL(material?.url); } catch {
      fail("studio-runtime-supplemental-packet-invalid");
    }
    if (
      material.kind !== "build-dependency-wheel"
      || !expectedNames.delete(material.name)
      || !safeFilename(material.filename)
      || filenames.has(material.filename)
      || !DIGEST.test(material.sha256 ?? "")
      || !Number.isSafeInteger(material.byteLength)
      || material.byteLength <= 0
      || parsed.protocol !== "https:"
      || parsed.hostname !== "files.pythonhosted.org"
      || parsed.username
      || parsed.password
      || parsed.search
      || parsed.hash
    ) fail("studio-runtime-supplemental-packet-invalid");
    filenames.add(material.filename);
    expectedTotal += material.byteLength;
  }
  if (expectedNames.size !== 0 || packet.expectedTotalDownloadBytes !== expectedTotal) {
    fail("studio-runtime-supplemental-packet-invalid");
  }
  return packet;
}

function validateSupplementalApprovalReceipt(packet, receipt) {
  if (
    !exactFields(receipt, RECEIPT_FIELDS)
    || receipt.schemaVersion !== 1
    || receipt.packetId !== packet.packetId
    || receipt.decision !== "approve-exact-supplemental-build-material-packet"
    || receipt.approvedBy !== "human-user"
    || typeof receipt.approvedAt !== "string"
    || !Number.isFinite(Date.parse(receipt.approvedAt))
    || receipt.retrievalAuthorized !== true
    || receipt.buildCandidateAuthorized !== true
    || receipt.installAuthorized !== false
    || receipt.modelExecutionAuthorized !== false
  ) fail("studio-runtime-supplemental-approval-invalid");
  return receipt;
}

export function validateSupplementalAuthorizedInputs(packet, approvalReceipt) {
  const validated = validateSupplementalPacket(packet);
  validateSupplementalApprovalReceipt(validated, approvalReceipt);
  return validated;
}

const BUILDER_TOOL_RECEIPT_FIELDS = new Set([
  "schemaVersion", "packetId", "decision", "approvedBy", "approvedAt",
  "retrievalAuthorized", "candidateBuildUseAuthorized", "installAuthorized",
  "modelsLockMutationAuthorized", "modelExecutionAuthorized", "publishAuthorized",
  "deployAuthorized",
]);

export function validateBuilderToolAuthorizedInputs(parentPacket, packet, receipt) {
  const approvedToolParentPacketId = parentPacket?.packetId === APPROVED_MATERIAL_PACKET_ID
    ? parentPacket.parentMaterialPacketId
    : parentPacket?.packetId;
  if (
    !packet || typeof packet !== "object" || Array.isArray(packet)
    || computeBuilderToolPacketId(packet) !== packet.packetId
    || packet.schemaVersion !== 1
    || packet.status !== "awaiting-exact-builder-tool-approval"
    || packet.approved !== false
    || packet.parentMaterialPacketId !== approvedToolParentPacketId
    || (parentPacket?.packetId === APPROVED_MATERIAL_PACKET_ID
      && approvedToolParentPacketId !== APPROVED_BASE_MATERIAL_PACKET_ID)
    || packet.target?.platform !== "darwin" || packet.target?.architecture !== "arm64"
    || packet.tool?.name !== "uv" || packet.tool?.version !== REQUIRED_UV_VERSION
    || !Number.isSafeInteger(packet.tool?.executable?.byteLength) || packet.tool.executable.byteLength <= 0
    || !DIGEST.test(packet.tool?.executable?.sha256 ?? "")
    || packet.tool.executable.memberPath !== APPROVED_BUILDER_TOOL_MEMBER_PATH
    || packet.tool.executable.officialMatchState !== "unverified-until-approved-archive-retrieval"
    || !DIGEST.test(packet.tool?.archive?.sha256 ?? "")
    || !Number.isSafeInteger(packet.tool?.archive?.byteLength) || packet.tool.archive.byteLength <= 0
    || packet.tool.archive.filename !== APPROVED_BUILDER_TOOL_ARCHIVE_FILENAME
    || packet.tool.archive.url !== APPROVED_BUILDER_TOOL_ARCHIVE_URL
    || canonicalJson(packet.redirectPolicy) !== canonicalJson({
      originalUrl: APPROVED_BUILDER_TOOL_ARCHIVE_URL,
      statusCode: 302,
      maximumHops: 1,
      targetScheme: "https:",
      targetHost: APPROVED_BUILDER_TOOL_REDIRECT_HOST,
      targetPath: APPROVED_BUILDER_TOOL_REDIRECT_PATH,
      allowEphemeralSignedQuery: true,
      forwardAuthorization: false,
      forwardCookies: false,
      allowSecondRedirect: false,
    })
    || packet.authorization?.retrievalAuthorized !== false
    || packet.authorization?.candidateBuildUseAuthorized !== false
    || packet.authorization?.installAuthorized !== false
    || packet.authorization?.modelsLockMutationAuthorized !== false
    || packet.authorization?.modelExecutionAuthorized !== false
  ) fail("studio-runtime-builder-tool-packet-invalid");
  if (
    !exactFields(receipt, BUILDER_TOOL_RECEIPT_FIELDS)
    || receipt.schemaVersion !== 1 || receipt.packetId !== packet.packetId
    || receipt.decision !== "approve-exact-worker-runtime-builder-tool-packet"
    || receipt.approvedBy !== "human-user" || !Number.isFinite(Date.parse(receipt.approvedAt))
    || receipt.retrievalAuthorized !== true || receipt.candidateBuildUseAuthorized !== true
    || receipt.installAuthorized !== false || receipt.modelsLockMutationAuthorized !== false
    || receipt.modelExecutionAuthorized !== false || receipt.publishAuthorized !== false
    || receipt.deployAuthorized !== false
  ) fail("studio-runtime-builder-tool-approval-invalid");
  return packet;
}

function tarText(block, start, length) {
  const bytes = block.subarray(start, start + length);
  const end = bytes.indexOf(0);
  try {
    return decodeRuntimeUtf8(bytes.subarray(0, end === -1 ? bytes.length : end));
  } catch {
    fail("studio-runtime-source-archive-invalid");
  }
}

function tarOctal(block, start, length) {
  const bytes = block.subarray(start, start + length);
  if ((bytes[0] & 0x80) !== 0) fail("studio-runtime-source-archive-invalid");
  const value = bytes.toString("ascii").replace(/\0.*$/su, "").trim();
  if (!/^[0-7]*$/u.test(value)) fail("studio-runtime-source-archive-invalid");
  const parsed = value ? Number.parseInt(value, 8) : 0;
  if (!Number.isSafeInteger(parsed) || parsed < 0) fail("studio-runtime-source-archive-invalid");
  return parsed;
}

function safeTarPath(value) {
  const normalized = value.replace(/^\.\//u, "").replace(/\/+$/u, "");
  return normalized.length > 0
    && !normalized.startsWith("/")
    && !normalized.includes("\\")
    && !normalized.includes("\0")
    && !normalized.split("/").includes("..")
    && !normalized.split("/").includes("")
    ? normalized
    : null;
}

function validateTarChecksum(header) {
  const recorded = tarOctal(header, 148, 8);
  let sum = 0;
  for (let index = 0; index < header.length; index += 1) {
    sum += index >= 148 && index < 156 ? 0x20 : header[index];
  }
  if (recorded !== sum) fail("studio-runtime-source-archive-invalid");
}

function validatePaxBody(bytes, { portableRuntimePaths = false } = {}) {
  let offset = 0;
  while (offset < bytes.length) {
    const space = bytes.indexOf(0x20, offset);
    if (space < 0) fail("studio-runtime-source-archive-invalid");
    const lengthText = bytes.subarray(offset, space).toString("ascii");
    if (!/^[1-9][0-9]*$/u.test(lengthText)) fail("studio-runtime-source-archive-invalid");
    const length = Number(lengthText);
    if (!Number.isSafeInteger(length) || length <= 0 || offset + length > bytes.length) {
      fail("studio-runtime-source-archive-invalid");
    }
    const record = bytes.subarray(space + 1, offset + length);
    if (record.at(-1) !== 0x0a) fail("studio-runtime-source-archive-invalid");
    const equal = record.indexOf(0x3d);
    if (equal <= 0) fail("studio-runtime-source-archive-invalid");
    let key;
    let value;
    try {
      key = decodeRuntimeUtf8(record.subarray(0, equal));
      value = decodeRuntimeUtf8(record.subarray(equal + 1, -1));
    } catch {
      fail("studio-runtime-source-archive-invalid");
    }
    if (["mtime", "atime", "ctime"].includes(key)) {
      if (!/^-?[0-9]+(?:\.[0-9]+)?$/u.test(value)) fail("studio-runtime-source-archive-invalid");
    } else if (portableRuntimePaths && key === "path") {
      const path = safeTarPath(value);
      const strippedPath = path?.split("/").slice(1).join("/");
      if (!strippedPath || !isPortableRuntimeRelativePath(strippedPath)) {
        fail("studio-runtime-path-invalid", strippedPath ?? value);
      }
      fail("studio-runtime-source-archive-invalid", "unsupported-pax-key:path");
    } else if (key !== "comment") {
      fail("studio-runtime-source-archive-invalid", `unsupported-pax-key:${key}`);
    }
    offset += length;
  }
}

export async function preflightTarGz(archivePath, {
  maxEntries = MAX_ARCHIVE_ENTRIES,
  maxExpandedBytes = MAX_EXPANDED_ARCHIVE_BYTES,
  exactMemberPath,
  extractMemberTo,
  builderToolArchive = false,
  portableRuntimePaths = false,
} = {}) {
  if (!Number.isSafeInteger(maxEntries) || maxEntries <= 0 || !Number.isSafeInteger(maxExpandedBytes) || maxExpandedBytes <= 0) {
    fail("studio-runtime-source-archive-limits-invalid");
  }
  const streamLimit = maxExpandedBytes + (maxEntries * 1024) + 1024;
  const iterator = createReadStream(archivePath).pipe(createGunzip())[Symbol.asyncIterator]();
  let buffered = Buffer.alloc(0);
  let streamBytes = 0;
  async function fill(length) {
    while (buffered.length < length) {
      const next = await iterator.next();
      if (next.done) return false;
      const chunk = Buffer.from(next.value);
      streamBytes += chunk.length;
      if (streamBytes > streamLimit) fail("studio-runtime-source-archive-limits-exceeded");
      buffered = buffered.length === 0 ? chunk : Buffer.concat([buffered, chunk]);
    }
    return true;
  }
  async function take(length) {
    if (!await fill(length)) fail("studio-runtime-source-archive-invalid");
    const value = buffered.subarray(0, length);
    buffered = buffered.subarray(length);
    return value;
  }
  async function consume(length, onChunk) {
    let remaining = length;
    while (remaining > 0) {
      if (buffered.length === 0 && !await fill(1)) fail("studio-runtime-source-archive-invalid");
      const count = Math.min(remaining, buffered.length);
      const value = buffered.subarray(0, count);
      buffered = buffered.subarray(count);
      if (onChunk) await onChunk(value);
      remaining -= count;
    }
  }
  let entries = 0;
  let expandedBytes = 0;
  let zeroBlocks = 0;
  let pendingPax = false;
  let member = null;
  const paths = new Set();
  const symbolicLinks = new Set();
  const strippedSymbolicLinks = new Set();
  let memberHandle;
  try {
    while (true) {
      const header = await take(512);
      if (header.every((byte) => byte === 0)) {
        zeroBlocks += 1;
        if (zeroBlocks === 2) break;
        continue;
      }
      if (zeroBlocks !== 0) fail("studio-runtime-source-archive-invalid");
      validateTarChecksum(header);
      const magic = header.subarray(257, 263).toString("binary");
      if (magic !== "ustar\0" && magic !== "ustar ") fail("studio-runtime-source-archive-invalid");
      const name = tarText(header, 0, 100);
      const prefix = tarText(header, 345, 155);
      const path = safeTarPath(prefix ? `${prefix}/${name}` : name);
      const type = tarText(header, 156, 1) || "0";
      const size = tarOctal(header, 124, 12);
      const mode = tarOctal(header, 100, 8);
      if (!path) fail("studio-runtime-source-archive-invalid");
      entries += 1;
      if (entries > maxEntries) fail("studio-runtime-source-archive-limits-exceeded");
      if (type === "x" || type === "g") {
        if (pendingPax || size > 64 * 1024) fail("studio-runtime-source-archive-invalid");
        const body = await take(size);
        validatePaxBody(body, { portableRuntimePaths });
        await consume((512 - (size % 512)) % 512);
        pendingPax = type === "x";
        continue;
      }
      if (type === "L" || type === "K" || type === "S") fail("studio-runtime-source-archive-invalid");
      if (!new Set(["0", "5", "2"]).has(type)) fail("studio-runtime-source-archive-invalid");
      if (builderToolArchive && !new Set(["0", "5"]).has(type)) fail("studio-runtime-builder-tool-archive-invalid");
      if (paths.has(path)) fail(builderToolArchive ? "studio-runtime-builder-tool-archive-invalid" : "studio-runtime-source-archive-invalid");
      const pathParts = path.split("/");
      const strippedPath = pathParts.slice(1).join("/");
      if (portableRuntimePaths && strippedPath && !isPortableRuntimeRelativePath(strippedPath)) {
        fail("studio-runtime-path-invalid", strippedPath);
      }
      for (let index = 1; index < pathParts.length; index += 1) {
        if (symbolicLinks.has(pathParts.slice(0, index).join("/"))) fail("studio-runtime-source-archive-invalid");
      }
      const strippedParts = strippedPath ? strippedPath.split("/") : [];
      for (let index = 1; index < strippedParts.length; index += 1) {
        if (strippedSymbolicLinks.has(strippedParts.slice(0, index).join("/"))) {
          fail("studio-runtime-source-archive-invalid");
        }
      }
      if (type === "2") {
        const linkTarget = tarText(header, 157, 100);
        if (!linkTarget || isAbsolute(linkTarget) || linkTarget.includes("\\") || linkTarget.includes("\0")) {
          fail("studio-runtime-source-archive-invalid");
        }
        const topLevel = pathParts[0];
        const resolvedTarget = resolve("/", dirname(path), linkTarget);
        if (resolvedTarget !== `/${topLevel}` && !resolvedTarget.startsWith(`/${topLevel}/`)) {
          fail("studio-runtime-source-archive-invalid");
        }
        if (!strippedPath) fail("studio-runtime-source-archive-invalid");
        const strippedTarget = resolve("/destination", dirname(strippedPath), linkTarget);
        if (strippedTarget !== "/destination" && !strippedTarget.startsWith("/destination/")) {
          fail("studio-runtime-source-archive-invalid");
        }
        const resolvedRuntimeTarget = relative("/destination", strippedTarget).split(sep).join("/");
        if (portableRuntimePaths && !isPortableRuntimeRelativePath(resolvedRuntimeTarget)) {
          fail("studio-runtime-path-invalid", resolvedRuntimeTarget);
        }
        if ([...paths].some((existing) => existing.startsWith(`${path}/`))) fail("studio-runtime-source-archive-invalid");
        symbolicLinks.add(path);
        strippedSymbolicLinks.add(strippedPath);
      }
      paths.add(path);
      pendingPax = false;
      if (type === "0") {
        expandedBytes += size;
        if (expandedBytes > maxExpandedBytes) fail("studio-runtime-source-archive-limits-exceeded");
      } else if (size !== 0) fail("studio-runtime-source-archive-invalid");
      if (
        builderToolArchive
        && type === "0"
        && (mode & 0o111) !== 0
        && path !== exactMemberPath
        && !(
          exactMemberPath === APPROVED_BUILDER_TOOL_MEMBER_PATH
          && path === APPROVED_BUILDER_TOOL_UNSELECTED_SIBLING_PATH
        )
      ) {
        fail("studio-runtime-builder-tool-archive-invalid");
      }
      if (path === exactMemberPath) {
        if (type !== "0" || member) fail("studio-runtime-builder-tool-archive-invalid");
        member = { path, byteLength: size, mode };
        if (extractMemberTo) memberHandle = await open(extractMemberTo, "wx", 0o600);
      }
      await consume(size, memberHandle ? async (chunk) => writeAll(memberHandle, chunk) : undefined);
      if (memberHandle) {
        await memberHandle.sync();
        await memberHandle.close();
        memberHandle = undefined;
      }
      await consume((512 - (size % 512)) % 512);
    }
    if (pendingPax || (exactMemberPath && !member)) fail(builderToolArchive ? "studio-runtime-builder-tool-archive-invalid" : "studio-runtime-source-archive-invalid");
    while (await fill(1)) {
      if (!buffered.every((byte) => byte === 0)) fail("studio-runtime-source-archive-invalid");
      buffered = Buffer.alloc(0);
    }
    return { entries, expandedBytes, member };
  } catch (error) {
    if (builderToolArchive && error?.message?.startsWith("studio-runtime-source-archive-")) {
      fail("studio-runtime-builder-tool-archive-invalid");
    }
    throw error;
  } finally {
    await memberHandle?.close().catch(() => {});
  }
}

function redirectPolicyCore(policy) {
  const core = { ...policy };
  delete core.policyId;
  delete core.status;
  return core;
}

export function computeGithubRedirectPolicyId(policy) {
  return `github-release-one-hop-v1-sha256-${sha256Bytes(canonicalJson(redirectPolicyCore(policy)))}`;
}

function validateGithubRedirectPolicy(packetId, material, policy, approval) {
  if (
    !exactFields(policy, REDIRECT_POLICY_FIELDS)
    || policy.schemaVersion !== 1
    || policy.packetId !== APPROVED_BASE_MATERIAL_PACKET_ID
    || policy.packetId !== packetId
    || policy.policyId !== APPROVED_GITHUB_REDIRECT_POLICY_ID
    || computeGithubRedirectPolicyId(policy) !== policy.policyId
    || policy.status !== "awaiting-exact-redirect-approval"
    || canonicalJson(policy.material) !== canonicalJson({
      name: "cpython",
      version: "3.12.14+20260901",
      originalUrl: APPROVED_GITHUB_ORIGINAL_URL,
      byteLength: APPROVED_PYTHON_BYTES,
      sha256: APPROVED_PYTHON_SHA256,
    })
    || canonicalJson(policy.redirect) !== canonicalJson({
      statusCode: 302,
      maximumHops: 1,
      targetScheme: "https:",
      targetHost: APPROVED_GITHUB_REDIRECT_HOST,
      targetPath: APPROVED_GITHUB_REDIRECT_PATH,
      allowEphemeralSignedQuery: true,
      forwardAuthorization: false,
      forwardCookies: false,
      allowSecondRedirect: false,
    })
    || canonicalJson(policy.authorization) !== canonicalJson({
      retrievalOnly: true,
      buildCandidateAuthorized: true,
      installAuthorized: false,
      modelExecutionAuthorized: false,
    })
    || material?.kind !== "python-runtime"
    || material.name !== "cpython"
    || material.version !== "3.12.14"
    || material.build !== "20260901"
    || material.target !== "aarch64-apple-darwin"
    || material.url !== APPROVED_GITHUB_ORIGINAL_URL
    || material.byteLength !== APPROVED_PYTHON_BYTES
    || material.sha256 !== APPROVED_PYTHON_SHA256
  ) fail("studio-runtime-github-redirect-policy-invalid");
  if (
    !exactFields(approval, REDIRECT_APPROVAL_FIELDS)
    || approval.schemaVersion !== 1
    || approval.policyId !== policy.policyId
    || approval.packetId !== packetId
    || approval.decision !== "approve-verified-github-release-one-hop-redirect"
    || approval.approvedBy !== "human-user"
    || typeof approval.approvedAt !== "string"
    || !Number.isFinite(Date.parse(approval.approvedAt))
    || approval.retrievalAuthorized !== true
    || approval.buildCandidateAuthorized !== true
    || approval.installAuthorized !== false
    || approval.modelExecutionAuthorized !== false
  ) fail("studio-runtime-github-redirect-approval-invalid");
  return policy;
}

async function pathExists(path) {
  try {
    await access(path);
    return true;
  } catch (error) {
    if (error?.code === "ENOENT") return false;
    throw error;
  }
}

async function responseForDirectMaterial(material, fetchImpl) {
  let response;
  try {
    response = await fetchImpl(material.url, {
      method: "GET",
      redirect: "manual",
      credentials: "omit",
      signal: AbortSignal.timeout(120_000),
      headers: { "User-Agent": "wearable-phase-a-runtime-builder" },
    });
  } catch {
    fail("studio-runtime-material-download-failed", material.filename);
  }
  if (response?.redirected === true || (response?.status >= 300 && response.status < 400)) {
    const location = response?.headers?.get("location") ?? "<missing>";
    let identity = location;
    try { identity = new URL(location, material.url).href; } catch {}
    fail("studio-runtime-redirect-rejected", `${material.url}->${identity}`);
  }
  if (!response?.ok || !response.body || response.redirected === true) {
    fail("studio-runtime-material-download-failed", material.filename);
  }
  const declared = response.headers.get("content-length");
  if (declared !== null && Number(declared) !== material.byteLength) {
    fail("studio-runtime-material-size-mismatch", material.filename);
  }
  return response;
}

async function writeExactResponse(response, material, destinationPath) {
  const declared = response.headers.get("content-length");
  if (declared !== null && Number(declared) !== material.byteLength) {
    fail("studio-runtime-material-size-mismatch", material.filename);
  }
  let handle;
  try {
    handle = await open(destinationPath, "wx", 0o600);
    const digest = createHash("sha256");
    let byteLength = 0;
    for await (const chunk of response.body) {
      const bytes = Buffer.from(chunk);
      byteLength += bytes.length;
      if (byteLength > material.byteLength) {
        fail("studio-runtime-material-size-mismatch", material.filename);
      }
      digest.update(bytes);
      await handle.write(bytes);
    }
    await handle.sync();
    await handle.close();
    handle = undefined;
    if (byteLength !== material.byteLength) {
      fail("studio-runtime-material-size-mismatch", material.filename);
    }
    if (digest.digest("hex") !== material.sha256) {
      fail("studio-runtime-material-digest-mismatch", material.filename);
    }
    await chmod(destinationPath, 0o600);
    return { byteLength, sha256: material.sha256 };
  } catch (error) {
    if (handle) await handle.close().catch(() => {});
    await rm(destinationPath, { force: true }).catch(() => {});
    throw error;
  }
}

async function requestBuilderTool(url, fetchImpl) {
  try {
    return await fetchImpl(url, {
      method: "GET",
      redirect: "manual",
      credentials: "omit",
      signal: AbortSignal.timeout(120_000),
      headers: {
        Accept: "application/octet-stream",
        "User-Agent": "wearable-phase-a-runtime-builder-tool",
        "X-GitHub-Api-Version": "2022-11-28",
      },
    });
  } catch {
    fail("studio-runtime-builder-tool-download-failed");
  }
}

async function writeExactBuilderToolArchive(response, archive, destinationPath) {
  const declared = response.headers.get("content-length");
  if (declared !== null && Number(declared) !== archive.byteLength) {
    fail("studio-runtime-builder-tool-archive-size-mismatch");
  }
  let handle;
  try {
    handle = await open(destinationPath, "wx", 0o600);
    const digest = createHash("sha256");
    let byteLength = 0;
    for await (const chunk of response.body) {
      const bytes = Buffer.from(chunk);
      byteLength += bytes.length;
      if (byteLength > archive.byteLength) fail("studio-runtime-builder-tool-archive-size-mismatch");
      digest.update(bytes);
      await writeAll(handle, bytes);
    }
    await handle.sync();
    await handle.close();
    handle = undefined;
    if (byteLength !== archive.byteLength) fail("studio-runtime-builder-tool-archive-size-mismatch");
    if (digest.digest("hex") !== archive.sha256) fail("studio-runtime-builder-tool-archive-digest-mismatch");
  } catch (error) {
    await handle?.close().catch(() => {});
    await rm(destinationPath, { force: true }).catch(() => {});
    throw error;
  }
}

export async function fetchApprovedBuilderTool({
  parentPacket,
  packet,
  approvalReceipt,
  toolRoot,
  fetchImpl = globalThis.fetch,
}) {
  const validated = validateBuilderToolAuthorizedInputs(parentPacket, packet, approvalReceipt);
  const requestedRoot = resolve(toolRoot);
  const root = join(await realpath(dirname(requestedRoot)), basename(requestedRoot));
  if (await pathExists(root)) fail("studio-runtime-builder-tool-quarantine-exists");
  const archiveDirectory = join(root, "archive");
  const extractedDirectory = join(root, "extracted", dirname(validated.tool.executable.memberPath));
  const archivePartial = join(root, ".archive.partial");
  const memberPartial = join(root, ".member.partial");
  const archivePath = join(archiveDirectory, validated.tool.archive.filename);
  const executablePath = join(root, "extracted", validated.tool.executable.memberPath);
  const receiptPath = join(root, "verified-tool-receipt.json");
  try {
    await mkdir(archiveDirectory, { recursive: true, mode: 0o700 });
    await mkdir(extractedDirectory, { recursive: true, mode: 0o700 });
    const first = await requestBuilderTool(validated.tool.archive.url, fetchImpl);
    const location = first?.headers?.get("location");
    let target;
    try { target = new URL(location, validated.tool.archive.url); } catch {
      fail("studio-runtime-builder-tool-redirect-rejected");
    }
    if (
      first?.redirected === true || first?.status !== 302
      || target.protocol !== "https:" || target.hostname !== APPROVED_BUILDER_TOOL_REDIRECT_HOST
      || target.port !== ""
      || target.pathname !== APPROVED_BUILDER_TOOL_REDIRECT_PATH
      || target.username || target.password || target.hash
    ) fail("studio-runtime-builder-tool-redirect-rejected");
    const finalResponse = await requestBuilderTool(target.href, fetchImpl);
    if (
      finalResponse?.redirected === true
      || (finalResponse?.status >= 300 && finalResponse.status < 400)
      || finalResponse?.status !== 200 || !finalResponse.body
    ) fail("studio-runtime-builder-tool-redirect-rejected");
    await writeExactBuilderToolArchive(finalResponse, validated.tool.archive, archivePartial);
    await rename(archivePartial, archivePath);
    const preflight = await preflightTarGz(archivePath, {
      exactMemberPath: validated.tool.executable.memberPath,
      extractMemberTo: memberPartial,
      builderToolArchive: true,
    });
    if (preflight.member.byteLength !== validated.tool.executable.byteLength) {
      fail("studio-runtime-builder-tool-member-size-mismatch");
    }
    if (await sha256File(memberPartial) !== validated.tool.executable.sha256) {
      fail("studio-runtime-builder-tool-member-digest-mismatch");
    }
    await chmod(memberPartial, 0o700);
    await rename(memberPartial, executablePath);
    const executable = await lstat(executablePath);
    if (!executable.isFile() || executable.isSymbolicLink() || executable.nlink !== 1) {
      fail("studio-runtime-builder-tool-member-invalid");
    }
    const receipt = {
      schemaVersion: 1,
      status: "verified-approved-builder-tool",
      packetId: validated.packetId,
      approvalReceiptSha256: sha256Bytes(canonicalJson(approvalReceipt)),
      archive: {
        relativePath: `archive/${validated.tool.archive.filename}`,
        sourceUrl: validated.tool.archive.url,
        redirectTargetIdentity: `${target.origin}${target.pathname}`,
        byteLength: validated.tool.archive.byteLength,
        sha256: validated.tool.archive.sha256,
      },
      member: {
        relativePath: `extracted/${validated.tool.executable.memberPath}`,
        memberPath: validated.tool.executable.memberPath,
        byteLength: validated.tool.executable.byteLength,
        sha256: validated.tool.executable.sha256,
      },
      candidateBuildUseAuthorized: true,
      installAuthorized: false,
      modelsLockMutationAuthorized: false,
      modelExecutionAuthorized: false,
      publishAuthorized: false,
      deployAuthorized: false,
    };
    const receiptBytes = Buffer.from(`${JSON.stringify(receipt, null, 2)}\n`);
    await writeFile(receiptPath, receiptBytes, { mode: 0o600, flag: "wx" });
    const result = Object.freeze({
      root,
      archivePath,
      executablePath,
      receiptPath,
      receipt: Object.freeze(receipt),
      receiptSha256: sha256Bytes(receiptBytes),
    });
    verifiedBuilderToolCapabilities.add(result);
    return result;
  } catch (error) {
    await rm(root, { recursive: true, force: true }).catch(() => {});
    throw error;
  }
}

export async function reuseApprovedBuilderTool({
  parentPacket,
  packet,
  approvalReceipt,
  toolRoot,
}) {
  const validated = validateBuilderToolAuthorizedInputs(parentPacket, packet, approvalReceipt);
  const requestedRoot = resolve(toolRoot);
  const root = join(await realpath(dirname(requestedRoot)), basename(requestedRoot));
  const executablePath = join(root, "extracted", validated.tool.executable.memberPath);
  const receiptPath = join(root, "verified-tool-receipt.json");
  const receiptBytes = await readOpenedRegularFile(receiptPath).catch(() => (
    fail("studio-runtime-builder-tool-verified-receipt-invalid")
  ));
  let receipt;
  try { receipt = parseRuntimeJsonBytes(receiptBytes); } catch { fail("studio-runtime-builder-tool-verified-receipt-invalid"); }
  const capability = Object.freeze({
    root,
    archivePath: join(root, "archive", validated.tool.archive.filename),
    executablePath,
    receiptPath,
    receipt: Object.freeze(receipt),
    receiptSha256: sha256Bytes(receiptBytes),
  });
  verifiedBuilderToolCapabilities.add(capability);
  await validateVerifiedBuilderToolCapability(validated, approvalReceipt, capability);
  return capability;
}

async function validateVerifiedBuilderToolCapability(packet, approvalReceipt, capability) {
  if (!capability || typeof capability !== "object" || !verifiedBuilderToolCapabilities.has(capability)) {
    fail("studio-runtime-builder-tool-verified-receipt-invalid");
  }
  const root = resolve(capability.root);
  if (await realpath(root).catch(() => null) !== root) fail("studio-runtime-builder-tool-verified-receipt-invalid");
  const rootDetails = await lstat(root).catch(() => fail("studio-runtime-builder-tool-verified-receipt-invalid"));
  const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
  if (
    !rootDetails.isDirectory() || rootDetails.isSymbolicLink()
    || (currentUid !== null && rootDetails.uid !== currentUid)
    || (rootDetails.mode & 0o077) !== 0
  ) fail("studio-runtime-builder-tool-verified-receipt-invalid");
  const archivePath = join(root, "archive", packet.tool.archive.filename);
  const executablePath = join(root, "extracted", packet.tool.executable.memberPath);
  const receiptPath = join(root, "verified-tool-receipt.json");
  if (
    resolve(capability.archivePath) !== archivePath
    || resolve(capability.executablePath) !== executablePath
    || resolve(capability.receiptPath) !== receiptPath
  ) fail("studio-runtime-builder-tool-verified-receipt-invalid");
  const receiptBytes = await readFile(receiptPath).catch(() => fail("studio-runtime-builder-tool-verified-receipt-invalid"));
  const receiptDetails = await lstat(receiptPath).catch(() => fail("studio-runtime-builder-tool-verified-receipt-invalid"));
  if (
    !receiptDetails.isFile() || receiptDetails.isSymbolicLink() || receiptDetails.nlink !== 1
    || (currentUid !== null && receiptDetails.uid !== currentUid) || (receiptDetails.mode & 0o077) !== 0
  ) fail("studio-runtime-builder-tool-verified-receipt-invalid");
  let receipt;
  try { receipt = parseRuntimeJsonBytes(receiptBytes); } catch { fail("studio-runtime-builder-tool-verified-receipt-invalid"); }
  const expectedReceipt = {
    schemaVersion: 1,
    status: "verified-approved-builder-tool",
    packetId: packet.packetId,
    approvalReceiptSha256: sha256Bytes(canonicalJson(approvalReceipt)),
    archive: {
      relativePath: `archive/${packet.tool.archive.filename}`,
      sourceUrl: packet.tool.archive.url,
      redirectTargetIdentity: `https://${APPROVED_BUILDER_TOOL_REDIRECT_HOST}${APPROVED_BUILDER_TOOL_REDIRECT_PATH}`,
      byteLength: packet.tool.archive.byteLength,
      sha256: packet.tool.archive.sha256,
    },
    member: {
      relativePath: `extracted/${packet.tool.executable.memberPath}`,
      memberPath: packet.tool.executable.memberPath,
      byteLength: packet.tool.executable.byteLength,
      sha256: packet.tool.executable.sha256,
    },
    candidateBuildUseAuthorized: true,
    installAuthorized: false,
    modelsLockMutationAuthorized: false,
    modelExecutionAuthorized: false,
    publishAuthorized: false,
    deployAuthorized: false,
  };
  const expectedReceiptBytes = Buffer.from(`${JSON.stringify(expectedReceipt, null, 2)}\n`);
  const receiptSha256 = sha256Bytes(receiptBytes);
  if (
    canonicalJson(receipt) !== canonicalJson(expectedReceipt)
    || !receiptBytes.equals(expectedReceiptBytes)
    || canonicalJson(capability.receipt) !== canonicalJson(expectedReceipt)
    || capability.receiptSha256 !== receiptSha256
  ) fail("studio-runtime-builder-tool-verified-receipt-invalid");
  const entries = await walkTree(root);
  const allowed = new Set([
    "archive", `archive/${packet.tool.archive.filename}`,
    "extracted", ...packet.tool.executable.memberPath.split("/").slice(0, -1).map((_, index, parts) => `extracted/${parts.slice(0, index + 1).join("/")}`),
    `extracted/${packet.tool.executable.memberPath}`,
    "verified-tool-receipt.json",
  ]);
  if (entries.some((entry) => (
    !allowed.has(entry.relativePath)
    || entry.details.isSymbolicLink()
    || (!entry.details.isFile() && !entry.details.isDirectory())
    || (currentUid !== null && entry.details.uid !== currentUid)
  ))) fail("studio-runtime-builder-tool-verified-receipt-invalid");
  const archive = await lstat(archivePath).catch(() => fail("studio-runtime-builder-tool-verified-receipt-invalid"));
  if (
    !archive.isFile() || archive.isSymbolicLink() || archive.nlink !== 1
    || (currentUid !== null && archive.uid !== currentUid) || (archive.mode & 0o077) !== 0
    || archive.size !== packet.tool.archive.byteLength || await sha256File(archivePath) !== packet.tool.archive.sha256
  ) fail("studio-runtime-builder-tool-archive-digest-mismatch");
  const executable = await lstat(executablePath).catch(() => fail("studio-runtime-builder-tool-executable-invalid"));
  if ((currentUid !== null && executable.uid !== currentUid) || (executable.mode & 0o077) !== 0) {
    fail("studio-runtime-builder-tool-executable-invalid");
  }
  await validateUvExecutableIdentity(packet, executablePath);
  return { root, archivePath, executablePath, receiptPath, receipt, receiptSha256 };
}

export async function fetchVerifiedGithubReleaseRedirect({
  packetId,
  material,
  redirectPolicy,
  redirectApproval,
  destinationPath,
  fetchImpl = globalThis.fetch,
}) {
  validateGithubRedirectPolicy(packetId, material, redirectPolicy, redirectApproval);
  const request = async (url) => {
    try {
      return await fetchImpl(url, {
        method: "GET",
        redirect: "manual",
        credentials: "omit",
        signal: AbortSignal.timeout(120_000),
        headers: { "User-Agent": "wearable-phase-a-runtime-builder" },
      });
    } catch {
      fail("studio-runtime-material-download-failed", material.filename);
    }
  };
  const first = await request(material.url);
  const location = first?.headers?.get("location");
  let target;
  try {
    target = new URL(location, material.url);
  } catch {
    fail("studio-runtime-redirect-rejected", `${material.url}-><invalid>`);
  }
  if (
    first?.redirected === true
    || first?.status !== 302
    || target.protocol !== "https:"
    || target.host !== APPROVED_GITHUB_REDIRECT_HOST
    || target.pathname !== APPROVED_GITHUB_REDIRECT_PATH
    || target.username
    || target.password
    || target.hash
  ) {
    const identity = target ? `${target.origin}${target.pathname}` : "<invalid>";
    fail("studio-runtime-redirect-rejected", `${material.url}->${identity}`);
  }
  const finalResponse = await request(target.href);
  if (
    finalResponse?.redirected === true
    || (finalResponse?.status >= 300 && finalResponse.status < 400)
    || finalResponse?.status !== 200
    || !finalResponse.body
  ) fail("studio-runtime-redirect-rejected", `${target.origin}${target.pathname}->second-hop-or-body-invalid`);
  await writeExactResponse(finalResponse, material, destinationPath);
  return {
    statusCode: 302,
    hops: 1,
    originalUrl: material.url,
    targetIdentity: `${target.origin}${target.pathname}`,
    byteLength: material.byteLength,
    sha256: material.sha256,
  };
}

export async function fetchApprovedMaterials({
  packet,
  approvalReceipt,
  githubRedirectPolicy,
  githubRedirectApproval,
  quarantineRoot,
  fetchImpl = globalThis.fetch,
}) {
  const validated = validateAuthorizedInputs(packet, approvalReceipt);
  const pythonMaterial = validated.materials.find((item) => item.url === APPROVED_GITHUB_ORIGINAL_URL);
  validateGithubRedirectPolicy(
    validated.packetId,
    pythonMaterial,
    githubRedirectPolicy,
    githubRedirectApproval,
  );
  const root = resolve(quarantineRoot);
  if (await pathExists(root)) fail("studio-runtime-quarantine-exists");
  await mkdir(root, { mode: 0o700 });
  await chmod(root, 0o700);
  const receipts = [];
  for (let index = 0; index < validated.materials.length; index += 1) {
    const material = validated.materials[index];
    const destination = join(root, material.filename);
    const partial = join(root, `.${String(index).padStart(3, "0")}-${material.sha256}.partial`);
    try {
      let redirect = "none";
      if (material.url === APPROVED_GITHUB_ORIGINAL_URL) {
        const redirectReceipt = await fetchVerifiedGithubReleaseRedirect({
          packetId: validated.packetId,
          material,
          redirectPolicy: githubRedirectPolicy,
          redirectApproval: githubRedirectApproval,
          destinationPath: partial,
          fetchImpl,
        });
        redirect = redirectReceipt.targetIdentity;
      } else {
        const response = await responseForDirectMaterial(material, fetchImpl);
        await writeExactResponse(response, material, partial);
      }
      await rename(partial, destination);
      receipts.push({
        filename: material.filename,
        sha256: material.sha256,
        byteLength: material.byteLength,
        source: "downloaded",
        redirect,
      });
    } finally {
      await rm(partial, { force: true }).catch(() => {});
    }
  }
  return {
    quarantineRoot: root,
    receipts,
    downloadedBytes: receipts.reduce((total, item) => total + item.byteLength, 0),
    reusedBytes: 0,
  };
}

async function validateQuarantine(packet, quarantineRoot) {
  const root = resolve(quarantineRoot);
  const details = await lstat(root).catch((error) => {
    if (error?.code === "ENOENT") fail("studio-runtime-material-missing", "quarantine");
    throw error;
  });
  if (!details.isDirectory() || details.isSymbolicLink()) fail("studio-runtime-quarantine-invalid");
  const expected = new Set(packet.materials.map((item) => item.filename));
  const actual = await readdir(root);
  for (const filename of actual) {
    if (!expected.has(filename)) fail("studio-runtime-quarantine-extra", filename);
  }
  for (const material of packet.materials) {
    const path = join(root, material.filename);
    const file = await lstat(path).catch((error) => {
      if (error?.code === "ENOENT") fail("studio-runtime-material-missing", material.filename);
      throw error;
    });
    if (!file.isFile() || file.isSymbolicLink() || file.nlink !== 1) {
      fail("studio-runtime-material-invalid", material.filename);
    }
    if (file.size !== material.byteLength) {
      fail("studio-runtime-material-size-mismatch", material.filename);
    }
    if (await sha256File(path) !== material.sha256) {
      fail("studio-runtime-material-digest-mismatch", material.filename);
    }
  }
  return root;
}

async function snapshotVerifiedMaterials(packet, quarantineRoot, snapshotRoot) {
  if (await pathExists(snapshotRoot)) fail("studio-runtime-material-snapshot-exists");
  await mkdir(snapshotRoot, { recursive: true, mode: 0o700 });
  for (const material of packet.materials) {
    const sourcePath = join(quarantineRoot, material.filename);
    const destinationPath = join(snapshotRoot, material.filename);
    let sourceHandle;
    let destinationHandle;
    try {
      sourceHandle = await open(sourcePath, constants.O_RDONLY | constants.O_NOFOLLOW);
      const sourceDetails = await sourceHandle.stat();
      if (!sourceDetails.isFile() || sourceDetails.nlink !== 1 || sourceDetails.size !== material.byteLength) {
        fail("studio-runtime-material-snapshot-source-invalid", material.filename);
      }
      destinationHandle = await open(destinationPath, "wx", 0o600);
      const digest = createHash("sha256");
      let byteLength = 0;
      for await (const chunk of sourceHandle.createReadStream({ autoClose: false })) {
        const bytes = Buffer.from(chunk);
        byteLength += bytes.length;
        if (byteLength > material.byteLength) fail("studio-runtime-material-size-mismatch", material.filename);
        digest.update(bytes);
        await writeAll(destinationHandle, bytes);
      }
      await destinationHandle.sync();
      if (byteLength !== material.byteLength || digest.digest("hex") !== material.sha256) {
        fail("studio-runtime-material-snapshot-mismatch", material.filename);
      }
    } finally {
      await sourceHandle?.close().catch(() => {});
      await destinationHandle?.close().catch(() => {});
    }
    const copied = await lstat(destinationPath);
    if (!copied.isFile() || copied.isSymbolicLink() || copied.nlink !== 1 || copied.size !== material.byteLength || await sha256File(destinationPath) !== material.sha256) {
      fail("studio-runtime-material-snapshot-mismatch", material.filename);
    }
  }
  return snapshotRoot;
}

export async function reuseApprovedMaterials({ packet, approvalReceipt, quarantineRoot }) {
  const validated = validateAuthorizedInputs(packet, approvalReceipt);
  const root = await validateQuarantine(validated, quarantineRoot);
  const receipts = validated.materials.map((material) => ({
    filename: material.filename,
    sha256: material.sha256,
    byteLength: material.byteLength,
    source: "reused-rehashed",
    redirect: material.url === APPROVED_GITHUB_ORIGINAL_URL
      ? `https://${APPROVED_GITHUB_REDIRECT_HOST}${APPROVED_GITHUB_REDIRECT_PATH}`
      : "none",
  }));
  return {
    quarantineRoot: root,
    receipts,
    downloadedBytes: 0,
    reusedBytes: receipts.reduce((total, item) => total + item.byteLength, 0),
  };
}

export async function fetchApprovedSupplementalBuildMaterials({
  packet,
  approvalReceipt,
  quarantineRoot,
  fetchImpl = globalThis.fetch,
}) {
  const validated = validateSupplementalAuthorizedInputs(packet, approvalReceipt);
  const root = resolve(quarantineRoot);
  if (await pathExists(root)) fail("studio-runtime-quarantine-exists");
  await mkdir(root, { mode: 0o700 });
  await chmod(root, 0o700);
  const receipts = [];
  for (let index = 0; index < validated.materials.length; index += 1) {
    const material = validated.materials[index];
    const destination = join(root, material.filename);
    const partial = join(root, `.${String(index).padStart(3, "0")}-${material.sha256}.partial`);
    try {
      const response = await responseForDirectMaterial(material, fetchImpl);
      await writeExactResponse(response, material, partial);
      await rename(partial, destination);
      receipts.push({
        filename: material.filename,
        sha256: material.sha256,
        byteLength: material.byteLength,
        source: "downloaded",
        redirect: "none",
      });
    } finally {
      await rm(partial, { force: true }).catch(() => {});
    }
  }
  return {
    quarantineRoot: root,
    receipts,
    downloadedBytes: receipts.reduce((total, item) => total + item.byteLength, 0),
    reusedBytes: 0,
  };
}

export async function reuseApprovedSupplementalBuildMaterials({
  packet,
  approvalReceipt,
  quarantineRoot,
}) {
  const validated = validateSupplementalAuthorizedInputs(packet, approvalReceipt);
  const root = await validateQuarantine(validated, quarantineRoot);
  const receipts = validated.materials.map((material) => ({
    filename: material.filename,
    sha256: material.sha256,
    byteLength: material.byteLength,
    source: "reused-rehashed",
    redirect: "none",
  }));
  return {
    quarantineRoot: root,
    receipts,
    downloadedBytes: 0,
    reusedBytes: receipts.reduce((total, item) => total + item.byteLength, 0),
  };
}

function safeSubpath(root, path) {
  const fromRoot = relative(resolve(root), resolve(path));
  return fromRoot !== ".." && !fromRoot.startsWith(`..${sep}`) && !isAbsolute(fromRoot);
}

async function assertNoSymlinkedAncestors(root, target) {
  let current = resolve(target);
  const boundary = resolve(root);
  while (current !== boundary) {
    try {
      const details = await lstat(current);
      if (details.isSymbolicLink()) fail("studio-runtime-cli-path-symlink", current);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    const parent = dirname(current);
    if (parent === current) fail("studio-runtime-cli-path-outside-worktree");
    current = parent;
  }
}

export async function validateCliOutputPaths(worktreeRoot, candidateRoot, outputDoc) {
  const lexicalRoot = resolve(worktreeRoot);
  const root = await realpath(worktreeRoot);
  const targets = [
    candidateRoot,
    outputDoc,
  ];
  for (const target of targets) {
    const lexicalTarget = resolve(target);
    const fromLexicalRoot = relative(lexicalRoot, lexicalTarget);
    if (!fromLexicalRoot || fromLexicalRoot === ".." || fromLexicalRoot.startsWith(`..${sep}`) || isAbsolute(fromLexicalRoot)) {
      fail("studio-runtime-cli-path-outside-worktree");
    }
    const resolved = join(root, fromLexicalRoot);
    if (resolved === root || !safeSubpath(root, resolved)) fail("studio-runtime-cli-path-outside-worktree");
    await assertNoSymlinkedAncestors(root, resolved);
  }
  return { root, candidateRoot: join(root, relative(lexicalRoot, resolve(candidateRoot))), outputDoc: join(root, relative(lexicalRoot, resolve(outputDoc))) };
}

function cleanBuildEnvironment(home, temporary) {
  return {
    PATH: "/usr/bin:/bin",
    HOME: home,
    TMPDIR: temporary,
    LANG: "C",
    LC_ALL: "C",
    TZ: "UTC",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
    PYTHONSAFEPATH: "1",
    HF_HUB_OFFLINE: "1",
    TRANSFORMERS_OFFLINE: "1",
    UV_OFFLINE: "1",
    UV_PYTHON_DOWNLOADS: "never",
    UV_NO_PROGRESS: "1",
    UV_NO_CONFIG: "1",
  };
}

async function runCommand(commandRunner, command, args, options) {
  try {
    return await commandRunner(command, args, {
      ...options,
      encoding: "utf8",
      maxBuffer: 32 * 1024 * 1024,
      timeout: options?.timeout ?? COMMAND_TIMEOUT_MS,
      killSignal: "SIGKILL",
    });
  } catch (error) {
    const processOutput = `${error?.stdout ?? ""}\n${error?.stderr ?? ""}`;
    const output = `${processOutput}\n${error?.message ?? ""}`;
    if (/network|download|offline.*(?:not found|unavailable|required)|required.*network/iu.test(processOutput)) {
      fail("studio-runtime-network-required-build", output.trim().slice(0, 500));
    }
    fail("studio-runtime-offline-build-failed", output.trim().slice(0, 500));
  }
}

export async function spawnBoundedProcess(command, args, options = {}) {
  const {
    timeout = COMMAND_TIMEOUT_MS,
    maxBuffer = 32 * 1024 * 1024,
    encoding = "utf8",
    killSignal = "SIGKILL",
    ...spawnOptions
  } = options;
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(command, args, {
      ...spawnOptions,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const stdout = [];
    const stderr = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let settled = false;
    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) rejectPromise(error);
      else resolvePromise(result);
    };
    const overflow = () => {
      child.kill(killSignal);
      const error = new Error("studio-runtime-command-output-limit-exceeded");
      error.stdout = Buffer.concat(stdout).toString(encoding);
      error.stderr = Buffer.concat(stderr).toString(encoding);
      finish(error);
    };
    child.stdout.on("data", (chunk) => {
      stdoutBytes += chunk.length;
      if (stdoutBytes > maxBuffer) return overflow();
      stdout.push(chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderrBytes += chunk.length;
      if (stderrBytes > maxBuffer) return overflow();
      stderr.push(chunk);
    });
    child.once("error", (error) => finish(error));
    child.once("close", (code, signal) => {
      const output = {
        stdout: Buffer.concat(stdout).toString(encoding),
        stderr: Buffer.concat(stderr).toString(encoding),
      };
      if (code === 0) return finish(null, output);
      const error = new Error(`command exited with code ${code ?? "null"}${signal ? ` signal ${signal}` : ""}`);
      Object.assign(error, output, { code, signal });
      finish(error);
    });
    const timer = setTimeout(() => {
      child.kill(killSignal);
      const error = new Error("command timed out");
      error.stdout = Buffer.concat(stdout).toString(encoding);
      error.stderr = Buffer.concat(stderr).toString(encoding);
      finish(error);
    }, timeout);
    timer.unref();
  });
}

async function validateRegularExecutable(path, code) {
  const details = await lstat(path).catch(() => fail(code));
  if (!details.isFile() || details.isSymbolicLink() || details.nlink !== 1 || (details.mode & 0o111) === 0) fail(code);
  return details;
}

async function validateUvExecutableIdentity(toolPacket, uvPath) {
  const details = await validateRegularExecutable(uvPath, "studio-runtime-builder-tool-executable-invalid");
  if (details.size !== toolPacket.tool.executable.byteLength) fail("studio-runtime-builder-tool-executable-size-mismatch");
  if (await sha256File(uvPath) !== toolPacket.tool.executable.sha256) fail("studio-runtime-builder-tool-executable-digest-mismatch");
}

async function sha256FileHandle(handle) {
  const digest = createHash("sha256");
  for await (const chunk of handle.createReadStream({ autoClose: false, start: 0 })) digest.update(chunk);
  return digest.digest("hex");
}

async function inspectOpenedRegularFile(path, { expectedByteLength, expectedSha256 } = {}) {
  let handle;
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const before = await handle.stat();
    if (!before.isFile() || before.nlink !== 1) fail("studio-runtime-final-file-invalid", path);
    if (expectedByteLength !== undefined && before.size !== expectedByteLength) {
      fail("studio-runtime-final-file-mismatch", path);
    }
    const sha256 = await sha256FileHandle(handle);
    const after = await handle.stat();
    if (
      !after.isFile() || after.nlink !== 1
      || after.dev !== before.dev || after.ino !== before.ino
      || after.size !== before.size || after.mtimeMs !== before.mtimeMs
      || (expectedSha256 !== undefined && sha256 !== expectedSha256)
    ) fail("studio-runtime-final-file-mismatch", path);
    return { details: before, sha256 };
  } catch (error) {
    if (error?.code === "ELOOP" || error?.code === "ENOENT") {
      fail("studio-runtime-final-file-invalid", path);
    }
    throw error;
  } finally {
    await handle?.close().catch(() => {});
  }
}

async function readOpenedRegularFile(path, { expectedByteLength, expectedSha256 } = {}) {
  let handle;
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const before = await handle.stat();
    if (!before.isFile() || before.nlink !== 1) fail("studio-runtime-final-file-invalid", path);
    if (expectedByteLength !== undefined && before.size !== expectedByteLength) {
      fail("studio-runtime-final-file-mismatch", path);
    }
    const bytes = await handle.readFile();
    const after = await handle.stat();
    if (
      !after.isFile() || after.nlink !== 1
      || after.dev !== before.dev || after.ino !== before.ino
      || after.size !== before.size || after.mtimeMs !== before.mtimeMs
      || bytes.length !== before.size
      || (expectedSha256 !== undefined && sha256Bytes(bytes) !== expectedSha256)
    ) fail("studio-runtime-final-file-mismatch", path);
    return bytes;
  } catch (error) {
    if (error?.code === "ELOOP" || error?.code === "ENOENT") {
      fail("studio-runtime-final-file-invalid", path);
    }
    throw error;
  } finally {
    await handle?.close().catch(() => {});
  }
}

export async function runVerifiedUvInvocation({
  toolPacket,
  verifiedTool,
  policyPath,
  args,
  options = {},
  processRunner = spawnBoundedProcess,
}) {
  await validateRegularExecutable(SANDBOX_EXECUTABLE, "studio-runtime-network-sandbox-unavailable");
  const expected = toolPacket?.tool?.executable;
  const receiptMember = verifiedTool?.receipt?.member;
  if (
    !expected
    || receiptMember?.memberPath !== expected.memberPath
    || receiptMember?.byteLength !== expected.byteLength
    || receiptMember?.sha256 !== expected.sha256
  ) fail("studio-runtime-builder-tool-verified-receipt-invalid");
  let handle;
  let invocationRoot;
  try {
    handle = await open(verifiedTool.executablePath, constants.O_RDONLY | constants.O_NOFOLLOW);
    const details = await handle.stat();
    const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
    if (
      !details.isFile()
      || details.nlink !== 1
      || (details.mode & 0o111) === 0
      || (currentUid !== null && details.uid !== currentUid)
      || (details.mode & 0o077) !== 0
    ) fail("studio-runtime-builder-tool-executable-invalid");
    if (details.size !== expected.byteLength) fail("studio-runtime-builder-tool-executable-size-mismatch");
    if (await sha256FileHandle(handle) !== expected.sha256) {
      fail("studio-runtime-builder-tool-executable-digest-mismatch");
    }
    invocationRoot = await mkdtemp(join(dirname(verifiedTool.executablePath), ".uv-invocation-"));
    await chmod(invocationRoot, 0o700);
    const invocationPath = join(invocationRoot, "uv");
    try {
      await link(verifiedTool.executablePath, invocationPath);
    } catch {
      fail("studio-runtime-builder-tool-executable-pin-failed");
    }
    const aliasBefore = await lstat(invocationPath).catch(() => fail("studio-runtime-builder-tool-executable-pin-failed"));
    if (
      !aliasBefore.isFile()
      || aliasBefore.isSymbolicLink()
      || aliasBefore.dev !== details.dev
      || aliasBefore.ino !== details.ino
      || aliasBefore.nlink !== 2
      || aliasBefore.size !== details.size
      || await sha256File(invocationPath) !== expected.sha256
    ) fail("studio-runtime-builder-tool-executable-pin-failed");
    const result = await runCommand(processRunner, SANDBOX_EXECUTABLE, ["-f", policyPath, invocationPath, ...args], options);
    const aliasAfter = await lstat(invocationPath).catch(() => fail("studio-runtime-builder-tool-executable-changed-during-run"));
    const handleAfter = await handle.stat();
    if (
      !aliasAfter.isFile()
      || aliasAfter.isSymbolicLink()
      || aliasAfter.dev !== details.dev
      || aliasAfter.ino !== details.ino
      || aliasAfter.nlink !== 2
      || handleAfter.dev !== details.dev
      || handleAfter.ino !== details.ino
      || handleAfter.nlink !== 2
      || handleAfter.size !== expected.byteLength
      || await sha256File(invocationPath) !== expected.sha256
      || await sha256FileHandle(handle) !== expected.sha256
    ) fail("studio-runtime-builder-tool-executable-changed-during-run");
    return result;
  } catch (error) {
    if (error?.code === "ELOOP" || error?.code === "ENOENT") {
      fail("studio-runtime-builder-tool-executable-invalid");
    }
    throw error;
  } finally {
    if (invocationRoot) await rm(invocationRoot, { recursive: true, force: true });
    await handle?.close().catch(() => {});
  }
}

async function runSandboxed(commandRunner, policyPath, executable, args, options = {}) {
  await validateRegularExecutable(SANDBOX_EXECUTABLE, "studio-runtime-network-sandbox-unavailable");
  return runCommand(commandRunner, SANDBOX_EXECUTABLE, ["-f", policyPath, executable, ...args], options);
}

async function runApprovedUv(processRunner, toolPacket, verifiedTool, policyPath, args, options = {}) {
  return runVerifiedUvInvocation({ toolPacket, verifiedTool, policyPath, args, options, processRunner });
}

async function validateUv(processRunner, toolPacket, verifiedTool, policyPath) {
  const result = await runApprovedUv(processRunner, toolPacket, verifiedTool, policyPath, ["--version"]);
  const approvedVersion = new RegExp(`^uv ${REQUIRED_UV_VERSION.replaceAll(".", "\\.")}(?: \\([^\\r\\n]+\\))?$`, "u");
  if (result.stdout.trim() !== toolPacket.tool.executable.expectedVersionOutput || !approvedVersion.test(result.stdout.trim())) {
    fail("studio-runtime-uv-version-mismatch", result.stdout.trim());
  }
}

async function archiveEntries(archivePath, commandRunner = execFile, maxEntries = MAX_ARCHIVE_ENTRIES) {
  const result = await commandRunner("/usr/bin/tar", ["-tzf", archivePath], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
    timeout: TAR_TIMEOUT_MS,
    killSignal: "SIGKILL",
  });
  const entries = result.stdout.split("\n").filter(Boolean);
  if (entries.length === 0 || entries.length > maxEntries) fail("studio-runtime-source-archive-invalid");
  for (const entry of entries) {
    const normalized = entry.replace(/^\.\//u, "");
    if (
      normalized.startsWith("/")
      || normalized.includes("\\")
      || normalized.split("/").some((part) => part === "..")
    ) fail("studio-runtime-source-archive-invalid", entry);
  }
  return entries;
}

export async function extractArchive(archivePath, destination, {
  maxEntries = MAX_ARCHIVE_ENTRIES,
  maxExpandedBytes = MAX_EXPANDED_ARCHIVE_BYTES,
  commandRunner = execFile,
  portableRuntimePaths = false,
} = {}) {
  await preflightTarGz(archivePath, { maxEntries, maxExpandedBytes, portableRuntimePaths });
  await archiveEntries(archivePath, commandRunner, maxEntries);
  await mkdir(destination, { recursive: true, mode: 0o700 });
  await commandRunner("/usr/bin/tar", [
    "-xzf",
    archivePath,
    "-C",
    destination,
    "--strip-components",
    "1",
    "--no-same-owner",
    "--no-same-permissions",
  ], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024, timeout: TAR_TIMEOUT_MS, killSignal: "SIGKILL" });
  async function expandedUsage(current) {
    let count = 0;
    let bytes = 0;
    const names = await readdir(current);
    const relativePaths = names.map((name) => relative(destination, join(current, name)).split(sep).join("/"));
    if (portableRuntimePaths) sortPortableRuntimePaths(relativePaths);
    for (const name of names) {
      const path = join(current, name);
      const details = await lstat(path);
      count += 1;
      if (details.isDirectory()) {
        const child = await expandedUsage(path);
        count += child.count;
        bytes += child.bytes;
      } else if (details.isFile()) bytes += details.size;
      else if (details.isSymbolicLink()) {
        const target = await readlink(path);
        if (!target || isAbsolute(target) || target.includes("\\") || target.includes("\0")) {
          fail("studio-runtime-source-archive-invalid", name);
        }
        const resolvedTarget = resolve(dirname(path), target);
        const extractionRoot = resolve(destination);
        if (resolvedTarget !== extractionRoot && !safeSubpath(extractionRoot, resolvedTarget)) {
          fail("studio-runtime-source-archive-invalid", name);
        }
      } else fail("studio-runtime-source-archive-invalid", name);
      if (count > maxEntries || bytes > maxExpandedBytes) fail("studio-runtime-source-archive-limits-exceeded");
    }
    return { count, bytes };
  }
  const expanded = await expandedUsage(destination);
  if (expanded.count > maxEntries || expanded.bytes > maxExpandedBytes) {
    fail("studio-runtime-source-archive-limits-exceeded");
  }
}

async function collectSymbolicLinks(root, current = root) {
  const links = [];
  const names = await readdir(current);
  const relativePaths = names.map((name) => relative(root, join(current, name)).split(sep).join("/"));
  sortPortableRuntimePaths(relativePaths);
  for (const name of names.sort(compareRuntimePathsByUtf8Bytes)) {
    const path = join(current, name);
    const details = await lstat(path);
    if (details.isSymbolicLink()) links.push(path);
    else if (details.isDirectory()) links.push(...await collectSymbolicLinks(root, path));
  }
  return links;
}

export async function materializeInternalPythonFileLinks(pythonRoot) {
  const root = await realpath(pythonRoot);
  const links = await collectSymbolicLinks(root);
  for (const [index, linkPath] of links.entries()) {
    const relativePath = relative(root, linkPath).split(sep).join("/");
    let sourceHandle;
    try {
      const linkDetails = await lstat(linkPath);
      const linkTarget = await readlink(linkPath);
      if (isAbsolute(linkTarget)) fail("studio-runtime-python-link-rejected", relativePath);
      const realTarget = await realpath(resolve(dirname(linkPath), linkTarget));
      if (resolve(realTarget) === root || !safeSubpath(root, realTarget)) {
        fail("studio-runtime-python-link-rejected", relativePath);
      }
      sourceHandle = await open(realTarget, constants.O_RDONLY | constants.O_NOFOLLOW);
      const targetDetails = await sourceHandle.stat();
      if (!targetDetails.isFile() || targetDetails.nlink !== 1) fail("studio-runtime-python-link-rejected", relativePath);
      const temporary = join(dirname(linkPath), `.studio-materialized-link-${index}-${sha256Bytes(relativePath).slice(0, 16)}`);
      let destinationHandle;
      try {
        destinationHandle = await open(temporary, "wx", 0o600);
        for await (const chunk of sourceHandle.createReadStream({ autoClose: false })) {
          await writeAll(destinationHandle, Buffer.from(chunk));
        }
        await destinationHandle.sync();
        await destinationHandle.close();
        destinationHandle = null;
        await chmod(temporary, targetDetails.mode & 0o777);
        const currentLink = await lstat(linkPath);
        if (!currentLink.isSymbolicLink() || currentLink.dev !== linkDetails.dev || currentLink.ino !== linkDetails.ino) {
          fail("studio-runtime-python-link-rejected", relativePath);
        }
        await rename(temporary, linkPath);
      } finally {
        await destinationHandle?.close().catch(() => {});
        await rm(temporary, { force: true }).catch(() => {});
      }
    } catch (error) {
      if (error?.message?.startsWith("studio-runtime-python-link-rejected:")) throw error;
      fail("studio-runtime-python-link-materialization-failed", relativePath);
    } finally {
      await sourceHandle?.close().catch(() => {});
    }
  }
}

async function normalizePythonArchiveExecutableModes(pythonRoot) {
  for (const entry of await walkTree(pythonRoot)) {
    if (
      entry.details.isFile()
      && (entry.details.mode & 0o111) !== 0
      && !executableAllowed(`python/${entry.relativePath}`)
    ) {
      await chmod(entry.path, 0o644);
    }
  }
}

async function removePythonArchiveBytecode(pythonRoot) {
  const root = resolve(pythonRoot);
  const entries = await walkTree(root);
  for (const entry of entries) {
    if (
      entry.details.isFile()
      && entry.relativePath.endsWith(".pyc")
      && safeSubpath(root, entry.path)
    ) {
      await rm(entry.path);
    }
  }
  const cacheDirectories = entries
    .filter((entry) => entry.details.isDirectory() && basename(entry.relativePath) === "__pycache__")
    .sort((left, right) => (
      right.relativePath.length - left.relativePath.length
      || compareRuntimePathsByUtf8Bytes(left.relativePath, right.relativePath)
    ));
  for (const entry of cacheDirectories) {
    if (!safeSubpath(root, entry.path)) fail("studio-runtime-bytecode-rejected", entry.relativePath);
    await rm(entry.path, { recursive: true, force: true });
  }
}

async function walkTree(root, current = root) {
  const entries = [];
  const names = await readdir(current);
  sortPortableRuntimePaths(names.map((name) => relative(root, join(current, name)).split(sep).join("/")));
  names.sort(compareRuntimePathsByUtf8Bytes);
  for (const name of names) {
    if (name === ".DS_Store" || name.startsWith("._")) {
      fail("studio-runtime-resource-fork-rejected", name);
    }
    const path = join(current, name);
    const details = await lstat(path);
    const relativePath = relative(root, path).split(sep).join("/");
    if (details.isSymbolicLink() || (!details.isDirectory() && !details.isFile())) {
      fail("studio-runtime-tree-link-rejected", relativePath);
    }
    if (details.isFile() && details.nlink !== 1) {
      fail("studio-runtime-tree-link-rejected", relativePath);
    }
    entries.push({ path, relativePath, details });
    if (details.isDirectory()) entries.push(...await walkTree(root, path));
  }
  return entries;
}

function executableAllowed(relativePath) {
  return relativePath === "bin/run-offline-worker"
    || relativePath.startsWith("python/bin/")
    || /\.(?:dylib|so(?:\.\d+)*)$/u.test(relativePath);
}

function recordPath(line) {
  if (!line) return null;
  if (!line.startsWith('"')) return line.split(",", 1)[0];
  let value = "";
  for (let index = 1; index < line.length; index += 1) {
    if (line[index] !== '"') {
      value += line[index];
      continue;
    }
    if (line[index + 1] === '"') {
      value += '"';
      index += 1;
      continue;
    }
    return line[index + 1] === "," || index + 1 === line.length ? value : null;
  }
  return null;
}

function portableRecordPathOrFail(line) {
  const relativePath = recordPath(line);
  if (relativePath && !isPortableRuntimeRelativePath(relativePath)) {
    fail("studio-runtime-path-invalid", relativePath);
  }
  return relativePath;
}

async function validateInstalledRuntimePathDomain(sitePackages) {
  await walkTree(sitePackages);
  const directories = sortPortableRuntimePaths(await readdir(sitePackages));
  for (const directory of directories.filter((name) => name.endsWith(".dist-info"))) {
    const record = join(sitePackages, directory, "RECORD");
    if (!await pathExists(record)) continue;
    for (const line of (await readFile(record, "utf8")).split(/\r?\n/u)) portableRecordPathOrFail(line);
  }
}

async function normalizeRecordedInstalledModes(sitePackages) {
  for (const directory of sortPortableRuntimePaths(await readdir(sitePackages))) {
    if (!directory.endsWith(".dist-info")) continue;
    const record = join(sitePackages, directory, "RECORD");
    if (!await pathExists(record)) continue;
    for (const line of (await readFile(record, "utf8")).split(/\r?\n/u)) {
      const relativePath = portableRecordPathOrFail(line);
      if (
        !relativePath
        || isAbsolute(relativePath)
        || relativePath.includes("\\")
        || relativePath.split("/").some((part) => part === ".." || part === "")
      ) continue;
      const path = join(sitePackages, ...relativePath.split("/"));
      if (!safeSubpath(sitePackages, path)) continue;
      let details;
      try {
        details = await lstat(path);
      } catch (error) {
        if (error?.code === "ENOENT") continue;
        throw error;
      }
      if (
        details.isFile()
        && (details.mode & 0o111) !== 0
        && !executableAllowed(`site-packages/${relativePath}`)
      ) await chmod(path, 0o644);
    }
  }
}

function csvPath(value) {
  return /[,"\r\n]/u.test(value) ? `"${value.replaceAll('"', '""')}"` : value;
}

async function canonicalizeInstalledRecords(sitePackages) {
  for (const directory of sortPortableRuntimePaths(await readdir(sitePackages))
    .filter((name) => name.endsWith(".dist-info"))) {
    const recordRelativePath = `${directory}/RECORD`;
    const record = join(sitePackages, directory, "RECORD");
    if (!await pathExists(record)) continue;
    const paths = new Set([recordRelativePath]);
    for (const line of (await readFile(record, "utf8")).split(/\r?\n/u)) {
      const relativePath = portableRecordPathOrFail(line);
      if (
        relativePath
        && !isAbsolute(relativePath)
        && !relativePath.includes("\\")
        && !relativePath.split("/").some((part) => part === ".." || part === "")
      ) paths.add(relativePath);
    }
    const lines = [];
    for (const relativePath of sortPortableRuntimePaths(paths)) {
      const path = join(sitePackages, ...relativePath.split("/"));
      if (!safeSubpath(sitePackages, path)) continue;
      let details;
      try {
        details = await lstat(path);
      } catch (error) {
        if (error?.code === "ENOENT") continue;
        throw error;
      }
      if (!details.isFile()) continue;
      if (relativePath === recordRelativePath) {
        lines.push(`${csvPath(relativePath)},,`);
      } else {
        const digest = createHash("sha256").update(await readFile(path)).digest("base64url");
        lines.push(`${csvPath(relativePath)},sha256=${digest},${details.size}`);
      }
    }
    await writeFile(record, `${lines.join("\n")}\n`);
  }
}

export async function validateInstalledRecordOwnership(sitePackages) {
  const owned = new Set();
  for (const directory of sortPortableRuntimePaths(await readdir(sitePackages)).filter((name) => name.endsWith(".dist-info"))) {
    const record = join(sitePackages, directory, "RECORD");
    if (!await pathExists(record)) continue;
    for (const line of (await readFile(record, "utf8")).split(/\r?\n/u)) {
      const relativePath = portableRecordPathOrFail(line);
      if (relativePath && !isAbsolute(relativePath) && !relativePath.includes("\\") && !relativePath.split("/").includes("..")) {
        owned.add(relativePath);
      }
    }
  }
  for (const entry of await walkTree(sitePackages)) {
    if (entry.details.isFile() && !owned.has(entry.relativePath)) {
      fail("studio-runtime-unowned-installed-file", entry.relativePath);
    }
  }
  return { unownedFiles: [] };
}

export async function normalizeInstalledRuntimeArtifacts(sitePackages, sourceMaterial, forbiddenPaths) {
  await validateInstalledRuntimePathDomain(sitePackages);
  const targetLock = join(sitePackages, ".lock");
  if (await pathExists(targetLock)) {
    const details = await lstat(targetLock);
    if (!details.isFile() || details.isSymbolicLink() || details.nlink !== 1 || details.size !== 0) {
      fail("studio-runtime-uv-target-lock-invalid");
    }
    await rm(targetLock);
  }
  for (const generatedPath of [
    join(sitePackages, "bin"),
    join(sitePackages, "aiohttp", ".hash"),
    join(sitePackages, "aiohttp", "_websocket", ".hash"),
  ]) {
    if (await pathExists(generatedPath)) await rm(generatedPath, { recursive: true, force: true });
  }
  for (const directory of sortPortableRuntimePaths(await readdir(sitePackages))) {
    if (!directory.endsWith(".dist-info")) continue;
    await rm(join(sitePackages, directory, "uv_cache.json"), { force: true });
    const metadataPath = join(sitePackages, directory, "METADATA");
    if (!await pathExists(metadataPath)) continue;
    const metadata = await readFile(metadataPath, "utf8");
    if (
      normalizedName(metadataValue(metadata, "Name") ?? "") === normalizedName(sourceMaterial.name)
      && metadataValue(metadata, "Version") === sourceMaterial.version
    ) {
      await rm(join(sitePackages, directory, "direct_url.json"), { force: true });
    }
  }
  await normalizeRecordedInstalledModes(sitePackages);
  const macBuildHome = /\/Users\/[A-Za-z0-9._-]+\//gu;
  for (const entry of await walkTree(sitePackages)) {
    if (!entry.details.isFile() || entry.details.size > MAX_TEXT_SCAN_BYTES) continue;
    const bytes = await readFile(entry.path);
    if (bytes.includes(0)) continue;
    const text = bytes.toString("utf8");
    if (forbiddenPaths.some((needle) => needle && text.includes(needle))) continue;
    const normalized = text.replace(macBuildHome, "<build-home>/");
    if (normalized !== text) await writeFile(entry.path, normalized);
  }
  await canonicalizeInstalledRecords(sitePackages);
}

export async function sanitizeRuntimeTree(runtimeRoot, forbiddenPaths) {
  const entries = await walkTree(runtimeRoot);
  for (const entry of entries) {
    if (entry.details.isDirectory()) continue;
    if (entry.relativePath.endsWith(".pyc") || entry.relativePath.split("/").includes("__pycache__")) {
      fail("studio-runtime-bytecode-rejected", entry.relativePath);
    }
    const executable = (entry.details.mode & 0o111) !== 0;
    if (executable && !executableAllowed(entry.relativePath)) {
      fail("studio-runtime-executable-not-allowlisted", entry.relativePath);
    }
    if (entry.details.size <= MAX_TEXT_SCAN_BYTES) {
      const bytes = await readFile(entry.path);
      if (!bytes.includes(0)) {
        const text = bytes.toString("utf8");
        const matched = forbiddenPaths.find((needle) => needle && text.includes(needle));
        if (matched || /\/Users\/[A-Za-z0-9._-]+\//u.test(text)) {
          fail("studio-runtime-absolute-build-path", entry.relativePath);
        }
      }
    }
  }
  for (const entry of entries.sort((left, right) => (
    right.relativePath.length - left.relativePath.length
    || compareRuntimePathsByUtf8Bytes(left.relativePath, right.relativePath)
  ))) {
    if (entry.details.isDirectory()) await chmod(entry.path, 0o755);
    else await chmod(entry.path, executableAllowed(entry.relativePath) && (entry.details.mode & 0o111) !== 0 ? 0o755 : 0o644);
  }
}

export async function runtimeManifest(runtimeRoot) {
  const files = [];
  const collisionPaths = new Set();
  const directoryPaths = new Set();
  for (const entry of await walkTree(runtimeRoot)) {
    const collisionPath = runtimePathCollisionKey(entry.relativePath);
    if (collisionPaths.has(collisionPath)) fail("studio-runtime-path-collision", entry.relativePath);
    collisionPaths.add(collisionPath);
    if (entry.details.isDirectory()) directoryPaths.add(collisionPath);
    if (!entry.details.isFile()) continue;
    const opened = await inspectOpenedRegularFile(entry.path);
    if (
      opened.details.dev !== entry.details.dev
      || opened.details.ino !== entry.details.ino
      || opened.details.size !== entry.details.size
    ) fail("studio-runtime-tree-changed-during-read", entry.relativePath);
    files.push({
      relativePath: entry.relativePath,
      byteLength: opened.details.size,
      sha256: opened.sha256,
      type: "file",
      executable: (opened.details.mode & 0o111) !== 0,
    });
  }
  if ([...directoryPaths].some((path) => files.some((file) => (
    runtimePathCollisionKey(file.relativePath) === path
  )))) fail("studio-runtime-path-collision");
  files.sort((left, right) => (
    compareRuntimePathsByUtf8Bytes(left.relativePath, right.relativePath)
  ));
  const digest = createHash("sha256");
  for (const file of files) {
    digest.update(file.relativePath, "utf8");
    digest.update("\0");
    digest.update(file.sha256, "ascii");
    digest.update("\0");
    digest.update(String(file.byteLength), "ascii");
    digest.update("\0file\0");
    digest.update(file.executable ? "1\n" : "0\n", "ascii");
  }
  return { schemaVersion: 1, files, treeSha256: digest.digest("hex") };
}

function normalizedName(value) {
  return value.toLowerCase().replace(/[-_.]+/gu, "-");
}

function metadataValue(text, field) {
  const match = new RegExp(`^${field}:\\s*(.+)$`, "imu").exec(text);
  return match?.[1]?.trim() ?? null;
}

export async function packageInventory(runtimeRoot, packet) {
  const siteRoot = join(runtimeRoot, "site-packages");
  const names = sortPortableRuntimePaths(await readdir(siteRoot))
    .filter((name) => name.endsWith(".dist-info"));
  const installed = new Map();
  for (const directory of names) {
    const metadataPath = join(siteRoot, directory, "METADATA");
    const metadata = await readFile(metadataPath, "utf8").catch(() => {
      fail(
        Object.hasOwn(packet, "workerDependencyLockSha256")
          ? "studio-runtime-scene-metadata-missing"
          : "studio-runtime-package-metadata-invalid",
        directory,
      );
    });
    const name = metadataValue(metadata, "Name");
    const version = metadataValue(metadata, "Version");
    if (!name || !version) fail("studio-runtime-package-metadata-invalid", directory);
    const key = normalizedName(name);
    if (installed.has(key)) fail("studio-runtime-package-metadata-invalid", directory);
    const recordPath = join(siteRoot, directory, "RECORD");
    const record = await readFile(recordPath, "utf8").catch(() => "");
    const moduleRoots = sortPortableRuntimePaths(new Set(record.split("\n").filter(Boolean).flatMap((line) => {
      const relativePath = portableRecordPathOrFail(line);
      const first = relativePath?.split("/", 1)[0];
      if (!first || first.endsWith(".dist-info") || first.endsWith(".data")) return [];
      return [first];
    })));
    const licenseFiles = sortPortableRuntimePaths(await readdir(join(siteRoot, directory), { recursive: true }))
      .filter((item) => /(?:^|\/)(?:licen[cs]e|copying|notice)(?:\.|$)/iu.test(item))
      .map((item) => `${directory}/${item}`);
    installed.set(key, {
      name,
      version,
      moduleRoots,
      licenseExpression: metadataValue(metadata, "License-Expression")
        ?? metadataValue(metadata, "License")
        ?? "UNKNOWN",
      licenseEvidence: [`${directory}/METADATA`, ...licenseFiles],
      rawMetadata: metadata,
    });
  }
  const packageMaterials = packet.materials.filter((item) => (
    item.kind === "python-distribution" || item.kind === "source-package"
  ));
  const approvedPackageNames = new Set(packageMaterials.map((item) => normalizedName(item.name)));
  for (const name of installed.keys()) {
    if (!approvedPackageNames.has(name)) fail("studio-runtime-unapproved-installed-package", name);
  }
  if (Object.hasOwn(packet, "workerDependencyLockSha256") || Object.hasOwn(packet, "runtimeProfileId")) {
    validateSceneRuntimeMetadata(
      [...installed.values()],
      installed.get("scenedetect")?.rawMetadata ?? null,
      packet,
    );
  }
  const packages = packageMaterials.map((material) => {
    const metadata = installed.get(normalizedName(material.name));
    if (!metadata || metadata.version !== material.version) {
      fail("studio-runtime-package-version-mismatch", `${material.name}==${material.version}`);
    }
    return {
      name: material.name,
      version: material.version,
      sourceMaterialId: `${material.kind}:${material.name}:${material.version}:${material.sha256}`,
      sourceSha256: material.sha256,
      licenseExpression: metadata.licenseExpression,
      licenseEvidence: metadata.licenseEvidence,
      moduleRoots: metadata.moduleRoots,
    };
  }).sort((left, right) => left.name.localeCompare(right.name, "en"));
  const python = packet.materials.find((item) => item.kind === "python-runtime");
  return {
    schemaVersion: 1,
    python: {
      name: python.name,
      version: python.version,
      build: python.build ?? null,
      sourceMaterialId: `${python.kind}:${python.name}:${python.version}:${python.sha256}`,
      sourceSha256: python.sha256,
      licenseExpression: "PSF-2.0",
      licenseEvidence: ["python/LICENSE"],
      moduleRoots: ["python"],
    },
    packages,
    recordOwnership: { unownedFiles: [] },
    licenseReview: "pending",
  };
}

function launcherText() {
  return `#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
RUNTIME_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
export PYTHONHOME="$RUNTIME_ROOT/python"
export PYTHONPATH="$RUNTIME_ROOT/site-packages"
export PYTHONNOUSERSITE=1
export PYTHONSAFEPATH=1
export PYTHONDONTWRITEBYTECODE=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
exec /usr/bin/sandbox-exec -f "$RUNTIME_ROOT/policy/deny-network.sb" \\
  "$RUNTIME_ROOT/python/bin/python3.12" "$@"
`;
}

function requirementsFor(materials) {
  return `${materials.map((item) => (
    `${item.name}==${item.version} --hash=sha256:${item.sha256}`
  )).join("\n")}\n`;
}

async function writePrivateJson(path, value) {
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  await chmod(path, 0o600);
}

export async function runRuntimeSelfCheck(runtimeRoot, commandRunner = execFile, scratchParent) {
  const root = resolve(runtimeRoot);
  const requestedScratchParent = resolve(scratchParent ?? dirname(dirname(root)));
  if (requestedScratchParent === root || safeSubpath(root, requestedScratchParent)) {
    fail("studio-runtime-self-check-scratch-invalid");
  }
  const ownedScratch = await mkdtemp(join(await realpath(requestedScratchParent), ".runtime-self-check-"));
  const home = join(ownedScratch, "home");
  const temporary = join(ownedScratch, "tmp");
  const cache = join(ownedScratch, "cache");
  const python = join(root, "python", "bin", "python3.12");
  const source = `import json, pathlib, sys
import mlx_vlm
root = pathlib.Path(${JSON.stringify(root)}).resolve()
module = pathlib.Path(mlx_vlm.__file__).resolve()
if root not in module.parents:
    raise SystemExit("module-origin-invalid")
print(json.dumps({"status":"ready","pythonVersion":".".join(map(str, sys.version_info[:3]))}, sort_keys=True))
`;
  try {
    await chmod(ownedScratch, 0o700);
    await Promise.all([
      mkdir(home, { mode: 0o700 }),
      mkdir(temporary, { mode: 0o700 }),
      mkdir(cache, { mode: 0o700 }),
    ]);
    const result = await runSandboxed(commandRunner, join(root, "policy", "deny-network.sb"), python, ["-B", "-c", source], {
      cwd: root,
      env: {
        PATH: "/usr/bin:/bin",
        HOME: home,
        TMPDIR: temporary,
        XDG_CACHE_HOME: cache,
        HF_HOME: join(cache, "huggingface"),
        LANG: "C",
        LC_ALL: "C",
        PYTHONHOME: join(root, "python"),
        PYTHONPATH: join(root, "site-packages"),
        PYTHONNOUSERSITE: "1",
        PYTHONSAFEPATH: "1",
        PYTHONDONTWRITEBYTECODE: "1",
        HF_HUB_OFFLINE: "1",
        TRANSFORMERS_OFFLINE: "1",
      },
    });
    const lines = result.stdout.trim().split("\n").filter(Boolean);
    let parsed;
    try { parsed = JSON.parse(lines.at(-1)); } catch { fail("studio-runtime-self-check-failed"); }
    if (parsed?.status !== "ready") fail("studio-runtime-self-check-failed");
    return parsed;
  } finally {
    await rm(ownedScratch, { recursive: true, force: true });
  }
}

export async function buildWorkerRuntimeBundle({
  packet,
  approvalReceipt,
  quarantineRoot,
  supplementalPacket,
  supplementalApprovalReceipt,
  supplementalQuarantineRoot,
  builderToolPacket,
  builderToolApprovalReceipt,
  verifiedBuilderTool,
  outputRoot,
  commandRunner = execFile,
  uvProcessRunner = spawnBoundedProcess,
}) {
  const validated = validateAuthorizedInputs(packet, approvalReceipt);
  const supplementalValues = [
    supplementalPacket,
    supplementalApprovalReceipt,
    supplementalQuarantineRoot,
  ];
  const supplementalProvided = supplementalValues.some((value) => value !== undefined);
  if (supplementalProvided && supplementalValues.some((value) => value === undefined)) {
    fail("studio-runtime-supplemental-packet-invalid");
  }
  const validatedSupplemental = supplementalProvided
    ? validateSupplementalAuthorizedInputs(supplementalPacket, supplementalApprovalReceipt)
    : null;
  if (validatedSupplemental && (
    validated.packetId !== APPROVED_MATERIAL_PACKET_ID
    || validated.parentMaterialPacketId !== APPROVED_BASE_MATERIAL_PACKET_ID
    || validatedSupplemental.parentMaterialPacketId !== APPROVED_BASE_MATERIAL_PACKET_ID
    || validatedSupplemental.parentMaterialPacketId !== validated.parentMaterialPacketId
  )) fail("studio-runtime-supplemental-parent-mismatch");
  const validatedTool = validateBuilderToolAuthorizedInputs(validated, builderToolPacket, builderToolApprovalReceipt);
  const verifiedTool = await validateVerifiedBuilderToolCapability(validatedTool, builderToolApprovalReceipt, verifiedBuilderTool);
  await validateRegularExecutable(SANDBOX_EXECUTABLE, "studio-runtime-network-sandbox-unavailable");
  const quarantine = await validateQuarantine(validated, quarantineRoot);
  const supplementalQuarantine = validatedSupplemental
    ? await validateQuarantine(validatedSupplemental, supplementalQuarantineRoot)
    : null;
  const output = resolve(outputRoot);
  if (await pathExists(output)) fail("studio-runtime-output-exists");
  await mkdir(output, { mode: 0o700 });
  const runtimeRoot = join(output, "runtime-root");
  const buildRoot = join(output, ".build");
  const buildHome = join(buildRoot, "home");
  const buildTemporary = join(buildRoot, "tmp");
  const sourceRoot = join(buildRoot, "source");
  const wheelhouse = join(buildRoot, "wheelhouse");
  const sitePackages = join(runtimeRoot, "site-packages");
  const sandboxPolicy = join(buildRoot, "deny-network.sb");
  const materialSnapshot = join(buildRoot, "material-snapshot");
  const supplementalSnapshot = join(buildRoot, "supplemental-material-snapshot");
  await Promise.all([
    mkdir(buildHome, { recursive: true, mode: 0o700 }),
    mkdir(buildTemporary, { recursive: true, mode: 0o700 }),
    mkdir(sourceRoot, { recursive: true, mode: 0o700 }),
    mkdir(wheelhouse, { recursive: true, mode: 0o700 }),
    mkdir(sitePackages, { recursive: true, mode: 0o755 }),
    mkdir(join(runtimeRoot, "bin"), { recursive: true, mode: 0o755 }),
    mkdir(join(runtimeRoot, "policy"), { recursive: true, mode: 0o755 }),
  ]);
  await writeFile(sandboxPolicy, "(version 1)\n(allow default)\n(deny network*)\n", { mode: 0o600 });
  const buildMaterials = await snapshotVerifiedMaterials(validated, quarantine, materialSnapshot);
  const buildSupplementalMaterials = validatedSupplemental
    ? await snapshotVerifiedMaterials(validatedSupplemental, supplementalQuarantine, supplementalSnapshot)
    : null;
  await validateUv(uvProcessRunner, validatedTool, verifiedTool, sandboxPolicy);
  const pythonMaterial = validated.materials.find((item) => item.kind === "python-runtime");
  const sourceMaterial = validated.materials.find((item) => item.kind === "source-package");
  await extractArchive(join(buildMaterials, pythonMaterial.filename), join(runtimeRoot, "python"), {
    portableRuntimePaths: true,
  });
  await materializeInternalPythonFileLinks(join(runtimeRoot, "python"));
  await removePythonArchiveBytecode(join(runtimeRoot, "python"));
  await normalizePythonArchiveExecutableModes(join(runtimeRoot, "python"));
  await extractArchive(join(buildMaterials, sourceMaterial.filename), sourceRoot);
  const pythonPath = join(runtimeRoot, "python", "bin", "python3.12");
  const environment = cleanBuildEnvironment(buildHome, buildTemporary);
  const buildFindLinks = ["--find-links", buildMaterials];
  if (buildSupplementalMaterials) {
    buildFindLinks.push("--find-links", buildSupplementalMaterials);
  }
  await runApprovedUv(uvProcessRunner, validatedTool, verifiedTool, sandboxPolicy, [
    "--offline",
    "--no-python-downloads",
    "--no-progress",
    "build",
    "--wheel",
    "--no-index",
    ...buildFindLinks,
    "--python",
    pythonPath,
    "--out-dir",
    wheelhouse,
    "--no-create-gitignore",
    sourceRoot,
  ], { cwd: buildRoot, env: environment });
  const builtWheels = sortPortableRuntimePaths(await readdir(wheelhouse)).filter((name) => name.endsWith(".whl"));
  if (builtWheels.length !== 1) fail("studio-runtime-source-wheel-invalid");
  const distributionMaterials = validated.materials.filter((item) => item.kind === "python-distribution");
  const requirementsPath = join(buildRoot, "requirements.txt");
  await writeFile(requirementsPath, requirementsFor(distributionMaterials), { mode: 0o600 });
  const commonInstall = [
    "--offline",
    "--no-python-downloads",
    "--no-progress",
    "pip",
    "install",
    "--no-index",
    "--find-links",
    buildMaterials,
    "--require-hashes",
    "--no-deps",
    "--link-mode",
    "copy",
    "--target",
    sitePackages,
    "--python",
    pythonPath,
  ];
  await runApprovedUv(uvProcessRunner, validatedTool, verifiedTool, sandboxPolicy, [...commonInstall, "-r", requirementsPath], {
    cwd: buildRoot,
    env: environment,
  });
  const builtWheelPath = join(wheelhouse, builtWheels[0]);
  const builtWheelSha256 = await sha256File(builtWheelPath);
  const sourceRequirement = join(buildRoot, "source-requirement.txt");
  await writeFile(
    sourceRequirement,
    `${sourceMaterial.name} @ ${pathToFileURL(builtWheelPath).href} --hash=sha256:${builtWheelSha256}\n`,
    { mode: 0o600 },
  );
  await runApprovedUv(uvProcessRunner, validatedTool, verifiedTool, sandboxPolicy, [...commonInstall, "-r", sourceRequirement], {
    cwd: buildRoot,
    env: environment,
  });
  const forbiddenPaths = [
    output,
    buildMaterials,
    buildSupplementalMaterials,
    buildRoot,
    sourceRoot,
  ];
  await normalizeInstalledRuntimeArtifacts(sitePackages, sourceMaterial, forbiddenPaths);
  await writeFile(join(runtimeRoot, "bin", "run-offline-worker"), launcherText(), { mode: 0o755 });
  await chmod(join(runtimeRoot, "bin", "run-offline-worker"), 0o755);
  await writeFile(
    join(runtimeRoot, "policy", "deny-network.sb"),
    "(version 1)\n(allow default)\n(deny network*)\n",
    { mode: 0o644 },
  );
  await sanitizeRuntimeTree(runtimeRoot, forbiddenPaths);
  await validateInstalledRecordOwnership(sitePackages);
  const manifest = await runtimeManifest(runtimeRoot);
  const packages = await packageInventory(runtimeRoot, validated);
  const manifestPath = join(output, "runtime-manifest.json");
  const packagesPath = join(output, "runtime-packages.json");
  const receiptPath = join(output, "derivation-receipt.json");
  const archivePath = join(output, "runtime.tar");
  await writePrivateJson(manifestPath, manifest);
  await writePrivateJson(packagesPath, packages);
  await createDeterministicRuntimeTar(runtimeRoot, archivePath);
  await assertDeterministicRuntimeTar(archivePath);
  const receipt = {
    schemaVersion: 1,
    status: "succeeded",
    materialPacketId: validated.packetId,
    materialPacketSha256: sha256Bytes(canonicalJson(validated)),
    approvalReceiptSha256: sha256Bytes(canonicalJson(approvalReceipt)),
    supplementalMaterialPacketId: validatedSupplemental?.packetId ?? null,
    supplementalApprovalReceiptSha256: validatedSupplemental
      ? sha256Bytes(canonicalJson(supplementalApprovalReceipt))
      : null,
    builderToolPacketId: validatedTool.packetId,
    builderToolApprovalReceiptSha256: sha256Bytes(canonicalJson(builderToolApprovalReceipt)),
    builderToolVerifiedReceiptSha256: verifiedTool.receiptSha256,
    builderToolExecutionSecurityBoundary: BUILDER_TOOL_EXECUTION_SECURITY_BOUNDARY,
    builderToolExecutable: {
      byteLength: validatedTool.tool.executable.byteLength,
      sha256: validatedTool.tool.executable.sha256,
      sourceArchiveSha256: validatedTool.tool.archive.sha256,
      sourceMemberPath: validatedTool.tool.executable.memberPath,
    },
    ...runtimeDependencyLockBindings(validated),
    target: validated.target,
    builder: { uvVersion: REQUIRED_UV_VERSION, archiveFormat: "ustar", networkSandbox: "macos-sandbox-exec-deny-network" },
    promotionVerification: {
      relocationImportSelfCheck: "passed-before-promotion",
      selfCheckScratchBoundary: "owned-private-outside-runtime-and-candidate-tree",
      finalTreeVerification: "descriptor-safe-manifest-match-after-self-check-and-document-staging",
      finalFileVerification: "runtime-tar-and-all-sidecars-rehashed-after-document-staging-immediately-before-promotion",
      promotionFailureRecovery: "candidate-directory-dev-ino-plus-bundle-four-files-tree-exact-or-recovery-required",
    },
    commands: [
      validatedSupplemental
        ? "uv --offline --no-python-downloads build --wheel --no-index --find-links <quarantine> --find-links <supplemental-quarantine>"
        : "uv --offline --no-python-downloads build --wheel --no-index --find-links <quarantine>",
      "uv --offline --no-python-downloads pip install --no-index --find-links <quarantine> --require-hashes --no-deps --target <site-packages>",
    ],
    startedAt: FIXED_TIME,
    completedAt: FIXED_TIME,
    archiveSha256: await sha256File(archivePath),
    treeSha256: manifest.treeSha256,
  };
  await writePrivateJson(receiptPath, receipt);
  await rm(buildRoot, { recursive: true, force: true });
  const result = {
    runtimeRoot,
    archivePath,
    manifestPath,
    packagesPath,
    receiptPath,
    archiveSha256: receipt.archiveSha256,
    manifestSha256: await sha256File(manifestPath),
    packagesSha256: await sha256File(packagesPath),
    receiptSha256: await sha256File(receiptPath),
    builderToolVerifiedReceiptSha256: verifiedTool.receiptSha256,
    builderToolExecutionSecurityBoundary: BUILDER_TOOL_EXECUTION_SECURITY_BOUNDARY,
    treeSha256: manifest.treeSha256,
    totalInstalledBytes: manifest.files.reduce((total, file) => total + file.byteLength, 0),
    pythonVersion: pythonMaterial.version,
    minimumOsVersion: validated.materials.reduce((selected, item) => {
      if (!item.minimumOsVersion) return selected;
      return selected === null || Number.parseFloat(item.minimumOsVersion) > Number.parseFloat(selected)
        ? item.minimumOsVersion
        : selected;
    }, null),
    licenseReview: packages.licenseReview,
    installedDistributionCount: packages.packages.length,
    unknownLicenseNames: packages.packages
      .filter((item) => item.licenseExpression === "UNKNOWN")
      .map((item) => item.name),
    scenePackageLicenses: packages.packages
      .filter((item) => ["platformdirs", "scenedetect"].includes(item.name))
      .map((item) => ({ name: item.name, version: item.version, licenseExpression: item.licenseExpression })),
  };
  return result;
}

export function parseWorkerRuntimeBuildCli(argv, basePath = process.cwd()) {
  const allowed = new Set(["--materials", "--candidate-root", "--output-doc"]);
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!allowed.has(flag) || !value || value.startsWith("--") || values.has(flag)) {
      fail("studio-runtime-cli-arguments-invalid");
    }
    values.set(flag, value);
  }
  if (argv.length !== 6 || [...allowed].some((flag) => !values.has(flag))) {
    fail("studio-runtime-cli-arguments-invalid");
  }
  return {
    materials: resolve(basePath, values.get("--materials")),
    candidateRoot: resolve(basePath, values.get("--candidate-root")),
    outputDoc: resolve(basePath, values.get("--output-doc")),
  };
}

function bundlePacketCore(packet, supplementalPacket, builderToolPacket, built) {
  const sidecar = (relativePath, sha256, byteLength) => ({ relativePath, sha256, byteLength });
  return {
    schemaVersion: 1,
    status: "awaiting-exact-bundle-and-license-approval",
    approved: false,
    enabled: false,
    licenseReview: "pending",
    materialPacketId: packet.packetId,
    supplementalMaterialPacketId: supplementalPacket.packetId,
    builderToolPacketId: builderToolPacket.packetId,
    builderToolExecutableSha256: builderToolPacket.tool.executable.sha256,
    builderToolExecutableBytes: builderToolPacket.tool.executable.byteLength,
    builderToolVerifiedReceiptSha256: built.builderToolVerifiedReceiptSha256,
    builderToolExecutionSecurityBoundary: built.builderToolExecutionSecurityBoundary,
    ...runtimeDependencyLockBindings(packet),
    pythonVersion: built.pythonVersion,
    platform: "darwin",
    architecture: "arm64",
    minimumOsVersion: built.minimumOsVersion ?? "unknown",
    treeSha256: built.treeSha256,
    totalInstalledBytes: built.totalInstalledBytes,
    archive: sidecar("runtime.tar", built.archiveSha256, built.archiveBytes),
    manifest: sidecar("runtime-manifest.json", built.manifestSha256, built.manifestBytes),
    packageInventory: sidecar("runtime-packages.json", built.packagesSha256, built.packagesBytes),
    derivationReceipt: sidecar("derivation-receipt.json", built.receiptSha256, built.receiptBytes),
    licenseInventorySummary: {
      sha256: built.packagesSha256,
      installedDistributionCount: built.installedDistributionCount,
      unknownLicenseNames: built.unknownLicenseNames,
      scenePackages: built.scenePackageLicenses,
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
}

function renderBundlePacket(packet) {
  return `# Worker Runtime Bundle Approval Packet

This is a non-approved candidate description. It authorizes neither installation, lock mutation, model execution, publishing, nor deployment.

- Bundle packet ID: \`${packet.packetId}\`
- Material packet ID: \`${packet.materialPacketId}\`
- Supplemental build-material packet ID: \`${packet.supplementalMaterialPacketId}\`
- Builder-tool packet ID: \`${packet.builderToolPacketId}\`
- Builder executable: ${packet.builderToolExecutableBytes} bytes, SHA-256 \`${packet.builderToolExecutableSha256}\`
- Verified builder-tool receipt SHA-256: \`${packet.builderToolVerifiedReceiptSha256}\`
- Builder execution pin: \`${packet.builderToolExecutionSecurityBoundary.inodePin}\`
- Local MVP trust boundary: compromised processes under the same login account are out of scope; this packet does not claim same-UID race immunity.
- Status: \`${packet.status}\`
- \`approved=false\`
- \`enabled=false\`
- License review: \`${packet.licenseReview}\`
- Python: \`${packet.pythonVersion}\`
- Target: \`${packet.platform}-${packet.architecture}\` (minimum macOS \`${packet.minimumOsVersion}\`)
- Dependency lock SHA-256: \`${packet.dependencyLockSha256}\`
- Worker dependency lock SHA-256: \`${packet.workerDependencyLockSha256}\`
- Runtime tree SHA-256: \`${packet.treeSha256}\`
- Total installed bytes: \`${packet.totalInstalledBytes}\`

## License inventory

- License inventory SHA-256: \`${packet.licenseInventorySummary.sha256}\`
- Installed distributions: \`${packet.licenseInventorySummary.installedDistributionCount}\`
- Unknown license expressions: \`${packet.licenseInventorySummary.unknownLicenseNames.length}\`
- Unknown package names: ${packet.licenseInventorySummary.unknownLicenseNames.length === 0
    ? "none"
    : packet.licenseInventorySummary.unknownLicenseNames.map((name) => `\`${name}\``).join(", ")}
- Scene additions: ${packet.licenseInventorySummary.scenePackages.length === 0
    ? "none"
    : packet.licenseInventorySummary.scenePackages.map((item) => (
      `\`${item.name}==${item.version}\` (${item.licenseExpression})`
    )).join(", ")}

Unknown expressions remain \`UNKNOWN\`; this technical candidate is not legal clearance.

## No-illusion matrix

- \`retrieved=${packet.noIllusion.retrieved}\`
- \`built=${packet.noIllusion.built}\`
- \`deterministic=${packet.noIllusion.deterministic}\`
- \`installed=${packet.noIllusion.installed}\`
- \`qwenProbeRun=${packet.noIllusion.qwenProbeRun}\`
- \`customerContentUsed=${packet.noIllusion.customerContentUsed}\`
- \`productionReady=${packet.noIllusion.productionReady}\`

## Exact candidate files

| File | SHA-256 | Bytes |
| --- | --- | ---: |
| ${packet.archive.relativePath} | ${packet.archive.sha256} | ${packet.archive.byteLength} |
| ${packet.manifest.relativePath} | ${packet.manifest.sha256} | ${packet.manifest.byteLength} |
| ${packet.packageInventory.relativePath} | ${packet.packageInventory.sha256} | ${packet.packageInventory.byteLength} |
| ${packet.derivationReceipt.relativePath} | ${packet.derivationReceipt.sha256} | ${packet.derivationReceipt.byteLength} |

## Required next decision

Review \`runtime-packages.json\`, then explicitly approve or reject this exact bundle packet ID. Bundle and license approval still does not authorize \`models.lock.json\` mutation, installation, model execution, publishing, or deployment. Lock mutation, installation, a real-model probe, publishing, and deployment are separate later human gates.

- \`approve exact worker runtime bundle packet ${packet.packetId} and license inventory sha256 ${packet.packageInventory.sha256}\`
- \`reject worker runtime bundle packet ${packet.packetId}\`
`;
}

export async function verifyBuiltCandidateBeforePromotion(built) {
  const identities = [
    [built.archivePath, built.archiveSha256],
    [built.manifestPath, built.manifestSha256],
    [built.packagesPath, built.packagesSha256],
    [built.receiptPath, built.receiptSha256],
  ];
  const verified = [];
  for (const [path, sha256] of identities) {
    const inspected = await inspectOpenedRegularFile(path, { expectedSha256: sha256 });
    verified.push({ path, sha256: inspected.sha256, byteLength: inspected.details.size });
  }
  const manifestBytes = await readOpenedRegularFile(built.manifestPath, {
    expectedByteLength: verified[1].byteLength,
    expectedSha256: built.manifestSha256,
  });
  let expectedManifest;
  try { expectedManifest = parseRuntimeJsonBytes(manifestBytes); } catch { fail("studio-runtime-final-tree-mismatch"); }
  const actualManifest = await runtimeManifest(built.runtimeRoot);
  if (
    canonicalJson(actualManifest) !== canonicalJson(expectedManifest)
    || actualManifest.treeSha256 !== built.treeSha256
  ) fail("studio-runtime-final-tree-mismatch");
  return {
    archiveBytes: verified[0].byteLength,
    manifestBytes: verified[1].byteLength,
    packagesBytes: verified[2].byteLength,
    receiptBytes: verified[3].byteLength,
  };
}

function publishedCandidateFiles(built, root) {
  return [
    {
      relativePath: "runtime.tar",
      path: join(root, "runtime.tar"),
      sha256: built.archiveSha256,
      byteLength: built.archiveBytes,
    },
    {
      relativePath: "runtime-manifest.json",
      path: join(root, "runtime-manifest.json"),
      sha256: built.manifestSha256,
      byteLength: built.manifestBytes,
    },
    {
      relativePath: "runtime-packages.json",
      path: join(root, "runtime-packages.json"),
      sha256: built.packagesSha256,
      byteLength: built.packagesBytes,
    },
    {
      relativePath: "derivation-receipt.json",
      path: join(root, "derivation-receipt.json"),
      sha256: built.receiptSha256,
      byteLength: built.receiptBytes,
    },
  ];
}

export async function verifyPublishedCandidate(root, built) {
  const rootDetails = await lstat(root).catch(() => fail("studio-runtime-final-file-invalid", root));
  if (
    !rootDetails.isDirectory() || rootDetails.isSymbolicLink()
    || (rootDetails.mode & 0o7777) !== 0o700
  ) fail("studio-runtime-final-file-invalid", root);
  const expectedNames = publishedCandidateFiles(built, root)
    .map((file) => file.relativePath)
    .sort(compareRuntimePathsByUtf8Bytes);
  const actualNames = sortPortableRuntimePaths(await readdir(root));
  if (canonicalJson(actualNames) !== canonicalJson(expectedNames)) {
    fail("studio-runtime-final-file-mismatch", root);
  }
  for (const file of publishedCandidateFiles(built, root)) {
    const inspected = await inspectOpenedRegularFile(file.path, {
      expectedByteLength: file.byteLength,
      expectedSha256: file.sha256,
    });
    if ((inspected.details.mode & 0o7777) !== 0o600) {
      fail("studio-runtime-final-file-mismatch", file.path);
    }
  }
  if (canonicalJson(sortPortableRuntimePaths(await readdir(root))) !== canonicalJson(expectedNames)) {
    fail("studio-runtime-final-file-mismatch", root);
  }
  return rootDetails;
}

async function reconcileFailedCandidatePromotion({
  destination,
  recoveryRoot,
  expectedDirectory,
  expectedBuilt,
}) {
  let destinationDetails;
  try {
    destinationDetails = await lstat(destination);
  } catch (error) {
    if (error?.code === "ENOENT") return "absent";
    return "recovery-required";
  }
  if (
    !destinationDetails.isDirectory() || destinationDetails.isSymbolicLink()
    || destinationDetails.dev !== expectedDirectory.dev
    || destinationDetails.ino !== expectedDirectory.ino
  ) return "recovery-required";
  try {
    await verifyPublishedCandidate(destination, expectedBuilt);
  } catch {
    return "recovery-required";
  }
  try {
    await rename(destination, recoveryRoot);
    return "recovered";
  } catch {
    return "recovery-required";
  }
}

export async function buildAndFinalizeWorkerRuntimeCandidate({
  buildInput,
  candidateRoot,
  outputDoc,
  packet,
  supplementalPacket,
  builderToolPacket,
  buildRunner = buildWorkerRuntimeBundle,
  selfCheckRunner = (runtimeRoot, scratchParent) => runRuntimeSelfCheck(runtimeRoot, execFile, scratchParent),
  documentWriter = writeFile,
  candidatePromoter = rename,
}) {
  const destination = resolve(candidateRoot);
  const document = resolve(outputDoc);
  if (await pathExists(destination) || await pathExists(document)) fail("studio-runtime-output-exists");
  const stageParent = await realpath(dirname(destination));
  const stageRoot = await mkdtemp(join(stageParent, ".worker-runtime-stage-"));
  const firstRoot = join(stageRoot, "first");
  const secondRoot = join(stageRoot, "second");
  const relocated = join(stageRoot, "relocated-runtime");
  const publishedCandidate = join(stageRoot, "published-candidate");
  const recoveredCandidate = join(stageRoot, "recovered-candidate");
  const stagedDocument = join(stageRoot, "bundle-approval.md");
  const promotionIdentityPath = join(stageRoot, "promotion-identity.json");
  let promotedDocumentIdentity;
  let complete = false;
  let recoveryRequired = false;
  try {
    await chmod(stageRoot, 0o700);
    const first = await buildRunner({ ...buildInput, outputRoot: firstRoot });
    const second = await buildRunner({ ...buildInput, outputRoot: secondRoot });
    for (const key of ["archiveSha256", "manifestSha256", "packagesSha256", "receiptSha256", "treeSha256"]) {
      if (first[key] !== second[key]) fail("studio-runtime-repeated-build-mismatch", key);
    }
    await rename(first.runtimeRoot, relocated);
    try {
      await selfCheckRunner(relocated, stageRoot);
    } finally {
      if (await pathExists(relocated)) await rename(relocated, first.runtimeRoot);
    }
    const byteLengths = await verifyBuiltCandidateBeforePromotion(first);
    const final = { ...first, ...byteLengths };
    const core = bundlePacketCore(packet, supplementalPacket, builderToolPacket, final);
    const bundlePacket = {
      ...core,
      packetId: `worker-runtime-bundle-v1-sha256-${sha256Bytes(canonicalJson(core))}`,
    };
    await documentWriter(stagedDocument, renderBundlePacket(bundlePacket), { mode: 0o600, flag: "wx" });
    await chmod(stagedDocument, 0o600);
    const stagedDetails = await lstat(stagedDocument);
    if (!stagedDetails.isFile() || stagedDetails.isSymbolicLink() || stagedDetails.nlink !== 1) {
      fail("studio-runtime-approval-document-invalid");
    }
    const finalByteLengths = await verifyBuiltCandidateBeforePromotion(first);
    if (canonicalJson(finalByteLengths) !== canonicalJson(byteLengths)) {
      fail("studio-runtime-final-file-mismatch");
    }
    await mkdir(publishedCandidate, { mode: 0o700 });
    for (const [source, relativePath] of [
      [first.archivePath, "runtime.tar"],
      [first.manifestPath, "runtime-manifest.json"],
      [first.packagesPath, "runtime-packages.json"],
      [first.receiptPath, "derivation-receipt.json"],
    ]) await rename(source, join(publishedCandidate, relativePath));
    const [stageDetails, candidateDetails] = await Promise.all([
      lstat(stageRoot),
      verifyPublishedCandidate(publishedCandidate, final),
    ]);
    if (!stageDetails.isDirectory() || stageDetails.isSymbolicLink()) {
      fail("studio-runtime-promotion-identity-invalid");
    }
    const expectedCandidateIdentity = {
      schemaVersion: 1,
      bundlePacketId: bundlePacket.packetId,
      treeSha256: final.treeSha256,
      archiveSha256: final.archiveSha256,
      manifestSha256: final.manifestSha256,
      packagesSha256: final.packagesSha256,
      receiptSha256: final.receiptSha256,
      ownedRunIdentity: {
        stageDevice: String(stageDetails.dev),
        stageInode: String(stageDetails.ino),
        candidateDevice: String(candidateDetails.dev),
        candidateInode: String(candidateDetails.ino),
      },
    };
    await writePrivateJson(promotionIdentityPath, expectedCandidateIdentity);
    promotedDocumentIdentity = { dev: stagedDetails.dev, ino: stagedDetails.ino };
    await link(stagedDocument, document);
    const promotedDetails = await lstat(document);
    if (
      !promotedDetails.isFile() || promotedDetails.isSymbolicLink()
      || promotedDetails.dev !== stagedDetails.dev || promotedDetails.ino !== stagedDetails.ino
      || promotedDetails.nlink !== 2
    ) fail("studio-runtime-approval-document-invalid");
    try {
      await candidatePromoter(publishedCandidate, destination);
    } catch (promotionError) {
      const reconciliation = await reconcileFailedCandidatePromotion({
        destination,
        recoveryRoot: recoveredCandidate,
        expectedDirectory: candidateDetails,
        expectedBuilt: final,
      });
      if (reconciliation === "recovery-required") {
        recoveryRequired = true;
        const error = new Error("studio-runtime-promotion-recovery-required");
        error.cause = promotionError;
        throw error;
      }
      throw promotionError;
    }
    complete = true;
    return {
      retrievalSafe: true,
      bundlePacket,
      final: {
        archiveSha256: final.archiveSha256,
        manifestSha256: final.manifestSha256,
        packagesSha256: final.packagesSha256,
        receiptSha256: final.receiptSha256,
        builderToolVerifiedReceiptSha256: final.builderToolVerifiedReceiptSha256,
        builderToolExecutionSecurityBoundary: final.builderToolExecutionSecurityBoundary,
        treeSha256: final.treeSha256,
        totalInstalledBytes: final.totalInstalledBytes,
        pythonVersion: final.pythonVersion,
        minimumOsVersion: final.minimumOsVersion,
        licenseReview: final.licenseReview,
        archiveBytes: final.archiveBytes,
        manifestBytes: final.manifestBytes,
        packagesBytes: final.packagesBytes,
        receiptBytes: final.receiptBytes,
        archivePath: join(destination, "runtime.tar"),
        manifestPath: join(destination, "runtime-manifest.json"),
        packagesPath: join(destination, "runtime-packages.json"),
        receiptPath: join(destination, "derivation-receipt.json"),
      },
    };
  } finally {
    if (!complete && promotedDocumentIdentity) {
      const current = await lstat(document).catch(() => null);
      if (
        current?.isFile() && !current.isSymbolicLink()
        && current.dev === promotedDocumentIdentity.dev && current.ino === promotedDocumentIdentity.ino
      ) await rm(document, { force: true });
    }
    if (!recoveryRequired) await rm(stageRoot, { recursive: true, force: true });
  }
}

async function fileBytes(path) {
  return (await stat(path)).size;
}

async function main() {
  const cli = parseWorkerRuntimeBuildCli(process.argv.slice(2));
  const receiptPath = cli.materials.endsWith(".json")
    ? `${cli.materials.slice(0, -".json".length)}-approval.json`
    : `${cli.materials}-approval.json`;
  const redirectPolicyPath = join(
    dirname(cli.materials),
    "worker-runtime-github-redirect-policy.json",
  );
  const redirectApprovalPath = join(
    dirname(cli.materials),
    "worker-runtime-github-redirect-approval.json",
  );
  const supplementalPath = join(
    dirname(cli.materials),
    "worker-runtime-build-materials.json",
  );
  const supplementalApprovalPath = join(
    dirname(cli.materials),
    "worker-runtime-build-materials-approval.json",
  );
  const builderToolPath = join(dirname(cli.materials), "worker-runtime-builder-tool.json");
  const builderToolApprovalPath = join(dirname(cli.materials), "worker-runtime-builder-tool-approval.json");
  const [
    packet,
    approvalReceipt,
    githubRedirectPolicy,
    githubRedirectApproval,
    supplementalPacket,
    supplementalApprovalReceipt,
    builderToolPacket,
    builderToolApprovalReceipt,
  ] = await Promise.all([
    readFile(cli.materials).then(parseRuntimeJsonBytes),
    readFile(receiptPath).then(parseRuntimeJsonBytes),
    readFile(redirectPolicyPath).then(parseRuntimeJsonBytes),
    readFile(redirectApprovalPath).then(parseRuntimeJsonBytes),
    readFile(supplementalPath).then(parseRuntimeJsonBytes),
    readFile(supplementalApprovalPath).then(parseRuntimeJsonBytes),
    readFile(builderToolPath).then(parseRuntimeJsonBytes),
    readFile(builderToolApprovalPath).then(parseRuntimeJsonBytes),
  ]);
  const validatedParent = validateAuthorizedInputs(packet, approvalReceipt);
  validateSupplementalAuthorizedInputs(supplementalPacket, supplementalApprovalReceipt);
  const validatedTool = validateBuilderToolAuthorizedInputs(validatedParent, builderToolPacket, builderToolApprovalReceipt);
  if (validatedTool.packetId !== PROPOSED_BUILDER_TOOL_PACKET_ID) fail("studio-runtime-builder-tool-packet-invalid");
  const pythonMaterial = packet.materials.find((item) => item.url === APPROVED_GITHUB_ORIGINAL_URL);
  const redirectPolicyPacketId = packet.parentMaterialPacketId ?? packet.packetId;
  validateGithubRedirectPolicy(
    redirectPolicyPacketId,
    pythonMaterial,
    githubRedirectPolicy,
    githubRedirectApproval,
  );
  const { root: cwd } = await validateCliOutputPaths(process.cwd(), cli.candidateRoot, cli.outputDoc);
  const disk = await statfs(cwd, { bigint: true });
  const freeBytes = disk.bavail * disk.bsize;
  const requiredBytes = BigInt(
    packet.expectedTotalDownloadBytes + supplementalPacket.expectedTotalDownloadBytes + builderToolPacket.tool.archive.byteLength,
  ) * 12n + 2n * 1024n * 1024n * 1024n;
  process.stdout.write(`diskFreeBytes=${freeBytes} estimatedRequiredBytes=${requiredBytes}\n`);
  if (freeBytes < requiredBytes) fail("studio-runtime-insufficient-disk-space");
  const quarantineRoot = join(
    cwd,
    ".superpowers",
    "quarantine",
    `${packet.packetId}-${githubRedirectPolicy.policyId}`,
  );
  await mkdir(dirname(quarantineRoot), { recursive: true, mode: 0o700 });
  const retrieval = await pathExists(quarantineRoot)
    ? await reuseApprovedMaterials({ packet, approvalReceipt, quarantineRoot })
    : await fetchApprovedMaterials({
      packet,
      approvalReceipt,
      githubRedirectPolicy,
      githubRedirectApproval,
      quarantineRoot,
    });
  const supplementalQuarantineRoot = join(
    cwd,
    ".superpowers",
    "quarantine",
    supplementalPacket.packetId,
  );
  const supplementalRetrieval = await pathExists(supplementalQuarantineRoot)
    ? await reuseApprovedSupplementalBuildMaterials({
      packet: supplementalPacket,
      approvalReceipt: supplementalApprovalReceipt,
      quarantineRoot: supplementalQuarantineRoot,
    })
    : await fetchApprovedSupplementalBuildMaterials({
      packet: supplementalPacket,
      approvalReceipt: supplementalApprovalReceipt,
      quarantineRoot: supplementalQuarantineRoot,
    });
  const builderToolQuarantineRoot = join(
    cwd,
    ".superpowers",
    "quarantine",
    builderToolPacket.packetId,
  );
  await mkdir(dirname(cli.candidateRoot), { recursive: true, mode: 0o700 });
  await mkdir(dirname(cli.outputDoc), { recursive: true });
  const verifiedBuilderTool = await pathExists(builderToolQuarantineRoot)
    ? await reuseApprovedBuilderTool({
      parentPacket: validatedParent,
      packet: builderToolPacket,
      approvalReceipt: builderToolApprovalReceipt,
      toolRoot: builderToolQuarantineRoot,
    })
    : await fetchApprovedBuilderTool({
      parentPacket: validatedParent,
      packet: builderToolPacket,
      approvalReceipt: builderToolApprovalReceipt,
      toolRoot: builderToolQuarantineRoot,
    });
  const built = await buildAndFinalizeWorkerRuntimeCandidate({
    buildInput: {
      packet,
      approvalReceipt,
      quarantineRoot,
      supplementalPacket,
      supplementalApprovalReceipt,
      supplementalQuarantineRoot,
      builderToolPacket,
      builderToolApprovalReceipt,
      verifiedBuilderTool,
    },
    candidateRoot: cli.candidateRoot,
    outputDoc: cli.outputDoc,
    packet,
    supplementalPacket,
    builderToolPacket,
  });
  const { bundlePacket } = built;
  process.stdout.write(`materialPacketId=${packet.packetId}\n`);
  process.stdout.write(`supplementalMaterialPacketId=${supplementalPacket.packetId}\n`);
  process.stdout.write(`bundlePacketId=${bundlePacket.packetId}\n`);
  process.stdout.write(
    `downloadedBytes=${retrieval.downloadedBytes + supplementalRetrieval.downloadedBytes} `
    + `reusedBytes=${retrieval.reusedBytes + supplementalRetrieval.reusedBytes}\n`,
  );
  process.stdout.write("approved=false installAuthorized=false modelExecutionAuthorized=false\n");
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
