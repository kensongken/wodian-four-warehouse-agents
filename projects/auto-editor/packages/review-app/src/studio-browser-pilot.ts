import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, realpath, rm, stat, writeFile } from "node:fs/promises";
import type { AddressInfo } from "node:net";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

import { chromium } from "playwright-core";
import ffmpegPath from "ffmpeg-static";

import {
  ReferenceStyleAnalysisSchema,
  type ModelAnalysisJobV1,
  type ModelAnalysisReceiptV1,
  type ReferenceRightsDeclaration,
  type ReferenceSample,
  type ReferenceVariantKind,
} from "@wearable/contracts";
import {
  JsonReferenceSampleStore,
  OFFLINE_STUDIO_CAPABILITY,
  ReferenceGuidedEditor,
  ReferenceSampleService,
  createReferenceMaterialMappingPlan,
  interpretReferencePromptRevision,
  renderReferenceVariants,
  type ReferenceEditorRenderRequest,
  type ReferenceMappingAssetSnapshot,
  type ReferenceRenderAsset,
  type StudioModelWorker,
  type StudioScope,
} from "@wearable/mobile-agent";

import {
  createStudioServer,
  listenStudioServer,
  type StudioReferenceRuntimeFactory,
} from "./studio-server";
import type { StudioReleaseMetadata } from "./studio-release";

type BrowserDraft = Readonly<{ id: string; mediaUrl: string }>;
type PilotReferenceArtifact = Readonly<{
  kind: ReferenceVariantKind;
  draftId: string;
  draftVersion: number;
  outputDigest: string;
}>;
type BrowserDemandEvidence = Readonly<{
  contentType: "single-product";
  goal: string;
  targetAudience: string;
  desiredEffect: string;
  mustInclude: string[];
  mustAvoid: string[];
  submittedBy: "customer-self-declared";
}>;

export type StudioBrowserPilotReport = Readonly<{
  syntheticOnly: true;
  automaticPublishing: false;
  release: StudioReleaseMetadata;
  releaseText: string;
  browserEngine: "chromium";
  traceFormat: "playwright";
  replayCommand: "node packages/review-app/node_modules/playwright-core/cli.js show-trace studio-browser-trace.zip";
  browserVersion: string;
  url: string;
  workflowSteps: string[];
  demandGoal: string;
  demandRequestBody: BrowserDemandEvidence;
  persistedDemand: BrowserDemandEvidence;
  matchVerdictText: string;
  matchVerdictClass: string;
  inventoryItemCount: number;
  opportunityCount: number;
  briefVersions: [number, number];
  initialDraftCount: number;
  externalEditUi: Readonly<{
    directActions: ["通过这版", "让 AI 修改", "去剪映精修"];
    handoffCreated: true;
    packagePrepared: true;
    oneClickOpenAttempted: true;
    packageFolderOpened: true;
    returnFolderOpened: true;
    platformVariant: "editorial-master";
    platformLabelVisible: true;
    mode: "file-package";
    degradedReason: "jianying-not-installed";
    falseImportClaim: false;
  }>;
  revisedDraftCount: number;
  initialDrafts: Array<{ id: string; mediaRoute: string }>;
  revisedDrafts: Array<{ id: string; mediaRoute: string }>;
  videoPlayback: Array<{ draftId: string; currentTime: number; paused: boolean; readyState: number }>;
  mediaResponses: Array<{ draftId: string; method: string; status: number; contentType: string; url: string }>;
  lineageDrawerCount: number;
  lineageRows: number;
  adoptDisabledAfterSatisfied: boolean[];
  customerFeedbackCount: number;
  adoptedDraftId: string;
  adoptedDraftVersion: number;
  adoptionResponse: { draftId: string; action: "adopt"; publishClaimed: false };
  finalV2DraftStatuses: Array<{ draftId: string; status: string }>;
  adoptionStatusText: string;
  revisionProposalText: string;
  confirmedProposalText: string;
  renderedProposalText: string;
  v2CreatedBeforeInternalConfirmation: boolean;
  playedVideoCount: number;
  draftPostCountAfterPartialFeedback: number;
  draftPostCountAfterProposal: number;
  draftPostCountAfterConfirmation: number;
  draftPostCountAfterExplicitRender: number;
  historyCounts: Record<string, number>;
  viewportWidth: number;
  documentScrollWidth: number;
  narrowPaneViewportWidth: 150;
  narrowPaneDocumentScrollWidth: number;
  focusOutline: string;
  publishButtonCount: number;
  publishRequestCount: number;
  networkActions: string[];
  historyDrawerOpenAtCapture: boolean;
  historyVisibleAtCapture: boolean;
  adoptionVisibleAtCapture: boolean;
  tracePath: string;
  screenshotPath: string;
  domSnapshotPath: string;
  networkLogPath: string;
  reportPath: string;
  referenceJourney: Readonly<{
    evidenceScope: "local-synthetic-browser-pilot";
    modelEvidenceScope: "synthetic-test-worker";
    captureViewportWidth: 1440;
    mobileCaptureViewportWidth: 320;
    sourceKind: "external-style-only";
    sourceKindReadOnly: boolean;
    samplePreviewPlayed: boolean;
    sampleVideoPreservedAcrossRefresh: boolean;
    draftVideosPreservedAcrossRefresh: boolean;
    mappingConfirmed: boolean;
    variantKinds: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"];
    variantDraftIds: string[];
    playedVariantCount: number;
    executionDimensions: ["rhythm", "transitions", "narrative", "audio"];
    capabilityLabel: string;
    capabilityMode: "offline-fallback";
    warningCodes: string[];
    capabilityWarningVisible: boolean;
    mappingConfirmRetry: Readonly<{
      operationKeys: [string, string];
      mappingIdentities: [string, string];
    }>;
    renderRetry: Readonly<{
      operationKeys: [string, string];
      reportIdentities: [string, string];
      renderDelegateInvocationCount: number;
    }>;
    approvalRetry: Readonly<{
      variant: "wechat-channels";
      responseDropped: true;
      operationKeys: [string, string];
      approvalIdentities: [string, string];
    }>;
    platformRevision: Readonly<{
      target: "wechat-channels";
      prompt: "开头缩短 1 秒";
      digestChanged: true;
      priorDraftId: string;
      revisedDraftId: string;
      priorDigest: string;
      revisedDigest: string;
      unaffectedDraftIdsPreserved: true;
      oldMediaStatus: 409;
      oldApprovalInvalidated: true;
    }>;
    currentApproval: Readonly<{
      approvedArtifactKinds: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"];
      approvalIds: [string, string, string, string];
      status: "approved";
      publishClaimed: false;
    }>;
    withdrawal: Readonly<{
      assetId: string;
      status: 201;
      mobileStatus: 409;
      reviseStatus: 409;
      approveStatus: 409;
      mediaStatus: 409;
      playerCountAfterFailure: 0;
      actionsDisabledAfterFailure: true;
      promptPreservedAfterFailure: true;
      currentApprovalAuditInvalidated: true;
      sampleInvalidated: true;
      mappingInvalidated: true;
    }>;
    sampleMediaRequestCount: number;
    variantMediaRequestCounts: Array<{ draftId: string; count: number }>;
    reportDrawerOpenAtCapture: boolean;
    versionIds: Readonly<{
      sampleId: string;
      rightsId: string;
      analysisId: string;
      selectionIdBeforeChange: string;
      mappingPlanId: string;
      executionReportId: string;
    }>;
    mediaResponses: Array<{ draftId: string; method: string; status: number; contentType: string; url: string }>;
    screenshotPath: string;
    mobileScreenshotPath: string;
    domSnapshotPath: string;
  }>;
}>;

