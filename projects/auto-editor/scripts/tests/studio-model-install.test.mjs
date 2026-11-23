import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { access, chmod, lstat, mkdir, mkdtemp, readdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { installArtifact, installPersonaBundle } from "../studio-model-installer-lib.mjs";

const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const revision = "a".repeat(40);
const execFileAsync = promisify(execFile);
const checkerPath = fileURLToPath(new URL("../check-studio-models.mjs", import.meta.url));
const installerPath = fileURLToPath(new URL("../install-studio-models.mjs", import.meta.url));
const codeBytes = Buffer.from("#!/bin/sh\nprintf synthetic-ready\n");
const weightBytes = Buffer.from("synthetic-weights");

function evidence(kind, sourceUrl) {
  return {
    applies: true,
    sourceId: kind,
    sourceRevision: revision,
    spdxExpression: "MIT",
    evidenceUrl: `${sourceUrl}/raw/${revision}/LICENSE`,
  };
}

function approvedArtifact() {
  const codeUrl = "https://models.invalid/org/code";
  const weightsUrl = "https://models.invalid/org/weights";
  return {
    id: "synthetic-installable",
    version: "fixture-v1",
    artifactKind: "binary-model",
    sources: [
      {
        id: "code",
        kind: "code",
        sourceUrl: codeUrl,
        sourceRevision: revision,
        inventoryVerified: true,
        files: [{
          relativePath: "bin/whisper-cli",
          sourceUrl: `${codeUrl}/raw/${revision}/whisper-cli`,
          required: true,
          executable: true,
          sha256: sha256(codeBytes),
          byteLength: codeBytes.byteLength,
        }],
      },
      {
        id: "weights",
        kind: "weights",
        sourceUrl: weightsUrl,
        sourceRevision: revision,
        inventoryVerified: true,
        files: [{
          relativePath: "models/model.bin",
          sourceUrl: `${weightsUrl}/resolve/${revision}/model.bin`,
          required: true,
          executable: false,
          sha256: sha256(weightBytes),
          byteLength: weightBytes.byteLength,
        }],
      },
    ],
    licenseEvidence: {
      code: evidence("code", codeUrl),
      weights: evidence("weights", weightsUrl),
    },
    licenseReview: "approved",
    runtimeBackend: "synthetic",
    quantization: null,
    enabled: true,
    approved: true,
    approvedBy: "synthetic-reviewer",
    approvedAt: "2026-09-12T06:00:00.000Z",
    testedDevices: [],
    testSetVersion: null,
  };
}

function approvedDerivedArtifact() {
  const artifact = approvedArtifact();
  const derivedBytes = Buffer.from("#!/bin/sh\nprintf derived-ready\n");
  artifact.id = "synthetic-derived-runtime";
  artifact.runtimeFiles = [{
    relativePath: "derived/bin/runtime-cli",
    candidateRelativePath: "derived/bin/runtime-cli",
    required: true,
    executable: true,
    sourceKind: "code",
    sha256: sha256(derivedBytes),
    byteLength: derivedBytes.byteLength,
  }];
  artifact.derivationReceipt = {
    schemaVersion: 1,
    recipeId: "synthetic-derived-runtime-v1",
    inputSourceIds: ["code"],
    outputTreeSha256: "1".repeat(64),
    probeReportSha256: "2".repeat(64),
    status: "succeeded",
    syntheticInput: true,
    realModel: true,
  };
  return { artifact, derivedBytes };
}

async function fixture(t, mutate = () => {}) {
  const directory = await mkdtemp(join(tmpdir(), "studio-installer-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const root = join(directory, "models");
  const lockPath = join(directory, "models.lock.json");
  const artifact = approvedArtifact();
  mutate(artifact);
  await writeFile(lockPath, JSON.stringify({ schemaVersion: 1, artifacts: [artifact] }));
  return { root, lockPath };
}

async function absent(path) {
  await assert.rejects(access(path));
}

async function checkedStatus(lockPath, root) {
  const { stdout } = await execFileAsync(process.execPath, [
    checkerPath, "--json", "--lock", lockPath, "--root", root,
  ]);
  return JSON.parse(stdout).artifacts[0].status;
}

test("metadata gates reject before fetch and before creating the model root", async (t) => {
  const cases = [
    ["independent code license", (artifact) => { artifact.licenseEvidence.code.sourceId = "weights"; }],
    ["independent weights license", (artifact) => { artifact.licenseEvidence.weights.sourceId = "code"; }],
    ["revision-bound file URL", (artifact) => { artifact.sources[1].files[0].sourceUrl = "https://models.invalid/org/weights/resolve/bad/model.bin"; }],
    ["non-null SHA", (artifact) => { artifact.sources[1].files[0].sha256 = null; }],
    ["non-null size", (artifact) => { artifact.sources[1].files[0].byteLength = null; }],
  ];
  for (const [name, mutate] of cases) {
    await t.test(name, async (subtest) => {
      const { root, lockPath } = await fixture(subtest, mutate);
      let fetchCalls = 0;
      await assert.rejects(
        installArtifact({
          artifactId: "synthetic-installable",
          lockPath,
          root,
          fetchImpl: async () => { fetchCalls += 1; throw new Error("must-not-fetch"); },
        }),
        /studio-model-installer-(?:license-evidence-invalid|lock-invalid)/,
      );
      assert.equal(fetchCalls, 0);
      await absent(root);
    });
  }
});

test("download length, digest, and interruption failures leave no installed residue", async (t) => {
  const cases = [
    ["response too long", Buffer.concat([weightBytes, Buffer.from("x")]), null],
    ["response too short", weightBytes.subarray(0, 2), null],
    ["digest mismatch", weightBytes, (artifact) => { artifact.sources[1].files[0].sha256 = "0".repeat(64); }],
    ["interrupted stream", null, null],
  ];
  for (const [name, responseBytes, mutate] of cases) {
    await t.test(name, async (subtest) => {
      const { root, lockPath } = await fixture(subtest, mutate ?? (() => {}));
      const fetchImpl = async (url) => {
        if (url.endsWith("whisper-cli")) return new Response(codeBytes);
        if (name === "interrupted stream") {
          return new Response(new ReadableStream({
            start(controller) {
              controller.enqueue(weightBytes.subarray(0, 2));
              controller.error(new Error("synthetic-interruption"));
            },
          }));
        }
        return new Response(responseBytes);
      };

      await assert.rejects(
        installArtifact({
          artifactId: "synthetic-installable",
          lockPath,
          root,
          fetchImpl,
        }),
        /studio-model-installer-(?:digest-mismatch|download-failed)/,
      );
      await absent(root);
      const directoryEntries = await readFile(lockPath, "utf8");
      assert.match(directoryEntries, /synthetic-installable/);
    });
  }
});

test("approved code and weights are verified in staging before installation", async (t) => {
  const { root, lockPath } = await fixture(t);
  const result = await installArtifact({
    artifactId: "synthetic-installable",
    lockPath,
    root,
    fetchImpl: async (url) => new Response(url.endsWith("whisper-cli") ? codeBytes : weightBytes),
  });

  assert.deepEqual(result, { artifact: "synthetic-installable", status: "ready", root });
  const binaryPath = join(root, "bin/whisper-cli");
  const modelPath = join(root, "models/model.bin");
  assert.deepEqual(await readFile(binaryPath), codeBytes);
  assert.deepEqual(await readFile(modelPath), weightBytes);
  await access(binaryPath, constants.X_OK);
  assert.equal((await lstat(modelPath)).mode & 0o111, 0);
  assert.equal(await checkedStatus(lockPath, root), "ready");
  assert.equal((await execFileAsync(binaryPath)).stdout, "synthetic-ready");

  await chmod(binaryPath, 0o600);
  assert.equal(await checkedStatus(lockPath, root), "permission-mismatch");
  await chmod(binaryPath, 0o700);
  await chmod(modelPath, 0o700);
  assert.equal(await checkedStatus(lockPath, root), "permission-mismatch");
});

test("fixed codeload GitHub archive binds to the matching raw license revision", async (t) => {
  const { root, lockPath } = await fixture(t, (artifact) => {
    const source = artifact.sources[0];
    source.sourceUrl = "https://codeload.github.com/example/code";
    source.files[0].sourceUrl = `https://codeload.github.com/example/code/tar.gz/${revision}`;
    artifact.licenseEvidence.code.evidenceUrl =
      `https://raw.githubusercontent.com/example/code/${revision}/LICENSE`;
  });

  const result = await installArtifact({
    artifactId: "synthetic-installable",
    lockPath,
    root,
    fetchImpl: async (url) => new Response(url.includes("codeload.github.com") ? codeBytes : weightBytes),
  });

  assert.equal(result.status, "ready");
  assert.deepEqual(await readFile(join(root, "bin/whisper-cli")), codeBytes);
});

test("derived runtime requires an explicit verified candidate root before installation", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "studio-derived-installer-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const root = join(directory, "models");
  const lockPath = join(directory, "models.lock.json");
  const { artifact } = approvedDerivedArtifact();
  await writeFile(lockPath, JSON.stringify({ schemaVersion: 1, artifacts: [artifact] }));

  await assert.rejects(
    installArtifact({
      artifactId: artifact.id,
      lockPath,
      root,
      fetchImpl: async () => { throw new Error("network-must-not-run"); },
    }),
    /studio-model-installer-derived-source-required/,
  );
  await absent(root);
});

test("derived runtime is copied only after candidate digest and permissions match", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "studio-derived-installer-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const root = join(directory, "models");
  const derivedRoot = join(directory, "candidate");
  const lockPath = join(directory, "models.lock.json");
  const { artifact, derivedBytes } = approvedDerivedArtifact();
  const candidatePath = join(derivedRoot, "derived/bin/runtime-cli");
  await mkdir(join(derivedRoot, "derived/bin"), { recursive: true });
  await writeFile(candidatePath, derivedBytes, { mode: 0o700 });
  await writeFile(lockPath, JSON.stringify({ schemaVersion: 1, artifacts: [artifact] }));

  const result = await installArtifact({
    artifactId: artifact.id,
    lockPath,
    root,
    derivedRoot,
    fetchImpl: async () => { throw new Error("network-must-not-run"); },
  });

  assert.equal(result.status, "ready");
  assert.deepEqual(await readFile(join(root, "derived/bin/runtime-cli")), derivedBytes);
  assert.equal(await checkedStatus(lockPath, root), "ready");
  assert.equal((await execFileAsync(join(root, "derived/bin/runtime-cli"))).stdout, "derived-ready");
});

test("derived runtime digest mismatch leaves the destination absent", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "studio-derived-installer-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const root = join(directory, "models");
  const derivedRoot = join(directory, "candidate");
  const lockPath = join(directory, "models.lock.json");
  const { artifact } = approvedDerivedArtifact();
  const candidatePath = join(derivedRoot, "derived/bin/runtime-cli");
  await mkdir(join(derivedRoot, "derived/bin"), { recursive: true });
  await writeFile(candidatePath, "tampered", { mode: 0o700 });
  await writeFile(lockPath, JSON.stringify({ schemaVersion: 1, artifacts: [artifact] }));

  await assert.rejects(
    installArtifact({ artifactId: artifact.id, lockPath, root, derivedRoot }),
    /studio-model-installer-derived-source-invalid/,
  );
  await absent(root);
});

