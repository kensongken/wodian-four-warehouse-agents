import { execFile, spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  chmodSync,
  closeSync,
  constants,
  fchmodSync,
  fstatSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readSync,
  realpathSync,
  rmdirSync,
  unlinkSync,
  writeSync,
  type BigIntStats,
} from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { TextDecoder } from "node:util";

import {
  assertReceiptMatchesJob,
  ModelAnalysisJobV1Schema,
  ModelAnalysisReceiptV1Schema,
  StudioModelStateV1Schema,
  type ModelAnalysisJobV1,
  type ModelAnalysisReceiptV1,
  type StudioModelStateV1,
} from "@wearable/contracts";
import {
  createStudioWorkerStageTelemetryTracker,
  type StudioWorkerStageTelemetrySummaryV1,
} from "./studio-worker-stage-telemetry";

export interface StudioModelWorker {
  run(
    job: ModelAnalysisJobV1,
    signal: AbortSignal,
    context?: StudioTrustedWorkerContext,
  ): Promise<ModelAnalysisReceiptV1>;
  health(): Promise<StudioModelStateV1>;
}

export type StudioTrustedWorkerContext = Readonly<{
  customerId: string;
  sessionId: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  transcriptSpans: readonly Readonly<{
    sourceDigest: string;
    startMs: number;
    endMs: number;
    text: string;
  }>[];
  demand: Readonly<{
    demandId: string;
    matchAssessmentId: string;
    sourceDigest: string;
    sourceStartMs: number;
    sourceEndMs: number;
    text: string;
  }> | null;
}>;

export type LocalProcessStudioModelWorkerConfig = Readonly<{
  command: string;
  commandIdentity?: Readonly<{
    device: string;
    inode: string;
    byteLength: number;
    sha256: string;
  }>;
  args: readonly string[];
  cwd: string;
  env: Readonly<Record<string, string>>;
  timeoutMs: number;
  maxStdoutBytes: number;
  maxStderrBytes: number;
  allowedMediaRoot: string;
  stageTelemetry?: null | Readonly<{
    protocol: "studio-worker-stage-telemetry-v1";
    maxBytes: 4096;
  }>;
}>;

type LocalProcessStudioModelWorkerInput = Omit<LocalProcessStudioModelWorkerConfig, "env"> & Readonly<{
  env?: Readonly<Record<string, string>>;
  spawnProcess?: WorkerSpawn;
}>;

type NormalizedLocalProcessStudioModelWorkerConfig = Omit<
  LocalProcessStudioModelWorkerConfig,
  "stageTelemetry"
> & Readonly<{
  stageTelemetry: NonNullable<LocalProcessStudioModelWorkerConfig["stageTelemetry"]> | null;
}>;

type WorkerStdio = ["pipe", "pipe", "pipe"] | ["pipe", "pipe", "pipe", "pipe"];

type WorkerSpawn = (
  command: string,
  args: readonly string[],
  options: {
    cwd: string;
    detached: boolean;
    env: Readonly<Record<string, string>>;
    shell: false;
    stdio: WorkerStdio;
  },
) => ChildProcessWithoutNullStreams;

const FIXED_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
const FORMAL_LAUNCHER_SOURCE_WRAPPER = 'launcher_copy=$1; shift; . "$launcher_copy"';
const FIXED_ENVIRONMENT_KEYS = [
  "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
] as const;
type OwnedWorkerProcess = {
  child: ChildProcessWithoutNullStreams;
  processGroupId: number | null;
  released: boolean;
  releasePromise?: Promise<boolean>;
};

const activeWorkerChildren = new Set<OwnedWorkerProcess>();

export class StudioModelWorkerTimeoutError extends Error {
  readonly stageTelemetry: StudioWorkerStageTelemetrySummaryV1 | null;

  constructor(stageTelemetry: StudioWorkerStageTelemetrySummaryV1 | null) {
    super("studio-model-worker-timeout");
    this.name = "StudioModelWorkerTimeoutError";
    this.stageTelemetry = stageTelemetry;
  }
}

export class StudioModelWorkerFailureError extends Error {
  readonly receipt: ModelAnalysisReceiptV1;
  readonly stageTelemetry: StudioWorkerStageTelemetrySummaryV1;

  constructor(
    receipt: ModelAnalysisReceiptV1,
    stageTelemetry: StudioWorkerStageTelemetrySummaryV1,
  ) {
    super("studio-model-worker-analysis-failed");
    this.name = "StudioModelWorkerFailureError";
    this.receipt = receipt;
    this.stageTelemetry = stageTelemetry;
  }
}

installParentLifecycleCleanup();

export class LocalProcessStudioModelWorker implements StudioModelWorker {
  private readonly config: NormalizedLocalProcessStudioModelWorkerConfig;
  private readonly spawnProcess: WorkerSpawn;

