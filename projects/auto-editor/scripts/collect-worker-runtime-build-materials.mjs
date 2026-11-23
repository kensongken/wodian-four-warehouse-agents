#!/usr/bin/env node

import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

export const PARENT_MATERIAL_PACKET_ID =
  "worker-runtime-materials-v1-sha256-8147590c4832c5ad9ac04b2b9a370dc5b55b61832334a745ada538c2b8de4895";
export const APPROVED_MLX_LOCK_SHA256 =
  "774b30f7f04e3d78faf72f75fca0d0b5823f8244d751c6c10f9c97f45fd85521";
export const MLX_SOURCE_ARTIFACT = Object.freeze({
  name: "mlx-vlm",
  version: "0.6.6",
  sourceRevision: "c9e27b08311f9c1f38690c46034b04c7598d73ee",
  sha256: "d2f99a0258fa33ef0d15ea731a6893573e5664b84488aa2dc965865b577c8ddb",
  byteLength: 46_408_783,
  filename: "mlx-vlm-0.6.6.tar.gz",
});
export const PYPI_METADATA_ENDPOINTS = Object.freeze({
  setuptools: "https://pypi.org/pypi/setuptools/json",
  wheel: "https://pypi.org/pypi/wheel/json",
});

const BUILD_REQUIREMENTS = Object.freeze(["setuptools>=61.0", "wheel"]);
const MAX_METADATA_BYTES = 2 * 1024 * 1024;
const PARENT_RUNTIME_PYTHON_VERSION = "3.12.14";
const REQUIRES_PYTHON_POLICY = "single-greater-than-or-equal-release-clause-v1";
const LICENSE_EXPRESSION_POLICY = "single-spdx-license-identifier-v1";

function fail(code, detail = "") {
  throw new Error(detail ? `${code}:${detail}` : code);
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

function normalizeProjectName(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9._-]+$/.test(value)) {
    fail("project-name-invalid");
  }
  return value.toLowerCase().replace(/[-_.]+/g, "-");
}

function requirementProject(requirement) {
  if (!BUILD_REQUIREMENTS.includes(requirement)) {
    fail("build-requirement-not-approved", String(requirement));
  }
  return requirement === "wheel" ? "wheel" : "setuptools";
}

function parseStableVersion(value) {
  if (typeof value !== "string" || !/^(0|[1-9]\d*)(\.(0|[1-9]\d*))*$/.test(value)) {
    fail("project-version-not-stable", String(value));
  }
  return value.split(".").map(Number);
}

function compareVersion(left, right) {
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference !== 0) return difference;
  }
  return 0;
}

function assertPython312Compatibility(specifier, scope) {
  const match = /^>=(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?$/.exec(
    specifier ?? "",
  );
  if (!match) fail("python-requires-unsupported", scope);
  const minimum = match.slice(1).filter((part) => part !== undefined).map(Number);
  const runtime = PARENT_RUNTIME_PYTHON_VERSION.split(".").map(Number);
  if (compareVersion(runtime, minimum) < 0) {
    fail("python-runtime-not-compatible", scope);
  }
  return specifier;
}

function parseWheelFilename(filename) {
  if (typeof filename !== "string" || !filename.endsWith(".whl")) {
    fail("wheel-filename-invalid", String(filename));
  }
  const parts = filename.slice(0, -4).split("-");
  if (parts.length < 5 || parts.length > 6) fail("wheel-filename-invalid", filename);
  const platformTag = parts.pop();
  const abiTag = parts.pop();
  const pythonTag = parts.pop();
  if (parts.length === 3 && !/^\d[0-9A-Za-z_]*$/.test(parts[2])) {
    fail("wheel-build-tag-invalid", filename);
  }
  const [distribution, version] = parts;
  if (!distribution || !version) fail("wheel-filename-invalid", filename);
  return {
    distribution,
    version,
    hasBuildTag: parts.length === 3,
    pythonTag,
    abiTag,
    platformTag,
  };
}

function immutableWheelUrl(value, filename) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    fail("wheel-url-not-immutable", String(value));
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname !== "files.pythonhosted.org" ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    !parsed.pathname.startsWith("/packages/") ||
    decodeURIComponent(basename(parsed.pathname)) !== filename
  ) {
    fail("wheel-url-not-immutable", parsed.href);
  }
  return parsed.href;
}

