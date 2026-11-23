import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: [
    "employee-supervision.spec.ts",
    "owner-supervision.spec.ts",
  ],
  use: {
    baseURL: "http://127.0.0.1:3000",
  },
  webServer: {
    command: "SUPERVISION_LOCAL_SYNTHETIC_ACCEPTANCE=desensitized-only node_modules/.bin/next dev --hostname 127.0.0.1 --port 3000",
    url: "http://127.0.0.1:3000",
    reuseExistingServer: false,
  },
});
