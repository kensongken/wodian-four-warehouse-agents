import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFile as execFileCallback } from "node:child_process";
import {
  chmod,
  copyFile,
  lstat,
  mkdir,
  mkdtemp,
  readdir,
  readFile,
  realpath,
  rename,
  rm,
  stat,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import { gzipSync } from "node:zlib";

const testApi = process.env.VITEST ? await import("vitest") : await import("node:test");
const test = testApi.test ?? testApi.default;
const temporaryRoots = [];
testApi.afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => (
    rm(root, { recursive: true, force: true })
  )));
});

import {
  APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256,
  assertDeterministicRuntimeTar,
  buildWorkerRuntimeBundle,
  computeBuilderToolPacketId,
  computeMaterialPacketId,
  computeSupplementalMaterialPacketId,
  fetchApprovedBuilderTool,
  fetchApprovedMaterials,
  fetchApprovedSupplementalBuildMaterials,
  fetchVerifiedGithubReleaseRedirect,
  parseWorkerRuntimeBuildCli,
  reuseApprovedMaterials,
  runtimeDependencyLockBindings,
  runRuntimeSelfCheck,
  validateSceneMaterialExtension,
  validateSceneRuntimeMetadata,
  spawnBoundedProcess,
  validateCliOutputPaths,
  validateBuilderToolAuthorizedInputs,
  validateSupplementalAuthorizedInputs,
} from "../build-worker-runtime-bundle.mjs";
import * as runtimeBuilder from "../build-worker-runtime-bundle.mjs";
import { compareRuntimePathsByUtf8Bytes } from "../studio-runtime-canonical.mjs";
import {
  JS_UTF16_PATH_ORDER,
  UTF8_PATH_ORDER,
} from "./worker-runtime-path-order-fixture.mjs";

const execFile = promisify(execFileCallback);
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

const ordinaryPathOrder = (left, right) => left < right ? -1 : left > right ? 1 : 0;
const APPROVED_PACKET_ID =
  "worker-runtime-materials-v1-sha256-8147590c4832c5ad9ac04b2b9a370dc5b55b61832334a745ada538c2b8de4895";
const APPROVED_SCENE_PACKET_ID =
  "worker-runtime-materials-v1-sha256-505b5f9ad6b9d009bea1c5f4ab8d5b7b5fb6513277a11985d114fcbd2669730f";
const APPROVED_REDIRECT_POLICY_ID =
  "github-release-one-hop-v1-sha256-c1a15a5f32b42b20bf8d331b457c95f6c67e4527d051b4608497782bcd9cd920";
const APPROVED_PYTHON_MATERIAL = Object.freeze({
  kind: "python-runtime",
  name: "cpython",
  version: "3.12.14",
  build: "20260901",
  target: "aarch64-apple-darwin",
  filename: "cpython-3.12.14+20260901-aarch64-apple-darwin-install_only.tar.gz",
  url: "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-aarch64-apple-darwin-install_only.tar.gz",
  sha256: "3ee3ee547cedfeb7c2b16b2b7156039f7b470bb8f857e226fd3d2eb11db83c76",
  byteLength: 25_135_464,
});
const APPROVED_REDIRECT_PATH =
  "/github-production-release-asset/162334160/0748cc26-9857-48e2-b4f4-9a76653fb5a1";
const APPROVED_SUPPLEMENTAL_PACKET_ID =
  "worker-runtime-build-materials-v1-sha256-0805bab07fa760a6edb7d78f703a1930896faca3a76965f68666620a07ddd693";
const APPROVED_WORKER_LOCK_SHA256 =
  "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041";
const SCENE_MATERIALS = Object.freeze([{
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
}, {
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
}]);

function sceneExtensionFixture() {
  const baseMaterials = [{
    kind: "python-runtime",
    name: "cpython",
    version: "3.12.14",
    filename: "python.tar.gz",
    url: "https://fixtures.invalid/python.tar.gz",
    sha256: "1".repeat(64),
    byteLength: 100,
  }, {
    kind: "source-package",
    name: "mlx-vlm",
    version: "0.6.6",
    filename: "mlx-vlm.tar.gz",
    url: "https://fixtures.invalid/mlx-vlm.tar.gz",
    sha256: "2".repeat(64),
    byteLength: 200,
  }, ...[
    ["click", "8.3.1", "3"],
    ["numpy", "2.4.1", "4"],
    ["tqdm", "4.67.1", "5"],
    ["opencv-python", "4.13.0.90", "6"],
  ].map(([name, version, digest]) => ({
    kind: "python-distribution",
    name,
    version,
    filename: `${name}-${version}-py3-none-any.whl`,
    url: `https://fixtures.invalid/${name}-${version}-py3-none-any.whl`,
    sha256: digest.repeat(64),
    byteLength: 10,
  }))];
  const baseCore = {
    schemaVersion: 1,
    status: "awaiting-exact-retrieval-approval",
    approved: false,
    installAuthorized: false,
    target: {
      pythonVersionRange: ">=3.12.0 <3.13",
      platform: "darwin",
      architecture: "arm64",
    },
    dependencyLockSha256: APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256,
    metadataSources: ["https://fixtures.invalid/metadata"],
    expectedTotalDownloadBytes: baseMaterials.reduce((sum, item) => sum + item.byteLength, 0),
    materials: baseMaterials,
  };
  const parentMaterialPacketId = computeMaterialPacketId(baseCore);
  const materials = [...baseMaterials, ...SCENE_MATERIALS];
  const core = {
    ...baseCore,
    parentMaterialPacketId,
    baseMaterialCount: baseMaterials.length,
    workerDependencyLockSha256: APPROVED_WORKER_LOCK_SHA256,
    expectedTotalDownloadBytes: materials.reduce((sum, item) => sum + item.byteLength, 0),
    materials,
  };
  return { ...core, packetId: computeMaterialPacketId(core) };
}

function withPacketDigest(packet) {
  return { ...packet, packetId: computeMaterialPacketId(packet) };
}

const VALID_SCENE_METADATA = `Metadata-Version: 2.4
Name: scenedetect
Version: 0.7.1
Requires-Dist: click~=8.0,!=8.3.0
Requires-Dist: numpy
Requires-Dist: opencv-python
Requires-Dist: platformdirs
Requires-Dist: tqdm
Provides-Extra: pyav
Requires-Dist: av>=9.2; extra == "pyav"
Provides-Extra: moviepy
Requires-Dist: moviepy; extra == "moviepy"
`;

const VALID_SCENE_INVENTORY = Object.freeze([
  { name: "scenedetect", version: "0.7.1" },
  { name: "platformdirs", version: "4.11.8" },
  { name: "click", version: "8.3.1" },
  { name: "numpy", version: "2.4.1" },
  { name: "tqdm", version: "4.67.1" },
  { name: "opencv-python", version: "4.13.0.90" },
]);

function builderToolPacket(parentMaterialPacketId, archiveBytes, uvBytes, expectedVersionOutput = "uv 0.12.3") {
  const core = {
    schemaVersion: 1,
    status: "awaiting-exact-builder-tool-approval",
    approved: false,
    parentMaterialPacketId,
    target: { platform: "darwin", architecture: "arm64" },
    tool: {
      name: "uv",
      version: "0.12.3",
      archive: { filename: "uv-aarch64-apple-darwin.tar.gz", url: "https://api.github.com/repos/astral-sh/uv/releases/assets/505387707", byteLength: archiveBytes.length, sha256: sha256(archiveBytes) },
      executable: { memberPath: "uv-aarch64-apple-darwin/uv", byteLength: uvBytes.length, sha256: sha256(uvBytes), expectedVersionOutput, officialMatchState: "unverified-until-approved-archive-retrieval" },
    },
    metadataEvidence: {
      release: { url: "https://api.github.com/repos/astral-sh/uv/releases/tags/0.12.3", responseByteLength: 1, responseSha256: "b".repeat(64) },
      distributionManifest: { url: "https://github.com/astral-sh/uv/releases/download/0.12.3/dist-manifest.json", responseByteLength: 1, responseSha256: "c".repeat(64) },
    },
    redirectPolicy: {
      originalUrl: "https://api.github.com/repos/astral-sh/uv/releases/assets/505387707",
      statusCode: 302,
      maximumHops: 1,
      targetScheme: "https:",
      targetHost: "release-assets.githubusercontent.com",
      targetPath: "/github-production-release-asset/699532645/3433a4a2-db55-4d29-8976-5438ac5ee0e0",
      allowEphemeralSignedQuery: true,
      forwardAuthorization: false,
      forwardCookies: false,
      allowSecondRedirect: false,
    },
    authorization: { retrievalAuthorized: false, candidateBuildUseAuthorized: false, installAuthorized: false, modelsLockMutationAuthorized: false, modelExecutionAuthorized: false },
  };
  return { ...core, packetId: computeBuilderToolPacketId(core) };
}

