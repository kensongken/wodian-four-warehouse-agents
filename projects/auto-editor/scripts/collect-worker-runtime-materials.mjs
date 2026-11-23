#!/usr/bin/env node

import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { lstat, mkdir, readFile, writeFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { execFile as execFileCallback } from "node:child_process";
import { promisify } from "node:util";

export const APPROVED_MLX_LOCK_SHA256 =
  "774b30f7f04e3d78faf72f75fca0d0b5823f8244d751c6c10f9c97f45fd85521";
export const APPROVED_WORKER_LOCK_SHA256 =
  "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041";
export const APPROVED_BASE_MATERIAL_PACKET_ID =
  "worker-runtime-materials-v1-sha256-8147590c4832c5ad9ac04b2b9a370dc5b55b61832334a745ada538c2b8de4895";

export const DARWIN_ARM64_CP312_TAGS = Object.freeze({
  pythonVersion: "3.12",
  implementation: "cp312",
  platform: "darwin",
  architecture: "arm64",
});

const APPROVED_MLX_SOURCE = Object.freeze({
  id: "mlx-vlm-0.6.6",
  name: "mlx-vlm",
  version: "0.6.6",
  sourceRevision: "c9e27b08311f9c1f38690c46034b04c7598d73ee",
  sourceUrl:
    "https://codeload.github.com/Blaizzy/mlx-vlm/tar.gz/c9e27b08311f9c1f38690c46034b04c7598d73ee",
  sha256: "d2f99a0258fa33ef0d15ea731a6893573e5664b84488aa2dc965865b577c8ddb",
  byteLength: 46_408_783,
  filename: "mlx-vlm-0.6.6.tar.gz",
});

const ASTRAL_RELEASE_API =
  "https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=5&page=1";
const ASTRAL_RELEASE_PAGE =
  "https://github.com/astral-sh/python-build-standalone/releases";
const ASTRAL_PYTHON_DOCS = "https://docs.astral.sh/uv/concepts/python-versions/";
const MAX_METADATA_BYTES = 64 * 1024 * 1024;
const execFile = promisify(execFileCallback);
const SCENE_RUNTIME_MATERIALS = Object.freeze({
  platformdirs: Object.freeze({
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
  }),
  scenedetect: Object.freeze({
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
  }),
});

function fail(code, detail = "") {
  throw new Error(detail ? `${code}:${detail}` : code);
}

function assertSha256(value, code) {
  if (!/^[a-f0-9]{64}$/.test(value ?? "")) fail(code);
  return value;
}

function assertPositiveInteger(value, code) {
  if (!Number.isSafeInteger(value) || value <= 0) fail(code);
  return value;
}

function publicHttpsUrl(value, code) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    fail(code);
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    fail(code);
  }
  return parsed;
}

function digestFromMetadata(value, code) {
  const match = /^sha256:([a-f0-9]{64})$/.exec(value ?? "");
  if (!match) fail(code);
  return match[1];
}

function compareVersion(left, right) {
  const a = left.split(".").map(Number);
  const b = right.split(".").map(Number);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const difference = (a[index] ?? 0) - (b[index] ?? 0);
    if (difference !== 0) return difference;
  }
  return 0;
}

function scoreComparison(left, right) {
  for (let index = 0; index < left.length; index += 1) {
    const difference = left[index] - right[index];
    if (difference !== 0) return difference;
  }
  return 0;
}

function canonicalizeProjectName(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9._-]+$/.test(value)) {
    fail("package-name-invalid");
  }
  return value.toLowerCase().replace(/[-_.]+/g, "-");
}

function parseWheelIdentity(filename) {
  if (!filename.endsWith(".whl")) return null;
  const parts = filename.slice(0, -4).split("-");
  const identityParts = parts.slice(0, -3);
  if (identityParts.length !== 2 && identityParts.length !== 3) return null;
  const [distribution, version, buildTag] = identityParts;
  if (!distribution || !version) return null;
  if (buildTag && !/^\d[0-9A-Za-z_]*$/.test(buildTag)) return null;
  return { distribution, version };
}

