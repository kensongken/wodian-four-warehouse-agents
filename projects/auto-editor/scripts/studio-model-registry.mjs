import { createHash } from "node:crypto";
import { constants, createReadStream } from "node:fs";
import { access, lstat, readFile, realpath } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

const digestPattern = /^[a-f0-9]{64}$/;
const revisionPattern = /^[a-f0-9]{40}$/;
const approvedLicenses = new Set([
  "Apache-2.0", "BSD-3-Clause", "ISC", "MIT", "MPL-2.0", "Apache-2.0 AND MIT",
]);

function urlBoundToRevision(sourceUrl, revision, fileUrl) {
  try {
    const source = new URL(sourceUrl);
    const file = new URL(fileUrl);
    const sourcePath = source.pathname.replace(/\/$/u, "");
    if (source.protocol !== "https:" || file.protocol !== "https:") return false;
    if (source.origin !== file.origin || !file.pathname.startsWith(`${sourcePath}/`)) return false;
    return file.pathname.startsWith(`${sourcePath}/resolve/${revision}/`)
      || file.pathname.startsWith(`${sourcePath}/raw/${revision}/`)
      || (source.hostname === "codeload.github.com"
        && file.pathname === `${sourcePath}/tar.gz/${revision}`)
      || file.pathname === `${sourcePath}/archive/${revision}`
      || file.pathname === `${sourcePath}/archive/${revision}.tar.gz`
      || file.pathname === `${sourcePath}/archive/${revision}.zip`;
  } catch {
    return false;
  }
}

function licenseUrlBoundToRevision(sourceUrl, revision, evidenceUrl) {
  if (urlBoundToRevision(sourceUrl, revision, evidenceUrl)) return true;
  try {
    const source = new URL(sourceUrl);
    const evidence = new URL(evidenceUrl);
    const sourcePath = source.pathname.replace(/\/$/u, "");
    return source.protocol === "https:"
      && source.hostname === "codeload.github.com"
      && evidence.protocol === "https:"
      && evidence.hostname === "raw.githubusercontent.com"
      && evidence.pathname.startsWith(`${sourcePath}/${revision}/`);
  } catch {
    return false;
  }
}

function hasEvidencePlaceholder(sourceUrl, revision, evidenceUrl) {
  try {
    const source = new URL(sourceUrl);
    const sourcePath = source.pathname.replace(/\/$/u, "");
    const url = new URL(evidenceUrl);
    let revisionPrefix = ["resolve", "raw", "archive"]
      .map((kind) => `${sourcePath}/${kind}/${revision}`)
      .find((candidate) => url.pathname.startsWith(candidate));
    if (!revisionPrefix
      && source.hostname === "codeload.github.com"
      && url.hostname === "raw.githubusercontent.com"
      && url.pathname.startsWith(`${sourcePath}/${revision}/`)) {
      revisionPrefix = `${sourcePath}/${revision}`;
    }
    if (!revisionPrefix) return true;
    const location = decodeURIComponent(
      `${url.pathname.slice(revisionPrefix.length)}${url.search}${url.hash}`,
    );
    return /(?:^|[^a-z0-9])(?:todo|placeholder)(?=$|[^a-z0-9])/iu.test(location);
  } catch {
    return true;
  }
}

function validApprovalTime(value) {
  return typeof value === "string"
    && value.endsWith("Z")
    && Number.isFinite(Date.parse(value));
}

function validRelativeFilePath(value) {
  return typeof value === "string"
    && value.length > 0
    && !isAbsolute(value)
    && !value.split(/[\\/]/u).includes("..");
}

function derivedRuntimeManifest(artifact) {
  sourceManifest(artifact);
  const files = artifact?.runtimeFiles;
  const receipt = artifact?.derivationReceipt;
  if (!Array.isArray(files) || files.length === 0
    || receipt?.schemaVersion !== 1
    || typeof receipt?.recipeId !== "string"
    || !receipt.recipeId.trim()
    || !Array.isArray(receipt?.inputSourceIds)
    || receipt.inputSourceIds.length === 0
    || new Set(receipt.inputSourceIds).size !== receipt.inputSourceIds.length
    || !receipt.inputSourceIds.every((id) => artifact.sources.some((source) => source.id === id))
    || !digestPattern.test(receipt?.outputTreeSha256)
    || !digestPattern.test(receipt?.probeReportSha256)
    || receipt?.status !== "succeeded"
    || receipt?.syntheticInput !== true
    || receipt?.realModel !== true) {
    throw new Error("studio-model-registry-lock-invalid");
  }
  const manifest = files.map((file) => {
    if (file?.required !== true
      || !validRelativeFilePath(file?.relativePath)
      || !validRelativeFilePath(file?.candidateRelativePath)
      || typeof file?.executable !== "boolean"
      || !["code", "weights"].includes(file?.sourceKind)
      || !artifact.sources.some((source) => source.kind === file.sourceKind)
      || !digestPattern.test(file?.sha256)
      || !Number.isSafeInteger(file?.byteLength)
      || file.byteLength <= 0
      || Object.hasOwn(file, "sourceUrl")) {
      throw new Error("studio-model-registry-lock-invalid");
    }
    return Object.freeze({ ...file, derived: true });
  });
  if (new Set(manifest.map((file) => file.relativePath)).size !== manifest.length
    || new Set(manifest.map((file) => file.candidateRelativePath)).size !== manifest.length) {
    throw new Error("studio-model-registry-lock-invalid");
  }
  return manifest;
}