function builderToolReceipt(packet, overrides = {}) {
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

function approvedSupplementalPacket() {
  return {
    schemaVersion: 1,
    status: "awaiting-exact-supplemental-retrieval-approval",
    approved: false,
    retrievalAuthorized: false,
    installAuthorized: false,
    modelExecutionAuthorized: false,
    parentMaterialPacketId: APPROVED_PACKET_ID,
    sourceArtifact: {
      name: "mlx-vlm",
      version: "0.6.6",
      sourceRevision: "c9e27b08311f9c1f38690c46034b04c7598d73ee",
      sha256: "d2f99a0258fa33ef0d15ea731a6893573e5664b84488aa2dc965865b577c8ddb",
      byteLength: 46_408_783,
      filename: "mlx-vlm-0.6.6.tar.gz",
    },
    buildSystem: {
      backend: "setuptools.build_meta",
      requirements: ["setuptools>=61.0", "wheel"],
    },
    approvedDependencyLockSha256: APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256,
    target: { pythonVersion: "3.12.14", platform: "darwin", architecture: "arm64" },
    diagnosis: "offline-build-missing-build-system-requirements",
    discoveryMetadataSources: [
      "https://pypi.org/pypi/setuptools/json",
      "https://pypi.org/pypi/wheel/json",
    ],
    approvalMetadataSources: [
      "https://pypi.org/pypi/setuptools/84.0.0/json",
      "https://pypi.org/pypi/wheel/0.48.0/json",
    ],
    expectedTotalDownloadBytes: 851_536,
    materials: [{
      kind: "build-dependency-wheel",
      name: "setuptools",
      requirement: "setuptools>=61.0",
      version: "84.0.0",
      filename: "setuptools-84.0.0-py3-none-any.whl",
      url: "https://files.pythonhosted.org/packages/95/9c/c510029fc6ef33a6275cd2c5d3cecd6613dfd6aa401d57c54f1c18852ccf/setuptools-84.0.0-py3-none-any.whl",
      sha256: "51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670",
      byteLength: 818_216,
      pythonTag: "py3",
      abiTag: "none",
      platformTag: "any",
      yanked: false,
      pythonCompatibility: {
        runtimeVersion: "3.12.14",
        releaseRequiresPython: ">=3.10",
        fileRequiresPython: ">=3.10",
        policy: "single-greater-than-or-equal-release-clause-v1",
      },
      declaredLicenseExpression: "MIT",
      licenseEvidence: {
        metadataUrl: "https://pypi.org/pypi/setuptools/84.0.0/json",
        field: "info.license_expression",
        value: "MIT",
        validationPolicy: "single-spdx-license-identifier-v1",
      },
      licenseReviewState: "declared-expression-recorded-pending-human-review",
      metadataEvidence: {
        metadataUrl: "https://pypi.org/pypi/setuptools/84.0.0/json",
        responseSha256: "24d07932459261bbf9e66e5cfdebfeba23a977234ab24bf9c3a1df053c53ba21",
        responseByteLength: 8_162,
      },
    }, {
      kind: "build-dependency-wheel",
      name: "wheel",
      requirement: "wheel",
      version: "0.48.0",
      filename: "wheel-0.48.0-py3-none-any.whl",
      url: "https://files.pythonhosted.org/packages/2e/29/69cfbb602cd91690c55d38ba9fe53e6a7e76a6fa647bf38f19c138d25449/wheel-0.48.0-py3-none-any.whl",
      sha256: "3217dcc807155e45db462d7ef2431f5ddda0d7273b700d05a67b271ceb1287ab",
      byteLength: 33_320,
      pythonTag: "py3",
      abiTag: "none",
      platformTag: "any",
      yanked: false,
      pythonCompatibility: {
        runtimeVersion: "3.12.14",
        releaseRequiresPython: ">=3.9",
        fileRequiresPython: ">=3.9",
        policy: "single-greater-than-or-equal-release-clause-v1",
      },
      declaredLicenseExpression: "MIT",
      licenseEvidence: {
        metadataUrl: "https://pypi.org/pypi/wheel/0.48.0/json",
        field: "info.license_expression",
        value: "MIT",
        validationPolicy: "single-spdx-license-identifier-v1",
      },
      licenseReviewState: "declared-expression-recorded-pending-human-review",
      metadataEvidence: {
        metadataUrl: "https://pypi.org/pypi/wheel/0.48.0/json",
        responseSha256: "03f655da3a6dd010d9ac41d4771d5d973109cb662a1e10eb9d967535ffd145f8",
        responseByteLength: 4_390,
      },
    }],
    packetId: APPROVED_SUPPLEMENTAL_PACKET_ID,
  };
}

function approvedSupplementalReceipt(overrides = {}) {
  return {
    schemaVersion: 1,
    packetId: APPROVED_SUPPLEMENTAL_PACKET_ID,
    decision: "approve-exact-supplemental-build-material-packet",
    approvedBy: "human-user",
    approvedAt: "2026-09-24",
    retrievalAuthorized: true,
    buildCandidateAuthorized: true,
    installAuthorized: false,
    modelExecutionAuthorized: false,
    ...overrides,
  };
}

function approvedRedirectPolicy(overrides = {}) {
  return {
    schemaVersion: 1,
    packetId: APPROVED_PACKET_ID,
    material: {
      name: "cpython",
      version: "3.12.14+20260901",
      originalUrl: APPROVED_PYTHON_MATERIAL.url,
      byteLength: APPROVED_PYTHON_MATERIAL.byteLength,
      sha256: APPROVED_PYTHON_MATERIAL.sha256,
    },
    redirect: {
      statusCode: 302,
      maximumHops: 1,
      targetScheme: "https:",
      targetHost: "release-assets.githubusercontent.com",
      targetPath: APPROVED_REDIRECT_PATH,
      allowEphemeralSignedQuery: true,
      forwardAuthorization: false,
      forwardCookies: false,
      allowSecondRedirect: false,
    },
    authorization: {
      retrievalOnly: true,
      buildCandidateAuthorized: true,
      installAuthorized: false,
      modelExecutionAuthorized: false,
    },
    policyId: APPROVED_REDIRECT_POLICY_ID,
    status: "awaiting-exact-redirect-approval",
    ...overrides,
  };
}

function approvedRedirectReceipt(overrides = {}) {
  return {
    schemaVersion: 1,
    policyId: APPROVED_REDIRECT_POLICY_ID,
    packetId: APPROVED_PACKET_ID,
    decision: "approve-verified-github-release-one-hop-redirect",
    approvedBy: "human-user",
    approvedAt: "2026-09-23T16:49:58Z",
    retrievalAuthorized: true,
    buildCandidateAuthorized: true,
    installAuthorized: false,
    modelExecutionAuthorized: false,
    ...overrides,
  };
}

const approvedRedirectUrl = (query = "?signed=ephemeral") =>
  `https://release-assets.githubusercontent.com${APPROVED_REDIRECT_PATH}${query}`;

async function withRoot() {
  const root = await mkdtemp(join(tmpdir(), "worker-runtime-build-test-"));
  temporaryRoots.push(root);
  return root;
}

async function createTarGz(root, archiveName, treeName, files) {
  const source = join(root, `${archiveName}-source`, treeName);
  for (const file of files) {
    const path = join(source, file.path);
    await mkdir(dirname(path), { recursive: true });
    if (file.symlinkTarget) {
      await symlink(file.symlinkTarget, path);
    } else {
      await writeFile(path, file.contents, { mode: file.executable ? 0o755 : 0o644 });
      if (file.executable) await chmod(path, 0o755);
    }
  }
  const archive = join(root, archiveName);
  await execFile("/usr/bin/tar", ["--no-xattrs", "-czf", archive, "-C", dirname(source), treeName], {
    env: { ...process.env, COPYFILE_DISABLE: "1" },
  });
  return archive;
}

function material(kind, name, version, filename, bytes, extra = {}) {
  return {
    kind,
    name,
    version,
    filename,
    url: `https://fixtures.invalid/${filename}`,
    sha256: sha256(bytes),
    byteLength: bytes.byteLength,
    ...extra,
  };
}

function executableToolArchive(bytes) {
  const header = Buffer.alloc(512);
  header.write("uv-aarch64-apple-darwin/uv", 0, 100, "utf8");
  header.write("0000755\0", 100, 8, "ascii");
  header.write("0000000\0", 108, 8, "ascii");
  header.write("0000000\0", 116, 8, "ascii");
  header.write(`${bytes.length.toString(8).padStart(11, "0")}\0`, 124, 12, "ascii");
  header.write("00000000000\0", 136, 12, "ascii");
  header.fill(0x20, 148, 156);
  header.write("0", 156, 1, "ascii");
  header.write("ustar\0", 257, 6, "ascii");
  header.write("00", 263, 2, "ascii");
  const checksum = [...header].reduce((total, byte) => total + byte, 0);
  header.write(`${checksum.toString(8).padStart(6, "0")}\0 `, 148, 8, "ascii");
  return gzipSync(Buffer.concat([
    header,
    bytes,
    Buffer.alloc((512 - (bytes.length % 512)) % 512),
    Buffer.alloc(1024),
  ]), { mtime: 0 });
}

async function fixture(t, name, {
  uvMode = "normal",
  pythonLinkMode = null,
  pythonExecutableLibrary = false,
  pythonBytecode = false,
} = {}) {
  const root = await withRoot(t);
  const inputs = join(root, "inputs");
  await mkdir(inputs, { recursive: true });
  const pythonFiles = [{
    path: "bin/python3.12",
    executable: true,
    contents: `#!/bin/sh\nprintf '%s\\n' '{"status":"ready","pythonVersion":"3.12.14"}'\n`,
  }, {
    path: "LICENSE",
    contents: "Python Software Foundation License\n",
  }, {
    path: "include/python3.12/Python.h",
    contents: "/* uppercase path fixture */\n",
  }, {
    path: "include/python3.12/abstract.h",
    contents: "/* lowercase path fixture */\n",
  }];
  if (pythonLinkMode) {
    pythonFiles.push({
      path: "bin/python3",
      symlinkTarget: pythonLinkMode === "internal" ? "python3.12" : "/etc/passwd",
    });
  }
  if (pythonExecutableLibrary) {
    pythonFiles.push({
      path: "lib/python3.12/base64.py",
      executable: true,
      contents: "VALUE = 'stdlib'\n",
    });
  }
  if (pythonBytecode) {
    pythonFiles.push({
      path: "lib/python3.12/encodings/__pycache__/__init__.cpython-312.pyc",
      contents: "synthetic bytecode",
    });
  }
  const pythonArchive = await createTarGz(inputs, "python.tar.gz", "python", pythonFiles);
  const sourceArchive = await createTarGz(inputs, "mlx-vlm.tar.gz", "mlx-vlm-source", [{
    path: "pyproject.toml",
    contents: "[build-system]\nrequires = []\nbuild-backend = 'fixture'\n",
  }, {
    path: "mlx_vlm/__init__.py",
    contents: "__version__ = '0.6.6'\n",
  }]);
  const wheelPath = join(inputs, "fixture-1.0.0-py3-none-any.whl");
  await writeFile(wheelPath, "synthetic wheel bytes\n");
  const [pythonBytes, sourceBytes, wheelBytes] = await Promise.all([
    readFile(pythonArchive),
    readFile(sourceArchive),
    readFile(wheelPath),
  ]);
  const core = {
    schemaVersion: 1,
    status: "awaiting-exact-retrieval-approval",
    approved: false,
    installAuthorized: false,
    target: {
      pythonVersionRange: ">=3.12.0 <3.13",
      platform: "darwin",
      architecture: "arm64",
    },
    dependencyLockSha256: APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256,
    metadataSources: ["https://fixtures.invalid/metadata"],
    expectedTotalDownloadBytes: pythonBytes.length + sourceBytes.length + wheelBytes.length,
    materials: [
      material("python-runtime", "cpython", "3.12.14", "python.tar.gz", pythonBytes, {
        build: "fixture",
        target: "aarch64-apple-darwin",
      }),
      material("source-package", "mlx-vlm", "0.6.6", "mlx-vlm.tar.gz", sourceBytes, {
        sourceRevision: "c9e27b08311f9c1f38690c46034b04c7598d73ee",
      }),
      material(
        "python-distribution",
        "fixture",
        "1.0.0",
        "fixture-1.0.0-py3-none-any.whl",
        wheelBytes,
        { pythonTag: "py3", abiTag: "none", platformTag: "any", minimumOsVersion: null },
      ),
    ],
  };
  const packet = { ...core, packetId: computeMaterialPacketId(core) };
  const approvalReceipt = {
    schemaVersion: 1,
    packetId: packet.packetId,
    decision: "approve-exact-retrieval",
    approvedBy: "human-user",
    approvedAt: "2026-09-23T13:44:04Z",
    retrievalAuthorized: true,
    buildCandidateAuthorized: true,
    installAuthorized: false,
    modelExecutionAuthorized: false,
  };
  const quarantineRoot = join(root, "quarantine");
  await mkdir(quarantineRoot, { mode: 0o700 });
  await Promise.all([
    copyFile(pythonArchive, join(quarantineRoot, "python.tar.gz")),
    copyFile(sourceArchive, join(quarantineRoot, "mlx-vlm.tar.gz")),
    copyFile(wheelPath, join(quarantineRoot, "fixture-1.0.0-py3-none-any.whl")),
  ]);
  const uvPath = join(root, "fake-uv");
  const poison = uvMode === "absolute-path"
    ? "printf '%s\\n' \"{\\\"url\\\":\\\"file://$target/private-build\\\"}\" > \"$target/fixture-1.0.0.dist-info/direct_url.json\""
    : uvMode === "symlink"
      ? "[ -e \"$target/fixture_pkg/alias.py\" ] || ln -s __init__.py \"$target/fixture_pkg/alias.py\""
      : uvMode === "hardlink"
        ? "[ -e \"$target/fixture_pkg/hardlink.py\" ] || ln \"$target/fixture_pkg/__init__.py\" \"$target/fixture_pkg/hardlink.py\""
      : uvMode === "bytecode"
          ? "mkdir -p \"$target/fixture_pkg/__pycache__\"; printf pyc > \"$target/fixture_pkg/__pycache__/installed.pyc\""
        : uvMode === "upstream-build-artifacts"
          ? "mkdir -p \"$target/aiohttp/.hash\" \"$target/bin\"; printf '%s\\n' '/Users/test-user/work/project/source.py' > \"$target/aiohttp/.hash/source.hash\"; printf '%s\\n' '/Users/test-user/Desktop/project/example.py' > \"$target/fixture_pkg/upstream.txt\"; printf '%s\\n' 'fixture_pkg/upstream.txt,,' >> \"$target/fixture-1.0.0.dist-info/RECORD\"; printf '%s\\n' '#!/private/generated/python' > \"$target/bin/generated-tool\"; chmod 755 \"$target/bin/generated-tool\"; printf '%s\\n' '{\"url\":\"file:///private/generated/mlx_vlm.whl\"}' > \"$target/mlx_vlm-0.6.6.dist-info/direct_url.json\""
        : uvMode === "recorded-executable"
          ? "chmod 755 \"$target/fixture_pkg/__init__.py\""
        : uvMode === "unowned"
          ? "printf unknown > \"$target/fixture_pkg/unowned.dat\""
      : uvMode === "target-lock"
          ? ": > \"$target/.lock\""
        : uvMode === "non-ascii-record"
          ? "printf '%s\\n' 'é_ghost.py,,' >> \"$target/fixture-1.0.0.dist-info/RECORD\""
        : uvMode === "non-ascii-dist-info"
          ? "mkdir -p \"$target/é-1.0.0.dist-info\"; printf metadata > \"$target/é-1.0.0.dist-info/METADATA\""
        : uvMode === "non-ascii-license"
          ? "mkdir -p \"$target/fixture-1.0.0.dist-info/licenses\"; printf license > \"$target/fixture-1.0.0.dist-info/licenses/LICENSE-é\""
        : uvMode === "path-order"
          ? `mkdir -p \"$target/runtime\"; ${UTF8_PATH_ORDER.map((path) => (
            `: > \"$target/${path}\"; printf '%s,,\\n' '${path}' >> \"$target/fixture-1.0.0.dist-info/RECORD\"`
          )).join("; ")}`
        : uvMode === "executable"
          ? "printf '%s\\n' '#!/bin/sh' > \"$target/fixture_pkg/unrecorded-tool\"; chmod 755 \"$target/fixture_pkg/unrecorded-tool\""
          : ":";
  const buildFailure = uvMode === "network"
    ? "echo 'network access required for build dependency' >&2; exit 42"
    : uvMode === "generic-failure"
      ? "echo 'synthetic compiler failure' >&2; exit 43"
      : uvMode === "non-ascii-wheel"
        ? "mkdir -p \"$out\"; printf wheel > \"$out/é-0.6.6-py3-none-any.whl\"; exit 0"
    : "mkdir -p \"$out\"; printf wheel > \"$out/mlx_vlm-0.6.6-py3-none-any.whl\"; exit 0";
  await writeFile(uvPath, `#!/bin/sh
