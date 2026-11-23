import assert from "node:assert/strict";
import { createHash } from "node:crypto";

const test = process.env.VITEST
  ? (await import("vitest")).test
  : (await import("node:test")).default;

import {
  APPROVED_MLX_LOCK_SHA256,
  MLX_SOURCE_ARTIFACT,
  PARENT_MATERIAL_PACKET_ID,
  PYPI_METADATA_ENDPOINTS,
  collectWorkerRuntimeBuildMaterials,
  fetchExactPypiVersionMetadata,
  fetchPypiProjectMetadata,
  generateWorkerRuntimeBuildMaterialProposal,
  renderBuildMaterialProposalMarkdown,
  selectStablePurePythonWheel,
} from "../collect-worker-runtime-build-materials.mjs";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SHA_C = "c".repeat(64);
const SHA_D = "d".repeat(64);

function file(project, version, overrides = {}) {
  const filename = `${project.replaceAll("-", "_")}-${version}-py3-none-any.whl`;
  return {
    filename,
    packagetype: "bdist_wheel",
    python_version: "py3",
    url: `https://files.pythonhosted.org/packages/fixture/${filename}`,
    size: 12_345,
    digests: { sha256: project === "setuptools" ? SHA_A : SHA_B },
    requires_python: ">=3.9",
    yanked: false,
    yanked_reason: null,
    ...overrides,
  };
}

function metadata(project, overrides = {}) {
  const version = project === "setuptools" ? "80.9.0" : "0.46.1";
  return {
    info: {
      name: project,
      version,
      license_expression: "MIT",
      requires_python: ">=3.9",
      ...overrides.info,
    },
    urls: overrides.urls ?? [file(project, version)],
  };
}

function exactMetadataRecord(project, overrides = {}) {
  const projectMetadata = overrides.projectMetadata ?? metadata(project);
  const version = projectMetadata.info.version;
  return {
    projectMetadata,
    provenance: {
      metadataUrl: `https://pypi.org/pypi/${project}/${version}/json`,
      responseSha256: project === "setuptools" ? SHA_C : SHA_D,
      responseByteLength: project === "setuptools" ? 8_000 : 4_000,
      ...overrides.provenance,
    },
  };
}

function responseJson(value, overrides = {}) {
  const body = JSON.stringify(value);
  return new Response(body, {
    status: overrides.status ?? 200,
    headers: {
      "content-type": overrides.contentType ?? "application/json",
      "content-length": String(overrides.contentLength ?? Buffer.byteLength(body)),
    },
  });
}

test("selects one exact stable non-yanked py3-none-any wheel", () => {
  const selected = selectStablePurePythonWheel({
    requirement: "setuptools>=61.0",
    projectMetadata: metadata("setuptools"),
  });

  assert.deepEqual(selected, {
    kind: "build-dependency-wheel",
    name: "setuptools",
    requirement: "setuptools>=61.0",
    version: "80.9.0",
    filename: "setuptools-80.9.0-py3-none-any.whl",
    url: "https://files.pythonhosted.org/packages/fixture/setuptools-80.9.0-py3-none-any.whl",
    sha256: SHA_A,
    byteLength: 12_345,
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
      metadataUrl: PYPI_METADATA_ENDPOINTS.setuptools,
      field: "info.license_expression",
      value: "MIT",
      validationPolicy: "single-spdx-license-identifier-v1",
    },
    licenseReviewState: "declared-expression-recorded-pending-human-review",
  });
});

test("rejects license expressions that can alter Markdown structure or legal text", () => {
  for (const license_expression of [
    "MIT\n| injected |",
    "MIT|Apache-2.0",
    "`MIT`",
    "MIT\u0001",
    "<span>MIT</span>",
    " MIT",
  ]) {
    assert.throws(() => selectStablePurePythonWheel({
      requirement: "wheel",
      projectMetadata: metadata("wheel", { info: { license_expression } }),
    }), /license-expression-unsafe/);
  }
});

test("rejects a release or selected file that excludes Python 3.12.14", () => {
  assert.throws(() => selectStablePurePythonWheel({
    requirement: "wheel",
    projectMetadata: metadata("wheel", {
      info: { requires_python: ">=3.13" },
    }),
  }), /python-runtime-not-compatible:release/);
  assert.throws(() => selectStablePurePythonWheel({
    requirement: "wheel",
    projectMetadata: metadata("wheel", {
      urls: [file("wheel", "0.46.1", { requires_python: ">=3.13" })],
    }),
  }), /python-runtime-not-compatible:file/);
});

