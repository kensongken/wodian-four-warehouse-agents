import { readdir, rm } from "node:fs/promises";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import type { DraftPackage } from "@wearable/contracts";

import { PlatformAutomaticEditorialAdapter, type PrivateContentStudio } from "../src";
import { prepareRenderableStudio, studioScope } from "./fixtures/private-studio-fixtures";

describe("studio caption revision gate", () => {
  const cleanup: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("removes a failed first render and does not expose it as a reviewable draft", async () => {
    const { root, studio, brief } = await prepareRenderableStudio({
      editorial: new PlatformAutomaticEditorialAdapter(),
      resolveEditorialTranscriptSpans: async (cards) => cards.flatMap((card) => [
        {
          sourceAssetId: card.sourceAssetId,
          sourceDigest: card.sourceDigest,
          startMs: card.sourceStartMs + 1_000,
          endMs: card.sourceStartMs + 3_000,
          text: "第一句本地测试字幕",
        },
        {
          sourceAssetId: card.sourceAssetId,
          sourceDigest: card.sourceDigest,
          startMs: card.sourceStartMs + 2_500,
          endMs: card.sourceStartMs + 4_000,
          text: "第二句与第一句时间重叠",
        },
      ]),
    });
    cleanup.push(root);
    await expect(studio.createDrafts(studioScope, brief.id, "caption-overlap-parent"))
      .rejects.toThrow("draft-quality-check-failed");

    expect(await listDraftFiles(join(root, "drafts"))).toEqual([]);
    const history = await studio.listStudioHistory(studioScope);
    expect(history.drafts).toEqual([]);
  }, 120_000);

  it("removes a failed revision render and does not expose a legacy invalid timeline", async () => {
    const { root, studio, brief } = await prepareRenderableStudio({
      editorial: new PlatformAutomaticEditorialAdapter(),
      resolveEditorialTranscriptSpans: async (cards) => cards.map((card) => ({
        sourceAssetId: card.sourceAssetId,
        sourceDigest: card.sourceDigest,
        startMs: card.sourceStartMs + 1_000,
        endMs: card.sourceStartMs + 3_000,
        text: "一条时间合法的本地测试字幕",
      })),
    });
    cleanup.push(root);
    const drafts = await studio.createDrafts(studioScope, brief.id, "caption-valid-parent");
    const parent = drafts.find((draft) => (draft.captionCues?.length ?? 0) > 0)!;
    injectLegacyOverlappingCue(studio, parent);
    const legacyParent = await studio.getDraft(studioScope, parent.id);
    const filesBefore = await listDraftFiles(join(root, "drafts"));
    const historyBefore = await studio.listStudioHistory(studioScope);

    await expect(studio.reviseDraftCaptions(studioScope, legacyParent.id, {
      captionTexts: legacyParent.captionCues!.map((cue, index) => ({
        id: cue.id,
        text: index === 0 ? "第一句经过人工修正" : cue.text,
      })),
      style: legacyParent.captionStyle!,
      operationKey: "caption-overlap-revision",
    })).rejects.toThrow("caption-revision-quality-check-failed");

    expect(await listDraftFiles(join(root, "drafts"))).toEqual(filesBefore);
    const historyAfter = await studio.listStudioHistory(studioScope);
    expect(historyAfter.drafts.map((draft) => draft.id))
      .toEqual(historyBefore.drafts.map((draft) => draft.id));
    expect(historyAfter.drafts.some((draft) => draft.captionRevision !== undefined)).toBe(false);
  }, 120_000);
});

function injectLegacyOverlappingCue(studio: PrivateContentStudio, parent: DraftPackage): void {
  const firstCue = parent.captionCues![0]!;
  const overlappingCue = {
    ...firstCue,
    id: `${firstCue.id}-legacy-overlap`,
    sourceStartMs: firstCue.sourceStartMs + 500,
    sourceEndMs: firstCue.sourceEndMs + 500,
    timelineStartMs: firstCue.timelineStartMs + 500,
    timelineEndMs: firstCue.timelineEndMs + 500,
    text: "旧版本遗留的重叠字幕",
  };
  const internal = studio as unknown as { drafts: Map<string, DraftPackage> };
  internal.drafts.set(parent.id, {
    ...parent,
    captionCues: [firstCue, overlappingCue, ...parent.captionCues!.slice(1)],
  });
}

async function listDraftFiles(root: string): Promise<string[]> {
  return (await readdir(root, { recursive: true }))
    .map(String)
    .filter((path) => path.endsWith(".mp4"))
    .sort();
}
