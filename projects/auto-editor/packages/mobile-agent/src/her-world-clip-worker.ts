import type { HerWorldClipHandoffV1, HerWorldClipReceiptV1 } from "@wearable/contracts";

import type { HerWorldClipGatewayClient, HerWorldClaimedClipJob } from "./her-world-clip-gateway";
import type { HerWorldClipIntake } from "./her-world-clip-intake";
import type { HerWorldStoryRenderer } from "./her-world-story-renderer";

type RunnerLike = Readonly<{ claimNext(): Promise<HerWorldClaimedClipJob | null> }>;
type GatewayLike = Pick<HerWorldClipGatewayClient, "stage" | "report" | "uploadOutput">;

type WorkerOptions = Readonly<{
  runner: RunnerLike;
  gatewayFor: (claim: HerWorldClaimedClipJob) => GatewayLike;
  intake: HerWorldClipIntake;
  renderer: HerWorldStoryRenderer;
  now?: () => string;
}>;

export type HerWorldClipWorkerResult =
  | Readonly<{ outcome: "no-job" }>
  | Readonly<{ outcome: "review-ready" | "needs-voice" | "needs-review"; jobId: string }>
  | Readonly<{ outcome: "failed"; jobId: string; failureReported: boolean; failureCode: string }>;

type WorkerStep = "download" | "start-render" | "render" | "upload" | "finish";

function safeFailure(error: unknown, step: WorkerStep): NonNullable<HerWorldClipReceiptV1["error"]> {
  const message = error instanceof Error ? error.message : "";
  if (step === "upload") {
    if (message === "her-world-gateway-output-timeout") {
      return {
        code: "clip-output-upload-timeout",
        message: "成片已经生成，但送回网站超时。原故事和成片都还在，可以重新制作。",
        retryable: true,
      };
    }
    return {
      code: "clip-output-upload-failed",
      message: "成片已经生成，但没有完整送回网站。原故事和成片都还在，可以重新制作。",
      retryable: true,
    };
  }
  if (step === "finish") {
    return {
      code: "clip-receipt-failed",
      message: "成片已经送达，但网站没有完成登记。原故事和成片都还在，可以重新制作。",
      retryable: true,
    };
  }
  if (step === "download") {
    return {
      code: "clip-media-download-failed",
      message: "照片或声音没有完整取回，尚未开始制作。原素材不会被覆盖，可以重新制作。",
      retryable: true,
    };
  }
  return {
    code: "clip-render-failed",
    message: "剪辑电脑没有完成合成。原故事与素材没有被覆盖，可以重新制作。",
    retryable: true,
  };
}

function receiptFor(
  handoff: HerWorldClipHandoffV1,
  status: HerWorldClipReceiptV1["status"],
  progress: number,
  updatedAt: string,
  error: HerWorldClipReceiptV1["error"] = null,
): HerWorldClipReceiptV1 {
  return {
    schemaVersion: 1,
    kind: "her-world-story-render-receipt",
    jobId: handoff.jobId,
    storyId: handoff.story.id,
    storyRevision: handoff.story.revision,
    status,
    progress,
    outputs: [],
    ownerAccepted: false,
    published: false,
    error,
    updatedAt,
  };
}

/**
 * Processes at most one claimed story. The worker can return a candidate for
 * owner review, but it never accepts or publishes on the owner's behalf.
 */
export class HerWorldClipWorker {
  private readonly runner: RunnerLike;
  private readonly gatewayFor: WorkerOptions["gatewayFor"];
  private readonly intake: HerWorldClipIntake;
  private readonly renderer: HerWorldStoryRenderer;
  private readonly now: () => string;

  constructor(options: WorkerOptions) {
    this.runner = options.runner;
    this.gatewayFor = options.gatewayFor;
    this.intake = options.intake;
    this.renderer = options.renderer;
    this.now = options.now ?? (() => new Date().toISOString());
  }

  async runOnce(): Promise<HerWorldClipWorkerResult> {
    const claim = await this.runner.claimNext();
    if (!claim) return { outcome: "no-job" };
    const gateway = this.gatewayFor(claim);
    let step: WorkerStep = "download";
    try {
      const { staged } = await gateway.stage(this.intake);
      step = "start-render";
      await gateway.report(claim.handoff, receiptFor(claim.handoff, "rendering", 20, this.now()));
      step = "render";
      const result = await this.renderer.renderVisualDraft(staged);
      if (result.readiness === "review-ready") {
        step = "upload";
        await gateway.uploadOutput(claim.handoff, result.progressReceipt, result.outputPath);
        step = "finish";
        await gateway.report(claim.handoff, result.progressReceipt);
        return { outcome: "review-ready", jobId: claim.jobId };
      }
      if (result.readiness === "needs-review") {
        await gateway.report(claim.handoff, result.progressReceipt);
        return { outcome: "needs-review", jobId: claim.jobId };
      }
      await gateway.report(claim.handoff, receiptFor(claim.handoff, "failed", 80, this.now(), {
        code: "approved-voice-required",
        message: "这条故事还缺少店主本人认可的声音，候选片没有进入确认环节。",
        retryable: true,
      }));
      return { outcome: "needs-voice", jobId: claim.jobId };
    } catch (error) {
      const failure = safeFailure(error, step);
      let failureReported = false;
      try {
        await gateway.report(claim.handoff, receiptFor(claim.handoff, "failed", 0, this.now(), failure));
        failureReported = true;
      } catch {
        // The claim will expire and can be recovered without exposing secrets.
      }
      return { outcome: "failed", jobId: claim.jobId, failureReported, failureCode: failure.code };
    }
  }
}