function parseWheelCompatibility(filename) {
  if (!filename.endsWith(".whl")) return null;
  const parts = filename.slice(0, -4).split("-");
  if (parts.length < 5) return null;
  const platformPart = parts.at(-1);
  const abiPart = parts.at(-2);
  const pythonPart = parts.at(-3);
  const pythonTags = pythonPart.split(".");
  const abiTags = abiPart.split(".");
  const platformTags = platformPart.split(".");

  let runtimeRank = null;
  if (pythonTags.includes("cp312") && abiTags.includes("cp312")) {
    runtimeRank = 0;
  } else if (
    abiTags.includes("abi3") &&
    pythonTags.some((tag) => {
      const match = /^cp3(\d+)$/.exec(tag);
      return match && Number(match[1]) <= 12;
    })
  ) {
    runtimeRank = 1;
  } else if (
    abiTags.includes("none") &&
    pythonTags.some((tag) => tag === "py3" || tag === "py312")
  ) {
    runtimeRank = 2;
  }

  if (
    platformTags.length === 1 &&
    platformTags[0] === "any" &&
    runtimeRank === 2
  ) {
    return {
      score: [runtimeRank, 2, 0, 0],
      pythonTag: pythonPart,
      abiTag: abiPart,
      platformTag: platformPart,
      minimumOsVersion: null,
    };
  }

  if (runtimeRank === null) return null;
  const compatiblePlatforms = platformTags.flatMap((tag) => {
    const match = /^macosx_(\d+)_(\d+)_(arm64|universal2)$/.exec(tag);
    if (!match) return [];
    return [{
      tag,
      major: Number(match[1]),
      minor: Number(match[2]),
      architectureRank: match[3] === "arm64" ? 0 : 1,
    }];
  });
  if (compatiblePlatforms.length === 0) return null;
  compatiblePlatforms.sort((left, right) => scoreComparison(
    [left.architectureRank, left.major, left.minor],
    [right.architectureRank, right.major, right.minor],
  ));
  const platform = compatiblePlatforms[0];
  return {
    score: [runtimeRank, platform.architectureRank, platform.major, platform.minor],
    pythonTag: pythonPart,
    abiTag: abiPart,
    platformTag: platform.tag,
    minimumOsVersion: `${platform.major}.${platform.minor}`,
  };
}

export function selectDarwinArm64Wheel({ packageName, packageVersion, wheels }) {
  const expectedProjectName = canonicalizeProjectName(packageName);
  if (typeof packageVersion !== "string" || packageVersion.length === 0) {
    fail("package-version-invalid", packageName);
  }
  if (!Array.isArray(wheels)) fail("wheel-list-invalid", packageName);
  const seenUrls = new Set();
  const compatible = [];
  for (const wheel of wheels) {
    const parsedUrl = publicHttpsUrl(wheel.url, "wheel-url-invalid");
    if (parsedUrl.hostname !== "files.pythonhosted.org") {
      fail("wheel-source-not-approved", packageName);
    }
    if (seenUrls.has(parsedUrl.href)) fail("duplicate-material-url", parsedUrl.href);
    seenUrls.add(parsedUrl.href);
    const filename = decodeURIComponent(basename(parsedUrl.pathname));
    const identity = parseWheelIdentity(filename);
    if (!identity) fail("wheel-filename-invalid", filename);
    if (canonicalizeProjectName(identity.distribution) !== expectedProjectName) {
      fail("wheel-project-name-mismatch", filename);
    }
    if (identity.version !== packageVersion) {
      fail("wheel-version-mismatch", filename);
    }
    const compatibility = parseWheelCompatibility(filename);
    if (!compatibility) continue;
    const sha256 = digestFromMetadata(wheel.hash, "wheel-hash-invalid");
    const byteLength = assertPositiveInteger(wheel.size, "wheel-size-invalid");
    compatible.push({
      filename,
      url: parsedUrl.href,
      sha256,
      byteLength,
      ...compatibility,
    });
  }
  if (compatible.length === 0) fail("no-compatible-wheel", packageName);
  compatible.sort((left, right) => scoreComparison(left.score, right.score));
  if (
    compatible.length > 1 &&
    scoreComparison(compatible[0].score, compatible[1].score) === 0
  ) {
    fail("ambiguous-wheel-candidates", packageName);
  }
  const { score: _score, ...selected } = compatible[0];
  return selected;
}

