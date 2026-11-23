import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

import { runReferenceGuidedEditingPilot } from "./reference-guided-editing-pilot";

const requestedDirectory = process.argv.slice(2).find((argument) => argument !== "--");
const runDirectory = requestedDirectory
  ? resolve(requestedDirectory)
  : await mkdtemp(resolve(tmpdir(), "reference-guided-editing-pilot-"));
const report = await runReferenceGuidedEditingPilot(runDirectory);

// Deliberately print only the local run-record location. Exact media paths and
// input/output digests remain inside that mode-0600 record, never in CLI/API JSON.
process.stdout.write(`${resolve(report.runRecordPath)}\n`);