  constructor(input: LocalProcessStudioModelWorkerInput) {
    const { spawnProcess = spawn as WorkerSpawn, ...configInput } = input;
    let config: NormalizedLocalProcessStudioModelWorkerConfig;
    try {
      config = {
        ...configInput,
        env: configInput.env ?? fixedOfflineEnvironment(configInput.allowedMediaRoot),
        stageTelemetry: configInput.stageTelemetry ?? null,
      };
    } catch {
      throw new Error("studio-model-worker-config-invalid");
    }
    if (
      !config.command.trim()
      || !config.cwd.trim()
      || !config.allowedMediaRoot.trim()
      || !isTrustedWorkerDirectory(config.cwd)
      || (config.commandIdentity !== undefined && !isValidCommandIdentity(config.commandIdentity))
      || !isValidEnvironment(config.env)
      || !isPositiveInteger(config.timeoutMs)
      || !isPositiveInteger(config.maxStdoutBytes)
      || !isPositiveInteger(config.maxStderrBytes)
      || !isValidStageTelemetryConfig(config.stageTelemetry)
    ) {
      throw new Error("studio-model-worker-config-invalid");
    }
    this.config = Object.freeze({
      ...config,
      args: Object.freeze([...config.args]),
      env: Object.freeze({ ...config.env }),
      stageTelemetry: config.stageTelemetry === null
        ? null
        : Object.freeze({ ...config.stageTelemetry }),
      ...(config.commandIdentity === undefined
        ? {}
        : { commandIdentity: Object.freeze({ ...config.commandIdentity }) }),
    });
    this.spawnProcess = spawnProcess;
  }

  async health(): Promise<StudioModelStateV1> {
    return StudioModelStateV1Schema.parse({
      mode: "offline-fallback",
      installed: false,
      modelName: null,
      checkpointDigest: null,
    });
  }

  run(
    jobInput: ModelAnalysisJobV1,
    signal: AbortSignal,
    context?: StudioTrustedWorkerContext,
  ): Promise<ModelAnalysisReceiptV1> {
    let job: ModelAnalysisJobV1;
    try {
      job = ModelAnalysisJobV1Schema.parse(jobInput);
    } catch {
      return Promise.reject(new Error("studio-model-worker-invalid-job"));
    }

    let canonicalSourcePath: string;
    try {
      canonicalSourcePath = this.resolveAllowedSource(job.sourcePath);
    } catch (error) {
      return Promise.reject(error);
    }
    try {
      job = ModelAnalysisJobV1Schema.parse({ ...job, sourcePath: canonicalSourcePath });
    } catch {
      return Promise.reject(new Error("studio-model-worker-invalid-job"));
    }
    try {
      assertTrustedWorkerContext(job, context);
    } catch (error) {
      return Promise.reject(error);
    }
    if (signal.aborted) return Promise.reject(abortError(signal));

    let staged: StagedSource;
    try {
      staged = stageSource(job.sourcePath, this.config.env.TMPDIR!);
    } catch {
      return Promise.reject(new Error("studio-model-source-integrity-failed"));
    }
    const originalJob = job;
    try {
      job = ModelAnalysisJobV1Schema.parse({ ...job, sourcePath: staged.path });
    } catch {
      staged.cleanup();
      return Promise.reject(new Error("studio-model-worker-invalid-job"));
    }
    return this.runChild(job, signal, context, originalJob)
      .finally(() => staged.cleanup());
  }

  private resolveAllowedSource(sourcePath: string): string {
    let allowedRoot: string;
    let source: string;
    try {
      allowedRoot = realpathSync(this.config.allowedMediaRoot);
      source = realpathSync(isAbsolute(sourcePath) ? sourcePath : resolve(this.config.cwd, sourcePath));
    } catch {
      throw new Error("studio-model-source-unavailable");
    }
    const withinRoot = relative(allowedRoot, source);
    if (
      withinRoot === ".."
      || withinRoot.startsWith(`..${sep}`)
      || isAbsolute(withinRoot)
    ) {
      throw new Error("studio-model-source-outside-allowed-root");
    }
    return source;
  }

