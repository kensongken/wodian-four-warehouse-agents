import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { chmod, lstat, mkdir, readFile, rename, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

const testApi = process.env.VITEST ? await import("vitest") : await import("node:test");
const test = testApi.test ?? testApi.default;
const roots = [];
testApi.afterEach(async () => Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))));

import {
  computeBuilderToolPacketId,
  extractArchive,
  fetchApprovedBuilderTool,
  preflightTarGz,
  reuseApprovedBuilderTool,
  runVerifiedUvInvocation,
  spawnBoundedProcess,
} from "../build-worker-runtime-bundle.mjs";

const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const ASSET_URL = "https://api.github.com/repos/astral-sh/uv/releases/assets/505387707";
const ASSET_PATH = "/github-production-release-asset/699532645/3433a4a2-db55-4d29-8976-5438ac5ee0e0";
const MEMBER = "uv-aarch64-apple-darwin/uv";

async function ownedRoot(name) {
  const root = join(tmpdir(), `worker-runtime-tool-${process.pid}-${Date.now()}-${name}-${Math.random().toString(16).slice(2)}`);
  roots.push(root);
  return root;
}

function octal(value, width) {
  return `${value.toString(8).padStart(width - 1, "0")}\0`;
}

function tarGz(entries) {
  const blocks = [];
  for (const entry of entries) {
    const body = Buffer.from(entry.body ?? "");
    const header = Buffer.alloc(512);
    header.write(entry.name, 0, 100, "utf8");
    header.write(octal(entry.mode ?? 0o644, 8), 100, 8, "ascii");
    header.write(octal(0, 8), 108, 8, "ascii");
    header.write(octal(0, 8), 116, 8, "ascii");
    header.write(octal(entry.size ?? body.length, 12), 124, 12, "ascii");
    header.write(octal(0, 12), 136, 12, "ascii");
    header.fill(0x20, 148, 156);
    header.write(entry.type ?? "0", 156, 1, "ascii");
    if (entry.linkname) header.write(entry.linkname, 157, 100, "utf8");
    header.write("ustar\0", 257, 6, "ascii");
    header.write("00", 263, 2, "ascii");
    header.write(octal([...header].reduce((total, byte) => total + byte, 0), 8), 148, 8, "ascii");
    blocks.push(header, body, Buffer.alloc((512 - (body.length % 512)) % 512));
  }
  blocks.push(Buffer.alloc(1024));
  return gzipSync(Buffer.concat(blocks), { mtime: 0 });
}

function paxRecord(key, value) {
  let length = key.length + value.length + 4;
  while (`${length} ${key}=${value}\n`.length !== length) length = `${length} ${key}=${value}\n`.length;
  return `${length} ${key}=${value}\n`;
}

