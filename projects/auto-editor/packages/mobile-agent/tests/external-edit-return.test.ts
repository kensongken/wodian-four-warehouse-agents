import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  ExternalEditHandoffService,
  ExternalEditInbox,
  ExternalEditReturnService,
} from "../src";
import { prepareRenderableStudio, studioScope } from "./fixtures/private-studio-fixtures";

const digest = (bytes: Buffer) => createHash("sha256").update(bytes).digest("hex");
// These cases prepare real playable drafts before checking the return gate.
// A busy local Mac can spend over one minute in ffmpeg, so the timeout must
// cover media work without weakening any of the confirmation assertions.
const realVideoTestTimeoutMs = 300_000;

describe("external edit return service", () => {
  let fixture: Awaited<ReturnType<typeof prepareRenderableStudio>> | null = null;
  let root: string | null = null;

  afterEach(async () => {
    if (fixture) await rm(fixture.root, { recursive: true, force: true });
    if (root) await rm(root, { recursive: true, force: true });
    fixture = null;
    root = null;
  }, realVideoTestTimeoutMs);

  it("creates no V2 before explicit confirmation, then keeps V1 immutable and accepts only one candidate", async () => {
    fixture = await prepareRenderableStudio();
    root = await mkdtemp(join(tmpdir(), "external-edit-return-"));
    const parentAndAlternative = await fixture.studio.createDrafts(studioScope, fixture.brief.id, "return-test-drafts");
    const parent = parentAndAlternative[0]!;
    const alternative = parentAndAlternative[1]!;
    const originalParentBytes = await fixture.studio.readDraftOutput(studioScope, parent.id);
    const originalParentDigest = parent.outputDigest;
    const handoffs = new ExternalEditHandoffService({
      root: join(root, "handoffs"),
      now: () => "2026-09-30T08:00:00.000Z",
      resolveParentDraft: async (scope, draftId) => {
        try { return await fixture!.studio.getDraft(scope, draftId); } catch { return null; }
      },
    });
    const handoff = await handoffs.create(studioScope, {
      parentDraftId: parent.id,
      parentDraftVersion: parent.version,
      parentDraftDigest: parent.outputDigest,
      platformVariant: "wechat-channels",
      revisionInstruction: "保留打磨镜头，开头节奏更快。",
      mustKeep: ["打磨镜头"],
      includeSources: false,
      sourceAssetBindings: [],
      operationKey: "return-handoff-create",
    });
    await handoffs.transition(studioScope, {
      handoffId: handoff.id, status: "ready", operationKey: "return-handoff-ready", packageDigest: "d".repeat(64),
    });
    await handoffs.transition(studioScope, {
      handoffId: handoff.id, status: "editing", operationKey: "return-handoff-editing",
    });

    let currentTime = Date.parse("2026-09-30T08:00:00.000Z");
    const inbox = new ExternalEditInbox({
      root: join(root, "inbox"),
      stableForMs: 0,
      now: () => new Date(currentTime).toISOString(),
      probeVideo: async () => ({ durationSeconds: alternative.durationSeconds, width: 1080, height: 1920 }),
    });
    const inboxDirectory = await inbox.createReturnDirectory(handoff);
    const firstCandidateBytes = await fixture.studio.readDraftOutput(studioScope, alternative.id);
    const secondCandidateBytes = Buffer.from(firstCandidateBytes).map((byte, index) => index < 24 ? byte ^ 1 : byte);
    await writeFile(join(inboxDirectory, "candidate-one.mp4"), firstCandidateBytes);
    await writeFile(join(inboxDirectory, "candidate-two.mp4"), secondCandidateBytes);

    const returns = new ExternalEditReturnService({
      root: join(root, "accepted"),
      inbox,
      handoffs,
      studio: fixture.studio,
      now: () => new Date(currentTime).toISOString(),
    });
    const candidates = await returns.listCandidates(studioScope, handoff.id);
    expect(candidates).toHaveLength(2);
    const beforeConfirmation = await fixture.studio.listStudioHistory(studioScope);
    expect(beforeConfirmation.drafts).toHaveLength(2);

    const chosen = candidates.find((candidate) => candidate.digest === digest(firstCandidateBytes))!;
    const created = await returns.accept(studioScope, handoff.id, chosen.id, "confirm-return-1");
    const retry = await returns.accept(studioScope, handoff.id, chosen.id, "confirm-return-1");
    const afterConfirmation = await fixture.studio.listStudioHistory(studioScope);
    const persistedParent = await fixture.studio.getDraft(studioScope, parent.id);

    expect(created).toMatchObject({
      id: retry.id,
      version: parent.version + 1,
      status: "reviewable",
      outputDigest: digest(firstCandidateBytes),
      externalEdit: {
        origin: "external-editor-return",
        externalEditor: "unverified-external-file",
        declaredEditor: "jianying-desktop",
        editorProvenance: "return-folder-only",
        parentDraftId: parent.id,
        handoffId: handoff.id,
        lineageConfidence: "parent-only",
      },
      publishClaimed: false,
    });
    expect(afterConfirmation.drafts).toHaveLength(3);
    expect(persistedParent.outputDigest).toBe(originalParentDigest);
    expect(await fixture.studio.readDraftOutput(studioScope, parent.id)).toEqual(originalParentBytes);
    await expect(returns.accept(studioScope, handoff.id, candidates.find((item) => item.id !== chosen.id)!.id, "confirm-return-2"))
      .rejects.toThrow("external-edit-already-returned");
    expect(await readFile(created.outputPath)).toEqual(firstCandidateBytes);
    expect(await handoffs.get(studioScope, handoff.id)).toMatchObject({ status: "returned", publishClaimed: false });
  }, realVideoTestTimeoutMs);

  it("does not create a draft when the candidate has not been explicitly selected", async () => {
    fixture = await prepareRenderableStudio();
    root = await mkdtemp(join(tmpdir(), "external-edit-return-pending-"));
    const drafts = await fixture.studio.createDrafts(studioScope, fixture.brief.id, "pending-test-drafts");
    const handoffs = new ExternalEditHandoffService({
      root: join(root, "handoffs"),
      resolveParentDraft: async (scope, id) => {
        try { return await fixture!.studio.getDraft(scope, id); } catch { return null; }
      },
    });
    const handoff = await handoffs.create(studioScope, {
      parentDraftId: drafts[0]!.id, parentDraftVersion: drafts[0]!.version, parentDraftDigest: drafts[0]!.outputDigest,
      platformVariant: "xiaohongshu", revisionInstruction: "节奏快一点。", mustKeep: [],
      includeSources: false, sourceAssetBindings: [], operationKey: "pending-handoff-create",
    });
    await handoffs.transition(studioScope, {
      handoffId: handoff.id, status: "ready", operationKey: "pending-handoff-ready", packageDigest: "d".repeat(64),
    });
    await handoffs.transition(studioScope, {
      handoffId: handoff.id, status: "editing", operationKey: "pending-handoff-editing",
    });
    const inbox = new ExternalEditInbox({
      root: join(root, "inbox"), stableForMs: 0,
      probeVideo: async () => ({ durationSeconds: 35, width: 1080, height: 1920 }),
    });
    const inboxDirectory = await inbox.createReturnDirectory(handoff);
    await mkdir(inboxDirectory, { recursive: true });
    await writeFile(join(inboxDirectory, "candidate.mp4"), await fixture.studio.readDraftOutput(studioScope, drafts[1]!.id));
    const returns = new ExternalEditReturnService({ root: join(root, "accepted"), inbox, handoffs, studio: fixture.studio });
    expect(await returns.listCandidates(studioScope, handoff.id)).toHaveLength(1);
    expect((await fixture.studio.listStudioHistory(studioScope)).drafts).toHaveLength(2);
  }, realVideoTestTimeoutMs);

  it("records an ignored candidate durably without deleting its exported file", async () => {
    fixture = await prepareRenderableStudio();
    root = await mkdtemp(join(tmpdir(), "external-edit-return-ignore-"));
    const drafts = await fixture.studio.createDrafts(studioScope, fixture.brief.id, "ignore-test-drafts");
    const handoffs = new ExternalEditHandoffService({
      root: join(root, "handoffs"),
      resolveParentDraft: async (scope, id) => {
        try { return await fixture!.studio.getDraft(scope, id); } catch { return null; }
      },
    });
    const handoff = await handoffs.create(studioScope, {
      parentDraftId: drafts[0]!.id, parentDraftVersion: drafts[0]!.version, parentDraftDigest: drafts[0]!.outputDigest,
      platformVariant: "xiaohongshu", revisionInstruction: "节奏快一点。", mustKeep: [],
      includeSources: false, sourceAssetBindings: [], operationKey: "ignore-handoff-create",
    });
    await handoffs.transition(studioScope, {
      handoffId: handoff.id, status: "degraded", operationKey: "ignore-handoff-degraded", packageDigest: "d".repeat(64),
    });
    const inbox = new ExternalEditInbox({
      root: join(root, "inbox"), stableForMs: 0,
      probeVideo: async () => ({ durationSeconds: 35, width: 1080, height: 1920 }),
    });
    const inboxDirectory = await inbox.createReturnDirectory(handoff);
    const candidateBytes = await fixture.studio.readDraftOutput(studioScope, drafts[1]!.id);
    const candidatePath = join(inboxDirectory, "candidate.mp4");
    await writeFile(candidatePath, candidateBytes);
    const receiptRoot = join(root, "accepted");
    const returns = new ExternalEditReturnService({ root: receiptRoot, inbox, handoffs, studio: fixture.studio });
    const [candidate] = await returns.listCandidates(studioScope, handoff.id);
    await returns.ignore(studioScope, handoff.id, candidate!.id, "ignore-candidate-once");
    expect((await returns.listCandidates(studioScope, handoff.id))[0]?.status).toBe("ignored");

    const restartedReturns = new ExternalEditReturnService({ root: receiptRoot, inbox, handoffs, studio: fixture.studio });
    expect((await restartedReturns.listCandidates(studioScope, handoff.id))[0]?.status).toBe("ignored");
    await expect(restartedReturns.accept(studioScope, handoff.id, candidate!.id, "accept-ignored-candidate"))
      .rejects.toThrow("external-edit-candidate-ignored");
    expect(await readFile(candidatePath)).toEqual(candidateBytes);
  }, realVideoTestTimeoutMs);
});