  private runChild(
    job: ModelAnalysisJobV1,
    signal: AbortSignal,
    context: StudioTrustedWorkerContext | undefined,
    receiptJob: ModelAnalysisJobV1 = job,
  ): Promise<ModelAnalysisReceiptV1> {
    return new Promise((resolveRun, rejectRun) => {
      let child: ChildProcessWithoutNullStreams;
      let verifiedCommand: VerifiedCommand;
      try {
        verifiedCommand = prepareVerifiedCommand(
          this.config.command,
          this.config.commandIdentity,
          this.config.env.TMPDIR!,
        );
      } catch {
        rejectRun(new Error("studio-model-worker-launcher-integrity-failed"));
        return;
      }
      const detached = process.platform !== "win32";
      try {
        const telemetryEnabled = this.config.stageTelemetry !== null;
        child = this.spawnProcess(
          verifiedCommand.path,
          [
            ...verifiedCommand.argsPrefix,
            ...this.config.args,
            ...(telemetryEnabled ? ["--telemetry-fd", "3"] : []),
          ],
          {
            cwd: this.config.cwd,
            detached,
            env: context === undefined
              ? this.config.env
              : { ...this.config.env, STUDIO_VISION_CONTEXT_JSON: JSON.stringify(context) },
            shell: false,
            stdio: telemetryEnabled
              ? ["pipe", "pipe", "pipe", "pipe"]
              : ["pipe", "pipe", "pipe"],
          },
        );
      } catch {
        verifiedCommand.cleanup();
        rejectRun(new Error("studio-model-worker-spawn-failed"));
        return;
      }
      const ownedProcess = trackActiveChild(child, detached);

      const stdout: Buffer[] = [];
      let stdoutBytes = 0;
      let stderrBytes = 0;
      let terminalError: Error | null = null;
      let settled = false;
      const telemetryTracker = this.config.stageTelemetry === null
        ? null
        : createStudioWorkerStageTelemetryTracker({
          nowMs: () => performance.now(),
          maxBytes: this.config.stageTelemetry.maxBytes,
        });

      const cleanup = () => {
        clearTimeout(timer);
        signal.removeEventListener("abort", onAbort);
        verifiedCommand.cleanup();
      };
      const finishError = (error: Error) => {
        if (settled) return;
        settled = true;
        void releaseOwnedWorkerProcess(ownedProcess).then((released) => {
          cleanup();
          rejectRun(released ? error : new Error("studio-model-worker-process-cleanup-failed"));
        });
      };
      const finishSuccess = (receipt: ModelAnalysisReceiptV1) => {
        if (settled) return;
        settled = true;
        void releaseOwnedWorkerProcess(ownedProcess).then((released) => {
          cleanup();
          if (released) resolveRun(receipt);
          else rejectRun(new Error("studio-model-worker-process-cleanup-failed"));
        });
      };
      const terminate = (error: Error) => {
        if (settled || terminalError) return;
        terminalError = error;
        signalOwnedWorkerProcess(ownedProcess);
      };
      const onAbort = () => terminate(abortError(signal));
      const timer = setTimeout(() => {
        terminate(new StudioModelWorkerTimeoutError(null));
      }, this.config.timeoutMs);

      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();

      child.stdout.on("data", (chunk: Buffer | string) => {
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        stdoutBytes += bytes.byteLength;
        if (stdoutBytes > this.config.maxStdoutBytes) {
          terminate(new Error("studio-model-worker-stdout-overflow"));
          return;
        }
        stdout.push(bytes);
      });
      child.stderr.on("data", (chunk: Buffer | string) => {
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        stderrBytes += bytes.byteLength;
        if (stderrBytes > this.config.maxStderrBytes) {
          terminate(new Error("studio-model-worker-stderr-overflow"));
        }
      });
      if (telemetryTracker !== null) {
        const telemetryStream = child.stdio[3];
        if (telemetryStream == null || !("on" in telemetryStream)) {
          terminate(new Error("telemetry-invalid"));
        } else {
          telemetryStream.on("data", (chunk: Buffer | string) => {
            try {
              telemetryTracker.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
            } catch (error) {
              terminate(new Error(
                error instanceof Error && error.message === "telemetry-overflow"
                  ? "telemetry-overflow"
                  : "telemetry-invalid",
              ));
            }
          });
        }
      }
      child.stdin.on("error", () => {
        // The close/error handlers map the process outcome without exposing raw I/O.
      });
      child.once("error", () => finishError(
        terminalError ?? new Error("studio-model-worker-spawn-failed"),
      ));
      child.once("close", (code) => {
        if (settled) return;
        if (terminalError instanceof StudioModelWorkerTimeoutError) {
          try {
            const summary = telemetryTracker?.completeTerminated(performance.now()) ?? null;
            terminalError = new StudioModelWorkerTimeoutError(summary);
          } catch (error) {
            terminalError = new Error(
              error instanceof Error && error.message === "telemetry-overflow"
                ? "telemetry-overflow"
                : "telemetry-invalid",
            );
          }
        }
        if (terminalError) {
          finishError(terminalError);
          return;
        }
        if (code !== 0) {
          finishError(new Error("studio-model-worker-retryable-failure"));
          return;
        }
        try {
          const receipt = parseSingleReceipt(
            new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(stdout)),
          );
          assertReceiptMatchesJob(job, receipt);
          if (receipt.inputDigest !== canonicalJobDigest(job)) {
            throw new Error("studio-model-receipt-binding-mismatch");
          }
          const publicReceipt = ModelAnalysisReceiptV1Schema.parse({
            ...receipt,
            sourcePath: receiptJob.sourcePath,
            inputDigest: canonicalJobDigest(receiptJob),
          });
          assertReceiptMatchesJob(receiptJob, publicReceipt);
          if (telemetryTracker !== null) {
            if (publicReceipt.status === "succeeded") {
              telemetryTracker.complete();
            } else {
              const summary = telemetryTracker.completeTerminated(performance.now());
              finishError(new StudioModelWorkerFailureError(publicReceipt, summary));
              return;
            }
          }
          finishSuccess(publicReceipt);
        } catch (error) {
          if (
            error instanceof Error
            && ["telemetry-invalid", "telemetry-overflow"].includes(error.message)
          ) {
            finishError(new Error(error.message));
            return;
          }
          if (
            error instanceof Error
            && [
              "studio-model-receipt-binding-mismatch",
              "studio-model-worker-receipt-line-count-invalid",
            ].includes(error.message)
          ) {
            finishError(new Error(error.message));
          } else {
            finishError(new Error("studio-model-worker-invalid-receipt"));
          }
        }
      });
      const sendJob = async (waitForInterpreter: boolean) => {
        try {
          verifiedCommand.revalidate();
          if (waitForInterpreter) await waitForLauncherInterpreterHandoff(child);
          if (settled) return;
          if (waitForInterpreter) verifiedCommand.revalidate();
          verifiedCommand.cleanup();
          child.stdin.end(`${JSON.stringify(job)}\n`);
        } catch {
          terminate(new Error("studio-model-worker-launcher-integrity-failed"));
        }
      };
      if (this.config.commandIdentity === undefined) void sendJob(false);
      else child.once("spawn", () => { void sendJob(true); });
    });
  }
}

