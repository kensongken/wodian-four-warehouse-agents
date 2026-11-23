import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

it("provides a loopback-only Mini start command on port 3001", () => {
  const packageJsonPath = resolve(process.cwd(), "package.json");
  const packageJson = JSON.parse(readFileSync(packageJsonPath, "utf8")) as {
    scripts?: Record<string, string>;
  };

  expect(packageJson.scripts?.["dev:mini"]).toBe("next dev --hostname 127.0.0.1 --port 3001");
});
it("provides a production-only Mini start command on port 3001", () => {
  const packageJsonPath = resolve(process.cwd(), "package.json");
  const packageJson = JSON.parse(readFileSync(packageJsonPath, "utf8")) as {
    scripts?: Record<string, string>;
  };

  expect(packageJson.scripts?.["start:mini"]).toBe("next start --hostname 127.0.0.1 --port 3001");
});

it("keeps the Mini handoff smoke request on the current decision-card contract fields", () => {
  const handoffPath = resolve(process.cwd(), "MINI_NIGHT_HANDOFF.md");
  const handoff = readFileSync(handoffPath, "utf8");

  expect(handoff).toContain('"productDescription"');
  expect(handoff).toContain('"latestCustomerMessage"');
  expect(handoff).not.toContain('"customerMessage"');
  expect(handoff).not.toContain('"product"');
  expect(handoff).toContain('"tenantId":"demo-tenant"');
  expect(handoff).toContain('"operatorId":"demo-operator"');
});

it("tells a freshly cloned Mini workspace to install locked dependencies first", () => {
  const handoffPath = resolve(process.cwd(), "MINI_NIGHT_HANDOFF.md");
  const handoff = readFileSync(handoffPath, "utf8");

  expect(handoff).toContain("pnpm install --frozen-lockfile");
});

it("gives Mini a remote candidate clone command instead of a Mac-only path", () => {
  const handoffPath = resolve(process.cwd(), "MINI_NIGHT_HANDOFF.md");
  const handoff = readFileSync(handoffPath, "utf8");

  expect(handoff).toContain("https://github.com/stonemelinda795-oss/jewelry-sales-copilot.git");
  expect(handoff).not.toContain("/private/tmp/sales-ui-candidate-");
});

it("keeps browser-test artifacts separate from the production build", () => {
  const playwrightConfigPath = resolve(process.cwd(), "playwright.config.ts");
  const playwrightConfig = readFileSync(playwrightConfigPath, "utf8");
  expect(playwrightConfig).toContain("node_modules/.bin/next build && node_modules/.bin/next start");
});
