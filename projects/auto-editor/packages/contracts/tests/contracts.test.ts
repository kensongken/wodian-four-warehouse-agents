import { describe, expect, it } from "vitest";
import { CaptureSessionSchema } from "../src/index";

describe("capture session contract", () => {
  it("requires confirmed consent for visitor recording", () => {
    expect(() => CaptureSessionSchema.parse({
      id: "cap_001", startedAt: "2026-09-03T09:00:00.000Z",
      mode: "creator-necklace", privacyState: "visitor",
      visitorConsent: "confirmed", status: "recording", productRef: null,
    })).not.toThrow();
  });

  it("rejects visitor recording without confirmed consent", () => {
    expect(() => CaptureSessionSchema.parse({
      id: "cap_002", startedAt: "2026-09-03T09:00:00.000Z",
      mode: "daily-pendant", privacyState: "visitor",
      visitorConsent: "declined", status: "recording", productRef: null,
    })).toThrow("visitor mode requires confirmed consent");
  });

  it("rejects visitor recording when consent is omitted", () => {
    expect(() => CaptureSessionSchema.parse({
      id: "cap_004", startedAt: "2026-09-03T09:00:00.000Z",
      mode: "daily-pendant", privacyState: "visitor",
      status: "recording", productRef: null,
    })).toThrow("visitor mode requires confirmed consent");
  });

  it("rejects recording while privacy is paused", () => {
    expect(() => CaptureSessionSchema.parse({
      id: "cap_003", startedAt: "2026-09-03T09:00:00.000Z",
      mode: "maker-brooch", privacyState: "paused",
      status: "recording", productRef: null,
    })).toThrow("paused mode cannot record");
  });
});