function sourceManifest(artifact) {
  const files = artifact.sources.flatMap((source) => {
    if (!Array.isArray(source?.files) || source.files.length === 0) {
      throw new Error("studio-model-registry-lock-invalid");
    }
    return source.files.map((file) => {
      if (file?.required !== true
        || !validRelativeFilePath(file?.relativePath)
        || typeof file?.executable !== "boolean"
        || !digestPattern.test(file?.sha256)
        || !Number.isSafeInteger(file?.byteLength)
        || file.byteLength <= 0
        || !urlBoundToRevision(source.sourceUrl, source.sourceRevision, file?.sourceUrl)) {
        throw new Error("studio-model-registry-lock-invalid");
      }
      return Object.freeze({
        ...file,
        sourceId: source.id,
        sourceKind: source.kind,
        sourceRootUrl: source.sourceUrl,
        sourceRevision: source.sourceRevision,
      });
    });
  });
  if (new Set(files.map((file) => file.relativePath)).size !== files.length) {
    throw new Error("studio-model-registry-lock-invalid");
  }
  return files;
}

export async function loadModelLock(lockPath) {
  let lock;
  try {
    lock = JSON.parse(await readFile(resolve(lockPath), "utf8"));
  } catch {
    throw new Error("studio-model-registry-lock-invalid");
  }
  if (lock?.schemaVersion !== 1 || !Array.isArray(lock.artifacts)) {
    throw new Error("studio-model-registry-lock-invalid");
  }
  const ids = lock.artifacts.map((artifact) => artifact?.id);
  if (
    ids.some((id) => typeof id !== "string" || !id.trim())
    || new Set(ids).size !== ids.length
  ) throw new Error("studio-model-registry-lock-invalid");
  return lock;
}

export function findArtifact(lock, artifactId) {
  const matches = lock.artifacts.filter((artifact) => artifact.id === artifactId);
  if (matches.length !== 1) throw new Error("studio-model-registry-artifact-unknown");
  return matches[0];
}

export function artifactApprovalIssue(artifact) {
  if (!(artifact?.approved === true
    && artifact?.enabled === true
    && artifact?.licenseReview === "approved"
    && typeof artifact?.approvedBy === "string"
    && artifact.approvedBy.trim() !== ""
    && validApprovalTime(artifact?.approvedAt))) return "artifact-unapproved";
  if (
    typeof artifact?.version !== "string"
    || !artifact.version.trim()
    || !["source-package", "binary-model", "checkpoint"].includes(artifact?.artifactKind)
    || typeof artifact?.runtimeBackend !== "string"
    || !artifact.runtimeBackend.trim()
    || !Array.isArray(artifact?.sources)
    || artifact.sources.length === 0
  ) return "lock-invalid";
  const sourceIds = artifact.sources.map((source) => source?.id);
  const sourceKinds = artifact.sources.map((source) => source?.kind);
  if (
    sourceIds.some((id) => typeof id !== "string" || !id.trim())
    || new Set(sourceIds).size !== sourceIds.length
    || new Set(sourceKinds).size !== sourceKinds.length
    || sourceKinds.some((kind) => !["code", "weights"].includes(kind))
  ) return "lock-invalid";
  for (const kind of ["code", "weights"]) {
    const source = artifact.sources.find((candidate) => candidate.kind === kind);
    const evidence = artifact.licenseEvidence?.[kind];
    if (!source) {
      if (
        evidence?.applies !== false
        || evidence?.sourceId !== null
        || evidence?.sourceRevision !== null
        || evidence?.spdxExpression !== null
        || evidence?.evidenceUrl !== null
      ) return "license-evidence-invalid";
      continue;
    }
    if (
      !revisionPattern.test(source?.sourceRevision)
      || source?.inventoryVerified !== true
      || evidence?.applies !== true
      || evidence?.sourceId !== source.id
      || evidence?.sourceRevision !== source.sourceRevision
      || !approvedLicenses.has(evidence?.spdxExpression)
      || !licenseUrlBoundToRevision(source.sourceUrl, source.sourceRevision, evidence?.evidenceUrl)
      || hasEvidencePlaceholder(source.sourceUrl, source.sourceRevision, evidence?.evidenceUrl)
    ) return "license-evidence-invalid";
  }
  try {
    artifactManifest(artifact);
  } catch {
    return "lock-invalid";
  }
  return null;
}