if [ "$1" = "--version" ]; then
  ${uvMode === "version-metadata"
    ? "echo 'uv 0.12.3 (fixture-build aarch64-apple-darwin)'"
    : "echo 'uv 0.12.3'"}
  exit 0
fi
command=""
for arg in "$@"; do
  [ "$arg" = "build" ] && command=build
  [ "$arg" = "install" ] && command=install
done
if [ "$command" = build ]; then
  out=""
  previous=""
  for arg in "$@"; do
    [ "$previous" = "--out-dir" ] && out="$arg"
    previous="$arg"
  done
  ${buildFailure}
fi
target=""
previous=""
for arg in "$@"; do
  [ "$previous" = "--target" ] && target="$arg"
  previous="$arg"
done
mkdir -p "$target/fixture_pkg" "$target/fixture-1.0.0.dist-info" "$target/mlx_vlm" "$target/mlx_vlm-0.6.6.dist-info"
printf "VALUE = 'fixture'\\n" > "$target/fixture_pkg/__init__.py"
printf "Metadata-Version: 2.4\\nName: fixture\\nVersion: 1.0.0\\nLicense-Expression: MIT\\n" > "$target/fixture-1.0.0.dist-info/METADATA"
printf "fixture_pkg/__init__.py,,\\nfixture-1.0.0.dist-info/METADATA,,\\n" > "$target/fixture-1.0.0.dist-info/RECORD"
printf "__version__ = '0.6.6'\\n" > "$target/mlx_vlm/__init__.py"
printf "Metadata-Version: 2.4\\nName: mlx-vlm\\nVersion: 0.6.6\\nLicense-Expression: MIT\\n" > "$target/mlx_vlm-0.6.6.dist-info/METADATA"
printf "bin/generated-tool,sha256=%s,1\\nmlx_vlm/__init__.py,,\\nmlx_vlm-0.6.6.dist-info/METADATA,,\\nmlx_vlm-0.6.6.dist-info/RECORD,,\\nmlx_vlm-0.6.6.dist-info/uv_cache.json,,\\n" "$target" > "$target/mlx_vlm-0.6.6.dist-info/RECORD"
printf '{"target":"%s"}\\n' "$target" > "$target/mlx_vlm-0.6.6.dist-info/uv_cache.json"
${poison}
`, { mode: 0o700 });
  await chmod(uvPath, 0o700);
  const uvBytes = await readFile(uvPath);
  const toolArchiveBytes = executableToolArchive(uvBytes);
  const toolPacket = builderToolPacket(
    packet.packetId,
    toolArchiveBytes,
    uvBytes,
    uvMode === "version-metadata" ? "uv 0.12.3 (fixture-build aarch64-apple-darwin)" : "uv 0.12.3",
  );
  const toolApprovalReceipt = builderToolReceipt(toolPacket);
  let toolFetchCalls = 0;
  const verifiedBuilderTool = await fetchApprovedBuilderTool({
    parentPacket: packet,
    packet: toolPacket,
    approvalReceipt: toolApprovalReceipt,
    toolRoot: join(root, "verified-builder-tool"),
    fetchImpl: async () => {
      toolFetchCalls += 1;
      return toolFetchCalls === 1
        ? new Response(null, { status: 302, headers: { location: "https://release-assets.githubusercontent.com/github-production-release-asset/699532645/3433a4a2-db55-4d29-8976-5438ac5ee0e0?signed=fixture" } })
        : new Response(toolArchiveBytes, { status: 200, headers: { "content-length": String(toolArchiveBytes.length) } });
    },
  });
  return {
    packet,
    approvalReceipt,
    quarantineRoot,
    outputRoot: join(root, name),
    builderToolPacket: toolPacket,
    builderToolApprovalReceipt: toolApprovalReceipt,
    verifiedBuilderTool,
    root,
  };
}

test("same approved inputs produce byte-identical archive, tree, and sidecars", async (t) => {
  const firstInput = await fixture(t, "first");
  const secondInput = { ...firstInput, outputRoot: join(firstInput.root, "second") };
  const first = await buildWorkerRuntimeBundle(firstInput);
  const second = await buildWorkerRuntimeBundle(secondInput);

  assert.equal(first.archiveSha256, second.archiveSha256);
  assert.equal(first.treeSha256, second.treeSha256);
  const receipt = JSON.parse(await readFile(first.receiptPath, "utf8"));
  assert.equal(receipt.builderToolVerifiedReceiptSha256, firstInput.verifiedBuilderTool.receiptSha256);
  assert.deepEqual(receipt.promotionVerification, {
    relocationImportSelfCheck: "passed-before-promotion",
    selfCheckScratchBoundary: "owned-private-outside-runtime-and-candidate-tree",
    finalTreeVerification: "descriptor-safe-manifest-match-after-self-check-and-document-staging",
    finalFileVerification: "runtime-tar-and-all-sidecars-rehashed-after-document-staging-immediately-before-promotion",
    promotionFailureRecovery: "candidate-directory-dev-ino-plus-bundle-four-files-tree-exact-or-recovery-required",
  });
  assert.deepEqual(receipt.builderToolExecutionSecurityBoundary, {
    inodePin: "opened-o_nofollow-handle-private-hardlink-dev-ino-sha256-pre-post",
    sameLoginAccountCompromise: "out-of-scope-local-mvp",
  });
  assert.equal(first.builderToolVerifiedReceiptSha256, firstInput.verifiedBuilderTool.receiptSha256);
  for (const key of ["manifestPath", "packagesPath", "receiptPath"]) {
    assert.deepEqual(await readFile(first[key]), await readFile(second[key]), key);
  }
});

test("builder rejects non-ASCII runtime paths while UTF-8 comparison remains deterministic", async (t) => {
  const input = await fixture(t, "tree-order-contract", { uvMode: "path-order" });
  assert.deepEqual([...UTF8_PATH_ORDER].sort(ordinaryPathOrder), JS_UTF16_PATH_ORDER);
  assert.deepEqual([...UTF8_PATH_ORDER].sort(compareRuntimePathsByUtf8Bytes), UTF8_PATH_ORDER);
  await assert.rejects(
    buildWorkerRuntimeBundle(input),
    /studio-runtime-path-invalid/,
  );
});

test("finalized candidate publishes exactly the four approved files and cleans its owned build tree", async (t) => {
  const input = await fixture(t, "exact-four-candidate");
  const candidateRoot = join(input.root, "published-candidate");
  const outputDoc = join(input.root, "bundle.md");

  const finalized = await runtimeBuilder.buildAndFinalizeWorkerRuntimeCandidate({
    buildInput: input,
    candidateRoot,
    outputDoc,
    packet: input.packet,
    supplementalPacket: approvedSupplementalPacket(),
    builderToolPacket: input.builderToolPacket,
  });

  assert.deepEqual((await readdir(candidateRoot)).sort(), [
    "derivation-receipt.json",
    "runtime-manifest.json",
    "runtime-packages.json",
    "runtime.tar",
  ]);
  const approvalDocument = await readFile(outputDoc, "utf8");
  assert.equal(
    approvalDocument.includes(`License inventory SHA-256: \`${finalized.final.packagesSha256}\``),
    true,
  );
  assert.match(approvalDocument, /Installed distributions: `2`/);
  assert.match(approvalDocument, /Unknown license expressions: `0`/);
  assert.match(approvalDocument, /`retrieved=true`/);
  assert.match(approvalDocument, /`built=true`/);
  assert.match(approvalDocument, /`deterministic=true`/);
  assert.match(approvalDocument, /`installed=false`/);
  assert.match(approvalDocument, /`qwenProbeRun=false`/);
  assert.match(approvalDocument, /`customerContentUsed=false`/);
  assert.match(approvalDocument, /`productionReady=false`/);
  assert.deepEqual((await readdir(input.root)).filter((name) => name.startsWith(".worker-runtime-stage-")), []);
});

