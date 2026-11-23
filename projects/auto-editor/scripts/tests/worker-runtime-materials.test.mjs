import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import * as materialCollector from "../collect-worker-runtime-materials.mjs";

const test = process.env.VITEST
  ? (await import("vitest")).test
  : (await import("node:test")).default;

import {
  APPROVED_MLX_LOCK_SHA256,
  collectWorkerRuntimeMaterials,
  fetchAstralReleaseIndex,
  renderMaterialPacketMarkdown,
  resolveLockedDistributions,
  selectDarwinArm64Wheel,
  selectInstallOnlyPython,
} from "../collect-worker-runtime-materials.mjs";

const extendWorkerRuntimeMaterials = (...args) => {
  assert.equal(
    typeof materialCollector.extendWorkerRuntimeMaterials,
    "function",
    "production must export the local-only material extension function",
  );
  return materialCollector.extendWorkerRuntimeMaterials(...args);
};

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SHA_C = "c".repeat(64);
const SCENEDETECT_SHA256 =
  "91b67902275b2e0d29a12f6d70435f04e0bd7852a6dc8a69c901c6069328dffa";
const PLATFORMDIRS_SHA256 =
  "52f2f181bbfde907966932cc8312d967d02976422d66d537ea16092b8e291081";
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

function wheel(filename, overrides = {}) {
  return {
    url: `https://files.pythonhosted.org/packages/fixture/${filename}`,
    hash: `sha256:${SHA_A}`,
    size: 1234,
    ...overrides,
  };
}

function pythonAsset({
  version = "3.12.14",
  build = "20260901",
  tag = build,
  url = `https://github.com/astral-sh/python-build-standalone/releases/download/${tag}/cpython-${version}%2B${build}-aarch64-apple-darwin-install_only.tar.gz`,
  digest = `sha256:${SHA_B}`,
  size = 25_135_464,
} = {}) {
  return {
    release: {
      tag_name: tag,
      draft: false,
      prerelease: false,
      published_at: `${build.slice(0, 4)}-${build.slice(4, 6)}-${build.slice(6, 8)}T00:00:00Z`,
    },
    asset: {
      name: `cpython-${version}+${build}-aarch64-apple-darwin-install_only.tar.gz`,
      browser_download_url: url,
      digest,
      size,
    },
  };
}

function releaseIndex(...entries) {
  return entries.map(({ release, asset }) => ({ ...release, assets: [asset] }));
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

function basePacketFixture() {
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
    dependencyLockSha256: APPROVED_MLX_LOCK_SHA256,
    metadataSources: ["https://example.invalid/base-metadata"],
    expectedTotalDownloadBytes: 30,
    materials: [{
      kind: "python-runtime",
      name: "cpython",
      version: "3.12.14",
      build: "20260901",
      target: "aarch64-apple-darwin",
      filename: "cpython-fixture.tar.gz",
      url: "https://example.invalid/cpython-fixture.tar.gz",
      sha256: SHA_A,
      byteLength: 10,
    }, {
      kind: "python-distribution",
      name: "existing-package",
      version: "1.0.0",
      filename: "existing_package-1.0.0-py3-none-any.whl",
      url: "https://files.pythonhosted.org/packages/fixture/existing_package-1.0.0-py3-none-any.whl",
      sha256: SHA_B,
      byteLength: 20,
      pythonTag: "py3",
      abiTag: "none",
      platformTag: "any",
      minimumOsVersion: null,
    }],
  };
  const digest = createHash("sha256").update(canonicalJson(packetCore)).digest("hex");
  return { ...packetCore, packetId: `worker-runtime-materials-v1-sha256-${digest}` };
}

function sceneWorkerLock({
  duplicateScenedetect = false,
  omitPlatformdirs = false,
  scenedetectUrl = "https://files.pythonhosted.org/packages/b8/a2/938555bc170ee504ecf33f328dc838b40c3b887d58bf4ee8beefb6c33fd8/scenedetect-0.7.1-py3-none-any.whl",
  scenedetectSha256 = SCENEDETECT_SHA256,
  scenedetectSize = 146194,
} = {}) {
  const scenedetect = `[[package]]
name = "scenedetect"
version = "0.7.1"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "${scenedetectUrl}", hash = "sha256:${scenedetectSha256}", size = ${scenedetectSize} },
]`;
  const platformdirs = `[[package]]
name = "platformdirs"
version = "4.11.8"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/packages/f4/e1/5b7b8bbb55084d1425bcb9bc823ff519e1b2be05f6ebb0089e2eacc38413/platformdirs-4.11.8-py3-none-any.whl", hash = "sha256:${PLATFORMDIRS_SHA256}", size = 24027 },
]`;
  return `version = 1
revision = 3
requires-python = ">=3.12"

${scenedetect}

${duplicateScenedetect ? scenedetect : ""}

${omitPlatformdirs ? "" : platformdirs}
`;
}

