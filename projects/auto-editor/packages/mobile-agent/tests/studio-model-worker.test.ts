import { spawn, spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import fs, { chmodSync, lstatSync, readdirSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { access, chmod, lstat, mkdir, mkdtemp, readFile, readdir, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, relative } from "node:path";
import { pathToFileURL } from "node:url";

import {
  ModelAnalysisJobV1Schema,
  ModelAnalysisReceiptV1Schema,
  type ModelAnalysisJobV1,
} from "@wearable/contracts";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  LocalProcessStudioModelWorker,
  StudioModelWorkerFailureError,
  StudioModelWorkerTimeoutError,
} from "../src/studio-model-worker";

let mediaRoot = "";
let sourcePath = "";
let outsideRoot = "";

const validJob = (overrides: Record<string, unknown> = {}) =>
  ModelAnalysisJobV1Schema.parse({
    schemaVersion: 1,
    jobId: "job-1",
    customerId: "customer-a",
    sessionId: "session-1",
    sourceAssetId: "asset-a",
    sourceDigest: "a".repeat(64),
    sourcePath,
    sourceStartMs: 1_000,
    sourceEndMs: 4_000,
    layer: "vision",
    operationKey: "analyze-asset-a-vision-v1",
    requestedAt: "2026-09-12T02:00:00.000Z",
    ...overrides,
  });

const canonicalJobDigest = (job: ModelAnalysisJobV1) => createHash("sha256")
  .update(JSON.stringify(Object.fromEntries(
    Object.entries(job).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0),
  )))
  .digest("hex");

const validReceipt = (overrides: Record<string, unknown> = {}) =>
  ModelAnalysisReceiptV1Schema.parse({
    schemaVersion: 1,
    jobId: "job-1",
    customerId: "customer-a",
    sessionId: "session-1",
    sourceAssetId: "asset-a",
    sourceDigest: "a".repeat(64),
    sourcePath,
    sourceStartMs: 1_000,
    sourceEndMs: 4_000,
    layer: "vision",
    operationKey: "analyze-asset-a-vision-v1",
    adapterName: "fixture-vision-adapter",
    modelName: "fixture-qwen",
    modelVersion: "2b-fixture-v1",
    checkpointDigest: "c".repeat(64),
    inputDigest: "d".repeat(64),
    promptVersion: "studio-vision-zh-v1",
    observations: ["possibly a jewelry close-up"],
    suggestedTags: ["single-product"],
    qualitySignals: { frameCount: 3, usable: true },
    confidence: 0.72,
    warnings: ["model-observation-requires-human-review"],
    startedAt: "2026-09-12T02:00:01.000Z",
    completedAt: "2026-09-12T02:00:01.120Z",
    runtimeMs: 120,
    status: "succeeded",
    failure: null,
    synthetic: true,
    providerMode: "local",
    ...overrides,
  });

type FakeOutput = Readonly<{
  stdout?: string;
  stderr?: string;
  exitCode?: number;
  script?: string;
  timeoutMs?: number;
  maxStdoutBytes?: number;
  maxStderrBytes?: number;
  command?: string;
  cwd?: string;
  allowedMediaRoot?: string;
  args?: string[];
  env?: Record<string, string>;
  bindSourcePath?: boolean;
  stageTelemetry?: null | Readonly<{
    protocol: "studio-worker-stage-telemetry-v1";
    maxBytes: 4096;
  }>;
}>;

const bindReceiptToExistingStagedJobLines = [
  "receipt.sourcePath = job.sourcePath;",
  "const canonicalJob = JSON.stringify(Object.fromEntries(Object.entries(job).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)));",
  "receipt.inputDigest = require('node:crypto').createHash('sha256').update(canonicalJob).digest('hex');",
];

const bindReceiptToStagedJobLines = [
  "const job = JSON.parse(input.trim());",
  ...bindReceiptToExistingStagedJobLines,
];

const fakeWorker = (output: FakeOutput) => {
  const script = output.script ?? [
    "let input = '';",
    "process.stdin.on('data', (chunk) => { input += chunk; });",
    "process.stdin.on('end', () => {",
    `let output = ${JSON.stringify(output.stdout ?? "")};`,
    ...(output.bindSourcePath === false ? [] : [
      "try {",
      "const receipt = JSON.parse(output.trim());",
      ...bindReceiptToStagedJobLines,
      "output = JSON.stringify(receipt) + '\\n';",
      "} catch {}",
    ]),
    "process.stdout.write(output);",
    `process.stderr.write(${JSON.stringify(output.stderr ?? "")});`,
    `process.exitCode = ${output.exitCode ?? 0};`,
    "});",
  ].join("\n");
  return new LocalProcessStudioModelWorker({
    command: output.command ?? process.execPath,
    args: output.args ?? ["-e", script],
    cwd: output.cwd ?? mediaRoot,
    env: output.env ?? {
      PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
      HOME: mediaRoot,
      LANG: "en_US.UTF-8",
      LC_ALL: "en_US.UTF-8",
      TMPDIR: tmpdir(),
      HF_HUB_OFFLINE: "1",
      TRANSFORMERS_OFFLINE: "1",
    },
    timeoutMs: output.timeoutMs ?? 1_000,
    maxStdoutBytes: output.maxStdoutBytes ?? 64 * 1024,
    maxStderrBytes: output.maxStderrBytes ?? 64,
    allowedMediaRoot: output.allowedMediaRoot ?? mediaRoot,
    stageTelemetry: output.stageTelemetry ?? null,
  });
};

const echoOutgoingSourceWorker = (output: Pick<FakeOutput, "cwd" | "allowedMediaRoot"> = {}) => {
  const receipt = validReceipt();
  const script = [
    "let input = '';",
    "process.stdin.setEncoding('utf8');",
    "process.stdin.on('data', (chunk) => { input += chunk; });",
    "process.stdin.on('end', () => {",
    "const job = JSON.parse(input.trim());",
    `const receipt = ${JSON.stringify(receipt)};`,
    ...bindReceiptToExistingStagedJobLines,
    "process.stdout.write(JSON.stringify(receipt) + '\\n');",
    "});",
  ].join("\n");
  return fakeWorker({ ...output, script });
};

const makeFixtureWorker = (
  fixture: "wrong-source" | "second-json-line" | "invalid-json" | "stdout-overflow"
    | "stderr-overflow" | "nonzero" | "hang",
) => {
  switch (fixture) {
    case "wrong-source":
      return fakeWorker({
        stdout: `${JSON.stringify(validReceipt({ sourcePath: join(mediaRoot, "other.mp4") }))}\n`,
        bindSourcePath: false,
      });
    case "second-json-line": {
      const line = JSON.stringify(validReceipt());
      return fakeWorker({ stdout: `${line}\n${line}\n` });
    }
    case "invalid-json":
      return fakeWorker({ stdout: "not-json\n" });
    case "stdout-overflow":
      return fakeWorker({ stdout: `${"x".repeat(65)}\n`, maxStdoutBytes: 64 });
    case "stderr-overflow":
      return fakeWorker({
        stdout: `${JSON.stringify(validReceipt())}\n`,
        stderr: "敏".repeat(22),
        maxStderrBytes: 64,
      });
    case "nonzero":
      return fakeWorker({
        stdout: '{"partial":"private output"',
        stderr: `raw transcript must stay private: ${sourcePath}`,
        exitCode: 7,
        maxStderrBytes: 4 * 1024,
      });
    case "hang":
      return fakeWorker({
        script: "process.stdout.write('{\"partial\":'); process.stdin.resume(); setInterval(() => {}, 1_000);",
        timeoutMs: 5_000,
      });
  }
};

const processGroupFixtureWorker = (
  markerPath: string,
  readyPath: string,
  timeoutMs = 500,
) => {
  const grandchild = [
    "const { writeFileSync } = require('node:fs');",
    `setTimeout(() => writeFileSync(${JSON.stringify(markerPath)}, 'escaped'), 900);`,
    "setInterval(() => {}, 1_000);",
  ].join("\n");
  const parent = [
    "const { spawn } = require('node:child_process');",
    "const { writeFileSync } = require('node:fs');",
    `spawn(process.execPath, ['-e', ${JSON.stringify(grandchild)}], { stdio: 'ignore' });`,
    `writeFileSync(${JSON.stringify(readyPath)}, 'ready');`,
    "process.stdin.resume();",
    "setInterval(() => {}, 1_000);",
  ].join("\n");
  return fakeWorker({ script: parent, timeoutMs });
};

const terminalOrphanFixtureWorker = (
  pidPath: string,
  outcome: "exit-7" | "exit-0" | "valid-receipt",
  receiptDelayMs = 0,
) => {
  const grandchild = "setInterval(() => {}, 1000);";
  const lines = [
    "const { spawn } = require('node:child_process');",
    "const { writeFileSync } = require('node:fs');",
    `const grandchild = spawn(process.execPath, ['-e', ${JSON.stringify(grandchild)}], { stdio: 'ignore' });`,
    `writeFileSync(${JSON.stringify(pidPath)}, JSON.stringify({ worker: process.pid, grandchild: grandchild.pid }));`,
  ];
  if (outcome === "exit-7") lines.push("process.exit(7);");
  else if (outcome === "exit-0") lines.push("process.exit(0);");
  else lines.push(
    "let input = '';",
    "process.stdin.on('data', (chunk) => { input += chunk; });",
    "process.stdin.on('end', () => setTimeout(() => {",
    `const receipt = ${JSON.stringify(validReceipt())};`,
    ...bindReceiptToStagedJobLines,
    "process.stdout.write(JSON.stringify(receipt) + '\\n', () => process.exit(0));",
    `}, ${receiptDelayMs}));`,
  );
  return fakeWorker({ script: lines.join("\n"), timeoutMs: 2_000 });
};