export function defaultStudioBrowserExecutable(): string {
  const configured = process.env.STUDIO_BROWSER_EXECUTABLE?.trim();
  if (configured) return resolve(configured);
  const candidates = process.platform === "darwin"
    ? [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
      ]
    : process.platform === "win32"
      ? [
          "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
          "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        ]
      : ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"];
  const executable = candidates.find((candidate) => existsSync(candidate));
  if (!executable) throw new Error("studio-browser-executable-unavailable");
  return executable;
}

export async function runStudioBrowserPilot(
  outputDirectoryValue: string,
  options: Readonly<{ executablePath?: string }> = {},
): Promise<StudioBrowserPilotReport> {
  const outputDirectory = resolve(outputDirectoryValue);
  const runtimeRoot = join(outputDirectory, "runtime");
  const tracePath = join(outputDirectory, "studio-browser-trace.zip");
  const screenshotPath = join(outputDirectory, "studio-browser.png");
  const domSnapshotPath = join(outputDirectory, "studio-browser-dom.html");
  const networkLogPath = join(outputDirectory, "studio-browser-network.json");
  const reportPath = join(outputDirectory, "studio-browser-report.json");
  const referenceScreenshotPath = join(outputDirectory, "studio-reference-browser.png");
  const referenceMobileScreenshotPath = join(outputDirectory, "studio-reference-browser-mobile.png");
  const referenceDomSnapshotPath = join(outputDirectory, "studio-reference-browser-dom.html");
  const referenceFixturePath = join(outputDirectory, "studio-reference-fixture.mov");
  await mkdir(outputDirectory, { recursive: true });
  await makeSyntheticReferenceMov(referenceFixturePath);

  let referenceRenderInvocationCount = 0;
  let readReferenceSnapshot: (() => Promise<Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>>) | null = null;

  const server = createStudioServer({
    rootDir: runtimeRoot,
    now: () => "2026-09-11T08:00:00.000Z",
    modelWorker: createBrowserPilotReferenceWorker(),
    // Keep the synthetic browser journey deterministic even when Jianying is installed on the host.
    findJianyingApp: async () => null,
    openExternalEditTarget: async () => undefined,
    referenceRuntimeFactory: async (input) => {
      const runtime = await createBrowserPilotReferenceRuntime(input, () => {
        referenceRenderInvocationCount += 1;
      });
      readReferenceSnapshot = () => runtime.store.snapshot() as Promise<Awaited<
        ReturnType<JsonReferenceSampleStore["snapshot"]>
      >>;
      return runtime;
    },
  });
  await listenStudioServer(server, 0, "127.0.0.1");
  const address = server.address() as AddressInfo | null;
  if (!address || typeof address === "string") throw new Error("studio-browser-server-address-missing");
  const url = `http://127.0.0.1:${address.port}/studio`;
  const browser = await chromium.launch({
    executablePath: options.executablePath ?? defaultStudioBrowserExecutable(),
    headless: true,
    args: ["--autoplay-policy=no-user-gesture-required"],
  });
  const context = await browser.newContext({ viewport: { width: 320, height: 900 } });
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
  const page = await context.newPage();
  const network: Array<{ method: string; status: number; contentType: string; url: string }> = [];
  const networkActions: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/api/studio/feedback") && request.method() === "POST") {
      const body = request.postDataJSON() as { action?: unknown };
      if (typeof body.action === "string") networkActions.push(body.action);
    }
  });
  page.on("response", (response) => {
    network.push({
      method: response.request().method(),
      status: response.status(),
      contentType: response.headers()["content-type"] ?? "",
      url: response.url(),
    });
  });
  try {
    const sessionResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/api/studio/session") && response.request().method() === "POST");
    await page.goto(url, { waitUntil: "domcontentloaded" });
    const sessionPayload = await (await sessionResponsePromise).json() as {
      automaticPublishing: boolean;
      release: StudioReleaseMetadata;
    };
    if (sessionPayload.automaticPublishing !== false) throw new Error("studio-browser-publish-boundary-missing");
    await waitForStatus(page, "已准备 4 段演示素材，可以开始体验");

    const originalViewport = page.viewportSize();
    if (!originalViewport) throw new Error("studio-browser-viewport-missing");
    await page.setViewportSize({ width: 150, height: originalViewport.height });
    const narrowPaneLayout = await page.evaluate(() => ({
      viewportWidth: window.innerWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      customerSelectorRight: document.querySelector(".customer-chip")?.getBoundingClientRect().right ?? 0,
    }));
    if (narrowPaneLayout.viewportWidth !== 150 || narrowPaneLayout.documentScrollWidth > 150 ||
        narrowPaneLayout.customerSelectorRight > 150) {
      throw new Error("studio-browser-narrow-pane-overflow");
    }
    await page.setViewportSize({ width: 750, height: originalViewport.height });
    await page.locator('.studio-bottom-nav [data-studio-tab="works"]').click();
    const desktopWorksContentWidth = await page.locator("#drafts")
      .evaluate((element) => element.getBoundingClientRect().width);
    if (desktopWorksContentWidth < 280) throw new Error("studio-browser-desktop-works-column-collapsed");
    await page.locator('.studio-bottom-nav [data-studio-tab="create"]').click();
    await page.setViewportSize(originalViewport);

    const releaseText = (await page.locator("#studio-release").textContent())?.trim() ?? "";
    const workflowSteps = await page.locator(".studio-step h2").allTextContents();
    const inventoryItemCount = await page.locator(".material-card").count();
    await page.locator("details.advanced-tools > summary").click();
    await page.locator("#studio-add-reference").click();
    const referenceJourney = await runReferenceBrowserJourney(page, network, {
      fixturePath: referenceFixturePath,
      screenshotPath: referenceScreenshotPath,
      mobileScreenshotPath: referenceMobileScreenshotPath,
      domSnapshotPath: referenceDomSnapshotPath,
    }, () => referenceRenderInvocationCount, async () => {
      if (!readReferenceSnapshot) throw new Error("studio-reference-browser-store-unavailable");
      return readReferenceSnapshot();
    });

    const demandInput = {
      contentType: "single-product" as const,
      goal: "让顾客理解设计灵感",
      targetAudience: "准备到店的顾客",
      desiredEffect: "愿意预约到店",
      mustInclude: ["纯色合成商品夹具 1"],
      mustAvoid: ["真实客户姓名"],
      submittedBy: "customer-self-declared" as const,
    };
    const demandRequestPromise = page.waitForRequest((request) =>
      request.url().endsWith("/api/studio/demands") && request.method() === "POST");
    const demandResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/api/studio/demands") && response.request().method() === "POST");
    await page.locator('.studio-bottom-nav [data-studio-tab="create"]').click();
    await page.locator('.studio-bottom-nav [data-studio-tab="works"]').click();
    if (await page.locator(".studio-shell").getAttribute("data-active-tab") !== "works") {
      throw new Error("studio-home-works-shortcut-did-not-navigate");
    }
    const emptyWorks = (await page.locator("#drafts").innerText()).trim();
    if (!emptyWorks.includes("这里会显示你的内容初稿") || !emptyWorks.includes("开始创作")) {
      throw new Error("studio-empty-works-guidance-missing");
    }
    await page.locator('#drafts [data-studio-tab="create"]').click();
    if (await page.locator(".studio-shell").getAttribute("data-active-tab") !== "create") {
      throw new Error("studio-empty-works-create-action-did-not-navigate");
    }
    await page.locator('.studio-bottom-nav [data-studio-tab="create"]').click();
    await page.locator("details.advanced-tools").evaluate((element) => {
      (element as HTMLDetailsElement).open = true;
    });
    await page.locator(".advanced-demand summary").click();
    await page.locator('[name="contentType"]').selectOption(demandInput.contentType);
    await page.locator('[name="goal"]').fill(demandInput.goal);
    await page.locator('[name="targetAudience"]').fill(demandInput.targetAudience);
    await page.locator('[name="desiredEffect"]').fill(demandInput.desiredEffect);
    await page.locator('[name="mustInclude"]').fill(demandInput.mustInclude.join("\n"));
    await page.locator('[name="mustAvoid"]').fill(demandInput.mustAvoid.join("\n"));
    await page.locator('[name="submittedBy"]').selectOption(demandInput.submittedBy);
    await page.locator('#studio-demand-form button[type="submit"]').click();
    const demandRequest = await demandRequestPromise;
    const demandResponse = await demandResponsePromise;
    const demandRequestBody = demandEvidence(demandRequest.postDataJSON());
    const demandResponsePayload = await demandResponse.json() as { demand?: { id?: unknown } };
    const demandId = String(demandResponsePayload.demand?.id ?? "");
    if (!demandId) throw new Error("studio-browser-demand-response-missing");
    await waitForStatus(page, "这次需求已记录，请检查现有素材是否匹配");

    await page.locator("#assess-demand").click();
    await waitForStatus(page, "现有素材适合继续，请核对任务书");
    const readyMatchCard = page.locator("#match-assessment .verdict-ready");
    if (await readyMatchCard.count() !== 1) throw new Error("studio-browser-green-match-card-missing");
    const matchVerdictText = await readyMatchCard.locator(".card-heading strong").innerText();
    const matchVerdictClass = await readyMatchCard.getAttribute("class") ?? "";
    const opportunityCount = await page.locator("#opportunities .decision-card").count();

    await page.locator('.studio-bottom-nav [data-studio-tab="library"]').click();
    await page.locator(".material-details summary").first().click();
    await page.locator("[data-studio-fact-id]").first().check();
    await page.locator('.studio-bottom-nav [data-studio-tab="create"]').click();
    const firstDraftResponse = page.waitForResponse((response) =>
      response.url().endsWith("/api/studio/drafts") && response.request().method() === "POST",
    { timeout: 120_000 });
    await page.locator("#confirm-brief").click();
    const initialDraftsPayload = await (await firstDraftResponse).json() as { drafts: BrowserDraft[] };
    await waitForStatus(page, "2 个平台版本已准备好，请播放后再做决定");
    await page.locator('.studio-shell[data-active-tab="works"] #drafts .draft-card').first().waitFor();
    const firstBriefVersion = await readBriefVersion(page);
    const initialDraftCount = await page.locator(".draft-card").count();
    const initialDrafts = publicDrafts(initialDraftsPayload.drafts);

    const sourceDrawers = page.locator("details.source-drawer");
    const lineageDrawerCount = await sourceDrawers.count();
    for (let index = 0; index < lineageDrawerCount; index += 1) {
      await sourceDrawers.nth(index).locator("summary").click();
    }
    const lineageRows = await page.locator("details.source-drawer li").count();
    const initialVideoPlayback = await playCurrentDraftVideos(page, "#drafts video");

    const directActions = await page.locator(".draft-card").first().locator(".draft-decision-actions button").allTextContents();
    if (directActions.join("|") !== "通过这版|让 AI 修改|去剪映精修") {
      throw new Error("studio-browser-external-edit-actions-invalid");
    }
    await page.locator(".draft-card").first().locator("[data-open-external-edit]").click();
    const externalForm = page.locator("[data-external-edit-form]").first();
    await externalForm.locator('[name="revisionInstruction"]').fill("保留设计师原话，节奏紧凑一些");
    const externalHandoffResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/api/studio/external-edit-handoffs") && response.request().method() === "POST");
    const externalPrepareResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/prepare") && response.request().method() === "POST");
    const externalOpenResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/open") && response.request().method() === "POST");
    await externalForm.locator('button[type="submit"]').click();
    const externalHandoffResponse = await externalHandoffResponsePromise;
    const externalPrepareResponse = await externalPrepareResponsePromise;
    const externalHandoffPayload = await externalHandoffResponse.json() as { handoff?: { id?: unknown } };
    const externalPreparedPayload = await externalPrepareResponse.json() as {
      mode?: unknown; degradedReason?: unknown; handoff?: { status?: unknown };
    };
    const externalOpenPayload = await (await externalOpenResponsePromise).json() as {
      opened?: unknown; jianyingOpened?: unknown; packageFolderOpened?: unknown; returnFolderOpened?: unknown;
    };
    if (!externalHandoffPayload.handoff?.id || externalPreparedPayload.mode !== "file-package"
      || externalPreparedPayload.degradedReason !== "jianying-not-installed"
      || externalPreparedPayload.handoff?.status !== "degraded") {
      throw new Error("studio-browser-external-edit-fallback-invalid");
    }
    if (externalOpenPayload.opened !== true || externalOpenPayload.jianyingOpened !== false
      || externalOpenPayload.packageFolderOpened !== true || externalOpenPayload.returnFolderOpened !== true) {
      throw new Error("studio-browser-external-edit-one-click-open-invalid");
    }
    await page.locator(`[data-external-edit-panel="${initialDraftsPayload.drafts[0]!.id}"]`)
      .getByText(/本机未识别到剪映/).waitFor();
    const externalPanelText = await page.locator(`[data-external-edit-panel="${initialDraftsPayload.drafts[0]!.id}"]`).innerText();
    if (externalPanelText.includes("已自动导入剪映")) throw new Error("studio-browser-false-jianying-import-claim");
    if (!externalPanelText.includes("原视频母版") || !externalPanelText.includes("平台身份已跟随本次交接任务保存")) {
      throw new Error("studio-browser-external-edit-platform-label-missing");
    }
    const externalEditUi: StudioBrowserPilotReport["externalEditUi"] = {
      directActions: ["通过这版", "让 AI 修改", "去剪映精修"],
      handoffCreated: true,
      packagePrepared: true,
      oneClickOpenAttempted: true,
      packageFolderOpened: true,
      returnFolderOpened: true,
      platformVariant: "editorial-master",
      platformLabelVisible: true,
      mode: "file-package",
      degradedReason: "jianying-not-installed",
      falseImportClaim: false,
    };

    await page.locator("[data-open-ai-edit]").nth(0).click();
    const firstFeedback = page.locator("[data-customer-feedback]").nth(0);
    await firstFeedback.locator('[name="satisfaction"]').selectOption("partially-satisfied");
    await firstFeedback.locator('input[value="style"]').check();
    await firstFeedback.locator('[name="comment"]').fill("请让设计师观点更突出");
    await firstFeedback.locator('button[type="submit"]').click();
    await waitForStatus(page, "修改提案已生成，请先内部确认；尚未生成 V2 初稿");
    const draftPostCountAfterPartialFeedback = countDraftPosts(network);
    const revisionProposalText = await page.locator("#revision-proposal").innerText();
    const draftPostCountAfterProposal = countDraftPosts(network);
    const v2CreatedBeforeInternalConfirmation = draftPostCountAfterProposal > 1;

    await page.locator("#confirm-revision-proposal").click();
    await waitForStatus(page, "任务书 V2 已创建；请另外生成新版剪辑方向");
    const confirmedProposalText = await page.locator("#revision-proposal").innerText();
    const secondBriefVersion = await readBriefVersion(page);
    const draftPostCountAfterConfirmation = countDraftPosts(network);

    const revisedDraftResponse = page.waitForResponse((response) =>
      response.url().endsWith("/api/studio/drafts") && response.request().method() === "POST",
    { timeout: 120_000 });
    await page.locator("#make-revision-drafts").click();
    const revisedDraftsPayload = await (await revisedDraftResponse).json() as { drafts: BrowserDraft[] };
    await waitForStatus(page, "2 个平台版本已准备好，请播放后再做决定");
    const renderedProposalText = await page.locator("#revision-proposal").innerText();
    const revisedDraftCount = await page.locator(".draft-card").count();
    const revisedDrafts = publicDrafts(revisedDraftsPayload.drafts);
    const draftPostCountAfterExplicitRender = countDraftPosts(network);
    const revisedVideoPlayback = await playCurrentDraftVideos(page, "#drafts video");
    const videoPlayback = [...initialVideoPlayback, ...revisedVideoPlayback];
    assertExactVideoPlayback(
      videoPlayback,
      [...initialDrafts, ...revisedDrafts].map((draft) => draft.id),
    );

    await page.locator("[data-open-ai-edit]").nth(0).click();
    const satisfiedFeedback = page.locator("[data-customer-feedback]").nth(0);
    await satisfiedFeedback.locator('[name="satisfaction"]').selectOption("satisfied");
    const finalHistoryResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/api/studio/history") && response.request().method() === "POST");
    await satisfiedFeedback.locator('button[type="submit"]').click();
    const finalHistoryPayload = await (await finalHistoryResponsePromise).json() as {
      history?: { demands?: unknown[] };
    };
    const persistedDemandValue = finalHistoryPayload.history?.demands?.find((value) =>
      typeof value === "object" && value !== null && "id" in value && value.id === demandId);
    const persistedDemand = demandEvidence(persistedDemandValue);
    await waitForStatus(page, "客户已标记满意，只解锁这一份精确初稿的采用");
    const adoptDisabledAfterSatisfied = await page.locator("[data-adopt-draft]")
      .evaluateAll((buttons) => buttons.map((button) => (button as HTMLButtonElement).disabled));
    const adoptedDraftId = revisedDrafts[0]!.id;
    const adoptionResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/api/studio/feedback") && response.request().method() === "POST");
    const adoptionHistoryResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/api/studio/history") && response.request().method() === "POST");
    await page.locator(`[data-adopt-draft="${adoptedDraftId}"]`).click();
    const adoptionPayload = await (await adoptionResponsePromise).json() as {
      feedback?: { draftId?: unknown; action?: unknown; publishClaimed?: unknown };
    };
    const adoptionFeedback = adoptionPayload.feedback;
    if (adoptionFeedback?.draftId !== adoptedDraftId || adoptionFeedback.action !== "adopt" ||
        adoptionFeedback.publishClaimed !== false) {
      throw new Error("studio-browser-adoption-response-invalid");
    }
    const adoptionResponse = {
      draftId: adoptedDraftId,
      action: "adopt" as const,
      publishClaimed: false as const,
    };
    const adoptionHistoryPayload = await (await adoptionHistoryResponsePromise).json() as {
      history?: { drafts?: Array<{ id?: unknown; version?: unknown; status?: unknown }> };
    };
    const finalV2DraftStatuses = revisedDrafts.map((draft) => {
      const persisted = adoptionHistoryPayload.history?.drafts?.find((value) =>
        value.id === draft.id && value.version === secondBriefVersion);
      return { draftId: draft.id, status: typeof persisted?.status === "string" ? persisted.status : "" };
    });
    if (finalV2DraftStatuses.filter((draft) => draft.status === "adopted").length !== 1 ||
        finalV2DraftStatuses.filter((draft) => draft.status === "rejected").length !== 1 ||
        finalV2DraftStatuses.find((draft) => draft.draftId === adoptedDraftId)?.status !== "adopted") {
      throw new Error("studio-browser-adoption-history-invalid");
    }
    await waitForStatus(page, "已记录“采用这版”，采用仍不是发布");
    const adoptionStatusText = await page.locator("#studio-status").innerText();
    const customerFeedbackCount = await page.locator("#studio-history .history-group")
      .filter({ has: page.locator("h3", { hasText: "客户反馈" }) })
      .locator("li").count();

    const historyCounts = await page.locator("#studio-history .history-group").evaluateAll((groups) =>
      Object.fromEntries(groups.map((group) => [
        group.querySelector("h3")?.textContent ?? "",
        group.querySelectorAll("li").length,
      ])));
    const layout = await page.evaluate(() => {
      const customer = document.querySelector<HTMLSelectElement>("#studio-customer");
      customer?.focus();
      const style = customer ? getComputedStyle(customer) : null;
      return {
        viewportWidth: window.innerWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        focusOutline: style ? `${style.outlineStyle} ${style.outlineWidth}` : "",
      };
    });
    const publishButtonCount = await page.locator(
      "button:has-text('发布'), [data-publish], [formaction*='publish']",
    ).count();
    const publishRequestCount = network.filter((entry) => new URL(entry.url).pathname.includes("publish")).length;

    const finalSourceDrawers = page.locator("details.source-drawer");
    for (let index = 0; index < await finalSourceDrawers.count(); index += 1) {
      if (!await finalSourceDrawers.nth(index).evaluate((drawer) => (drawer as HTMLDetailsElement).open)) {
        await finalSourceDrawers.nth(index).locator("summary").click();
      }
    }
    const historyDrawer = page.locator("details.history-drawer");
    if (!await historyDrawer.evaluate((drawer) => (drawer as HTMLDetailsElement).open)) {
      await historyDrawer.locator("summary").click();
    }
    const historyDrawerOpenAtCapture = await historyDrawer
      .evaluate((drawer) => (drawer as HTMLDetailsElement).open);
    const historyVisibleAtCapture = await page.locator("#studio-history").isVisible();
    const adoptionVisibleAtCapture = await page.locator("#studio-status").isVisible() &&
      (await page.locator("#studio-status").innerText()) === adoptionStatusText;

    await page.screenshot({ path: screenshotPath, fullPage: true });
    await writeFile(domSnapshotPath, `${await page.content()}\n`, "utf8");
    await writeJson(networkLogPath, network);
    const mediaResponses = [...initialDrafts, ...revisedDrafts].map((draft) => {
      const entry = [...network].reverse().find((candidate) =>
        new URL(candidate.url).pathname === draft.mediaRoute && candidate.method === "GET");
      if (!entry) throw new Error(`studio-browser-media-response-missing:${draft.id}`);
      return { draftId: draft.id, ...entry };
    });
    await context.tracing.stop({ path: tracePath });
    const report: StudioBrowserPilotReport = {
      syntheticOnly: true,
      automaticPublishing: false,
      release: sessionPayload.release,
      releaseText,
      browserEngine: "chromium",
      traceFormat: "playwright",
      replayCommand: "node packages/review-app/node_modules/playwright-core/cli.js show-trace studio-browser-trace.zip",
      browserVersion: browser.version(),
      url,
      workflowSteps,
      demandGoal: demandRequestBody.goal,
      demandRequestBody,
      persistedDemand,
      matchVerdictText,
      matchVerdictClass,
      inventoryItemCount,
      opportunityCount,
      briefVersions: [firstBriefVersion, secondBriefVersion],
      initialDraftCount,
      externalEditUi,
      revisedDraftCount,
      initialDrafts,
      revisedDrafts,
      videoPlayback,
      mediaResponses,
      lineageDrawerCount,
      lineageRows,
      adoptDisabledAfterSatisfied,
      customerFeedbackCount,
      adoptedDraftId,
      adoptedDraftVersion: secondBriefVersion,
      adoptionResponse,
      finalV2DraftStatuses,
      adoptionStatusText,
      revisionProposalText,
      confirmedProposalText,
      renderedProposalText,
      v2CreatedBeforeInternalConfirmation,
      playedVideoCount: videoPlayback.length,
      draftPostCountAfterPartialFeedback,
      draftPostCountAfterProposal,
      draftPostCountAfterConfirmation,
      draftPostCountAfterExplicitRender,
      historyCounts,
      ...layout,
      narrowPaneViewportWidth: 150,
      narrowPaneDocumentScrollWidth: narrowPaneLayout.documentScrollWidth,
      publishButtonCount,
      publishRequestCount,
      networkActions,
      historyDrawerOpenAtCapture,
      historyVisibleAtCapture,
      adoptionVisibleAtCapture,
      tracePath,
      screenshotPath,
      domSnapshotPath,
      networkLogPath,
      reportPath,
      referenceJourney,
    };
    await writeJson(reportPath, report);
    return report;
  } finally {
    await context.close().catch(() => undefined);
    await browser.close().catch(() => undefined);
    await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
  }
}

