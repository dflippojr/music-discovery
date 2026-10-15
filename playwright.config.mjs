import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "./tests/ui", fullyParallel: false, workers: 1, retries: 0, timeout: 60_000,
  expect: { timeout: 5_000 },
  reporter: [["list"], ["html", { open: "never" }], ["junit", { outputFile: "test-results/ui.xml" }]],
  globalTeardown: "./tests/ui/teardown.mjs",
  use: { baseURL: "http://127.0.0.1:4173", trace: "retain-on-failure", screenshot: "only-on-failure" },
  // Functional journeys do not depend on the colour scheme, so they run in the light
  // projects only; layout and accessibility specs run in all four.
  projects: [
    { name: "ui-desktop-light", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 }, colorScheme: "light" } },
    { name: "ui-desktop-dark", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 }, colorScheme: "dark" }, testIgnore: "**/journeys.spec.mjs" },
    { name: "ui-phone-light", use: { ...devices["iPhone SE"], colorScheme: "light" } },
    { name: "ui-phone-dark", use: { ...devices["iPhone SE"], colorScheme: "dark" }, testIgnore: "**/journeys.spec.mjs" },
  ],
  webServer: {
    command: "npm run ui:start", url: "http://127.0.0.1:4173/__ui_health",
    reuseExistingServer: false, timeout: 60_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
  },
});