export function artifactManifest(artifact) {
  if (!Array.isArray(artifact?.sources) || artifact.sources.length === 0) {
    throw new Error("studio-model-registry-lock-invalid");
  }
  if (Object.hasOwn(artifact, "runtimeFiles") || Object.hasOwn(artifact, "derivationReceipt")) {
    return derivedRuntimeManifest(artifact);
  }
  return sourceManifest(artifact);
}

export function modelFilePath(root, relativePath) {
  const resolvedRoot = resolve(root);
  const path = resolve(resolvedRoot, relativePath);
  const withinRoot = relative(resolvedRoot, path);
  if (withinRoot === ".." || withinRoot.startsWith(`..${sep}`) || isAbsolute(withinRoot)) {
    throw new Error("studio-model-registry-lock-invalid");
  }
  return path;
}

function isContained(root, path) {
  const fromRoot = relative(root, path);
  return fromRoot === "" || (
    fromRoot !== ".."
    && !fromRoot.startsWith(`..${sep}`)
    && !isAbsolute(fromRoot)
  );
}

async function lstatIfPresent(path) {
  try {
    return await lstat(path);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function canonicalRootPath(resolvedRoot) {
  const rootDetails = await lstatIfPresent(resolvedRoot);
  if (rootDetails !== null) {
    if (rootDetails.isSymbolicLink() || !rootDetails.isDirectory()) {
      throw new Error("studio-model-registry-path-unsafe");
    }
    return realpath(resolvedRoot);
  }

  const missing = [];
  let existing = resolvedRoot;
  while (await lstatIfPresent(existing) === null) {
    const parent = dirname(existing);
    if (parent === existing) throw new Error("studio-model-registry-path-unsafe");
    missing.unshift(existing.slice(parent.length + (parent.endsWith(sep) ? 0 : 1)));
    existing = parent;
  }
  const existingDetails = await lstat(existing);
  if (!existingDetails.isDirectory()) throw new Error("studio-model-registry-path-unsafe");
  return resolve(await realpath(existing), ...missing);
}

export async function inspectModelPath(root, relativePath) {
  const resolvedRoot = resolve(root);
  const destination = modelFilePath(resolvedRoot, relativePath);
  const canonicalRoot = await canonicalRootPath(resolvedRoot);
  const pathParts = relative(resolvedRoot, destination).split(sep).filter(Boolean);
  let current = resolvedRoot;
  let details = await lstatIfPresent(current);
  if (details === null) return { destination, details: null };

  for (const [index, part] of pathParts.entries()) {
    current = join(current, part);
    details = await lstatIfPresent(current);
    if (details === null) return { destination, details: null };
    if (details.isSymbolicLink()) throw new Error("studio-model-registry-path-unsafe");
    if (index < pathParts.length - 1 && !details.isDirectory()) {
      throw new Error("studio-model-registry-path-unsafe");
    }
    const canonical = await realpath(current);
    if (!isContained(canonicalRoot, canonical)) {
      throw new Error("studio-model-registry-path-unsafe");
    }
  }
  return { destination, details };
}

export async function digestFile(path) {
  const digest = createHash("sha256");
  for await (const chunk of createReadStream(path)) digest.update(chunk);
  return digest.digest("hex");
}

async function permissionMatches(path, details, executable) {
  const hasExecuteBit = (details.mode & 0o111) !== 0;
  if (!executable) return !hasExecuteBit;
  if (!hasExecuteBit) return false;
  try {
    await access(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

export async function inspectModelFile(root, file) {
  let inspected;
  try {
    inspected = await inspectModelPath(root, file.relativePath);
  } catch (error) {
    if (error?.message === "studio-model-registry-path-unsafe") {
      return { path: modelFilePath(root, file.relativePath), status: "unsafe-path" };
    }
    throw error;
  }
  if (inspected.details === null) return { path: inspected.destination, status: "missing" };
  if (!inspected.details.isFile()
    || inspected.details.size !== file.byteLength
    || await digestFile(inspected.destination) !== file.sha256) {
    return { path: inspected.destination, status: "digest-mismatch" };
  }
  if (!await permissionMatches(inspected.destination, inspected.details, file.executable)) {
    return { path: inspected.destination, status: "permission-mismatch" };
  }
  return { path: inspected.destination, status: "ready" };
}

export async function inspectArtifact(artifact, root) {
  if (artifactApprovalIssue(artifact) !== null) {
    return { id: artifact.id, version: artifact.version, status: "unapproved", files: [] };
  }
  const files = [];
  for (const file of artifactManifest(artifact)) {
    const inspected = await inspectModelFile(root, file);
    files.push({ relativePath: file.relativePath, status: inspected.status });
  }
  const statuses = new Set(files.map((file) => file.status));
  const status = ["unsafe-path", "missing", "digest-mismatch", "permission-mismatch"]
    .find((candidate) => statuses.has(candidate)) ?? "ready";
  return { id: artifact.id, version: artifact.version, status, files };
}
