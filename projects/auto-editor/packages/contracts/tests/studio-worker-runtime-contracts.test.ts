import { describe, expect, it } from "vitest";

import {
  StudioModelArtifactRegistryV1Schema,
  StudioWorkerRuntimeBundleV1Schema,
  StudioWorkerRuntimeProbeReceiptV1Schema,
} from "../src";

const APPROVED_DEPENDENCY_LOCK_SHA256 =
  "774b30f7f04e3d78faf72f75fca0d0b5823f8244d751c6c10f9c97f45fd85521";
const APPROVED_WORKER_DEPENDENCY_LOCK_SHA256 =
  "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041";
const APPROVAL_PACKET_ID = `worker-runtime-bundle-v1-sha256-${"a".repeat(64)}`;

const lockedFile = (relativePath: string, digit: string) => ({
  relativePath,
  sha256: digit.repeat(64),
  byteLength: 128,
});

const workerRuntimeFixture = (overrides: Record<string, unknown> = {}) => ({
  schemaVersion: 1 as const,
  id: "synthetic-worker-runtime",
  version: "0.6.6-py312",
  pythonVersion: "3.12.13",
  platform: "darwin" as const,
  architecture: "arm64" as const,
  minimumOsVersion: "14.0",
  dependencyLockSha256: APPROVED_DEPENDENCY_LOCK_SHA256,
  workerDependencyLockSha256: APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
  materialInventorySha256: "1".repeat(64),
  approvalPacketId: APPROVAL_PACKET_ID,
  archive: lockedFile("runtime.tar", "1"),
  manifest: lockedFile("runtime-manifest.json", "2"),
  packageInventory: lockedFile("runtime-packages.json", "3"),
  derivationReceipt: lockedFile("derivation-receipt.json", "4"),
  treeSha256: "5".repeat(64),
  licenseReview: "approved" as const,
  enabled: true,
  approved: true,
  approvedBy: "local-runtime-owner",
  approvedAt: "2026-09-23T01:00:00.000Z",
  probe: null,
  ...overrides,
});

const probeReceiptFixture = (overrides: Record<string, unknown> = {}) => ({
  schemaVersion: 1 as const,
  runtime: {
    id: "synthetic-worker-runtime",
    version: "0.6.6-py312",
    treeSha256: "5".repeat(64),
  },
  model: {
    id: "qwen3-vl-2b-instruct-4bit",
    version: "2b-4bit",
    sha256: "7".repeat(64),
  },
  promptVersion: "studio-vision-zh-v2",
  inputSha256: "8".repeat(64),
  outputSha256: "9".repeat(64),
  observationCount: 1,
  observationKinds: ["visible-product"],
  runtimeMs: 1_250,
  maximumResidentSetBytes: 32_000_000,
  syntheticInput: true,
  customerContentUsed: false,
  realModel: true,
  formalWorkerPath: true,
  networkDenied: true,
  providerMode: "local" as const,
  publishedClaimed: false,
  ...overrides,
});