function isValidStageTelemetryConfig(
  value: NormalizedLocalProcessStudioModelWorkerConfig["stageTelemetry"],
): boolean {
  return value === null || (
    typeof value === "object"
    && Object.keys(value).sort().join("\0") === "maxBytes\0protocol"
    && value.protocol === "studio-worker-stage-telemetry-v1"
    && value.maxBytes === 4096
  );
}

async function waitForLauncherInterpreterHandoff(
  child: ChildProcessWithoutNullStreams,
): Promise<void> {
  if (process.platform === "win32" || child.pid === undefined) {
    throw new Error("studio-model-worker-launcher-integrity-failed");
  }
  while (true) {
    const command = await processCommand(child.pid);
    if (command !== "/bin/sh" && command !== "sh") return;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 10));
  }
}

function processCommand(pid: number): Promise<string> {
  return new Promise((resolveCommand, rejectCommand) => {
    execFile(
      "/bin/ps",
      ["-p", String(pid), "-o", "comm="],
      { encoding: "utf8", env: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin" }, maxBuffer: 4_096 },
      (error, stdout) => {
        if (error || stdout.trim().length === 0) {
          rejectCommand(new Error("studio-model-worker-launcher-integrity-failed"));
          return;
        }
        resolveCommand(stdout.trim());
      },
    );
  });
}

function assertTrustedWorkerContext(
  job: ModelAnalysisJobV1,
  context: StudioTrustedWorkerContext | undefined,
): void {
  if (context === undefined) return;
  const expectedKeys = [
    "customerId", "sessionId", "sourceAssetId", "sourceDigest", "sourceStartMs",
    "sourceEndMs", "transcriptSpans", "demand",
  ].sort();
  if (
    job.layer !== "vision"
    || Object.keys(context).sort().join("\0") !== expectedKeys.join("\0")
    || context.customerId !== job.customerId
    || context.sessionId !== job.sessionId
    || context.sourceAssetId !== job.sourceAssetId
    || context.sourceDigest !== job.sourceDigest
    || context.sourceStartMs !== job.sourceStartMs
    || context.sourceEndMs !== job.sourceEndMs
  ) throw new Error("studio-model-worker-context-binding-mismatch");
  if (!Array.isArray(context.transcriptSpans)) {
    throw new Error("studio-model-worker-context-invalid");
  }
  for (const span of context.transcriptSpans) {
    if (
      !span || typeof span !== "object"
      || span.sourceDigest !== job.sourceDigest
      || !Number.isSafeInteger(span.startMs)
      || !Number.isSafeInteger(span.endMs)
      || span.startMs < job.sourceStartMs
      || span.endMs > job.sourceEndMs
      || span.endMs <= span.startMs
      || typeof span.text !== "string"
      || !span.text.trim()
    ) throw new Error("studio-model-worker-context-binding-mismatch");
  }
  if (context.demand !== null) {
    const demand = context.demand;
    if (
      !demand.demandId.trim()
      || !demand.matchAssessmentId.trim()
      || !demand.text.trim()
      || demand.sourceDigest !== job.sourceDigest
      || demand.sourceStartMs !== job.sourceStartMs
      || demand.sourceEndMs !== job.sourceEndMs
    ) throw new Error("studio-model-worker-context-binding-mismatch");
  }
}

function parseSingleReceipt(stdout: string): ModelAnalysisReceiptV1 {
  if (!stdout.endsWith("\n")) {
    throw new Error("studio-model-worker-receipt-line-count-invalid");
  }
  const line = stdout.slice(0, -1);
  if (!line || line.includes("\n")) {
    throw new Error("studio-model-worker-receipt-line-count-invalid");
  }
  const normalizedLine = line.endsWith("\r") ? line.slice(0, -1) : line;
  if (!normalizedLine || normalizedLine.includes("\r")) {
    throw new Error("studio-model-worker-receipt-line-count-invalid");
  }
  try {
    return ModelAnalysisReceiptV1Schema.parse(JSON.parse(normalizedLine));
  } catch {
    throw new Error("studio-model-worker-invalid-receipt");
  }
}

function canonicalJobDigest(job: ModelAnalysisJobV1): string {
  return createHash("sha256").update(canonicalJson(job)).digest("hex");
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function trackActiveChild(
  child: ChildProcessWithoutNullStreams,
  detached: boolean,
): OwnedWorkerProcess {
  const ownedProcess: OwnedWorkerProcess = {
    child,
    processGroupId: detached && Number.isSafeInteger(child.pid) && child.pid! > 0
      ? child.pid!
      : null,
    released: false,
  };
  activeWorkerChildren.add(ownedProcess);
  return ownedProcess;
}

function signalOwnedWorkerProcess(ownedProcess: OwnedWorkerProcess): void {
  if (ownedProcess.released) return;
  if (ownedProcess.processGroupId !== null) {
    try {
      process.kill(-ownedProcess.processGroupId, "SIGKILL");
      return;
    } catch {
      // A missing group is confirmed below; otherwise retain the direct-child fallback.
    }
  }
  try { ownedProcess.child.kill("SIGKILL"); } catch { /* already gone */ }
}

function ownedProcessGroupExists(ownedProcess: OwnedWorkerProcess): boolean {
  if (ownedProcess.processGroupId === null) return false;
  try {
    process.kill(-ownedProcess.processGroupId, 0);
    return true;
  } catch (error) {
    return !(
      error instanceof Error
      && "code" in error
      && (error as NodeJS.ErrnoException).code === "ESRCH"
    );
  }
}

function releaseOwnedWorkerProcess(ownedProcess: OwnedWorkerProcess): Promise<boolean> {
  if (ownedProcess.released) return Promise.resolve(true);
  if (ownedProcess.releasePromise !== undefined) return ownedProcess.releasePromise;
  ownedProcess.releasePromise = (async () => {
    const deadline = Date.now() + 500;
    do {
      signalOwnedWorkerProcess(ownedProcess);
      if (!ownedProcessGroupExists(ownedProcess)) {
        ownedProcess.released = true;
        activeWorkerChildren.delete(ownedProcess);
        return true;
      }
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 10));
    } while (!ownedProcess.released && Date.now() < deadline);
    ownedProcess.released = true;
    activeWorkerChildren.delete(ownedProcess);
    return !ownedProcessGroupExists(ownedProcess);
  })();
  return ownedProcess.releasePromise;
}