async function runReferenceBrowserJourney(
  page: import("playwright-core").Page,
  network: Array<{ method: string; status: number; contentType: string; url: string }>,
  paths: Readonly<{
    fixturePath: string;
    screenshotPath: string;
    mobileScreenshotPath: string;
    domSnapshotPath: string;
  }>,
  readRenderInvocationCount: () => number,
  readWorkflowSnapshot: () => Promise<Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>>,
): Promise<StudioBrowserPilotReport["referenceJourney"]> {
  const uploadResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/studio/reference-samples/import") && response.request().method() === "POST");
  await page.locator("#reference-file").setInputFiles(paths.fixturePath);
  await page.locator('#reference-upload-form [name="sourceKind"]').selectOption("external-style-only");
  await page.locator('#reference-upload-form button[type="submit"]').click();
  const uploadPayload = await (await uploadResponsePromise).json() as {
    sample?: { id?: unknown; sourceAssetId?: unknown };
  };
  const sampleId = requirePilotId(uploadPayload.sample?.id, "studio-reference-browser-sample-missing");
  const sampleSourceAssetId = requirePilotId(
    uploadPayload.sample?.sourceAssetId,
    "studio-reference-browser-source-asset-missing",
  );
  await waitForReferenceStatus(page, "样片已入库；请播放预览并确认来源范围。");

  const samplePreview = page.locator("#reference-sample-card video");
  await samplePreview.evaluate(async (video: HTMLVideoElement) => {
    video.muted = true;
    await video.play();
  });
  await page.waitForFunction(() => {
    const video = document.querySelector<HTMLVideoElement>("#reference-sample-card video");
    return Boolean(video && video.currentTime > 0 && video.readyState >= 3);
  });
  const samplePreviewPlayed = await samplePreview.evaluate((video: HTMLVideoElement) =>
    video.currentTime > 0 && video.readyState >= 3);
  const sourceKindReadOnly = await page.locator("#reference-rights-source-kind").evaluate((control) =>
    control.tagName === "OUTPUT" && control.textContent?.trim() === "外部样片，只学风格");
  await samplePreview.evaluate((video: HTMLVideoElement) => {
    video.dataset.browserPilotNode = "sample-preview";
  });
  const sampleTimeBeforeRefresh = await samplePreview.evaluate((video: HTMLVideoElement) => video.currentTime);
  const sampleHistoryRefresh = page.waitForResponse((response) =>
    response.url().endsWith("/api/studio/reference-history") && response.request().method() === "GET");
  await page.locator("#refresh-reference-history").click();
  await sampleHistoryRefresh;
  const sampleVideoPreservedAcrossRefresh = await page.locator("#reference-sample-card video")
    .evaluate((video: HTMLVideoElement, before) =>
      video.dataset.browserPilotNode === "sample-preview" && video.currentTime >= Number(before) - 0.01,
    sampleTimeBeforeRefresh);

  const rightsResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/studio/reference-rights") && response.request().method() === "POST");
  await page.locator("#declare-reference-rights").click();
  const rightsPayload = await (await rightsResponsePromise).json() as { rights?: { id?: unknown } };
  const rightsId = requirePilotId(rightsPayload.rights?.id, "studio-reference-browser-rights-missing");
  await waitForReferenceStatus(page, "来源范围已确认；现在可以分析样片。");

  const analysisResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/studio/reference-analyses") && response.request().method() === "POST", { timeout: 120_000 });
  await page.locator("#analyze-reference").click();
  const analysisResponse = await analysisResponsePromise;
  const analysisPayload = await analysisResponse.json() as {
    error?: unknown;
    analysis?: {
      id?: unknown;
      rules?: Array<{ id?: unknown }>;
      capability?: { label?: unknown; mode?: unknown };
      warnings?: unknown[];
    };
  };
  if (!analysisResponse.ok()) {
    throw new Error(`studio-reference-browser-analysis-failed:${String(analysisPayload.error || analysisResponse.status())}`);
  }
  const analysisId = requirePilotId(analysisPayload.analysis?.id, "studio-reference-browser-analysis-missing");
  const capabilityLabel = requirePilotId(
    analysisPayload.analysis?.capability?.label,
    "studio-reference-browser-capability-missing",
  );
  const capabilityMode = analysisPayload.analysis?.capability?.mode;
  if (capabilityMode !== "offline-fallback") {
    throw new Error("studio-reference-browser-capability-truth-missing");
  }
  const warningCodes = (analysisPayload.analysis?.warnings ?? []).map((value) => String(value));
  await waitForReferenceStatus(page, "风格拆解卡已准备；请勾选这次真正要学的规则。");
  const analysisCardText = await page.locator("#reference-analysis").innerText();
  const capabilityWarningVisible = analysisCardText.includes(capabilityLabel) &&
    analysisCardText.includes(capabilityMode) && warningCodes.every((warning) => analysisCardText.includes(warning));
  if (!capabilityWarningVisible) throw new Error("studio-reference-browser-capability-warning-not-visible");

  const ruleInputs = page.locator("[data-reference-rule-id]");
  const ruleCount = await ruleInputs.count();
  if (ruleCount < 2) throw new Error("studio-reference-browser-rule-subset-unavailable");
  for (let index = ruleCount - 1; index >= 1; index -= 1) {
    await page.locator("[data-reference-rule-id]").nth(index).uncheck();
  }
  const firstSelectionResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/studio/reference-style-selections") && response.request().method() === "POST");
  await page.locator("#confirm-reference-style").click();
  const firstSelectionPayload = await (await firstSelectionResponsePromise).json() as {
    selection?: { id?: unknown };
  };
  const selectionIdBeforeChange = requirePilotId(
    firstSelectionPayload.selection?.id,
    "studio-reference-browser-selection-missing",
  );
  await waitForReferenceStatus(page, "风格规则已确认；可以建立素材匹配计划。");

  const mappingResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/studio/reference-mapping-plans") && response.request().method() === "POST");
  await page.locator("#create-reference-mapping").click();
  const mappingPayload = await (await mappingResponsePromise).json() as {
    mappingPlan?: { id?: unknown; status?: unknown };
  };
  const mappingPlanId = requirePilotId(mappingPayload.mappingPlan?.id, "studio-reference-browser-mapping-missing");
  await waitForReferenceStatus(page, "匹配计划已生成；请逐条复核后人工确认。");

  const mappingConfirmOperationKeys: string[] = [];
  const mappingConfirmIdentities: string[] = [];
  let mappingConfirmAttempts = 0;
  await page.route("**/api/studio/reference-mapping-plans/confirm", async (route, request) => {
    mappingConfirmAttempts += 1;
    const body = request.postDataJSON() as { operationKey?: unknown };
    mappingConfirmOperationKeys.push(String(body.operationKey ?? ""));
    if (mappingConfirmAttempts === 1) {
      const persisted = await route.fetch();
      const payload = await persisted.json() as { mappingPlan?: Record<string, unknown> };
      mappingConfirmIdentities.push(mappingPlanRetryIdentity(payload.mappingPlan));
      await route.abort("failed");
      return;
    }
    await route.continue();
  });
  await page.locator("#confirm-reference-mapping").click();
  await waitForReferenceStatus(page, "本次参考样片操作未完成，已保留上一个有效阶段。");
  const mappingConfirmResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/studio/reference-mapping-plans/confirm") && response.request().method() === "POST");
  await page.locator("#confirm-reference-mapping").click();
  const mappingConfirmPayload = await (await mappingConfirmResponsePromise).json() as {
    mappingPlan?: Record<string, unknown>;
  };
  mappingConfirmIdentities.push(mappingPlanRetryIdentity(mappingConfirmPayload.mappingPlan));
  await page.unroute("**/api/studio/reference-mapping-plans/confirm");
  const mappingConfirmed = mappingConfirmPayload.mappingPlan?.id === mappingPlanId &&
    mappingConfirmPayload.mappingPlan?.status === "confirmed";
  if (!mappingConfirmed) throw new Error("studio-reference-browser-mapping-confirmation-missing");
  if (mappingConfirmOperationKeys.length !== 2 ||
      mappingConfirmOperationKeys[0] !== mappingConfirmOperationKeys[1] ||
      mappingConfirmIdentities.length !== 2 ||
      mappingConfirmIdentities[0] !== mappingConfirmIdentities[1]) {
    throw new Error("studio-reference-browser-mapping-confirm-retry-invalid");
  }
  await waitForReferenceStatus(page, "匹配计划已人工确认；四版渲染闸门已解锁。");

  const renderOperationKeys: string[] = [];
  const renderIdentities: string[] = [];
  const renderRequestListener = (request: import("playwright-core").Request) => {
    if (!request.url().endsWith("/api/studio/reference-drafts") || request.method() !== "POST") return;
    const body = request.postDataJSON() as { operationKey?: unknown };
    renderOperationKeys.push(String(body.operationKey ?? ""));
  };
  page.on("request", renderRequestListener);
  let reportFetchAttempts = 0;
  await page.route("**/api/studio/reference-execution-reports/*", async (route) => {
    reportFetchAttempts += 1;
    if (reportFetchAttempts === 1) {
      await route.abort("failed");
      return;
    }
    await route.continue();
  });
  const firstRenderResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/studio/reference-drafts") && response.request().method() === "POST", { timeout: 300_000 });
  await page.locator("#render-reference-drafts").click();
  const firstRenderResponse = await firstRenderResponsePromise;
  const firstRenderPayload = await firstRenderResponse.json() as { report?: Record<string, unknown> };
  renderIdentities.push(reportRetryIdentity(firstRenderPayload.report));
  await waitForReferenceStatus(page, "本次参考样片操作未完成，已保留上一个有效阶段。");
  const renderResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/studio/reference-drafts") && response.request().method() === "POST", { timeout: 300_000 });
  await page.locator("#render-reference-drafts").click();
  const renderResponse = await renderResponsePromise;
  const renderPayload = await renderResponse.json() as {
    error?: unknown;
    report?: {
      id?: unknown;
      version?: unknown;
      artifacts?: Array<{
        kind?: unknown;
        draftId?: unknown;
        draftVersion?: unknown;
        outputDigest?: unknown;
      }>;
      rhythm?: unknown;
      transitions?: unknown;
      narrative?: unknown;
      audio?: unknown;
    };
  };
  if (!renderResponse.ok()) {
    throw new Error(`studio-reference-browser-render-failed:${String(renderPayload.error || renderResponse.status())}`);
  }
  renderIdentities.push(reportRetryIdentity(renderPayload.report as unknown as Record<string, unknown>));
  page.off("request", renderRequestListener);
  await page.unroute("**/api/studio/reference-execution-reports/*");
  const renderDelegateInvocationCount = readRenderInvocationCount();
  if (renderOperationKeys.length !== 2 || renderOperationKeys[0] !== renderOperationKeys[1] ||
      renderIdentities.length !== 2 || renderIdentities[0] !== renderIdentities[1] ||
      renderDelegateInvocationCount !== 1) {
    throw new Error("studio-reference-browser-render-retry-invalid");
  }
  const executionReportId = requirePilotId(
    renderPayload.report?.id,
    "studio-reference-browser-report-missing",
  );
  await waitForReferenceStatus(page, "四个待审版本已生成；请逐片播放并查看执行报告。");
  const artifacts = (renderPayload.report?.artifacts ?? []).map(requirePilotArtifact);
  const variantKinds = artifacts.map((artifact) => artifact.kind);
  if (JSON.stringify(variantKinds) !== JSON.stringify([
    "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
  ])) throw new Error("studio-reference-browser-variant-set-invalid");
  const variantDraftIds = artifacts.map((artifact) => artifact.draftId);

  const playback = await playCurrentDraftVideos(page, "#reference-drafts video");
  assertExactVideoPlayback(playback, variantDraftIds);
  await page.locator("#reference-drafts video").evaluateAll((videos, ids) => {
    videos.forEach((video, index) => {
      (video as HTMLVideoElement).dataset.browserPilotNode = `draft-${ids[index]}`;
    });
  }, variantDraftIds);
  const draftTimesBeforeRefresh = await page.locator("#reference-drafts video")
    .evaluateAll((videos) => videos.map((video) => (video as HTMLVideoElement).currentTime));
  const draftHistoryRefresh = page.waitForResponse((response) =>
    response.url().endsWith("/api/studio/reference-history") && response.request().method() === "GET");
  await page.locator("#refresh-reference-history").click();
  await draftHistoryRefresh;
  const draftVideosPreservedAcrossRefresh = await page.locator("#reference-drafts video")
    .evaluateAll((videos, input) => videos.length === input.ids.length && videos.every((video, index) =>
      (video as HTMLVideoElement).dataset.browserPilotNode === `draft-${input.ids[index]}` &&
      (video as HTMLVideoElement).currentTime >= input.times[index]! - 0.01), {
      ids: variantDraftIds,
      times: draftTimesBeforeRefresh,
    });
  const reportDrawer = page.locator("#reference-report-drawer");
  if (!await reportDrawer.evaluate((drawer) => (drawer as HTMLDetailsElement).open)) {
    await reportDrawer.locator(":scope > summary").click();
  }
  const reportDrawerOpenAtCapture = await reportDrawer.evaluate((drawer) =>
    (drawer as HTMLDetailsElement).open);
  const mobileViewport = page.viewportSize();
  if (mobileViewport?.width !== 320) throw new Error("studio-reference-browser-mobile-viewport-invalid");
  await page.screenshot({ path: paths.mobileScreenshotPath, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: paths.screenshotPath, fullPage: true });
  await writeFile(paths.domSnapshotPath, `${await page.content()}\n`, "utf8");
  if (mobileViewport) await page.setViewportSize(mobileViewport);
  const mediaResponses = variantDraftIds.map((draftId) => {
    const mediaRoute = `/api/studio/reference-drafts/${encodeURIComponent(draftId)}/media`;
    const entry = [...network].reverse().find((candidate) =>
      new URL(candidate.url).pathname === mediaRoute && candidate.method === "GET");
    if (!entry) throw new Error(`studio-reference-browser-media-response-missing:${draftId}`);
    return { draftId, ...entry };
  });
  const sampleMediaPath = `/api/studio/reference-samples/${encodeURIComponent(sampleId)}/media`;
  const sampleMediaRequestCount = network.filter((entry) =>
    new URL(entry.url).pathname === sampleMediaPath && entry.method === "GET").length;
  const variantMediaRequestCounts = variantDraftIds.map((draftId) => ({
    draftId,
    count: network.filter((entry) =>
      new URL(entry.url).pathname === `/api/studio/reference-drafts/${encodeURIComponent(draftId)}/media` &&
      entry.method === "GET").length,
  }));

  const origin = new URL(page.url()).origin;
  const initialByKind = new Map(artifacts.map((artifact) => [artifact.kind, artifact]));
  const initialWechat = initialByKind.get("wechat-channels");
  if (!initialWechat) throw new Error("studio-reference-browser-wechat-missing");

  const approvalOperationKeys: string[] = [];
  const approvalIdentities: string[] = [];
  let approvalAttempts = 0;
  const initialApprovalPath =
    `/api/studio/reference-drafts/${encodeURIComponent(initialWechat.draftId)}/approve`;
  await page.route(`**${initialApprovalPath}`, async (route, request) => {
    approvalAttempts += 1;
    const body = request.postDataJSON() as { operationKey?: unknown };
    approvalOperationKeys.push(String(body.operationKey ?? ""));
    if (approvalAttempts === 1) {
      const persisted = await route.fetch();
      const payload = await persisted.json() as { approval?: Record<string, unknown> };
      approvalIdentities.push(approvalRetryIdentity(payload.approval));
      await route.abort("failed");
      return;
    }
    await route.continue();
  });
  await page.locator(
    `[data-reference-approval-confirm="${initialWechat.draftId}"]`,
  ).check();
  await page.locator(`[data-reference-approve="${initialWechat.draftId}"]`).click();
  await waitForReferenceStatus(page, "本次参考样片操作未完成，已保留上一个有效阶段。");
  const approvalReplayResponsePromise = page.waitForResponse((response) =>
    new URL(response.url()).pathname === initialApprovalPath && response.request().method() === "POST");
  await page.locator(`[data-reference-approve="${initialWechat.draftId}"]`).click();
  const approvalReplayResponse = await approvalReplayResponsePromise;
  const approvalReplayPayload = await approvalReplayResponse.json() as {
    approval?: Record<string, unknown>;
  };
  approvalIdentities.push(approvalRetryIdentity(approvalReplayPayload.approval));
  await page.unroute(`**${initialApprovalPath}`);
  if (!approvalReplayResponse.ok() || approvalOperationKeys.length !== 2 ||
      approvalOperationKeys[0] !== approvalOperationKeys[1] ||
      approvalIdentities.length !== 2 || approvalIdentities[0] !== approvalIdentities[1]) {
    throw new Error("studio-reference-browser-approval-retry-invalid");
  }
  const priorApprovalId = requirePilotId(
    approvalReplayPayload.approval?.id,
    "studio-reference-browser-approval-missing",
  );

  const revisionPrompt = "开头缩短 1 秒" as const;
  await page.locator(
    `[data-reference-revision-prompt="${initialWechat.draftId}"]`,
  ).fill(revisionPrompt);
  const revisionPath =
    `/api/studio/reference-drafts/${encodeURIComponent(initialWechat.draftId)}/revise`;
  const revisionResponsePromise = page.waitForResponse((response) =>
    new URL(response.url()).pathname === revisionPath && response.request().method() === "POST", {
      timeout: 300_000,
    });
  await page.locator(`[data-reference-revise="${initialWechat.draftId}"]`).click();
  const revisionResponse = await revisionResponsePromise;
  const revisionPayload = await revisionResponse.json() as {
    error?: unknown;
    report?: {
      id?: unknown;
      version?: unknown;
      artifacts?: Array<{
        kind?: unknown;
        draftId?: unknown;
        draftVersion?: unknown;
        outputDigest?: unknown;
      }>;
    };
  };
  if (!revisionResponse.ok()) {
    throw new Error(`studio-reference-browser-revision-failed:${String(
      revisionPayload.error || revisionResponse.status(),
    )}`);
  }
  const revisedReportId = requirePilotId(
    revisionPayload.report?.id,
    "studio-reference-browser-revised-report-missing",
  );
  const revisedReportVersion = requirePilotVersion(
    revisionPayload.report?.version,
    "studio-reference-browser-revised-report-version-missing",
  );
  const revisedArtifacts = (revisionPayload.report?.artifacts ?? []).map(requirePilotArtifact);
  const revisedByKind = new Map(revisedArtifacts.map((artifact) => [artifact.kind, artifact]));
  const revisedWechat = revisedByKind.get("wechat-channels");
  if (!revisedWechat) throw new Error("studio-reference-browser-revised-wechat-missing");
  const digestChanged = revisedWechat.outputDigest !== initialWechat.outputDigest;
  const unaffectedDraftIdsPreserved = (["editorial-master", "xiaohongshu", "douyin"] as const)
    .every((kind) => {
      const before = initialByKind.get(kind);
      const after = revisedByKind.get(kind);
      return Boolean(before && after && before.draftId === after.draftId &&
        before.draftVersion === after.draftVersion && before.outputDigest === after.outputDigest);
    });
  if (!digestChanged || revisedWechat.draftId === initialWechat.draftId ||
      !unaffectedDraftIdsPreserved) {
    throw new Error("studio-reference-browser-platform-revision-binding-invalid");
  }
  const oldPlatformMedia = await page.context().request.get(
    `${origin}/api/studio/reference-drafts/${encodeURIComponent(initialWechat.draftId)}/media`,
    { headers: { origin } },
  );
  if (oldPlatformMedia.status() !== 409) {
    throw new Error("studio-reference-browser-old-platform-media-not-invalidated");
  }
  const afterRevisionSnapshot = await readWorkflowSnapshot();
  const oldApprovalInvalidated = afterRevisionSnapshot.approvals.some((approval) =>
    approval.id === priorApprovalId && approval.status === "invalidated");
  if (!oldApprovalInvalidated) {
    throw new Error("studio-reference-browser-old-platform-approval-not-invalidated");
  }

  const currentApprovalIds = new Map<ReferenceVariantKind, string>();
  for (const artifact of revisedArtifacts) {
    await page.locator(`[data-reference-approval-confirm="${artifact.draftId}"]`).check();
    const path = `/api/studio/reference-drafts/${encodeURIComponent(artifact.draftId)}/approve`;
    const responsePromise = page.waitForResponse((response) =>
      new URL(response.url()).pathname === path && response.request().method() === "POST");
    await page.locator(`[data-reference-approve="${artifact.draftId}"]`).click();
    const response = await responsePromise;
    const payload = await response.json() as {
      approval?: { id?: unknown; publishClaimed?: unknown };
    };
    if (!response.ok() || payload.approval?.publishClaimed !== false) {
      throw new Error("studio-reference-browser-current-approval-failed");
    }
    currentApprovalIds.set(artifact.kind, requirePilotId(
      payload.approval.id,
      "studio-reference-browser-current-approval-missing",
    ));
  }
  const mobileApprovalResponse = await page.context().request.get(
    `${origin}/api/studio/reference-mobile-review`,
    { headers: { origin } },
  );
  const mobileApprovalPayload = await mobileApprovalResponse.json() as {
    review?: {
      status?: unknown;
      publishClaimed?: unknown;
      approval?: { approvedArtifactKinds?: unknown };
      artifacts?: Array<{ kind?: unknown; status?: unknown }>;
    };
  };
  const approvedArtifactKinds = mobileApprovalPayload.review?.artifacts
    ?.filter((artifact) => artifact.status === "approved")
    .map((artifact) => String(artifact.kind)) ?? [];
  if (!mobileApprovalResponse.ok() || mobileApprovalPayload.review?.status !== "approved" ||
      mobileApprovalPayload.review.publishClaimed !== false ||
      JSON.stringify(approvedArtifactKinds) !== JSON.stringify([
        "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
      ])) {
    throw new Error("studio-reference-browser-mobile-approval-invalid");
  }

  const withdrawalResponse = await page.context().request.post(
    `${origin}/api/studio/reference-assets/${encodeURIComponent(sampleSourceAssetId)}/withdraw`,
    {
      headers: { origin, "content-type": "application/json" },
      data: { operationKey: "studio-reference-browser-withdraw-lineage" },
    },
  );
  if (withdrawalResponse.status() !== 201) {
    throw new Error("studio-reference-browser-withdrawal-failed");
  }

  const failedPrompt = "开头缩短 0.5 秒";
  await page.locator(
    `[data-reference-revision-prompt="${revisedWechat.draftId}"]`,
  ).fill(failedPrompt);
  const failedRevisionPath =
    `/api/studio/reference-drafts/${encodeURIComponent(revisedWechat.draftId)}/revise`;
  const failedRevisionResponsePromise = page.waitForResponse((response) =>
    new URL(response.url()).pathname === failedRevisionPath && response.request().method() === "POST");
  await page.locator(`[data-reference-revise="${revisedWechat.draftId}"]`).click();
  const failedRevisionResponse = await failedRevisionResponsePromise;
  const failedRevisionPayload = await failedRevisionResponse.json() as { error?: unknown };
  try {
    await page.locator("#reference-status")
      .getByText("素材权限或版本已失效；播放与审批已关闭。", { exact: true })
      .waitFor({ timeout: 2_000 });
  } catch {
    throw new Error(`studio-reference-browser-withdrawal-ui-mismatch:${failedRevisionResponse.status()}:` +
      `${String(failedRevisionPayload.error)}:${String(await page.locator("#reference-status").textContent())}`);
  }
  const playerCountAfterFailure = await page.locator("#reference-drafts video").count();
  const promptPreservedAfterFailure = await page.locator(
    `[data-reference-revision-prompt="${revisedWechat.draftId}"]`,
  ).inputValue() === failedPrompt;
  const actionsDisabledAfterFailure = await page.locator(
    "#reference-drafts [data-reference-revise], #reference-drafts [data-reference-approve], " +
    "#reference-drafts [data-reference-approval-confirm], #reference-drafts [data-reference-revision-prompt]",
  ).evaluateAll((controls) => controls.length === 16 && controls.every((control) =>
    (control as HTMLButtonElement | HTMLInputElement | HTMLTextAreaElement).disabled));

  const failedMobileResponse = await page.context().request.get(
    `${origin}/api/studio/reference-mobile-review`,
    { headers: { origin } },
  );
  const master = revisedByKind.get("editorial-master");
  if (!master) throw new Error("studio-reference-browser-revised-master-missing");
  const failedApprovalResponse = await page.context().request.post(
    `${origin}/api/studio/reference-drafts/${encodeURIComponent(master.draftId)}/approve`,
    {
      headers: { origin, "content-type": "application/json" },
      data: {
        reportId: revisedReportId,
        reportVersion: revisedReportVersion,
        draftVersion: master.draftVersion,
        draftDigest: master.outputDigest,
        operationKey: "studio-reference-browser-approve-after-withdraw",
      },
    },
  );
  const failedMediaResponse = await page.context().request.get(
    `${origin}/api/studio/reference-drafts/${encodeURIComponent(revisedWechat.draftId)}/media`,
    { headers: { origin } },
  );
  const afterWithdrawalSnapshot = await readWorkflowSnapshot();
  const currentApprovalAuditInvalidated = [...currentApprovalIds.values()].every((approvalId) =>
    afterWithdrawalSnapshot.approvals.some((approval) =>
      approval.id === approvalId && approval.status === "invalidated"));
  const sampleInvalidated = afterWithdrawalSnapshot.samples.some((sample) =>
    sample.id === sampleId && sample.status === "invalidated");
  const mappingInvalidated = afterWithdrawalSnapshot.mappingPlans.some((mapping) =>
    mapping.id === mappingPlanId && mapping.status === "invalidated");
  if (failedRevisionResponse.status() !== 409 || failedMobileResponse.status() !== 409 ||
      failedApprovalResponse.status() !== 409 || failedMediaResponse.status() !== 409 ||
      playerCountAfterFailure !== 0 || !actionsDisabledAfterFailure ||
      !promptPreservedAfterFailure || !currentApprovalAuditInvalidated ||
      !sampleInvalidated || !mappingInvalidated) {
    throw new Error("studio-reference-browser-withdrawal-fail-closed-invalid");
  }
  await writeFile(paths.domSnapshotPath, `${await page.content()}\n`, "utf8");

  return {
    evidenceScope: "local-synthetic-browser-pilot",
    modelEvidenceScope: "synthetic-test-worker",
    captureViewportWidth: 1440,
    mobileCaptureViewportWidth: mobileViewport.width,
    sourceKind: "external-style-only",
    sourceKindReadOnly,
    samplePreviewPlayed,
    sampleVideoPreservedAcrossRefresh,
    draftVideosPreservedAcrossRefresh,
    mappingConfirmed,
    variantKinds: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
    variantDraftIds,
    playedVariantCount: playback.length,
    executionDimensions: ["rhythm", "transitions", "narrative", "audio"],
    capabilityLabel,
    capabilityMode,
    warningCodes,
    capabilityWarningVisible,
    mappingConfirmRetry: {
      operationKeys: mappingConfirmOperationKeys as [string, string],
      mappingIdentities: mappingConfirmIdentities as [string, string],
    },
    renderRetry: {
      operationKeys: renderOperationKeys as [string, string],
      reportIdentities: renderIdentities as [string, string],
      renderDelegateInvocationCount,
    },
    approvalRetry: {
      variant: "wechat-channels",
      responseDropped: true,
      operationKeys: approvalOperationKeys as [string, string],
      approvalIdentities: approvalIdentities as [string, string],
    },
    platformRevision: {
      target: "wechat-channels",
      prompt: revisionPrompt,
      digestChanged: true,
      priorDraftId: initialWechat.draftId,
      revisedDraftId: revisedWechat.draftId,
      priorDigest: initialWechat.outputDigest,
      revisedDigest: revisedWechat.outputDigest,
      unaffectedDraftIdsPreserved: true,
      oldMediaStatus: 409,
      oldApprovalInvalidated: true,
    },
    currentApproval: {
      approvedArtifactKinds: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
      approvalIds: [
        currentApprovalIds.get("editorial-master")!,
        currentApprovalIds.get("xiaohongshu")!,
        currentApprovalIds.get("douyin")!,
        currentApprovalIds.get("wechat-channels")!,
      ],
      status: "approved",
      publishClaimed: false,
    },
    withdrawal: {
      assetId: sampleSourceAssetId,
      status: 201,
      mobileStatus: 409,
      reviseStatus: 409,
      approveStatus: 409,
      mediaStatus: 409,
      playerCountAfterFailure: 0,
      actionsDisabledAfterFailure: true,
      promptPreservedAfterFailure: true,
      currentApprovalAuditInvalidated: true,
      sampleInvalidated: true,
      mappingInvalidated: true,
    },
    sampleMediaRequestCount,
    variantMediaRequestCounts,
    reportDrawerOpenAtCapture,
    versionIds: {
      sampleId,
      rightsId,
      analysisId,
      selectionIdBeforeChange,
      mappingPlanId,
      executionReportId,
    },
    mediaResponses,
    screenshotPath: paths.screenshotPath,
    mobileScreenshotPath: paths.mobileScreenshotPath,
    domSnapshotPath: paths.domSnapshotPath,
  };
}

