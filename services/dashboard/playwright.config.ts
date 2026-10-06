import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.browser.ts",
  workers: 1,
  timeout: 30000,
  use: { ignoreHTTPSErrors: true },
  reporter: "list",
});