function releaseOwnedWorkerProcessSync(ownedProcess: OwnedWorkerProcess): void {
  if (ownedProcess.released) return;
  const deadline = Date.now() + 250;
  do {
    signalOwnedWorkerProcess(ownedProcess);
    if (!ownedProcessGroupExists(ownedProcess)) break;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5);
  } while (Date.now() < deadline);
  ownedProcess.released = true;
  activeWorkerChildren.delete(ownedProcess);
}

function killAllActiveProcessGroups(): void {
  for (const ownedProcess of [...activeWorkerChildren]) {
    releaseOwnedWorkerProcessSync(ownedProcess);
  }
}

function installParentLifecycleCleanup(): void {
  process.once("exit", killAllActiveProcessGroups);
  process.once("uncaughtExceptionMonitor", killAllActiveProcessGroups);
  if (process.platform === "win32") return;
  for (const [signal, exitCode] of [
    ["SIGTERM", 143], ["SIGINT", 130], ["SIGHUP", 129],
  ] as const) {
    process.on(signal, () => {
      killAllActiveProcessGroups();
      process.exit(exitCode);
    });
  }
}

function abortError(signal: AbortSignal): Error {
  const reason = signal.reason;
  return reason instanceof Error && reason.name === "TimeoutError"
    ? new StudioModelWorkerTimeoutError(null)
    : new Error("studio-model-worker-aborted");
}

function isPositiveInteger(value: number): boolean {
  return Number.isInteger(value) && value > 0;
}

function isValidCommandIdentity(
  value: NonNullable<LocalProcessStudioModelWorkerConfig["commandIdentity"]>,
): boolean {
  return /^\d+$/.test(value.device)
    && /^\d+$/.test(value.inode)
    && Number.isSafeInteger(value.byteLength)
    && value.byteLength > 0
    && /^[a-f0-9]{64}$/.test(value.sha256);
}

function isValidEnvironment(value: Readonly<Record<string, string>>): boolean {
  const keys = Object.keys(value ?? {}).sort();
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && keys.join("\0") === [...FIXED_ENVIRONMENT_KEYS].sort().join("\0")
    && Object.entries(value).every(([key, entry]) => (
      /^[A-Za-z_][A-Za-z0-9_]*$/.test(key)
      && typeof entry === "string"
      && !entry.includes("\0")
    ))
    && value.PATH === FIXED_SYSTEM_PATH
    && value.LANG === "en_US.UTF-8"
    && value.LC_ALL === "en_US.UTF-8"
    && value.HF_HUB_OFFLINE === "1"
    && value.TRANSFORMERS_OFFLINE === "1"
    && isPrivateDirectory(value.HOME)
    && isPrivateDirectory(value.TMPDIR);
}

function fixedOfflineEnvironment(allowedMediaRoot: string): Readonly<Record<string, string>> {
  if (!isAbsolute(allowedMediaRoot)) throw new Error("studio-model-worker-config-invalid");
  const requestedRoot = lstatSync(allowedMediaRoot, { bigint: true });
  const root = realpathSync(allowedMediaRoot);
  const rootDetails = lstatSync(root, { bigint: true });
  const uid = currentUserId();
  if (
    requestedRoot.isSymbolicLink()
    || !rootDetails.isDirectory()
    || rootDetails.uid !== uid
    || (rootDetails.mode & 0o022n) !== 0n
  ) throw new Error("studio-model-worker-config-invalid");
  const privateRoot = join(root, ".studio-model-worker-private");
  const home = join(privateRoot, "home");
  const temporary = join(privateRoot, "tmp");
  ensurePrivateDirectory(privateRoot);
  ensurePrivateDirectory(home);
  ensurePrivateDirectory(temporary);
  return Object.freeze({
    PATH: FIXED_SYSTEM_PATH,
    HOME: home,
    LANG: "en_US.UTF-8",
    LC_ALL: "en_US.UTF-8",
    TMPDIR: temporary,
    HF_HUB_OFFLINE: "1",
    TRANSFORMERS_OFFLINE: "1",
  });
}

function currentUserId(): bigint {
  const uid = typeof process.geteuid === "function" ? process.geteuid() : process.getuid?.();
  if (!Number.isSafeInteger(uid) || uid === undefined || uid < 0) {
    throw new Error("studio-model-worker-config-invalid");
  }
  return BigInt(uid);
}