test("rejects requires-python syntax outside the observed narrow PEP 440 subset", () => {
  for (const requiresPython of [">=3.9,<4", "~=3.9", ">=3.9; python_version < '4'", "3.12.*"]) {
    assert.throws(() => selectStablePurePythonWheel({
      requirement: "wheel",
      projectMetadata: metadata("wheel", {
        info: { requires_python: requiresPython },
      }),
    }), /python-requires-unsupported:release/);
  }
  assert.throws(() => selectStablePurePythonWheel({
    requirement: "wheel",
    projectMetadata: metadata("wheel", {
      urls: [file("wheel", "0.46.1", { requires_python: undefined })],
    }),
  }), /python-requires-unsupported:file/);
});

test("rejects prerelease, dev, local, and mutable versions", () => {
  for (const version of ["81.0.0rc1", "81.0.0.dev1", "81.0.0+local", "latest"]) {
    assert.throws(() => selectStablePurePythonWheel({
      requirement: "setuptools>=61.0",
      projectMetadata: metadata("setuptools", {
        info: { version },
        urls: [file("setuptools", version)],
      }),
    }), /project-version-not-stable/);
  }
});

test("enforces the exact source requirement and its lower bound", () => {
  assert.throws(() => selectStablePurePythonWheel({
    requirement: "setuptools>=60.9",
    projectMetadata: metadata("setuptools", {
      info: { version: "60.9.0" },
      urls: [file("setuptools", "60.9.0")],
    }),
  }), /build-requirement-not-approved/);
  assert.throws(() => selectStablePurePythonWheel({
    requirement: "setuptools>=61.0",
    projectMetadata: metadata("setuptools", {
      info: { version: "60.9.0" },
      urls: [file("setuptools", "60.9.0")],
    }),
  }), /requirement-not-satisfied/);
});

test("rejects yanked, impure, malformed, inconsistent, and ambiguous wheels", () => {
  const invalid = [
    [file("wheel", "0.46.1", { yanked: true }), /no-exact-pure-python-wheel/],
    [file("wheel", "0.46.1", {
      filename: "wheel-0.46.1-cp312-cp312-macosx_11_0_arm64.whl",
    }), /no-exact-pure-python-wheel/],
    [file("wheel", "0.46.1", { size: 0 }), /wheel-size-invalid/],
    [file("wheel", "0.46.1", { digests: { sha256: SHA_A.toUpperCase() } }), /wheel-digest-invalid/],
    [file("wheel", "0.46.1", {
      filename: "another-0.46.1-py3-none-any.whl",
    }), /wheel-project-name-mismatch/],
    [file("wheel", "0.46.1", {
      filename: "wheel-0.45.0-py3-none-any.whl",
    }), /wheel-version-mismatch/],
    [file("wheel", "0.46.1", {
      url: "https://files.pythonhosted.org/packages/fixture/wheel-0.46.1-py3-none-any.whl?download=1",
    }), /wheel-url-not-immutable/],
  ];
  for (const [candidate, error] of invalid) {
    assert.throws(() => selectStablePurePythonWheel({
      requirement: "wheel",
      projectMetadata: metadata("wheel", { urls: [candidate] }),
    }), error);
  }
  assert.throws(() => selectStablePurePythonWheel({
    requirement: "wheel",
    projectMetadata: metadata("wheel", {
      urls: [
        file("wheel", "0.46.1"),
        file("wheel", "0.46.1", {
          url: "https://files.pythonhosted.org/packages/other/wheel-0.46.1-py3-none-any.whl",
        }),
      ],
    }),
  }), /ambiguous-wheel-candidates/);
});

test("rejects project metadata whose normalized project or wheel filename identity differs", () => {
  assert.throws(() => selectStablePurePythonWheel({
    requirement: "wheel",
    projectMetadata: metadata("wheel", { info: { name: "not-wheel" } }),
  }), /metadata-project-name-mismatch/);
  assert.throws(() => selectStablePurePythonWheel({
    requirement: "wheel",
    projectMetadata: metadata("wheel", {
      info: { version: "0.46.1" },
      urls: [file("wheel", "0.46.2")],
    }),
  }), /wheel-version-mismatch/);
});