test("derived runtime still rejects mutable or incomplete input provenance", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "studio-derived-installer-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const root = join(directory, "models");
  const lockPath = join(directory, "models.lock.json");
  const { artifact } = approvedDerivedArtifact();
  artifact.sources[0].files[0].sourceUrl = "https://models.invalid/org/code/raw/main/whisper-cli";
  await writeFile(lockPath, JSON.stringify({ schemaVersion: 1, artifacts: [artifact] }));

  await assert.rejects(
    installArtifact({ artifactId: artifact.id, lockPath, root, derivedRoot: join(directory, "candidate") }),
    /studio-model-installer-lock-invalid/,
  );
  await absent(root);
});

test("one verified Hugging Face CDN hop retains revision, size, and digest binding", async (t) => {
  const { root, lockPath } = await fixture(t, (artifact) => {
    const source = artifact.sources[1];
    source.sourceUrl = "https://huggingface.co/example/weights";
    source.files[0].sourceUrl = `https://huggingface.co/example/weights/resolve/${revision}/model.bin`;
    artifact.licenseEvidence.weights.evidenceUrl =
      `https://huggingface.co/example/weights/raw/${revision}/LICENSE`;
  });
  const requested = [];

  const result = await installArtifact({
    artifactId: "synthetic-installable",
    lockPath,
    root,
    fetchImpl: async (url, options) => {
      requested.push({ url, options });
      if (url.endsWith("whisper-cli")) return new Response(codeBytes);
      if (url.includes("/resolve/")) {
        return new Response(null, {
          status: 302,
          headers: {
            location: "https://us.aws.cdn.hf.co/xet-bridge-us/fixed/signed",
            "x-repo-commit": revision,
            "x-linked-size": String(weightBytes.byteLength),
            "x-linked-etag": `\"${sha256(weightBytes)}\"`,
          },
        });
      }
      return new Response(weightBytes);
    },
  });

  assert.equal(result.status, "ready");
  assert.equal(requested.length, 3);
  assert.equal(requested[1].options.redirect, "manual");
  assert.equal(requested[2].options.redirect, "error");
  assert.equal(requested[2].url, "https://us.aws.cdn.hf.co/xet-bridge-us/fixed/signed");
});