function requirePilotId(value: unknown, code: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(code);
  return value;
}

function requirePilotVersion(value: unknown, code: string): number {
  if (!Number.isInteger(value) || (value as number) <= 0) throw new Error(code);
  return value as number;
}

function requirePilotArtifact(value: Readonly<{
  kind?: unknown;
  draftId?: unknown;
  draftVersion?: unknown;
  outputDigest?: unknown;
}>): PilotReferenceArtifact {
  if (!(value.kind === "editorial-master" || value.kind === "xiaohongshu" || value.kind === "douyin" ||
      value.kind === "wechat-channels")) {
    throw new Error("studio-reference-browser-artifact-kind-invalid");
  }
  const outputDigest = requirePilotId(
    value.outputDigest,
    "studio-reference-browser-artifact-digest-missing",
  );
  if (!/^[a-f0-9]{64}$/.test(outputDigest)) {
    throw new Error("studio-reference-browser-artifact-digest-invalid");
  }
  return {
    kind: value.kind,
    draftId: requirePilotId(value.draftId, "studio-reference-browser-draft-id-missing"),
    draftVersion: requirePilotVersion(
      value.draftVersion,
      "studio-reference-browser-draft-version-missing",
    ),
    outputDigest,
  };
}

function approvalRetryIdentity(approval: Record<string, unknown> | undefined): string {
  return JSON.stringify({
    id: approval?.id ?? null,
    version: approval?.version ?? null,
    bindings: approval?.bindings ?? null,
    variant: approval?.variant ?? null,
    reviewerId: approval?.reviewerId ?? null,
    fileDigest: approval?.fileDigest ?? null,
    status: approval?.status ?? null,
    publishClaimed: approval?.publishClaimed ?? null,
  });
}

