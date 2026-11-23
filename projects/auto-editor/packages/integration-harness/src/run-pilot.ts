import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { runSyntheticEditorialPilot } from "./editorial-pilot";

const requestedDirectory = process.argv.slice(2).find((argument) => argument !== "--");
if (!requestedDirectory) {
  throw new Error("usage: pnpm pilot -- <output-directory>");
}
const outputDirectory = resolve(requestedDirectory);
await mkdir(outputDirectory, { recursive: true });
const report = await runSyntheticEditorialPilot(outputDirectory);
const reportPath = resolve(outputDirectory, "editorial-pilot-report.json");
await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
process.stdout.write(`${JSON.stringify({ reportPath, report }, null, 2)}\n`);