function exactSha256(value) {
  if (!/^[a-f0-9]{64}$/.test(value ?? "")) fail("wheel-digest-invalid");
  return value;
}

function positiveByteLength(value) {
  if (!Number.isSafeInteger(value) || value <= 0) fail("wheel-size-invalid");
  return value;
}

function licenseRecord(project, metadata, metadataUrl) {
  const raw = metadata.info?.license_expression;
  let value = null;
  if (raw !== null && raw !== undefined && raw !== "") {
    if (typeof raw !== "string" || !/^[A-Za-z0-9][A-Za-z0-9.+-]{0,99}$/.test(raw)) {
      fail("license-expression-unsafe", project);
    }
    value = raw;
  }
  return {
    declaredLicenseExpression: value,
    licenseEvidence: {
      metadataUrl,
      field: "info.license_expression",
      value,
      validationPolicy: LICENSE_EXPRESSION_POLICY,
    },
    licenseReviewState: value
      ? "declared-expression-recorded-pending-human-review"
      : "pending-human-review-unclear-expression",
  };
}

export function selectStablePurePythonWheel({
  requirement,
  projectMetadata,
  metadataUrl = PYPI_METADATA_ENDPOINTS[requirementProject(requirement)],
}) {
  const project = requirementProject(requirement);
  if (!projectMetadata || typeof projectMetadata !== "object") {
    fail("project-metadata-invalid", project);
  }
  if (normalizeProjectName(projectMetadata.info?.name) !== project) {
    fail("metadata-project-name-mismatch", project);
  }
  const versionParts = parseStableVersion(projectMetadata.info?.version);
  const version = projectMetadata.info.version;
  if (requirement === "setuptools>=61.0" && compareVersion(versionParts, [61, 0]) < 0) {
    fail("requirement-not-satisfied", `${project}==${version}`);
  }
  const releaseRequiresPython = assertPython312Compatibility(
    projectMetadata.info?.requires_python,
    "release",
  );
  if (!Array.isArray(projectMetadata.urls)) fail("project-release-files-invalid", project);

  const eligible = [];
  for (const candidate of projectMetadata.urls) {
    if (
      !candidate ||
      candidate.packagetype !== "bdist_wheel" ||
      candidate.yanked !== false
    ) {
      continue;
    }
    const parsed = parseWheelFilename(candidate.filename);
    if (normalizeProjectName(parsed.distribution) !== project) {
      fail("wheel-project-name-mismatch", candidate.filename);
    }
    if (parsed.version !== version) fail("wheel-version-mismatch", candidate.filename);
    if (
      parsed.hasBuildTag ||
      parsed.pythonTag !== "py3" ||
      parsed.abiTag !== "none" ||
      parsed.platformTag !== "any" ||
      candidate.python_version !== "py3"
    ) {
      continue;
    }
    eligible.push({ candidate, parsed });
  }
  if (eligible.length === 0) fail("no-exact-pure-python-wheel", project);
  if (eligible.length > 1) fail("ambiguous-wheel-candidates", project);

  const { candidate, parsed } = eligible[0];
  const fileRequiresPython = assertPython312Compatibility(
    candidate.requires_python,
    "file",
  );
  return deepFreeze({
    kind: "build-dependency-wheel",
    name: project,
    requirement,
    version,
    filename: candidate.filename,
    url: immutableWheelUrl(candidate.url, candidate.filename),
    sha256: exactSha256(candidate.digests?.sha256),
    byteLength: positiveByteLength(candidate.size),
    pythonTag: parsed.pythonTag,
    abiTag: parsed.abiTag,
    platformTag: parsed.platformTag,
    yanked: false,
    pythonCompatibility: {
      runtimeVersion: PARENT_RUNTIME_PYTHON_VERSION,
      releaseRequiresPython,
      fileRequiresPython,
      policy: REQUIRES_PYTHON_POLICY,
    },
    ...licenseRecord(project, projectMetadata, metadataUrl),
  });
}

function assertExactProjectSet(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail("metadata-project-set-invalid");
  }
  const projects = Object.keys(value).sort();
  if (projects.length !== 2 || projects[0] !== "setuptools" || projects[1] !== "wheel") {
    fail("metadata-project-set-invalid");
  }
}