test("candidate inventory rejects non-ASCII names before canonical sorting", async (t) => {
  const input = await fixture(t, "candidate-path-domain");
  const built = await buildWorkerRuntimeBundle(input);
  const candidateRoot = join(input.root, "candidate-path-domain-copy");
  await mkdir(candidateRoot, { mode: 0o700 });
  for (const [source, name] of [
    [built.archivePath, "runtime.tar"],
    [built.manifestPath, "runtime-manifest.json"],
    [built.packagesPath, "runtime-packages.json"],
    [built.receiptPath, "derivation-receipt.json"],
  ]) {
    await copyFile(source, join(candidateRoot, name));
    await chmod(join(candidateRoot, name), 0o600);
  }
  await writeFile(join(candidateRoot, "é-extra"), "fixture\n", { mode: 0o600 });

  assert.equal(typeof runtimeBuilder.verifyPublishedCandidate, "function");
  await assert.rejects(
    runtimeBuilder.verifyPublishedCandidate(candidateRoot, built),
    /studio-runtime-path-invalid/,
  );
});

test("candidate remains importable after moving to a different absolute path", async (t) => {
  const input = await fixture(t, "origin");
  const built = await buildWorkerRuntimeBundle(input);
  const moved = join(input.root, "relocated", "runtime");
  await mkdir(dirname(moved), { recursive: true });
  await rename(built.runtimeRoot, moved);
  const result = await runRuntimeSelfCheck(moved);
  assert.equal(result.status, "ready");
});

test("runtime self-check keeps HOME, temp, and caches outside the runtime tree", async (t) => {
  const root = await withRoot(t);
  const runtimeRoot = join(root, "candidate", "runtime-root");
  const scratchParent = join(root, "self-check-scratch");
  await mkdir(join(runtimeRoot, "python", "bin"), { recursive: true });
  await mkdir(join(runtimeRoot, "policy"), { recursive: true });
  await writeFile(join(runtimeRoot, "python", "bin", "python3.12"), "fixture");
  await writeFile(join(runtimeRoot, "policy", "deny-network.sb"), "fixture");
  await mkdir(scratchParent);
  const realScratchParent = await realpath(scratchParent);
  let observed;
  const result = await runRuntimeSelfCheck(runtimeRoot, async (_command, _args, options) => {
    observed = options.env;
    for (const path of [observed.HOME, observed.TMPDIR, observed.XDG_CACHE_HOME]) {
      assert.equal(path.startsWith(`${runtimeRoot}/`), false);
      assert.equal(path.startsWith(`${realScratchParent}/`), true);
      assert.equal((await stat(path)).isDirectory(), true);
    }
    return { stdout: '{"status":"ready","pythonVersion":"3.12.14"}\n', stderr: "" };
  }, scratchParent);
  assert.equal(result.status, "ready");
  for (const path of [observed.HOME, observed.TMPDIR, observed.XDG_CACHE_HOME]) {
    await assert.rejects(lstat(path), { code: "ENOENT" });
  }
  await assert.rejects(
    runRuntimeSelfCheck(runtimeRoot, async () => { throw new Error("injected-self-check-failure"); }, scratchParent),
    /studio-runtime-offline-build-failed:injected-self-check-failure/,
  );
  assert.deepEqual((await readdir(realScratchParent)).filter((name) => name.startsWith(".runtime-self-check-")), []);
});

test("post-relocation tree mutation fails before candidate or document promotion", async (t) => {
  const input = await fixture(t, "post-self-check-mutation");
  const candidateRoot = join(input.root, "published-candidate");
  const outputDoc = join(input.root, "bundle.md");
  await assert.rejects(
    runtimeBuilder.buildAndFinalizeWorkerRuntimeCandidate({
      buildInput: input,
      candidateRoot,
      outputDoc,
      packet: input.packet,
      supplementalPacket: approvedSupplementalPacket(),
      builderToolPacket: input.builderToolPacket,
      selfCheckRunner: async (runtimeRoot) => {
        await writeFile(join(runtimeRoot, "site-packages", "fixture_pkg", "__init__.py"), "mutated\n");
        return { status: "ready", pythonVersion: "3.12.14" };
      },
    }),
    /studio-runtime-final-tree-mismatch/,
  );
  await assert.rejects(lstat(candidateRoot), { code: "ENOENT" });
  await assert.rejects(lstat(outputDoc), { code: "ENOENT" });
  assert.deepEqual((await readdir(input.root)).filter((name) => name.startsWith(".worker-runtime-stage-")), []);
});

test("post-relocation sidecar mutation fails before promotion", async (t) => {
  const input = await fixture(t, "post-self-check-sidecar-mutation");
  const candidateRoot = join(input.root, "published-candidate");
  const outputDoc = join(input.root, "bundle.md");
  await assert.rejects(
    runtimeBuilder.buildAndFinalizeWorkerRuntimeCandidate({
      buildInput: input,
      candidateRoot,
      outputDoc,
      packet: input.packet,
      supplementalPacket: approvedSupplementalPacket(),
      builderToolPacket: input.builderToolPacket,
      selfCheckRunner: async (runtimeRoot) => {
        await writeFile(join(dirname(runtimeRoot), "first", "derivation-receipt.json"), "mutated\n");
        return { status: "ready", pythonVersion: "3.12.14" };
      },
    }),
    /studio-runtime-final-file-mismatch/,
  );
  await assert.rejects(lstat(candidateRoot), { code: "ENOENT" });
  await assert.rejects(lstat(outputDoc), { code: "ENOENT" });
  assert.deepEqual((await readdir(input.root)).filter((name) => name.startsWith(".worker-runtime-stage-")), []);
});

