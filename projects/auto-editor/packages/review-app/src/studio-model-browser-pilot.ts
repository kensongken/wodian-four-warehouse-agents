import { createHash, randomInt } from "node:crypto";
import { lstat, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

import type {
  ModelAnalysisJobV1,
  ModelAnalysisReceiptV1,
  StudioAnalysisLayer,
} from "@wearable/contracts";
import type { StudioModelWorker } from "@wearable/mobile-agent";
import { chromium, type Page, type Request } from "playwright-core";

import {
  defaultStudioBrowserExecutable,
} from "./studio-browser-pilot";
import { createStudioServer, listenStudioServer } from "./studio-server";

const REQUESTED_LAYERS = Object.freeze([
  "technical", "scene", "transcript", "embedding", "vision",
] as const satisfies readonly StudioAnalysisLayer[]);

type BrowserBrief = Readonly<{
  id: string;
  version: number;
  parentBriefId: string | null;
  revisionProposalId: string | null;
}>;

type BrowserDraft = Readonly<{
  id: string;
  version: number;
  briefId: string;
}>;

type BrowserFeedback = Readonly<{
  id: string;
  draftId: string;
  briefId: string;
  satisfaction: string;
}>;

type BrowserProposal = Readonly<{
  id: string;
  parentBriefId: string;
  parentDraftId: string;
}>;

type PublicNetworkEntry = Readonly<{
  method: string;
  url: string;
  resourceType: string;
  postData: unknown;
}>;

export type StudioModelBrowserPilotReport = Readonly<{
  schemaVersion: 1;
  browserEngine: "chromium";
  browserVersion: string;
  url: string;
  providerEvidence: "fixture/synthetic";
  syntheticOnly: true;
  modelMode: "offline-fallback";
  modelDegraded: true;
  fixtureMechanicalPass: boolean;
  realProviderAcceptance: Readonly<{
    status: "blocked";
    reason: "approved-local-browser-evidence-not-provided";
  }>;
  demandGoal: string;
  postAnalysisMatchVerdict: "ready";
  requestedLayers: readonly StudioAnalysisLayer[];
  analysisLayerStatuses: Readonly<Record<StudioAnalysisLayer, string>>;
  analyzedAssetCount: number;
  correctedSuggestionCount: number;
  duplicateKeepCount: number;
  materialSuitabilityCounts: Readonly<{ suitable: number; "not-suitable": number }>;
  customerSatisfaction: string[];
  revisionProposalCount: number;
  reviewedReceiptBindings: ReadonlyArray<Readonly<{
    receiptJobId: string;
    sourceAssetId: string;
    sourceDigest: string;
    sourceStartMs: number;
    sourceEndMs: number;
  }>>;
  falseConfirmedFactCount: number;
  crossCustomerExposureCount: number;
  originalSnapshotCount: number;
  originalMutationCount: number;
  draftRequestCountBeforeBusinessMatch: number;
  networkWorkflowOrder: readonly string[];
  lineage: Readonly<{
    demandId: string;
    businessMatchId: string;
    briefVersions: readonly number[];
    initialBriefId: string;
    initialBriefParentId: null;
    revisedBriefId: string;
    revisedBriefParentId: string;
    revisionProposalId: string;
    revisedBriefRevisionProposalId: string;
    initialDraftBriefIds: readonly string[];
    revisedDraftBriefIds: readonly string[];
    partialFeedbackDraftVersion: number;
    satisfiedFeedbackDraftVersion: number;
  }>;
  deleteOriginalButtonCount: number;
  uploadButtonCount: number;
  publishButtonCount: number;
  deleteRequestCount: number;
  uploadRequestCount: number;
  publishRequestCount: number;
  externalNetworkRequestCount: number;
  modelNetworkRequestCount: number;
  tracePath: string;
  domSnapshotPath: string;
  screenshotPath: string;
  networkLogPath: string;
  reportPath: string;
}>;

export function defaultStudioModelBrowserExecutable(): string {
  return defaultStudioBrowserExecutable();
}

export async function runStudioModelBrowserPilot(
  outputDirectoryValue: string,
  options: Readonly<{ executablePath?: string }> = {},
): Promise<StudioModelBrowserPilotReport> {
  const outputDirectory = resolve(outputDirectoryValue);
  const runtimeRoot = join(outputDirectory, "runtime");
  const tracePath = join(outputDirectory, "studio-model-browser-trace.zip");
  const domSnapshotPath = join(outputDirectory, "studio-model-browser-dom.html");
  const screenshotPath = join(outputDirectory, "studio-model-browser.png");
  const networkLogPath = join(outputDirectory, "studio-model-browser-network.json");
  const reportPath = join(outputDirectory, "studio-model-browser-report.json");
  await mkdir(outputDirectory, { recursive: true });

  const server = createStudioServer({
    rootDir: runtimeRoot,
    now: () => "2026-09-13T08:00:00.000Z",
    modelWorker: fixtureWorker(),
    modelStatusReader: async () => ({
      mode: "offline-fallback",
      localOnly: true,
      degraded: true,
      artifacts: [
        { id: "fixture-five-layer-worker", version: "fixture-v1", status: "synthetic-only" },
      ],
      visionProbe: "not-run: fixture-provider-evidence",
    }),
  });
  const port = await listenOnRandomNonProductionPort(server);
  const url = `http://127.0.0.1:${port}/studio`;
  const browser = await chromium.launch({
    executablePath: options.executablePath ?? defaultStudioModelBrowserExecutable(),
    headless: true,
  });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
  const page = await context.newPage();
  const network: PublicNetworkEntry[] = [];
  let traceStopped = false;
  page.on("request", (request) => network.push(publicNetworkEntry(request)));

  try {
    await page.goto(url, { waitUntil: "domcontentloaded" });
    await waitForStatus(page, "已准备 4 段演示素材，可以开始体验");
    const originalsBefore = await snapshotStudioOriginals(runtimeRoot);

    // First collect all five local-analysis layers for two assets. The demanded
    // fourth asset deliberately remains unanalysed so human review cannot turn
    // model suggestions into confirmed facts or block the later business match.
    // The mobile-first app hides non-active workflows, so drive the same library
    // navigation a customer uses before selecting material-analysis controls.
    await page.locator('.studio-bottom-nav [data-studio-tab="library"]').click();
    await page.locator('.studio-shell[data-active-tab="library"][data-current-step="materials"]')
      .waitFor({ state: "visible" });
    await page.locator(".advanced-analysis-drawer").evaluate((element) => {
      (element as HTMLDetailsElement).open = true;
    });
    const analysisPayloads: Array<{ analysis?: { receipts?: ModelAnalysisReceiptV1[] } }> = [];
    const analysisButtons = page.locator(".material-card")
      .filter({ hasNotText: "纯色合成商品夹具 4" }).locator("[data-start-analysis]");
    if (await analysisButtons.count() < 2) throw new Error("studio-model-browser-analysis-assets-missing");
    for (let index = 0; index < 2; index += 1) {
      const responsePromise = waitForApiResponse(page, "/api/studio/analysis/jobs");
      await analysisButtons.nth(index).evaluate((element) => {
        const details = element.closest("details");
        if (details) details.open = true;
      });
      await analysisButtons.nth(index).click();
      const response = await responsePromise;
      analysisPayloads.push(await response.json() as { analysis?: { receipts?: ModelAnalysisReceiptV1[] } });
      await page.locator(".analysis-card").first().waitFor({ state: "visible" });
    }
    const firstReceipts = analysisPayloads[0]?.analysis?.receipts ?? [];
    const analysisLayerStatuses = Object.fromEntries(REQUESTED_LAYERS.map((layer) => [
      layer,
      firstReceipts.find((receipt) => receipt.layer === layer)?.status ?? "missing",
    ])) as Record<StudioAnalysisLayer, string>;

    const refreshedAnalysisResponse = waitForApiResponse(page, "/api/studio/analysis/history");
    await page.locator("#refresh-analysis").click();
    await refreshedAnalysisResponse;

    page.once("dialog", async (dialog) => dialog.accept("合成商品细节特写"));
    const correctionResponse = waitForApiResponse(page, "/api/studio/analysis/review");
    await page.locator('[data-analysis-review-action="correct-suggestion"]').first().click();
    await correctionResponse;

    const duplicateResponse = waitForApiResponse(page, "/api/studio/analysis/review");
    await page.locator('[data-analysis-review-action="keep-from-duplicate-group"]').first().click();
    await duplicateResponse;

    await page.locator('.studio-bottom-nav [data-studio-tab="create"]').click();
    await page.locator('.studio-shell[data-active-tab="create"]').waitFor({ state: "visible" });
    await page.locator("details.advanced-tools").evaluate((element) => {
      (element as HTMLDetailsElement).open = true;
    });
    await page.locator(".advanced-demand summary").click();
    const demandGoal = "用合成素材讲清楚设计灵感";
    await page.locator('[name="contentType"]').selectOption("single-product");
    await page.locator('[name="goal"]').fill(demandGoal);
    await page.locator('[name="targetAudience"]').fill("预约到店的合成顾客");
    await page.locator('[name="desiredEffect"]').fill("看懂合成设计概念");
    await page.locator('[name="mustInclude"]').fill("纯色合成商品夹具 4");
    await page.locator('[name="mustAvoid"]').fill("真实客户身份");
    await page.locator('[name="submittedBy"]').selectOption("customer-self-declared");
    const demandResponse = waitForApiResponse(page, "/api/studio/demands");
    await page.locator('#studio-demand-form button[type="submit"]').click();
    const demandResult = await demandResponse;
    const demandPayload = await demandResult.json() as { demand?: { id?: unknown } };
    const demandId = String(demandPayload.demand?.id ?? "");
    if (!demandResult.ok() || !demandId) throw new Error("studio-model-browser-demand-response-invalid");
    await waitForStatus(page, "这次需求已记录");

    // Suitability feedback is required to bind to a real demand/match pair.
    // This first match is only that binding context; the audited business match
    // is deliberately performed after both human decisions below.
    const bindingMatchResponse = waitForApiResponse(page, "/api/studio/matches");
    await page.locator("#assess-demand").click();
    const bindingMatchResult = await bindingMatchResponse;
    if (!bindingMatchResult.ok()) throw new Error("studio-model-browser-binding-match-failed");
    const bindingMatchPayload = await bindingMatchResult.json() as { assessment?: { id?: unknown; verdict?: unknown } };
    if (!bindingMatchPayload.assessment?.id) throw new Error("studio-model-browser-binding-match-invalid");
    await waitForStatus(page, bindingMatchPayload.assessment.verdict === "ready"
      ? "现有素材适合继续"
      : "现有素材部分匹配");

    await page.locator('.studio-bottom-nav [data-studio-tab="library"]').click();
    await page.locator('.studio-shell[data-active-tab="library"][data-current-step="materials"]')
      .waitFor({ state: "visible" });
    const suitabilityRefresh = waitForApiResponse(page, "/api/studio/analysis/history");
    await page.locator("#refresh-analysis").click();
    await suitabilityRefresh;

    page.once("dialog", async (dialog) => dialog.accept("人工确认这段合成素材适合当前需求"));
    const suitableResponse = waitForApiResponse(page, "/api/studio/analysis/suitability");
    await page.locator('[data-material-suitability="suitable"]').first().click();
    const suitableResult = await suitableResponse;
    if (!suitableResult.ok()) {
      throw new Error(`studio-model-browser-suitability-failed:${suitableResult.status()}:${await suitableResult.text()}`);
    }

    page.once("dialog", async (dialog) => dialog.accept("人工确认第二段合成素材不适合当前需求"));
    const notSuitableResponse = waitForApiResponse(page, "/api/studio/analysis/suitability");
    await page.locator('[data-material-suitability="not-suitable"]').nth(1).click();
    await notSuitableResponse;

    await page.locator('.studio-bottom-nav [data-studio-tab="create"]').click();
    await page.locator('.studio-shell[data-active-tab="create"]').waitFor({ state: "visible" });
    const businessMatchResponse = waitForApiResponse(page, "/api/studio/matches");
    await page.locator("#assess-demand").click();
    const businessMatchResult = await businessMatchResponse;
    if (!businessMatchResult.ok()) throw new Error("studio-model-browser-business-match-failed");
    const businessMatchPayload = await businessMatchResult.json() as { assessment?: { id?: unknown; verdict?: unknown } };
    if (businessMatchPayload.assessment?.verdict !== "ready") {
      throw new Error(`studio-model-browser-business-match-not-ready:${String(businessMatchPayload.assessment?.verdict)}`);
    }
    const businessMatchId = String(businessMatchPayload.assessment.id ?? "");
    const postAnalysisMatchVerdict = "ready" as const;
    await waitForStatus(page, "现有素材适合继续");

    await page.locator('.studio-bottom-nav [data-studio-tab="library"]').click();
    await page.locator('.studio-shell[data-active-tab="library"][data-current-step="materials"]')
      .waitFor({ state: "visible" });
    await page.locator(".material-details summary").first().click();
    await page.locator("[data-studio-fact-id]").first().check();
    await page.locator('.studio-bottom-nav [data-studio-tab="create"]').click();
    await page.locator('.studio-shell[data-active-tab="create"]').waitFor({ state: "visible" });
    if (await page.locator("#confirm-brief").isDisabled()) {
      throw new Error("studio-model-browser-confirm-brief-disabled");
    }
    const briefResponse = waitForApiResponse(page, "/api/studio/briefs");
    const draftResponse = waitForApiResponse(page, "/api/studio/drafts");
    // A rejected brief never starts rendering. Keep that pending wait handled
    // while reporting the actual brief rejection below.
    void draftResponse.catch(() => undefined);
    await page.locator("#confirm-brief").click();
    const briefResult = await briefResponse;
    const briefPayload = await briefResult.json() as { brief?: BrowserBrief };
    if (!briefResult.ok() || !briefPayload.brief) {
      throw new Error(`studio-model-browser-brief-response-invalid:${briefResult.status()}:${JSON.stringify(briefPayload)}`);
    }
    const initialBrief = briefPayload.brief;
    const draftResult = await draftResponse;
    const draftPayload = await draftResult.json() as { drafts?: BrowserDraft[] };
    if (!draftResult.ok() || !draftPayload.drafts?.length) throw new Error("studio-model-browser-v1-drafts-invalid");
    const initialDrafts = draftPayload.drafts;
    await waitForStatus(page, "两种剪辑方向已准备好，请播放后再做决定");

    await page.locator(".customer-feedback-drawer summary").nth(0).click();
    const partialForm = page.locator("[data-customer-feedback]").nth(0);
    await partialForm.locator('[name="satisfaction"]').selectOption("partially-satisfied");
    await partialForm.locator('[name="issueCategories"][value="style"]').check();
    await partialForm.locator('[name="comment"]').fill("请让设计师观点更突出");
    const partialResponse = waitForApiResponse(page, "/api/studio/customer-feedback");
    const proposalResponse = waitForApiResponse(page, "/api/studio/revision-proposals");
    await partialForm.locator('button[type="submit"]').click();
    const partialResult = await partialResponse;
    const proposalResult = await proposalResponse;
    const partialPayload = await partialResult.json() as { feedback?: BrowserFeedback };
    const proposalPayload = await proposalResult.json() as { proposal?: BrowserProposal };
    if (!partialResult.ok() || !partialPayload.feedback || !proposalResult.ok() || !proposalPayload.proposal) {
      throw new Error("studio-model-browser-revision-proposal-invalid");
    }
    await waitForStatus(page, "修改提案已生成");

    if (await page.locator("#confirm-revision-proposal").isDisabled()) {
      throw new Error(`studio-model-browser-revision-proposal-not-confirmable:${JSON.stringify(proposalPayload.proposal)}`);
    }
    const confirmResponse = waitForApiResponse(page, "/api/studio/revision-proposals/confirm");
    await page.locator("#confirm-revision-proposal").click();
    const confirmResult = await confirmResponse;
    const confirmPayload = await confirmResult.json() as { brief?: BrowserBrief };
    if (!confirmResult.ok() || !confirmPayload.brief) throw new Error("studio-model-browser-revised-brief-invalid");
    const revisedBrief = confirmPayload.brief;
    await waitForStatus(page, "任务书 V2 已创建");

    const revisedDraftResponse = waitForApiResponse(page, "/api/studio/drafts");
    await page.locator("#make-revision-drafts").click();
    const revisedDraftResult = await revisedDraftResponse;
    const revisedDraftPayload = await revisedDraftResult.json() as { drafts?: BrowserDraft[] };
    if (!revisedDraftResult.ok() || !revisedDraftPayload.drafts?.length) throw new Error("studio-model-browser-v2-drafts-invalid");
    const revisedDrafts = revisedDraftPayload.drafts;
    await waitForStatus(page, "两种剪辑方向已准备好，请播放后再做决定");

    await page.locator(".customer-feedback-drawer summary").nth(0).click();
    const satisfiedForm = page.locator("[data-customer-feedback]").nth(0);
    await satisfiedForm.locator('[name="satisfaction"]').selectOption("satisfied");
    const satisfiedResponse = waitForApiResponse(page, "/api/studio/customer-feedback");
    await satisfiedForm.locator('button[type="submit"]').click();
    const satisfiedResult = await satisfiedResponse;
    const satisfiedPayload = await satisfiedResult.json() as { feedback?: BrowserFeedback };
    if (!satisfiedResult.ok() || !satisfiedPayload.feedback) throw new Error("studio-model-browser-v2-feedback-invalid");
    await waitForStatus(page, "客户已标记满意");

    const analysisHistory = await page.evaluate(async () => {
      const response = await fetch("/api/studio/analysis/history", {
        method: "POST", headers: { "content-type": "application/json" }, body: "{}",
      });
      return response.json();
    }) as { history?: {
      entries?: Array<{ evidence?: { card?: { confirmedFacts?: unknown[] } } }>;
      reviews?: Array<{ action?: string; receiptJobId?: string; sourceAssetId?: string; sourceDigest?: string; sourceStartMs?: number; sourceEndMs?: number }>;
      suitabilityReviews?: Array<{ decision?: string; receiptJobId?: string; sourceAssetId?: string; sourceDigest?: string; sourceStartMs?: number; sourceEndMs?: number }>;
    } };
    const studioHistory = await page.evaluate(async () => {
      const response = await fetch("/api/studio/history", {
        method: "POST", headers: { "content-type": "application/json" }, body: "{}",
      });
      return response.json();
    }) as { history?: { customerFeedback?: Array<{ satisfaction?: string }>; revisionProposals?: unknown[] } };
    const modelStatus = await page.evaluate(async () => {
      const response = await fetch("/api/studio/models/status", {
        method: "POST", headers: { "content-type": "application/json" }, body: "{}",
      });
      return response.json();
    }) as { models?: { mode?: string; degraded?: boolean } };
    if (modelStatus.models?.mode !== "offline-fallback" || modelStatus.models.degraded !== true) {
      throw new Error("studio-model-browser-fixture-provider-boundary-invalid");
    }

    const otherScope = await page.evaluate(async () => {
      const sessionResponse = await fetch("/api/studio/session", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ customerId: "synthetic-customer-b" }),
      });
      const session = await sessionResponse.json();
      const historyResponse = await fetch("/api/studio/analysis/history", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${session.session.token}`,
        },
        body: "{}",
      });
      return historyResponse.json();
    }) as { history?: { entries?: unknown[] } };

    const reviewedReceiptBindings = [
      ...(analysisHistory.history?.reviews ?? []),
      ...(analysisHistory.history?.suitabilityReviews ?? []),
    ].map((row) => ({
      receiptJobId: String(row.receiptJobId ?? ""),
      sourceAssetId: String(row.sourceAssetId ?? ""),
      sourceDigest: String(row.sourceDigest ?? ""),
      sourceStartMs: Number(row.sourceStartMs ?? -1),
      sourceEndMs: Number(row.sourceEndMs ?? -1),
    }));
    const falseConfirmedFactCount = (analysisHistory.history?.entries ?? [])
      .flatMap((entry) => entry.evidence?.card?.confirmedFacts ?? []).length;
    const crossCustomerExposureCount = otherScope.history?.entries?.length ?? 0;
    const originalMutationCount = countStudioOriginalIntegrityViolations(originalsBefore)
      + changedDigestCount(originalsBefore, await snapshotStudioOriginals(runtimeRoot));
    const correctedSuggestionCount = (analysisHistory.history?.reviews ?? [])
      .filter((review) => review.action === "correct-suggestion").length;
    const duplicateKeepCount = (analysisHistory.history?.reviews ?? [])
      .filter((review) => review.action === "keep-from-duplicate-group").length;
    const suitability = analysisHistory.history?.suitabilityReviews ?? [];
    const materialSuitabilityCounts = {
      suitable: suitability.filter((review) => review.decision === "suitable").length,
      "not-suitable": suitability.filter((review) => review.decision === "not-suitable").length,
    };
    const customerSatisfaction = (studioHistory.history?.customerFeedback ?? [])
      .map((feedback) => String(feedback.satisfaction ?? ""))
      .sort();
    const revisionProposalCount = studioHistory.history?.revisionProposals?.length ?? 0;
    const workflowEvidence = auditedWorkflowEvidence(network);
    const initialDraftBriefIds = [...new Set(initialDrafts.map((draft) => draft.briefId))];
    const revisedDraftBriefIds = [...new Set(revisedDrafts.map((draft) => draft.briefId))];
    const partialFeedbackDraftVersion = initialDrafts.find((draft) =>
      draft.id === partialPayload.feedback?.draftId)?.version ?? -1;
    const satisfiedFeedbackDraftVersion = revisedDrafts.find((draft) =>
      draft.id === satisfiedPayload.feedback?.draftId)?.version ?? -1;
    if (initialBrief.version !== 1 || initialBrief.parentBriefId !== null ||
        revisedBrief.version !== 2 || revisedBrief.parentBriefId !== initialBrief.id ||
        revisedBrief.revisionProposalId !== proposalPayload.proposal.id ||
        proposalPayload.proposal.parentBriefId !== initialBrief.id ||
        !initialDraftBriefIds.every((id) => id === initialBrief.id) ||
        !revisedDraftBriefIds.every((id) => id === revisedBrief.id) ||
        partialFeedbackDraftVersion !== 1 || satisfiedFeedbackDraftVersion !== 2) {
      throw new Error("studio-model-browser-lineage-invalid");
    }
    const lineage = {
      demandId,
      businessMatchId,
      briefVersions: [1, 2] as const,
      initialBriefId: initialBrief.id,
      initialBriefParentId: null,
      revisedBriefId: revisedBrief.id,
      revisedBriefParentId: initialBrief.id,
      revisionProposalId: proposalPayload.proposal.id,
      revisedBriefRevisionProposalId: proposalPayload.proposal.id,
      initialDraftBriefIds,
      revisedDraftBriefIds,
      partialFeedbackDraftVersion,
      satisfiedFeedbackDraftVersion,
    } as const;

    const deleteOriginalButtonCount = await page.locator('[data-delete-original], button:has-text("删除原片")').count();
    const uploadButtonCount = await page.locator('[data-upload-provider], button:has-text("上传模型")').count();
    const publishButtonCount = await page.locator('[data-publish], button:has-text("发布")').count();
    const paths = network.map((entry) => new URL(entry.url).pathname);
    const deleteRequestCount = paths.filter((path) => /delete|remove-original/iu.test(path)).length;
    const uploadRequestCount = paths.filter((path) => /upload/iu.test(path)).length;
    const publishRequestCount = paths.filter((path) => /publish/iu.test(path)).length;
    const externalNetworkRequestCount = network.filter((entry) => {
      const hostname = new URL(entry.url).hostname;
      return hostname !== "127.0.0.1" && hostname !== "localhost" && hostname !== "::1";
    }).length;
    const modelNetworkRequestCount = network.filter((entry) =>
      new URL(entry.url).pathname.includes("/providers/") || /openai|huggingface|modelscope/iu.test(entry.url),
    ).length;

    await page.screenshot({ path: screenshotPath, fullPage: true });
    await writeFile(domSnapshotPath, `${await page.content()}\n`, "utf8");
    await writeFile(networkLogPath, `${JSON.stringify(network, null, 2)}\n`, "utf8");
    await context.tracing.stop({ path: tracePath });
    traceStopped = true;

    const fixtureMechanicalPass = REQUESTED_LAYERS.every((layer) => analysisLayerStatuses[layer] === "succeeded")
      && correctedSuggestionCount === 1
      && duplicateKeepCount === 1
      && materialSuitabilityCounts.suitable === 1
      && materialSuitabilityCounts["not-suitable"] === 1
      && customerSatisfaction.includes("partially-satisfied")
      && customerSatisfaction.includes("satisfied")
      && revisionProposalCount === 1
      && falseConfirmedFactCount === 0
      && crossCustomerExposureCount === 0
      && originalMutationCount === 0
      && deleteOriginalButtonCount === 0
      && uploadButtonCount === 0
      && publishButtonCount === 0
      && deleteRequestCount === 0
      && uploadRequestCount === 0
      && publishRequestCount === 0
      && externalNetworkRequestCount === 0
      && modelNetworkRequestCount === 0;
    const report: StudioModelBrowserPilotReport = {
      schemaVersion: 1,
      browserEngine: "chromium",
      browserVersion: browser.version(),
      url,
      providerEvidence: "fixture/synthetic",
      syntheticOnly: true,
      modelMode: "offline-fallback",
      modelDegraded: true,
      fixtureMechanicalPass,
      realProviderAcceptance: {
        status: "blocked",
        reason: "approved-local-browser-evidence-not-provided",
      },
      demandGoal,
      postAnalysisMatchVerdict,
      requestedLayers: REQUESTED_LAYERS,
      analysisLayerStatuses,
      analyzedAssetCount: analysisPayloads.length,
      correctedSuggestionCount,
      duplicateKeepCount,
      materialSuitabilityCounts,
      customerSatisfaction,
      revisionProposalCount,
      reviewedReceiptBindings,
      falseConfirmedFactCount,
      crossCustomerExposureCount,
      originalSnapshotCount: originalsBefore.size,
      originalMutationCount,
      draftRequestCountBeforeBusinessMatch: workflowEvidence.draftRequestCountBeforeBusinessMatch,
      networkWorkflowOrder: workflowEvidence.networkWorkflowOrder,
      lineage,
      deleteOriginalButtonCount,
      uploadButtonCount,
      publishButtonCount,
      deleteRequestCount,
      uploadRequestCount,
      publishRequestCount,
      externalNetworkRequestCount,
      modelNetworkRequestCount,
      tracePath,
      domSnapshotPath,
      screenshotPath,
      networkLogPath,
      reportPath,
    };
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
    return report;
  } finally {
    if (!traceStopped) await context.tracing.stop({ path: tracePath }).catch(() => undefined);
    await context.close().catch(() => undefined);
    await browser.close().catch(() => undefined);
    await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
  }
}

async function listenOnRandomNonProductionPort(
  server: ReturnType<typeof createStudioServer>,
): Promise<number> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const port = randomInt(43_000, 55_000);
    if (port === 4_287) continue;
    try {
      await listenStudioServer(server, port, "127.0.0.1");
      return port;
    } catch (error) {
      if (!hasCode(error, "EADDRINUSE")) throw error;
    }
  }
  throw new Error("studio-model-browser-port-unavailable");
}

function fixtureWorker(): StudioModelWorker {
  return {
    async run(job) {
      return fixtureReceipt(job);
    },
    async health() {
      return { mode: "offline-fallback", installed: false, modelName: null, checkpointDigest: null };
    },
  };
}

function fixtureReceipt(job: ModelAnalysisJobV1): ModelAnalysisReceiptV1 {
  const { requestedAt: _requestedAt, ...binding } = job;
  const checkpointDigest = job.layer === "technical" ? null : "c".repeat(64);
  const qualitySignals: Record<string, unknown> = job.layer === "technical"
    ? { durationMs: job.sourceEndMs, hasVideo: true, hasAudio: true, width: 180, height: 320 }
    : job.layer === "scene"
      ? { scenes: [{
          startMs: job.sourceStartMs,
          endMs: job.sourceEndMs,
          representativeFrameMs: job.sourceStartMs + Math.floor((job.sourceEndMs - job.sourceStartMs) / 2),
        }] }
      : job.layer === "transcript"
        ? { detectedLanguage: "zh", spans: [{
            startMs: job.sourceStartMs,
            endMs: Math.min(job.sourceEndMs, job.sourceStartMs + 2_000),
            text: "合成设计灵感讲解",
          }] }
        : job.layer === "embedding"
          ? {
              embedding: [1, 0],
              dimension: 2,
              representativeFrames: [{
                sourceDigest: job.sourceDigest,
                startMs: job.sourceStartMs,
                endMs: job.sourceEndMs,
                representativeFrameMs: job.sourceStartMs + Math.floor((job.sourceEndMs - job.sourceStartMs) / 2),
                representativeFrameDigest: "f".repeat(64),
              }],
              artifact: {
                id: "fixture-openclip",
                version: "fixture-v1",
                sha256: checkpointDigest,
              },
            }
          : {
              uncertainty: ["合成材质仍需人工确认"],
              demandEvidenceCandidates: [{
                requirement: "合成商品特写",
                reason: "合成画面提供候选证据",
              }],
              forbiddenContentWarnings: [],
            };
  const modelName = job.layer === "embedding" ? "fixture-openclip" : `fixture-${job.layer}`;
  return {
    ...binding,
    adapterName: `${job.layer}-fixture-adapter`,
    modelName,
    modelVersion: "fixture-v1",
    checkpointDigest,
    inputDigest: job.sourceDigest,
    promptVersion: `${job.layer}-fixture-prompt-v1`,
    observations: job.layer === "vision" ? ["合成画面可能包含商品近景"] : [],
    suggestedTags: job.layer === "vision" ? ["合成商品特写"] : [],
    qualitySignals,
    confidence: 0.8,
    warnings: ["fixture-provider-evidence-only"],
    startedAt: "2026-09-13T08:00:00.000Z",
    completedAt: "2026-09-13T08:00:00.010Z",
    runtimeMs: 10,
    status: "succeeded",
    failure: null,
    synthetic: true,
    providerMode: "offline-fallback",
  };
}

function waitForApiResponse(page: Page, pathname: string) {
  return page.waitForResponse((response) =>
    response.request().method() === "POST" && new URL(response.url()).pathname === pathname,
  { timeout: 120_000 });
}

async function waitForStatus(page: Page, text: string): Promise<void> {
  await page.locator("#studio-status").filter({ hasText: text }).waitFor({ timeout: 120_000 });
}

function publicNetworkEntry(request: Request) {
  let postData: unknown = null;
  if (request.method() === "POST") {
    try {
      const value = request.postDataJSON();
      postData = redactNetworkBody(value);
    } catch {
      postData = "unparsed-json-body";
    }
  }
  return {
    method: request.method(),
    url: request.url(),
    resourceType: request.resourceType(),
    postData,
  };
}

function auditedWorkflowEvidence(network: readonly PublicNetworkEntry[]) {
  const posts = network.map((entry, index) => ({
    entry,
    index,
    path: new URL(entry.url).pathname,
    body: isRecord(entry.postData) ? entry.postData : {},
  })).filter((row) => row.entry.method === "POST");
  const all = (path: string) => posts.filter((row) => row.path === path);
  const analyses = all("/api/studio/analysis/jobs");
  const reviews = all("/api/studio/analysis/review");
  const suitability = all("/api/studio/analysis/suitability");
  const matches = all("/api/studio/matches");
  const briefs = all("/api/studio/briefs");
  const drafts = all("/api/studio/drafts");
  const feedback = all("/api/studio/customer-feedback");
  const proposals = all("/api/studio/revision-proposals");
  const confirmations = all("/api/studio/revision-proposals/confirm");
  const businessMatch = matches.at(-1);
  if (analyses.length !== 2 || reviews.length !== 2 || suitability.length !== 2 ||
      matches.length < 2 || briefs.length !== 1 || drafts.length !== 2 || feedback.length !== 2 ||
      proposals.length !== 1 || confirmations.length !== 1 || !businessMatch) {
    throw new Error("studio-model-browser-workflow-request-count-invalid");
  }
  const named = [
    ["analysis", analyses[0]],
    ["analysis", analyses[1]],
    ["correct-suggestion", reviews.find((row) => row.body.action === "correct-suggestion")],
    ["keep-duplicate", reviews.find((row) => row.body.action === "keep-from-duplicate-group")],
    ["suitable", suitability.find((row) => row.body.decision === "suitable")],
    ["not-suitable", suitability.find((row) => row.body.decision === "not-suitable")],
    ["business-match", businessMatch],
    ["brief-v1", briefs[0]],
    ["drafts-v1", drafts[0]],
    ["partial-feedback-v1", feedback.find((row) => row.body.satisfaction === "partially-satisfied")],
    ["revision-proposal", proposals[0]],
    ["confirm-revision", confirmations[0]],
    ["drafts-v2", drafts[1]],
    ["satisfied-feedback-v2", feedback.find((row) => row.body.satisfaction === "satisfied")],
  ] as const;
  if (named.some(([, row]) => !row)) throw new Error("studio-model-browser-workflow-request-missing");
  const ordered = [...named].sort((left, right) => left[1]!.index - right[1]!.index);
  const networkWorkflowOrder = ordered.map(([name]) => name);
  const expected = named.map(([name]) => name);
  if (JSON.stringify(networkWorkflowOrder) !== JSON.stringify(expected)) {
    throw new Error(`studio-model-browser-workflow-order-invalid:${networkWorkflowOrder.join(",")}`);
  }
  const draftRequestCountBeforeBusinessMatch = drafts.filter((row) => row.index < businessMatch.index).length;
  if (draftRequestCountBeforeBusinessMatch !== 0) {
    throw new Error("studio-model-browser-draft-before-business-match");
  }
  return { networkWorkflowOrder, draftRequestCountBeforeBusinessMatch };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function redactNetworkBody(value: unknown): unknown {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  return Object.fromEntries(Object.entries(value).map(([key, entry]) => [
    key,
    /token|path|authorization|customerId|sessionId|actorId/iu.test(key) ? "[redacted]" : entry,
  ]));
}

function hasCode(error: unknown, code: string): boolean {
  return Boolean(error && typeof error === "object" && "code" in error && error.code === code);
}

export type StudioOriginalSnapshotEntry = Readonly<{
  assetIds: readonly string[];
  declaredDigest: string;
  declaredByteLength: number;
  exists: boolean;
  regularFile: boolean;
  actualDigest: string | null;
  actualByteLength: number | null;
}>;

export type StudioOriginalSnapshot = ReadonlyMap<string, StudioOriginalSnapshotEntry>;

export async function snapshotStudioOriginals(rootValue: string): Promise<StudioOriginalSnapshot> {
  const root = resolve(rootValue);
  const assetPaths: string[] = [];
  await walk(root);
  const result = new Map<string, StudioOriginalSnapshotEntry>();
  for (const assetPath of assetPaths.sort(codePointCompare)) {
    const asset = parseOriginalRecord(await readFile(assetPath, "utf8"), "asset");
    const customerRoot = resolve(dirname(assetPath), "../..");
    assertContained(root, customerRoot);
    const blobRecordPath = join(customerRoot, "blobs", asset.blobId, "blob.json");
    const blob = parseOriginalRecord(await readFile(blobRecordPath, "utf8"), "blob");
    if (blob.id !== asset.blobId || blob.digest !== asset.digest || blob.byteLength !== asset.byteLength) {
      throw new Error("studio-model-browser-original-record-mismatch");
    }
    const originalPath = join(dirname(blobRecordPath), "original.bin");
    assertContained(root, originalPath);
    const key = relative(root, originalPath);
    let exists = true;
    let regularFile = false;
    let actualDigest: string | null = null;
    let actualByteLength: number | null = null;
    try {
      const [metadata, bytes] = await Promise.all([lstat(originalPath), readFile(originalPath)]);
      regularFile = metadata.isFile() && !metadata.isSymbolicLink();
      actualDigest = createHash("sha256").update(bytes).digest("hex");
      actualByteLength = bytes.byteLength;
    } catch (error) {
      if (!hasCode(error, "ENOENT")) throw error;
      exists = false;
    }
    const prior = result.get(key);
    result.set(key, {
      assetIds: [...new Set([...(prior?.assetIds ?? []), asset.id])].sort(codePointCompare),
      declaredDigest: asset.digest,
      declaredByteLength: asset.byteLength,
      exists,
      regularFile,
      actualDigest,
      actualByteLength,
    });
  }
  return result;

  async function walk(directory: string): Promise<void> {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        await walk(path);
      } else if (entry.isFile() && entry.name === "asset.json") {
        assetPaths.push(path);
      }
    }
  }
}

export function countStudioOriginalMutations(
  before: StudioOriginalSnapshot,
  after: StudioOriginalSnapshot,
): number {
  return [...before].filter(([path, snapshot]) =>
    JSON.stringify(after.get(path) ?? null) !== JSON.stringify(snapshot)).length;
}

export function countStudioOriginalIntegrityViolations(snapshot: StudioOriginalSnapshot): number {
  return [...snapshot.values()].filter((entry) =>
    !entry.exists || !entry.regularFile || entry.actualDigest !== entry.declaredDigest ||
    entry.actualByteLength !== entry.declaredByteLength).length;
}

function changedDigestCount(before: StudioOriginalSnapshot, after: StudioOriginalSnapshot): number {
  return countStudioOriginalMutations(before, after);
}

function parseOriginalRecord(value: string, kind: "asset" | "blob") {
  const record = JSON.parse(value) as Record<string, unknown>;
  const id = record.id;
  const blobId = kind === "asset" ? record.blobId : record.id;
  const digest = record.digest;
  const byteLength = record.byteLength;
  if (typeof id !== "string" || !/^[A-Za-z0-9_-]+$/u.test(id) ||
      typeof blobId !== "string" || !/^blob_[a-f0-9]{64}$/u.test(blobId) ||
      typeof digest !== "string" || !/^[a-f0-9]{64}$/u.test(digest) ||
      !Number.isSafeInteger(byteLength) || Number(byteLength) < 0) {
    throw new Error(`studio-model-browser-${kind}-record-invalid`);
  }
  return { id, blobId, digest, byteLength: Number(byteLength) };
}

function assertContained(root: string, path: string): void {
  const value = relative(root, path);
  if (value === ".." || value.startsWith(`..${sep}`) || resolve(path) === root) {
    throw new Error("studio-model-browser-original-path-outside-root");
  }
}

function codePointCompare(left: string, right: string): number {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0)!);
  const rightPoints = Array.from(right, (value) => value.codePointAt(0)!);
  for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index]! - rightPoints[index]!;
  }
  return leftPoints.length - rightPoints.length;
}

async function runCli(): Promise<void> {
  const args = process.argv.slice(2).filter((argument) => argument !== "--");
  const outputIndex = args.indexOf("--output");
  const outputDirectory = args[outputIndex + 1];
  if (outputIndex === -1 || !outputDirectory || outputDirectory.startsWith("--")) {
    throw new Error("studio-model-browser-output-required");
  }
  const report = await runStudioModelBrowserPilot(outputDirectory);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  if (!report.fixtureMechanicalPass) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await runCli();
}
