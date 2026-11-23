import { lstat, mkdir, readdir, realpath } from "node:fs/promises";
import { isAbsolute, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

import {
  prepareRealMaterialAcceptance,
  renderRealMaterialAcceptance,
} from "./real-material-acceptance";

type Command = Readonly<{
  subcommand: "prepare" | "render";
  manifestPath: string;
  decisionPath: string | null;
  approvedRoots: readonly string[];
  runDirectory: string;
}>;

async function main(): Promise<void> {
  const command = parseCommand(process.argv.slice(2));
  if (command.subcommand === "prepare") {
    await requireEmptyRunDirectory(command.runDirectory);
    const result = await prepareRealMaterialAcceptance({
      manifestPath: command.manifestPath,
      approvedRoots: command.approvedRoots,
      runDirectory: command.runDirectory,
    });
    if (result.report.evidenceScope !== "local-real-media") {
      throw new Error("real-material-cli-synthetic-scope-forbidden");
    }
    process.stdout.write(`${result.reviewPacketPath}\n`);
    return;
  }

  await requirePreparedRunDirectory(command.runDirectory);
  const report = await renderRealMaterialAcceptance({
    manifestPath: command.manifestPath,
    decisionPath: command.decisionPath!,
    approvedRoots: command.approvedRoots,
    runDirectory: command.runDirectory,
  });
  if (report.evidenceScope !== "local-real-media") {
    throw new Error("real-material-cli-synthetic-scope-forbidden");
  }
  process.stdout.write(`${join(await realpath(command.runDirectory), "evidence", "public-report.json")}\n`);
}

export function parseCommand(args: readonly string[]): Command {
  const normalizedArgs = args[0] === "--" ? args.slice(1) : args;
  const [subcommand, ...flags] = normalizedArgs;
  if (subcommand !== "prepare" && subcommand !== "render") {
    throw new Error("real-material-cli-subcommand-required");
  }
  let manifestPath: string | null = null;
  let decisionPath: string | null = null;
  let runDirectory: string | null = null;
  const approvedRoots: string[] = [];
  for (let index = 0; index < flags.length; index += 2) {
    const flag = flags[index];
    const value = flags[index + 1];
    if (!flag?.startsWith("--") || value === undefined || value.startsWith("--")) {
      throw new Error("real-material-cli-flag-value-required");
    }
    if (!["--manifest", "--decision", "--approved-root", "--run-directory"].includes(flag)) {
      throw new Error(`real-material-cli-unknown-flag:${flag}`);
    }
    if (!isAbsolute(value)) throw new Error(`real-material-cli-absolute-path-required:${flag}`);
    const normalized = resolve(value);
    if (flag === "--manifest") {
      if (manifestPath !== null) throw new Error("real-material-cli-duplicate-manifest");
      manifestPath = normalized;
    } else if (flag === "--decision") {
      if (decisionPath !== null) throw new Error("real-material-cli-duplicate-decision");
      decisionPath = normalized;
    } else if (flag === "--run-directory") {
      if (runDirectory !== null) throw new Error("real-material-cli-duplicate-run-directory");
      runDirectory = normalized;
    } else {
      approvedRoots.push(normalized);
    }
  }
  if (!manifestPath || !runDirectory || approvedRoots.length === 0) {
    throw new Error("real-material-cli-required-argument-missing");
  }
  if (subcommand === "prepare" && decisionPath !== null) {
    throw new Error("real-material-cli-decision-not-allowed-on-prepare");
  }
  if (subcommand === "render" && decisionPath === null) {
    throw new Error("real-material-cli-decision-required-on-render");
  }
  return { subcommand, manifestPath, decisionPath, approvedRoots, runDirectory };
}

async function requireEmptyRunDirectory(path: string): Promise<void> {
  await mkdir(path, { recursive: true, mode: 0o700 });
  const metadata = await lstat(path);
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
    throw new Error("real-material-cli-run-directory-invalid");
  }
  if ((await readdir(path)).length > 0) throw new Error("real-material-cli-run-directory-not-empty");
}

async function requirePreparedRunDirectory(path: string): Promise<void> {
  const metadata = await lstat(join(path, "runtime", "real-material-acceptance-private.json"))
    .catch(() => null);
  if (!metadata?.isFile() || metadata.isSymbolicLink()) {
    throw new Error("real-material-cli-run-directory-unprepared");
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error: unknown) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