test("owned build stages clean on build and document failpoints without touching stale legacy paths", async (t) => {
  const input = await fixture(t, "stage-cleanup");
  const candidateRoot = join(input.root, "published-candidate");
  const outputDoc = join(input.root, "bundle.md");
  const staleFirst = `${candidateRoot}.first`;
  const staleSecond = `${candidateRoot}.second`;
  const staleRelocated = `${candidateRoot}.first/runtime-root.relocated`;
  await mkdir(staleRelocated, { recursive: true });
  await mkdir(staleSecond, { recursive: true });
  await writeFile(join(staleFirst, "user-marker"), "preserve");
  await writeFile(join(staleSecond, "user-marker"), "preserve");

  await assert.rejects(runtimeBuilder.buildAndFinalizeWorkerRuntimeCandidate({
    buildInput: input,
    candidateRoot,
    outputDoc,
    packet: input.packet,
    supplementalPacket: approvedSupplementalPacket(),
    builderToolPacket: input.builderToolPacket,
    buildRunner: async ({ outputRoot }) => {
      await mkdir(outputRoot, { recursive: true });
      throw new Error("injected-build-failure");
    },
  }), /injected-build-failure/);

  await assert.rejects(runtimeBuilder.buildAndFinalizeWorkerRuntimeCandidate({
    buildInput: input,
    candidateRoot,
    outputDoc,
    packet: input.packet,
    supplementalPacket: approvedSupplementalPacket(),
    builderToolPacket: input.builderToolPacket,
    documentWriter: async () => { throw new Error("injected-document-failure"); },
  }), /injected-document-failure/);

  await assert.rejects(runtimeBuilder.buildAndFinalizeWorkerRuntimeCandidate({
    buildInput: input,
    candidateRoot,
    outputDoc,
    packet: input.packet,
    supplementalPacket: approvedSupplementalPacket(),
    builderToolPacket: input.builderToolPacket,
    candidatePromoter: async () => { throw new Error("injected-candidate-promotion-failure"); },
  }), /injected-candidate-promotion-failure/);

  assert.equal(await readFile(join(staleFirst, "user-marker"), "utf8"), "preserve");
  assert.equal(await readFile(join(staleSecond, "user-marker"), "utf8"), "preserve");
  await assert.rejects(lstat(candidateRoot), { code: "ENOENT" });
  await assert.rejects(lstat(outputDoc), { code: "ENOENT" });
  assert.deepEqual((await readdir(input.root)).filter((name) => name.startsWith(".worker-runtime-stage-")), []);
});

test("promotion reconciles an exact owned candidate when rename succeeds before the promoter throws", async (t) => {
  const input = await fixture(t, "promotion-rename-then-throw");
  const candidateRoot = join(input.root, "published-candidate");
  const outputDoc = join(input.root, "bundle.md");
  const userPath = join(input.root, "user-owned");
  await mkdir(userPath);
  await writeFile(join(userPath, "marker"), "preserve");
  await assert.rejects(runtimeBuilder.buildAndFinalizeWorkerRuntimeCandidate({
    buildInput: input,
    candidateRoot,
    outputDoc,
    packet: input.packet,
    supplementalPacket: approvedSupplementalPacket(),
    builderToolPacket: input.builderToolPacket,
    candidatePromoter: async (source, destination) => {
      await rename(source, destination);
      throw new Error("injected-after-rename");
    },
  }), /injected-after-rename/);
  await assert.rejects(lstat(candidateRoot), { code: "ENOENT" });
  await assert.rejects(lstat(outputDoc), { code: "ENOENT" });
  assert.equal(await readFile(join(userPath, "marker"), "utf8"), "preserve");
  assert.deepEqual((await readdir(input.root)).filter((name) => name.startsWith(".worker-runtime-stage-")), []);
});

test("document staging mutation is detected before candidate promotion is called", async (t) => {
  const input = await fixture(t, "document-writer-mutation");
  const candidateRoot = join(input.root, "published-candidate");
  const outputDoc = join(input.root, "bundle.md");
  let promotionCalls = 0;
  await assert.rejects(runtimeBuilder.buildAndFinalizeWorkerRuntimeCandidate({
    buildInput: input,
    candidateRoot,
    outputDoc,
    packet: input.packet,
    supplementalPacket: approvedSupplementalPacket(),
    builderToolPacket: input.builderToolPacket,
    documentWriter: async (path, contents, options) => {
      await writeFile(path, contents, options);
      await writeFile(
        join(dirname(path), "first", "runtime-root", "site-packages", "fixture_pkg", "__init__.py"),
        "mutated-after-document-stage\n",
      );
    },
    candidatePromoter: async (source, destination) => {
      promotionCalls += 1;
      await rename(source, destination);
    },
  }), /studio-runtime-final-tree-mismatch/);
  assert.equal(promotionCalls, 0);
  await assert.rejects(lstat(candidateRoot), { code: "ENOENT" });
  await assert.rejects(lstat(outputDoc), { code: "ENOENT" });
  assert.deepEqual((await readdir(input.root)).filter((name) => name.startsWith(".worker-runtime-stage-")), []);
});

test("promotion mismatch is left untouched with staged recovery evidence", async (t) => {
  const input = await fixture(t, "promotion-mismatch-recovery");
  const candidateRoot = join(input.root, "published-candidate");
  const outputDoc = join(input.root, "bundle.md");
  await assert.rejects(runtimeBuilder.buildAndFinalizeWorkerRuntimeCandidate({
    buildInput: input,
    candidateRoot,
    outputDoc,
    packet: input.packet,
    supplementalPacket: approvedSupplementalPacket(),
    builderToolPacket: input.builderToolPacket,
    candidatePromoter: async (_source, destination) => {
      await mkdir(destination);
      await writeFile(join(destination, "user-marker"), "preserve");
      throw new Error("injected-mismatched-destination");
    },
  }), /studio-runtime-promotion-recovery-required/);
  assert.equal(await readFile(join(candidateRoot, "user-marker"), "utf8"), "preserve");
  await assert.rejects(lstat(outputDoc), { code: "ENOENT" });
  const retainedStages = (await readdir(input.root)).filter((name) => name.startsWith(".worker-runtime-stage-"));
  assert.equal(retainedStages.length, 1);
  assert.equal((await lstat(join(input.root, retainedStages[0], "bundle-approval.md"))).isFile(), true);
  assert.equal((await lstat(join(input.root, retainedStages[0], "promotion-identity.json"))).isFile(), true);
});

test("approved Python archive internal file symlinks become regular files", async (t) => {
  const input = await fixture(t, "python-internal-link", { pythonLinkMode: "internal" });
  const built = await buildWorkerRuntimeBundle(input);
  const materialized = await lstat(join(built.runtimeRoot, "python", "bin", "python3"));
  assert.equal(materialized.isFile(), true);
  assert.equal(materialized.isSymbolicLink(), false);
  assert.deepEqual(
    await readFile(join(built.runtimeRoot, "python", "bin", "python3")),
    await readFile(join(built.runtimeRoot, "python", "bin", "python3.12")),
  );
});

test("non-ASCII internal Python symlink paths fail before materialization", async (t) => {
  const root = await withRoot(t);
  const pythonRoot = join(root, "python");
  await mkdir(join(pythonRoot, "bin"), { recursive: true });
  await writeFile(join(pythonRoot, "bin", "python3.12"), "fixture\n");
  const linkPath = join(pythonRoot, "bin", "é-python");
  await symlink("python3.12", linkPath);

  assert.equal(typeof runtimeBuilder.materializeInternalPythonFileLinks, "function");
  await assert.rejects(
    runtimeBuilder.materializeInternalPythonFileLinks(pythonRoot),
    /studio-runtime-path-invalid/,
  );
  assert.equal((await lstat(linkPath)).isSymbolicLink(), true);
});

test("Python runtime archives reject non-ASCII member paths before extraction", async (t) => {
  const root = await withRoot(t);
  const archive = await createTarGz(root, "non-ascii-python.tar.gz", "python", [{
    path: "bin/é-python",
    contents: "fixture\n",
  }]);
  const destination = join(root, "extracted-python");

  await assert.rejects(
    runtimeBuilder.extractArchive(archive, destination, { portableRuntimePaths: true }),
    /studio-runtime-path-invalid/,
  );
  await assert.rejects(lstat(destination), { code: "ENOENT" });
});

test("installed RECORD paths are validated even when they name no installed file", async (t) => {
  const input = await fixture(t, "non-ascii-record", { uvMode: "non-ascii-record" });
  await assert.rejects(
    buildWorkerRuntimeBundle(input),
    /studio-runtime-path-invalid/,
  );
});

for (const mode of ["non-ascii-dist-info", "non-ascii-license"]) {
  test(`installed ${mode} paths are rejected before normalization`, async (t) => {
    const input = await fixture(t, mode, { uvMode: mode });
    await assert.rejects(buildWorkerRuntimeBundle(input), /studio-runtime-path-invalid/);
  });
}

test("non-ASCII generated wheel names fail before use as an install input", async (t) => {
  const input = await fixture(t, "non-ascii-wheel", { uvMode: "non-ascii-wheel" });
  await assert.rejects(buildWorkerRuntimeBundle(input), /studio-runtime-path-invalid/);
});

test("Python archive symlinks escaping its tree remain rejected", async (t) => {
  const input = await fixture(t, "python-external-link", { pythonLinkMode: "external" });
  await assert.rejects(
    buildWorkerRuntimeBundle(input),
    /studio-runtime-source-archive-invalid/,
  );
});

test("Python archive non-program files lose undeclared executable bits", async (t) => {
  const input = await fixture(t, "python-executable-library", { pythonExecutableLibrary: true });
  const built = await buildWorkerRuntimeBundle(input);
  const normalized = await stat(join(built.runtimeRoot, "python", "lib", "python3.12", "base64.py"));
  assert.equal(normalized.mode & 0o111, 0);
});

