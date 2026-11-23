import assert from "node:assert/strict";
import { createHash } from "node:crypto";

const testApi = process.env.VITEST ? await import("vitest") : await import("node:test");
const test = testApi.test ?? testApi.default;

import {
  collectWorkerRuntimeBuilderTool,
  generateWorkerRuntimeBuilderToolProposal,
  renderWorkerRuntimeBuilderToolProposal,
} from "../collect-worker-runtime-builder-tool.mjs";

const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const archiveSha = "546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843";
const manifestSha = "a".repeat(64);
const executableSha = "2b4ccdac26598ca8c300e5c36d24297fa1471c350c46d2f34c835bf06be303ab";

function fixture() {
  const distributionManifestBytes = Buffer.from(JSON.stringify({
    announcement_tag: "0.12.3",
    announcement_is_prerelease: false,
    artifacts: {
      "uv-aarch64-apple-darwin.tar.gz": {
        name: "uv-aarch64-apple-darwin.tar.gz",
        kind: "executable-zip",
        target_triples: ["aarch64-apple-darwin"],
        assets: [{ id: "uv-aarch64-apple-darwin-exe-uv", name: "uv", path: "uv", kind: "executable" }],
        checksum: "uv-aarch64-apple-darwin.tar.gz.sha256",
        checksums: { sha256: archiveSha },
      },
    },
  }));
  const releaseMetadataBytes = Buffer.from(JSON.stringify({
    id: 366879609,
    tag_name: "0.12.3",
    draft: false,
    prerelease: false,
    published_at: "2026-08-07T16:34:42Z",
    assets: [{
      id: 505387705,
      name: "dist-manifest.json",
      size: distributionManifestBytes.length,
      digest: `sha256:${sha256(distributionManifestBytes)}`,
      browser_download_url: "https://github.com/astral-sh/uv/releases/download/0.12.3/dist-manifest.json",
    }, {
      id: 505387707,
      name: "uv-aarch64-apple-darwin.tar.gz",
      size: 17_686_637,
      digest: `sha256:${archiveSha}`,
      url: "https://api.github.com/repos/astral-sh/uv/releases/assets/505387707",
      browser_download_url: "https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-apple-darwin.tar.gz",
    }],
  }));
  return {
    releaseMetadataBytes,
    distributionManifestBytes,
    redirectObservation: {
      statusCode: 302,
      targetScheme: "https:",
      targetHost: "release-assets.githubusercontent.com",
      targetPath: "/github-production-release-asset/699532645/3433a4a2-db55-4d29-8976-5438ac5ee0e0",
    },
    localDiagnostic: {
      byteLength: 40_152_144,
      sha256: executableSha,
      nlink: 1,
      versionOutput: "uv 0.12.3 (507230998 2026-08-07 aarch64-apple-darwin)",
    },
  };
}

test("proposal binds official release metadata, exact archive, redirect, and conditional executable identity", () => {
  const packet = collectWorkerRuntimeBuilderTool(fixture());
  assert.match(packet.packetId, /^worker-runtime-builder-tool-v1-sha256-[a-f0-9]{64}$/u);
  assert.equal(packet.tool.archive.sha256, archiveSha);
  assert.equal(packet.tool.archive.byteLength, 17_686_637);
  assert.equal(packet.tool.archive.url, "https://api.github.com/repos/astral-sh/uv/releases/assets/505387707");
  assert.equal(packet.tool.executable.memberPath, "uv-aarch64-apple-darwin/uv");
  assert.equal(packet.tool.executable.sha256, executableSha);
  assert.equal(packet.tool.executable.officialMatchState, "unverified-until-approved-archive-retrieval");
  assert.equal(packet.authorization.installAuthorized, false);
  assert.equal(packet.authorization.modelExecutionAuthorized, false);
});

test("proposal rejects changed official response bytes, wrong archive identity, and unsafe redirects", () => {
  const base = fixture();
  const changedRelease = Buffer.from(base.releaseMetadataBytes);
  changedRelease[0] ^= 1;
  assert.throws(() => collectWorkerRuntimeBuilderTool({ ...base, releaseMetadataBytes: changedRelease }), /builder-tool-release-metadata-invalid/);

  const manifest = JSON.parse(base.distributionManifestBytes);
  manifest.artifacts["uv-aarch64-apple-darwin.tar.gz"].checksums.sha256 = "f".repeat(64);
  assert.throws(() => collectWorkerRuntimeBuilderTool({
    ...base,
    distributionManifestBytes: Buffer.from(JSON.stringify(manifest)),
  }), /builder-tool-archive-identity-mismatch/);

  assert.throws(() => collectWorkerRuntimeBuilderTool({
    ...base,
    redirectObservation: { ...base.redirectObservation, targetHost: "evil.example" },
  }), /builder-tool-redirect-invalid/);
});

test("metadata generation never calls an injected archive body fetch", async () => {
  const base = fixture();
  let bodyFetchCalls = 0;
  const packet = await generateWorkerRuntimeBuilderToolProposal({
    metadataFetch: async (kind) => kind === "release"
      ? base.releaseMetadataBytes
      : base.distributionManifestBytes,
    redirectProbe: async () => base.redirectObservation,
    localDiagnostic: base.localDiagnostic,
    archiveBodyFetch: async () => { bodyFetchCalls += 1; },
  });
  assert.equal(bodyFetchCalls, 0);
  assert.match(packet.packetId, /^worker-runtime-builder-tool-v1-sha256-/u);
});

test("document is non-authorizing and names exact approval and separate later gates", () => {
  const packet = collectWorkerRuntimeBuilderTool(fixture());
  const markdown = renderWorkerRuntimeBuilderToolProposal(packet);
  assert.match(markdown, new RegExp(`approve exact worker runtime builder tool packet ${packet.packetId}`, "u"));
  assert.match(markdown, /approved=false/u);
  assert.match(markdown, /does not authorize.*models\.lock\.json.*installation.*model execution/isu);
  assert.match(markdown, /unverified-until-approved-archive-retrieval/u);
});
