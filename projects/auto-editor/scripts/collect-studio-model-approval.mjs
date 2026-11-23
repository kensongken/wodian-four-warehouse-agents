import { createHash } from "node:crypto";
import { lstat, mkdir, mkdtemp, open, rm } from "node:fs/promises";
import { isAbsolute, join } from "node:path";

const revisionPattern = /^[a-f0-9]{40}$/u;
const spdxPattern = /^[A-Za-z0-9][A-Za-z0-9.+()-]*(?: (?:AND|OR) [A-Za-z0-9][A-Za-z0-9.+()-]*)*$/u;
const maximumCandidateBytes = 32 * 1024 * 1024 * 1024;

function fail(code) {
  throw new Error(`studio-model-approval-${code}`);
}

function parseRevisionBoundUrl(value, revision) {
  let url;
  try {
    url = new URL(value);
  } catch {
    fail("source-not-revision-bound");
  }
  if (url.protocol !== "https:" || url.username || url.password) {
    fail("source-not-revision-bound");
  }

  const segments = url.pathname.split("/").filter(Boolean).map(decodeURIComponent);
  if (url.hostname === "codeload.github.com"
    && segments.length === 4
    && ["tar.gz", "zip"].includes(segments[2])
    && segments[3] === revision) {
    return { url, repositoryKey: `github.com/${segments[0]}/${segments[1]}` };
  }
  if (url.hostname === "raw.githubusercontent.com"
    && segments.length >= 4
    && segments[2] === revision) {
    return { url, repositoryKey: `github.com/${segments[0]}/${segments[1]}` };
  }

  const markerIndex = segments.findIndex((segment) => ["resolve", "raw", "archive"].includes(segment));
  if (markerIndex < 2 || segments[markerIndex + 1] !== revision) {
    fail("source-not-revision-bound");
  }
  return { url, repositoryKey: `${url.origin}/${segments.slice(0, markerIndex).join("/")}` };
}

function assertEvidenceUrl(value, sourceRevision, sourceLocation) {
  let evidence;
  try {
    evidence = parseRevisionBoundUrl(value, sourceRevision);
  } catch {
    fail("license-evidence-invalid");
  }
  if (evidence.repositoryKey !== sourceLocation.repositoryKey) {
    fail("license-evidence-invalid");
  }
}

function assertSafeRelativePath(value) {
  if (typeof value !== "string"
    || value.length === 0
    || isAbsolute(value)
    || value.split(/[\\/]/u).includes("..")) {
    fail("source-invalid");
  }
}

function validateSources(sources) {
  if (!Array.isArray(sources) || sources.length === 0) fail("source-invalid");
  const identities = new Set();
  return sources.map((source) => {
    if (typeof source?.artifactId !== "string" || !source.artifactId.trim()
      || typeof source?.sourceId !== "string" || !source.sourceId.trim()
      || !["code", "weights"].includes(source?.kind)
      || !revisionPattern.test(source?.sourceRevision)) {
      fail("source-invalid");
    }
    const sourceLocation = parseRevisionBoundUrl(source.sourceUrl, source.sourceRevision);
    assertSafeRelativePath(source.relativePath);
    if (!Number.isSafeInteger(source.maxByteLength)
      || source.maxByteLength <= 0
      || source.maxByteLength > maximumCandidateBytes) {
      fail("source-invalid");
    }
    if (typeof source?.license?.spdxExpression !== "string"
      || !spdxPattern.test(source.license.spdxExpression)
      || typeof source?.license?.evidenceUrl !== "string") {
      fail("license-evidence-invalid");
    }
    assertEvidenceUrl(source.license.evidenceUrl, source.sourceRevision, sourceLocation);

    const identity = `${source.artifactId}\0${source.sourceId}\0${source.relativePath}`;
    if (identities.has(identity)) fail("source-invalid");
    identities.add(identity);
    return Object.freeze({
      artifactId: source.artifactId,
      sourceId: source.sourceId,
      kind: source.kind,
      sourceUrl: source.sourceUrl,
      sourceRevision: source.sourceRevision,
      relativePath: source.relativePath,
      maxByteLength: source.maxByteLength,
      license: Object.freeze({
        spdxExpression: source.license.spdxExpression,
        evidenceUrl: source.license.evidenceUrl,
      }),
    });
  });
}