test("Python archive bytecode caches are removed before final validation", async (t) => {
  const input = await fixture(t, "python-bytecode", { pythonBytecode: true });
  const built = await buildWorkerRuntimeBundle(input);
  await assert.rejects(
    lstat(join(built.runtimeRoot, "python", "lib", "python3.12", "encodings", "__pycache__")),
    { code: "ENOENT" },
  );
});

test("approval receipt is verified before quarantine output", async (t) => {
  const input = await fixture(t, "approval");
  const destination = join(input.root, "new-quarantine");
  await assert.rejects(fetchApprovedMaterials({
    packet: input.packet,
    approvalReceipt: { ...input.approvalReceipt, packetId: `worker-runtime-materials-v1-sha256-${"f".repeat(64)}` },
    quarantineRoot: destination,
    fetchImpl: async () => { throw new Error("must-not-fetch"); },
  }), /studio-runtime-material-approval-invalid/);
  await assert.rejects(lstat(destination), { code: "ENOENT" });
});

test("supplemental packet and receipt are exact before any fetch or output", async (t) => {
  const root = await withRoot(t);
  const packet = approvedSupplementalPacket();
  assert.equal(computeSupplementalMaterialPacketId(packet), APPROVED_SUPPLEMENTAL_PACKET_ID);
  assert.equal(
    validateSupplementalAuthorizedInputs(packet, approvedSupplementalReceipt()).packetId,
    APPROVED_SUPPLEMENTAL_PACKET_ID,
  );
  const cases = [
    ["missing", undefined],
    ["forged", approvedSupplementalReceipt({ approvedBy: "attacker" })],
    ["wrong-packet", approvedSupplementalReceipt({ packetId: `worker-runtime-build-materials-v1-sha256-${"f".repeat(64)}` })],
    ["wrong-decision", approvedSupplementalReceipt({ decision: "approve-exact-retrieval" })],
    ["install-authorized", approvedSupplementalReceipt({ installAuthorized: true })],
  ];
  for (const [name, approvalReceipt] of cases) {
    let fetchCalls = 0;
    const quarantineRoot = join(root, name);
    await assert.rejects(fetchApprovedSupplementalBuildMaterials({
      packet,
      approvalReceipt,
      quarantineRoot,
      fetchImpl: async () => {
        fetchCalls += 1;
        throw new Error("must-not-fetch");
      },
    }), /studio-runtime-supplemental-(?:packet|approval)-invalid/);
    assert.equal(fetchCalls, 0, name);
    await assert.rejects(lstat(quarantineRoot), { code: "ENOENT" });
  }
});

test("builder validates supplemental approval before output", async (t) => {
  const input = await fixture(t, "supplemental-approval");
  await assert.rejects(buildWorkerRuntimeBundle({
    ...input,
    supplementalPacket: approvedSupplementalPacket(),
    supplementalApprovalReceipt: approvedSupplementalReceipt({ approvedBy: "attacker" }),
    supplementalQuarantineRoot: join(input.root, "supplemental-quarantine"),
  }), /studio-runtime-supplemental-approval-invalid/);
  await assert.rejects(lstat(input.outputRoot), { code: "ENOENT" });
});

test("builder tool packet, approval, and executable bytes are exact before output", async (t) => {
  const input = await fixture(t, "builder-tool-approval");
  assert.equal(validateBuilderToolAuthorizedInputs(
    input.packet,
    input.builderToolPacket,
    input.builderToolApprovalReceipt,
  ).packetId, input.builderToolPacket.packetId);
  for (const changed of [
    { builderToolApprovalReceipt: undefined },
    { builderToolApprovalReceipt: builderToolReceipt(input.builderToolPacket, { approvedBy: "attacker" }) },
  ]) {
    await assert.rejects(buildWorkerRuntimeBundle({ ...input, ...changed }), /studio-runtime-builder-tool-(?:packet|approval)-invalid/);
    await assert.rejects(lstat(input.outputRoot), { code: "ENOENT" });
  }
  await assert.rejects(buildWorkerRuntimeBundle({
    ...input,
    verifiedBuilderTool: { ...input.verifiedBuilderTool },
  }), /studio-runtime-builder-tool-verified-receipt-invalid/);
  await assert.rejects(lstat(input.outputRoot), { code: "ENOENT" });
  const { verifiedBuilderTool: omittedCapability, ...prepopulated } = input;
  await assert.rejects(buildWorkerRuntimeBundle({
    ...prepopulated,
    uvPath: omittedCapability.executablePath,
  }), /studio-runtime-builder-tool-verified-receipt-invalid/);
  await assert.rejects(lstat(input.outputRoot), { code: "ENOENT" });
  await writeFile(input.verifiedBuilderTool.executablePath, "#!/bin/sh\necho uv 0.12.3\n# changed\n", { mode: 0o700 });
  await assert.rejects(buildWorkerRuntimeBundle(input), /studio-runtime-builder-tool-executable-(?:size|digest)-mismatch/);
  await assert.rejects(lstat(input.outputRoot), { code: "ENOENT" });
});

test("the approved scene extension reuses the builder tool anchored to its approved base packet", () => {
  const uvBytes = Buffer.from("approved uv executable fixture");
  const archiveBytes = executableToolArchive(uvBytes);
  const scenePacket = {
    packetId: APPROVED_SCENE_PACKET_ID,
    parentMaterialPacketId: APPROVED_PACKET_ID,
  };
  const toolPacket = builderToolPacket(APPROVED_PACKET_ID, archiveBytes, uvBytes);

  assert.equal(
    validateBuilderToolAuthorizedInputs(scenePacket, toolPacket, builderToolReceipt(toolPacket)).packetId,
    toolPacket.packetId,
  );
});

test("exported build rejects a supplemental packet for a different parent", async (t) => {
  const input = await fixture(t, "supplemental-parent");
  await assert.rejects(buildWorkerRuntimeBundle({
    ...input,
    supplementalPacket: approvedSupplementalPacket(),
    supplementalApprovalReceipt: approvedSupplementalReceipt(),
    supplementalQuarantineRoot: join(input.root, "supplemental-quarantine"),
  }), /studio-runtime-supplemental-parent-mismatch/);
  await assert.rejects(lstat(input.outputRoot), { code: "ENOENT" });
});

test("uv commands are reverified and wrapped by the macOS deny-network sandbox with timeouts", async (t) => {
  const input = await fixture(t, "sandboxed-uv");
  const calls = [];
  const built = await buildWorkerRuntimeBundle({
    ...input,
    commandRunner: async (command, args, options) => {
      calls.push({ kind: "other", command, args, options });
      return execFile(command, args, options);
    },
    uvProcessRunner: async (command, args, options) => {
      calls.push({ kind: "uv", command, args, options });
      return spawnBoundedProcess(command, args, options);
    },
  });
  assert.match(built.archiveSha256, /^[a-f0-9]{64}$/u);
  const uvCalls = calls.filter((call) => call.kind === "uv");
  assert.equal(uvCalls.length, 4);
  assert.equal(uvCalls.every((call) => call.command === "/usr/bin/sandbox-exec"), true);
  assert.equal(uvCalls.every((call) => call.args[0] === "-f" && call.options.timeout > 0), true);
  assert.equal(uvCalls.every((call) => call.args[2].includes(".uv-invocation-") && call.args[2].endsWith("/uv")), true);
  assert.equal(uvCalls.every((call) => !call.args.includes(input.verifiedBuilderTool.executablePath)), true);
  assert.equal(uvCalls.every((call) => !call.args.includes(input.quarantineRoot)), true);
  assert.equal(uvCalls.every((call) => call.args.some((arg) => arg.includes("material-snapshot")) || call.args.includes("--version")), true);
});

test("CLI outputs must be strict real children without symlinked ancestors", async (t) => {
  const root = await withRoot(t);
  const worktree = join(root, "worktree");
  const outside = join(root, "outside");
  await mkdir(worktree);
  await mkdir(outside);
  await symlink(outside, join(worktree, "linked"));
  await assert.rejects(validateCliOutputPaths(worktree, join(worktree, "linked", "candidate"), join(worktree, "packet.md")), /studio-runtime-cli-path-symlink/);
  await assert.rejects(validateCliOutputPaths(worktree, worktree, join(worktree, "packet.md")), /studio-runtime-cli-path-outside-worktree/);
  await assert.rejects(validateCliOutputPaths(worktree, join(root, "sibling"), join(worktree, "packet.md")), /studio-runtime-cli-path-outside-worktree/);
  await validateCliOutputPaths(worktree, join(worktree, "candidate"), join(worktree, "docs", "packet.md"));
  await symlink(outside, join(worktree, "candidate.first"));
  await validateCliOutputPaths(worktree, join(worktree, "candidate"), join(worktree, "docs", "packet.md"));
});

test("GitHub redirect policy approval is mandatory and exact before any fetch or output", async (t) => {
  const root = await withRoot(t);
  const cases = [
    ["missing", undefined],
    ["forged", approvedRedirectReceipt({ approvedBy: "attacker" })],
    ["wrong-packet", approvedRedirectReceipt({ packetId: `worker-runtime-materials-v1-sha256-${"f".repeat(64)}` })],
    ["wrong-policy", approvedRedirectReceipt({ policyId: `github-release-one-hop-v1-sha256-${"f".repeat(64)}` })],
  ];
  for (const [name, redirectApproval] of cases) {
    let fetchCalls = 0;
    const destinationPath = join(root, `${name}.partial`);
    await assert.rejects(fetchVerifiedGithubReleaseRedirect({
      packetId: APPROVED_PACKET_ID,
      material: APPROVED_PYTHON_MATERIAL,
      redirectPolicy: approvedRedirectPolicy(),
      redirectApproval,
      destinationPath,
      fetchImpl: async () => {
        fetchCalls += 1;
        throw new Error("fetch-must-not-run");
      },
    }), /studio-runtime-github-redirect-approval-invalid/);
    assert.equal(fetchCalls, 0, name);
    await assert.rejects(lstat(destinationPath), { code: "ENOENT" });
  }
});