function exactMetadataRecord(project, record) {
  if (!record?.projectMetadata || !record?.provenance) {
    fail("exact-metadata-provenance-missing", project);
  }
  const version = record.projectMetadata.info?.version;
  parseStableVersion(version);
  const expectedUrl = `https://pypi.org/pypi/${project}/${version}/json`;
  if (record.provenance.metadataUrl !== expectedUrl) {
    fail("exact-metadata-url-mismatch", project);
  }
  if (!/^[a-f0-9]{64}$/.test(record.provenance.responseSha256 ?? "")) {
    fail("exact-metadata-digest-invalid", project);
  }
  if (!Number.isSafeInteger(record.provenance.responseByteLength) ||
      record.provenance.responseByteLength <= 0 ||
      record.provenance.responseByteLength > MAX_METADATA_BYTES) {
    fail("exact-metadata-size-invalid", project);
  }
  return record;
}

export function collectWorkerRuntimeBuildMaterials({
  metadataRecordsByProject,
}) {
  if (metadataRecordsByProject === undefined) {
    fail("exact-metadata-provenance-missing");
  }
  assertExactProjectSet(metadataRecordsByProject);
  const materials = BUILD_REQUIREMENTS.map((requirement) => {
    const project = requirementProject(requirement);
    const record = exactMetadataRecord(project, metadataRecordsByProject[project]);
    return {
      ...selectStablePurePythonWheel({
        requirement,
        projectMetadata: record.projectMetadata,
        metadataUrl: record.provenance.metadataUrl,
      }),
      metadataEvidence: { ...record.provenance },
    };
  });
  const packetCore = {
    schemaVersion: 1,
    status: "awaiting-exact-supplemental-retrieval-approval",
    approved: false,
    retrievalAuthorized: false,
    installAuthorized: false,
    modelExecutionAuthorized: false,
    parentMaterialPacketId: PARENT_MATERIAL_PACKET_ID,
    sourceArtifact: MLX_SOURCE_ARTIFACT,
    buildSystem: {
      backend: "setuptools.build_meta",
      requirements: [...BUILD_REQUIREMENTS],
    },
    approvedDependencyLockSha256: APPROVED_MLX_LOCK_SHA256,
    target: {
      pythonVersion: PARENT_RUNTIME_PYTHON_VERSION,
      platform: "darwin",
      architecture: "arm64",
    },
    diagnosis: "offline-build-missing-build-system-requirements",
    discoveryMetadataSources: BUILD_REQUIREMENTS.map((requirement) =>
      PYPI_METADATA_ENDPOINTS[requirementProject(requirement)]),
    approvalMetadataSources: materials.map((material) =>
      material.metadataEvidence.metadataUrl),
    expectedTotalDownloadBytes: materials.reduce(
      (total, material) => total + material.byteLength,
      0,
    ),
    materials,
  };
  const digest = createHash("sha256").update(canonicalJson(packetCore)).digest("hex");
  return deepFreeze({
    ...packetCore,
    packetId: `worker-runtime-build-materials-v1-sha256-${digest}`,
  });
}

async function fetchPypiMetadataResponse(endpoint, fetchImpl) {
  const response = await fetchImpl(endpoint, {
    method: "GET",
    redirect: "error",
    credentials: "omit",
    signal: AbortSignal.timeout(30_000),
    headers: {
      Accept: "application/json",
      "User-Agent": "wearable-phase-a-runtime-build-materials",
    },
  });
  if (response.redirected || !response.ok) {
    fail("pypi-metadata-http-error", String(response.status));
  }
  if (!response.headers.get("content-type")?.toLowerCase().includes("application/json")) {
    fail("pypi-metadata-type-invalid");
  }
  const declaredLength = Number(response.headers.get("content-length") ?? 0);
  if (!Number.isFinite(declaredLength) || declaredLength < 0 || declaredLength > MAX_METADATA_BYTES) {
    fail("pypi-metadata-too-large");
  }
  if (!response.body) fail("pypi-metadata-body-missing");
  const chunks = [];
  let received = 0;
  for await (const chunk of response.body) {
    received += chunk.byteLength;
    if (received > MAX_METADATA_BYTES) fail("pypi-metadata-too-large");
    chunks.push(chunk);
  }
  let parsed;
  try {
    const raw = Buffer.concat(chunks);
    parsed = JSON.parse(raw.toString("utf8"));
    return deepFreeze({
      projectMetadata: parsed,
      provenance: {
        metadataUrl: endpoint,
        responseSha256: createHash("sha256").update(raw).digest("hex"),
        responseByteLength: raw.byteLength,
      },
    });
  } catch {
    fail("pypi-metadata-json-invalid");
  }
}