function packetFor(archiveBytes, uvBytes, executable = {}) {
  const core = {
    schemaVersion: 1,
    status: "awaiting-exact-builder-tool-approval",
    approved: false,
    parentMaterialPacketId: "worker-runtime-materials-v1-sha256-8147590c4832c5ad9ac04b2b9a370dc5b55b61832334a745ada538c2b8de4895",
    target: { platform: "darwin", architecture: "arm64" },
    tool: {
      name: "uv",
      version: "0.12.3",
      archive: { filename: "uv-aarch64-apple-darwin.tar.gz", url: ASSET_URL, byteLength: archiveBytes.length, sha256: sha256(archiveBytes) },
      executable: {
        memberPath: MEMBER,
        byteLength: uvBytes.length,
        sha256: sha256(uvBytes),
        expectedVersionOutput: "uv 0.12.3 (fixture aarch64-apple-darwin)",
        officialMatchState: "unverified-until-approved-archive-retrieval",
        ...executable,
      },
    },
    metadataEvidence: {
      release: { url: "https://api.github.com/repos/astral-sh/uv/releases/tags/0.12.3", responseByteLength: 1, responseSha256: "b".repeat(64) },
      distributionManifest: { url: "https://github.com/astral-sh/uv/releases/download/0.12.3/dist-manifest.json", responseByteLength: 1, responseSha256: "c".repeat(64) },
    },
    redirectPolicy: {
      originalUrl: ASSET_URL,
      statusCode: 302,
      maximumHops: 1,
      targetScheme: "https:",
      targetHost: "release-assets.githubusercontent.com",
      targetPath: ASSET_PATH,
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
  return { ...core, packetId: computeBuilderToolPacketId(core) };
}

function approval(packet, overrides = {}) {
  return {
    schemaVersion: 1,
    packetId: packet.packetId,
    decision: "approve-exact-worker-runtime-builder-tool-packet",
    approvedBy: "human-user",
    approvedAt: "2026-09-25T00:00:00Z",
    retrievalAuthorized: true,
    candidateBuildUseAuthorized: true,
    installAuthorized: false,
    modelsLockMutationAuthorized: false,
    modelExecutionAuthorized: false,
    publishAuthorized: false,
    deployAuthorized: false,
    ...overrides,
  };
}

function redirectUrl(query = "?signed=ephemeral") {
  return `https://release-assets.githubusercontent.com${ASSET_PATH}${query}`;
}

function twoHopFetch(archiveBytes, calls) {
  return async (url, init) => {
    calls.push({ url, init });
    if (calls.length === 1) return new Response(null, { status: 302, headers: { location: redirectUrl(), "set-cookie": "never=forward" } });
    return new Response(archiveBytes, { status: 200, headers: { "content-length": String(archiveBytes.length) } });
  };
}

test("builder-tool approval is exact before fetch or quarantine output", async () => {
  const uv = Buffer.from("#!/bin/sh\necho fixture\n");
  const archive = tarGz([{ name: MEMBER, body: uv, mode: 0o755 }]);
  const packet = packetFor(archive, uv);
  for (const [name, receipt] of [
    ["missing", undefined],
    ["forged", approval(packet, { approvedBy: "attacker" })],
    ["wrong-packet", approval(packet, { packetId: `worker-runtime-builder-tool-v1-sha256-${"f".repeat(64)}` })],
    ["over-authorized", approval(packet, { installAuthorized: true })],
    ["publish-authorized", approval(packet, { publishAuthorized: true })],
    ["deploy-authorized", approval(packet, { deployAuthorized: true })],
  ]) {
    const root = await ownedRoot(name);
    let fetchCalls = 0;
    await assert.rejects(fetchApprovedBuilderTool({
      parentPacket: { packetId: packet.parentMaterialPacketId },
      packet,
      approvalReceipt: receipt,
      toolRoot: root,
      fetchImpl: async () => { fetchCalls += 1; throw new Error("must-not-fetch"); },
    }), /studio-runtime-builder-tool-(?:packet|approval)-invalid/);
    assert.equal(fetchCalls, 0, name);
    await assert.rejects(lstat(root), { code: "ENOENT" });
  }
});

test("builder-tool retrieval enforces one credential-free exact redirect and emits a verified receipt", async () => {
  const uv = Buffer.from("#!/bin/sh\necho fixture\n");
  const archive = tarGz([{ name: MEMBER, body: uv, mode: 0o755 }]);
  const packet = packetFor(archive, uv);
  const root = await ownedRoot("success");
  const calls = [];
  const verified = await fetchApprovedBuilderTool({
    parentPacket: { packetId: packet.parentMaterialPacketId }, packet, approvalReceipt: approval(packet),
    toolRoot: root, fetchImpl: twoHopFetch(archive, calls),
  });
  assert.equal(calls.length, 2);
  assert.equal(new Headers(calls[0].init.headers).get("accept"), "application/octet-stream");
  for (const call of calls) {
    const headers = new Headers(call.init.headers);
    assert.equal(headers.has("authorization"), false);
    assert.equal(headers.has("cookie"), false);
    assert.equal(call.init.credentials, "omit");
    assert.equal(call.init.redirect, "manual");
  }
  assert.deepEqual(await readFile(verified.executablePath), uv);
  assert.equal(verified.receipt.packetId, packet.packetId);
  assert.equal(verified.receipt.archive.sha256, packet.tool.archive.sha256);
  assert.equal(verified.receipt.member.sha256, packet.tool.executable.sha256);
  assert.equal(verified.receipt.publishAuthorized, false);
  assert.equal(verified.receipt.deployAuthorized, false);
  assert.match(verified.receiptSha256, /^[a-f0-9]{64}$/u);
  const reused = await reuseApprovedBuilderTool({
    parentPacket: { packetId: packet.parentMaterialPacketId },
    packet,
    approvalReceipt: approval(packet),
    toolRoot: root,
  });
  assert.equal(reused.receiptSha256, verified.receiptSha256);
  assert.deepEqual(await readFile(reused.executablePath), uv);
});

test("verified uv invocation executes the opened inode when its pathname is replaced after verification", async () => {
  const root = await ownedRoot("opened-inode");
  await mkdir(root, { mode: 0o700 });
  const uvPath = join(root, "uv");
  const original = Buffer.from("#!/bin/sh\nprintf 'original-inode\\n'\n");
  const replacement = Buffer.from("#!/bin/sh\nprintf 'replacement-path\\n'\n");
  await writeFile(uvPath, original, { mode: 0o700 });
  await chmod(uvPath, 0o700);
  const archive = tarGz([{ name: MEMBER, body: original, mode: 0o755 }]);
  const packet = packetFor(archive, original);
  const policyPath = join(root, "deny-network.sb");
  await writeFile(policyPath, "(version 1)\n(allow default)\n(deny network*)\n", { mode: 0o600 });
  let replacements = 0;
  const result = await runVerifiedUvInvocation({
    toolPacket: packet,
    verifiedTool: {
      executablePath: uvPath,
      receipt: { member: { byteLength: original.length, sha256: sha256(original), memberPath: MEMBER } },
    },
    policyPath,
    args: [],
    processRunner: async (command, args, options) => {
      replacements += 1;
      await rename(uvPath, `${uvPath}.opened`);
      await writeFile(uvPath, replacement, { mode: 0o700 });
      await chmod(uvPath, 0o700);
      return spawnBoundedProcess(command, args, options);
    },
  });
  assert.equal(replacements, 1);
  assert.equal(result.stdout, "original-inode\n");
  assert.deepEqual(await readFile(uvPath), replacement);
});

test("verified uv invocation rejects output when its pinned alias is replaced during the run", async () => {
  const root = await ownedRoot("pinned-alias-tamper");
  await mkdir(root, { mode: 0o700 });
  const uvPath = join(root, "uv");
  const original = Buffer.from("#!/bin/sh\nprintf 'original-inode\\n'\n");
  const replacement = Buffer.from("#!/bin/sh\nprintf 'replacement-alias\\n'\n");
  await writeFile(uvPath, original, { mode: 0o700 });
  await chmod(uvPath, 0o700);
  const archive = tarGz([{ name: MEMBER, body: original, mode: 0o755 }]);
  const packet = packetFor(archive, original);
  const policyPath = join(root, "deny-network.sb");
  await writeFile(policyPath, "(version 1)\n(allow default)\n(deny network*)\n", { mode: 0o600 });
  await assert.rejects(runVerifiedUvInvocation({
    toolPacket: packet,
    verifiedTool: {
      executablePath: uvPath,
      receipt: { member: { byteLength: original.length, sha256: sha256(original), memberPath: MEMBER } },
    },
    policyPath,
    args: [],
    processRunner: async (command, args, options) => {
      const result = await spawnBoundedProcess(command, args, options);
      const aliasPath = args[2];
      await rename(aliasPath, `${aliasPath}.opened`);
      await writeFile(aliasPath, replacement, { mode: 0o700 });
      await chmod(aliasPath, 0o700);
      return result;
    },
  }), /studio-runtime-builder-tool-executable-changed-during-run/);
});

test("builder-tool retrieval rejects second hops and archive or member identity mismatch without promotion", async () => {
  const uv = Buffer.from("#!/bin/sh\necho fixture\n");
  const archive = tarGz([{ name: MEMBER, body: uv, mode: 0o755 }]);
  for (const [name, packet, fetchImpl, code] of [
    ["second-hop", packetFor(archive, uv), async () => new Response(null, { status: 302, headers: { location: redirectUrl() } }), /studio-runtime-builder-tool-redirect-rejected/],
    ["archive", packetFor(archive, uv), twoHopFetch(Buffer.concat([archive, Buffer.from("changed")]), []), /studio-runtime-builder-tool-archive-size-mismatch/],
    ["archive-digest", packetFor(archive, uv), twoHopFetch(Buffer.alloc(archive.length), []), /studio-runtime-builder-tool-archive-digest-mismatch/],
    ["member", packetFor(archive, uv, { sha256: "f".repeat(64) }), twoHopFetch(archive, []), /studio-runtime-builder-tool-member-digest-mismatch/],
  ]) {
    const root = await ownedRoot(name);
    await assert.rejects(fetchApprovedBuilderTool({
      parentPacket: { packetId: packet.parentMaterialPacketId }, packet, approvalReceipt: approval(packet), toolRoot: root, fetchImpl,
    }), code);
    await assert.rejects(lstat(root), { code: "ENOENT" });
  }
});

test("builder-tool retrieval rejects wrong redirect status, host, path, and scheme", async () => {
  const uv = Buffer.from("#!/bin/sh\necho fixture\n");
  const archive = tarGz([{ name: MEMBER, body: uv, mode: 0o755 }]);
  const packet = packetFor(archive, uv);
  for (const [name, status, location] of [
    ["status", 307, redirectUrl()],
    ["host", 302, `https://evil.example${ASSET_PATH}?signed=x`],
    ["path", 302, "https://release-assets.githubusercontent.com/wrong?signed=x"],
    ["scheme", 302, `http://release-assets.githubusercontent.com${ASSET_PATH}?signed=x`],
  ]) {
    const root = await ownedRoot(`redirect-${name}`);
    await assert.rejects(fetchApprovedBuilderTool({
      parentPacket: { packetId: packet.parentMaterialPacketId }, packet, approvalReceipt: approval(packet), toolRoot: root,
      fetchImpl: async () => new Response(null, { status, headers: { location } }),
    }), /studio-runtime-builder-tool-redirect-rejected/);
    await assert.rejects(lstat(root), { code: "ENOENT" });
  }
});

test("builder-tool retrieval rejects a non-default HTTPS redirect port before a second request", async () => {
  const uv = Buffer.from("#!/bin/sh\necho fixture\n");
  const archive = tarGz([{ name: MEMBER, body: uv, mode: 0o755 }]);
  const packet = packetFor(archive, uv);
  const root = await ownedRoot("redirect-port");
  let requests = 0;
  await assert.rejects(fetchApprovedBuilderTool({
    parentPacket: { packetId: packet.parentMaterialPacketId },
    packet,
    approvalReceipt: approval(packet),
    toolRoot: root,
    fetchImpl: async () => {
      requests += 1;
      return new Response(null, {
        status: 302,
        headers: { location: `https://release-assets.githubusercontent.com:8443${ASSET_PATH}?signed=x` },
      });
    },
  }), /studio-runtime-builder-tool-redirect-rejected/);
  assert.equal(requests, 1);
  await assert.rejects(lstat(root), { code: "ENOENT" });
});

test("builder-tool archive rejects traversal, duplicate member, links, special entries, and another executable", async () => {
  const uv = Buffer.from("#!/bin/sh\necho fixture\n");
  const cases = [
    ["traversal", [{ name: "../escape", body: "x" }, { name: MEMBER, body: uv, mode: 0o755 }]],
    ["duplicate", [{ name: MEMBER, body: uv, mode: 0o755 }, { name: MEMBER, body: uv, mode: 0o755 }]],
    ["symlink-member", [{ name: MEMBER, type: "2", linkname: "uv-real", mode: 0o755 }]],
    ["hardlink", [{ name: MEMBER, body: uv, mode: 0o755 }, { name: "other", type: "1", linkname: MEMBER }]],
    ["special", [{ name: MEMBER, body: uv, mode: 0o755 }, { name: "device", type: "3" }]],
    ["extra-executable", [{ name: MEMBER, body: uv, mode: 0o755 }, { name: "uv-aarch64-apple-darwin/other-tool", body: "x", mode: 0o755 }]],
  ];
  for (const [name, entries] of cases) {
    const archive = tarGz(entries);
    const packet = packetFor(archive, uv);
    const root = await ownedRoot(name);
    await assert.rejects(fetchApprovedBuilderTool({
      parentPacket: { packetId: packet.parentMaterialPacketId }, packet, approvalReceipt: approval(packet),
      toolRoot: root, fetchImpl: twoHopFetch(archive, []),
    }), /studio-runtime-builder-tool-archive-invalid/);
    await assert.rejects(lstat(root), { code: "ENOENT" });
  }
});

test("builder-tool archive accepts the official uvx sibling but extracts only the exact approved uv member", async () => {
  const uv = Buffer.from("#!/bin/sh\necho fixture\n");
  const archive = tarGz([
    { name: "uv-aarch64-apple-darwin/", type: "5", mode: 0o755 },
    { name: "uv-aarch64-apple-darwin/uvx", body: "unselected sibling", mode: 0o755 },
    { name: MEMBER, body: uv, mode: 0o755 },
  ]);
  const packet = packetFor(archive, uv);
  const root = await ownedRoot("official-uvx-sibling");
  const verified = await fetchApprovedBuilderTool({
    parentPacket: { packetId: packet.parentMaterialPacketId },
    packet,
    approvalReceipt: approval(packet),
    toolRoot: root,
    fetchImpl: twoHopFetch(archive, []),
  });
  assert.deepEqual(await readFile(verified.executablePath), uv);
  await assert.rejects(lstat(join(root, "extracted", "uv-aarch64-apple-darwin", "uvx")), { code: "ENOENT" });
});

test("tar preflight rejects declared expanded bytes before extraction", async () => {
  const archive = tarGz([{ name: "huge", body: "", size: 4096 }]);
  const root = await ownedRoot("oversize");
  const archivePath = `${root}.tar.gz`;
  roots.push(archivePath);
  await import("node:fs/promises").then(({ writeFile }) => writeFile(archivePath, archive));
  await assert.rejects(preflightTarGz(archivePath, { maxEntries: 8, maxExpandedBytes: 1024 }), /studio-runtime-source-archive-limits-exceeded/);
});

test("tar preflight accepts a bounded global PAX comment without changing entry identity or size", async () => {
  const archive = tarGz([
    { name: "GlobalHead", type: "g", body: paxRecord("comment", "exact-source-revision") },
    { name: "source/file.py", body: "print('ok')\n" },
  ]);
  const root = await ownedRoot("global-pax-comment");
  const archivePath = `${root}.tar.gz`;
  roots.push(archivePath);
  await import("node:fs/promises").then(({ writeFile }) => writeFile(archivePath, archive));
  const result = await preflightTarGz(archivePath, { maxEntries: 8, maxExpandedBytes: 1024 });
  assert.equal(result.entries, 2);
  assert.equal(result.expandedBytes, Buffer.byteLength("print('ok')\n"));
});

test("runtime archive preflight keeps PAX path overrides unsupported", async () => {
  const archive = tarGz([
    { name: "PaxHead", type: "x", body: paxRecord("path", "python/bin/python3.12") },
    { name: "python/bin/placeholder", body: "fixture" },
  ]);
  const root = await ownedRoot("runtime-pax-path");
  const archivePath = `${root}.tar.gz`;
  roots.push(archivePath);
  await writeFile(archivePath, archive);
  await assert.rejects(
    preflightTarGz(archivePath, { maxEntries: 8, maxExpandedBytes: 1024, portableRuntimePaths: true }),
    /studio-runtime-source-archive-invalid:unsupported-pax-key:path/,
  );
});

test("archive extraction never invokes tar when preflight exceeds expanded-byte limits", async () => {
  const archive = tarGz([{ name: "huge", body: "", size: 4096 }]);
  const root = await ownedRoot("preflight-before-extract");
  const archivePath = `${root}.tar.gz`;
  roots.push(archivePath);
  await import("node:fs/promises").then(({ writeFile }) => writeFile(archivePath, archive));
  let tarCalls = 0;
  await assert.rejects(extractArchive(archivePath, root, {
    maxEntries: 8,
    maxExpandedBytes: 1024,
    commandRunner: async () => { tarCalls += 1; throw new Error("tar-must-not-run"); },
  }), /studio-runtime-source-archive-limits-exceeded/);
  assert.equal(tarCalls, 0);
  await assert.rejects(lstat(root), { code: "ENOENT" });
});

test("archive preflight rejects a symlink-parent escape before invoking tar", async () => {
  const archive = tarGz([
    { name: "root/link", type: "2", linkname: "../../outside" },
    { name: "root/link/file", body: "escape" },
  ]);
  const root = await ownedRoot("symlink-parent-escape");
  const archivePath = `${root}.tar.gz`;
  roots.push(archivePath);
  await import("node:fs/promises").then(({ writeFile }) => writeFile(archivePath, archive));
  let tarCalls = 0;
  await assert.rejects(extractArchive(archivePath, root, {
    commandRunner: async () => { tarCalls += 1; throw new Error("tar-must-not-run"); },
  }), /studio-runtime-source-archive-invalid/);
  assert.equal(tarCalls, 0);
  await assert.rejects(lstat(root), { code: "ENOENT" });
});

test("archive preflight rejects a symlink escape at its post-strip output path before invoking tar", async () => {
  const archive = tarGz([
    { name: "top/a/link", type: "2", linkname: "../../top/escape" },
  ]);
  const root = await ownedRoot("post-strip-symlink-escape");
  const archivePath = `${root}.tar.gz`;
  roots.push(archivePath);
  await writeFile(archivePath, archive);
  let tarCalls = 0;
  await assert.rejects(extractArchive(archivePath, root, {
    commandRunner: async () => { tarCalls += 1; throw new Error("tar-must-not-run"); },
  }), /studio-runtime-source-archive-invalid/);
  assert.equal(tarCalls, 0);
  await assert.rejects(lstat(root), { code: "ENOENT" });
});

test("archive extraction rejects an unsafe symlink materialized by tar before returning the source", async () => {
  const archive = tarGz([{ name: "top/file", body: "safe" }]);
  const root = await ownedRoot("post-extract-symlink-check");
  const archivePath = `${root}.tar.gz`;
  roots.push(archivePath);
  await writeFile(archivePath, archive);
  let tarCalls = 0;
  await assert.rejects(extractArchive(archivePath, root, {
    commandRunner: async (_command, args) => {
      tarCalls += 1;
      if (args[0] === "-tzf") return { stdout: "top/file\n", stderr: "" };
      await mkdir(join(root, "a"), { recursive: true });
      await symlink("../../escape", join(root, "a", "link"));
      return { stdout: "", stderr: "" };
    },
  }), /studio-runtime-source-archive-invalid/);
  assert.equal(tarCalls, 2);
});
