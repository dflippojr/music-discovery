import { test as base, expect } from "@playwright/test";
export const test = base.extend({
  expectedFailures: async ({}, use) => { await use(new Set()); },
  page: async ({ page, context, baseURL, expectedFailures }, use) => {
    const errors = [];
    page.on("pageerror", error => errors.push(String(error)));
    page.on("console", message => {
      if (message.type() === "error" && !expectedFailures.has(message.location().url)) errors.push(message.text());
    });
    page.on("requestfailed", request => {
      if (!expectedFailures.has(request.url())) errors.push(request.failure()?.errorText);
    });
    page.on("response", response => {
      if (response.status() >= 400 && !expectedFailures.has(response.url())) errors.push(response.url() + ": " + response.status());
    });
    await context.route("**/*", route => {
      const url = route.request().url();
      if (url.startsWith("blob:") || new URL(url).origin === baseURL) return route.continue();
      errors.push("Unexpected external request: " + url); return route.abort();
    });
    try { await use(page); }
    finally { expect(errors, "Unexpected console, page or request errors").toEqual([]); }
  },
});
export { expect };
export async function ready(page, path = "/") {
  await page.goto(path);
  await expect(page.locator(".md-source-status")).toHaveText("Source links loaded.");
  const scheme = await page.evaluate(() => matchMedia("(prefers-color-scheme: dark)").matches);
  await expect.poll(() => page.locator(".md-demo").evaluate(el => getComputedStyle(el).getPropertyValue("--md-bg").trim())).toBe(scheme ? "#14171a" : "#ffffff");
}
export async function pickSeed(page) {
  const input = page.getByRole("combobox", { name: "Search by title or artist" });
  await input.fill("Synthetic track 100");
  await expect(page.getByRole("option", { name: "Synthetic track 100 — artist0", exact: true })).toBeVisible();
  await input.press("Enter"); await expect(input).toHaveValue("Synthetic track 100 — artist0");
}
export async function showStudyLists(page) {
  await ready(page, "/study.html"); await page.getByRole("button", { name: "I agree, start" }).click();
  await pickSeed(page); await page.getByRole("button", { name: "Show the two lists" }).click();
  await expect(page.locator(".md-result")).toHaveCount(10);
}
export async function expectNoOverflow(page, width) {
  await expect.poll(() => page.evaluate(() => innerWidth)).toBe(width);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}
