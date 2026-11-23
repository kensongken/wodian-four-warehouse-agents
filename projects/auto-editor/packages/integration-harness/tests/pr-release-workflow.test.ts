import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "../../..");
const workflowPath = resolve(root, ".github/workflows/pr-release-gates.yml");
const packagePath = resolve(root, "package.json");
const workerCiRequirementsPath = resolve(root, "tools/media-model-worker/requirements-ci.txt");

describe("PR release gates workflow", () => {
  it("declares the root TypeScript launcher as a direct root dependency", async () => {
    const packageJson = JSON.parse(await readFile(packagePath, "utf8")) as {
      devDependencies?: Record<string, string>;
    };
    expect(packageJson.devDependencies?.tsx).toBeTruthy();
  });

  it("pins the declared local and CI runtime contract", async () => {
    const packageJson = JSON.parse(await readFile(packagePath, "utf8")) as {
      packageManager?: string;
      engines?: { node?: string };
      scripts?: Record<string, string>;
    };
    const value = await readFile(workflowPath, "utf8");
    expect(packageJson.packageManager).toBe("pnpm@11.19.0");
    expect(packageJson.engines?.node).toBe(">=24 <25");
    expect(packageJson.scripts?.["pr:contract-gate"]).toBe(
      "node scripts/run-pr-release-gate.mjs --mode contract",
    );
    expect(packageJson.scripts?.["pr:full-node-gate"]).toBe(
      "node scripts/run-pr-release-gate.mjs --mode full",
    );
    expect(value.match(/runs-on: ubuntu-latest/gu)).toHaveLength(2);
    expect(value.match(/node-version: 24\.x/gu)).toHaveLength(2);
    expect(value.match(/version: 11\.19\.0/gu)).toHaveLength(2);
    expect(value.match(/pnpm install --frozen-lockfile/gu)).toHaveLength(2);
  });

  it("uses approved triggers, read-only permissions, and stale-run cancellation", async () => {
    const value = await readFile(workflowPath, "utf8");
    expect(value).toContain("pull_request:");
    expect(value).toContain("branches: [stage-a]");
    expect(value).toContain("workflow_dispatch:");
    expect(value).not.toContain("pull_request_target");
    expect(value).toMatch(/permissions:\n  contents: read/u);
    expect(value).not.toMatch(/permissions:[\s\S]*?\bwrite\b/u);
    expect(value).toContain("cancel-in-progress: true");
    expect(value).not.toContain("secrets.");
    expect(value).not.toContain("upload-artifact");
  });

  it("defines exactly the two stable bounded jobs", async () => {
    const value = await readFile(workflowPath, "utf8");
    expect(value).toContain("  pr-contract-gate:");
    expect(value).toContain("  pr-full-node-gate:");
    expect(value.match(/^  pr-[a-z-]+:$/gmu)).toHaveLength(2);
    expect(value).toContain("timeout-minutes: 10");
    expect(value).toContain("timeout-minutes: 35");
    expect(value).toContain("pnpm pr:contract-gate");
    expect(value).toContain("pnpm pr:full-node-gate");
  });

  it("resolves a real PR or manual stage-a base before checking the diff", async () => {
    const value = await readFile(workflowPath, "utf8");
    expect(value).toContain("github.event.pull_request.base.sha");
    expect(value).toContain("git fetch --no-tags origin stage-a");
    expect(value).toContain("git rev-parse origin/stage-a");
    expect(value).toContain('git cat-file -e "$BASE_SHA^{commit}"');
    expect(value).toContain('git diff --check "$BASE_SHA"...HEAD');
  });

  it("checks out and verifies the exact PR head or manual-run SHA in both jobs", async () => {
    const value = await readFile(workflowPath, "utf8");
    const candidateExpression = "${{ github.event.pull_request.head.sha || github.sha }}";
    expect(value.split(`ref: ${candidateExpression}`)).toHaveLength(3);
    expect(value.split(`EXPECTED_SHA: ${candidateExpression}`)).toHaveLength(3);
    expect(value.match(/test "\$\(git rev-parse HEAD\)" = "\$EXPECTED_SHA"/gu)).toHaveLength(2);
  });

  it("fails closed when no supported browser exists", async () => {
    const value = await readFile(workflowPath, "utf8");
    expect(value).toContain("/usr/bin/google-chrome");
    expect(value).toContain("/usr/bin/chromium");
    expect(value).toContain("/usr/bin/chromium-browser");
    expect(value).toContain("studio-browser-executable-unavailable");
    expect(value).toContain("STUDIO_BROWSER_EXECUTABLE=");
    expect(value).not.toMatch(/continue-on-error:\s*true/u);
  });

  it("creates the lightweight Python worker shell required by Linux Node gates", async () => {
    const value = await readFile(workflowPath, "utf8");
    expect(value.match(/actions\/setup-python@v5/gu)).toHaveLength(1);
    expect(value).toContain('python-version: "3.11"');
    expect(value).toContain("python -m venv tools/media-model-worker/.venv");
    expect(value).toContain(
      "tools/media-model-worker/.venv/bin/python -m pip install --requirement " +
      "tools/media-model-worker/requirements-ci.txt",
    );
    expect(value).toContain(
      "tools/media-model-worker/.venv/bin/python -m pip install --no-deps scenedetect==0.7.1",
    );
    expect(value).toContain("STUDIO_TEST_PYTHON=");
    expect(value).toContain("tools/media-model-worker/.venv/bin/python");
  });

  it("pins only deterministic technical-analysis dependencies for Linux CI", async () => {
    const requirements = (await readFile(workerCiRequirementsPath, "utf8"))
      .trim().split(/\r?\n/u);
    expect(requirements).toEqual([
      "click==8.5.0",
      "numpy==2.2.6",
      "opencv-python-headless==4.12.0.88",
      "platformdirs==4.11.8",
      "tqdm==4.70.1",
    ]);
    expect(requirements.join("\n")).not.toMatch(/mlx|qwen|whisper|open.clip|torch/iu);
  });
});
