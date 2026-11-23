import { describe, expect, it } from "vitest";

import {
  RealMaterialCapabilityReportSchema,
  RealMaterialEvidenceScopeSchema,
  RealMaterialRunDecisionSchema,
  RealMaterialRunManifestSchema,
  RealMaterialRunReportSchema,
  RealMaterialStoryReadinessSchema,
} from "../src";

const DIGEST_A = "a".repeat(64);
const DIGEST_B = "b".repeat(64);
const NOW = "2026-09-22T00:00:00.000Z";

const validManifest = () => ({
  schemaVersion: 1 as const,
  runId: "real_run_01",
  customerId: "customer_01",
  actorId: "designer_01",
  createdAt: NOW,
  purpose: "real-material-acceptance" as const,
  inputs: [{
    inputId: "current_01",
    role: "current" as const,
    mediaKind: "video" as const,
    sourcePath: "/approved/current.mp4",
    sourceDigest: DIGEST_A,
    permissionState: "allowed" as const,
    allowedUses: ["story-analysis", "render"] as const,
    declaredOwnership: "customer-owned" as const,
    confirmedFacts: [{
      kind: "product-id" as const,
      value: "product_01",
      sourceStartMs: 0,
      sourceEndMs: 10_000,
      confirmedBy: "designer_01",
      confirmedAt: NOW,
    }],
  }],
  referencePolicy: "external-style-only" as const,
  requestedStoryTypes: ["product-journey"] as const,
  requestedVariants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const,
});

const validCapability = () => ({
  schemaVersion: 1 as const,
  runId: "real_run_01",
  generatedAt: NOW,
  layers: [
    { layer: "media", status: "ready", evidenceCodes: ["ffmpeg-probed"], message: "media tools ready" },
    { layer: "scene", status: "degraded", evidenceCodes: [], message: "scene detector unavailable" },
    { layer: "transcript", status: "blocked", evidenceCodes: [], message: "approved model unavailable" },
    { layer: "vision", status: "blocked", evidenceCodes: [], message: "approved model unavailable" },
    { layer: "embedding", status: "blocked", evidenceCodes: [], message: "approved model unavailable" },
  ],
});

const validReadiness = () => ({
  storyType: "product-journey" as const,
  status: "ready" as const,
  evidence: ["product_01:design", "product_01:finished"],
  missingEvidence: [],
  sourceIds: ["current_01"],
  errorCode: null,
  message: "two confirmed process stages are available",
});

const validAwaitingReport = () => ({
  schemaVersion: 1 as const,
  runId: "real_run_01",
  customerId: "customer_01",
  status: "awaiting-decision" as const,
  evidenceScope: "local-real-media" as const,
  generatedAt: NOW,
  reviewPacketDigest: DIGEST_B,
  decisionId: null,
  inputReceipts: [{
    inputId: "current_01",
    role: "current" as const,
    mediaKind: "video" as const,
    digest: DIGEST_A,
  }],
  capability: validCapability(),
  storyResults: [validReadiness()],
  referenceRuleResults: [],
  outputs: [],
  publishedClaimed: false as const,
});