function extensionInput(overrides = {}) {
  const basePacket = basePacketFixture();
  const workerLock = sceneWorkerLock();
  return {
    basePacket,
    workerLock,
    workerLockSha256: createHash("sha256").update(workerLock).digest("hex"),
    policy: {
      approvedBasePacketId: basePacket.packetId,
      approvedWorkerLockSha256: createHash("sha256").update(workerLock).digest("hex"),
    },
    ...overrides,
  };
}

function fixtureLock(wheels, dependencyMarker = null) {
  const dependency = dependencyMarker
    ? `{ name = "fixture", marker = "${dependencyMarker}" }`
    : `{ name = "fixture" }`;
  const wheelLines = wheels
    .map((item) => `    { url = "${item.url}", hash = "${item.hash}", size = ${item.size} },`)
    .join("\n");
  return `version = 1
revision = 3
requires-python = ">=3.10"

[[package]]
name = "mlx-vlm"
source = { editable = "." }
dependencies = [
    ${dependency},
]

[[package]]
name = "fixture"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
${wheelLines}
]
`;
}

function fixtureInput(overrides = {}) {
  const nativeWheel = wheel("fixture-1.0.0-cp312-cp312-macosx_11_0_arm64.whl");
  return {
    approvedLockSha256: APPROVED_MLX_LOCK_SHA256,
    mlxSourceTar: {
      ...APPROVED_MLX_SOURCE,
      uvLock: fixtureLock([nativeWheel]),
      uvLockSha256: APPROVED_MLX_LOCK_SHA256,
    },
    pythonReleaseIndex: releaseIndex(pythonAsset()),
    target: { python: "3.12", platform: "macos", architecture: "arm64" },
    ...overrides,
  };
}

test("selects cp312 macOS arm64 and rejects foreign wheels", () => {
  const selected = selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [
      wheel("fixture-1.0-cp312-cp312-macosx_11_0_arm64.whl"),
      wheel("fixture-1.0-cp312-cp312-manylinux_2_28_aarch64.whl"),
      wheel("fixture-1.0-cp312-cp312-macosx_11_0_x86_64.whl"),
    ],
  });

  assert.match(selected.url, /macosx_11_0_arm64/);
});

test("accepts a pure Python 3 wheel", () => {
  const selected = selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [wheel("fixture-1.0-py3-none-any.whl")],
  });

  assert.match(selected.url, /py3-none-any/);
});

test("accepts a pure wheel pinned to Python 3.12", () => {
  const selected = selectDarwinArm64Wheel({
    packageName: "multiprocess",
    packageVersion: "0.70.18",
    wheels: [wheel("multiprocess-0.70.18-py312-none-any.whl")],
  });

  assert.match(selected.url, /py312-none-any/);
});

test("accepts a compatible abi3 macOS arm64 wheel", () => {
  const selected = selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [wheel("fixture-1.0-cp39-abi3-macosx_11_0_arm64.whl")],
  });

  assert.match(selected.url, /cp39-abi3-macosx_11_0_arm64/);
});

test("accepts a Python-generic macOS arm64 wheel and chooses its lowest OS floor", () => {
  const selected = selectDarwinArm64Wheel({
    packageName: "mlx-metal",
    packageVersion: "0.32.0",
    wheels: [
      wheel("mlx_metal-0.32.0-py3-none-macosx_15_0_arm64.whl"),
      wheel("mlx_metal-0.32.0-py3-none-macosx_14_0_arm64.whl"),
    ],
  });

  assert.match(selected.url, /macosx_14_0_arm64/);
});

test("accepts universal2 and prefers native arm64", () => {
  const selected = selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [
      wheel("fixture-1.0-cp312-cp312-macosx_11_0_universal2.whl"),
      wheel("fixture-1.0-cp312-cp312-macosx_11_0_arm64.whl"),
    ],
  });

  assert.match(selected.url, /arm64/);
});