function ensurePrivateDirectory(path: string): void {
  try {
    mkdirSync(path, { mode: 0o700 });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
  }
  if (!isPrivateDirectory(path)) throw new Error("studio-model-worker-config-invalid");
}

function isPrivateDirectory(path: string): boolean {
  if (!isAbsolute(path)) return false;
  try {
    const details = lstatSync(path, { bigint: true });
    return !details.isSymbolicLink()
      && details.isDirectory()
      && details.uid === currentUserId()
      && (details.mode & 0o7777n) === 0o700n;
  } catch {
    return false;
  }
}

function isTrustedWorkerDirectory(path: string): boolean {
  if (!isAbsolute(path)) return false;
  try {
    const details = lstatSync(path, { bigint: true });
    return !details.isSymbolicLink()
      && details.isDirectory()
      && details.uid === currentUserId()
      && (details.mode & 0o022n) === 0n;
  } catch {
    return false;
  }
}

type VerifiedCommand = Readonly<{
  path: string;
  argsPrefix: readonly string[];
  revalidate: () => void;
  cleanup: () => void;
}>;

function prepareVerifiedCommand(
  command: string,
  expected: LocalProcessStudioModelWorkerConfig["commandIdentity"],
  temporaryRoot: string,
): VerifiedCommand {
  if (expected === undefined) {
    return Object.freeze({
      path: command,
      argsPrefix: Object.freeze([]),
      revalidate: () => {},
      cleanup: () => {},
    });
  }
  if (
    !isAbsolute(command)
    || !isTrustedWorkerDirectory(dirname(command))
    || !isPrivateDirectory(temporaryRoot)
  ) {
    throw new Error("studio-model-worker-launcher-integrity-failed");
  }
  const executionRoot = mkdtempSync(join(temporaryRoot, "launcher-"));
  chmodSync(executionRoot, 0o700);
  const executionRootIdentity = lstatSync(executionRoot, { bigint: true });
  if (
    !executionRootIdentity.isDirectory()
    || executionRootIdentity.uid !== currentUserId()
    || (executionRootIdentity.mode & 0o7777n) !== 0o700n
  ) {
    throw new Error("studio-model-worker-launcher-integrity-failed");
  }
  const copiedPath = join(executionRoot, `launcher-${randomUUID()}`);
  let sourceDescriptor: number | undefined;
  let destinationDescriptor: number | undefined;
  let copiedIdentity: BigIntStats | undefined;
  let failure: unknown;
  let cleaned = false;
  try {
    const sourceBefore = lstatSync(command, { bigint: true });
    assertExpectedCommandIdentity(sourceBefore, expected, 1n);
    sourceDescriptor = openSync(command, constants.O_RDONLY | constants.O_NOFOLLOW);
    const sourceOpened = fstatSync(sourceDescriptor, { bigint: true });
    if (!sameStableFile(sourceBefore, sourceOpened)) {
      throw new Error("studio-model-worker-launcher-integrity-failed");
    }
    assertExpectedCommandIdentity(sourceOpened, expected, 1n);
    destinationDescriptor = openSync(
      copiedPath,
      constants.O_RDWR | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      0o600,
    );
    copiedIdentity = fstatSync(destinationDescriptor, { bigint: true });
    const digest = createHash("sha256");
    const buffer = Buffer.allocUnsafe(Math.min(64 * 1024, expected.byteLength));
    let position = 0;
    while (position < expected.byteLength) {
      const bytesRead = readSync(
        sourceDescriptor,
        buffer,
        0,
        Math.min(buffer.byteLength, expected.byteLength - position),
        position,
      );
      if (bytesRead === 0) throw new Error("studio-model-worker-launcher-integrity-failed");
      digest.update(buffer.subarray(0, bytesRead));
      let written = 0;
      while (written < bytesRead) {
        const bytesWritten = writeSync(
          destinationDescriptor,
          buffer,
          written,
          bytesRead - written,
          position + written,
        );
        if (bytesWritten === 0) throw new Error("studio-model-worker-launcher-integrity-failed");
        written += bytesWritten;
      }
      position += bytesRead;
    }
    if (digest.digest("hex") !== expected.sha256) {
      throw new Error("studio-model-worker-launcher-integrity-failed");
    }
    fchmodSync(destinationDescriptor, 0o755);
    fsyncSync(destinationDescriptor);
    const sourceAfterDescriptor = fstatSync(sourceDescriptor, { bigint: true });
    const sourceAfterPath = lstatSync(command, { bigint: true });
    if (
      !sameStableFile(sourceOpened, sourceAfterDescriptor)
      || !sameStableFile(sourceOpened, sourceAfterPath)
    ) throw new Error("studio-model-worker-launcher-integrity-failed");
    assertExpectedCommandIdentity(sourceAfterDescriptor, expected, 1n);
    copiedIdentity = fstatSync(destinationDescriptor, { bigint: true });
    validateCopiedCommandDescriptor(destinationDescriptor, copiedIdentity, expected);
    const copiedPathIdentity = lstatSync(copiedPath, { bigint: true });
    if (!sameStableFile(copiedIdentity, copiedPathIdentity)) {
      throw new Error("studio-model-worker-launcher-integrity-failed");
    }
  } catch (error) {
    failure = error;
  } finally {
    if (sourceDescriptor !== undefined) closeSync(sourceDescriptor);
    if (destinationDescriptor !== undefined) closeSync(destinationDescriptor);
  }
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    safeCleanupOwnedFile(executionRoot, executionRootIdentity, copiedPath, copiedIdentity);
  };
  if (failure !== undefined || copiedIdentity === undefined) {
    cleanup();
    throw failure ?? new Error("studio-model-worker-launcher-integrity-failed");
  }
  return Object.freeze({
    path: "/bin/sh",
    argsPrefix: Object.freeze([
      "-c",
      FORMAL_LAUNCHER_SOURCE_WRAPPER,
      command,
      copiedPath,
    ]),
    revalidate: () => {
      validateCommandFile(command, expected, 1n);
      validateCopiedCommandFile(copiedPath, copiedIdentity!, expected);
    },
    cleanup,
  });
}

