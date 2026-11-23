import { readdir, rm, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { prepareRenderableStudio, studioScope } from "./fixtures/private-studio-fixtures";
import {
  StudioPreferenceRanker,
  type RecordCustomerFeedbackInput,
  type StudioPreferenceCandidate,
} from "../src";

describe("private studio immutable feedback", () => {
  const cleanup: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("reconciles three real rejected drafts from distinct source sets after a derived write failure", async () => {
    const { root, library, studio, assets, brief, demand, assessment } = await prepareRenderableStudio({
      withPreferenceRanker: true,
    });
    cleanup.push(root);
    const firstDrafts = await studio.createDrafts(studioScope, brief.id, "preference-drafts-v1");
    const secondBrief = await studio.confirmBrief(studioScope, {
      opportunityId: brief.opportunityId,
      matchAssessmentId: assessment.id,
      selectedAssetIds: [assets[0]!.id, assets[2]!.id, assets[3]!.id, assets[1]!.id],
      confirmedFactIds: ["fact-material-18k: 材质为 18K 金"],
      reviewRequirements: ["采用前人工复核"],
      operationKey: "preference-brief-v2",
    });
    const secondDrafts = await studio.createDrafts(studioScope, secondBrief.id, "preference-drafts-v2");
    const drafts = [firstDrafts[0]!, firstDrafts[1]!, secondDrafts[0]!];

    for (const [index, draft] of drafts.slice(0, 2).entries()) {
      await studio.recordFeedback(studioScope, {
        draftId: draft.id,
        action: "reject",
        reason: "explicit human rejection",
        operationKey: `preference-reject-${index}`,
      });
    }
    const baseCandidates: StudioPreferenceCandidate[] = (await studio.rankMaterials(
      studioScope,
      demand.id,
    )).map(({ preferenceAdjustment: _adjustment, score: _score, reasons: _reasons,
      preferencePolicyVersion: _policy, ...candidate }) => candidate);
    const failedDerivedWrite = vi.spyOn(library, "writeStudioPreferenceState")
      .mockRejectedValueOnce(new Error("injected-preference-write-failure"));
    await expect(studio.recordFeedback(studioScope, {
      draftId: drafts[2]!.id,
      action: "reject",
      reason: "explicit human rejection",
      operationKey: "preference-reject-2",
    })).rejects.toThrow("injected-preference-write-failure");
    failedDerivedWrite.mockRestore();

    await expect(studio.listFeedback(studioScope, drafts[2]!.id)).resolves.toHaveLength(1);
    const restarted = new StudioPreferenceRanker(library, {
      now: () => "2026-09-10T08:00:00.000Z",
    });
    await restarted.reconcilePersistedDraftFeedback(studioScope);
    const ranked = await restarted.rank(studioScope, baseCandidates, demand);
    expect(ranked).toHaveLength(4);
    expect(ranked.every((row) => row.eligible)).toBe(true);
    expect(ranked.every((row) => row.preferenceAdjustment === -0.05)).toBe(true);
    expect(ranked.every((row) =>
      row.reasons.includes("rejected-preference:draft-adoption:single-product"))).toBe(true);
  }, 300_000);

  it("does not retain draft feedback when its canonical atomic commit fails", async () => {
    const { root, library, studio, assets, brief, demand, assessment } = await prepareRenderableStudio({
      withPreferenceRanker: true,
    });
    cleanup.push(root);
    const firstDrafts = await studio.createDrafts(studioScope, brief.id, "canonical-failure-drafts-v1");
    const secondBrief = await studio.confirmBrief(studioScope, {
      opportunityId: brief.opportunityId,
      matchAssessmentId: assessment.id,
      selectedAssetIds: [assets[0]!.id, assets[2]!.id, assets[3]!.id, assets[1]!.id],
      confirmedFactIds: ["fact-material-18k: 材质为 18K 金"],
      reviewRequirements: ["采用前人工复核"],
      operationKey: "canonical-failure-brief-v2",
    });
    const secondDrafts = await studio.createDrafts(
      studioScope,
      secondBrief.id,
      "canonical-failure-drafts-v2",
    );
    const drafts = [firstDrafts[0]!, firstDrafts[1]!, secondDrafts[0]!];
    for (const [index, draft] of drafts.slice(0, 2).entries()) {
      await studio.recordFeedback(studioScope, {
        draftId: draft.id,
        action: "reject",
        reason: "explicit human rejection",
        operationKey: `canonical-failure-reject-${index}`,
      });
    }
    const canonicalFailure = vi.spyOn(library, "writeStudioDraftFeedbackState")
      .mockRejectedValueOnce(new Error("injected-canonical-draft-write-failure"));
    await expect(studio.recordFeedback(studioScope, {
      draftId: drafts[2]!.id,
      action: "reject",
      reason: "explicit human rejection",
      operationKey: "canonical-failure-reject-2",
    })).rejects.toThrow("injected-canonical-draft-write-failure");
    canonicalFailure.mockRestore();

    await expect(studio.listFeedback(studioScope, drafts[2]!.id)).resolves.toEqual([]);
    const ranked = await studio.rankMaterials(studioScope, demand.id);
    expect(ranked.every((row) => row.preferenceAdjustment === 0)).toBe(true);
  }, 300_000);

  it("records dissatisfaction and proposes changes without creating a brief or video", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const drafts = await studio.createDrafts(studioScope, brief.id, "structured-draft-op-1");
    const before = await listFiles(join(root, "drafts"));

    const feedback = await studio.recordCustomerFeedback(studioScope, {
      draftId: drafts[0]!.id,
      satisfaction: "partially-satisfied",
      issueCategories: ["style"],
      comment: "请让设计师观点更突出",
      mustKeep: ["成品结尾"],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "customer-feedback-1",
    });
    const proposal = await studio.proposeRevision(studioScope, feedback.id, "proposal-1");
    await expect(studio.proposeRevision(studioScope, feedback.id, "proposal-1"))
      .resolves.toEqual(proposal);
    await expect(studio.proposeRevision({ ...studioScope, actorId: "actor-b" }, feedback.id, "proposal-1"))
      .rejects.toThrow("revision-proposal-operation-actor-mismatch");
    await expect(studio.proposeRevision(studioScope, "different-feedback-id", "proposal-1"))
      .rejects.toThrow("revision-proposal-operation-key-conflict");

    expect(proposal.status).toBe("proposed");
    expect(await listFiles(join(root, "drafts"))).toEqual(before);
    const history = await studio.listStudioHistory(studioScope);
    expect(history.briefs).toEqual([brief]);
    expect(history.customerFeedback).toEqual([feedback]);
    expect(history.revisionProposals).toEqual([proposal]);
  }, 120_000);

  it("creates V2 only after confirmation, keeps V1 readable, and never auto-renders", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const drafts = await studio.createDrafts(studioScope, brief.id, "structured-draft-op-2");
    const feedback = await studio.recordCustomerFeedback(studioScope, {
      draftId: drafts[0]!.id,
      satisfaction: "unsatisfied",
      issueCategories: ["style"],
      comment: "请让成品结尾更突出",
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "customer-feedback-confirm",
    });
    const proposal = await studio.proposeRevision(studioScope, feedback.id, "proposal-confirm");
    const siblingFeedback = await studio.recordCustomerFeedback(studioScope, {
      draftId: drafts[1]!.id,
      satisfaction: "partially-satisfied",
      issueCategories: ["style"],
      comment: "请让设计师观点更突出",
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "customer-feedback-sibling",
    });
    const siblingProposal = await studio.proposeRevision(
      studioScope,
      siblingFeedback.id,
      "proposal-sibling",
    );
    const before = await listFiles(join(root, "drafts"));

    const revised = await studio.confirmRevisionProposal(studioScope, proposal.id, "confirm-1");

    expect(revised).toMatchObject({
      version: 2,
      parentBriefId: brief.id,
      feedbackId: feedback.id,
      revisionProposalId: proposal.id,
    });
    expect(await studio.getBrief(studioScope, brief.id)).toEqual(brief);
    expect(await studio.getRevisionProposal(studioScope, proposal.id)).toMatchObject({
      status: "executed",
      confirmedByActorId: studioScope.actorId,
    });
    expect(await studio.getRevisionProposal(studioScope, siblingProposal.id))
      .toMatchObject({ status: "superseded", confirmedByActorId: null, confirmedAt: null });
    await expect(studio.confirmRevisionProposal(
      studioScope,
      siblingProposal.id,
      "confirm-superseded-sibling",
    )).rejects.toThrow("revision-proposal-not-current");
    expect(await listFiles(join(root, "drafts"))).toEqual(before);
    expect((await studio.listStudioHistory(studioScope)).drafts).toHaveLength(2);
    await expect(studio.confirmRevisionProposal(studioScope, proposal.id, "confirm-1"))
      .resolves.toEqual(revised);
    await expect(studio.confirmRevisionProposal(
      { ...studioScope, actorId: "actor-b" },
      proposal.id,
      "confirm-1",
    )).rejects.toThrow("revision-confirmation-operation-actor-mismatch");
    await expect(studio.confirmRevisionProposal(studioScope, "different-proposal-id", "confirm-1"))
      .rejects.toThrow("revision-confirmation-operation-key-conflict");
    await expect(studio.getRevisionProposal({ ...studioScope, actorId: "actor-b" }, proposal.id))
      .rejects.toThrow("revision-proposal-actor-mismatch");
    await expect(studio.recordCustomerFeedback(studioScope, {
      draftId: drafts[1]!.id,
      satisfaction: "satisfied",
      issueCategories: [],
      comment: null,
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "feedback-on-stale-brief",
    })).rejects.toThrow("brief-version-not-current");
  }, 120_000);

  it("supersedes a proposed revision when newer exact-draft feedback is satisfied", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const [draft] = await studio.createDrafts(studioScope, brief.id, "latest-feedback-draft");
    const unsatisfied = await studio.recordCustomerFeedback(studioScope, {
      draftId: draft!.id,
      satisfaction: "unsatisfied",
      issueCategories: ["style"],
      comment: "请让设计师观点更突出",
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "latest-feedback-unsatisfied",
    });
    const proposal = await studio.proposeRevision(studioScope, unsatisfied.id, "latest-feedback-proposal");
    const satisfied = await studio.recordCustomerFeedback(studioScope, {
      draftId: draft!.id,
      satisfaction: "satisfied",
      issueCategories: [],
      comment: null,
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "latest-feedback-satisfied",
    });

    await expect(studio.getRevisionProposal(studioScope, proposal.id))
      .resolves.toMatchObject({ status: "superseded" });
    await expect(studio.proposeRevision(studioScope, unsatisfied.id, "latest-feedback-stale-propose"))
      .rejects.toThrow("customer-feedback-not-current");
    await expect(studio.confirmRevisionProposal(studioScope, proposal.id, "latest-feedback-stale-confirm"))
      .rejects.toThrow("revision-proposal-not-current");
    const history = await studio.listStudioHistory(studioScope);
    expect(history.customerFeedback).toEqual([unsatisfied, satisfied]);
    expect(history.revisionProposals).toEqual([expect.objectContaining({ id: proposal.id, status: "superseded" })]);
  }, 120_000);

  it("rejects an older V1 proposal after a newer exact-draft proposal creates V2", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const [draft] = await studio.createDrafts(studioScope, brief.id, "cross-version-draft-v1");
    const olderFeedback = await studio.recordCustomerFeedback(studioScope, {
      draftId: draft!.id,
      satisfaction: "unsatisfied",
      issueCategories: ["style"],
      comment: "请让设计师观点更突出",
      mustKeep: [], mustRemove: [], needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "cross-version-feedback-older",
    });
    const olderProposal = await studio.proposeRevision(studioScope, olderFeedback.id, "cross-version-proposal-older");
    const latestFeedback = await studio.recordCustomerFeedback(studioScope, {
      draftId: draft!.id,
      satisfaction: "unsatisfied",
      issueCategories: ["style"],
      comment: "请让成品结尾更突出",
      mustKeep: [], mustRemove: [], needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "cross-version-feedback-latest",
    });
    const latestProposal = await studio.proposeRevision(studioScope, latestFeedback.id, "cross-version-proposal-latest");
    const revised = await studio.confirmRevisionProposal(studioScope, latestProposal.id, "cross-version-confirm-latest");

    expect(revised.version).toBe(2);
    await expect(studio.confirmRevisionProposal(studioScope, olderProposal.id, "cross-version-confirm-older"))
      .rejects.toThrow("revision-proposal-not-current");
    const history = await studio.listStudioHistory(studioScope);
    expect(history.revisionProposals).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: olderProposal.id, status: "superseded" }),
      expect.objectContaining({ id: latestProposal.id, status: "executed" }),
    ]));
  }, 120_000);

  it("fails closed for vague, capture-gap, fact-error, conflicting, and cross-actor feedback", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const [draft] = await studio.createDrafts(studioScope, brief.id, "structured-draft-op-3");
    const vague: RecordCustomerFeedbackInput = {
      draftId: draft!.id,
      satisfaction: "unsatisfied" as const,
      issueCategories: [],
      comment: "",
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared" as const,
      operationKey: "customer-feedback-vague",
    };
    await expect(studio.recordCustomerFeedback(studioScope, vague))
      .rejects.toThrow("customer-feedback-detail-required");

    const gapInput = {
      ...vague,
      issueCategories: ["shot-selection" as const],
      needsReshoot: ["补一个新场景"],
      operationKey: "customer-feedback-gap",
    };
    const gapFeedback = await studio.recordCustomerFeedback(studioScope, gapInput);
    await expect(studio.recordCustomerFeedback(studioScope, gapInput)).resolves.toEqual(gapFeedback);
    await expect(studio.recordCustomerFeedback(studioScope, {
      ...gapInput,
      needsReshoot: ["补另一个场景"],
    })).rejects.toThrow("customer-feedback-operation-key-conflict");
    await expect(studio.recordCustomerFeedback({ ...studioScope, actorId: "actor-b" }, gapInput))
      .rejects.toThrow("customer-feedback-operation-actor-mismatch");
    const gapProposal = await studio.proposeRevision(studioScope, gapFeedback.id, "proposal-gap");
    expect(gapProposal.captureGaps).toEqual(["补一个新场景"]);
    expect(gapProposal.understoodFeedback).toContain("现有素材无法完成：补一个新场景");
    expect(gapProposal.understoodFeedback).not.toContain("补拍：");
    await expect(studio.confirmRevisionProposal(studioScope, gapProposal.id, "confirm-gap"))
      .rejects.toThrow("revision-proposal-has-unresolved-gaps");

    const factFeedback = await studio.recordCustomerFeedback(studioScope, {
      ...vague,
      issueCategories: ["fact-error"],
      comment: "18K 材质不对",
      operationKey: "customer-feedback-fact-error",
    });
    const factProposal = await studio.proposeRevision(studioScope, factFeedback.id, "proposal-fact-error");
    expect(factProposal.unsupportedRequests).not.toEqual([]);
    await expect(studio.confirmRevisionProposal(studioScope, factProposal.id, "confirm-fact-error"))
      .rejects.toThrow("revision-proposal-has-unsupported-requests");
  }, 120_000);

  it("accepts exact customer-feedback limits and rejects every overflow deterministically", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const [draft] = await studio.createDrafts(studioScope, brief.id, "feedback-limit-draft");
    const items = Array.from({ length: 20 }, (_, index) =>
      `${String(index).padStart(2, "0")}${"项".repeat(198)}`);
    await expect(studio.recordCustomerFeedback(studioScope, {
      draftId: draft!.id,
      satisfaction: "partially-satisfied",
      issueCategories: ["pacing"],
      comment: "c".repeat(1000),
      mustKeep: items,
      mustRemove: items.map((item) => item.replace("项", "删")),
      needsReshoot: items.map((item) => item.replace("项", "拍")),
      submittedBy: "customer-self-declared",
      operationKey: "k".repeat(200),
    })).resolves.toMatchObject({ version: 1 });

    const base = {
      draftId: draft!.id,
      satisfaction: "partially-satisfied" as const,
      issueCategories: ["pacing" as const],
      comment: "节奏太慢",
      mustKeep: [] as string[],
      mustRemove: [] as string[],
      needsReshoot: [] as string[],
      submittedBy: "customer-self-declared" as const,
    };
    const failures = [
      [{ ...base, comment: "c".repeat(1001), operationKey: "comment-over" }, "customer-feedback-comment-too-long"],
      [{ ...base, mustKeep: Array.from({ length: 21 }, (_, index) => `item-${index}`), operationKey: "keep-count-over" }, "customer-feedback-must-keep-too-many"],
      [{ ...base, mustRemove: ["x".repeat(201)], operationKey: "remove-item-over" }, "customer-feedback-must-remove-item-too-long"],
      [{ ...base, needsReshoot: ["x".repeat(201)], operationKey: "reshoot-item-over" }, "customer-feedback-needs-reshoot-item-too-long"],
      [{ ...base, operationKey: "k".repeat(201) }, "customer-feedback-operation-key-too-long"],
    ] as const;
    for (const [input, error] of failures) {
      await expect(studio.recordCustomerFeedback(studioScope, input)).rejects.toThrow(error);
    }
  }, 120_000);

  it("requires the latest exact-draft customer feedback to be satisfied before adoption", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const [draft] = await studio.createDrafts(studioScope, brief.id, "structured-draft-op-4");

    await expect(studio.adoptDraft(studioScope, draft!.id, "adopt-before-satisfaction"))
      .rejects.toThrow("satisfied-customer-feedback-required");
    await studio.recordCustomerFeedback(studioScope, {
      draftId: draft!.id,
      satisfaction: "partially-satisfied",
      issueCategories: ["pacing"],
      comment: "节奏太慢",
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "customer-feedback-unsatisfied-adopt",
    });
    await expect(studio.getDraft(studioScope, draft!.id))
      .resolves.toMatchObject({ status: "changes-requested" });
    await expect(studio.adoptDraft(studioScope, draft!.id, "adopt-after-dissatisfaction"))
      .rejects.toThrow("satisfied-customer-feedback-required");
    const satisfied = await studio.recordCustomerFeedback(studioScope, {
      draftId: draft!.id,
      satisfaction: "satisfied",
      issueCategories: [],
      comment: null,
      mustKeep: [],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared",
      operationKey: "customer-feedback-satisfied-adopt",
    });
    expect(satisfied.publishClaimed).toBe(false);
    const adopted = await studio.adoptDraft(studioScope, draft!.id, "adopt-after-satisfaction");
    expect(adopted.publishClaimed).toBe(false);
    await expect(studio.getDraft(studioScope, draft!.id))
      .resolves.toMatchObject({ status: "adopted", publishClaimed: false });
  }, 120_000);

  it("allows exactly one adopted draft per brief version under concurrent requests", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const drafts = await studio.createDrafts(studioScope, brief.id, "one-adopt-drafts");
    await Promise.all(drafts.map((draft, index) => recordSatisfied(studio, draft.id, `one-adopt-${index}`)));

    const results = await Promise.allSettled([
      studio.adoptDraft(studioScope, drafts[0]!.id, "one-adopt-first"),
      studio.adoptDraft(studioScope, drafts[1]!.id, "one-adopt-second"),
    ]);
    const fulfilled = results.filter((result) => result.status === "fulfilled");
    const rejected = results.filter((result) => result.status === "rejected");
    expect(fulfilled).toHaveLength(1);
    expect(rejected).toHaveLength(1);
    expect((rejected[0] as PromiseRejectedResult).reason).toEqual(
      expect.objectContaining({ message: "draft-brief-already-adopted" }),
    );
    const adoptedEvent = (fulfilled[0] as PromiseFulfilledResult<Awaited<ReturnType<typeof studio.adoptDraft>>>).value;
    const operationKey = adoptedEvent.draftId === drafts[0]!.id ? "one-adopt-first" : "one-adopt-second";
    await expect(studio.adoptDraft(studioScope, adoptedEvent.draftId, operationKey)).resolves.toEqual(adoptedEvent);

    const history = await studio.listStudioHistory(studioScope);
    const sameVersion = history.drafts.filter((draft) => draft.briefId === brief.id && draft.version === brief.version);
    expect(sameVersion.filter((draft) => draft.status === "adopted")).toHaveLength(1);
    expect(sameVersion.filter((draft) => draft.status === "rejected")).toHaveLength(1);
  }, 120_000);

  it("rejects contradictory satisfied feedback before it can unlock adoption", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const [draft] = await studio.createDrafts(studioScope, brief.id, "contradictory-satisfied-draft");
    const base = {
      draftId: draft!.id,
      satisfaction: "satisfied" as const,
      issueCategories: [] as RecordCustomerFeedbackInput["issueCategories"],
      comment: null,
      mustKeep: [] as string[],
      mustRemove: [] as string[],
      needsReshoot: [] as string[],
      submittedBy: "customer-self-declared" as const,
    };
    const contradictions = [
      { issueCategories: ["fact-error" as const], operationKey: "satisfied-fact-error" },
      { mustRemove: ["错误事实镜头"], operationKey: "satisfied-must-remove" },
      { needsReshoot: ["补拍新场景"], operationKey: "satisfied-needs-reshoot" },
    ];
    for (const contradiction of contradictions) {
      await expect(studio.recordCustomerFeedback(studioScope, { ...base, ...contradiction }))
        .rejects.toThrow("satisfied-customer-feedback-contradiction");
    }
    await expect(studio.adoptDraft(studioScope, draft!.id, "adopt-after-contradictions"))
      .rejects.toThrow("satisfied-customer-feedback-required");
  }, 280_000);

  it("blocks the legacy revise path without creating a new brief", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const drafts = await studio.createDrafts(studioScope, brief.id, "draft-op-1");
    const originalDraft = drafts[0]!;

    const event = await studio.recordFeedback(studioScope, {
      draftId: originalDraft.id,
      action: "request-changes",
      reason: "请让设计师观点更突出",
      operationKey: "feedback-op-1",
    });
    await expect(studio.reviseBrief(studioScope, brief.id, event.id, "brief-op-1"))
      .rejects.toThrow("structured-revision-confirmation-required");

    expect(await studio.getBrief(studioScope, brief.id)).toEqual(brief);
    expect((await studio.listStudioHistory(studioScope)).briefs).toEqual([brief]);
    expect(await studio.getDraft(studioScope, originalDraft.id)).toMatchObject({
      id: originalDraft.id,
      status: "changes-requested",
    });
  }, 280_000);

  it("invalidates every dependent draft immediately after withdrawal and blocks new rendering", async () => {
    const { root, library, studio, assets, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const drafts = await studio.createDrafts(studioScope, brief.id, "draft-op-withdraw");
    const sourceAssetId = drafts[0]!.sourceMap[0]!.sourceAssetId;
    await recordSatisfied(studio, drafts[0]!.id, "satisfied-before-withdrawal");
    await studio.adoptDraft(studioScope, drafts[0]!.id, "adopt-before-withdrawal");

    await library.withdraw(studioScope.actorId, studioScope.customerId, sourceAssetId, "withdraw-op-1");

    await expect(studio.adoptDraft(studioScope, drafts[0]!.id, "adopt-before-withdrawal"))
      .rejects.toThrow("draft-source-withdrawn");
    await expect(studio.adoptDraft(studioScope, drafts[0]!.id, "feedback-op-withdrawn"))
      .rejects.toThrow("draft-source-withdrawn");
    await expect(studio.getDraft(studioScope, drafts[0]!.id))
      .resolves.toMatchObject({ status: "invalidated" });
    await expect(studio.getDraft(studioScope, drafts[1]!.id))
      .resolves.toMatchObject({ status: "invalidated" });
    await expect(studio.createDrafts(studioScope, brief.id, "new-render-after-withdrawal"))
      .rejects.toThrow("draft-source-withdrawn");
    expect(assets.map((asset) => asset.id)).toContain(sourceAssetId);
  }, 120_000);

  it("fails closed when a rendered draft is overwritten or missing without mutating its stored record", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const drafts = await studio.createDrafts(studioScope, brief.id, "draft-op-output-integrity");
    const overwritten = drafts[0]!;
    const missing = drafts[1]!;
    await recordSatisfied(studio, overwritten.id, "satisfied-before-overwrite");
    await recordSatisfied(studio, missing.id, "satisfied-before-missing");

    await writeFile(overwritten.outputPath, Buffer.from([0x00, 0x01, 0x02]));
    await unlink(missing.outputPath);

    await expect(studio.getDraft(studioScope, overwritten.id))
      .resolves.toMatchObject({ id: overwritten.id, outputDigest: overwritten.outputDigest, status: "invalidated" });
    await expect(studio.adoptDraft(studioScope, overwritten.id, "adopt-overwritten-output"))
      .rejects.toThrow("studio-render-output-digest-mismatch");
    await expect(studio.getDraft(studioScope, missing.id))
      .resolves.toMatchObject({ id: missing.id, outputDigest: missing.outputDigest, status: "invalidated" });
    await expect(studio.adoptDraft(studioScope, missing.id, "adopt-missing-output"))
      .rejects.toThrow("studio-render-output-unavailable");
    expect(await studio.listFeedback(studioScope, overwritten.id)).toEqual([]);
    expect(await studio.listFeedback(studioScope, missing.id)).toEqual([]);
  }, 120_000);

  it("checks the material inventory once when loading a history with multiple drafts", async () => {
    const { root, library, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    await studio.createDrafts(studioScope, brief.id, "draft-op-history-batch");
    const inventory = vi.spyOn(library, "inventoryAt");

    const history = await studio.listStudioHistory(studioScope);

    expect(history.drafts).toHaveLength(2);
    expect(inventory).toHaveBeenCalledTimes(1);
  }, 120_000);

  it("allows only adopt, request-changes, or reject and makes feedback retries body-bound", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const [draft] = await studio.createDrafts(studioScope, brief.id, "draft-op-feedback");
    const input = {
      draftId: draft!.id,
      action: "reject" as const,
      reason: "不符合本次表达",
      operationKey: "feedback-op-replay",
    };

    const first = await studio.recordFeedback(studioScope, input);
    await expect(studio.recordFeedback(studioScope, input)).resolves.toEqual(first);
    await expect(studio.recordFeedback(studioScope, {
      ...input,
      action: "adopt",
      reason: null,
    })).rejects.toThrow("feedback-operation-key-conflict");
    await expect(studio.recordFeedback(studioScope, {
      ...input,
      action: "publish" as never,
    })).rejects.toThrow("feedback-action-not-allowed");
    expect(first.publishClaimed).toBe(false);
  }, 120_000);

  it("keeps every feedback event append-only and readable in version order", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const [draft] = await studio.createDrafts(studioScope, brief.id, "draft-op-feedback-history");
    const first = await studio.recordFeedback(studioScope, {
      draftId: draft!.id,
      action: "request-changes",
      reason: "观点提前",
      operationKey: "feedback-history-1",
    });
    const second = await studio.recordFeedback(studioScope, {
      draftId: draft!.id,
      action: "reject",
      reason: "改稿前保留原判断",
      operationKey: "feedback-history-2",
    });

    const history = await studio.listFeedback(studioScope, draft!.id);

    expect(history).toEqual([first, second]);
    expect(history.map((event) => event.version)).toEqual([1, 2]);
    history[0]!.reason = "attempted rewrite";
    await expect(studio.listFeedback(studioScope, draft!.id)).resolves.toEqual([first, second]);
  }, 120_000);
});

async function listFiles(root: string): Promise<string[]> {
  return (await readdir(root, { recursive: true })).sort();
}

async function recordSatisfied(
  studio: Awaited<ReturnType<typeof prepareRenderableStudio>>["studio"],
  draftId: string,
  operationKey: string,
): Promise<void> {
  await studio.recordCustomerFeedback(studioScope, {
    draftId,
    satisfaction: "satisfied",
    issueCategories: [],
    comment: null,
    mustKeep: [],
    mustRemove: [],
    needsReshoot: [],
    submittedBy: "customer-self-declared",
    operationKey,
  });
}
