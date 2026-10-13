import { defineConfig, devices } from "@playwright/test";
// Drives an instance verify.py already launched: no webServer, no teardown here.
const evidence = process.env.VERIFY_EVIDENCE ?? ".verify/manual";
export default defineConfig({
  testDir: ".", testMatch: "drive.spec.mjs", fullyParallel: false, workers: 1, retries: 0, timeout: 60_000,
  expect: { timeout: 5_000 }, outputDir: evidence + "/drive-results", reporter: [["list"]],
  use: { baseURL: process.env.VERIFY_BASE_URL ?? "http://127.0.0.1:4173", trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 }, colorScheme: "light" } },
    // Chromium: WebKit screenshots inject a style that the CSP refuses.
    { name: "phone", use: { ...devices["Pixel 7"], colorScheme: "light" } },
  ],
});