test("one same-origin Hugging Face cache hop retains repository revision and file path", async (t) => {
  const { root, lockPath } = await fixture(t, (artifact) => {
    const source = artifact.sources[1];
    source.sourceUrl = "https://huggingface.co/example/weights";
    source.files[0].sourceUrl = `https://huggingface.co/example/weights/resolve/${revision}/model.bin`;
    artifact.licenseEvidence.weights.evidenceUrl =
      `https://huggingface.co/example/weights/raw/${revision}/LICENSE`;
  });
  const requested = [];
  const cacheUrl = `https://huggingface.co/api/resolve-cache/models/example/weights/${revision}/model.bin`;

  const result = await installArtifact({
    artifactId: "synthetic-installable",
    lockPath,
    root,
    fetchImpl: async (url, options) => {
      requested.push({ url, options });
      if (url.endsWith("whisper-cli")) return new Response(codeBytes);
      if (url.includes("/resolve/")) {
        return new Response(null, {
          status: 307,
          headers: {
            location: `/api/resolve-cache/models/example/weights/${revision}/model.bin?etag=fixed`,
            "x-repo-commit": revision,
            "x-linked-etag": '"git-object-sha1"',
          },
        });
      }
      return new Response(weightBytes);
    },
  });

  assert.equal(result.status, "ready");
  assert.equal(requested[2].url, cacheUrl + "?etag=fixed");
  assert.equal(requested[2].options.redirect, "error");
});

