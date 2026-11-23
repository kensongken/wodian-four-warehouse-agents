import { createHash, randomUUID } from "node:crypto";
import { chmod, copyFile, lstat, mkdir, mkdtemp, open, rename, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

import {
  artifactApprovalIssue,
  artifactManifest,
  digestFile,
  findArtifact,
  inspectModelFile,
  inspectModelPath,
  loadModelLock,
} from "./studio-model-registry.mjs";

export const PERSONA_ARTIFACT_IDS = Object.freeze([
  "whisper.cpp-1.9.2-cli-darwin-arm64",
  "whisper.cpp-1.9.2-small-q5_1",
  "mlx-vlm-0.6.6",
  "qwen3-vl-2b-instruct-4bit",
]);

function headerMatchesLockedFile(response, file) {
  const linkedEtag = response.headers.get("x-linked-etag")?.replace(/^W\//u, "").replace(/^"|"$/gu, "");
  return response.headers.get("x-repo-commit") === file.sourceRevision
    && response.headers.get("x-linked-size") === String(file.byteLength)
    && linkedEtag === file.sha256;
}

function approvedHuggingFaceCacheRedirect(response, file, source, target) {
  if (response.status !== 307
    || response.headers.get("x-repo-commit") !== file.sourceRevision
    || target.origin !== source.origin) return false;
  let targetPath;
  try {
    targetPath = decodeURIComponent(target.pathname);
  } catch {
    return false;
  }
  const sourcePath = new URL(file.sourceRootUrl).pathname.replace(/\/$/u, "");
  const resolvePrefix = `${sourcePath}/resolve/${file.sourceRevision}/`;
  if (!source.pathname.startsWith(resolvePrefix)) return false;
  const sourceFilePath = source.pathname.slice(resolvePrefix.length);
  const expectedPath = `/api/resolve-cache/models${sourcePath}/${file.sourceRevision}/${sourceFilePath}`;
  return targetPath === expectedPath;
}

function approvedHuggingFaceRedirect(response, file) {
  let source;
  let target;
  try {
    source = new URL(file.sourceUrl);
    target = new URL(response.headers.get("location"), source);
  } catch {
    return null;
  }
  if (source.hostname !== "huggingface.co"
    || target.protocol !== "https:"
    || target.username
    || target.password) return null;
  if (approvedHuggingFaceCacheRedirect(response, file, source, target)) return target.href;
  if (response.status !== 302
    || !headerMatchesLockedFile(response, file)
    || !/(?:^|\.)hf\.co$/u.test(target.hostname)
    || target.hostname === "hf.co") return null;
  return target.href;
}

async function fetchLockedFile(file, fetchImpl) {
  let response;
  try {
    response = await fetchImpl(file.sourceUrl, { redirect: "manual" });
  } catch {
    throw new Error("studio-model-installer-download-failed");
  }
  if (response?.redirected === true) {
    throw new Error("studio-model-installer-redirect-rejected");
  }
  if (response?.status >= 300 && response?.status < 400) {
    const target = approvedHuggingFaceRedirect(response, file);
    if (target === null) throw new Error("studio-model-installer-redirect-rejected");
    try {
      response = await fetchImpl(target, { redirect: "error" });
    } catch {
      throw new Error("studio-model-installer-download-failed");
    }
    if (response?.redirected === true || (response?.status >= 300 && response?.status < 400)) {
      throw new Error("studio-model-installer-redirect-rejected");
    }
  }
  if (!response?.ok || !response.body) {
    throw new Error("studio-model-installer-download-failed");
  }
  return response;
}

async function downloadLockedFile(file, stagingPath, fetchImpl) {
  let handle;
  try {
    const response = await fetchLockedFile(file, fetchImpl);
    handle = await open(stagingPath, "wx", 0o600);
    const digest = createHash("sha256");
    let byteLength = 0;
    try {
      for await (const chunk of response.body) {
        const bytes = Buffer.from(chunk);
        byteLength += bytes.byteLength;
        if (byteLength > file.byteLength) {
          throw new Error("studio-model-installer-digest-mismatch");
        }
        digest.update(bytes);
        await handle.write(bytes);
      }
    } catch (error) {
      if (error instanceof Error && error.message.startsWith("studio-model-installer-")) {
        throw error;
      }
      throw new Error("studio-model-installer-download-failed");
    }
    await handle.sync();
    await handle.close();
    handle = undefined;
    if (byteLength !== file.byteLength || digest.digest("hex") !== file.sha256) {
      throw new Error("studio-model-installer-digest-mismatch");
    }
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

export async function installArtifact({
  artifactId,
  lockPath,
  root,
  derivedRoot = null,
  fetchImpl = fetch,
}) {
  const lock = await loadModelLock(lockPath);
  let artifact;
  try {
    artifact = findArtifact(lock, artifactId);
  } catch {
    throw new Error("studio-model-installer-artifact-unknown");
  }
  const approvalIssue = artifactApprovalIssue(artifact);
  if (approvalIssue !== null) {
    throw new Error(`studio-model-installer-${approvalIssue}`);
  }
  const files = artifactManifest(artifact);
  const isDerived = files.every((file) => file.derived === true);
  const resolvedRoot = resolve(root);
  const pending = [];
  for (const file of files) {
    const inspected = await inspectModelFile(resolvedRoot, file);
    if (inspected.status === "unsafe-path") {
      throw new Error("studio-model-installer-path-unsafe");
    }
    if (inspected.status !== "ready") {
      pending.push({ file, destination: inspected.path });
    }
  }
  if (pending.length === 0) return { artifact: artifact.id, status: "ready", root: resolvedRoot };

  if (isDerived) {
    if (typeof derivedRoot !== "string" || !derivedRoot.trim()) {
      throw new Error("studio-model-installer-derived-source-required");
    }
    const resolvedDerivedRoot = resolve(derivedRoot);
    for (const item of pending) {
      const candidate = await inspectModelFile(resolvedDerivedRoot, {
        ...item.file,
        relativePath: item.file.candidateRelativePath,
      });
      if (candidate.status !== "ready") {
        throw new Error("studio-model-installer-derived-source-invalid");
      }
      item.sourcePath = candidate.path;
    }
  }

  const stagingRoot = await mkdtemp(join(tmpdir(), `studio-model-install-${randomUUID()}-`));
  try {
    for (const [index, item] of pending.entries()) {
      item.stagingPath = join(stagingRoot, String(index));
      if (isDerived) {
        await copyFile(item.sourcePath, item.stagingPath);
        const details = await lstat(item.stagingPath);
        if (!details.isFile()
          || details.size !== item.file.byteLength
          || await digestFile(item.stagingPath) !== item.file.sha256) {
          throw new Error("studio-model-installer-derived-source-invalid");
        }
      } else {
        await downloadLockedFile(item.file, item.stagingPath, fetchImpl);
      }
    }
    for (const item of pending) {
      try {
        await inspectModelPath(resolvedRoot, item.file.relativePath);
      } catch {
        throw new Error("studio-model-installer-path-unsafe");
      }
    }
    for (const item of pending) {
      await mkdir(dirname(item.destination), { recursive: true, mode: 0o700 });
    }
    for (const item of pending) {
      try {
        await inspectModelPath(resolvedRoot, item.file.relativePath);
      } catch {
        throw new Error("studio-model-installer-path-unsafe");
      }
      await chmod(item.stagingPath, item.file.executable ? 0o700 : 0o600);
    }
    for (const item of pending) {
      await rename(item.stagingPath, item.destination);
    }
    for (const file of files) {
      if ((await inspectModelFile(resolvedRoot, file)).status !== "ready") {
        throw new Error("studio-model-installer-commit-verification-failed");
      }
    }
  } finally {
    await rm(stagingRoot, { recursive: true, force: true });
  }
  return { artifact: artifact.id, status: "ready", root: resolvedRoot };
}

export async function installPersonaBundle({
  lockPath,
  root,
  derivedRoot = null,
  fetchImpl = fetch,
  installArtifactImpl = installArtifact,
}) {
  const lock = await loadModelLock(lockPath);
  for (const artifactId of PERSONA_ARTIFACT_IDS) {
    let artifact;
    try {
      artifact = findArtifact(lock, artifactId);
    } catch {
      throw new Error("studio-model-installer-artifact-unknown");
    }
    const approvalIssue = artifactApprovalIssue(artifact);
    if (approvalIssue !== null) throw new Error(`studio-model-installer-${approvalIssue}`);
  }

  const artifacts = [];
  for (const artifactId of PERSONA_ARTIFACT_IDS) {
    artifacts.push(await installArtifactImpl({
      artifactId, lockPath, root, derivedRoot, fetchImpl,
    }));
  }
  return Object.freeze({ status: "ready", artifacts: Object.freeze(artifacts) });
}