describe("real material acceptance contracts", () => {
  it("accepts one strict versioned real-material manifest", () => {
    const manifest = RealMaterialRunManifestSchema.parse(validManifest());

    expect(manifest.inputs[0]!.sourcePath).toBe("/approved/current.mp4");
    expect(manifest.requestedVariants).toEqual([
      "editorial-master",
      "xiaohongshu",
      "douyin",
      "wechat-channels",
    ]);
  });

  it("rejects duplicate input IDs and incomplete variant sets", () => {
    const manifest = validManifest();
    expect(() => RealMaterialRunManifestSchema.parse({
      ...manifest,
      inputs: [manifest.inputs[0], { ...manifest.inputs[0] }],
    })).toThrow("duplicate-real-material-input-id");

    expect(() => RealMaterialRunManifestSchema.parse({
      ...manifest,
      requestedVariants: ["editorial-master", "xiaohongshu"],
    })).toThrow("four-real-material-variants-required");
  });

  it("rejects non-allowed permissions and inconsistent reference declarations", () => {
    const manifest = validManifest();
    expect(() => RealMaterialRunManifestSchema.parse({
      ...manifest,
      inputs: [{ ...manifest.inputs[0], permissionState: "review-required" }],
    })).toThrow();

    expect(() => RealMaterialRunManifestSchema.parse({
      ...manifest,
      inputs: [{
        ...manifest.inputs[0],
        inputId: "reference_01",
        role: "reference",
        declaredOwnership: "customer-owned",
        allowedUses: ["reference-analysis"],
        confirmedFacts: [],
      }],
    })).toThrow("reference-policy-ownership-mismatch");
  });

  it.each(["first-person-choice", "emotion", "opinion"] as const)(
    "rejects %s as a confirmed fact on an image",
    (kind) => {
      const manifest = validManifest();
      expect(() => RealMaterialRunManifestSchema.parse({
        ...manifest,
        inputs: [{
          ...manifest.inputs[0],
          mediaKind: "image",
          sourcePath: "/approved/current.jpg",
          confirmedFacts: [{
            kind,
            value: "unsupported image claim",
            sourceStartMs: null,
            sourceEndMs: null,
            confirmedBy: "designer_01",
            confirmedAt: NOW,
          }],
        }],
      })).toThrow("image-cannot-confirm-spoken-or-emotional-fact");
    },
  );

  it("requires image bounds to be null and timed video facts to have increasing bounds", () => {
    const manifest = validManifest();
    expect(() => RealMaterialRunManifestSchema.parse({
      ...manifest,
      inputs: [{ ...manifest.inputs[0], confirmedFacts: [{
        ...manifest.inputs[0]!.confirmedFacts[0],
        sourceStartMs: 10_000,
        sourceEndMs: 10_000,
      }] }],
    })).toThrow("confirmed-fact-end-must-follow-start");

    expect(() => RealMaterialRunManifestSchema.parse({
      ...manifest,
      inputs: [{
        ...manifest.inputs[0],
        mediaKind: "image",
        sourcePath: "/approved/current.jpg",
        confirmedFacts: [{
          ...manifest.inputs[0]!.confirmedFacts[0],
          sourceStartMs: 0,
          sourceEndMs: 1,
        }],
      }],
    })).toThrow("image-confirmed-fact-bounds-must-be-null");
  });

  it("binds a designer decision to one review packet and unique selections", () => {
    const decision = {
      schemaVersion: 1 as const,
      decisionId: "decision_01",
      runId: "real_run_01",
      reviewPacketDigest: DIGEST_A,
      selectedStoryTypes: ["product-journey"] as const,
      selectedReferenceRuleIds: ["rule_01"],
      mappingPlan: { id: "mapping_01", version: 1, digest: DIGEST_B },
      confirmedBy: "designer_01",
      confirmedAt: NOW,
      operationKey: "render_real_run_01_v1",
    };

    expect(RealMaterialRunDecisionSchema.parse(decision).mappingPlan.version).toBe(1);
    expect(() => RealMaterialRunDecisionSchema.parse({
      ...decision,
      selectedStoryTypes: ["product-journey", "product-journey"],
    })).toThrow("duplicate-selected-story-type");
    expect(() => RealMaterialRunDecisionSchema.parse({
      ...decision,
      selectedReferenceRuleIds: ["rule_01", "rule_01"],
    })).toThrow("duplicate-selected-reference-rule-id");
    expect(() => RealMaterialRunDecisionSchema.parse({ ...decision, reviewPacketDigest: "short" })).toThrow();
    expect(() => RealMaterialRunDecisionSchema.parse({
      ...decision,
      mappingPlan: { ...decision.mappingPlan, version: 0 },
    })).toThrow();
  });

  it("accepts only complete unique capability layers and stable readiness states", () => {
    expect(RealMaterialCapabilityReportSchema.parse(validCapability()).layers).toHaveLength(5);
    expect(() => RealMaterialCapabilityReportSchema.parse({
      ...validCapability(),
      layers: validCapability().layers.slice(0, 4),
    })).toThrow("five-real-material-capability-layers-required");

    expect(RealMaterialStoryReadinessSchema.parse(validReadiness()).status).toBe("ready");
    expect(() => RealMaterialStoryReadinessSchema.parse({
      ...validReadiness(),
      status: "guessing",
    })).toThrow();
    expect(() => RealMaterialStoryReadinessSchema.parse({
      ...validReadiness(),
      status: "insufficient-evidence",
      errorCode: "free-form-error",
    })).toThrow();
  });

  it("keeps public reports path-free, strict, unpublished, and scope-explicit", () => {
    const report = RealMaterialRunReportSchema.parse(validAwaitingReport());

    expect(report.inputReceipts[0]).toEqual({
      inputId: "current_01",
      role: "current",
      mediaKind: "video",
      digest: DIGEST_A,
    });
    expect("sourcePath" in report.inputReceipts[0]!).toBe(false);
    expect(report.publishedClaimed).toBe(false);
    expect(RealMaterialEvidenceScopeSchema.parse("local-synthetic-test")).toBe("local-synthetic-test");
    expect(() => RealMaterialRunReportSchema.parse({
      ...validAwaitingReport(),
      sourcePath: "/private/customer.mp4",
    })).toThrow();
    expect(() => RealMaterialRunReportSchema.parse({
      ...validAwaitingReport(),
      inputReceipts: [{ ...validAwaitingReport().inputReceipts[0], sourcePath: "/private/customer.mp4" }],
    })).toThrow();
    expect(() => RealMaterialRunReportSchema.parse({
      ...validAwaitingReport(),
      publishedClaimed: true,
    })).toThrow();
  });
});
