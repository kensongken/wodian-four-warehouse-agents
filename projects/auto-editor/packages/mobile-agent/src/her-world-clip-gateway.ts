import {
  HerWorldClipHandoffV1Schema,
  HerWorldClipReceiptV1Schema,
  assertHerWorldClipReceiptMatches,
  type HerWorldClipHandoffV1,
  type HerWorldClipReceiptV1,
} from "@wearable/contracts";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

import { HerWorldClipIntake, type HerWorldStagedClipJob } from "./her-world-clip-intake";

type Fetcher = (url: string, init: RequestInit) => Promise<Response>;
type GatewayOptions = Readonly<{
  baseUrl: string;
  jobId: string;
  bearerToken: string;
  fetcher?: Fetcher;
  requestTimeoutMs?: number;
  outputUploadTimeoutMs?: number;
}>;

type RunnerOptions = Readonly<{
  baseUrl: string;
  runnerSecret: string;
  fetcher?: Fetcher;
  requestTimeoutMs?: number;
}>;

export type HerWorldClaimedClipJob = Readonly<{
  jobId: string;
  jobToken: string;
  handoff: HerWorldClipHandoffV1;
}>;

export type HerWorldClipGatewaySnapshot = Readonly<{
  handoff: HerWorldClipHandoffV1;
  status: HerWorldClipReceiptV1["status"];
  progress: number;
  receipt: HerWorldClipReceiptV1 | null;
  expiresAt: string;
}>;

export class HerWorldClipGatewayClient {
  private readonly jobUrl: string;
  private readonly token: string;
  private readonly fetcher: Fetcher;
  private readonly requestTimeoutMs: number;
  private readonly outputUploadTimeoutMs: number;

  constructor(options: GatewayOptions) {
    const base = new URL(options.baseUrl);
    if (base.protocol !== "https:" || base.username || base.password) throw new Error("her-world-gateway-url-invalid");
    if (!/^[a-f0-9]{32}$/.test(options.jobId)) throw new Error("her-world-gateway-job-id-invalid");
    if (!/^[A-Za-z0-9._~-]{32,256}$/.test(options.bearerToken)) throw new Error("her-world-gateway-token-invalid");
    this.jobUrl = new URL(`/api/clip/jobs/${options.jobId}`, base.origin).toString();
    this.token = options.bearerToken;
    this.fetcher = options.fetcher ?? ((url, init) => fetch(url, init));
    this.requestTimeoutMs = options.requestTimeoutMs ?? 30_000;
    if (!Number.isSafeInteger(this.requestTimeoutMs) || this.requestTimeoutMs < 1) throw new Error("her-world-gateway-timeout-invalid");
    this.outputUploadTimeoutMs = options.outputUploadTimeoutMs ?? 120_000;
    if (!Number.isSafeInteger(this.outputUploadTimeoutMs) || this.outputUploadTimeoutMs < 1) {
      throw new Error("her-world-gateway-output-timeout-invalid");
    }
  }

  async read(): Promise<HerWorldClipGatewaySnapshot> {
    const response = await this.request("GET");
    let body: unknown;
    try { body = await response.json(); }
    catch { throw new Error("her-world-gateway-response-invalid"); }
    if (!body || typeof body !== "object") throw new Error("her-world-gateway-response-invalid");
    const row = body as Record<string, unknown>;
    const handoff = HerWorldClipHandoffV1Schema.parse(row.handoff);
    if (this.jobUrl.endsWith(`/${handoff.jobId}`) === false) throw new Error("her-world-gateway-job-binding-mismatch");
    const receipt = row.receipt === null || row.receipt === undefined ? null : HerWorldClipReceiptV1Schema.parse(row.receipt);
    if (receipt) assertHerWorldClipReceiptMatches(handoff, receipt);
    const status = typeof row.status === "string" ? row.status : "";
    const progress = Number(row.progress);
    const expiresAt = typeof row.expires_at === "string" ? row.expires_at : "";
    if (!["queued", "pulling", "rendering", "review-ready", "failed", "cancelled"].includes(status)
      || !Number.isInteger(progress) || progress < 0 || progress > 100 || !Number.isFinite(Date.parse(expiresAt))) {
      throw new Error("her-world-gateway-response-invalid");
    }
    return { handoff, status: status as HerWorldClipGatewaySnapshot["status"], progress, receipt, expiresAt };
  }

  async stage(intake: HerWorldClipIntake): Promise<{ snapshot: HerWorldClipGatewaySnapshot; staged: HerWorldStagedClipJob }> {
    const snapshot = await this.read();
    const staged = await intake.stage(snapshot.handoff, this.token);
    return { snapshot, staged };
  }