test("Hugging Face redirect rejects changed host, revision, or a second hop", async (t) => {
  const cases = [
    ["changed host", "https://example.invalid/cache/model.bin", revision, 200],
    ["changed revision", `/api/resolve-cache/models/example/weights/${"b".repeat(40)}/model.bin`, revision, 200],
    ["second hop", `/api/resolve-cache/models/example/weights/${revision}/model.bin`, revision, 302],
  ];
  for (const [name, location, repoCommit, finalStatus] of cases) {
    await t.test(name, async (subtest) => {
      const { root, lockPath } = await fixture(subtest, (artifact) => {
        const source = artifact.sources[1];
        source.sourceUrl = "https://huggingface.co/example/weights";
        source.files[0].sourceUrl = `https://huggingface.co/example/weights/resolve/${revision}/model.bin`;
        artifact.licenseEvidence.weights.evidenceUrl =
          `https://huggingface.co/example/weights/raw/${revision}/LICENSE`;
      });
      await assert.rejects(
        installArtifact({
          artifactId: "synthetic-installable",
          lockPath,
          root,
          fetchImpl: async (url) => {
            if (url.endsWith("whisper-cli")) return new Response(codeBytes);
            if (url.includes("/resolve/")) {
              return new Response(null, {
                status: 307,
                headers: { location, "x-repo-commit": repoCommit },
              });
            }
            return new Response(finalStatus === 200 ? weightBytes : null, {
              status: finalStatus,
              headers: finalStatus === 302 ? { location: "https://example.invalid/again" } : {},
            });
          },
        }),
        /studio-model-installer-redirect-rejected/,
      );
      await absent(root);
    });
  }
});