function pythonCandidate(release, asset, target) {
  if (release.draft || release.prerelease) return null;
  if (!/^\d{8}$/.test(release.tag_name ?? "")) return null;
  const escapedTarget = target.replaceAll("-", "\\-");
  const filenamePattern = new RegExp(
    `^cpython-(3\\.12\\.\\d+)\\+(\\d{8})-${escapedTarget}-install_only\\.tar\\.gz$`,
  );
  const filenameMatch = filenamePattern.exec(asset.name ?? "");
  if (!filenameMatch) return null;
  const [version, build] = filenameMatch.slice(1);
  if (build !== release.tag_name) fail("python-build-release-mismatch", asset.name);
  const parsedUrl = publicHttpsUrl(
    asset.browser_download_url,
    "python-release-url-invalid",
  );
  if (parsedUrl.pathname.includes("/releases/latest/") || parsedUrl.pathname.includes("/latest/")) {
    fail("mutable-python-release-url", parsedUrl.href);
  }
  const expectedPrefix = `/astral-sh/python-build-standalone/releases/download/${release.tag_name}/`;
  if (parsedUrl.hostname !== "github.com" || !parsedUrl.pathname.startsWith(expectedPrefix)) {
    fail("python-release-source-not-approved", parsedUrl.href);
  }
  if (decodeURIComponent(basename(parsedUrl.pathname)) !== asset.name) {
    fail("python-release-filename-mismatch", asset.name);
  }
  return {
    kind: "python-runtime",
    name: "cpython",
    version,
    build,
    target,
    filename: asset.name,
    url: parsedUrl.href,
    sha256: digestFromMetadata(asset.digest, "python-release-digest-invalid"),
    byteLength: assertPositiveInteger(asset.size, "python-release-size-invalid"),
  };
}

export function selectInstallOnlyPython(releaseIndex, {
  versionRange,
  target,
}) {
  if (versionRange !== ">=3.12.0 <3.13" || target !== "aarch64-apple-darwin") {
    fail("python-target-not-approved");
  }
  if (!Array.isArray(releaseIndex)) fail("python-release-index-invalid");
  const candidates = releaseIndex.flatMap((release) =>
    Array.isArray(release.assets)
      ? release.assets.map((asset) => pythonCandidate(release, asset, target)).filter(Boolean)
      : []);
  if (candidates.length === 0) fail("python-install-only-candidate-missing");
  candidates.sort((left, right) =>
    compareVersion(right.version, left.version) || right.build.localeCompare(left.build));
  if (
    candidates.length > 1 &&
    candidates[0].version === candidates[1].version &&
    candidates[0].build === candidates[1].build
  ) {
    fail("ambiguous-python-candidates", `${candidates[0].version}+${candidates[0].build}`);
  }
  return candidates[0];
}

function extractArrayLines(block, key) {
  const lines = block.split("\n");
  const start = lines.findIndex((line) => line === `${key} = [`);
  if (start < 0) return [];
  const result = [];
  for (let index = start + 1; index < lines.length; index += 1) {
    if (lines[index] === "]") return result;
    result.push(lines[index]);
  }
  fail("uv-lock-array-unterminated", key);
}

function quotedField(line, key) {
  const match = new RegExp(`${key} = "([^"]+)"`).exec(line);
  return match?.[1] ?? null;
}

function quotedArrayField(line, key) {
  const match = new RegExp(`${key} = \\[([^\\]]*)\\]`).exec(line);
  if (!match) return [];
  return [...match[1].matchAll(/"([^"]+)"/g)].map((item) => item[1]);
}

function parseDependencyLine(line) {
  return {
    name: quotedField(line, "name"),
    version: quotedField(line, "version"),
    marker: quotedField(line, "marker"),
    extras: quotedArrayField(line, "extra"),
  };
}