const realTranscriptionProcessGroupFixture = async (
  fixtureRoot: string,
  outcome: "invalid-receipt" | "hang",
  timeoutMs: number,
  options: Readonly<{
    additionalHarnessArgs?: readonly string[];
    childStartDelayMs?: number;
    keepHarnessAliveAfterChild?: boolean;
    legacyReportedCommandShape?: string;
  }> = {},
) => {
  const pidPath = join(fixtureRoot, "transcription-child.json");
  const harnessPidPath = join(fixtureRoot, "transcription-harness.json");
  const childPath = join(fixtureRoot, "transcription-child.py");
  const harnessPath = join(fixtureRoot, "transcription-harness.py");
  const harnessNonce = randomBytes(32).toString("hex");
  const workerRoot = join(process.cwd(), "tools/media-model-worker");
  await writeFile(childPath, [
    "import json, os, pathlib, time",
    `pathlib.Path(${JSON.stringify(pidPath)}).write_text(json.dumps({`,
    "    'pid': os.getpid(),",
    "    'pgid': os.getpgid(0),",
    "    'parentPgid': os.getpgid(os.getppid()),",
    "}), encoding='utf-8')",
    "while True: time.sleep(1)",
    "",
  ].join("\n"));
  await writeFile(harnessPath, [
    "import json, os, pathlib, subprocess, sys, threading, time",
    "identity_result = subprocess.run([",
    "    '/bin/ps', '-ww', '-p', str(os.getpid()),",
    "    '-o', 'pid=,ppid=,pgid=,uid=,lstart=',",
    "], check=True, capture_output=True, text=True)",
    "identity_parts = identity_result.stdout.strip().split(maxsplit=4)",
    "if len(identity_parts) != 5: raise RuntimeError('fixture-identity-invalid')",
    "identity = {",
    `    'nonce': ${JSON.stringify(harnessNonce)},`,
    "    'pid': int(identity_parts[0]),",
    "    'ppid': int(identity_parts[1]),",
    "    'pgid': int(identity_parts[2]),",
    "    'uid': int(identity_parts[3]),",
    "    'startedAt': ' '.join(identity_parts[4].split()),",
    `    'reportedCommandShape': ${options.legacyReportedCommandShape === undefined ? "sys.executable" : JSON.stringify(options.legacyReportedCommandShape)},`,
    "}",
    "if identity['pid'] != os.getpid() or identity['ppid'] != os.getppid() or identity['pgid'] != os.getpgid(0) or identity['uid'] != os.getuid():",
    "    raise RuntimeError('fixture-identity-mismatch')",
    `identity_path = pathlib.Path(${JSON.stringify(harnessPidPath)})`,
    `identity_tmp_path = pathlib.Path(${JSON.stringify(harnessPidPath)} + '.tmp-' + ${JSON.stringify(harnessNonce)})`,
    "identity_fd = os.open(identity_tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
    "os.fchmod(identity_fd, 0o600)",
    "with os.fdopen(identity_fd, 'w', encoding='utf-8') as identity_file:",
    "    json.dump(identity, identity_file)",
    "    identity_file.flush()",
    "    os.fsync(identity_file.fileno())",
    "os.rename(identity_tmp_path, identity_path)",
    `sys.path.insert(0, ${JSON.stringify(workerRoot)})`,
    "import transcription",
    "sys.stdin.readline()",
    "def run_transcription_child():",
    "    try:",
    ...(options.childStartDelayMs === undefined ? [] : [
      `        time.sleep(${options.childStartDelayMs / 1_000})`,
    ]),
    `        transcription._run_bounded_command([sys.executable, ${JSON.stringify(childPath)}], 'whisper')`,
    "    except BaseException:",
    "        pass",
    "thread = threading.Thread(target=run_transcription_child, daemon=True)",
    "thread.start()",
    `while not pathlib.Path(${JSON.stringify(pidPath)}).exists(): time.sleep(0.01)`,
    ...(outcome === "invalid-receipt" ? [
      "sys.stdout.write('{}\\n')",
      "sys.stdout.flush()",
      "os._exit(0)",
    ] : [
      "thread.join()",
      ...(options.keepHarnessAliveAfterChild ? ["while True: time.sleep(1)"] : []),
    ]),
    "",
  ].join("\n"));
  return {
    pidPath,
    harnessNonce,
    harnessPidPath,
    harnessPath,
    worker: fakeWorker({
      command: "/usr/bin/python3",
      args: [harnessPath, ...(options.additionalHarnessArgs ?? [])],
      timeoutMs,
      maxStderrBytes: 4 * 1024,
    }),
  };
};

type FixtureProcessIdentity = Readonly<{
  pid: number;
  ppid: number;
  pgid: number;
  uid: number;
  startedAt: string;
}>;

type TranscriptionHarnessIdentity = FixtureProcessIdentity & Readonly<{
  nonce: string;
  reportedCommandShape: string;
}>;

const assertTranscriptionHarnessIdentity = (
  identity: TranscriptionHarnessIdentity,
  expectedNonce: string,
  expectedParentPid: number,
) => {
  const exactKeys = [
    "nonce", "pgid", "pid", "ppid", "reportedCommandShape", "startedAt", "uid",
  ];
  const expectedUid = process.getuid?.();
  if (
    identity === null
    || typeof identity !== "object"
    || Object.keys(identity).sort().join("\0") !== exactKeys.join("\0")
    || identity.nonce !== expectedNonce
    || !/^[a-f0-9]{64}$/.test(identity.nonce)
    || !Number.isSafeInteger(identity.pid)
    || !Number.isSafeInteger(identity.ppid)
    || !Number.isSafeInteger(identity.pgid)
    || !Number.isSafeInteger(identity.uid)
    || identity.pid <= 0
    || identity.pid !== identity.pgid
    || identity.ppid !== expectedParentPid
    || identity.uid !== expectedUid
    || typeof identity.startedAt !== "string"
    || identity.startedAt.trim().length === 0
    || typeof identity.reportedCommandShape !== "string"
  ) throw new Error("fixture-transcription-harness-ownership-mismatch");
};

const readTranscriptionHarnessIdentity = async (
  path: string,
  expectedNonce: string,
  expectedParentPid: number,
) => {
  const details = await lstat(path);
  if (
    details.isSymbolicLink()
    || !details.isFile()
    || details.uid !== process.getuid?.()
    || (details.mode & 0o7777) !== 0o600
    || details.nlink !== 1
  ) throw new Error("fixture-transcription-harness-identity-file-invalid");
  const identity = JSON.parse(await readFile(path, "utf8")) as TranscriptionHarnessIdentity;
  assertTranscriptionHarnessIdentity(identity, expectedNonce, expectedParentPid);
  return identity;
};

const readTranscriptionHarnessIdentityIfPresent = async (
  path: string,
  expectedNonce: string,
  expectedParentPid: number,
) => {
  try {
    return await readTranscriptionHarnessIdentity(path, expectedNonce, expectedParentPid);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
};

const inspectProcessIdentity = (pid: number): FixtureProcessIdentity | null => {
  const result = spawnSync("/bin/ps", [
    "-ww",
    "-p",
    String(pid),
    "-o",
    "pid=,ppid=,pgid=,uid=,lstart=",
  ], { encoding: "utf8" });
  const output = result.stdout.trim();
  if (result.status === 1 && output.length === 0) return null;
  if (result.status !== 0) throw new Error("fixture-process-inspection-failed");
  const match = output.match(/^(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.+?)\s*$/);
  if (match === null) throw new Error("fixture-process-inspection-invalid");
  return {
    pid: Number(match[1]),
    ppid: Number(match[2]),
    pgid: Number(match[3]),
    uid: Number(match[4]),
    startedAt: match[5]!.trim().replace(/\s+/g, " "),
  };
};

const sameProcessIdentity = (
  actual: FixtureProcessIdentity,
  expected: FixtureProcessIdentity,
) => actual.pid === expected.pid
  && actual.ppid === expected.ppid
  && actual.pgid === expected.pgid
  && actual.uid === expected.uid
  && actual.startedAt === expected.startedAt;

const processGroupExists = (pgid: number) => {
  try {
    process.kill(-pgid, 0);
    return true;
  } catch {
    return false;
  }
};

const waitForProcessGroupToExit = async (pgid: number, timeoutMs: number) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!processGroupExists(pgid)) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error(`fixture-process-group-still-alive:${pgid}`);
};

const terminateVerifiedTranscriptionHarnessGroup = async (
  identity: TranscriptionHarnessIdentity,
  expectedNonce: string,
  expectedParentPid: number,
) => {
  assertTranscriptionHarnessIdentity(identity, expectedNonce, expectedParentPid);
  const snapshot = inspectProcessIdentity(identity.pid);
  if (snapshot === null) {
    if (processGroupExists(identity.pgid)) {
      throw new Error("fixture-transcription-harness-ownership-mismatch");
    }
    return;
  }
  if (
    snapshot.pid !== identity.pid
    || snapshot.pgid !== identity.pgid
    || snapshot.uid !== identity.uid
    || snapshot.startedAt !== identity.startedAt
  ) throw new Error("fixture-transcription-harness-ownership-mismatch");
  process.kill(-identity.pgid, "SIGKILL");
  await waitForProcessGroupToExit(identity.pgid, 2_000);
};

const emergencyTerminateTranscriptionHarnessGroup = async (
  identity: TranscriptionHarnessIdentity,
  expectedNonce: string,
  expectedParentPid: number,
) => {
  assertTranscriptionHarnessIdentity(identity, expectedNonce, expectedParentPid);
  const snapshot = inspectProcessIdentity(identity.pid);
  if (snapshot === null) return;
  const owned = snapshot.pid === identity.pid
    && snapshot.pgid === identity.pgid
    && snapshot.uid === identity.uid
    && snapshot.startedAt === identity.startedAt;
  if (!owned) throw new Error("fixture-emergency-harness-ownership-mismatch");
  process.kill(-identity.pgid, "SIGKILL");
  await waitForProcessGroupToExit(identity.pgid, 2_000);
};

const waitForFile = async (path: string, timeoutMs: number) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await access(path).then(() => true, () => false)) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("fixture-ready-timeout");
};

const processExists = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};

const waitForProcessesToExit = async (pids: readonly number[], timeoutMs: number) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (pids.every((pid) => !processExists(pid))) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error(`fixture-processes-still-alive:${pids.filter(processExists).join(",")}`);
};

const launcherIdentity = async (path: string) => {
  const details = await lstat(path, { bigint: true });
  return {
    device: details.dev.toString(),
    inode: details.ino.toString(),
    byteLength: Number(details.size),
    sha256: createHash("sha256").update(await import("node:fs/promises").then(({ readFile }) => readFile(path))).digest("hex"),
  };
};

const fixedTestEnvironment = (temporaryRoot: string) => ({
  PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
  HOME: mediaRoot,
  LANG: "en_US.UTF-8",
  LC_ALL: "en_US.UTF-8",
  TMPDIR: temporaryRoot,
  HF_HUB_OFFLINE: "1",
  TRANSFORMERS_OFFLINE: "1",
});