test("prefers the lowest compatible macOS minimum version", () => {
  const selected = selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [
      wheel("fixture-1.0-cp312-cp312-macosx_14_0_arm64.whl"),
      wheel("fixture-1.0-cp312-cp312-macosx_11_0_arm64.whl"),
    ],
  });

  assert.match(selected.url, /macosx_11_0_arm64/);
});

test("rejects ambiguous equal-priority wheel candidates", () => {
  assert.throws(() => selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [
      wheel("fixture-1.0-cp312-cp312-macosx_11_0_arm64.whl"),
      wheel("fixture-1.0-cp312-cp312-macosx_11_0_arm64.whl", {
        url: "https://files.pythonhosted.org/packages/other/fixture-1.0-cp312-cp312-macosx_11_0_arm64.whl",
        hash: `sha256:${SHA_B}`,
      }),
    ],
  }), /ambiguous-wheel-candidates/);
});

test("rejects packages with only incompatible wheels", () => {
  assert.throws(() => selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [
      wheel("fixture-1.0-cp312-cp312-manylinux_2_28_aarch64.whl"),
      wheel("fixture-1.0-cp311-cp311-macosx_11_0_arm64.whl"),
    ],
  }), /no-compatible-wheel/);
});

test("rejects wheels missing exact digest or byte length", () => {
  assert.throws(() => selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [wheel("fixture-1.0-py3-none-any.whl", { hash: undefined })],
  }), /wheel-hash-invalid/);
  assert.throws(() => selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [wheel("fixture-1.0-py3-none-any.whl", { size: undefined })],
  }), /wheel-size-invalid/);
});

test("rejects duplicate wheel URLs", () => {
  const duplicate = wheel("fixture-1.0-py3-none-any.whl");
  assert.throws(() => selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [duplicate, { ...duplicate }],
  }), /duplicate-material-url/);
});

test("rejects a wheel whose normalized project name does not match the lock", () => {
  assert.throws(() => selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [wheel("another_project-1.0-py3-none-any.whl")],
  }), /wheel-project-name-mismatch/);
});

test("rejects a wheel whose version does not match the lock", () => {
  assert.throws(() => selectDarwinArm64Wheel({
    packageName: "fixture",
    packageVersion: "1.0",
    wheels: [wheel("fixture-2.0-py3-none-any.whl")],
  }), /wheel-version-mismatch/);
});

test("accepts equivalent hyphen, underscore, and dot project-name normalization", () => {
  for (const packageName of ["my-package", "my_package", "my.package"]) {
    const selected = selectDarwinArm64Wheel({
      packageName,
      packageVersion: "1.0.0",
      wheels: [wheel("my_package-1.0.0-py3-none-any.whl")],
    });
    assert.equal(selected.filename, "my_package-1.0.0-py3-none-any.whl");
  }
});