for (const [name, firstStatus, location] of [
  ["wrong status", 307, approvedRedirectUrl()],
  ["wrong host", 302, `https://evil.example${APPROVED_REDIRECT_PATH}?signed=ephemeral`],
  ["wrong path", 302, "https://release-assets.githubusercontent.com/github-production-release-asset/wrong/object?signed=ephemeral"],
  ["wrong scheme", 302, `http://release-assets.githubusercontent.com${APPROVED_REDIRECT_PATH}?signed=ephemeral`],
]) {
  test(`GitHub redirect policy rejects ${name}`, async (t) => {
    const root = await withRoot(t);
    let fetchCalls = 0;
    await assert.rejects(fetchVerifiedGithubReleaseRedirect({
      packetId: APPROVED_PACKET_ID,
      material: APPROVED_PYTHON_MATERIAL,
      redirectPolicy: approvedRedirectPolicy(),
      redirectApproval: approvedRedirectReceipt(),
      destinationPath: join(root, "python.partial"),
      fetchImpl: async () => {
        fetchCalls += 1;
        return new Response(null, { status: firstStatus, headers: { location } });
      },
    }), /studio-runtime-redirect-rejected/);
    assert.equal(fetchCalls, 1);
  });
}

test("GitHub redirect request forwards neither credentials nor response cookies", async (t) => {
  const root = await withRoot(t);
  const calls = [];
  await assert.rejects(fetchVerifiedGithubReleaseRedirect({
    packetId: APPROVED_PACKET_ID,
    material: APPROVED_PYTHON_MATERIAL,
    redirectPolicy: approvedRedirectPolicy(),
    redirectApproval: approvedRedirectReceipt(),
    destinationPath: join(root, "python.partial"),
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      if (calls.length === 1) {
        return new Response(null, {
          status: 302,
          headers: { location: approvedRedirectUrl(), "set-cookie": "session=must-not-forward" },
        });
      }
      return new Response("short", { status: 200, headers: { "content-length": "5" } });
    },
  }), /studio-runtime-material-size-mismatch/);
  assert.equal(calls.length, 2);
  for (const call of calls) {
    const headers = new Headers(call.init.headers);
    assert.equal(headers.has("authorization"), false);
    assert.equal(headers.has("cookie"), false);
    assert.equal(call.init.credentials, "omit");
  }
});

test("GitHub redirect policy rejects a second hop", async (t) => {
  const root = await withRoot(t);
  let fetchCalls = 0;
  await assert.rejects(fetchVerifiedGithubReleaseRedirect({
    packetId: APPROVED_PACKET_ID,
    material: APPROVED_PYTHON_MATERIAL,
    redirectPolicy: approvedRedirectPolicy(),
    redirectApproval: approvedRedirectReceipt(),
    destinationPath: join(root, "python.partial"),
    fetchImpl: async () => {
      fetchCalls += 1;
      return fetchCalls === 1
        ? new Response(null, { status: 302, headers: { location: approvedRedirectUrl() } })
        : new Response(null, { status: 302, headers: { location: approvedRedirectUrl("?second=hop") } });
    },
  }), /studio-runtime-redirect-rejected/);
  assert.equal(fetchCalls, 2);
});

test("GitHub redirect policy rejects final body length mismatch", async (t) => {
  const root = await withRoot(t);
  let fetchCalls = 0;
  await assert.rejects(fetchVerifiedGithubReleaseRedirect({
    packetId: APPROVED_PACKET_ID,
    material: APPROVED_PYTHON_MATERIAL,
    redirectPolicy: approvedRedirectPolicy(),
    redirectApproval: approvedRedirectReceipt(),
    destinationPath: join(root, "python.partial"),
    fetchImpl: async () => {
      fetchCalls += 1;
      return fetchCalls === 1
        ? new Response(null, { status: 302, headers: { location: approvedRedirectUrl() } })
        : new Response("short", { status: 200, headers: { "content-length": "5" } });
    },
  }), /studio-runtime-material-size-mismatch/);
  assert.equal(fetchCalls, 2);
});

test("GitHub redirect policy rejects final body digest mismatch at exact length", async (t) => {
  const root = await withRoot(t);
  let fetchCalls = 0;
  const wrongBytes = Buffer.alloc(APPROVED_PYTHON_MATERIAL.byteLength);
  await assert.rejects(fetchVerifiedGithubReleaseRedirect({
    packetId: APPROVED_PACKET_ID,
    material: APPROVED_PYTHON_MATERIAL,
    redirectPolicy: approvedRedirectPolicy(),
    redirectApproval: approvedRedirectReceipt(),
    destinationPath: join(root, "python.partial"),
    fetchImpl: async () => {
      fetchCalls += 1;
      return fetchCalls === 1
        ? new Response(null, { status: 302, headers: { location: approvedRedirectUrl() } })
        : new Response(wrongBytes, {
          status: 200,
          headers: { "content-length": String(wrongBytes.length) },
        });
    },
  }), /studio-runtime-material-digest-mismatch/);
  assert.equal(fetchCalls, 2);
  await assert.rejects(lstat(join(root, "python.partial")), { code: "ENOENT" });
});

test("documented CLI flags map to the builder's camel-case paths", () => {
  const parsed = parseWorkerRuntimeBuildCli([
    "--materials", "materials.json",
    "--candidate-root", "candidate",
    "--output-doc", "packet.md",
  ], "/private/worktree");
  assert.deepEqual(parsed, {
    materials: "/private/worktree/materials.json",
    candidateRoot: "/private/worktree/candidate",
    outputDoc: "/private/worktree/packet.md",
  });
});

test("builder accepts approved uv 0.12.3 with official build metadata", async (t) => {
  const input = await fixture(t, "uv-version-metadata", { uvMode: "version-metadata" });
  const built = await buildWorkerRuntimeBundle(input);
  assert.match(built.archiveSha256, /^[a-f0-9]{64}$/u);
});

test("complete owned quarantine is reused only after every material is rehashed", async (t) => {
  const input = await fixture(t, "reuse");
  const reused = await reuseApprovedMaterials({
    packet: input.packet,
    approvalReceipt: input.approvalReceipt,
    quarantineRoot: input.quarantineRoot,
  });
  assert.equal(reused.downloadedBytes, 0);
  assert.equal(reused.reusedBytes, input.packet.expectedTotalDownloadBytes);
  assert.equal(reused.receipts.length, input.packet.materials.length);
  assert.equal(reused.receipts.every((item) => item.source === "reused-rehashed"), true);

  await writeFile(join(input.quarantineRoot, input.packet.materials[2].filename), "tampered");
  await assert.rejects(reuseApprovedMaterials({
    packet: input.packet,
    approvalReceipt: input.approvalReceipt,
    quarantineRoot: input.quarantineRoot,
  }), /studio-runtime-material-(?:size|digest)-mismatch/);
});

test("builder rejects wrong lock, forged packet id, and missing approval", async (t) => {
  const input = await fixture(t, "approval-rejections");
  await assert.rejects(buildWorkerRuntimeBundle({
    ...input,
    packet: { ...input.packet, dependencyLockSha256: "f".repeat(64) },
  }), /studio-runtime-material-packet-id-mismatch/);
  const wrongLockCore = {
    ...input.packet,
    dependencyLockSha256: "f".repeat(64),
  };
  delete wrongLockCore.packetId;
  const wrongLockPacket = { ...wrongLockCore, packetId: computeMaterialPacketId(wrongLockCore) };
  await assert.rejects(buildWorkerRuntimeBundle({
    ...input,
    packet: wrongLockPacket,
    approvalReceipt: { ...input.approvalReceipt, packetId: wrongLockPacket.packetId },
  }), /studio-runtime-lock-mismatch/);
  await assert.rejects(buildWorkerRuntimeBundle({
    ...input,
    approvalReceipt: undefined,
  }), /studio-runtime-material-approval-invalid/);
});

test("builder rejects extra, missing, and digest-mismatched quarantine bytes", async (t) => {
  const extra = await fixture(t, "extra");
  await writeFile(join(extra.quarantineRoot, "extra.whl"), "extra");
  await assert.rejects(buildWorkerRuntimeBundle(extra), /studio-runtime-quarantine-extra/);

  const missing = await fixture(t, "missing");
  await rm(join(missing.quarantineRoot, "fixture-1.0.0-py3-none-any.whl"));
  await assert.rejects(buildWorkerRuntimeBundle(missing), /studio-runtime-material-missing/);

  const changed = await fixture(t, "changed");
  await writeFile(join(changed.quarantineRoot, "fixture-1.0.0-py3-none-any.whl"), "changed");
  await assert.rejects(buildWorkerRuntimeBundle(changed), /studio-runtime-material-(?:size|digest)-mismatch/);
});

test("builder rejects an offline build that asks for network", async (t) => {
  const input = await fixture(t, "network", { uvMode: "network" });
  await assert.rejects(buildWorkerRuntimeBundle(input), /studio-runtime-network-required-build/);
});

test("builder does not mislabel an unrelated offline command failure as network-required", async (t) => {
  const input = await fixture(t, "generic-failure", { uvMode: "generic-failure" });
  await assert.rejects(buildWorkerRuntimeBundle(input), /studio-runtime-offline-build-failed/);
});

