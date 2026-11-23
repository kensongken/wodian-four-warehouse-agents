import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { promisify } from "node:util";
import { execFile } from "node:child_process";

import {
  CustomerMaterialLibrary,
  ExternalEditHandoffService,
  ExternalEditInbox,
  ExternalEditPackageBuilder,
  ExternalEditReturnService,
  JianyingDesktopBridge,
  OfflineFixtureUnderstandingAdapter,
  PrivateContentStudio,
  type StudioScope,
} from "@wearable/mobile-agent";
import ffmpegPath from "ffmpeg-static";

const require = createRequire(import.meta.url);
const ffprobePath = (require("ffprobe-static") as { path?: unknown }).path;
const execFileAsync = promisify(execFile);

export type ExternalEditRoundtripPilotReport = Readonly<{
  syntheticOnly: true;
  publishingClaimed: false;
  mode: "file-package";
  degradedReason: "jianying-not-installed";
  parentDraftId: string;
  parentDigestBefore: string;
  parentDigestAfter: string;
  candidateDigest: string;
  returnedDraftId: string;
  returnedDraftDigest: string;
  initialDraftCount: number;
  beforeConfirmationDraftCount: number;
  finalDraftCount: number;
  packageFiles: Array<{ relativePath: string; digest: string; sizeBytes: number }>;
  v1Preserved: true;
  explicitCandidateConfirmation: true;
  v2Playable: true;
  handoffReturned: true;
  replayedHandoffStatus: "returned";
  replayedReturnDraftId: string;
  fullRuntimeRestartRecovery: "not-verified";
  evidenceScope: "local-synthetic-service-pilot";
  reportPath: string;
}>;

const scope: StudioScope = Object.freeze({
  actorId: "external-edit-pilot-operator",
  customerId: "external-edit-pilot-customer",
  sessionId: "external-edit-pilot-session",
});

function sha256(value: Buffer | string): string {
  return createHash("sha256").update(value).digest("hex");
}