test("selects the newest exact install-only Python patch and build", () => {
  const selected = selectInstallOnlyPython(releaseIndex(
    pythonAsset({ version: "3.12.13", build: "20260807" }),
    pythonAsset({ version: "3.12.14", build: "20260825" }),
    pythonAsset({ version: "3.12.14", build: "20260901" }),
  ), {
    versionRange: ">=3.12.0 <3.13",
    target: "aarch64-apple-darwin",
  });

  assert.equal(selected.version, "3.12.14");
  assert.equal(selected.build, "20260901");
  assert.match(selected.url, /releases\/download\/20260901\//);
});

test("bounds the explicit official metadata request without authorization headers", async () => {
  let request;
  const index = releaseIndex(pythonAsset());
  const result = await fetchAstralReleaseIndex(async (url, init) => {
    request = { url, init };
    return new Response(JSON.stringify(index), {
      headers: { "content-type": "application/json" },
    });
  });

  assert.deepEqual(result, index);
  assert.match(request.url, /^https:\/\/api\.github\.com\/repos\/astral-sh\/python-build-standalone\/releases\?/);
  assert.equal(request.init.method, "GET");
  assert.equal(request.init.redirect, "error");
  assert.equal(request.init.headers.Authorization, undefined);
  assert.equal(request.init.signal instanceof AbortSignal, true);
});

test("rejects mutable Python release aliases", () => {
  const mutable = pythonAsset({
    url: "https://github.com/astral-sh/python-build-standalone/releases/latest/download/cpython-3.12.14%2B20260901-aarch64-apple-darwin-install_only.tar.gz",
  });

  assert.throws(() => selectInstallOnlyPython(releaseIndex(mutable), {
    versionRange: ">=3.12.0 <3.13",
    target: "aarch64-apple-darwin",
  }), /mutable-python-release-url/);
});

test("resolves only marker-compatible locked distributions", () => {
  const selected = resolveLockedDistributions(
    fixtureLock([wheel("fixture-1.0.0-py3-none-any.whl")], "sys_platform == 'darwin'"),
  );

  assert.deepEqual(selected.map((item) => `${item.name}==${item.version}`), ["fixture==1.0.0"]);
  assert.deepEqual(
    resolveLockedDistributions(
      fixtureLock([wheel("fixture-1.0.0-py3-none-any.whl")], "sys_platform == 'win32'"),
    ),
    [],
  );
});

test("follows explicitly requested dependency extras without enabling root extras", () => {
  const portable = (name) => wheel(`${name}-1.0.0-py3-none-any.whl`, {
    url: `https://files.pythonhosted.org/packages/fixture/${name}-1.0.0-py3-none-any.whl`,
  });
  const lock = `version = 1
revision = 3
requires-python = ">=3.10"

[[package]]
name = "mlx-vlm"
source = { editable = "." }
dependencies = [
    { name = "fixture", extra = ["http"] },
]

[package.optional-dependencies]
ui = [
    { name = "unrequested-ui" },
]

[[package]]
name = "fixture"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "${portable("fixture").url}", hash = "sha256:${SHA_A}", size = 1234 },
]

[package.optional-dependencies]
http = [
    { name = "http-addon" },
]

[[package]]
name = "http-addon"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "${portable("http_addon").url}", hash = "sha256:${SHA_B}", size = 1234 },
]

[[package]]
name = "unrequested-ui"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "${portable("unrequested_ui").url}", hash = "sha256:${SHA_C}", size = 1234 },
]
`;

  assert.deepEqual(
    resolveLockedDistributions(lock).map((item) => item.name),
    ["fixture", "http-addon"],
  );
});

test("rejects mismatched lock content without calling an injected body fetch", async () => {
  let fetchCalls = 0;
  await assert.rejects(collectWorkerRuntimeMaterials({
    ...fixtureInput(),
    fetchImpl: async () => {
      fetchCalls += 1;
      throw new Error("body-fetch-forbidden");
    },
  }), /embedded-lock-content-digest-not-approved/);

  assert.equal(fetchCalls, 0);
});

test("rejects every mismatched approved mlx source identity field", async () => {
  const mismatches = [
    ["id", "mlx-vlm-forged"],
    ["name", "not-mlx-vlm"],
    ["version", "0.6.7"],
    ["sourceUrl", `https://example.com/mlx-vlm/${APPROVED_MLX_SOURCE.sourceRevision}`],
    ["sourceRevision", "f".repeat(40), {
      sourceUrl: `https://codeload.github.com/Blaizzy/mlx-vlm/tar.gz/${"f".repeat(40)}`,
    }],
    ["sha256", SHA_C],
    ["byteLength", APPROVED_MLX_SOURCE.byteLength + 1],
    ["filename", "renamed.tar.gz"],
  ];

  for (const [field, value, companions = {}] of mismatches) {
    const input = fixtureInput();
    input.mlxSourceTar = {
      ...input.mlxSourceTar,
      ...companions,
      [field]: value,
    };
    await assert.rejects(
      collectWorkerRuntimeMaterials(input),
      new RegExp(`mlx-source-identity-mismatch:${field}`),
      field,
    );
  }
});

test("rejects a lock digest other than the approved upstream digest", async () => {
  await assert.rejects(collectWorkerRuntimeMaterials(fixtureInput({
    approvedLockSha256: "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041",
  })), /dependency-lock-digest-not-approved/);
});

test("renders a human decision packet without private source paths", () => {
  const packet = {
    packetId: `worker-runtime-materials-v1-sha256-${SHA_A}`,
    status: "awaiting-exact-retrieval-approval",
    approved: false,
    installAuthorized: false,
    target: {
      pythonVersionRange: ">=3.12.0 <3.13",
      platform: "darwin",
      architecture: "arm64",
    },
    dependencyLockSha256: APPROVED_MLX_LOCK_SHA256,
    expectedTotalDownloadBytes: 1234,
    metadataSources: ["https://docs.astral.sh/uv/concepts/python-versions/"],
    materials: [{
      kind: "python-distribution",
      name: "fixture",
      version: "1.0.0",
      filename: "fixture-1.0.0-py3-none-any.whl",
      sha256: SHA_A,
      byteLength: 1234,
      url: "https://files.pythonhosted.org/packages/fixture/fixture-1.0.0-py3-none-any.whl",
    }],
  };
  const markdown = renderMaterialPacketMarkdown(packet);

  assert.match(markdown, new RegExp(`approve exact retrieval packet ${packet.packetId}`));
  assert.match(markdown, /reject packet/);
  assert.match(markdown, /approved=false/);
  assert.match(markdown, /installAuthorized=false/);
  assert.doesNotMatch(markdown, /\/Users\//);
});

test("extends a verified base packet with exactly the two scene distributions", () => {
  const input = extensionInput();
  const packet = extendWorkerRuntimeMaterials(input, input.policy);

  assert.equal(packet.parentMaterialPacketId, input.basePacket.packetId);
  assert.equal(packet.baseMaterialCount, input.basePacket.materials.length);
  assert.equal(packet.dependencyLockSha256, APPROVED_MLX_LOCK_SHA256);
  assert.equal(packet.workerDependencyLockSha256, input.workerLockSha256);
  assert.deepEqual(
    packet.materials.slice(0, input.basePacket.materials.length),
    input.basePacket.materials,
  );
  assert.deepEqual(packet.materials.slice(-2), [{
    kind: "python-distribution",
    name: "platformdirs",
    version: "4.11.8",
    filename: "platformdirs-4.11.8-py3-none-any.whl",
    url: "https://files.pythonhosted.org/packages/f4/e1/5b7b8bbb55084d1425bcb9bc823ff519e1b2be05f6ebb0089e2eacc38413/platformdirs-4.11.8-py3-none-any.whl",
    sha256: PLATFORMDIRS_SHA256,
    byteLength: 24027,
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
    sha256: SCENEDETECT_SHA256,
    byteLength: 146194,
    pythonTag: "py3",
    abiTag: "none",
    platformTag: "any",
    minimumOsVersion: null,
  }]);
  assert.equal(packet.materials.length, input.basePacket.materials.length + 2);
  assert.equal(packet.approved, false);
  assert.equal(packet.installAuthorized, false);
});

test("rejects changed packet or Worker lock identities before composition", () => {
  const input = extensionInput();
  assert.throws(() => extendWorkerRuntimeMaterials({
    ...input,
    basePacket: { ...input.basePacket, packetId: `worker-runtime-materials-v1-sha256-${SHA_C}` },
  }, input.policy), /base-material-packet-id-mismatch/);
  assert.throws(() => extendWorkerRuntimeMaterials({
    ...input,
    workerLockSha256: SHA_C,
  }, input.policy), /worker-dependency-lock-digest-mismatch/);
});

test("rejects missing, duplicate, or identity-changed scene materials", () => {
  for (const [label, workerLock, expected] of [
    ["missing", sceneWorkerLock({ omitPlatformdirs: true }), /scene-runtime-package-missing:platformdirs/],
    ["duplicate", sceneWorkerLock({ duplicateScenedetect: true }), /scene-runtime-package-ambiguous:scenedetect/],
    ["url", sceneWorkerLock({ scenedetectUrl: "https://files.pythonhosted.org/packages/forged/scenedetect-0.7.1-py3-none-any.whl" }), /scene-runtime-material-identity-mismatch:scenedetect/],
    ["hash", sceneWorkerLock({ scenedetectSha256: SHA_C }), /scene-runtime-material-identity-mismatch:scenedetect/],
    ["size", sceneWorkerLock({ scenedetectSize: 146195 }), /scene-runtime-material-identity-mismatch:scenedetect/],
  ]) {
    const input = extensionInput({
      workerLock,
      workerLockSha256: createHash("sha256").update(workerLock).digest("hex"),
    });
    input.policy.approvedWorkerLockSha256 = input.workerLockSha256;
    assert.throws(() => extendWorkerRuntimeMaterials(input, input.policy), expected, label);
  }
});

test("the pure extension path never calls an injected network fetch", () => {
  let fetchCalls = 0;
  const input = extensionInput({
    fetchImpl: () => {
      fetchCalls += 1;
      throw new Error("network-forbidden");
    },
  });

  extendWorkerRuntimeMaterials(input, input.policy);
  assert.equal(fetchCalls, 0);
});
