import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { access, lstat, mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { collectApprovalCandidate } from "../collect-studio-model-approval.mjs";

const revision = "a".repeat(40);
const fixtureBytes = Buffer.from("fixed model candidate bytes");
const fixtureSha256 = createHash("sha256").update(fixtureBytes).digest("hex");

function fixedSource(overrides = {}) {
  return {
    artifactId: "synthetic-model",
    sourceId: "weights",
    kind: "weights",
    sourceUrl: `https://huggingface.co/example/model/resolve/${revision}/model.safetensors`,
    sourceRevision: revision,
    relativePath: "models/model.safetensors",
    maxByteLength: fixtureBytes.byteLength,
    license: {
      spdxExpression: "MIT",
      evidenceUrl: `https://huggingface.co/example/model/raw/${revision}/LICENSE`,
    },
    ...overrides,
  };
}

async function stagingFixture(t) {
  const directory = await mkdtemp(join(tmpdir(), "studio-model-approval-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return directory;
}

function bytesResponse(bytes = fixtureBytes, init = {}) {
  return new Response(bytes, {
    status: 200,
    headers: { "content-length": String(bytes.byteLength) },
    ...init,
  });
}

test("rejects a mutable model URL before fetch or staging", async (t) => {
  const stagingRoot = await stagingFixture(t);
  let fetchCalls = 0;

  await assert.rejects(
    collectApprovalCandidate({
      sources: [fixedSource({
        sourceUrl: "https://huggingface.co/example/model/resolve/main/model.safetensors",
      })],
      fetchImpl: async () => {
        fetchCalls += 1;
        return bytesResponse();
      },
      stagingRoot,
    }),
    /studio-model-approval-source-not-revision-bound/,
  );

  assert.equal(fetchCalls, 0);
  assert.deepEqual(await readdir(stagingRoot), []);
});

test("records exact bytes and license evidence without approving", async (t) => {
  const stagingRoot = await stagingFixture(t);
  let requestOptions;

  const packet = await collectApprovalCandidate({
    sources: [fixedSource()],
    fetchImpl: async (_url, options) => {
      requestOptions = options;
      return bytesResponse();
    },
    stagingRoot,
  });

  assert.equal(requestOptions.redirect, "error");
  assert.deepEqual(packet, {
    schemaVersion: 1,
    status: "candidate",
    approved: false,
    files: [{
      artifactId: "synthetic-model",
      sourceId: "weights",
      kind: "weights",
      sourceUrl: `https://huggingface.co/example/model/resolve/${revision}/model.safetensors`,
      sourceRevision: revision,
      relativePath: "models/model.safetensors",
      byteLength: fixtureBytes.byteLength,
      sha256: fixtureSha256,
    }],
    licenses: [{
      artifactId: "synthetic-model",
      sourceId: "weights",
      sourceRevision: revision,
      spdxExpression: "MIT",
      evidenceUrl: `https://huggingface.co/example/model/raw/${revision}/LICENSE`,
    }],
  });
  assert.equal(Object.isFrozen(packet), true);
  assert.equal(Object.isFrozen(packet.files), true);
  assert.equal(Object.isFrozen(packet.files[0]), true);
  assert.deepEqual(await readdir(stagingRoot), []);
});

test("accepts a GitHub source archive and license bound to the same commit", async (t) => {
  const stagingRoot = await stagingFixture(t);
  const packet = await collectApprovalCandidate({
    sources: [fixedSource({
      sourceId: "code",
      kind: "code",
      sourceUrl: `https://codeload.github.com/example/model/tar.gz/${revision}`,
      relativePath: "packages/model.tar.gz",
      license: {
        spdxExpression: "MIT",
        evidenceUrl: `https://raw.githubusercontent.com/example/model/${revision}/LICENSE`,
      },
    })],
    fetchImpl: async () => bytesResponse(),
    stagingRoot,
  });

  assert.equal(packet.files[0].sourceRevision, revision);
  assert.equal(packet.files[0].sha256, fixtureSha256);
  assert.equal(packet.licenses[0].evidenceUrl,
    `https://raw.githubusercontent.com/example/model/${revision}/LICENSE`);
});

test("snapshots caller-owned source metadata before the first await", async (t) => {
  const stagingRoot = await stagingFixture(t);
  const source = fixedSource();
  const originalUrl = source.sourceUrl;
  const collection = collectApprovalCandidate({
    sources: [source],
    fetchImpl: async (url) => {
      assert.equal(url, originalUrl);
      return bytesResponse();
    },
    stagingRoot,
  });

  source.sourceUrl = "https://huggingface.co/example/model/resolve/main/attacker.bin";
  source.relativePath = "changed/after-validation.bin";
  source.license.evidenceUrl = "https://attacker.invalid/LICENSE";

  const packet = await collection;
  assert.equal(packet.files[0].sourceUrl, originalUrl);
  assert.equal(packet.files[0].relativePath, "models/model.safetensors");
  assert.equal(packet.licenses[0].evidenceUrl,
    `https://huggingface.co/example/model/raw/${revision}/LICENSE`);
});

test("rejects redirects and removes its private staging directory", async (t) => {
  const stagingRoot = await stagingFixture(t);

  await assert.rejects(
    collectApprovalCandidate({
      sources: [fixedSource()],
      fetchImpl: async () => new Response(null, {
        status: 302,
        headers: { location: "https://cdn.invalid/model.safetensors" },
      }),
      stagingRoot,
    }),
    /studio-model-approval-redirect-rejected/,
  );

  assert.deepEqual(await readdir(stagingRoot), []);
});

test("rejects a response whose declared size exceeds the bound", async (t) => {
  const stagingRoot = await stagingFixture(t);
  const body = new ReadableStream({
    pull(controller) {
      controller.enqueue(fixtureBytes);
      controller.close();
    },
  });

  await assert.rejects(
    collectApprovalCandidate({
      sources: [fixedSource({ maxByteLength: fixtureBytes.byteLength - 1 })],
      fetchImpl: async () => new Response(body, {
        status: 200,
        headers: { "content-length": String(fixtureBytes.byteLength) },
      }),
      stagingRoot,
    }),
    /studio-model-approval-file-too-large/,
  );

  assert.deepEqual(await readdir(stagingRoot), []);
});

test("rejects an empty candidate file", async (t) => {
  const stagingRoot = await stagingFixture(t);

  await assert.rejects(
    collectApprovalCandidate({
      sources: [fixedSource()],
      fetchImpl: async () => bytesResponse(Buffer.alloc(0)),
      stagingRoot,
    }),
    /studio-model-approval-file-empty/,
  );

  assert.deepEqual(await readdir(stagingRoot), []);
});

test("bounds streamed bytes, uses mode 0600, and cleans partial files", async (t) => {
  const stagingRoot = await stagingFixture(t);
  let streamController;
  const body = new ReadableStream({
    start(controller) {
      streamController = controller;
    },
  });

  const rejection = assert.rejects(
    collectApprovalCandidate({
      sources: [fixedSource()],
      fetchImpl: async () => new Response(body),
      stagingRoot,
    }),
    /studio-model-approval-file-too-large/,
  );

  let temporaryPath;
  for (let attempt = 0; attempt < 100 && !temporaryPath; attempt += 1) {
    const [privateDirectory] = await readdir(stagingRoot);
    if (privateDirectory) {
      const [temporaryFile] = await readdir(join(stagingRoot, privateDirectory));
      if (temporaryFile) temporaryPath = join(stagingRoot, privateDirectory, temporaryFile);
    }
    if (!temporaryPath) await new Promise((resolve) => setTimeout(resolve, 1));
  }
  assert.ok(temporaryPath, "collector did not create a bounded private staging file");
  assert.equal((await lstat(temporaryPath)).mode & 0o777, 0o600);
  assert.deepEqual(await readFile(temporaryPath), Buffer.alloc(0));

  streamController.enqueue(Buffer.concat([fixtureBytes, Buffer.from("x")]));
  streamController.close();
  await rejection;
  assert.deepEqual(await readdir(stagingRoot), []);
});

test("rejects incomplete license evidence before fetch", async (t) => {
  const stagingRoot = await stagingFixture(t);
  let fetchCalls = 0;

  await assert.rejects(
    collectApprovalCandidate({
      sources: [fixedSource({ license: { spdxExpression: "MIT", evidenceUrl: null } })],
      fetchImpl: async () => {
        fetchCalls += 1;
        return bytesResponse();
      },
      stagingRoot,
    }),
    /studio-model-approval-license-evidence-invalid/,
  );

  assert.equal(fetchCalls, 0);
  assert.deepEqual(await readdir(stagingRoot), []);
  await assert.doesNotReject(access(stagingRoot));
});