async function runMedia(command: string, args: readonly string[]): Promise<void> {
  await new Promise<void>((resolvePromise, reject) => {
    const child = spawn(command, [...args], { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", reject);
    child.once("close", (status) => status === 0
      ? resolvePromise()
      : reject(new Error(`external-edit-pilot-media-failed:${stderr.trim()}`)));
  });
}

async function createSyntheticSource(path: string, index: number): Promise<void> {
  const colors = ["0x6d3b2d", "0x326a63", "0x876942", "0x425c78"];
  const tones = [392, 440, 494, 523];
  await runMedia(ffmpegPath!, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", `color=c=${colors[index]}:s=180x320:r=25:d=10.5`,
    "-f", "lavfi", "-i", `sine=frequency=${tones[index]}:sample_rate=44100:duration=10.5`,
    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-y", path,
  ]);
}

/** Real ffmpeg/ffprobe over synthetic media; no customer data or Jianying install is touched. */
export async function runExternalEditRoundtripPilot(workDirValue: string): Promise<ExternalEditRoundtripPilotReport> {
  if (!ffmpegPath || typeof ffprobePath !== "string") throw new Error("external-edit-pilot-media-tools-unavailable");
  const workDir = resolve(workDirValue);
  const runRoot = await mkdtemp(join(tmpdir(), "external-edit-roundtrip-"));
  const inputRoot = join(runRoot, "inputs");
  const outputRoot = join(runRoot, "outputs");
  const stateRoot = join(runRoot, "state");
  const reportPath = join(workDir, "external-edit-roundtrip-pilot.json");
  await Promise.all([
    mkdir(inputRoot, { recursive: true, mode: 0o700 }),
    mkdir(outputRoot, { recursive: true, mode: 0o700 }),
    mkdir(stateRoot, { recursive: true, mode: 0o700 }),
    mkdir(workDir, { recursive: true, mode: 0o700 }),
  ]);

  try {
    const library = new CustomerMaterialLibrary(join(runRoot, "library"), ({ actorId, customerId }) =>
      actorId === scope.actorId && customerId === scope.customerId);
    const studio = new PrivateContentStudio(library, new OfflineFixtureUnderstandingAdapter(), {
      outputRoot,
      now: () => "2026-09-30T10:00:00.000Z",
    });
    const assets = [];
    for (let index = 0; index < 4; index += 1) {
      const path = join(inputRoot, `synthetic-source-${index + 1}.mp4`);
      await createSyntheticSource(path, index);
      const bytes = await readFile(path);
      const asset = await library.importOriginal(scope.actorId, scope.customerId, {
        requestId: `external-edit-pilot-import-${index + 1}`,
        bytes,
        mediaKind: "video",
        fileName: `synthetic-source-${index + 1}.mp4`,
        capturedAt: `2026-09-30T10:00:0${index}.000Z`,
        sessionId: scope.sessionId,
        viewPermission: "allowed",
        usePermission: "allowed",
      });
      assets.push(asset);
      await studio.analyze(scope, asset.id, {
        synthetic: true,
        contentType: "single-product",
        segment: { startMs: 0, endMs: 10_500 },
        ffprobe: { durationSeconds: 10.5, hasVideo: true, hasAudio: true, width: 180, height: 320 },
        observations: [`合成镜头 ${index + 1}`],
        merchandiseFacts: ["example-attribute: 示例属性字段待人工确认"],
      });
    }

    const demand = await studio.createDemand(scope, {
      contentType: "single-product",
      goal: "用合成镜头解释珠宝制作过程",
      targetAudience: "合成测试观众",
      desiredEffect: "看懂工序和设计师观点",
      mustInclude: [],
      mustAvoid: [],
      submittedBy: "operator-on-behalf",
      operationKey: "external-edit-pilot-demand",
    });
    const assessment = await studio.assessDemand(scope, demand.id, "external-edit-pilot-match");
    if (assessment.verdict !== "ready") throw new Error("external-edit-pilot-match-not-ready");
    const discovery = await studio.discoverOpportunities(scope, {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    });
    const opportunity = discovery.opportunities[0];
    if (!opportunity) throw new Error("external-edit-pilot-opportunity-missing");
    const brief = await studio.confirmBrief(scope, {
      opportunityId: opportunity.id,
      matchAssessmentId: assessment.id,
      selectedAssetIds: assets.map((asset) => asset.id),
      confirmedFactIds: ["example-attribute: 示例属性字段待人工确认"],
      reviewRequirements: ["合成素材仅用于本地测试"],
      operationKey: "external-edit-pilot-brief",
    });
    const initialDrafts = await studio.createDrafts(scope, brief.id, "external-edit-pilot-drafts");
    if (initialDrafts.length !== 2) throw new Error("external-edit-pilot-v1-draft-count-invalid");
    const parent = initialDrafts[0]!;
    const alternative = initialDrafts[1]!;
    const parentBytesBefore = await studio.readDraftOutput(scope, parent.id);
    const parentDigestBefore = sha256(parentBytesBefore);

    const handoffsRoot = join(stateRoot, "handoffs");
    const handoffs = new ExternalEditHandoffService({
      root: handoffsRoot,
      now: () => "2026-09-30T10:00:00.000Z",
      resolveParentDraft: async (candidateScope, draftId) => {
        try { return await studio.getDraft(candidateScope, draftId); } catch { return null; }
      },
    });
    const handoff = await handoffs.create(scope, {
      parentDraftId: parent.id,
      parentDraftVersion: parent.version,
      parentDraftDigest: parent.outputDigest,
      platformVariant: "editorial-master",
      revisionInstruction: "节奏紧凑一些，保留设计师讲制作细节的原话。",
      mustKeep: ["设计师讲制作细节的原话"],
      includeSources: false,
      sourceAssetBindings: [],
      operationKey: "external-edit-pilot-handoff",
    });

    const scratch = join(runRoot, "parent-package-source.mp4");
    await writeFile(scratch, parentBytesBefore, { mode: 0o600, flag: "wx" });
    const packageBuilder = new ExternalEditPackageBuilder({
      root: join(stateRoot, "packages"),
      now: () => "2026-09-30T10:00:00.000Z",
      resolveSources: async (_handoff, currentParent) => [{
        role: "parent-video",
        sourcePath: scratch,
        relativePath: "video/parent.mp4",
        digest: currentParent.outputDigest,
      }],
    });
    const bridge = new JianyingDesktopBridge({
      root: join(stateRoot, "bridge"),
      packageBuilder,
      findApp: async () => null,
      openTarget: async () => undefined,
    });
    const prepared = await bridge.prepare(handoff, parent);
    if (prepared.mode !== "file-package" || prepared.degradedReason !== "jianying-not-installed") {
      throw new Error("external-edit-pilot-fallback-boundary-invalid");
    }
    await handoffs.transition(scope, {
      handoffId: handoff.id,
      status: "degraded",
      operationKey: "external-edit-pilot-package-prepared",
      packageDigest: prepared.package.packageDigest,
    });

    let clockTick = Date.parse("2026-09-30T10:00:00.000Z");
    const inbox = new ExternalEditInbox({
      root: join(stateRoot, "inbox"),
      stableForMs: 0,
      now: () => new Date(clockTick++).toISOString(),
      ffprobePath,
    });
    const returnDirectory = await inbox.createReturnDirectory(handoff);
    const syntheticExportPath = join(returnDirectory, "synthetic-export.mp4");
    await writeFile(syntheticExportPath, await studio.readDraftOutput(scope, alternative.id), { mode: 0o600, flag: "wx" });
    const returnsRoot = join(stateRoot, "return-receipts");
    const returns = new ExternalEditReturnService({
      root: returnsRoot,
      inbox,
      handoffs,
      studio,
      now: () => new Date(clockTick++).toISOString(),
    });
    const candidates = await returns.listCandidates(scope, handoff.id);
    if (candidates.length !== 1) throw new Error("external-edit-pilot-candidate-count-invalid");
    const beforeConfirmation = await studio.listStudioHistory(scope);
    if (beforeConfirmation.drafts.length !== initialDrafts.length) {
      throw new Error("external-edit-pilot-created-v2-before-confirmation");
    }
    const accepted = await returns.accept(scope, handoff.id, candidates[0]!.id, "external-edit-pilot-confirm-v2");
    const afterConfirmation = await studio.listStudioHistory(scope);
    const parentAfter = await studio.getDraft(scope, parent.id);
    const acceptedBytes = await studio.readDraftOutput(scope, accepted.id);
    const acceptedDigest = sha256(acceptedBytes);
    if (acceptedDigest !== accepted.outputDigest) throw new Error("external-edit-pilot-v2-digest-invalid");
    const probe = await execFileAsync(ffprobePath, [
      "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
      "-of", "json", accepted.outputPath,
    ], { timeout: 15_000, maxBuffer: 1024 * 1024 });
    const media = JSON.parse(probe.stdout) as { streams?: Array<{ codec_type: string }>; format?: { duration?: string } };
    if (!media.streams?.some((stream) => stream.codec_type === "video") || Number(media.format?.duration) < 30) {
      throw new Error("external-edit-pilot-v2-not-decodable");
    }
    if (afterConfirmation.drafts.length !== initialDrafts.length + 1
      || parentAfter.outputDigest !== parentDigestBefore
      || accepted.externalEdit?.lineageConfidence !== "parent-only"
      || accepted.publishClaimed !== false) {
      throw new Error("external-edit-pilot-version-lineage-invalid");
    }
    const replayedHandoffs = new ExternalEditHandoffService({
      root: handoffsRoot,
      resolveParentDraft: async (candidateScope, draftId) => {
        try { return await studio.getDraft(candidateScope, draftId); } catch { return null; }
      },
    });
    const replayedHandoff = await replayedHandoffs.get(scope, handoff.id);
    const replayedReturns = new ExternalEditReturnService({ root: returnsRoot, inbox, handoffs: replayedHandoffs, studio });
    const replayedDraft = await replayedReturns.accept(scope, handoff.id, candidates[0]!.id, "external-edit-pilot-confirm-v2");
    if (replayedHandoff.status !== "returned" || replayedDraft.id !== accepted.id) {
      throw new Error("external-edit-pilot-service-replay-invalid");
    }

    const packageFiles = prepared.package.files.map((file) => ({
      relativePath: file.relativePath,
      digest: file.digest,
      sizeBytes: file.sizeBytes,
    }));
    const report: ExternalEditRoundtripPilotReport = {
      syntheticOnly: true,
      publishingClaimed: false,
      mode: "file-package",
      degradedReason: "jianying-not-installed",
      parentDraftId: parent.id,
      parentDigestBefore,
      parentDigestAfter: parentAfter.outputDigest,
      candidateDigest: candidates[0]!.digest,
      returnedDraftId: accepted.id,
      returnedDraftDigest: acceptedDigest,
      initialDraftCount: initialDrafts.length,
      beforeConfirmationDraftCount: beforeConfirmation.drafts.length,
      finalDraftCount: afterConfirmation.drafts.length,
      packageFiles,
      v1Preserved: true,
      explicitCandidateConfirmation: true,
      v2Playable: true,
      handoffReturned: true,
      replayedHandoffStatus: "returned",
      replayedReturnDraftId: replayedDraft.id,
      fullRuntimeRestartRecovery: "not-verified",
      evidenceScope: "local-synthetic-service-pilot",
      reportPath,
    };
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
    return report;
  } finally {
    await rm(runRoot, { recursive: true, force: true });
  }
}
