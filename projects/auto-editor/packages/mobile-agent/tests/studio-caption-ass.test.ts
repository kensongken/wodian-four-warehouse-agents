import { describe, expect, it } from "vitest";

import { captionCuesToAss } from "../src";

describe("studio burned-in captions", () => {
  it("creates a safe two-line Chinese ASS subtitle with the selected template", () => {
    const ass = captionCuesToAss([{
      id: "caption-1",
      sourceAssetId: "asset-1",
      sourceDigest: "a".repeat(64),
      sourceStartMs: 1_000,
      sourceEndMs: 4_000,
      timelineStartMs: 1_230,
      timelineEndMs: 4_560,
      text: "现在展示自动剪辑结果｜{danger}\\override这段超长文字",
      reviewStatus: "model-suggestion",
    }], {
      template: "social-highlight",
      fontSizePx: 56,
      primaryColor: "#FFE066",
      outlineColor: "#000000",
      outlineWidthPx: 5,
      bottomMarginPx: 190,
      maxCharsPerLine: 10,
    }, "STHeiti");

    expect(ass).toContain("Style: Default,STHeiti,56,&H0066E0FF");
    expect(ass).toContain("Dialogue: 0,0:00:01.23,0:00:04.56");
    expect(ass).toContain("\\N");
    expect(ass).toContain("…");
    expect(ass).not.toContain("{danger}");
    expect(ass).not.toContain("\\override");
  });
});
