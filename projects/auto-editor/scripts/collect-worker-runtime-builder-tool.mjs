#!/usr/bin/env node

import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const RELEASE_URL = "https://api.github.com/repos/astral-sh/uv/releases/tags/0.12.3";
const ARCHIVE_URL = "https://api.github.com/repos/astral-sh/uv/releases/assets/505387707";
const ARCHIVE_BROWSER_URL = "https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-apple-darwin.tar.gz";
const MANIFEST_URL = "https://github.com/astral-sh/uv/releases/download/0.12.3/dist-manifest.json";
const ARCHIVE_NAME = "uv-aarch64-apple-darwin.tar.gz";
const ARCHIVE_BYTES = 17_686_637;
const ARCHIVE_SHA256 = "546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843";
const RELEASE_ASSET_PATH = "/github-production-release-asset/699532645/3433a4a2-db55-4d29-8976-5438ac5ee0e0";
const MAX_METADATA_BYTES = 2 * 1024 * 1024;

function fail(code, detail = "") { throw new Error(detail ? `${code}:${detail}` : code); }
function sha256(value) { return createHash("sha256").update(value).digest("hex"); }
function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function parseJson(bytes, code) {
  if (!Buffer.isBuffer(bytes) || bytes.length === 0 || bytes.length > MAX_METADATA_BYTES) fail(code);
  try { return JSON.parse(bytes.toString("utf8")); } catch { fail(code); }
}

function exactDigest(value) {
  if (!/^[a-f0-9]{64}$/u.test(value ?? "")) fail("builder-tool-digest-invalid");
  return value;
}

export function collectWorkerRuntimeBuilderTool({
  releaseMetadataBytes,
  distributionManifestBytes,
  redirectObservation,
  localDiagnostic,
}) {
  const release = parseJson(releaseMetadataBytes, "builder-tool-release-metadata-invalid");
  const manifest = parseJson(distributionManifestBytes, "builder-tool-distribution-manifest-invalid");
  if (
    release.id !== 366879609 || release.tag_name !== "0.12.3" || release.draft !== false
    || release.prerelease !== false || release.published_at !== "2026-08-07T16:34:42Z"
    || manifest.announcement_tag !== "0.12.3" || manifest.announcement_is_prerelease !== false
  ) fail("builder-tool-release-metadata-invalid");
  const archiveAsset = release.assets?.filter((item) => item?.name === ARCHIVE_NAME);
  const manifestAsset = release.assets?.filter((item) => item?.name === "dist-manifest.json");
  if (archiveAsset?.length !== 1 || manifestAsset?.length !== 1) fail("builder-tool-release-metadata-invalid");
  if (
    archiveAsset[0].id !== 505387707 || archiveAsset[0].size !== ARCHIVE_BYTES
    || archiveAsset[0].digest !== `sha256:${ARCHIVE_SHA256}`
    || archiveAsset[0].url !== ARCHIVE_URL
    || archiveAsset[0].browser_download_url !== ARCHIVE_BROWSER_URL
    || manifestAsset[0].id !== 505387705
    || manifestAsset[0].size !== distributionManifestBytes.length
    || manifestAsset[0].digest !== `sha256:${sha256(distributionManifestBytes)}`
    || manifestAsset[0].browser_download_url !== MANIFEST_URL
  ) fail("builder-tool-archive-identity-mismatch");
  const artifact = manifest.artifacts?.[ARCHIVE_NAME];
  const uvMembers = artifact?.assets?.filter((item) => item?.name === "uv" && item?.kind === "executable");
  if (
    artifact?.kind !== "executable-zip"
    || artifact?.target_triples?.length !== 1
    || artifact.target_triples[0] !== "aarch64-apple-darwin"
    || artifact.checksums?.sha256 !== ARCHIVE_SHA256
    || uvMembers?.length !== 1 || uvMembers[0].path !== "uv"
  ) fail("builder-tool-archive-identity-mismatch");
  if (
    redirectObservation?.statusCode !== 302
    || redirectObservation.targetScheme !== "https:"
    || redirectObservation.targetHost !== "release-assets.githubusercontent.com"
    || redirectObservation.targetPath !== RELEASE_ASSET_PATH
  ) fail("builder-tool-redirect-invalid");
  if (
    localDiagnostic?.byteLength !== 40_152_144
    || exactDigest(localDiagnostic.sha256) !== "2b4ccdac26598ca8c300e5c36d24297fa1471c350c46d2f34c835bf06be303ab"
    || localDiagnostic.nlink !== 1
    || localDiagnostic.versionOutput !== "uv 0.12.3 (507230998 2026-08-07 aarch64-apple-darwin)"
  ) fail("builder-tool-local-diagnostic-invalid");
  const core = {
    schemaVersion: 1,
    status: "awaiting-exact-builder-tool-approval",
    approved: false,
    parentMaterialPacketId: "worker-runtime-materials-v1-sha256-8147590c4832c5ad9ac04b2b9a370dc5b55b61832334a745ada538c2b8de4895",
    target: { platform: "darwin", architecture: "arm64" },
    tool: {
      name: "uv",
      version: "0.12.3",
      archive: {
        filename: ARCHIVE_NAME,
        url: ARCHIVE_URL,
        byteLength: ARCHIVE_BYTES,
        sha256: ARCHIVE_SHA256,
      },
      executable: {
        memberPath: "uv-aarch64-apple-darwin/uv",
        byteLength: localDiagnostic.byteLength,
        sha256: localDiagnostic.sha256,
        expectedVersionOutput: localDiagnostic.versionOutput,
        officialMatchState: "unverified-until-approved-archive-retrieval",
      },
    },
    metadataEvidence: {
      release: { url: RELEASE_URL, responseByteLength: releaseMetadataBytes.length, responseSha256: sha256(releaseMetadataBytes) },
      distributionManifest: { url: MANIFEST_URL, responseByteLength: distributionManifestBytes.length, responseSha256: sha256(distributionManifestBytes) },
    },
    redirectPolicy: {
      originalUrl: ARCHIVE_URL,
      statusCode: 302,
      maximumHops: 1,
      targetScheme: "https:",
      targetHost: "release-assets.githubusercontent.com",
      targetPath: RELEASE_ASSET_PATH,
      allowEphemeralSignedQuery: true,
      forwardAuthorization: false,
      forwardCookies: false,
      allowSecondRedirect: false,
    },
    authorization: {
      retrievalAuthorized: false,
      candidateBuildUseAuthorized: false,
      installAuthorized: false,
      modelsLockMutationAuthorized: false,
      modelExecutionAuthorized: false,
    },
  };
  return Object.freeze({ ...core, packetId: `worker-runtime-builder-tool-v1-sha256-${sha256(canonicalJson(core))}` });
}