async function ensureSafeStagingRoot(stagingRoot) {
  if (typeof stagingRoot !== "string" || stagingRoot.length === 0) fail("staging-root-invalid");
  await mkdir(stagingRoot, { recursive: true, mode: 0o700 });
  const details = await lstat(stagingRoot);
  if (!details.isDirectory() || details.isSymbolicLink()) fail("staging-root-invalid");
}

function contentLength(response) {
  const raw = response.headers.get("content-length");
  if (raw === null) return null;
  if (!/^(?:0|[1-9][0-9]*)$/u.test(raw)) fail("content-length-invalid");
  const value = Number(raw);
  if (!Number.isSafeInteger(value)) fail("content-length-invalid");
  return value;
}

async function streamAndDigest(source, fetchImpl, privateDirectory, index) {
  let response;
  try {
    response = await fetchImpl(source.sourceUrl, { redirect: "error" });
  } catch {
    fail("download-failed");
  }
  if (response?.redirected === true || (response?.status >= 300 && response?.status < 400)) {
    fail("redirect-rejected");
  }
  if (!(response instanceof Response) || !response.ok || response.body === null) {
    fail("download-failed");
  }
  const declaredLength = contentLength(response);
  if (declaredLength !== null && declaredLength > source.maxByteLength) {
    try {
      await response.body.cancel();
    } catch {
      // Cleanup still runs in the outer finally block.
    }
    fail("file-too-large");
  }

  const temporaryPath = join(privateDirectory, `candidate-${String(index).padStart(3, "0")}.part`);
  const fileHandle = await open(temporaryPath, "wx", 0o600);
  const digest = createHash("sha256");
  let byteLength = 0;

  try {
    for await (const chunk of response.body) {
      const bytes = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
      byteLength += bytes.byteLength;
      if (byteLength > source.maxByteLength) fail("file-too-large");
      await fileHandle.write(bytes);
      digest.update(bytes);
    }
  } catch (error) {
    if (error?.message === "studio-model-approval-file-too-large") throw error;
    fail("download-failed");
  } finally {
    await fileHandle.close();
  }
  if (byteLength === 0) fail("file-empty");
  if (declaredLength !== null && declaredLength !== byteLength) fail("content-length-mismatch");
  return { byteLength, sha256: digest.digest("hex") };
}

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const nested of Object.values(value)) deepFreeze(nested);
  }
  return value;
}

export async function collectApprovalCandidate({ sources, fetchImpl, stagingRoot }) {
  const validatedSources = validateSources(sources);
  if (typeof fetchImpl !== "function") fail("fetch-invalid");
  await ensureSafeStagingRoot(stagingRoot);
  const privateDirectory = await mkdtemp(join(stagingRoot, ".studio-model-approval-"));

  try {
    const files = [];
    const licenses = [];
    for (const [index, source] of validatedSources.entries()) {
      const result = await streamAndDigest(source, fetchImpl, privateDirectory, index);
      files.push({
        artifactId: source.artifactId,
        sourceId: source.sourceId,
        kind: source.kind,
        sourceUrl: source.sourceUrl,
        sourceRevision: source.sourceRevision,
        relativePath: source.relativePath,
        byteLength: result.byteLength,
        sha256: result.sha256,
      });
      licenses.push({
        artifactId: source.artifactId,
        sourceId: source.sourceId,
        sourceRevision: source.sourceRevision,
        spdxExpression: source.license.spdxExpression,
        evidenceUrl: source.license.evidenceUrl,
      });
    }
    return deepFreeze({ schemaVersion: 1, status: "candidate", approved: false, files, licenses });
  } finally {
    await rm(privateDirectory, { recursive: true, force: true });
  }
}