function extractOptionalDependencies(block) {
  const lines = block.split("\n");
  const start = lines.findIndex((line) => line === "[package.optional-dependencies]");
  const optionalDependencies = {};
  if (start < 0) return optionalDependencies;
  for (let index = start + 1; index < lines.length;) {
    if (lines[index].startsWith("[")) break;
    const keyMatch = /^([A-Za-z0-9_-]+) = \[$/.exec(lines[index]);
    if (!keyMatch) {
      index += 1;
      continue;
    }
    const dependencies = [];
    index += 1;
    while (index < lines.length && lines[index] !== "]") {
      if (lines[index].trim().startsWith("{")) {
        dependencies.push(parseDependencyLine(lines[index]));
      }
      index += 1;
    }
    if (index >= lines.length) fail("uv-lock-array-unterminated", keyMatch[1]);
    optionalDependencies[keyMatch[1]] = dependencies;
    index += 1;
  }
  return optionalDependencies;
}

function parseUvLock(uvLock) {
  if (typeof uvLock !== "string" || !uvLock.startsWith("version = 1\n")) {
    fail("uv-lock-format-invalid");
  }
  const blocks = uvLock.replaceAll("\r\n", "\n").split(/^\[\[package\]\]\s*$/m).slice(1);
  return blocks.map((block) => {
    const name = /^name = "([^"]+)"$/m.exec(block)?.[1];
    const version = /^version = "([^"]+)"$/m.exec(block)?.[1] ?? null;
    const source = /^source = \{ ([^}]+) \}$/m.exec(block)?.[1] ?? "";
    if (!name || !source) fail("uv-lock-package-invalid");
    const dependencies = extractArrayLines(block, "dependencies")
      .filter((line) => line.trim().startsWith("{"))
      .map(parseDependencyLine);
    const wheels = extractArrayLines(block, "wheels")
      .filter((line) => line.trim().startsWith("{"))
      .map((line) => ({
        url: quotedField(line, "url"),
        hash: quotedField(line, "hash"),
        size: Number(/\bsize = (\d+)/.exec(line)?.[1]),
      }));
    const resolutionMarkers = extractArrayLines(block, "resolution-markers")
      .map((line) => /^\s*"([^"]+)",?\s*$/.exec(line)?.[1])
      .filter(Boolean);
    return {
      name,
      version,
      source,
      dependencies,
      optionalDependencies: extractOptionalDependencies(block),
      wheels,
      resolutionMarkers,
    };
  });
}

function stripOuterParentheses(value) {
  let result = value.trim();
  while (result.startsWith("(") && result.endsWith(")")) {
    result = result.slice(1, -1).trim();
  }
  return result;
}

function markerAtomMatches(atom) {
  const match = /^(python_full_version|sys_platform|platform_machine|implementation_name|extra)\s*(==|!=|>=|<)\s*'([^']+)'$/.exec(
    stripOuterParentheses(atom),
  );
  if (!match) fail("unsupported-environment-marker", atom);
  const [, field, operator, expected] = match;
  const values = {
    python_full_version: "3.12.0",
    sys_platform: "darwin",
    platform_machine: "arm64",
    implementation_name: "cpython",
    extra: "",
  };
  const actual = values[field];
  if (operator === "==") {
    return expected.endsWith(".*")
      ? actual.startsWith(expected.slice(0, -1))
      : actual === expected;
  }
  if (operator === "!=") return actual !== expected;
  const comparison = field === "python_full_version"
    ? compareVersion(actual, expected)
    : actual.localeCompare(expected);
  return operator === ">=" ? comparison >= 0 : comparison < 0;
}

function markerMatches(marker) {
  if (!marker) return true;
  return marker
    .split(/\s+or\s+/)
    .some((clause) => stripOuterParentheses(clause)
      .split(/\s+and\s+/)
      .every(markerAtomMatches));
}

function selectLockedPackage(packages, dependency) {
  let candidates = packages.filter((item) => item.name === dependency.name);
  if (dependency.version) {
    candidates = candidates.filter((item) => item.version === dependency.version);
  } else {
    candidates = candidates.filter((item) =>
      item.resolutionMarkers.length === 0 || item.resolutionMarkers.some(markerMatches));
  }
  if (candidates.length === 0) fail("locked-package-missing", dependency.name);
  if (candidates.length > 1) fail("ambiguous-locked-package", dependency.name);
  return candidates[0];
}