function mappingPlanRetryIdentity(mappingPlan: Record<string, unknown> | undefined): string {
  return JSON.stringify({
    id: mappingPlan?.id ?? null,
    version: mappingPlan?.version ?? null,
    contentDigest: mappingPlan?.contentDigest ?? null,
    permissionSetDigest: mappingPlan?.permissionSetDigest ?? null,
    status: mappingPlan?.status ?? null,
  });
}

function reportRetryIdentity(report: Record<string, unknown> | undefined): string {
  const artifacts = Array.isArray(report?.artifacts)
    ? report.artifacts.map((artifact) => {
        const value = artifact as Record<string, unknown>;
        return {
          kind: value.kind ?? null,
          draftId: value.draftId ?? null,
          outputDigest: value.outputDigest ?? null,
        };
      })
    : [];
  return JSON.stringify({ id: report?.id ?? null, version: report?.version ?? null, artifacts });
}

const createBrowserPilotReferenceRuntime = async (
  input: Parameters<StudioReferenceRuntimeFactory>[0],
  onRender: () => void,
): Promise<Awaited<ReturnType<StudioReferenceRuntimeFactory>>> => {
  const store = await JsonReferenceSampleStore.open(input.sessionRoot, input.scope);
  const sampleService = new ReferenceSampleService(input.library, input.sessionRoot, { now: input.now });
  const outputRoot = join(input.sessionRoot, "reference-renders");
  await mkdir(outputRoot, { recursive: true, mode: 0o700 });
  const renderRequest = async (
    request: ReferenceEditorRenderRequest,
    revisionOverride?: ReturnType<typeof interpretReferencePromptRevision>,
  ) => {
    const { scope, rights, analysis, mappingPlan, operationKey } = request;
    onRender();
    const visibleAssets = new Map((await input.library.listAssets(scope.actorId, scope.customerId))
      .map((asset) => [asset.id, asset]));
    const sources: ReferenceRenderAsset[] = [];
    const mappings = mappingPlan.mappings.filter((mapping) => mapping.sourceAssetId !== null);
    for (const assetId of new Set(mappings.map((mapping) => mapping.sourceAssetId!))) {
      const mapping = mappings.find((candidate) => candidate.sourceAssetId === assetId)!;
      const asset = visibleAssets.get(assetId);
      if (!asset || !mapping.permission) throw new Error("reference-render-source-not-authorized");
      const bytes = await input.library.readOriginal(scope.actorId, scope.customerId, asset.id);
      sources.push({
        asset,
        permission: mapping.permission,
        withdrawn: false,
        verifiedDigest: createHash("sha256").update(bytes).digest("hex"),
        bytes,
      });
    }
    const rendered = await renderReferenceVariants({
      mappingPlan,
      rights,
      analysis,
      assets: sources,
      outputRoot,
      operationKey,
      now: input.now(),
      ...(revisionOverride ? { revisionOverride } : {}),
    });
    return { ...rendered, outputDirectory: dirname(rendered.drafts[0]!.outputPath) };
  };
  const editor = new ReferenceGuidedEditor({
    store,
    now: input.now,
    analyze: async ({ scope, sample, rights }) => {
      const source = await input.library.inspectReferenceAnalysisAsset(
        scope.actorId, scope.customerId, scope.sessionId, sample.sourceAssetId,
      );
      const fixtureWorker = createBrowserPilotReferenceWorker();
      const receipts = await Promise.all((["scene", "transcript", "vision"] as const).map((layer, index) =>
        fixtureWorker.run({
          schemaVersion: 1,
          jobId: `synthetic_browser_pilot_${layer}_${index}`,
          customerId: scope.customerId,
          sessionId: scope.sessionId,
          sourceAssetId: source.asset.id,
          sourceDigest: source.asset.digest,
          sourcePath: source.sourcePath,
          sourceStartMs: 0,
          sourceEndMs: 10_500,
          layer,
          operationKey: `synthetic-browser-pilot-${layer}`,
          requestedAt: input.now(),
        }, new AbortController().signal)));
      return browserPilotSyntheticAnalysis(scope, sample, rights, receipts, input.now());
    },
    createMapping: async ({ scope, sample, rights, analysis, selection }) => {
      const cards = await input.library.inventoryAt(scope.actorId, scope.customerId, input.now());
      const assets = await input.library.listAssets(scope.actorId, scope.customerId);
      const allowedAssets = assets.filter((asset) =>
        asset.sessionId !== null && asset.usePermission === "allowed");
      const snapshots: ReferenceMappingAssetSnapshot[] = [];
      for (const asset of allowedAssets) {
        const bytes = await input.library.readOriginal(scope.actorId, scope.customerId, asset.id);
        snapshots.push({
          asset,
          permission: browserPilotReferencePermission(scope, asset),
          withdrawn: false,
          verifiedDigest: createHash("sha256").update(bytes).digest("hex"),
        });
      }
      return createReferenceMaterialMappingPlan({
        scope,
        sample,
        rights,
        analysis,
        selection,
        currentMaterials: cards.filter((card) => card.sessionId === scope.sessionId),
        authorizedHistoricalMaterials: cards.filter((card) => card.sessionId !== scope.sessionId),
        assets: snapshots,
      });
    },
    render: renderRequest,
    revise: async (request) => {
      if (request.target === null || request.prompt === null) {
        throw new Error("reference-revision-prompt-required");
      }
      return renderRequest(request, interpretReferencePromptRevision({
        prompt: request.prompt,
        target: request.target,
      }));
    },
    withCurrentAssetAuthorization: ({ scope, sample, mappingPlan }, work) => {
      const renderSources = new Map(mappingPlan.mappings.flatMap((mapping) => {
        if (!mapping.sourceAssetId) return [];
        if (!mapping.sourceDigest || !mapping.permission ||
            mapping.permission.view !== "allowed" || mapping.permission.use !== "allowed") {
          throw new Error("reference-workflow-asset-not-current");
        }
        return [[mapping.sourceAssetId, {
          role: "render-source" as const,
          assetId: mapping.sourceAssetId,
          assetVersion: mapping.permission.version,
          sourceDigest: mapping.sourceDigest,
          permission: {
            version: mapping.permission.version,
            epoch: mapping.permission.epoch,
            view: mapping.permission.view,
            use: mapping.permission.use,
          },
        }] as const];
      }));
      return input.library.withReferenceWorkflowAssets(scope.actorId, scope.customerId, [{
        role: "sample-source",
        assetId: sample.sourceAssetId,
        assetVersion: 1,
        sourceDigest: sample.sourceDigest,
      }, ...renderSources.values()], work);
    },
    readDraftOutputDigest: async ({ draft }) => {
      let canonicalRoot: string;
      let canonicalOutput: string;
      try {
        canonicalRoot = await realpath(resolve(outputRoot));
        canonicalOutput = await realpath(resolve(draft.outputPath));
      } catch {
        throw new Error("reference-draft-media-unavailable");
      }
      const withinRoot = relative(canonicalRoot, canonicalOutput);
      if (!withinRoot || withinRoot === ".." || withinRoot.startsWith(`..${sep}`) || isAbsolute(withinRoot)) {
        throw new Error("reference-draft-output-binding-invalid");
      }
      if (!(await stat(canonicalOutput).catch(() => null))?.isFile()) {
        throw new Error("reference-draft-media-unavailable");
      }
      return createHash("sha256").update(await readFile(canonicalOutput)).digest("hex");
    },
    cleanupRenderedOutput: async (result) => {
      const relativePath = relative(resolve(outputRoot), resolve(result.outputDirectory));
      if (!relativePath || relativePath.startsWith("..")) {
        throw new Error("reference-render-output-cleanup-forbidden");
      }
      await rm(result.outputDirectory, { recursive: true, force: true });
    },
  });
  return {
    sampleService,
    editor,
    store,
    enforceMaterialAuthorization: true,
    prepareAnalysis: async (scope, referenceSampleId) => {
      const sample = (await store.snapshot()).samples
        .filter((candidate) => candidate.id === referenceSampleId)
        .sort((left, right) => right.version - left.version)[0];
      if (!sample || sample.customerId !== scope.customerId || sample.sessionId !== scope.sessionId) {
        throw new Error("reference-sample-not-found");
      }
      if (sample.status !== "ready") throw new Error("reference-sample-not-ready");
      const source = await input.library.inspectReferenceAnalysisAsset(
        scope.actorId, scope.customerId, scope.sessionId, sample.sourceAssetId,
      );
      if (source.asset.digest !== sample.sourceDigest) {
        throw new Error("reference-analysis-source-binding-mismatch");
      }
      // The injected Task 8 runtime intentionally does not promote synthetic receipts
      // into the production model ledger; analyze() creates truthful fixture receipts.
    },
  };
};