test("records unclear public license evidence as pending instead of inventing SPDX", () => {
  const selected = selectStablePurePythonWheel({
    requirement: "wheel",
    projectMetadata: metadata("wheel", { info: { license_expression: null } }),
  });

  assert.equal(selected.declaredLicenseExpression, null);
  assert.deepEqual(selected.licenseEvidence, {
    metadataUrl: PYPI_METADATA_ENDPOINTS.wheel,
    field: "info.license_expression",
    value: null,
    validationPolicy: "single-spdx-license-identifier-v1",
  });
  assert.equal(selected.licenseReviewState, "pending-human-review-unclear-expression");
});

test("fetches only fixed official PyPI JSON endpoints without redirects or credentials", async () => {
  const calls = [];
  const result = await fetchPypiProjectMetadata("setuptools", async (...args) => {
    calls.push(args);
    return responseJson(metadata("setuptools"));
  });

  assert.equal(result.info.name, "setuptools");
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "https://pypi.org/pypi/setuptools/json");
  assert.equal(calls[0][1].method, "GET");
  assert.equal(calls[0][1].redirect, "error");
  assert.equal(calls[0][1].credentials, "omit");
  assert.equal(calls[0][1].headers.Authorization, undefined);
  assert.equal(calls[0][1].headers.Cookie, undefined);
  assert.ok(calls[0][1].signal instanceof AbortSignal);
});

test("accepts current-sized official metadata while keeping a finite response cap", async () => {
  const fixture = { padding: "x".repeat(1_100_000) };
  const result = await fetchPypiProjectMetadata(
    "setuptools",
    async () => responseJson(fixture),
  );

  assert.equal(result.padding.length, 1_100_000);
});

test("rejects unknown projects, redirects, wrong media type, and oversized metadata", async () => {
  let calls = 0;
  await assert.rejects(
    fetchPypiProjectMetadata("another", async () => {
      calls += 1;
      return responseJson({});
    }),
    /metadata-project-not-approved/,
  );
  assert.equal(calls, 0);

  await assert.rejects(
    fetchPypiProjectMetadata("wheel", async () => responseJson({}, { status: 302 })),
    /pypi-metadata-http-error:302/,
  );
  await assert.rejects(
    fetchPypiProjectMetadata("wheel", async () => responseJson({}, { contentType: "text/html" })),
    /pypi-metadata-type-invalid/,
  );
  await assert.rejects(
    fetchPypiProjectMetadata("wheel", async () => responseJson({}, { contentLength: 3_000_000 })),
    /pypi-metadata-too-large/,
  );
});

test("exact-version metadata hashes and binds the precise response bytes", async () => {
  const raw = `${JSON.stringify(metadata("wheel"), null, 2)}\n`;
  const calls = [];
  const record = await fetchExactPypiVersionMetadata(
    "wheel",
    "0.46.1",
    async (...args) => {
      calls.push(args);
      return new Response(raw, {
        status: 200,
        headers: {
          "content-type": "application/json",
          "content-length": String(Buffer.byteLength(raw)),
        },
      });
    },
  );

  assert.equal(calls[0][0], "https://pypi.org/pypi/wheel/0.46.1/json");
  assert.equal(calls[0][1].redirect, "error");
  assert.equal(calls[0][1].credentials, "omit");
  assert.deepEqual(record.projectMetadata, metadata("wheel"));
  assert.deepEqual(record.provenance, {
    metadataUrl: "https://pypi.org/pypi/wheel/0.46.1/json",
    responseSha256: createHash("sha256").update(raw).digest("hex"),
    responseByteLength: Buffer.byteLength(raw),
  });
});

