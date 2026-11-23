import { mkdir, mkdtemp } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { runStudioBrowserPilot } from "./studio-browser-pilot";

const packageDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(packageDirectory, "../..");
const requestedDirectory = process.argv.slice(2).find((argument) => argument !== "--");
const outputDirectory = requestedDirectory
  ? resolve(requestedDirectory)
  : resolve(repositoryRoot, ".superpowers/evidence/private-content-studio-mvp");

await mkdir(outputDirectory, { recursive: true });
const runDirectory = await mkdtemp(join(outputDirectory, "browser-run-"));
const report = await runStudioBrowserPilot(runDirectory);
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