const spawnTranscriptionParentHarness = async (
  fixtureRoot: string,
  harnessPath: string,
) => {
  const parentPath = join(fixtureRoot, "parent.ts");
  const source = join(fixtureRoot, "source.mp4");
  const workerModule = pathToFileURL(join(
    process.cwd(),
    "packages/mobile-agent/src/studio-model-worker.ts",
  )).href;
  await writeFile(source, "synthetic transcription parent signal media");
  await writeFile(parentPath, [
    `import { LocalProcessStudioModelWorker } from ${JSON.stringify(workerModule)};`,
    `const root = ${JSON.stringify(fixtureRoot)};`,
    "const worker = new LocalProcessStudioModelWorker({",
    "  command: '/usr/bin/python3',",
    `  args: [${JSON.stringify(harnessPath)}],`,
    "  cwd: root, timeoutMs: 60_000, maxStdoutBytes: 1024, maxStderrBytes: 4096,",
    "  allowedMediaRoot: root,",
    "});",
    "void worker.run({",
    "  schemaVersion: 1, jobId: 'transcription-parent-signal-job', customerId: 'customer-a',",
    "  sessionId: 'session-a', sourceAssetId: 'asset-a', sourceDigest: 'a'.repeat(64),",
    `  sourcePath: ${JSON.stringify(source)}, sourceStartMs: 0, sourceEndMs: 1000,`,
    "  layer: 'transcript', operationKey: 'transcription-parent-signal-operation', requestedAt: '2026-09-28T00:00:00.000Z',",
    "}, new AbortController().signal).catch(() => {});",
    "setInterval(() => {}, 1000);",
    "",
  ].join("\n"));
  const child = spawn(process.execPath, ["--import", "tsx", parentPath], {
    cwd: process.cwd(),
    detached: true,
    stdio: "ignore",
  });
  if (child.pid === undefined) throw new Error("fixture-parent-spawn-failed");
  const deadline = Date.now() + 2_000;
  let identity: FixtureProcessIdentity | null = null;
  while (Date.now() < deadline) {
    identity = inspectProcessIdentity(child.pid);
    if (identity !== null) break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  if (
    identity === null
    || identity.pid !== child.pid
    || identity.ppid !== process.pid
    || identity.pgid !== child.pid
    || identity.uid !== process.getuid?.()
  ) {
    child.kill("SIGKILL");
    throw new Error("fixture-parent-supervisor-ownership-mismatch");
  }
  return { child, identity };
};

const waitForSpawnExit = async (
  child: ReturnType<typeof spawn>,
  timeoutMs: number,
) => {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise<void>((resolveExit, rejectExit) => {
    const timeout = setTimeout(() => {
      child.off("exit", onExit);
      rejectExit(new Error("fixture-parent-exit-timeout"));
    }, timeoutMs);
    const onExit = () => {
      clearTimeout(timeout);
      resolveExit();
    };
    child.once("exit", onExit);
  });
};

type TranscriptionParentSupervisor = Awaited<ReturnType<typeof spawnTranscriptionParentHarness>>;

const stopRetainedTranscriptionParentSupervisor = async (
  supervisor: TranscriptionParentSupervisor,
) => {
  const { child, identity } = supervisor;
  if (child.exitCode !== null || child.signalCode !== null) return;
  let snapshot = inspectProcessIdentity(identity.pid);
  if (snapshot === null) {
    await waitForSpawnExit(child, 2_000);
    return;
  }
  if (!sameProcessIdentity(snapshot, identity)) {
    throw new Error("fixture-parent-supervisor-ownership-mismatch");
  }
  child.kill("SIGTERM");
  try {
    await waitForSpawnExit(child, 2_000);
    return;
  } catch (error) {
    if ((error as Error).message !== "fixture-parent-exit-timeout") throw error;
  }
  snapshot = inspectProcessIdentity(identity.pid);
  if (snapshot === null) {
    await waitForSpawnExit(child, 2_000);
    return;
  }
  if (!sameProcessIdentity(snapshot, identity)) {
    throw new Error("fixture-parent-supervisor-ownership-mismatch");
  }
  child.kill("SIGKILL");
  await waitForSpawnExit(child, 2_000);
};

const fixtureCleanupError = (errors: readonly unknown[]) => {
  if (errors.length === 1) return errors[0];
  return new AggregateError(errors, "fixture-transcription-cleanup-failed");
};

const cleanupTranscriptionParentFixture = async ({
  supervisor,
  fixtureRoot,
  harness,
  harnessNonce,
  harnessPidPath,
}: Readonly<{
  supervisor: TranscriptionParentSupervisor;
  fixtureRoot: string;
  harness?: TranscriptionHarnessIdentity;
  harnessNonce: string;
  harnessPidPath: string;
}>) => {
  const errors: unknown[] = [];
  let ownedHarness = harness;
  try {
    if (ownedHarness === undefined) {
      ownedHarness = await readTranscriptionHarnessIdentityIfPresent(
        harnessPidPath,
        harnessNonce,
        supervisor.identity.pid,
      );
    } else {
      assertTranscriptionHarnessIdentity(
        ownedHarness,
        harnessNonce,
        supervisor.identity.pid,
      );
    }
  } catch (error) {
    errors.push(error);
    ownedHarness = undefined;
  }
  try {
    await stopRetainedTranscriptionParentSupervisor(supervisor);
  } catch (error) {
    errors.push(error);
  }
  if (ownedHarness === undefined) {
    try {
      ownedHarness = await readTranscriptionHarnessIdentityIfPresent(
        harnessPidPath,
        harnessNonce,
        supervisor.identity.pid,
      );
    } catch (error) {
      errors.push(error);
    }
  }
  if (ownedHarness !== undefined) {
    try {
      await terminateVerifiedTranscriptionHarnessGroup(
        ownedHarness,
        harnessNonce,
        supervisor.identity.pid,
      );
    } catch (error) {
      errors.push(error);
    }
  }
  try {
    await rm(fixtureRoot, { recursive: true, force: true });
  } catch (error) {
    errors.push(error);
  }
  if (errors.length > 0) throw fixtureCleanupError(errors);
};

const combineFixtureScenarioAndCleanupErrors = (
  scenarioError: unknown,
  cleanupError: unknown,
) => scenarioError !== undefined && cleanupError !== undefined
  ? new AggregateError([scenarioError, cleanupError], "fixture-scenario-and-cleanup-failed")
  : scenarioError ?? cleanupError;

const runFixtureScenarioWithCleanup = async (
  scenario: () => Promise<void>,
  cleanup: () => Promise<void>,
) => {
  let scenarioError: unknown;
  let cleanupError: unknown;
  try {
    await scenario();
  } catch (error) {
    scenarioError = error;
  }
  try {
    await cleanup();
  } catch (error) {
    cleanupError = error;
  }
  const error = combineFixtureScenarioAndCleanupErrors(scenarioError, cleanupError);
  if (error !== undefined) throw error;
};

const exerciseTranscriptionFixtureCleanup = async (
  stage: "before-child-pid" | "after-child-pid-assertion" | "after-child-pid-timeout"
    | "command-shape-mismatch" | "directory-deleted" | "identity-mismatch"
    | "parent-exited-first",
) => {
  const fixtureRoot = await mkdtemp(join(tmpdir(), "studio-transcription-cleanup-"));
  const fixture = await realTranscriptionProcessGroupFixture(
    fixtureRoot,
    "hang",
    60_000,
    {
      additionalHarnessArgs: stage === "command-shape-mismatch"
        ? ["--fixture-command-shape-mismatch"]
        : undefined,
      childStartDelayMs: stage === "before-child-pid" ? 5_000 : undefined,
      keepHarnessAliveAfterChild: true,
      legacyReportedCommandShape: stage === "command-shape-mismatch"
        ? "/usr/bin/python3"
        : undefined,
    },
  );
  const supervisor = await spawnTranscriptionParentHarness(fixtureRoot, fixture.harnessPath);
  let harness: TranscriptionHarnessIdentity | undefined;
  let scenarioError: unknown;
  let cleanupError: unknown;
  let harnessSurvivedFixtureCleanup = false;
  let fixtureRootSurvivedFixtureCleanup = false;
  try {
    await waitForFile(fixture.harnessPidPath, 5_000);
    harness = await readTranscriptionHarnessIdentity(
      fixture.harnessPidPath,
      fixture.harnessNonce,
      supervisor.identity.pid,
    );
    if (stage === "command-shape-mismatch") {
      expect(harness.reportedCommandShape).toBe("/usr/bin/python3");
    }
    if (stage === "before-child-pid") {
      expect(await access(fixture.pidPath).then(() => true, () => false)).toBe(false);
    } else {
      await waitForFile(fixture.pidPath, 5_000);
      expect(JSON.parse(await readFile(fixture.pidPath, "utf8")).pid).toBeGreaterThan(0);
    }
    if (stage === "after-child-pid-timeout") {
      await waitForFile(join(fixtureRoot, "intentionally-missing"), 30);
    }
    if (stage === "directory-deleted") {
      await rm(fixtureRoot, { recursive: true, force: true });
      expect(await access(fixtureRoot).then(() => true, () => false)).toBe(false);
    }
    if (stage === "parent-exited-first") {
      supervisor.child.kill("SIGKILL");
      await waitForSpawnExit(supervisor.child, 2_000);
    }
    expect(`fixture-injected-${stage}`).toBe("fixture-cleanup-continues");
  } catch (error) {
    scenarioError = error;
  } finally {
    try {
      await cleanupTranscriptionParentFixture({
        supervisor,
        fixtureRoot,
        harness: stage === "identity-mismatch" && harness !== undefined
          ? { ...harness, nonce: "0".repeat(64) }
          : harness,
        harnessNonce: fixture.harnessNonce,
        harnessPidPath: fixture.harnessPidPath,
      });
    } catch (error) {
      cleanupError = error;
    } finally {
      fixtureRootSurvivedFixtureCleanup = await access(fixtureRoot).then(
        () => true,
        () => false,
      );
      if (harness !== undefined) {
        harnessSurvivedFixtureCleanup = processExists(harness.pid);
        if (harnessSurvivedFixtureCleanup) {
          await emergencyTerminateTranscriptionHarnessGroup(
            harness,
            fixture.harnessNonce,
            supervisor.identity.pid,
          );
        }
      }
      await rm(fixtureRoot, { recursive: true, force: true });
    }
  }
  return {
    cleanupError,
    combinedError: combineFixtureScenarioAndCleanupErrors(scenarioError, cleanupError),
    fixtureRootSurvivedFixtureCleanup,
    harnessSurvivedFixtureCleanup,
    scenarioError,
  };
};

describe("local process studio model worker", () => {
  beforeAll(async () => {
    mediaRoot = await realpath(await mkdtemp(join(tmpdir(), "studio-model-worker-media-")));
    outsideRoot = await realpath(await mkdtemp(join(tmpdir(), "studio-model-worker-outside-")));
    sourcePath = join(mediaRoot, "customer-a", "asset-a.mp4");
    await mkdir(dirname(sourcePath), { recursive: true });
    await writeFile(sourcePath, "synthetic media fixture");
    await writeFile(join(outsideRoot, "outside.mp4"), "outside fixture");
  });

  afterAll(async () => {
    await Promise.all([
      rm(mediaRoot, { recursive: true, force: true }),
      rm(outsideRoot, { recursive: true, force: true }),
    ]);
  });

  it("keeps three stdio streams when telemetry is disabled", async () => {
    let observed: { args: readonly string[]; stdio: readonly string[] } | undefined;
    const script = [
      "let input = '';",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      `const receipt = ${JSON.stringify(validReceipt())};`,
      ...bindReceiptToStagedJobLines,
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "});",
    ].join("\n");
    const worker = new LocalProcessStudioModelWorker({
      command: process.execPath,
      args: ["-e", script, "--"],
      cwd: mediaRoot,
      timeoutMs: 1_000,
      maxStdoutBytes: 64 * 1024,
      maxStderrBytes: 64,
      allowedMediaRoot: mediaRoot,
      stageTelemetry: null,
      spawnProcess: (command, args, options) => {
        observed = { args: [...args], stdio: [...options.stdio] };
        return spawn(command, args, options as any) as any;
      },
    });

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000))).resolves.toBeDefined();
    expect(observed?.stdio).toEqual(["pipe", "pipe", "pipe"]);
    expect(observed?.args).not.toContain("--telemetry-fd");
  });

  it("opens fd 3 and appends only the fixed telemetry argument when enabled", async () => {
    let observed: { args: readonly string[]; stdio: readonly string[] } | undefined;
    const telemetry = [
      "runtime-preaccept", "frontstage-extraction", "model-load",
      "processor-prepare", "model-generation", "output-validation",
    ].flatMap((stage, index) => [
      JSON.stringify({ schemaVersion: 1, sequence: index * 2 + 1, stage, state: "started" }),
      JSON.stringify({ schemaVersion: 1, sequence: index * 2 + 2, stage, state: "completed" }),
    ]).join("\n") + "\n";
    const script = [
      "const fs = require('node:fs');",
      `fs.writeSync(3, ${JSON.stringify(telemetry)});`,
      "let input = '';",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      `const receipt = ${JSON.stringify(validReceipt())};`,
      ...bindReceiptToStagedJobLines,
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "});",
    ].join("\n");
    const worker = new LocalProcessStudioModelWorker({
      command: process.execPath,
      args: ["-e", script, "--"],
      cwd: mediaRoot,
      timeoutMs: 1_000,
      maxStdoutBytes: 64 * 1024,
      maxStderrBytes: 64,
      allowedMediaRoot: mediaRoot,
      stageTelemetry: { protocol: "studio-worker-stage-telemetry-v1", maxBytes: 4096 },
      spawnProcess: (command, args, options) => {
        observed = { args: [...args], stdio: [...options.stdio] };
        return spawn(command, args, options as any) as any;
      },
    });

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000))).resolves.toBeDefined();
    expect(observed?.stdio).toEqual(["pipe", "pipe", "pipe", "pipe"]);
    expect(observed?.args.slice(-2)).toEqual(["--telemetry-fd", "3"]);
    expect(observed?.args.filter((value) => value === "--telemetry-fd")).toHaveLength(1);
  });

  it("returns a safe timing summary after a staged fixture times out", async () => {
    const telemetry = [
      [1, "runtime-preaccept", "started"],
      [2, "runtime-preaccept", "completed"],
      [3, "frontstage-extraction", "started"],
      [4, "frontstage-extraction", "completed"],
      [5, "model-load", "started"],
      [6, "model-load", "completed"],
      [7, "processor-prepare", "started"],
      [8, "processor-prepare", "completed"],
      [9, "model-generation", "started"],
    ].map(([sequence, stage, state]) => JSON.stringify({ schemaVersion: 1, sequence, stage, state }))
      .join("\n") + "\n";
    const script = [
      "const fs = require('node:fs');",
      `fs.writeSync(3, ${JSON.stringify(telemetry)});`,
      "process.stdin.resume();",
      "setInterval(() => {}, 1000);",
    ].join("\n");
    const worker = fakeWorker({
      script,
      args: ["-e", script, "--"],
      timeoutMs: 80,
      stageTelemetry: { protocol: "studio-worker-stage-telemetry-v1", maxBytes: 4096 },
    });

    try {
      await worker.run(validJob(), AbortSignal.timeout(1_000));
      throw new Error("fixture-did-not-time-out");
    } catch (error) {
      expect(error).toBeInstanceOf(StudioModelWorkerTimeoutError);
      expect((error as Error).message).toBe("studio-model-worker-timeout");
      const summary = (error as StudioModelWorkerTimeoutError).stageTelemetry;
      expect(summary?.lastStage).toBe("model-generation");
      expect(summary?.completedStageMs).toMatchObject({
        "runtime-preaccept": expect.any(Number),
        "frontstage-extraction": expect.any(Number),
        "model-load": expect.any(Number),
        "processor-prepare": expect.any(Number),
      });
      expect(summary?.activeStageElapsedMs).toBeGreaterThanOrEqual(0);
      expect(summary?.activeStageElapsedMs).toBeLessThan(1_000);
    }
  });

  it("preserves typed stage telemetry when the equal-budget external timeout wins", async () => {
    const telemetry = [
      [1, "runtime-preaccept", "started"],
      [2, "runtime-preaccept", "completed"],
      [3, "frontstage-extraction", "started"],
    ].map(([sequence, stage, state]) => JSON.stringify({ schemaVersion: 1, sequence, stage, state }))
      .join("\n") + "\n";
    const script = [
      "const fs = require('node:fs');",
      `fs.writeSync(3, ${JSON.stringify(telemetry)});`,
      "process.stdin.resume();",
      "setInterval(() => {}, 1000);",
    ].join("\n");
    const worker = fakeWorker({
      script,
      args: ["-e", script, "--"],
      timeoutMs: 100,
      stageTelemetry: { protocol: "studio-worker-stage-telemetry-v1", maxBytes: 4096 },
    });

    await expect(worker.run(validJob(), AbortSignal.timeout(100))).rejects.toMatchObject({
      name: "StudioModelWorkerTimeoutError",
      message: "studio-model-worker-timeout",
      stageTelemetry: {
        lastStage: "frontstage-extraction",
        completedStageMs: { "runtime-preaccept": expect.any(Number) },
        activeStageElapsedMs: expect.any(Number),
      },
    });
  });

  it("rejects an incomplete telemetry tail after timeout process cleanup", async () => {
    const payload = `${JSON.stringify({
      schemaVersion: 1,
      sequence: 1,
      stage: "runtime-preaccept",
      state: "started",
    })}\n{\"schemaVersion\":1`;
    const script = [
      "const fs = require('node:fs');",
      `fs.writeSync(3, ${JSON.stringify(payload)});`,
      "process.stdin.resume();",
      "setInterval(() => {}, 1000);",
    ].join("\n");
    const worker = fakeWorker({
      script,
      args: ["-e", script, "--"],
      timeoutMs: 80,
      stageTelemetry: { protocol: "studio-worker-stage-telemetry-v1", maxBytes: 4096 },
    });

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000)))
      .rejects.toThrow(/^telemetry-invalid$/);
  });

  it("returns a failed receipt with its validated terminal stage prefix only in telemetry mode", async () => {
    const telemetry = [
      [1, "runtime-preaccept", "started"],
      [2, "runtime-preaccept", "completed"],
      [3, "frontstage-extraction", "started"],
      [4, "frontstage-extraction", "completed"],
      [5, "model-load", "started"],
      [6, "model-load", "completed"],
      [7, "processor-prepare", "started"],
      [8, "processor-prepare", "completed"],
      [9, "model-generation", "started"],
    ].map(([sequence, stage, state]) => JSON.stringify({ schemaVersion: 1, sequence, stage, state }))
      .join("\n") + "\n";
    const receipt = validReceipt({
      status: "failed-retryable",
      failure: { code: "studio-vision-model-timeout" },
      observations: [],
      suggestedTags: [],
      confidence: 0,
    });
    const script = [
      "const fs = require('node:fs');",
      `fs.writeSync(3, ${JSON.stringify(telemetry)});`,
      "let input = '';",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      `const receipt = ${JSON.stringify(receipt)};`,
      ...bindReceiptToStagedJobLines,
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "});",
    ].join("\n");
    const worker = fakeWorker({
      script,
      args: ["-e", script, "--"],
      stageTelemetry: { protocol: "studio-worker-stage-telemetry-v1", maxBytes: 4096 },
    });

    try {
      await worker.run(validJob(), AbortSignal.timeout(1_000));
      throw new Error("fixture-did-not-fail");
    } catch (error) {
      expect(error).toBeInstanceOf(StudioModelWorkerFailureError);
      expect((error as StudioModelWorkerFailureError).receipt.failure?.code)
        .toBe("studio-vision-model-timeout");
      expect((error as StudioModelWorkerFailureError).stageTelemetry.lastStage)
        .toBe("model-generation");
    }
  });

  it("terminates on malformed or overflowing telemetry without exposing injected content", async () => {
    const injected = "/Users/test-user/private.mov prompt=steal";
    const fixtures = [
      {
        payload: `${JSON.stringify({ schemaVersion: 1, sequence: 1, stage: "runtime-preaccept", state: "started", injected })}\n`,
        error: "telemetry-invalid",
      },
      { payload: "x".repeat(4_097), error: "telemetry-overflow" },
    ];
    for (const fixture of fixtures) {
      const script = [
        "const fs = require('node:fs');",
        `fs.writeSync(3, ${JSON.stringify(fixture.payload)});`,
        "process.stdin.resume();",
        "setInterval(() => {}, 1000);",
      ].join("\n");
      try {
        await fakeWorker({
          script,
          args: ["-e", script, "--"],
          timeoutMs: 1_000,
          stageTelemetry: { protocol: "studio-worker-stage-telemetry-v1", maxBytes: 4096 },
        }).run(validJob(), AbortSignal.timeout(2_000));
        throw new Error("fixture-did-not-fail");
      } catch (error) {
        expect((error as Error).message).toBe(fixture.error);
        expect(String(error)).not.toContain(injected);
        expect(String(error)).not.toContain("private.mov");
      }
    }
  });

  it("accepts only one matching receipt line", async () => {
    const worker = fakeWorker({ stdout: `${JSON.stringify(validReceipt())}\n` });

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000))).resolves.toMatchObject({
      status: "succeeded",
      sourcePath,
    });
  });

  it("rebinds a verified staged-job digest to the canonical public job", async () => {
    const temporaryRoot = join(mediaRoot, "public-receipt-digest-staging");
    const observedPath = join(mediaRoot, "public-receipt-digest-observed.json");
    await mkdir(temporaryRoot, { mode: 0o700 });
    const script = [
      "const { createHash } = require('node:crypto');",
      "const { writeFileSync } = require('node:fs');",
      "let input = '';",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      "const job = JSON.parse(input.trim());",
      "const canonical = JSON.stringify(Object.fromEntries(Object.entries(job).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)));",
      "const stagedDigest = createHash('sha256').update(canonical).digest('hex');",
      `writeFileSync(${JSON.stringify(observedPath)}, JSON.stringify({ sourcePath: job.sourcePath, inputDigest: stagedDigest }), { mode: 0o600 });`,
      `const receipt = ${JSON.stringify(validReceipt())};`,
      "receipt.sourcePath = job.sourcePath;",
      "receipt.inputDigest = stagedDigest;",
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "});",
    ].join("\n");
    const publicJob = validJob();
    try {
      const receipt = await fakeWorker({
        script,
        env: fixedTestEnvironment(temporaryRoot),
      }).run(publicJob, AbortSignal.timeout(1_000));
      const observed = JSON.parse(await readFile(observedPath, "utf8")) as {
        sourcePath: string;
        inputDigest: string;
      };

      expect(observed.sourcePath).not.toBe(publicJob.sourcePath);
      expect(observed.sourcePath.startsWith(`${temporaryRoot}/`)).toBe(true);
      expect(observed.inputDigest).not.toBe(canonicalJobDigest(publicJob));
      expect(receipt).toMatchObject({
        sourcePath: publicJob.sourcePath,
        inputDigest: canonicalJobDigest(publicJob),
      });
    } finally {
      await rm(temporaryRoot, { recursive: true, force: true });
      await rm(observedPath, { force: true });
    }
  });

  it("rejects a child digest that does not bind the exact staged job", async () => {
    const script = [
      "let input = '';",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      "const job = JSON.parse(input.trim());",
      `const receipt = ${JSON.stringify(validReceipt())};`,
      "receipt.sourcePath = job.sourcePath;",
      "receipt.inputDigest = '0'.repeat(64);",
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "});",
    ].join("\n");

    await expect(fakeWorker({ script }).run(
      validJob(),
      AbortSignal.timeout(1_000),
    )).rejects.toThrow(/^studio-model-receipt-binding-mismatch$/);
  });

  it("spawns with only the fixed environment and adds trusted context only when supplied", async () => {
    const ambient = {
      PYTHONPATH: "/tmp/ambient-python",
      PYTHONHOME: "/tmp/ambient-home",
      VIRTUAL_ENV: "/tmp/ambient-venv",
      HTTPS_PROXY: "http://ambient.invalid",
      HF_TOKEN: "ambient-token",
      OPENAI_API_KEY: "test-ambient-provider-token",
      SSL_CERT_FILE: "/tmp/ambient-cert.pem",
    };
    const previous = Object.fromEntries(Object.keys(ambient).map((key) => [key, process.env[key]]));
    Object.assign(process.env, ambient);
    const fixed = {
      PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
      HOME: mediaRoot,
      LANG: "en_US.UTF-8",
      LC_ALL: "en_US.UTF-8",
      TMPDIR: tmpdir(),
      HF_HUB_OFFLINE: "1",
      TRANSFORMERS_OFFLINE: "1",
    };
    const script = [
      "let input = '';",
      "process.stdin.setEncoding('utf8');",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      `const receipt = ${JSON.stringify(validReceipt())};`,
      ...bindReceiptToStagedJobLines,
      "receipt.qualitySignals = { environment: process.env };",
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "});",
    ].join("\n");
    try {
      const worker = fakeWorker({ script, env: fixed });
      const withoutContext = await worker.run(validJob(), AbortSignal.timeout(1_000));
      const withoutContextEnvironment = withoutContext.qualitySignals.environment as Record<string, string>;
      const { __CF_USER_TEXT_ENCODING: macOsTextEncoding, ...portableEnvironment } = withoutContextEnvironment;
      expect(portableEnvironment).toEqual(fixed);
      expect(macOsTextEncoding).toMatch(/^0x[0-9A-F]+:0x[0-9A-F]+:0x[0-9A-F]+$/i);
      expect(withoutContextEnvironment).not.toHaveProperty("STUDIO_VISION_CONTEXT_JSON");
      for (const key of Object.keys(ambient)) {
        expect(withoutContextEnvironment).not.toHaveProperty(key);
      }

      const context = {
        customerId: "customer-a",
        sessionId: "session-1",
        sourceAssetId: "asset-a",
        sourceDigest: "a".repeat(64),
        sourceStartMs: 1_000,
        sourceEndMs: 4_000,
        transcriptSpans: [],
        demand: null,
      };
      const withContext = await worker.run(validJob(), AbortSignal.timeout(1_000), context);
      const withContextEnvironment = withContext.qualitySignals.environment as Record<string, string>;
      expect(JSON.parse(String(withContextEnvironment.STUDIO_VISION_CONTEXT_JSON)))
        .toEqual(context);
    } finally {
      for (const [key, value] of Object.entries(previous)) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
    }
  });

  it("derives default HOME and TMPDIR from the trusted media root and rejects unsafe environment overrides", async () => {
    const previous = {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      TMPDIR: process.env.TMPDIR,
      HTTPS_PROXY: process.env.HTTPS_PROXY,
    };
    Object.assign(process.env, {
      PATH: join(outsideRoot, "ambient-bin"),
      HOME: join(outsideRoot, "ambient-home"),
      TMPDIR: join(outsideRoot, "ambient-tmp"),
      HTTPS_PROXY: "http://ambient.invalid",
    });
    const script = [
      "let input = '';",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      `const receipt = ${JSON.stringify(validReceipt())};`,
      ...bindReceiptToStagedJobLines,
      "receipt.qualitySignals = { environment: process.env };",
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "});",
    ].join("\n");
    try {
      const worker = new LocalProcessStudioModelWorker({
        command: process.execPath,
        args: ["-e", script],
        cwd: mediaRoot,
        timeoutMs: 1_000,
        maxStdoutBytes: 64 * 1024,
        maxStderrBytes: 64,
        allowedMediaRoot: mediaRoot,
      });
      const receipt = await worker.run(validJob(), AbortSignal.timeout(1_000));
      const environment = receipt.qualitySignals.environment as Record<string, string>;
      const { __CF_USER_TEXT_ENCODING: _macOsTextEncoding, ...portableEnvironment } = environment;
      expect(portableEnvironment).toEqual({
        PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
        HOME: join(mediaRoot, ".studio-model-worker-private", "home"),
        LANG: "en_US.UTF-8",
        LC_ALL: "en_US.UTF-8",
        TMPDIR: join(mediaRoot, ".studio-model-worker-private", "tmp"),
        HF_HUB_OFFLINE: "1",
        TRANSFORMERS_OFFLINE: "1",
      });
      for (const path of [environment.HOME!, environment.TMPDIR!]) {
        expect((await lstat(path)).mode & 0o777).toBe(0o700);
      }
      expect(() => new LocalProcessStudioModelWorker({
        command: process.execPath,
        args: ["-e", script],
        cwd: mediaRoot,
        env: { ...portableEnvironment, HTTPS_PROXY: "http://ambient.invalid" },
        timeoutMs: 1_000,
        maxStdoutBytes: 64 * 1024,
        maxStderrBytes: 64,
        allowedMediaRoot: mediaRoot,
      })).toThrow(/^studio-model-worker-config-invalid$/);
      expect(() => new LocalProcessStudioModelWorker({
        command: process.execPath,
        args: ["-e", script],
        cwd: mediaRoot,
        env: { ...portableEnvironment, TMPDIR: "relative-tmp" },
        timeoutMs: 1_000,
        maxStdoutBytes: 64 * 1024,
        maxStderrBytes: 64,
        allowedMediaRoot: mediaRoot,
      })).toThrow(/^studio-model-worker-config-invalid$/);
    } finally {
      for (const [key, value] of Object.entries(previous)) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
    }
  });

  it("rejects relative, symlinked, and world-writable worker directories", async () => {
    const unsafeDirectory = join(mediaRoot, "unsafe-worker-cwd");
    const linkedDirectory = join(mediaRoot, "linked-worker-cwd");
    await mkdir(unsafeDirectory, { mode: 0o700 });
    await chmod(unsafeDirectory, 0o777);
    await symlink(mediaRoot, linkedDirectory);
    for (const cwd of ["relative-worker-cwd", linkedDirectory, unsafeDirectory]) {
      expect(() => new LocalProcessStudioModelWorker({
        command: process.execPath,
        args: ["-e", "process.exit(0)"],
        cwd,
        timeoutMs: 1_000,
        maxStdoutBytes: 64,
        maxStderrBytes: 64,
        allowedMediaRoot: mediaRoot,
      }), cwd).toThrow(/^studio-model-worker-config-invalid$/);
    }
  });

  it("never executes a launcher path replaced after its verified identity was captured", async () => {
    const launcherRoot = join(mediaRoot, "launcher-replacement");
    const launcherPath = join(launcherRoot, "run-offline-worker");
    const originalPath = join(launcherRoot, "verified-original");
    const markerPath = join(launcherRoot, "replacement-executed");
    await mkdir(launcherRoot, { mode: 0o700 });
    await writeFile(launcherPath, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
    await chmod(launcherPath, 0o755);
    const identity = await launcherIdentity(launcherPath);
    await import("node:fs/promises").then(({ rename }) => rename(launcherPath, originalPath));
    await writeFile(launcherPath, [
      "#!/bin/sh",
      `printf replacement > ${JSON.stringify(markerPath)}`,
      "exit 7",
      "",
    ].join("\n"), { mode: 0o755 });
    await chmod(launcherPath, 0o755);
    const worker = new LocalProcessStudioModelWorker({
      command: launcherPath,
      args: [],
      commandIdentity: identity,
      cwd: mediaRoot,
      timeoutMs: 1_000,
      maxStdoutBytes: 64,
      maxStderrBytes: 64,
      allowedMediaRoot: mediaRoot,
    } as any);

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000)))
      .rejects.toThrow(/^studio-model-worker-launcher-integrity-failed$/);
    await expect(access(markerPath)).rejects.toThrow();
  });

  it("detects a private launcher copy replaced in the spawn window and preserves the foreign inode", async () => {
    const launcherRoot = join(mediaRoot, "launcher-copy-tamper");
    const launcherPath = join(launcherRoot, "run-offline-worker");
    await mkdir(launcherRoot, { mode: 0o700 });
    await writeFile(launcherPath, [
      "#!/bin/sh",
      `exec ${JSON.stringify(process.execPath)} -e ${JSON.stringify([
        "let input = '';",
        "process.stdin.on('data', (chunk) => { input += chunk; });",
        "process.stdin.on('end', () => process.exit(0));",
      ].join("\n"))}`,
      "",
    ].join("\n"), { mode: 0o755 });
    await chmod(launcherPath, 0o755);
    const identity = await launcherIdentity(launcherPath);
    let replacedCopy = "";
    const worker = new LocalProcessStudioModelWorker({
      command: launcherPath,
      args: [],
      commandIdentity: identity,
      spawnProcess: (command, args, options) => {
        const child = spawn(command, args, options);
        replacedCopy = command === "/bin/sh" ? args[3]! : command;
        unlinkSync(replacedCopy);
        writeFileSync(replacedCopy, "#!/bin/sh\nexit 7\n", { mode: 0o755 });
        chmodSync(replacedCopy, 0o755);
        return child;
      },
      cwd: mediaRoot,
      timeoutMs: 1_000,
      maxStdoutBytes: 64,
      maxStderrBytes: 64,
      allowedMediaRoot: mediaRoot,
    });

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000)))
      .rejects.toThrow(/^studio-model-worker-launcher-integrity-failed$/);
    await expect(access(replacedCopy)).resolves.toBeUndefined();
  });

  it("executes a verified private copy with original launcher semantics and literal hostile arguments", async () => {
    const runtimeRoot = join(mediaRoot, "runtime root ; literal");
    const launcherRoot = join(runtimeRoot, "bin");
    const launcherPath = join(launcherRoot, "run-offline-worker");
    const workerScriptPath = join(runtimeRoot, "fixture-worker.cjs");
    const injectionMarker = join(mediaRoot, "launcher-argument-injected");
    const hostileArguments = [
      "literal ; shell metacharacters",
      `$(touch ${injectionMarker})`,
    ];
    const workerScript = [
      "let input = '';",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      `const receipt = ${JSON.stringify(validReceipt())};`,
      ...bindReceiptToStagedJobLines,
      "receipt.qualitySignals = { runtimeRoot: process.argv[2], arguments: process.argv.slice(3) };",
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "});",
    ].join("\n");
    await mkdir(launcherRoot, { mode: 0o700, recursive: true });
    await writeFile(workerScriptPath, workerScript, { mode: 0o600 });
    await writeFile(launcherPath, [
      "#!/bin/sh",
      "SCRIPT_DIR=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd -P)",
      "RUNTIME_ROOT=$(CDPATH= cd -- \"$SCRIPT_DIR/..\" && pwd -P)",
      `exec ${JSON.stringify(process.execPath)} ${JSON.stringify(workerScriptPath)} "$RUNTIME_ROOT" "$@"`,
      "",
    ].join("\n"), { mode: 0o755 });
    await chmod(launcherPath, 0o755);
    let executedCommand = "";
    let executedArguments: readonly string[] = [];
    let copiedLauncherPath = "";
    let observedCopy: Readonly<{
      sourceLinks: bigint;
      copiedLinks: bigint;
      distinctInode: boolean;
      copiedMode: bigint;
      directoryMode: bigint;
    }> | undefined;
    const worker = new LocalProcessStudioModelWorker({
      command: launcherPath,
      args: hostileArguments,
      commandIdentity: await launcherIdentity(launcherPath),
      spawnProcess: (command, args, options) => {
        executedCommand = command;
        executedArguments = [...args];
        copiedLauncherPath = command === "/bin/sh" ? args[3]! : command;
        const source = lstatSync(launcherPath, { bigint: true });
        const copied = lstatSync(copiedLauncherPath, { bigint: true });
        const privateDirectory = lstatSync(dirname(copiedLauncherPath), { bigint: true });
        observedCopy = {
          sourceLinks: source.nlink,
          copiedLinks: copied.nlink,
          distinctInode: copied.ino !== source.ino,
          copiedMode: copied.mode & 0o7777n,
          directoryMode: privateDirectory.mode & 0o7777n,
        };
        return spawn(command, args, options);
      },
      cwd: mediaRoot,
      timeoutMs: 1_000,
      maxStdoutBytes: 64 * 1024,
      maxStderrBytes: 64,
      allowedMediaRoot: mediaRoot,
    });

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000)))
      .resolves.toMatchObject({
        status: "succeeded",
        sourcePath,
        qualitySignals: { runtimeRoot, arguments: hostileArguments },
      });
    expect(executedCommand).toBe("/bin/sh");
    expect(observedCopy).toEqual({
      sourceLinks: 1n,
      copiedLinks: 1n,
      distinctInode: true,
      copiedMode: 0o755n,
      directoryMode: 0o700n,
    });
    expect(executedArguments.slice(0, 4)).toEqual([
      "-c",
      'launcher_copy=$1; shift; . "$launcher_copy"',
      launcherPath,
      copiedLauncherPath,
    ]);
    expect((await lstat(launcherPath, { bigint: true })).nlink).toBe(1n);
    await expect(access(copiedLauncherPath)).rejects.toThrow();
    await expect(access(dirname(copiedLauncherPath))).rejects.toThrow();
    await expect(access(injectionMarker)).rejects.toThrow();
  });

  it("fails closed when the verified launcher source is replaced after its private copy is spawned", async () => {
    const launcherRoot = join(mediaRoot, "launcher-source-spawn-replacement");
    const launcherPath = join(launcherRoot, "run-offline-worker");
    const originalPath = join(launcherRoot, "original-launcher");
    const markerPath = join(launcherRoot, "replacement-executed");
    const workerScriptPath = join(launcherRoot, "fixture-worker.cjs");
    await mkdir(launcherRoot, { mode: 0o700 });
    await writeFile(workerScriptPath, [
      "let input = '';",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      `const receipt = ${JSON.stringify(validReceipt())};`,
      ...bindReceiptToStagedJobLines,
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "});",
    ].join("\n"), { mode: 0o600 });
    await writeFile(launcherPath, [
      "#!/bin/sh",
      `exec ${JSON.stringify(process.execPath)} ${JSON.stringify(workerScriptPath)}`,
      "",
    ].join("\n"), { mode: 0o755 });
    await chmod(launcherPath, 0o755);
    const worker = new LocalProcessStudioModelWorker({
      command: launcherPath,
      args: [],
      commandIdentity: await launcherIdentity(launcherPath),
      spawnProcess: (command, args, options) => {
        const child = spawn(command, args, options);
        renameSync(launcherPath, originalPath);
        writeFileSync(launcherPath, `#!/bin/sh\nprintf replaced > ${JSON.stringify(markerPath)}\nexit 7\n`, { mode: 0o755 });
        chmodSync(launcherPath, 0o755);
        return child;
      },
      cwd: mediaRoot,
      timeoutMs: 1_000,
      maxStdoutBytes: 64 * 1024,
      maxStderrBytes: 64,
      allowedMediaRoot: mediaRoot,
    });

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000)))
      .rejects.toThrow(/^studio-model-worker-launcher-integrity-failed$/);
    await expect(access(markerPath)).rejects.toThrow();
  });

  it("supports concurrent formal launches while the approved source remains single-linked", async () => {
    const launcherRoot = join(mediaRoot, "launcher-copy-concurrent");
    const launcherPath = join(launcherRoot, "run-offline-worker");
    const workerScriptPath = join(launcherRoot, "fixture-worker.cjs");
    await mkdir(launcherRoot, { mode: 0o700 });
    await writeFile(workerScriptPath, [
      "let input = '';",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      `const receipt = ${JSON.stringify(validReceipt())};`,
      ...bindReceiptToStagedJobLines,
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "});",
    ].join("\n"), { mode: 0o600 });
    await writeFile(launcherPath, [
      "#!/bin/sh",
      `exec ${JSON.stringify(process.execPath)} ${JSON.stringify(workerScriptPath)}`,
      "",
    ].join("\n"), { mode: 0o755 });
    await chmod(launcherPath, 0o755);
    const worker = new LocalProcessStudioModelWorker({
      command: launcherPath,
      args: [],
      commandIdentity: await launcherIdentity(launcherPath),
      cwd: mediaRoot,
      timeoutMs: 2_000,
      maxStdoutBytes: 64 * 1024,
      maxStderrBytes: 64,
      allowedMediaRoot: mediaRoot,
    });

    await expect(Promise.all([
      worker.run(validJob(), AbortSignal.timeout(2_000)),
      worker.run(validJob(), AbortSignal.timeout(2_000)),
      worker.run(validJob(), AbortSignal.timeout(2_000)),
    ])).resolves.toHaveLength(3);
    expect((await lstat(launcherPath, { bigint: true })).nlink).toBe(1n);
  });

  it("copies the checked source into private immutable staging before the child can reopen it", async () => {
    const raceSource = join(mediaRoot, "customer-a", "race-source.mp4");
    const originalSource = join(mediaRoot, "customer-a", "race-source-original.mp4");
    await writeFile(raceSource, "trusted-source-bytes");
    const script = [
      "let input = '';",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => setTimeout(() => {",
      "const job = JSON.parse(input.trim());",
      `const receipt = ${JSON.stringify(validReceipt())};`,
      ...bindReceiptToExistingStagedJobLines,
      "receipt.qualitySignals = { observedContent: require('node:fs').readFileSync(job.sourcePath, 'utf8') };",
      "process.stdout.write(JSON.stringify(receipt) + '\\n');",
      "}, 100));",
    ].join("\n");
    const worker = fakeWorker({ script });
    const pending = worker.run(
      validJob({ sourcePath: raceSource }),
      AbortSignal.timeout(2_000),
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    await import("node:fs/promises").then(({ rename }) => rename(raceSource, originalSource));
    await writeFile(raceSource, "replacement-source-bytes");

    await expect(pending).resolves.toMatchObject({
      sourcePath: raceSource,
      qualitySignals: { observedContent: "trusted-source-bytes" },
    });
  });

  it("removes its staged media inode when the source changes during descriptor copy", async () => {
    const temporaryRoot = join(mediaRoot, "source-mutation-staging");
    const mutableSource = join(mediaRoot, "customer-a", "source-mutates-during-copy.mp4");
    await mkdir(temporaryRoot, { mode: 0o700 });
    await writeFile(mutableSource, Buffer.alloc(128 * 1024, 0x61), { mode: 0o644 });
    const originalWriteSync = fs.writeSync;
    let mutated = false;
    fs.writeSync = ((...args: Parameters<typeof fs.writeSync>) => {
      const result = Reflect.apply(originalWriteSync, fs, args) as number;
      if (!mutated) {
        mutated = true;
        chmodSync(mutableSource, 0o600);
      }
      return result;
    }) as typeof fs.writeSync;
    syncBuiltinESMExports();
    let pending: Promise<unknown>;
    try {
      pending = fakeWorker({ env: fixedTestEnvironment(temporaryRoot) }).run(
        validJob({ sourcePath: mutableSource }),
        AbortSignal.timeout(1_000),
      );
    } finally {
      fs.writeSync = originalWriteSync;
      syncBuiltinESMExports();
      chmodSync(mutableSource, 0o644);
    }

    await expect(pending!).rejects.toThrow(/^studio-model-source-integrity-failed$/);
    expect(await readdir(temporaryRoot)).toEqual([]);
  });

  it("removes its staged media inode after a simulated disk-full write failure", async () => {
    const temporaryRoot = join(mediaRoot, "disk-full-staging");
    await mkdir(temporaryRoot, { mode: 0o700 });
    const originalWriteSync = fs.writeSync;
    fs.writeSync = (() => {
      const error = new Error("synthetic disk full") as NodeJS.ErrnoException;
      error.code = "ENOSPC";
      throw error;
    }) as typeof fs.writeSync;
    syncBuiltinESMExports();
    let pending: Promise<unknown>;
    try {
      pending = fakeWorker({ env: fixedTestEnvironment(temporaryRoot) }).run(
        validJob(),
        AbortSignal.timeout(1_000),
      );
    } finally {
      fs.writeSync = originalWriteSync;
      syncBuiltinESMExports();
    }

    await expect(pending!).rejects.toThrow(/^studio-model-source-integrity-failed$/);
    expect(await readdir(temporaryRoot)).toEqual([]);
  });

  it("preserves a foreign staged-path replacement while removing only its opened inode", async () => {
    const temporaryRoot = join(mediaRoot, "staged-path-replacement");
    await mkdir(temporaryRoot, { mode: 0o700 });
    const originalWriteSync = fs.writeSync;
    let replacementPath = "";
    fs.writeSync = ((...args: Parameters<typeof fs.writeSync>) => {
      const result = Reflect.apply(originalWriteSync, fs, args) as number;
      if (!replacementPath) {
        const stagingDirectory = join(temporaryRoot, readdirSync(temporaryRoot)[0]!);
        replacementPath = join(stagingDirectory, "media");
        unlinkSync(replacementPath);
        writeFileSync(replacementPath, "foreign replacement", { mode: 0o600 });
      }
      return result;
    }) as typeof fs.writeSync;
    syncBuiltinESMExports();
    let pending: Promise<unknown>;
    try {
      pending = fakeWorker({ env: fixedTestEnvironment(temporaryRoot) }).run(
        validJob(),
        AbortSignal.timeout(1_000),
      );
    } finally {
      fs.writeSync = originalWriteSync;
      syncBuiltinESMExports();
    }

    await expect(pending!).rejects.toThrow(/^studio-model-source-integrity-failed$/);
    await expect(readFile(replacementPath, "utf8")).resolves.toBe("foreign replacement");
    await rm(dirname(replacementPath), { recursive: true, force: true });
  });

  it("removes its empty staging directory when the owned staged path disappears", async () => {
    const temporaryRoot = join(mediaRoot, "staged-path-disappears");
    await mkdir(temporaryRoot, { mode: 0o700 });
    const originalWriteSync = fs.writeSync;
    let removed = false;
    fs.writeSync = ((...args: Parameters<typeof fs.writeSync>) => {
      const result = Reflect.apply(originalWriteSync, fs, args) as number;
      if (!removed) {
        removed = true;
        const stagingDirectory = join(temporaryRoot, readdirSync(temporaryRoot)[0]!);
        unlinkSync(join(stagingDirectory, "media"));
      }
      return result;
    }) as typeof fs.writeSync;
    syncBuiltinESMExports();
    let pending: Promise<unknown>;
    try {
      pending = fakeWorker({ env: fixedTestEnvironment(temporaryRoot) }).run(
        validJob(),
        AbortSignal.timeout(1_000),
      );
    } finally {
      fs.writeSync = originalWriteSync;
      syncBuiltinESMExports();
    }

    await expect(pending!).rejects.toThrow(/^studio-model-source-integrity-failed$/);
    expect(await readdir(temporaryRoot)).toEqual([]);
  });

  it("removes staged media before an aborted run settles", async () => {
    const temporaryRoot = join(mediaRoot, "aborted-staging");
    const readyPath = join(mediaRoot, "aborted-staging-ready");
    await mkdir(temporaryRoot, { mode: 0o700 });
    const worker = fakeWorker({
      env: fixedTestEnvironment(temporaryRoot),
      script: [
        `require('node:fs').writeFileSync(${JSON.stringify(readyPath)}, 'ready');`,
        "process.stdin.resume();",
        "setInterval(() => {}, 1000);",
      ].join("\n"),
      timeoutMs: 5_000,
    });
    const controller = new AbortController();
    const pending = worker.run(validJob(), controller.signal);
    await waitForFile(readyPath, 1_000);
    controller.abort();

    await expect(pending).rejects.toThrow(/^studio-model-worker-aborted$/);
    expect(await readdir(temporaryRoot)).toEqual([]);
  });

  it("uses an OS sandbox to block direct and child-process sockets without trusting their exit code", async () => {
    const sandboxPath = "/usr/bin/sandbox-exec";
    await expect(access(sandboxPath)).resolves.toBeUndefined();
    const fixtureRoot = await mkdtemp(join(tmpdir(), "studio-network-denial-"));
    const policyPath = join(fixtureRoot, "deny-network.sb");
    const launcherPath = join(fixtureRoot, "run-offline-worker");
    await writeFile(policyPath, "(version 1)\n(deny network*)\n(allow default)\n");
    await writeFile(launcherPath, [
      "#!/bin/sh",
      "set -eu",
      `exec ${sandboxPath} -f ${JSON.stringify(policyPath)} \"$@\"`,
      "",
    ].join("\n"));
    await chmod(launcherPath, 0o755);
    let accepted = 0;
    const listener = createServer((socket) => {
      accepted += 1;
      socket.destroy();
    });
    await new Promise<void>((resolveListen, rejectListen) => {
      listener.once("error", rejectListen);
      listener.listen(0, "127.0.0.1", resolveListen);
    });
    const address = listener.address();
    if (address === null || typeof address === "string") throw new Error("fixture-listener-unavailable");
    const socketAttempt = [
      "const net = require('node:net');",
      `const socket = net.connect(${address.port}, '127.0.0.1');`,
      "socket.once('connect', () => process.exit(0));",
      "socket.once('error', () => process.exit(73));",
      "setTimeout(() => process.exit(74), 1000);",
    ].join("\n");
    try {
      await new Promise<void>((resolveControl, rejectControl) => {
        const control = spawn(process.execPath, ["-e", socketAttempt], { stdio: "ignore" });
        control.once("error", rejectControl);
        control.once("close", (code) => code === 0
          ? resolveControl()
          : rejectControl(new Error(`fixture-control-failed:${code}`)));
      });
      expect(accepted).toBe(1);

      for (const script of [
        socketAttempt,
        [
          "const { spawn } = require('node:child_process');",
          `const child = spawn(process.execPath, ['-e', ${JSON.stringify(socketAttempt)}], { stdio: 'ignore' });`,
          "child.once('error', () => process.exit(74));",
          "child.once('close', (code) => process.exit(code));",
        ].join("\n"),
      ]) {
        const worker = fakeWorker({
          command: launcherPath,
          args: [process.execPath, "-e", script],
          env: {
            PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
            HOME: fixtureRoot,
            LANG: "en_US.UTF-8",
            LC_ALL: "en_US.UTF-8",
            TMPDIR: fixtureRoot,
            HF_HUB_OFFLINE: "1",
            TRANSFORMERS_OFFLINE: "1",
          },
          maxStderrBytes: 4 * 1024,
          timeoutMs: 5_000,
        });
        await expect(worker.run(validJob(), AbortSignal.timeout(6_000)))
          .rejects.toThrow(/^studio-model-worker-retryable-failure$/);
      }
      expect(accepted).toBe(1);
    } finally {
      await new Promise<void>((resolveClose) => listener.close(() => resolveClose()));
      await rm(fixtureRoot, { recursive: true, force: true });
    }
  });

  it("resolves a relative source against the child cwd and sends its canonical absolute path", async () => {
    const childCwd = join(mediaRoot, "child-cwd");
    await mkdir(childCwd);
    const relativeSourcePath = relative(childCwd, sourcePath);

    await expect(echoOutgoingSourceWorker({ cwd: childCwd }).run(
      validJob({ sourcePath: relativeSourcePath }),
      AbortSignal.timeout(1_000),
    )).resolves.toMatchObject({ sourcePath });
  });

  it("sends an in-root symlink as its canonical target and binds the receipt to that target", async () => {
    const symlinkPath = join(mediaRoot, "customer-a", "linked-inside.mp4");
    await symlink(sourcePath, symlinkPath);

    await expect(echoOutgoingSourceWorker().run(
      validJob({ sourcePath: symlinkPath }),
      AbortSignal.timeout(1_000),
    )).resolves.toMatchObject({ sourcePath });
    await expect(fakeWorker({
      stdout: `${JSON.stringify(validReceipt({ sourcePath: symlinkPath }))}\n`,
      bindSourcePath: false,
    }).run(
      validJob({ sourcePath: symlinkPath }),
      AbortSignal.timeout(1_000),
    )).rejects.toThrow("studio-model-receipt-binding-mismatch");
  });

  it("reports a truthful uninstalled fallback health state", async () => {
    await expect(fakeWorker({}).health()).resolves.toEqual({
      mode: "offline-fallback",
      installed: false,
      modelName: null,
      checkpointDigest: null,
    });
  });

  it.each([
    ["wrong-source", "studio-model-receipt-binding-mismatch"],
    ["second-json-line", "studio-model-worker-receipt-line-count-invalid"],
    ["invalid-json", "studio-model-worker-invalid-receipt"],
    ["stdout-overflow", "studio-model-worker-stdout-overflow"],
    ["stderr-overflow", "studio-model-worker-stderr-overflow"],
  ] as const)("fails closed for %s", async (fixture, error) => {
    await expect(makeFixtureWorker(fixture).run(validJob(), AbortSignal.timeout(1_000)))
      .rejects.toThrow(error);
  });

  it.each([
    ["jobId", "job-2"],
    ["customerId", "customer-b"],
    ["sessionId", "session-2"],
    ["sourceAssetId", "asset-b"],
    ["sourceDigest", "b".repeat(64)],
    ["sourcePath", join(tmpdir(), "other-source.mp4")],
    ["sourceStartMs", 1_001],
    ["sourceEndMs", 4_001],
    ["layer", "transcript"],
    ["operationKey", "another-operation"],
  ])("rejects a receipt with a mismatched %s binding", async (field, value) => {
    const worker = fakeWorker({
      stdout: `${JSON.stringify(validReceipt({ [field]: value }))}\n`,
      bindSourcePath: field !== "sourcePath",
    });

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000)))
      .rejects.toThrow("studio-model-receipt-binding-mismatch");
  });

  it.each([
    ["customerId", "customer-b"],
    ["sessionId", "session-b"],
    ["sourceAssetId", "asset-b"],
    ["sourceDigest", "b".repeat(64)],
    ["sourceStartMs", 1_001],
    ["sourceEndMs", 4_001],
  ])("rejects trusted vision context with a mismatched %s before spawning", async (field, value) => {
    const marker = join(mediaRoot, `forged-context-${field}`);
    const worker = fakeWorker({
      script: `require('node:fs').writeFileSync(${JSON.stringify(marker)}, 'spawned')`,
    });
    const context = {
      customerId: "customer-a",
      sessionId: "session-1",
      sourceAssetId: "asset-a",
      sourceDigest: "a".repeat(64),
      sourceStartMs: 1_000,
      sourceEndMs: 4_000,
      transcriptSpans: [],
      demand: null,
      [field]: value,
    };

    await expect((worker.run as any)(validJob(), AbortSignal.timeout(1_000), context))
      .rejects.toThrow("studio-model-worker-context-binding-mismatch");
    await expect(access(marker)).rejects.toThrow();
  });

  it("rejects an unknown receipt key instead of leaking validator details", async () => {
    const worker = fakeWorker({
      stdout: `${JSON.stringify({ ...validReceipt(), rawTranscript: "private words" })}\n`,
    });

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000)))
      .rejects.toThrow(/^studio-model-worker-invalid-receipt$/);
  });

  it("rejects invalid UTF-8 bytes instead of decoding replacement-character suggestions", async () => {
    const line = Buffer.from(`${JSON.stringify(validReceipt())}\n`, "utf8");
    const replacementOffset = line.indexOf(Buffer.from("possibly", "utf8"));
    line[replacementOffset] = 0xff;
    const script = `process.stdin.resume(); process.stdin.on('end', () => process.stdout.write(Buffer.from(${JSON.stringify([...line])})));`;

    await expect(fakeWorker({ script }).run(validJob(), AbortSignal.timeout(1_000)))
      .rejects.toThrow(/^studio-model-worker-invalid-receipt$/);
  });

  it("requires one newline-terminated receipt without blank output lines", async () => {
    for (const stdout of [
      JSON.stringify(validReceipt()),
      `\n${JSON.stringify(validReceipt())}\n`,
      `${JSON.stringify(validReceipt())}\n\n`,
    ]) {
      await expect(fakeWorker({ stdout, bindSourcePath: false }).run(validJob(), AbortSignal.timeout(1_000)))
        .rejects.toThrow("studio-model-worker-receipt-line-count-invalid");
    }
  });

  it("measures output limits in bytes and accepts stderr at the exact byte cap", async () => {
    await expect(fakeWorker({
      stdout: `${JSON.stringify(validReceipt())}\n`,
      stderr: "x".repeat(64),
      maxStderrBytes: 64,
    }).run(validJob(), AbortSignal.timeout(1_000))).resolves.toMatchObject({ status: "succeeded" });

    await expect(fakeWorker({
      stdout: `${JSON.stringify(validReceipt())}\n`,
      stderr: "敏".repeat(22),
      maxStderrBytes: 64,
    }).run(validJob(), AbortSignal.timeout(1_000)))
      .rejects.toThrow("studio-model-worker-stderr-overflow");
  });

  it("maps non-zero exits to a retryable error without exposing child output", async () => {
    const rejection = makeFixtureWorker("nonzero").run(validJob(), AbortSignal.timeout(1_000));

    await expect(rejection).rejects.toThrow(/^studio-model-worker-retryable-failure$/);
    await expect(rejection).rejects.not.toThrow(sourcePath);
    await expect(rejection).rejects.not.toThrow("raw transcript");
  });

  it("does not authenticate an ordinary exit 73 as a network event", async () => {
    const rejection = fakeWorker({ exitCode: 73 }).run(validJob(), AbortSignal.timeout(1_000));

    await expect(rejection).rejects.toThrow(/^studio-model-worker-retryable-failure$/);
    await expect(rejection).rejects.not.toThrow(/network/i);
  });

  it("maps a child signal exit to a retryable error without exposing process details", async () => {
    const worker = fakeWorker({
      script: "process.stdin.resume(); process.stdin.on('end', () => process.kill(process.pid, 'SIGTERM'));",
    });

    await expect(worker.run(validJob(), AbortSignal.timeout(1_000)))
      .rejects.toThrow(/^studio-model-worker-retryable-failure$/);
  });

  it("kills orphaned process-group members before settling exit 7, exit 0, or a valid receipt", async () => {
    for (const outcome of ["exit-7", "exit-0", "valid-receipt"] as const) {
      const pidPath = join(mediaRoot, `terminal-orphan-${outcome}.json`);
      let grandchildPid: number | undefined;
      try {
        const pending = terminalOrphanFixtureWorker(pidPath, outcome)
          .run(validJob(), AbortSignal.timeout(2_000));
        if (outcome === "exit-7") {
          await expect(pending).rejects.toThrow(/^studio-model-worker-retryable-failure$/);
        } else if (outcome === "exit-0") {
          await expect(pending).rejects.toThrow(/^studio-model-worker-receipt-line-count-invalid$/);
        } else {
          await expect(pending).resolves.toMatchObject({ status: "succeeded" });
        }
        grandchildPid = JSON.parse(await readFile(pidPath, "utf8")).grandchild;
        expect(processExists(grandchildPid!)).toBe(false);
      } finally {
        if (grandchildPid !== undefined && processExists(grandchildPid)) {
          process.kill(grandchildPid, "SIGKILL");
          await waitForProcessesToExit([grandchildPid], 2_000).catch(() => {});
        }
      }
    }
  });

  it("cleans each completed process group without killing a concurrent worker group", async () => {
    const firstPidPath = join(mediaRoot, "concurrent-orphan-first.json");
    const secondPidPath = join(mediaRoot, "concurrent-orphan-second.json");
    let firstPids: { worker: number; grandchild: number } | undefined;
    let secondPids: { worker: number; grandchild: number } | undefined;
    try {
      const second = terminalOrphanFixtureWorker(secondPidPath, "valid-receipt", 400)
        .run(validJob(), AbortSignal.timeout(2_000));
      const secondSettled = second.then(
        (value) => ({ value, error: null }),
        (error: unknown) => ({ value: null, error }),
      );
      await waitForFile(secondPidPath, 1_000);
      secondPids = JSON.parse(await readFile(secondPidPath, "utf8"));
      const first = terminalOrphanFixtureWorker(firstPidPath, "valid-receipt")
        .run(validJob(), AbortSignal.timeout(2_000));

      await expect(first).resolves.toMatchObject({ jobId: "job-1" });
      firstPids = JSON.parse(await readFile(firstPidPath, "utf8"));
      expect(processExists(firstPids!.grandchild)).toBe(false);
      expect(processExists(secondPids!.worker)).toBe(true);
      expect(processExists(secondPids!.grandchild)).toBe(true);

      const secondResult = await secondSettled;
      if (secondResult.error !== null) throw secondResult.error;
      expect(secondResult.value).toMatchObject({ jobId: "job-1" });
      expect(processExists(secondPids!.grandchild)).toBe(false);
    } finally {
      for (const pid of [
        firstPids?.worker, firstPids?.grandchild, secondPids?.worker, secondPids?.grandchild,
      ]) {
        if (pid !== undefined && processExists(pid)) process.kill(pid, "SIGKILL");
      }
    }
  });

  it("kills a real transcription child before rejecting an invalid receipt", async () => {
    const fixtureRoot = await mkdtemp(join(mediaRoot, "transcription-invalid-"));
    const fixture = await realTranscriptionProcessGroupFixture(fixtureRoot, "invalid-receipt", 2_000);
    let childPid: number | undefined;
    try {
      const pending = fixture.worker.run(validJob(), AbortSignal.timeout(2_000));
      await waitForFile(fixture.pidPath, 1_000);
      const child = JSON.parse(await readFile(fixture.pidPath, "utf8"));
      childPid = child.pid;
      await expect(pending).rejects.toThrow(/^studio-model-worker-invalid-receipt$/);
      expect(child.pgid).toBe(child.parentPgid);
      expect(processExists(childPid!)).toBe(false);
    } finally {
      if (childPid !== undefined && processExists(childPid)) process.kill(childPid, "SIGKILL");
    }
  });

  it("kills a real transcription child when the Node worker times out", async () => {
    const fixtureRoot = await mkdtemp(join(mediaRoot, "transcription-timeout-"));
    const fixture = await realTranscriptionProcessGroupFixture(fixtureRoot, "hang", 300);
    let childPid: number | undefined;
    try {
      const pending = fixture.worker.run(validJob(), AbortSignal.timeout(2_000));
      await waitForFile(fixture.pidPath, 1_000);
      const child = JSON.parse(await readFile(fixture.pidPath, "utf8"));
      childPid = child.pid;
      await expect(pending).rejects.toThrow(/^studio-model-worker-timeout$/);
      expect(child.pgid).toBe(child.parentPgid);
      expect(processExists(childPid!)).toBe(false);
    } finally {
      if (childPid !== undefined && processExists(childPid)) process.kill(childPid, "SIGKILL");
    }
  });

  it("kills a real transcription child when the Node worker is aborted", async () => {
    const fixtureRoot = await mkdtemp(join(mediaRoot, "transcription-abort-"));
    const fixture = await realTranscriptionProcessGroupFixture(fixtureRoot, "hang", 5_000);
    const controller = new AbortController();
    let childPid: number | undefined;
    try {
      const pending = fixture.worker.run(validJob(), controller.signal);
      await waitForFile(fixture.pidPath, 1_000);
      const child = JSON.parse(await readFile(fixture.pidPath, "utf8"));
      childPid = child.pid;
      controller.abort();
      await expect(pending).rejects.toThrow(/^studio-model-worker-aborted$/);
      expect(child.pgid).toBe(child.parentPgid);
      expect(processExists(childPid!)).toBe(false);
    } finally {
      if (childPid !== undefined && processExists(childPid)) process.kill(childPid, "SIGKILL");
    }
  });

  it.each([
    "before-child-pid",
    "after-child-pid-assertion",
    "after-child-pid-timeout",
    "command-shape-mismatch",
    "directory-deleted",
    "parent-exited-first",
  ] as const)("fixture cleanup owns the transcription harness group after %s", async (stage) => {
    const result = await exerciseTranscriptionFixtureCleanup(stage);

    expect(result.cleanupError).toBeUndefined();
    expect(result.scenarioError).toBeInstanceOf(Error);
    expect(result.combinedError).toBe(result.scenarioError);
    if (stage === "after-child-pid-timeout") {
      expect((result.scenarioError as Error).message).toBe("fixture-ready-timeout");
    } else {
      expect((result.scenarioError as Error).name).toBe("AssertionError");
    }
    expect(result.fixtureRootSurvivedFixtureCleanup).toBe(false);
    expect(result.harnessSurvivedFixtureCleanup).toBe(false);
  }, 10_000);

  it("fails closed on a mismatched identity while the retained supervisor still cleans", async () => {
    const result = await exerciseTranscriptionFixtureCleanup("identity-mismatch");

    expect(result.scenarioError).toBeInstanceOf(Error);
    expect((result.scenarioError as Error).name).toBe("AssertionError");
    expect(result.cleanupError).toBeInstanceOf(Error);
    expect((result.cleanupError as Error).message).toBe(
      "fixture-transcription-harness-ownership-mismatch",
    );
    expect(result.combinedError).toBeInstanceOf(AggregateError);
    expect((result.combinedError as AggregateError).errors).toEqual([
      result.scenarioError,
      result.cleanupError,
    ]);
    expect(result.fixtureRootSurvivedFixtureCleanup).toBe(false);
    expect(result.harnessSurvivedFixtureCleanup).toBe(false);
  }, 10_000);

  it("kills a real transcription child when the Node parent receives SIGTERM", async () => {
    const fixtureRoot = await mkdtemp(join(tmpdir(), "studio-transcription-parent-signal-"));
    const fixture = await realTranscriptionProcessGroupFixture(fixtureRoot, "hang", 60_000);
    const supervisor = await spawnTranscriptionParentHarness(fixtureRoot, fixture.harnessPath);
    let harness: TranscriptionHarnessIdentity | undefined;
    let childPid: number | undefined;
    await runFixtureScenarioWithCleanup(
      async () => {
        await waitForFile(fixture.harnessPidPath, 5_000);
        harness = await readTranscriptionHarnessIdentity(
          fixture.harnessPidPath,
          fixture.harnessNonce,
          supervisor.identity.pid,
        );
        await waitForFile(fixture.pidPath, 5_000);
        const child = JSON.parse(await readFile(fixture.pidPath, "utf8"));
        childPid = child.pid;
        expect(child.pgid).toBe(child.parentPgid);
        supervisor.child.kill("SIGTERM");
        await waitForSpawnExit(supervisor.child, 2_000);
        await waitForProcessesToExit([childPid!], 2_000);
      },
      () => cleanupTranscriptionParentFixture({
        supervisor,
        fixtureRoot,
        harness,
        harnessNonce: fixture.harnessNonce,
        harnessPidPath: fixture.harnessPidPath,
      }),
    );
  }, 10_000);

  it("kills the child on timeout without returning partial output", async () => {
    await expect(makeFixtureWorker("hang").run(validJob(), AbortSignal.timeout(20)))
      .rejects.toThrow("studio-model-worker-timeout");
  });

  it("kills the whole child process group on timeout", async () => {
    const markerPath = join(mediaRoot, "grandchild-escaped.txt");
    const readyPath = join(mediaRoot, "grandchild-ready.txt");

    await expect(processGroupFixtureWorker(markerPath, readyPath).run(
      validJob(),
      AbortSignal.timeout(2_000),
    ))
      .rejects.toThrow("studio-model-worker-timeout");
    await expect(access(readyPath)).resolves.toBeUndefined();
    await new Promise((resolve) => setTimeout(resolve, 600));
    await expect(access(markerPath)).rejects.toThrow();
  });

  it("maps an explicit abort separately and kills the whole child process group", async () => {
    const markerPath = join(mediaRoot, "aborted-grandchild-escaped.txt");
    const readyPath = join(mediaRoot, "aborted-grandchild-ready.txt");
    const controller = new AbortController();
    const pending = processGroupFixtureWorker(markerPath, readyPath, 5_000)
      .run(validJob(), controller.signal);
    await waitForFile(readyPath, 1_000);
    controller.abort();

    await expect(pending).rejects.toThrow("studio-model-worker-aborted");
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    await expect(access(markerPath)).rejects.toThrow();
  });

  it("kills every active worker process group when the Node parent receives SIGTERM", async () => {
    const harnessRoot = await mkdtemp(join(tmpdir(), "studio-worker-parent-signal-"));
    const harnessPath = join(harnessRoot, "parent.ts");
    const harnessSource = join(harnessRoot, "source.mp4");
    const pidPath = join(harnessRoot, "children.json");
    const workerModule = pathToFileURL(join(
      process.cwd(),
      "packages/mobile-agent/src/studio-model-worker.ts",
    )).href;
    await writeFile(harnessSource, "synthetic parent signal media");
    const grandchild = "setInterval(() => {}, 1000);";
    const child = [
      "const { spawn } = require('node:child_process');",
      "const { writeFileSync } = require('node:fs');",
      `const grandchild = spawn(process.execPath, ['-e', ${JSON.stringify(grandchild)}], { stdio: 'ignore' });`,
      `writeFileSync(${JSON.stringify(pidPath)}, JSON.stringify({ worker: process.pid, grandchild: grandchild.pid }));`,
      "process.stdin.resume();",
      "setInterval(() => {}, 1000);",
    ].join("\n");
    await writeFile(harnessPath, [
      `import { LocalProcessStudioModelWorker } from ${JSON.stringify(workerModule)};`,
      `const root = ${JSON.stringify(harnessRoot)};`,
      "const worker = new LocalProcessStudioModelWorker({",
      "  command: process.execPath,",
      `  args: ['-e', ${JSON.stringify(child)}],`,
      "  cwd: root, timeoutMs: 60_000, maxStdoutBytes: 1024, maxStderrBytes: 1024,",
      "  allowedMediaRoot: root,",
      "});",
      "void worker.run({",
      "  schemaVersion: 1, jobId: 'parent-signal-job', customerId: 'customer-a',",
      "  sessionId: 'session-a', sourceAssetId: 'asset-a', sourceDigest: 'a'.repeat(64),",
      `  sourcePath: ${JSON.stringify(harnessSource)}, sourceStartMs: 0, sourceEndMs: 1000,`,
      "  layer: 'vision', operationKey: 'parent-signal-operation', requestedAt: '2026-09-27T00:00:00.000Z',",
      "}, new AbortController().signal).catch(() => {});",
      "setInterval(() => {}, 1000);",
      "",
    ].join("\n"));
    const parent = spawn(process.execPath, ["--import", "tsx", harnessPath], {
      cwd: process.cwd(),
      stdio: "ignore",
    });
    let childPids: { worker: number; grandchild: number } | undefined;
    try {
      await waitForFile(pidPath, 5_000);
      childPids = JSON.parse(await readFile(pidPath, "utf8"));
      expect(processExists(childPids!.worker)).toBe(true);
      expect(processExists(childPids!.grandchild)).toBe(true);
      parent.kill("SIGTERM");
      await new Promise<void>((resolveExit) => parent.once("exit", () => resolveExit()));
      await waitForProcessesToExit([childPids!.worker, childPids!.grandchild], 2_000);
    } finally {
      parent.kill("SIGKILL");
      await rm(harnessRoot, { recursive: true, force: true });
    }
  }, 10_000);

  it("rejects invalid jobs and source paths before spawning", async () => {
    const outsidePath = join(outsideRoot, "outside.mp4");
    const symlinkPath = join(mediaRoot, "customer-a", "linked-outside.mp4");
    await symlink(outsidePath, symlinkPath);
    const neverSpawn = fakeWorker({ command: join(mediaRoot, "missing-command") });

    await expect(neverSpawn.run(
      { ...validJob(), browserPath: sourcePath } as ModelAnalysisJobV1,
      AbortSignal.timeout(1_000),
    )).rejects.toThrow("studio-model-worker-invalid-job");
    for (const candidate of [outsidePath, symlinkPath]) {
      const rejection = neverSpawn.run(
        validJob({ sourcePath: candidate }),
        AbortSignal.timeout(1_000),
      );
      await expect(rejection).rejects.toThrow(/^studio-model-source-outside-allowed-root$/);
      await expect(rejection).rejects.not.toThrow(candidate);
    }
  });

  it("does not leak paths when the command cannot be spawned", async () => {
    const worker = fakeWorker({ command: join(mediaRoot, "missing-command") });
    const rejection = worker.run(validJob(), AbortSignal.timeout(1_000));

    await expect(rejection).rejects.toThrow(/^studio-model-worker-spawn-failed$/);
    await expect(rejection).rejects.not.toThrow(sourcePath);
  });
});