export function resolveLockedDistributions(uvLock, _tags = DARWIN_ARM64_CP312_TAGS) {
  const packages = parseUvLock(uvLock);
  const roots = packages.filter((item) => item.name === "mlx-vlm" && item.source.includes("editable"));
  if (roots.length !== 1) fail("mlx-vlm-lock-root-invalid");
  const selected = new Map();
  const visitDependencies = (item, dependencies) => {
    for (const dependency of dependencies) {
      if (!dependency.name) fail("uv-lock-dependency-invalid", item.name);
      if (!markerMatches(dependency.marker)) continue;
      visit(selectLockedPackage(packages, dependency), dependency.extras);
    }
  };
  const visit = (item, requestedExtras = []) => {
    const identity = `${item.name}==${item.version ?? "editable"}`;
    let state = selected.get(identity);
    if (!state) {
      state = { item, baseVisited: false, extrasVisited: new Set() };
      selected.set(identity, state);
    }
    if (!state.baseVisited) {
      state.baseVisited = true;
      visitDependencies(item, item.dependencies);
    }
    for (const extra of requestedExtras) {
      if (state.extrasVisited.has(extra)) continue;
      const extraDependencies = item.optionalDependencies[extra];
      if (!extraDependencies) fail("locked-package-extra-missing", `${item.name}[${extra}]`);
      state.extrasVisited.add(extra);
      visitDependencies(item, extraDependencies);
    }
  };
  visit(roots[0]);
  return [...selected.values()].map((state) => state.item)
    .filter((item) => item.source.includes("registry"))
    .map((item) => {
      if (item.source !== 'registry = "https://pypi.org/simple"') {
        fail("locked-package-source-not-approved", item.name);
      }
      const wheel = selectDarwinArm64Wheel({
        packageName: item.name,
        packageVersion: item.version,
        wheels: item.wheels,
      });
      return {
        kind: "python-distribution",
        name: item.name,
        version: item.version,
        ...wheel,
      };
    })
    .sort((left, right) => left.name.localeCompare(right.name) || left.version.localeCompare(right.version));
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.freeze(value);
}

function assertNoDuplicateMaterialUrls(materials) {
  const urls = new Set();
  for (const material of materials) {
    if (urls.has(material.url)) fail("duplicate-material-url", material.url);
    urls.add(material.url);
  }
}

function materialPacketId(packetCore) {
  const digest = createHash("sha256").update(canonicalJson(packetCore)).digest("hex");
  return `worker-runtime-materials-v1-sha256-${digest}`;
}

function validateBaseMaterialPacket(basePacket, approvedBasePacketId) {
  if (
    basePacket === null
    || typeof basePacket !== "object"
    || Array.isArray(basePacket)
    || basePacket.packetId !== approvedBasePacketId
  ) fail("base-material-packet-id-mismatch");
  const { packetId, ...packetCore } = basePacket;
  if (materialPacketId(packetCore) !== packetId) {
    fail("base-material-packet-digest-mismatch");
  }
  if (
    basePacket.schemaVersion !== 1
    || basePacket.status !== "awaiting-exact-retrieval-approval"
    || basePacket.approved !== false
    || basePacket.installAuthorized !== false
    || basePacket.dependencyLockSha256 !== APPROVED_MLX_LOCK_SHA256
    || basePacket.target?.pythonVersionRange !== ">=3.12.0 <3.13"
    || basePacket.target?.platform !== "darwin"
    || basePacket.target?.architecture !== "arm64"
    || !Array.isArray(basePacket.metadataSources)
    || !Array.isArray(basePacket.materials)
    || basePacket.materials.length === 0
  ) fail("base-material-packet-invalid");
  if (basePacket.materials.some((material) => (
    ["scenedetect", "platformdirs"].includes(canonicalizeProjectName(material?.name))
  ))) fail("base-material-packet-scene-delta-already-present");
  assertNoDuplicateMaterialUrls(basePacket.materials);
  const expectedBytes = basePacket.materials.reduce((total, material) => (
    total + assertPositiveInteger(material?.byteLength, "base-material-byte-length-invalid")
  ), 0);
  if (expectedBytes !== basePacket.expectedTotalDownloadBytes) {
    fail("base-material-total-byte-length-mismatch");
  }
  return basePacket;
}

function sceneRuntimeMaterial(packages, name) {
  const expected = SCENE_RUNTIME_MATERIALS[name];
  const candidates = packages.filter((item) => canonicalizeProjectName(item.name) === name);
  if (candidates.length === 0) fail("scene-runtime-package-missing", name);
  if (candidates.length !== 1) fail("scene-runtime-package-ambiguous", name);
  const item = candidates[0];
  if (
    item.version !== expected.version
    || item.source !== 'registry = "https://pypi.org/simple"'
  ) fail("scene-runtime-material-identity-mismatch", name);
  const selected = {
    kind: "python-distribution",
    name: item.name,
    version: item.version,
    ...selectDarwinArm64Wheel({
      packageName: item.name,
      packageVersion: item.version,
      wheels: item.wheels,
    }),
  };
  if (canonicalJson(selected) !== canonicalJson(expected)) {
    fail("scene-runtime-material-identity-mismatch", name);
  }
  return selected;
}