test("builder removes generated install artifacts and redacts upstream macOS build homes", async (t) => {
  const input = await fixture(t, "upstream-build-artifacts", { uvMode: "upstream-build-artifacts" });
  const built = await buildWorkerRuntimeBundle(input);
  await assert.rejects(lstat(join(built.runtimeRoot, "site-packages", "aiohttp", ".hash")), { code: "ENOENT" });
  await assert.rejects(lstat(join(built.runtimeRoot, "site-packages", "bin")), { code: "ENOENT" });
  await assert.rejects(
    lstat(join(built.runtimeRoot, "site-packages", "mlx_vlm-0.6.6.dist-info", "direct_url.json")),
    { code: "ENOENT" },
  );
  assert.equal(
    await readFile(join(built.runtimeRoot, "site-packages", "fixture_pkg", "upstream.txt"), "utf8"),
    "<build-home>/Desktop/project/example.py\n",
  );
});

test("builder removes uv's zero-byte target lock before RECORD ownership validation", async (t) => {
  const input = await fixture(t, "target-lock", { uvMode: "target-lock" });
  const built = await buildWorkerRuntimeBundle(input);
  await assert.rejects(lstat(join(built.runtimeRoot, "site-packages", ".lock")), { code: "ENOENT" });
});

test("builder clears undeclared execute bits only from files in installed RECORD manifests", async (t) => {
  const input = await fixture(t, "recorded-executable", { uvMode: "recorded-executable" });
  const built = await buildWorkerRuntimeBundle(input);
  const normalized = await stat(join(built.runtimeRoot, "site-packages", "fixture_pkg", "__init__.py"));
  assert.equal(normalized.mode & 0o111, 0);
});

test("builder drops uv cache timestamps and canonicalizes installed RECORD manifests", async (t) => {
  const input = await fixture(t, "canonical-records");
  const built = await buildWorkerRuntimeBundle(input);
  const metadataRoot = join(built.runtimeRoot, "site-packages", "mlx_vlm-0.6.6.dist-info");
  await assert.rejects(lstat(join(metadataRoot, "uv_cache.json")), { code: "ENOENT" });
  const record = await readFile(join(metadataRoot, "RECORD"), "utf8");
  assert.doesNotMatch(record, /bin\/generated-tool|worker-runtime-build-test/u);
  assert.match(record, /^mlx_vlm-0\.6\.6\.dist-info\/METADATA,sha256=[A-Za-z0-9_-]{43},\d+$/mu);
  assert.match(record, /^mlx_vlm-0\.6\.6\.dist-info\/RECORD,,$/mu);
});

test("builder rejects installed ordinary files with no RECORD owner", async (t) => {
  const input = await fixture(t, "unowned", { uvMode: "unowned" });
  await assert.rejects(buildWorkerRuntimeBundle(input), /studio-runtime-unowned-installed-file:fixture_pkg\/unowned\.dat/);
});

for (const [mode, error] of [
  ["symlink", /studio-runtime-tree-link-rejected/],
  ["hardlink", /studio-runtime-tree-link-rejected/],
  ["bytecode", /studio-runtime-bytecode-rejected/],
  ["absolute-path", /studio-runtime-absolute-build-path/],
  ["executable", /studio-runtime-executable-not-allowlisted/],
]) {
  test(`builder rejects ${mode} contamination in the final runtime tree`, async (t) => {
    const input = await fixture(t, mode, { uvMode: mode });
    await assert.rejects(buildWorkerRuntimeBundle(input), error);
  });
}

test("deterministic tar validator rejects non-normalized archive metadata", async (t) => {
  const input = await fixture(t, "tar-metadata");
  const built = await buildWorkerRuntimeBundle(input);
  const bytes = await readFile(built.archivePath);
  bytes.write("00000000001", 136, 11, "ascii");
  const changedPath = join(input.root, "changed.tar");
  await writeFile(changedPath, bytes);
  await assert.rejects(assertDeterministicRuntimeTar(changedPath), /studio-runtime-tar-metadata-invalid/);
});

test("quarantine and output files use private or normalized modes", async (t) => {
  const input = await fixture(t, "modes");
  const built = await buildWorkerRuntimeBundle(input);
  assert.equal((await stat(input.quarantineRoot)).mode & 0o777, 0o700);
  for (const path of [built.archivePath, built.manifestPath, built.packagesPath, built.receiptPath]) {
    assert.equal((await stat(path)).mode & 0o777, 0o600);
  }
});

test("scene material extension proves an unchanged parent plus exactly two approved rows", () => {
  const packet = sceneExtensionFixture();
  const validated = validateSceneMaterialExtension(packet);

  assert.equal(validated.parentMaterialPacketId, packet.parentMaterialPacketId);
  assert.equal(validated.workerDependencyLockSha256, APPROVED_WORKER_LOCK_SHA256);
  assert.deepEqual(validated.materials.slice(-2), SCENE_MATERIALS);
  assert.deepEqual(runtimeDependencyLockBindings(validated), {
    dependencyLockSha256: APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256,
    workerDependencyLockSha256: APPROVED_WORKER_LOCK_SHA256,
  });
});

test("scene material extension rejects missing or mismatched Worker lock", () => {
  const packet = sceneExtensionFixture();
  const { workerDependencyLockSha256: _removed, ...missing } = packet;
  assert.throws(
    () => validateSceneMaterialExtension(withPacketDigest(missing)),
    /studio-runtime-worker-lock-mismatch/,
  );
  assert.throws(
    () => validateSceneMaterialExtension(withPacketDigest({
      ...packet,
      workerDependencyLockSha256: "f".repeat(64),
    })),
    /studio-runtime-worker-lock-mismatch/,
  );
});

test("scene material extension rejects extra, replaced, or duplicate scene materials", () => {
  const packet = sceneExtensionFixture();
  const extra = {
    ...packet,
    materials: [...packet.materials, {
      kind: "python-distribution",
      name: "surprise",
      version: "1.0.0",
      filename: "surprise-1.0.0-py3-none-any.whl",
      url: "https://fixtures.invalid/surprise-1.0.0-py3-none-any.whl",
      sha256: "a".repeat(64),
      byteLength: 10,
    }],
    expectedTotalDownloadBytes: packet.expectedTotalDownloadBytes + 10,
  };
  assert.throws(
    () => validateSceneMaterialExtension(withPacketDigest(extra)),
    /studio-runtime-scene-delta-invalid/,
  );

  const replacedMaterials = packet.materials.map((item, index) => (
    index === 2 ? { ...item, version: "8.3.2" } : item
  ));
  assert.throws(
    () => validateSceneMaterialExtension(withPacketDigest({ ...packet, materials: replacedMaterials })),
    /studio-runtime-parent-material-mismatch/,
  );

  const prefix = packet.materials.slice(0, packet.baseMaterialCount);
  const duplicated = {
    ...packet,
    materials: [...prefix, SCENE_MATERIALS[1], SCENE_MATERIALS[1]],
    expectedTotalDownloadBytes: prefix.reduce((sum, item) => sum + item.byteLength, 0)
      + (SCENE_MATERIALS[1].byteLength * 2),
  };
  assert.throws(
    () => validateSceneMaterialExtension(withPacketDigest(duplicated)),
    /studio-runtime-scene-delta-invalid/,
  );
});

test("scene runtime METADATA accepts the fixed compatible dependency closure", () => {
  assert.deepEqual(
    validateSceneRuntimeMetadata(VALID_SCENE_INVENTORY, VALID_SCENE_METADATA),
    {
      click: "8.3.1",
      numpy: "2.4.1",
      "opencv-python": "4.13.0.90",
      platformdirs: "4.11.8",
      scenedetect: "0.7.1",
      tqdm: "4.67.1",
    },
  );
});

test("scene runtime METADATA ignores unselected real PySceneDetect extras", () => {
  assert.deepEqual(
    validateSceneRuntimeMetadata(VALID_SCENE_INVENTORY, VALID_SCENE_METADATA),
    Object.fromEntries(VALID_SCENE_INVENTORY.map(({ name, version }) => [name, version])),
  );
});

test("scene runtime METADATA rejects missing, malformed, or incomplete requirements", () => {
  assert.throws(
    () => validateSceneRuntimeMetadata(VALID_SCENE_INVENTORY, null),
    /studio-runtime-scene-metadata-missing/,
  );
  assert.throws(
    () => validateSceneRuntimeMetadata(
      VALID_SCENE_INVENTORY,
      VALID_SCENE_METADATA.replace("click~=8.0,!=8.3.0", "click => 8"),
    ),
    /studio-runtime-scene-requirement-malformed/,
  );
  assert.throws(
    () => validateSceneRuntimeMetadata(
      VALID_SCENE_INVENTORY,
      VALID_SCENE_METADATA.replace("Requires-Dist: tqdm\n", ""),
    ),
    /studio-runtime-scene-requirement-missing:tqdm/,
  );
  assert.throws(
    () => validateSceneRuntimeMetadata(
      VALID_SCENE_INVENTORY,
      VALID_SCENE_METADATA.replace('extra == "pyav"', 'sys_platform == "darwin"'),
    ),
    /studio-runtime-scene-requirement-malformed/,
  );
});

test("scene runtime METADATA rejects incompatible fixed dependencies and duplicate scene packages", () => {
  for (const [name, version] of [
    ["click", "8.3.0"],
    ["numpy", "2.3.0"],
    ["tqdm", "4.66.0"],
    ["opencv-python", "4.12.0.88"],
  ]) {
    const inventory = VALID_SCENE_INVENTORY.map((item) => (
      item.name === name ? { ...item, version } : item
    ));
    assert.throws(
      () => validateSceneRuntimeMetadata(inventory, VALID_SCENE_METADATA),
      new RegExp(`studio-runtime-scene-dependency-incompatible:${name}`),
      name,
    );
  }
  assert.throws(
    () => validateSceneRuntimeMetadata(
      [...VALID_SCENE_INVENTORY, { name: "scenedetect", version: "0.7.1" }],
      VALID_SCENE_METADATA,
    ),
    /studio-runtime-scene-package-ambiguous:scenedetect/,
  );
});