test("changed exact-version response bytes change deterministic packet provenance", async () => {
  const records = {
    setuptools: exactMetadataRecord("setuptools"),
    wheel: await fetchExactPypiVersionMetadata(
      "wheel",
      "0.46.1",
      async () => responseJson(metadata("wheel")),
    ),
  };
  const reformattedWheel = await fetchExactPypiVersionMetadata(
    "wheel",
    "0.46.1",
    async () => new Response(`${JSON.stringify(metadata("wheel"), null, 2)}\n`, {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  const first = collectWorkerRuntimeBuildMaterials({
    metadataRecordsByProject: records,
  });
  const second = collectWorkerRuntimeBuildMaterials({
    metadataRecordsByProject: {
      ...records,
      wheel: reformattedWheel,
    },
  });

  assert.notEqual(first.packetId, second.packetId);
  assert.notEqual(
    first.materials[1].metadataEvidence.responseSha256,
    second.materials[1].metadataEvidence.responseSha256,
  );
});

test("proposal is deterministic, bound to the failed source build, and never fetches material bodies", async () => {
  const metadataCalls = [];
  let bodyCalls = 0;
  const generate = () => generateWorkerRuntimeBuildMaterialProposal({
    metadataResponseFetch: async (project, endpoint) => {
      metadataCalls.push([project, endpoint]);
      if (endpoint === PYPI_METADATA_ENDPOINTS[project]) {
        return exactMetadataRecord(project, {
          provenance: { metadataUrl: endpoint },
        });
      }
      return exactMetadataRecord(project);
    },
    materialBodyFetch: async () => {
      bodyCalls += 1;
      throw new Error("material-body-fetch-must-not-run");
    },
  });

  const first = await generate();
  const second = await generate();

  assert.equal(bodyCalls, 0);
  assert.deepEqual(metadataCalls.slice(0, 4), [
    ["setuptools", PYPI_METADATA_ENDPOINTS.setuptools],
    ["wheel", PYPI_METADATA_ENDPOINTS.wheel],
    ["setuptools", "https://pypi.org/pypi/setuptools/80.9.0/json"],
    ["wheel", "https://pypi.org/pypi/wheel/0.46.1/json"],
  ]);
  assert.equal(first.packetId, second.packetId);
  assert.match(first.packetId, /^worker-runtime-build-materials-v1-sha256-[a-f0-9]{64}$/);
  assert.equal(first.parentMaterialPacketId, PARENT_MATERIAL_PACKET_ID);
  assert.deepEqual(first.sourceArtifact, MLX_SOURCE_ARTIFACT);
  assert.deepEqual(first.buildSystem, {
    backend: "setuptools.build_meta",
    requirements: ["setuptools>=61.0", "wheel"],
  });
  assert.equal(first.approvedDependencyLockSha256, APPROVED_MLX_LOCK_SHA256);
  assert.deepEqual(first.target, {
    pythonVersion: "3.12.14",
    platform: "darwin",
    architecture: "arm64",
  });
  assert.equal(first.diagnosis, "offline-build-missing-build-system-requirements");
  assert.equal(first.approved, false);
  assert.equal(first.retrievalAuthorized, false);
  assert.equal(first.installAuthorized, false);
  assert.equal(first.modelExecutionAuthorized, false);
  assert.equal(first.materials.length, 2);
  assert.equal(first.expectedTotalDownloadBytes, 24_690);
  assert.deepEqual(first.materials[0].metadataEvidence, exactMetadataRecord("setuptools").provenance);
});

test("collection rejects missing or extra project metadata", () => {
  assert.throws(() => collectWorkerRuntimeBuildMaterials({
    metadataRecordsByProject: { setuptools: exactMetadataRecord("setuptools") },
  }), /metadata-project-set-invalid/);
  assert.throws(() => collectWorkerRuntimeBuildMaterials({
    metadataRecordsByProject: {
      setuptools: exactMetadataRecord("setuptools"),
      wheel: exactMetadataRecord("wheel"),
      pip: exactMetadataRecord("pip"),
    },
  }), /metadata-project-set-invalid/);
});

test("collection rejects missing exact-version response provenance", () => {
  assert.throws(() => collectWorkerRuntimeBuildMaterials({
    metadataRecordsByProject: {
      setuptools: metadata("setuptools"),
      wheel: metadata("wheel"),
    },
  }), /exact-metadata-provenance-missing/);
});

test("rendered proposal stays non-authorizing and names exact approve and reject commands", async () => {
  const packet = await generateWorkerRuntimeBuildMaterialProposal({
    metadataResponseFetch: async (project, endpoint) => exactMetadataRecord(project, {
      provenance: { metadataUrl: endpoint },
    }),
    materialBodyFetch: async () => {
      throw new Error("must not run");
    },
  });

  const markdown = renderBuildMaterialProposalMarkdown(packet);
  assert.match(markdown, /approved=false/);
  assert.match(markdown, /retrievalAuthorized=false/);
  assert.match(markdown, /installAuthorized=false/);
  assert.match(markdown, /modelExecutionAuthorized=false/);
  assert.match(markdown, /Python `3\.12\.14`/);
  assert.match(markdown, /single-greater-than-or-equal-release-clause-v1/);
  assert.match(markdown, /https:\/\/pypi\.org\/pypi\/setuptools\/80\.9\.0\/json/);
  assert.match(markdown, new RegExp(SHA_C));
  assert.match(markdown, new RegExp(`approve exact supplemental build material packet ${packet.packetId}`));
  assert.match(markdown, new RegExp(`reject supplemental build material packet ${packet.packetId}`));
  assert.doesNotMatch(markdown, /\/Users\//);
});