export function extendWorkerRuntimeMaterials(input, policy = {}) {
  const approvedBasePacketId = policy.approvedBasePacketId
    ?? APPROVED_BASE_MATERIAL_PACKET_ID;
  const approvedWorkerLockSha256 = policy.approvedWorkerLockSha256
    ?? APPROVED_WORKER_LOCK_SHA256;
  const basePacket = validateBaseMaterialPacket(input?.basePacket, approvedBasePacketId);
  if (typeof input?.workerLock !== "string") fail("worker-dependency-lock-invalid");
  const actualWorkerLockSha256 = createHash("sha256")
    .update(input.workerLock, "utf8")
    .digest("hex");
  if (
    input.workerLockSha256 !== approvedWorkerLockSha256
    || actualWorkerLockSha256 !== approvedWorkerLockSha256
  ) fail("worker-dependency-lock-digest-mismatch");
  const packages = parseUvLock(input.workerLock);
  const delta = [
    sceneRuntimeMaterial(packages, "platformdirs"),
    sceneRuntimeMaterial(packages, "scenedetect"),
  ];
  const materials = [...basePacket.materials, ...delta];
  assertNoDuplicateMaterialUrls(materials);
  const packetCore = {
    schemaVersion: 1,
    status: "awaiting-exact-retrieval-approval",
    approved: false,
    installAuthorized: false,
    parentMaterialPacketId: basePacket.packetId,
    baseMaterialCount: basePacket.materials.length,
    target: basePacket.target,
    dependencyLockSha256: basePacket.dependencyLockSha256,
    workerDependencyLockSha256: actualWorkerLockSha256,
    metadataSources: basePacket.metadataSources,
    expectedTotalDownloadBytes: materials.reduce(
      (total, material) => total + material.byteLength,
      0,
    ),
    materials,
  };
  return deepFreeze({ ...packetCore, packetId: materialPacketId(packetCore) });
}

function approvedMlxMaterial(descriptor) {
  for (const [field, expected] of Object.entries(APPROVED_MLX_SOURCE)) {
    if (descriptor?.[field] !== expected) {
      fail("mlx-source-identity-mismatch", field);
    }
  }
  return {
    kind: "source-package",
    name: APPROVED_MLX_SOURCE.name,
    version: APPROVED_MLX_SOURCE.version,
    sourceRevision: APPROVED_MLX_SOURCE.sourceRevision,
    filename: APPROVED_MLX_SOURCE.filename,
    url: APPROVED_MLX_SOURCE.sourceUrl,
    sha256: APPROVED_MLX_SOURCE.sha256,
    byteLength: APPROVED_MLX_SOURCE.byteLength,
  };
}

export async function collectWorkerRuntimeMaterials(input) {
  if (input.approvedLockSha256 !== APPROVED_MLX_LOCK_SHA256) {
    fail("dependency-lock-digest-not-approved");
  }
  const mlxSource = approvedMlxMaterial(input.mlxSourceTar);
  if (input.mlxSourceTar?.uvLockSha256 !== APPROVED_MLX_LOCK_SHA256) {
    fail("embedded-lock-digest-not-approved");
  }
  if (typeof input.mlxSourceTar.uvLock !== "string") fail("uv-lock-format-invalid");
  const actualLockSha256 = createHash("sha256")
    .update(input.mlxSourceTar.uvLock, "utf8")
    .digest("hex");
  if (actualLockSha256 !== APPROVED_MLX_LOCK_SHA256) {
    fail("embedded-lock-content-digest-not-approved");
  }
  const target = input.target ?? {};
  if (
    target.python !== "3.12" ||
    target.platform !== "macos" ||
    target.architecture !== "arm64"
  ) {
    fail("runtime-material-target-not-approved");
  }

  const python = selectInstallOnlyPython(input.pythonReleaseIndex, {
    versionRange: ">=3.12.0 <3.13",
    target: "aarch64-apple-darwin",
  });
  const distributions = resolveLockedDistributions(
    input.mlxSourceTar.uvLock,
    DARWIN_ARM64_CP312_TAGS,
  );
  const materials = [python, mlxSource, ...distributions];
  assertNoDuplicateMaterialUrls(materials);
  const packetCore = {
    schemaVersion: 1,
    status: "awaiting-exact-retrieval-approval",
    approved: false,
    installAuthorized: false,
    target: {
      pythonVersionRange: ">=3.12.0 <3.13",
      platform: "darwin",
      architecture: "arm64",
    },
    dependencyLockSha256: actualLockSha256,
    metadataSources: [ASTRAL_PYTHON_DOCS, ASTRAL_RELEASE_PAGE, ASTRAL_RELEASE_API],
    expectedTotalDownloadBytes: materials.reduce(
      (total, material) => total + material.byteLength,
      0,
    ),
    materials,
  };
  return deepFreeze({
    ...packetCore,
    packetId: materialPacketId(packetCore),
  });
}

