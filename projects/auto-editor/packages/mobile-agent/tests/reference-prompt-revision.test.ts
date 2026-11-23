import { ReferencePromptRevisionSchema } from "@wearable/contracts";
import { describe, expect, it } from "vitest";

import { interpretReferencePromptRevision } from "../src/reference-prompt-revision";

describe("reference prompt revision", () => {
  it("turns the bounded Chinese command into a frame-aligned opening trim", () => {
    expect(interpretReferencePromptRevision({
      prompt: "开头缩短 1.23 秒",
      target: "xiaohongshu",
    })).toEqual({
      kind: "shorten-opening",
      target: "xiaohongshu",
      requestedSeconds: 1.23,
      trimMs: 1_240,
    });
  });

  it.each([
    "换一个更酷的开头",
    "开头缩短 0.4 秒",
    "开头缩短 5.1 秒",
    "开头缩短 NaN 秒",
  ])("fails closed for unsupported or out-of-bound prompt: %s", (prompt) => {
    expect(() => interpretReferencePromptRevision({
      prompt,
      target: "editorial-master",
    })).toThrow("reference-revision-prompt-unsupported");
  });

  it("rejects a forged trim amount that does not match the interpreted seconds", () => {
    expect(ReferencePromptRevisionSchema.safeParse({
      kind: "shorten-opening",
      target: "xiaohongshu",
      requestedSeconds: 1,
      trimMs: 5_000,
    }).success).toBe(false);
  });
});