export async function fetchPypiProjectMetadata(project, fetchImpl = globalThis.fetch) {
  const endpoint = PYPI_METADATA_ENDPOINTS[project];
  if (!endpoint) fail("metadata-project-not-approved", String(project));
  return (await fetchPypiMetadataResponse(endpoint, fetchImpl)).projectMetadata;
}

export async function fetchExactPypiVersionMetadata(
  project,
  version,
  fetchImpl = globalThis.fetch,
) {
  if (!PYPI_METADATA_ENDPOINTS[project]) {
    fail("metadata-project-not-approved", String(project));
  }
  parseStableVersion(version);
  return fetchPypiMetadataResponse(
    `https://pypi.org/pypi/${project}/${version}/json`,
    fetchImpl,
  );
}

function discoveredVersion(project, requirement, record) {
  if (!record?.projectMetadata || !record?.provenance) {
    fail("discovery-metadata-invalid", project);
  }
  if (record.provenance.metadataUrl !== PYPI_METADATA_ENDPOINTS[project]) {
    fail("discovery-metadata-url-mismatch", project);
  }
  if (normalizeProjectName(record.projectMetadata.info?.name) !== project) {
    fail("metadata-project-name-mismatch", project);
  }
  const version = record.projectMetadata.info?.version;
  const parts = parseStableVersion(version);
  if (requirement === "setuptools>=61.0" && compareVersion(parts, [61, 0]) < 0) {
    fail("requirement-not-satisfied", `${project}==${version}`);
  }
  return version;
}

export async function generateWorkerRuntimeBuildMaterialProposal({
  metadataResponseFetch = (project, endpoint) =>
    endpoint === PYPI_METADATA_ENDPOINTS[project]
      ? fetchPypiMetadataResponse(endpoint, globalThis.fetch)
      : fetchExactPypiVersionMetadata(project, endpoint.split("/").at(-2)),
  materialBodyFetch,
} = {}) {
  if (typeof metadataResponseFetch !== "function") fail("metadata-fetch-invalid");
  if (materialBodyFetch !== undefined && typeof materialBodyFetch !== "function") {
    fail("material-body-fetch-invalid");
  }
  const discoveries = await Promise.all(BUILD_REQUIREMENTS.map(
    async (requirement) => {
      const project = requirementProject(requirement);
      const endpoint = PYPI_METADATA_ENDPOINTS[project];
      const record = await metadataResponseFetch(project, endpoint);
      return [project, discoveredVersion(project, requirement, record)];
    },
  ));
  const versions = Object.fromEntries(discoveries);
  const entries = await Promise.all(BUILD_REQUIREMENTS.map(async (requirement) => {
    const project = requirementProject(requirement);
    const endpoint = `https://pypi.org/pypi/${project}/${versions[project]}/json`;
    const record = await metadataResponseFetch(project, endpoint);
    if (record?.projectMetadata?.info?.version !== versions[project]) {
      fail("exact-metadata-version-mismatch", project);
    }
    return [project, record];
  }));
  return collectWorkerRuntimeBuildMaterials({
    metadataRecordsByProject: Object.fromEntries(entries),
  });
}