function assertExpectedCommandIdentity(
  details: BigIntStats,
  expected: NonNullable<LocalProcessStudioModelWorkerConfig["commandIdentity"]>,
  expectedLinks: bigint,
): void {
  if (
    details.isSymbolicLink()
    || !details.isFile()
    || details.uid !== currentUserId()
    || details.nlink !== expectedLinks
    || (details.mode & 0o7777n) !== 0o755n
    || details.dev !== BigInt(expected.device)
    || details.ino !== BigInt(expected.inode)
    || details.size !== BigInt(expected.byteLength)
  ) throw new Error("studio-model-worker-launcher-integrity-failed");
}

function validateCommandFile(
  path: string,
  expected: NonNullable<LocalProcessStudioModelWorkerConfig["commandIdentity"]>,
  expectedLinks: bigint,
): BigIntStats {
  const before = lstatSync(path, { bigint: true });
  assertExpectedCommandIdentity(before, expected, expectedLinks);
  const descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const opened = fstatSync(descriptor, { bigint: true });
    if (!sameStableFile(before, opened) || hashDescriptor(descriptor, expected.byteLength) !== expected.sha256) {
      throw new Error("studio-model-worker-launcher-integrity-failed");
    }
    const after = lstatSync(path, { bigint: true });
    if (!sameStableFile(opened, after)) {
      throw new Error("studio-model-worker-launcher-integrity-failed");
    }
    return after;
  } finally {
    closeSync(descriptor);
  }
}

function validateCopiedCommandFile(
  path: string,
  identity: BigIntStats,
  expected: NonNullable<LocalProcessStudioModelWorkerConfig["commandIdentity"]>,
): void {
  const before = lstatSync(path, { bigint: true });
  if (!sameStableFile(identity, before)) {
    throw new Error("studio-model-worker-launcher-integrity-failed");
  }
  const descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const opened = fstatSync(descriptor, { bigint: true });
    if (!sameStableFile(before, opened)) {
      throw new Error("studio-model-worker-launcher-integrity-failed");
    }
    validateCopiedCommandDescriptor(descriptor, opened, expected);
    const after = lstatSync(path, { bigint: true });
    if (!sameStableFile(opened, after)) {
      throw new Error("studio-model-worker-launcher-integrity-failed");
    }
  } finally {
    closeSync(descriptor);
  }
}

function validateCopiedCommandDescriptor(
  descriptor: number,
  details: BigIntStats,
  expected: NonNullable<LocalProcessStudioModelWorkerConfig["commandIdentity"]>,
): void {
  if (
    !details.isFile()
    || details.uid !== currentUserId()
    || details.nlink !== 1n
    || (details.mode & 0o7777n) !== 0o755n
    || details.size !== BigInt(expected.byteLength)
    || hashDescriptor(descriptor, expected.byteLength) !== expected.sha256
  ) throw new Error("studio-model-worker-launcher-integrity-failed");
}

function hashDescriptor(descriptor: number, byteLength: number): string {
  const digest = createHash("sha256");
  const buffer = Buffer.allocUnsafe(Math.min(64 * 1024, byteLength));
  let position = 0;
  while (position < byteLength) {
    const bytesRead = readSync(
      descriptor,
      buffer,
      0,
      Math.min(buffer.byteLength, byteLength - position),
      position,
    );
    if (bytesRead === 0) throw new Error("studio-model-worker-launcher-integrity-failed");
    digest.update(buffer.subarray(0, bytesRead));
    position += bytesRead;
  }
  return digest.digest("hex");
}

function safeCleanupOwnedFile(
  rootPath: string,
  rootIdentity: BigIntStats,
  filePath: string,
  fileIdentity: BigIntStats | undefined,
): void {
  try {
    const currentRoot = lstatSync(rootPath, { bigint: true });
    if (!sameIdentity(rootIdentity, currentRoot)) return;
    if (fileIdentity !== undefined) {
      let currentFile: BigIntStats | undefined;
      try {
        currentFile = lstatSync(filePath, { bigint: true });
      } catch (error) {
        if (!(
          error instanceof Error
          && "code" in error
          && (error as NodeJS.ErrnoException).code === "ENOENT"
        )) return;
      }
      if (currentFile !== undefined) {
        if (!sameIdentity(fileIdentity, currentFile)) return;
        unlinkSync(filePath);
      }
    }
    rmdirSync(rootPath);
  } catch {
    // Only the exact file and empty directory created by this run may be removed.
  }
}

type StagedSource = Readonly<{
  path: string;
  cleanup: () => void;
}>;

