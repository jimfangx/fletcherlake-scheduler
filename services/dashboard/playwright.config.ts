import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.browser.ts",
  workers: 1,
  timeout: 30000,
  use: {
    ignoreHTTPSErrors: true,
    channel: process.env.FL_TEST_DASHBOARD_BROWSER_CHANNEL || undefined,
  },
  reporter: "list",
});