export function renderMaterialPacketMarkdown(packet) {
  const rows = packet.materials.map((material) =>
    `| ${material.kind} | ${material.name} | ${material.version}${material.build ? `+${material.build}` : ""} | ${material.filename} | ${material.sha256} | ${material.byteLength} | ${material.url} |`);
  return `# Worker Runtime Exact Material Retrieval Proposal

This document is a retrieval proposal only. It does not approve retrieval, build, installation, or execution.

- Packet ID: \`${packet.packetId}\`
- Status: \`${packet.status}\`
- \`approved=false\`
- \`installAuthorized=false\`
- Target: CPython \`${packet.target.pythonVersionRange}\`, \`${packet.target.platform}-${packet.target.architecture}\`
- Approved upstream \`uv.lock\` SHA-256: \`${packet.dependencyLockSha256}\`
${packet.workerDependencyLockSha256 ? `- Approved Worker \`uv.lock\` SHA-256: \`${packet.workerDependencyLockSha256}\`\n- Parent material packet: \`${packet.parentMaterialPacketId}\`` : ""}
- Expected total download size: \`${packet.expectedTotalDownloadBytes}\` bytes

## Exact materials

| Kind | Name | Exact version/build | Filename | SHA-256 | Bytes | Immutable candidate URL |
| --- | --- | --- | --- | --- | ---: | --- |
${rows.join("\n")}

## Metadata sources consulted

${packet.metadataSources.map((source) => `- ${source}`).join("\n")}

No selected Python archive, wheel, or source distribution body was downloaded while generating this proposal. ${packet.workerDependencyLockSha256 ? "The approved base packet and repository Worker lock were read locally only." : "The approved local `mlx-vlm` source archive was read only to verify its exact digest, byte length, and embedded upstream lock."}

## Required human decision

- \`approve exact retrieval packet ${packet.packetId}\`
- \`reject packet ${packet.packetId}\`

Approval of this packet would authorize retrieval of only the exact listed bytes into a later quarantine step. It would not authorize a build, installation, model-lock change, or model execution.
`;
}

async function hashFile(path) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest("hex");
}

async function inspectApprovedMlxSourceTar(path) {
  const sourcePath = resolve(path);
  const stat = await lstat(sourcePath);
  if (!stat.isFile() || stat.isSymbolicLink()) fail("mlx-source-not-regular-file");
  if (stat.size !== APPROVED_MLX_SOURCE.byteLength) fail("mlx-source-size-mismatch");
  if (await hashFile(sourcePath) !== APPROVED_MLX_SOURCE.sha256) {
    fail("mlx-source-digest-mismatch");
  }
  const listing = await execFile("tar", ["-tzf", sourcePath], {
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  });
  const entries = listing.stdout.split("\n").filter(Boolean);
  if (entries.some((entry) => entry.startsWith("/") || entry.split("/").includes(".."))) {
    fail("mlx-source-entry-unsafe");
  }
  const lockEntries = entries.filter((entry) => entry.endsWith("/uv.lock"));
  if (lockEntries.length !== 1) fail("embedded-lock-entry-invalid");
  const lockOutput = await execFile("tar", ["-xOzf", sourcePath, `./${lockEntries[0]}`], {
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  });
  const uvLock = lockOutput.stdout;
  const uvLockSha256 = createHash("sha256").update(uvLock).digest("hex");
  if (uvLockSha256 !== APPROVED_MLX_LOCK_SHA256) fail("embedded-lock-digest-not-approved");
  return { ...APPROVED_MLX_SOURCE, uvLock, uvLockSha256 };
}

