import { access, mkdtemp, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

import {
  defaultStudioBrowserExecutable,
  runStudioBrowserPilot,
} from "../src/studio-browser-pilot";
import * as studioBrowserPilotModule from "../src/studio-browser-pilot";

describe("private studio real-browser pilot", () => {
  const cleanup: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("captures replayable Chrome trace, screenshot, DOM and matching media network evidence", async () => {
    const root = await mkdtemp(join(tmpdir(), "private-studio-browser-pilot-"));
    cleanup.push(root);
    const executablePath = defaultStudioBrowserExecutable();
    await access(executablePath);

    const report = await runStudioBrowserPilot(root, { executablePath });

    expect(report).toMatchObject({
      syntheticOnly: true,
      automaticPublishing: false,
      release: {
        version: "1.0.0",
        label: "本地合成候选",
        boundary: "local-synthetic-candidate",
        production: false,
        deployed: false,
      },
      releaseText: "Release 1.0.0 · 本地合成候选 · local-synthetic-candidate · 非生产/未部署",
      browserEngine: "chromium",
      traceFormat: "playwright",
      replayCommand: "node packages/review-app/node_modules/playwright-core/cli.js show-trace studio-browser-trace.zip",
      workflowSteps: [
        "告诉我这次想讲什么",
        "素材库",
        "看看现有素材合不合适",
        "确认要用的素材",
        "我的作品",
        "按你的意见修改",
      ],
      demandGoal: "让顾客理解设计灵感",
      demandRequestBody: {
        contentType: "single-product",
        goal: "让顾客理解设计灵感",
        targetAudience: "准备到店的顾客",
        desiredEffect: "愿意预约到店",
        mustInclude: ["纯色合成商品夹具 1"],
        mustAvoid: ["真实客户姓名"],
        submittedBy: "customer-self-declared",
      },
      persistedDemand: {
        contentType: "single-product",
        goal: "让顾客理解设计灵感",
        targetAudience: "准备到店的顾客",
        desiredEffect: "愿意预约到店",
        mustInclude: ["纯色合成商品夹具 1"],
        mustAvoid: ["真实客户姓名"],
        submittedBy: "customer-self-declared",
      },
      matchVerdictText: "匹配结果：绿灯 · 适合继续",
      matchVerdictClass: expect.stringContaining("verdict-ready"),
      inventoryItemCount: 4,
      opportunityCount: 1,
      briefVersions: [1, 2],
      initialDraftCount: 2,
      externalEditUi: {
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
      },
      revisedDraftCount: 2,
      lineageDrawerCount: 2,
      lineageRows: 6,
      adoptDisabledAfterSatisfied: [false, true],
      customerFeedbackCount: 2,
      adoptedDraftVersion: 2,
      v2CreatedBeforeInternalConfirmation: false,
      playedVideoCount: 4,
      draftPostCountAfterPartialFeedback: 1,
      draftPostCountAfterProposal: 1,
      draftPostCountAfterConfirmation: 1,
      draftPostCountAfterExplicitRender: 2,
      viewportWidth: 320,
      documentScrollWidth: 320,
      narrowPaneViewportWidth: 150,
      narrowPaneDocumentScrollWidth: 150,
      focusOutline: "solid 3px",
      publishButtonCount: 0,
      publishRequestCount: 0,
      historyDrawerOpenAtCapture: true,
      historyVisibleAtCapture: true,
      adoptionVisibleAtCapture: true,
      adoptionResponse: {
        action: "adopt",
        publishClaimed: false,
      },
      referenceJourney: {
        evidenceScope: "local-synthetic-browser-pilot",
        modelEvidenceScope: "synthetic-test-worker",
        captureViewportWidth: 1440,
        mobileCaptureViewportWidth: 320,
        sourceKind: "external-style-only",
        sourceKindReadOnly: true,
        samplePreviewPlayed: true,
        sampleVideoPreservedAcrossRefresh: true,
        draftVideosPreservedAcrossRefresh: true,
        mappingConfirmed: true,
        variantKinds: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
        playedVariantCount: 4,
        executionDimensions: ["rhythm", "transitions", "narrative", "audio"],
        reportDrawerOpenAtCapture: true,
        capabilityMode: "offline-fallback",
        warningCodes: ["synthetic-browser-pilot-evidence-only"],
        capabilityWarningVisible: true,
        renderRetry: {
          renderDelegateInvocationCount: 1,
        },
        approvalRetry: {
          variant: "wechat-channels",
          responseDropped: true,
        },
        platformRevision: {
          target: "wechat-channels",
          digestChanged: true,
          unaffectedDraftIdsPreserved: true,
          oldMediaStatus: 409,
          oldApprovalInvalidated: true,
        },
        currentApproval: {
          approvedArtifactKinds: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
          status: "approved",
          publishClaimed: false,
        },
        withdrawal: {
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
        // Chromium may split one video playback into several range requests.
        // Element identity above, not request count, proves refresh preservation.
        sampleMediaRequestCount: expect.any(Number),
      },
    });
    expect(report.referenceJourney.sampleMediaRequestCount).toBeGreaterThan(0);
    expect(report.referenceJourney.approvalRetry.operationKeys).toHaveLength(2);
    expect(new Set(report.referenceJourney.approvalRetry.operationKeys)).toHaveLength(1);
    expect(report.referenceJourney.approvalRetry.approvalIdentities).toHaveLength(2);
    expect(new Set(report.referenceJourney.approvalRetry.approvalIdentities)).toHaveLength(1);
    expect(report.referenceJourney.platformRevision.priorDraftId)
      .not.toBe(report.referenceJourney.platformRevision.revisedDraftId);
    expect(report.referenceJourney.platformRevision.priorDigest)
      .not.toBe(report.referenceJourney.platformRevision.revisedDigest);
    expect(report.referenceJourney.currentApproval.approvalIds).toHaveLength(4);
    expect(new Set(report.referenceJourney.currentApproval.approvalIds)).toHaveLength(4);
    expect(report.referenceJourney.withdrawal.assetId).toBeTruthy();
    expect(report.initialDrafts).toHaveLength(2);
    expect(report.revisedDrafts).toHaveLength(2);
    expect(report.adoptedDraftId).toBe(report.revisedDrafts[0]!.id);
    expect(report.adoptionResponse.draftId).toBe(report.adoptedDraftId);
    expect(report.finalV2DraftStatuses).toEqual([
      { draftId: report.revisedDrafts[0]!.id, status: "adopted" },
      { draftId: report.revisedDrafts[1]!.id, status: "rejected" },
    ]);
    expect(report.adoptionStatusText).toContain("采用这版");
    expect(report.revisionProposalText).toContain("提案可以内部确认");
    expect(report.confirmedProposalText).toContain("提案已确认，等待生成新版初稿");
    expect(report.confirmedProposalText).not.toContain("提案仍有未解决项，不能确认");
    expect(report.renderedProposalText).toContain("提案已确认，新版初稿已生成");
    expect(report.renderedProposalText).not.toContain("等待生成新版初稿");
    expect(report.historyCounts).toMatchObject({
      "需求": 1,
      "匹配评估": 1,
      "任务书": 2,
      "初稿": 4,
      "客户反馈": 2,
      "修改提案": 1,
    });
    expect(report.videoPlayback.every((video) =>
      video.currentTime > 0 && video.readyState >= 3,
    )).toBe(true);
    expect(report.videoPlayback).toHaveLength(4);
    const expectedPlaybackDraftIds = [...report.initialDrafts, ...report.revisedDrafts]
      .map((draft) => draft.id).sort();
    const actualPlaybackDraftIds = report.videoPlayback.map((video) => video.draftId).sort();
    expect(actualPlaybackDraftIds).toEqual(expectedPlaybackDraftIds);
    expect(new Set(actualPlaybackDraftIds)).toHaveLength(4);
    expect(report.mediaResponses.map((entry) => entry.draftId).sort())
      .toEqual([...report.initialDrafts, ...report.revisedDrafts].map((draft) => draft.id).sort());
    for (const entry of report.mediaResponses) {
      expect([200, 206], JSON.stringify(entry)).toContain(entry.status);
      expect(entry.contentType).toBe("video/mp4");
    }
    expect(report.networkActions).not.toContain("request-changes");
    expect(report.referenceJourney.versionIds).toMatchObject({
      sampleId: expect.stringMatching(/^reference_sample_/),
      rightsId: expect.stringMatching(/^reference_rights_/),
      analysisId: expect.stringMatching(/^reference_analysis_/),
      selectionIdBeforeChange: expect.stringMatching(/^reference_style_selection_/),
      mappingPlanId: expect.stringMatching(/^reference_mapping_plan_/),
      executionReportId: expect.stringMatching(/^reference_execution_report_/),
    });
    expect(report.referenceJourney.variantDraftIds).toHaveLength(4);
    expect(new Set(report.referenceJourney.variantDraftIds)).toHaveLength(4);
    for (const entry of report.referenceJourney.mediaResponses) {
      expect([200, 206], JSON.stringify(entry)).toContain(entry.status);
      expect(entry.contentType).toBe("video/mp4");
    }
    expect(report.referenceJourney.variantMediaRequestCounts.map((entry) => entry.draftId))
      .toEqual(report.referenceJourney.variantDraftIds);
    for (const entry of report.referenceJourney.variantMediaRequestCounts) {
      expect(entry.count).toBeGreaterThan(0);
    }
    expect(report.referenceJourney.mappingConfirmRetry.operationKeys).toHaveLength(2);
    expect(new Set(report.referenceJourney.mappingConfirmRetry.operationKeys)).toHaveLength(1);
    expect(report.referenceJourney.mappingConfirmRetry.mappingIdentities).toHaveLength(2);
    expect(new Set(report.referenceJourney.mappingConfirmRetry.mappingIdentities)).toHaveLength(1);
    expect(report.referenceJourney.renderRetry.operationKeys).toHaveLength(2);
    expect(new Set(report.referenceJourney.renderRetry.operationKeys)).toHaveLength(1);
    expect(report.referenceJourney.renderRetry.reportIdentities).toHaveLength(2);
    expect(new Set(report.referenceJourney.renderRetry.reportIdentities)).toHaveLength(1);
    expect(report.referenceJourney.capabilityLabel).toBe("离线/模拟分析");
    for (const path of [
      report.tracePath,
      report.screenshotPath,
      report.domSnapshotPath,
      report.networkLogPath,
      report.reportPath,
      report.referenceJourney.screenshotPath,
      report.referenceJourney.mobileScreenshotPath,
      report.referenceJourney.domSnapshotPath,
    ]) {
      expect((await stat(path)).size).toBeGreaterThan(0);
    }
    const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
    await expect(access(join(
      repositoryRoot,
      "packages/review-app/node_modules/playwright-core/cli.js",
    ))).resolves.toBeUndefined();
    const dom = await readFile(report.domSnapshotPath, "utf8");
    for (const draft of [...report.initialDrafts, ...report.revisedDrafts]) expect(dom).toContain(draft.id);
    expect(dom).toContain("纯色合成商品夹具");
    expect(dom).toContain("纯色合成商品夹具 1");
    expect(dom).toContain("真实客户姓名");
    expect(dom).toMatch(/<details class="history-drawer" open="">/u);
    expect(dom).toContain("示例属性字段待人工确认");
    expect(dom).toContain("已通过");
    expect(dom).toContain("历史版本");
    expect(dom).toContain("Release 1.0.0 · 本地合成候选 · local-synthetic-candidate · 非生产/未部署");
    expect(dom).not.toContain("这枚戒指");
    expect(dom).not.toContain("18K 金");
    expect(dom).not.toContain("合成珠宝工作台镜头");
    const referenceDom = await readFile(report.referenceJourney.domSnapshotPath, "utf8");
    expect(referenceDom).toContain("参考视频制作");
    expect(referenceDom).toContain("原视频母版");
    expect(referenceDom).toContain("小红书版");
    expect(referenceDom).toContain("视频号版");
    expect(referenceDom).toContain("平台主动调整");
    expect(referenceDom).not.toMatch(/总相似度|totalSimilarity/u);
  }, 300_000);

  it("rejects duplicate playback ids even when four videos were reported", () => {
    const assertExactVideoPlayback = (
      studioBrowserPilotModule as unknown as {
        assertExactVideoPlayback: (
          playback: readonly { draftId: string }[],
          draftIds: readonly string[],
        ) => void;
      }
    ).assertExactVideoPlayback;

    expect(() => assertExactVideoPlayback([
      { draftId: "v1-a" },
      { draftId: "v1-b" },
      { draftId: "v2-a" },
      { draftId: "v2-a" },
    ], ["v1-a", "v1-b", "v2-a", "v2-b"]))
      .toThrow("studio-browser-video-playback-draft-mismatch");
  });
});