function browserPilotSyntheticAnalysis(
  scope: StudioScope,
  sample: ReferenceSample,
  rights: ReferenceRightsDeclaration,
  receipts: readonly ModelAnalysisReceiptV1[],
  createdAt: string,
) {
  if (receipts.some((receipt) => receipt.synthetic !== true || receipt.providerMode !== "offline-fallback")) {
    throw new Error("browser-pilot-synthetic-receipt-truth-required");
  }
  const sceneReceipt = receipts.find((receipt) => receipt.layer === "scene");
  if (!sceneReceipt) throw new Error("browser-pilot-scene-receipt-required");
  const sceneRows = sceneReceipt.qualitySignals.scenes;
  if (!Array.isArray(sceneRows) || sceneRows.length === 0) {
    throw new Error("browser-pilot-scenes-required");
  }
  const sections = sceneRows.map((row, index) => {
    const scene = row as { startMs: number; endMs: number };
    return {
      id: pilotEvidenceId("section", sceneReceipt.jobId, index),
      startMs: scene.startMs,
      endMs: scene.endMs,
      summary: `合成测试场景 ${index + 1}`,
    };
  });
  const transitionRows = Array.isArray(sceneReceipt.qualitySignals.transitionEvidence)
    ? sceneReceipt.qualitySignals.transitionEvidence
    : [];
  const transitions = transitionRows.map((row, index) => {
    const transition = row as { atMs: number; kind: "cut" | "fade" | "crossfade"; confidence: number };
    return { id: pilotEvidenceId("transition", sceneReceipt.jobId, index), ...transition };
  });
  const beatMarkers = Array.isArray(sceneReceipt.qualitySignals.beatMarkers)
    ? sceneReceipt.qualitySignals.beatMarkers as number[]
    : [];
  const transcriptReceipt = receipts.find((receipt) => receipt.layer === "transcript");
  const transcriptRows = Array.isArray(transcriptReceipt?.qualitySignals.spans)
    ? transcriptReceipt.qualitySignals.spans
    : [];
  const transcriptSpans = transcriptRows.map((row, index) => {
    const span = row as { startMs: number; endMs: number; text: string };
    return { id: pilotEvidenceId("transcript", transcriptReceipt!.jobId, index), ...span };
  });
  const first = sections[0]!;
  const last = sections[sections.length - 1]!;
  const rules = [
    {
      id: pilotEvidenceId("rule-rhythm", sceneReceipt.jobId, 0),
      kind: "rhythm" as const,
      description: `合成测试拆解 ${sections.length} 个场景`,
      referenceStartMs: first.startMs,
      referenceEndMs: last.endMs,
      evidenceIds: sections.map((section) => section.id),
    },
    ...transitions.map((transition, index) => ({
      id: pilotEvidenceId("rule-transition", sceneReceipt.jobId, index),
      kind: "transition" as const,
      description: `合成测试 ${transition.kind} 转场`,
      referenceStartMs: Math.max(first.startMs, transition.atMs - 1),
      referenceEndMs: Math.min(last.endMs, transition.atMs + 1),
      evidenceIds: [transition.id],
    })),
  ];
  const bindings = {
    sample: {
      id: sample.id, version: sample.version, customerId: sample.customerId, sessionId: sample.sessionId,
      sourceAssetId: sample.sourceAssetId, sourceDigest: sample.sourceDigest, sourceKind: sample.sourceKind,
      collectionMode: sample.collectionMode, candidateScope: sample.candidateScope,
    },
    rights: {
      id: rights.id, version: rights.version, customerId: rights.customerId, sessionId: rights.sessionId,
      referenceSampleId: sample.id, referenceSampleVersion: sample.version,
      referenceSampleDigest: sample.sourceDigest, sourceKind: rights.sourceKind,
      reusableElements: rights.reusableElements, contentDigest: rights.contentDigest,
    },
  };
  const content = {
    bindings,
    cuts: transitions.filter((transition) => transition.kind === "cut").map((transition) => transition.atMs),
    sections,
    transcriptSpans,
    beatMarkers,
    transitions,
    rules,
    warnings: ["synthetic-browser-pilot-evidence-only"],
  };
  const contentDigest = createHash("sha256").update(JSON.stringify(content)).digest("hex");
  return ReferenceStyleAnalysisSchema.parse({
    id: `reference_analysis_${contentDigest}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    contentDigest,
    ...content,
    confidence: Math.min(...receipts.map((receipt) => receipt.confidence)),
    status: "degraded",
    capability: OFFLINE_STUDIO_CAPABILITY,
    createdAt,
  });
}

function pilotEvidenceId(kind: string, jobId: string, index: number): string {
  return `${kind}_${createHash("sha256").update(`${jobId}\0${index}`).digest("hex")}`;
}

function browserPilotReferencePermission(
  scope: StudioScope,
  asset: ReferenceMappingAssetSnapshot["asset"],
) {
  return {
    id: `reference_permission_${createHash("sha256")
      .update(`${scope.customerId}\0${scope.sessionId}\0${asset.id}\0${asset.version}`)
      .digest("hex")}`,
    version: asset.version,
    epoch: asset.version,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    sourceAssetId: asset.id,
    sourceDigest: asset.digest,
    view: asset.viewPermission,
    use: asset.usePermission,
  };
}

function createBrowserPilotReferenceWorker(): StudioModelWorker {
  return {
    async run(job) {
      return referenceReceiptFor(job);
    },
    async health() {
      return { mode: "offline-fallback", installed: false, modelName: null, checkpointDigest: null };
    },
  };
}

function referenceReceiptFor(job: ModelAnalysisJobV1) {
  const duration = job.sourceEndMs - job.sourceStartMs;
  const boundaries = [
    job.sourceStartMs,
    job.sourceStartMs + Math.floor(duration * 0.22),
    job.sourceStartMs + Math.floor(duration * 0.49),
    job.sourceStartMs + Math.floor(duration * 0.74),
    job.sourceEndMs,
  ];
  const scenes = boundaries.slice(0, -1).map((startMs, index) => {
    const endMs = boundaries[index + 1]!;
    return {
      startMs,
      endMs,
      representativeFrameMs: startMs + Math.max(1, Math.floor((endMs - startMs) / 2)),
      representativeFrameDigest: createHash("sha256").update(`browser-pilot-frame-${index}`).digest("hex"),
    };
  });
  const qualitySignals = job.layer === "scene"
    ? {
        scenes,
        transitionEvidence: [
          { atMs: boundaries[1], kind: "cut", confidence: 0.96 },
          { atMs: boundaries[2], kind: "fade", confidence: 0.88 },
          { atMs: boundaries[3], kind: "crossfade", confidence: 0.84 },
        ],
        beatMarkers: boundaries.slice(1, -1),
      }
    : job.layer === "transcript"
      ? { spans: [{ startMs: boundaries[0], endMs: boundaries[2], text: "合成浏览器验收口播" }] }
      : {
          visualObservations: [{
            startMs: boundaries[2], endMs: boundaries[4],
            text: "合成浏览器验收画面结构", confidence: 0.86,
            status: "pending-human-confirmation",
          }],
        };
  const observations = job.layer === "vision"
    ? ["pending-visual-observation:合成浏览器验收画面结构"]
    : [];
  const { requestedAt: _requestedAt, ...binding } = job;
  return {
    ...binding,
    adapterName: `synthetic-browser-pilot-${job.layer}-adapter`,
    modelName: `synthetic-browser-pilot-${job.layer}-fixture`,
    modelVersion: "task8-v1",
    checkpointDigest: null,
    inputDigest: job.sourceDigest,
    promptVersion: "task8-browser-pilot-v1",
    observations,
    suggestedTags: [],
    qualitySignals,
    confidence: 0.86,
    warnings: [],
    startedAt: "2026-09-11T08:00:00.000Z",
    completedAt: "2026-09-11T08:00:00.100Z",
    runtimeMs: 100,
    status: "succeeded" as const,
    failure: null,
    synthetic: true,
    providerMode: "offline-fallback" as const,
  };
}

async function makeSyntheticReferenceMov(path: string): Promise<void> {
  if (!ffmpegPath) throw new Error("studio-reference-browser-ffmpeg-unavailable");
  await runPilotProcess(ffmpegPath, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=10.5",
    "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=44100:duration=10.5",
    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-f", "mov", "-y", path,
  ]);
}

async function runPilotProcess(command: string, args: string[]): Promise<void> {
  await new Promise<void>((resolveProcess, rejectProcess) => {
    const child = spawn(command, args, { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", rejectProcess);
    child.once("close", (status) => status === 0
      ? resolveProcess()
      : rejectProcess(new Error(`studio-reference-browser-ffmpeg-failed:${stderr.trim()}`)));
  });
}

export function assertExactVideoPlayback(
  playback: readonly { draftId: string }[],
  expectedDraftIds: readonly string[],
): void {
  const actual = playback.map((entry) => entry.draftId).sort();
  const expected = [...expectedDraftIds].sort();
  if (actual.length !== expected.length || new Set(actual).size !== actual.length ||
      new Set(expected).size !== expected.length || JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error("studio-browser-video-playback-draft-mismatch");
  }
}

function demandEvidence(value: unknown): BrowserDemandEvidence {
  if (typeof value !== "object" || value === null) {
    throw new Error("studio-browser-demand-evidence-missing");
  }
  const record = value as Record<string, unknown>;
  const mustInclude = record.mustInclude;
  const mustAvoid = record.mustAvoid;
  if (record.contentType !== "single-product" || typeof record.goal !== "string" ||
      typeof record.targetAudience !== "string" || typeof record.desiredEffect !== "string" ||
      !Array.isArray(mustInclude) || !mustInclude.every((item) => typeof item === "string") ||
      !Array.isArray(mustAvoid) || !mustAvoid.every((item) => typeof item === "string") ||
      record.submittedBy !== "customer-self-declared") {
    throw new Error("studio-browser-demand-evidence-invalid");
  }
  return {
    contentType: record.contentType,
    goal: record.goal,
    targetAudience: record.targetAudience,
    desiredEffect: record.desiredEffect,
    mustInclude: [...mustInclude],
    mustAvoid: [...mustAvoid],
    submittedBy: record.submittedBy,
  };
}

async function playCurrentDraftVideos(
  page: import("playwright-core").Page,
  selector = "video",
): Promise<Array<{ draftId: string; currentTime: number; paused: boolean; readyState: number }>> {
  const videos = page.locator(selector);
  for (let index = 0; index < await videos.count(); index += 1) {
    await videos.nth(index).evaluate(async (video: HTMLVideoElement) => {
      video.muted = true;
      await video.play();
    });
  }
  await page.waitForFunction((videoSelector) => [...document.querySelectorAll(videoSelector)].every((video) => {
    const media = video as HTMLVideoElement;
    return media.currentTime > 0 && media.readyState >= 3;
  }), selector);
  return videos.evaluateAll((items) => items.map((video) => {
    const media = video as HTMLVideoElement;
    const mediaRoute = new URL(media.currentSrc).pathname;
    return {
      draftId: decodeURIComponent(mediaRoute.split("/").at(-2) ?? ""),
      currentTime: media.currentTime,
      paused: media.paused,
      readyState: media.readyState,
    };
  }));
}

async function readBriefVersion(page: import("playwright-core").Page): Promise<number> {
  const briefText = await page.locator("#brief").innerText();
  const version = Number(/任务书 V(\d+)/u.exec(briefText)?.[1]);
  if (!Number.isSafeInteger(version)) throw new Error("studio-browser-brief-version-missing");
  return version;
}

function publicDrafts(drafts: BrowserDraft[]): Array<{ id: string; mediaRoute: string }> {
  return drafts.map((draft) => ({ id: draft.id, mediaRoute: draft.mediaUrl }));
}

function countDraftPosts(network: Array<{ method: string; url: string }>): number {
  return network.filter((entry) =>
    new URL(entry.url).pathname === "/api/studio/drafts" && entry.method === "POST").length;
}

async function waitForStatus(page: import("playwright-core").Page, expected: string): Promise<void> {
  try {
    await page.locator("#studio-status").getByText(expected, { exact: true }).waitFor({ state: "attached" });
  } catch {
    const actual = (await page.locator("#studio-status").innerText()).trim();
    throw new Error(`studio-browser-status-mismatch:${expected}:${actual}`);
  }
}

async function waitForReferenceStatus(
  page: import("playwright-core").Page,
  expected: string,
): Promise<void> {
  await page.locator("#reference-status").getByText(expected, { exact: true }).waitFor({ timeout: 180_000 });
}

async function writeJson(path: string, value: unknown): Promise<void> {
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}
