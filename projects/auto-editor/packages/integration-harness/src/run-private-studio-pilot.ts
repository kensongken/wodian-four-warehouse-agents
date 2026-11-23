import { mkdir, mkdtemp } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { runPrivateStudioPilot } from "./private-studio-pilot";

const packageDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(packageDirectory, "../..");
const requestedDirectory = process.argv.slice(2).find((argument) => argument !== "--");
const outputDirectory = requestedDirectory
  ? resolve(requestedDirectory)
  : resolve(repositoryRoot, ".superpowers/evidence/private-content-studio-mvp");

await mkdir(outputDirectory, { recursive: true });
const runDirectory = await mkdtemp(resolve(outputDirectory, "run-"));
const report = await runPrivateStudioPilot(runDirectory);
process.stdout.write(`${JSON.stringify({
  outputDirectory,
  runDirectory,
  reportPath: report.reportPath,
  sourceMapsPath: report.sourceMapsPath,
  ffprobePath: report.ffprobePath,
  negativeCasesPath: report.negativeCasesPath,
  draftCount: report.draftCount,
  syntheticOnly: report.syntheticOnly,
  automaticPublishing: report.automaticPublishing,
}, null, 2)}\n`);
