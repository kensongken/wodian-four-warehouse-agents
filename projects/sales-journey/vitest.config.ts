import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";
import { realpathSync } from "node:fs";

export default defineConfig({
  esbuild: {
    jsx: "automatic"
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: [realpathSync(fileURLToPath(new URL(
      "./node_modules/@testing-library/jest-dom/dist/vitest.mjs",
      import.meta.url,
    )))],
    include: ["tests/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["tests/e2e/**"],
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url))
    }
  }
});