  async report(handoff: HerWorldClipHandoffV1, receiptValue: HerWorldClipReceiptV1): Promise<void> {
    const receipt = HerWorldClipReceiptV1Schema.parse(receiptValue);
    assertHerWorldClipReceiptMatches(HerWorldClipHandoffV1Schema.parse(handoff), receipt);
    const response = await this.request("PATCH", receipt);
    const body = await response.json().catch(() => null) as { ok?: unknown } | null;
    if (!body?.ok) throw new Error("her-world-gateway-receipt-not-accepted");
  }

  async uploadOutput(handoffValue: HerWorldClipHandoffV1, receiptValue: HerWorldClipReceiptV1, outputPath: string): Promise<void> {
    const handoff = HerWorldClipHandoffV1Schema.parse(handoffValue);
    const receipt = HerWorldClipReceiptV1Schema.parse(receiptValue);
    assertHerWorldClipReceiptMatches(handoff, receipt);
    if (receipt.status !== "review-ready" || receipt.outputs.length !== 1) throw new Error("her-world-gateway-output-not-review-ready");
    const output = receipt.outputs[0]!;
    if (output.relativePath !== `${handoff.jobId}/outputs/${output.profile}.mp4`) throw new Error("her-world-gateway-output-path-invalid");
    const bytes = await readFile(outputPath);
    const digest = createHash("sha256").update(bytes).digest("hex");
    if (digest !== output.sha256) throw new Error("her-world-gateway-output-digest-mismatch");
    const response = await this.requestOutput(output.profile, bytes, digest);
    const body = await response.json().catch(() => null) as { ok?: unknown; sha256?: unknown } | null;
    if (!body?.ok || body.sha256 !== digest) throw new Error("her-world-gateway-output-not-accepted");
  }

  private async request(method: "GET" | "PATCH", body?: unknown): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    try {
      const response = await this.fetcher(this.jobUrl, {
        method,
        headers: {
          authorization: `Bearer ${this.token}`,
          ...(body === undefined ? {} : { "content-type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: "error",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`her-world-gateway-request-failed:${response.status}`);
      return response;
    } finally {
      clearTimeout(timer);
    }
  }

  private async requestOutput(profile: string, bytes: Uint8Array, digest: string): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.outputUploadTimeoutMs);
    try {
      const body = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
      const response = await this.fetcher(`${this.jobUrl}/output/${encodeURIComponent(profile)}`, {
        method: "PUT",
        headers: {
          authorization: `Bearer ${this.token}`,
          "content-type": "video/mp4",
          "content-length": String(bytes.byteLength),
          "x-content-sha256": digest,
        },
        body,
        redirect: "error",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`her-world-gateway-output-request-failed:${response.status}`);
      return response;
    } catch (error) {
      if ((error as { name?: unknown }).name === "AbortError") {
        throw new Error("her-world-gateway-output-timeout");
      }
      throw error;
    } finally { clearTimeout(timer); }
  }
}

export class HerWorldClipRunnerClient {
  private readonly claimUrl: string;
  private readonly runnerSecret: string;
  private readonly fetcher: Fetcher;
  private readonly requestTimeoutMs: number;

  constructor(options: RunnerOptions) {
    const base = new URL(options.baseUrl);
    if (base.protocol !== "https:" || base.username || base.password) throw new Error("her-world-runner-url-invalid");
    if (!/^[A-Za-z0-9._~-]{32,256}$/.test(options.runnerSecret)) throw new Error("her-world-runner-secret-invalid");
    this.claimUrl = new URL("/api/clip/runner", base.origin).toString();
    this.runnerSecret = options.runnerSecret;
    this.fetcher = options.fetcher ?? ((url, init) => fetch(url, init));
    this.requestTimeoutMs = options.requestTimeoutMs ?? 30_000;
  }

  async claimNext(): Promise<HerWorldClaimedClipJob | null> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    try {
      const response = await this.fetcher(this.claimUrl, {
        method: "POST", headers: { authorization: `Bearer ${this.runnerSecret}` }, redirect: "error", signal: controller.signal,
      });
      if (response.status === 204) return null;
      if (!response.ok) throw new Error(`her-world-runner-claim-failed:${response.status}`);
      const body = await response.json().catch(() => null) as Record<string, unknown> | null;
      const jobId = typeof body?.job_id === "string" ? body.job_id : "";
      const jobToken = typeof body?.job_token === "string" ? body.job_token : "";
      const handoff = HerWorldClipHandoffV1Schema.parse(body?.handoff);
      if (jobId !== handoff.jobId || !/^[a-f0-9]{32}$/.test(jobId) || !/^[A-Za-z0-9._~-]{32,256}$/.test(jobToken)) {
        throw new Error("her-world-runner-claim-binding-invalid");
      }
      return { jobId, jobToken, handoff };
    } finally { clearTimeout(timer); }
  }
}