test("symlinked roots, parents, and targets are rejected before fetch without external mutation", async (t) => {
  for (const kind of ["root", "parent", "target"]) {
    await t.test(kind, async (subtest) => {
      const { root, lockPath } = await fixture(subtest);
      const external = join(root, "..", `external-${kind}`);
      await mkdir(external, { recursive: true });
      const sentinel = join(external, "sentinel");
      await writeFile(sentinel, "unchanged");
      if (kind === "root") {
        await symlink(external, root);
      } else if (kind === "parent") {
        await mkdir(root, { recursive: true });
        await symlink(external, join(root, "bin"));
      } else {
        await mkdir(root, { recursive: true });
        await mkdir(join(root, "bin"), { recursive: true });
        await symlink(sentinel, join(root, "bin", "whisper-cli"));
      }
      let fetchCalls = 0;

      await assert.rejects(
        installArtifact({
          artifactId: "synthetic-installable",
          lockPath,
          root,
          fetchImpl: async () => { fetchCalls += 1; return new Response(codeBytes); },
        }),
        /studio-model-installer-path-unsafe/,
      );
      assert.equal(fetchCalls, 0);
      assert.equal(await readFile(sentinel, "utf8"), "unchanged");
      assert.deepEqual(await readdir(external), ["sentinel"]);
      assert.equal(await checkedStatus(lockPath, root), "unsafe-path");
    });
  }
});

test("persona bundle installs only approved Whisper q5_1 and official-source Qwen components", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "studio-persona-installer-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const root = join(directory, "models");
  const lockPath = join(directory, "models.lock.json");
  const artifactIds = [
    "whisper.cpp-1.9.2-cli-darwin-arm64",
    "whisper.cpp-1.9.2-small-q5_1",
    "mlx-vlm-0.6.6",
    "qwen3-vl-2b-instruct-4bit",
  ];
  await writeFile(lockPath, JSON.stringify({
    schemaVersion: 1,
    artifacts: artifactIds.map((id) => ({ ...approvedArtifact(), id })),
  }));
  const installed = [];

  const result = await installPersonaBundle({
    lockPath,
    root,
    installArtifactImpl: async ({ artifactId }) => {
      installed.push(artifactId);
      return { artifact: artifactId, status: "ready", root };
    },
  });

  assert.deepEqual(installed, artifactIds);
  assert.deepEqual(result.artifacts.map((item) => item.artifact), artifactIds);
  assert.equal(installed.includes("pyscenedetect-0.7.1"), false);
  assert.equal(installed.includes("openclip-3.3.0-vit-b-32"), false);
  assert.equal(installed.includes("whisper.cpp-1.9.2-small-q5_0"), false);
});

test("persona CLI installs the exact approved bundle with an explicit derived root", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "studio-persona-cli-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const root = join(directory, "models");
  const derivedRoot = join(directory, "candidate");
  const lockPath = join(directory, "models.lock.json");
  const artifactIds = [
    "whisper.cpp-1.9.2-cli-darwin-arm64",
    "whisper.cpp-1.9.2-small-q5_1",
    "mlx-vlm-0.6.6",
    "qwen3-vl-2b-instruct-4bit",
  ];
  await mkdir(join(root, "bin"), { recursive: true });
  await mkdir(join(root, "models"), { recursive: true });
  await writeFile(join(root, "bin/whisper-cli"), codeBytes, { mode: 0o700 });
  await writeFile(join(root, "models/model.bin"), weightBytes, { mode: 0o600 });
  await writeFile(lockPath, JSON.stringify({
    schemaVersion: 1,
    artifacts: artifactIds.map((id) => ({ ...approvedArtifact(), id })),
  }));

  const { stdout } = await execFileAsync(process.execPath, [
    installerPath,
    "--persona",
    "--lock", lockPath,
    "--root", root,
    "--derived-root", derivedRoot,
  ]);
  const result = JSON.parse(stdout);

  assert.equal(result.status, "ready");
  assert.deepEqual(result.artifacts.map((item) => item.artifact), artifactIds);
});

test("ordinary artifact modes never accept worker-runtime candidate arguments", async () => {
  await assert.rejects(
    execFileAsync(process.execPath, [
      installerPath,
      "--artifact", "synthetic-installable",
      "--candidate-root", "/tmp/synthetic-candidate",
    ]),
    (error) => error?.stderr?.includes("studio-model-installer-arguments-invalid"),
  );
});
