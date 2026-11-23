import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

import { runExternalEditRoundtripPilot } from "./external-edit-roundtrip-pilot";

const requestedDirectory = process.argv.slice(2).find((argument) => argument !== "--");
const runDirectory = requestedDirectory
  ? resolve(requestedDirectory)
  : await mkdtemp(resolve(tmpdir(), "external-edit-roundtrip-pilot-"));
const report = await runExternalEditRoundtripPilot(runDirectory);
process.stdout.write(`${resolve(report.reportPath)}\n`);