describe("studio worker runtime contracts", () => {
  it("accepts the exact approved dependency lock and keeps worker runtimes optional", () => {
    const approved = StudioWorkerRuntimeBundleV1Schema.parse(workerRuntimeFixture());
    expect(approved.id).toBe("synthetic-worker-runtime");

    expect(StudioModelArtifactRegistryV1Schema.parse({
      schemaVersion: 1,
      artifacts: [],
    }).workerRuntimes).toBeUndefined();
    expect(StudioModelArtifactRegistryV1Schema.parse({
      schemaVersion: 1,
      artifacts: [],
      workerRuntimes: [workerRuntimeFixture()],
    }).workerRuntimes).toHaveLength(1);
  });

  it("fails closed when the approved dependency lock digest changes", () => {
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(workerRuntimeFixture({
      dependencyLockSha256: "8".repeat(64),
    }))).toThrow("worker-runtime-dependency-lock-not-approved");
  });

  it("requires the exact repository Worker dependency lock independently", () => {
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(workerRuntimeFixture({
      workerDependencyLockSha256: APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
    }))).not.toThrow();

    const missingWorkerLock = workerRuntimeFixture();
    delete (missingWorkerLock as { workerDependencyLockSha256?: string })
      .workerDependencyLockSha256;
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(missingWorkerLock)).toThrow();
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(workerRuntimeFixture({
      workerDependencyLockSha256: "8".repeat(64),
    }))).toThrow("worker-runtime-worker-dependency-lock-not-approved");
  });

  it("rejects unknown fields, duplicate sidecar paths, and missing inventory identity", () => {
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(workerRuntimeFixture({
      unexpected: true,
    }))).toThrow();

    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(workerRuntimeFixture({
      packageInventory: lockedFile("runtime-manifest.json", "3"),
    }))).toThrow("duplicate-worker-runtime-sidecar-path");

    const missingInventory = workerRuntimeFixture();
    delete (missingInventory as { materialInventorySha256?: string }).materialInventorySha256;
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(missingInventory)).toThrow();
  });

  it("requires the exact approved bundle packet identity", () => {
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(workerRuntimeFixture({
      approvalPacketId: "worker-runtime-bundle-v1-sha256-short",
    }))).toThrow();

    const missingApprovalPacket = workerRuntimeFixture();
    delete (missingApprovalPacket as { approvalPacketId?: string }).approvalPacketId;
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(missingApprovalPacket)).toThrow();
  });

  it("accepts only darwin arm64 and Python 3.12 patch releases", () => {
    for (const [overrides, expectedMessage] of [
      [{ platform: "linux" }, "Invalid literal value"],
      [{ architecture: "x64" }, "Invalid literal value"],
      [{ pythonVersion: "3.11.9" }, "Invalid"],
      [{ pythonVersion: "3.13.0" }, "Invalid"],
      [{ pythonVersion: "3.12" }, "Invalid"],
    ] as const) {
      expect(() => StudioWorkerRuntimeBundleV1Schema.parse(
        workerRuntimeFixture(overrides),
      )).toThrow(expectedMessage);
    }
  });

  it("rejects enablement without approval and incomplete approval records", () => {
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(workerRuntimeFixture({
      approved: false,
      enabled: true,
      approvedBy: null,
      approvedAt: null,
    }))).toThrow("enabled-worker-runtime-must-be-approved");

    for (const overrides of [
      { approvedBy: null },
      { approvedAt: null },
      { licenseReview: "pending" },
    ]) {
      expect(() => StudioWorkerRuntimeBundleV1Schema.parse(
        workerRuntimeFixture(overrides),
      )).toThrow();
    }
  });

  it("binds a probe to the exact runtime tree and accepts only sanitized real-probe receipts", () => {
    const probe = {
      receiptPath: "runtime/probes/synthetic-worker-runtime.json",
      receiptSha256: "a".repeat(64),
      runtimeTreeSha256: "5".repeat(64),
      modelSha256: "7".repeat(64),
      promptVersion: "studio-vision-zh-v2",
      inputSha256: "8".repeat(64),
    };
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(
      workerRuntimeFixture({ probe }),
    )).not.toThrow();
    expect(() => StudioWorkerRuntimeBundleV1Schema.parse(workerRuntimeFixture({
      probe: { ...probe, runtimeTreeSha256: "b".repeat(64) },
    }))).toThrow("worker-runtime-probe-tree-binding-mismatch");

    expect(StudioWorkerRuntimeProbeReceiptV1Schema.parse(probeReceiptFixture()).realModel)
      .toBe(true);
    expect(() => StudioWorkerRuntimeProbeReceiptV1Schema.parse(probeReceiptFixture({
      sourcePath: "/private/customer/video.mov",
    }))).toThrow();
    expect(() => StudioWorkerRuntimeProbeReceiptV1Schema.parse(probeReceiptFixture({
      customerContentUsed: true,
    }))).toThrow();
  });

  it("rejects duplicate runtime IDs in the top-level model registry", () => {
    expect(() => StudioModelArtifactRegistryV1Schema.parse({
      schemaVersion: 1,
      artifacts: [],
      workerRuntimes: [workerRuntimeFixture(), workerRuntimeFixture()],
    })).toThrow("duplicate-worker-runtime-id");
  });
});