export function renderBuildMaterialProposalMarkdown(packet) {
  const rows = packet.materials.map((material) =>
    `| ${material.requirement} | ${material.version} | ${material.filename} | ${material.pythonCompatibility.releaseRequiresPython} | ${material.pythonCompatibility.fileRequiresPython} | ${material.sha256} | ${material.byteLength} | ${material.url} | ${material.metadataEvidence.metadataUrl} | ${material.metadataEvidence.responseSha256} | ${material.metadataEvidence.responseByteLength} | ${material.declaredLicenseExpression ?? "unclear; human review pending"} |`);
  return `# Worker Runtime Supplemental Build Material Proposal

This is a metadata-only proposal. No \`setuptools\` or \`wheel\` body was downloaded. It does not authorize retrieval, build resumption, installation, model-lock changes, or model execution.

- Packet ID: \`${packet.packetId}\`
- Parent material packet: \`${packet.parentMaterialPacketId}\`
- Status: \`${packet.status}\`
- \`approved=false\`
- \`retrievalAuthorized=false\`
- \`installAuthorized=false\`
- \`modelExecutionAuthorized=false\`
- Exact source artifact: \`${packet.sourceArtifact.filename}\`, revision \`${packet.sourceArtifact.sourceRevision}\`, SHA-256 \`${packet.sourceArtifact.sha256}\`
- Source build backend: \`${packet.buildSystem.backend}\`
- Exact source build requirements: ${packet.buildSystem.requirements.map((item) => `\`${item}\``).join(", ")}
- Approved upstream \`uv.lock\` SHA-256: \`${packet.approvedDependencyLockSha256}\`
- Target runtime: Python \`${packet.target.pythonVersion}\`, \`${packet.target.platform}-${packet.target.architecture}\`
- Requires-Python validation policy: \`${REQUIRES_PYTHON_POLICY}\`
- License-expression validation policy: \`${LICENSE_EXPRESSION_POLICY}\`
- Diagnosis: \`${packet.diagnosis}\`
- Proposed total retrieval: \`${packet.expectedTotalDownloadBytes}\` bytes

## Exact proposed supplemental materials

| Source requirement | Exact version | Filename | Release requires-python | File requires-python | SHA-256 | Bytes | Immutable candidate URL | Fixed version metadata URL | Metadata response SHA-256 | Metadata response bytes | Public license expression evidence |
| --- | --- | --- | --- | --- | --- | ---: | --- | --- | --- | ---: | --- |
${rows.join("\n")}

License expressions above are upstream declarations recorded from each fixed official PyPI JSON endpoint. They are not a legal or license approval; every item remains pending human review.

The Requires-Python validator deliberately supports only the syntax observed in these exact records: one \`>=X.Y\` or \`>=X.Y.Z\` release clause. Commas, exclusions, wildcards, compatible-release operators, environment markers and every other PEP 440 form fail closed rather than being partially interpreted.

## Mutable discovery metadata consulted (not approval evidence)

${packet.discoveryMetadataSources.map((source) => `- ${source}`).join("\n")}

## Fixed version metadata bound as approval evidence

${packet.approvalMetadataSources.map((source) => `- ${source}`).join("\n")}

## Required human decision

- \`approve exact supplemental build material packet ${packet.packetId}\`
- \`reject supplemental build material packet ${packet.packetId}\`

Approval of this supplemental packet would authorize only a later exact-byte retrieval step under a separately validated receipt. It would not by itself authorize installation, \`models.lock.json\` mutation, model execution, or production use.
`;
}

function parseCliArguments(argv) {
  const allowed = new Set(["--output-json", "--output-doc"]);
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!allowed.has(flag) || !value || value.startsWith("--") || values.has(flag)) {
      fail("cli-arguments-invalid");
    }
    values.set(flag, value);
  }
  if (argv.length !== 4 || [...allowed].some((flag) => !values.has(flag))) {
    fail("cli-arguments-invalid");
  }
  return {
    outputJson: resolve(values.get("--output-json")),
    outputDoc: resolve(values.get("--output-doc")),
  };
}

async function main() {
  const cli = parseCliArguments(process.argv.slice(2));
  const packet = await generateWorkerRuntimeBuildMaterialProposal();
  await mkdir(dirname(cli.outputJson), { recursive: true, mode: 0o700 });
  await mkdir(dirname(cli.outputDoc), { recursive: true, mode: 0o700 });
  await writeFile(cli.outputJson, `${JSON.stringify(packet, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
    flag: "wx",
  });
  await writeFile(cli.outputDoc, renderBuildMaterialProposalMarkdown(packet), {
    encoding: "utf8",
    mode: 0o600,
    flag: "wx",
  });
  process.stdout.write(`packetId=${packet.packetId}\n`);
  process.stdout.write(
    `materials=${packet.materials.length} metadataOnly=true expectedBytes=${packet.expectedTotalDownloadBytes}\n`,
  );
  process.stdout.write(
    "approved=false retrievalAuthorized=false installAuthorized=false modelExecutionAuthorized=false\n",
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