export async function fetchAstralReleaseIndex(fetchImpl = globalThis.fetch) {
  const response = await fetchImpl(ASTRAL_RELEASE_API, {
    method: "GET",
    redirect: "error",
    signal: AbortSignal.timeout(30_000),
    headers: {
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "wearable-phase-a-runtime-materials",
    },
  });
  if (!response.ok) fail("python-release-metadata-http-error", String(response.status));
  if (!response.headers.get("content-type")?.includes("application/json")) {
    fail("python-release-metadata-type-invalid");
  }
  const declaredLength = Number(response.headers.get("content-length") ?? 0);
  if (declaredLength > MAX_METADATA_BYTES) fail("python-release-metadata-too-large");
  let byteLength = 0;
  const chunks = [];
  for await (const chunk of response.body) {
    byteLength += chunk.byteLength;
    if (byteLength > MAX_METADATA_BYTES) fail("python-release-metadata-too-large");
    chunks.push(chunk);
  }
  const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!Array.isArray(parsed)) fail("python-release-index-invalid");
  return parsed;
}

function parseCliArguments(argv) {
  const legacyFlags = ["--mlx-source", "--output-json", "--output-doc"];
  const extensionFlags = ["--base-materials", "--worker-lock", "--output-json", "--output-doc"];
  const allowed = new Set([...legacyFlags, ...extensionFlags]);
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!allowed.has(flag) || !value || value.startsWith("--") || values.has(flag)) {
      fail("cli-arguments-invalid");
    }
    values.set(flag, value);
  }
  const isLegacy = argv.length === 6 && legacyFlags.every((flag) => values.has(flag));
  const isExtension = argv.length === 8 && extensionFlags.every((flag) => values.has(flag));
  if (isLegacy === isExtension) fail("cli-arguments-invalid");
  return isLegacy ? {
    mode: "upstream",
    mlxSource: values.get("--mlx-source"),
    outputJson: values.get("--output-json"),
    outputDoc: values.get("--output-doc"),
  } : {
    mode: "extension",
    baseMaterials: values.get("--base-materials"),
    workerLock: values.get("--worker-lock"),
    outputJson: values.get("--output-json"),
    outputDoc: values.get("--output-doc"),
  };
}

async function readRegularLocalFile(path, code) {
  const resolved = resolve(path);
  const details = await lstat(resolved);
  if (!details.isFile() || details.isSymbolicLink()) fail(code);
  return readFile(resolved, "utf8");
}

async function main() {
  const cli = parseCliArguments(process.argv.slice(2));
  let packet;
  if (cli.mode === "extension") {
    const [baseMaterialsJson, workerLock] = await Promise.all([
      readRegularLocalFile(cli.baseMaterials, "base-material-packet-file-invalid"),
      readRegularLocalFile(cli.workerLock, "worker-dependency-lock-file-invalid"),
    ]);
    let basePacket;
    try {
      basePacket = JSON.parse(baseMaterialsJson);
    } catch {
      fail("base-material-packet-json-invalid");
    }
    packet = extendWorkerRuntimeMaterials({
      basePacket,
      workerLock,
      workerLockSha256: createHash("sha256").update(workerLock, "utf8").digest("hex"),
    });
  } else {
    const [mlxSourceTar, pythonReleaseIndex] = await Promise.all([
      inspectApprovedMlxSourceTar(cli.mlxSource),
      fetchAstralReleaseIndex(),
    ]);
    packet = await collectWorkerRuntimeMaterials({
      mlxSourceTar,
      approvedLockSha256: APPROVED_MLX_LOCK_SHA256,
      pythonReleaseIndex,
      target: { python: "3.12", platform: "macos", architecture: "arm64" },
    });
  }
  await mkdir(dirname(resolve(cli.outputJson)), { recursive: true });
  await mkdir(dirname(resolve(cli.outputDoc)), { recursive: true });
  await writeFile(resolve(cli.outputJson), `${JSON.stringify(packet, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  await writeFile(resolve(cli.outputDoc), renderMaterialPacketMarkdown(packet), {
    encoding: "utf8",
    mode: 0o600,
  });
  process.stdout.write(`packetId=${packet.packetId}\n`);
  process.stdout.write("approved=false installAuthorized=false\n");
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