async function boundedResponse(response, code) {
  if (!response?.ok || response.redirected || !response.body) fail(code);
  const chunks = [];
  let length = 0;
  for await (const chunk of response.body) {
    length += chunk.byteLength;
    if (length > MAX_METADATA_BYTES) fail(code);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

async function defaultMetadataFetch(kind) {
  const url = kind === "release" ? RELEASE_URL : MANIFEST_URL;
  const first = await fetch(url, {
    method: "GET", redirect: "manual", credentials: "omit", signal: AbortSignal.timeout(30_000),
    headers: { Accept: "application/json", "User-Agent": "wearable-runtime-builder-tool-metadata", "X-GitHub-Api-Version": "2022-11-28" },
  });
  if (kind === "release") return boundedResponse(first, "builder-tool-release-metadata-http-error");
  if (first.status !== 302) fail("builder-tool-manifest-redirect-invalid");
  const target = new URL(first.headers.get("location") ?? "", url);
  if (target.protocol !== "https:" || target.hostname !== "release-assets.githubusercontent.com") fail("builder-tool-manifest-redirect-invalid");
  const final = await fetch(target, {
    method: "GET", redirect: "error", credentials: "omit", signal: AbortSignal.timeout(30_000),
    headers: { Accept: "application/json", "User-Agent": "wearable-runtime-builder-tool-metadata" },
  });
  return boundedResponse(final, "builder-tool-manifest-http-error");
}

async function defaultRedirectProbe() {
  const response = await fetch(ARCHIVE_URL, {
    method: "HEAD", redirect: "manual", credentials: "omit", signal: AbortSignal.timeout(30_000),
    headers: { Accept: "application/octet-stream", "User-Agent": "wearable-runtime-builder-tool-metadata", "X-GitHub-Api-Version": "2022-11-28" },
  });
  const target = new URL(response.headers.get("location") ?? "", ARCHIVE_URL);
  return { statusCode: response.status, targetScheme: target.protocol, targetHost: target.hostname, targetPath: target.pathname };
}

export async function generateWorkerRuntimeBuilderToolProposal({
  metadataFetch = defaultMetadataFetch,
  redirectProbe = defaultRedirectProbe,
  localDiagnostic,
  archiveBodyFetch,
} = {}) {
  if (archiveBodyFetch !== undefined && typeof archiveBodyFetch !== "function") fail("builder-tool-body-fetch-invalid");
  const [releaseMetadataBytes, distributionManifestBytes, redirectObservation] = await Promise.all([
    metadataFetch("release"), metadataFetch("manifest"), redirectProbe(),
  ]);
  return collectWorkerRuntimeBuilderTool({ releaseMetadataBytes, distributionManifestBytes, redirectObservation, localDiagnostic });
}

export function renderWorkerRuntimeBuilderToolProposal(packet) {
  return `# Worker Runtime Builder Tool Proposal

This is a metadata-only, non-approved proposal. No uv archive body was downloaded.

- Packet ID: \`${packet.packetId}\`
- Status: \`${packet.status}\`
- \`approved=false\`
- Target: \`${packet.target.platform}-${packet.target.architecture}\`
- Tool: \`${packet.tool.name} ${packet.tool.version}\`
- Official archive: \`${packet.tool.archive.filename}\`, ${packet.tool.archive.byteLength} bytes, SHA-256 \`${packet.tool.archive.sha256}\`
- Immutable archive URL: ${packet.tool.archive.url}
- Exact executable member: \`${packet.tool.executable.memberPath}\`
- Expected executable identity: ${packet.tool.executable.byteLength} bytes, SHA-256 \`${packet.tool.executable.sha256}\`
- Official executable match state: \`${packet.tool.executable.officialMatchState}\`
- Release metadata: ${packet.metadataEvidence.release.url}, ${packet.metadataEvidence.release.responseByteLength} bytes, SHA-256 \`${packet.metadataEvidence.release.responseSha256}\`
- Distribution manifest: ${packet.metadataEvidence.distributionManifest.url}, ${packet.metadataEvidence.distributionManifest.responseByteLength} bytes, SHA-256 \`${packet.metadataEvidence.distributionManifest.responseSha256}\`
- Redirect: one HTTP 302 hop to \`${packet.redirectPolicy.targetHost}${packet.redirectPolicy.targetPath}\`; signed query may vary; credentials/cookies are never forwarded.

The expected executable SHA-256 and length above are an untrusted local diagnostic until the exact approved official archive is retrieved, rehashed, boundedly extracted, and the exact member is independently rehashed. Any mismatch fails closed and requires a new proposal; it must never be treated as an official match by assertion.

## Required human decision

- \`approve exact worker runtime builder tool packet ${packet.packetId}\`
- \`reject worker runtime builder tool packet ${packet.packetId}\`

Approval authorizes only retrieval of this exact archive and use of the exact verified member for a new candidate build. It does not authorize \`models.lock.json\` mutation, installation, model execution, publishing, or deployment. Bundle/license approval, lock approval, installation approval, and real-model probe approval remain separate later gates.
`;
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length !== 4 || args[0] !== "--output-json" || args[2] !== "--output-doc") fail("builder-tool-cli-invalid");
  const localDiagnostic = {
    byteLength: 40_152_144,
    sha256: "2b4ccdac26598ca8c300e5c36d24297fa1471c350c46d2f34c835bf06be303ab",
    nlink: 1,
    versionOutput: "uv 0.12.3 (507230998 2026-08-07 aarch64-apple-darwin)",
  };
  const packet = await generateWorkerRuntimeBuilderToolProposal({ localDiagnostic });
  const outputJson = resolve(args[1]);
  const outputDoc = resolve(args[3]);
  await mkdir(dirname(outputJson), { recursive: true, mode: 0o700 });
  await mkdir(dirname(outputDoc), { recursive: true, mode: 0o700 });
  await writeFile(outputJson, `${JSON.stringify(packet, null, 2)}\n`, { mode: 0o600, flag: "wx" });
  await writeFile(outputDoc, renderWorkerRuntimeBuilderToolProposal(packet), { mode: 0o600, flag: "wx" });
  process.stdout.write(`packetId=${packet.packetId}\nmetadataOnly=true archiveBodyBytes=0\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
}