function stageSource(sourcePath: string, temporaryRoot: string): StagedSource {
  if (!isPrivateDirectory(temporaryRoot)) {
    throw new Error("studio-model-source-integrity-failed");
  }
  const stagingRoot = mkdtempSync(join(temporaryRoot, "source-"));
  chmodSync(stagingRoot, 0o700);
  const stagingRootIdentity = lstatSync(stagingRoot, { bigint: true });
  const stagedPath = join(stagingRoot, "media");
  let sourceDescriptor: number | undefined;
  let destinationDescriptor: number | undefined;
  let ownedStagedIdentity: BigIntStats | undefined;
  let failure: unknown;
  try {
    const sourceBefore = lstatSync(sourcePath, { bigint: true });
    if (sourceBefore.isSymbolicLink() || !sourceBefore.isFile() || sourceBefore.nlink !== 1n) {
      throw new Error("studio-model-source-integrity-failed");
    }
    sourceDescriptor = openSync(sourcePath, constants.O_RDONLY | constants.O_NOFOLLOW);
    const sourceOpened = fstatSync(sourceDescriptor, { bigint: true });
    if (!sameStableFile(sourceBefore, sourceOpened)) {
      throw new Error("studio-model-source-integrity-failed");
    }
    destinationDescriptor = openSync(
      stagedPath,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      0o600,
    );
    ownedStagedIdentity = fstatSync(destinationDescriptor, { bigint: true });
    const createdPathIdentity = lstatSync(stagedPath, { bigint: true });
    if (
      !ownedStagedIdentity.isFile()
      || ownedStagedIdentity.uid !== currentUserId()
      || ownedStagedIdentity.nlink !== 1n
      || (ownedStagedIdentity.mode & 0o7777n) !== 0o600n
      || ownedStagedIdentity.size !== 0n
      || !sameStableFile(ownedStagedIdentity, createdPathIdentity)
    ) throw new Error("studio-model-source-integrity-failed");
    const buffer = Buffer.allocUnsafe(64 * 1024);
    let position = 0;
    while (position < Number(sourceOpened.size)) {
      const bytesRead = readSync(
        sourceDescriptor,
        buffer,
        0,
        Math.min(buffer.byteLength, Number(sourceOpened.size) - position),
        position,
      );
      if (bytesRead === 0) throw new Error("studio-model-source-integrity-failed");
      let written = 0;
      while (written < bytesRead) {
        const bytesWritten = writeSync(destinationDescriptor, buffer, written, bytesRead - written);
        if (bytesWritten === 0) throw new Error("studio-model-source-integrity-failed");
        written += bytesWritten;
      }
      position += bytesRead;
    }
    fchmodSync(destinationDescriptor, 0o400);
    fsyncSync(destinationDescriptor);
    const sourceAfterDescriptor = fstatSync(sourceDescriptor, { bigint: true });
    const sourceAfterPath = lstatSync(sourcePath, { bigint: true });
    if (
      !sameStableFile(sourceOpened, sourceAfterDescriptor)
      || !sameStableFile(sourceOpened, sourceAfterPath)
    ) throw new Error("studio-model-source-integrity-failed");
    const stagedDescriptorIdentity = fstatSync(destinationDescriptor, { bigint: true });
    const stagedPathIdentity = lstatSync(stagedPath, { bigint: true });
    if (
      !sameIdentity(ownedStagedIdentity, stagedDescriptorIdentity)
      || !sameStableFile(stagedDescriptorIdentity, stagedPathIdentity)
      || stagedDescriptorIdentity.isSymbolicLink()
      || !stagedDescriptorIdentity.isFile()
      || stagedDescriptorIdentity.nlink !== 1n
      || stagedDescriptorIdentity.uid !== currentUserId()
      || (stagedDescriptorIdentity.mode & 0o7777n) !== 0o400n
      || stagedDescriptorIdentity.size !== sourceOpened.size
    ) throw new Error("studio-model-source-integrity-failed");
  } catch (error) {
    failure = error;
  } finally {
    try {
      if (sourceDescriptor !== undefined) closeSync(sourceDescriptor);
    } catch (error) {
      failure ??= error;
    }
    try {
      if (destinationDescriptor !== undefined) closeSync(destinationDescriptor);
    } catch (error) {
      failure ??= error;
    }
  }
  if (failure !== undefined || ownedStagedIdentity === undefined) {
    safeCleanupStagedSource(
      stagingRoot,
      stagingRootIdentity,
      stagedPath,
      ownedStagedIdentity,
    );
    throw failure ?? new Error("studio-model-source-integrity-failed");
  }
  return Object.freeze({
    path: stagedPath,
    cleanup: () => safeCleanupStagedSource(
      stagingRoot,
      stagingRootIdentity,
      stagedPath,
      ownedStagedIdentity,
    ),
  });
}

function safeCleanupStagedSource(
  stagingRoot: string,
  rootIdentity: BigIntStats,
  stagedPath: string,
  stagedIdentity: BigIntStats | undefined,
): void {
  safeCleanupOwnedFile(stagingRoot, rootIdentity, stagedPath, stagedIdentity);
}

function sameIdentity(
  left: BigIntStats,
  right: BigIntStats,
): boolean {
  return left.dev === right.dev && left.ino === right.ino;
}

function sameStableFile(
  left: BigIntStats,
  right: BigIntStats,
): boolean {
  return sameIdentity(left, right)
    && left.mode === right.mode
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs
    && left.ctimeNs === right.ctimeNs;
}
