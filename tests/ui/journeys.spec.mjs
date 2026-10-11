import { readFile } from "node:fs/promises";
import { test, expect, ready, pickSeed } from "./fixtures.mjs";

async function exportJson(page) {
  const event = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export my ratings" }).click();
  const download = await event;
  return JSON.parse(await readFile(await download.path(), "utf8"));
}

test("D1: search, keyboard selection, cancel, invalid input and random pick", async ({ page, isMobile }) => {
  await ready(page);
  const input = page.getByRole("combobox");
  await input.fill("not-a-synthetic-track");
  await expect(page.getByRole("option")).toHaveCount(0);
  await expect(page.locator(".md-result")).toHaveCount(0);
  await input.fill("Synthetic track 100"); await input.press("Escape");
  await expect(input).toHaveAttribute("aria-expanded", "false");
  await input.press("ArrowDown"); await input.press("Enter");
  await expect(page.locator(".md-result")).toHaveCount(10);
  await input.fill(""); await expect(page.locator(".md-result")).toHaveCount(0);
  await input.fill("Synthetic track 100");
  const option = page.getByRole("option", { name: "Synthetic track 100 — artist0", exact: true });
  if (isMobile) await option.tap(); else await option.click();
  await expect(input).toHaveValue("Synthetic track 100 — artist0");
  await input.press("Tab"); await expect(page.getByRole("button", { name: "Pick a random track" })).toBeFocused();
  await page.keyboard.press("Shift+Tab"); await expect(input).toBeFocused();
  await page.getByRole("button", { name: "Pick a random track" }).click();
  await expect(input).toHaveValue(/^Synthetic track /);
  await ready(page); await expect(input).toHaveValue("");
});

test("D2/D3: preferences, exclusion, rating changes and JSON export", async ({ page }) => {
  await ready(page); await pickSeed(page);
  const calmer = page.getByRole("button", { name: /^Calmer/ });
  for (const state of ["like", "dislike", "neutral"]) {
    await calmer.click(); await expect(calmer).toHaveAttribute("data-state", state);
  }
  await calmer.click();
  const exclude = page.getByRole("checkbox", { name: "Exclude the seed artist" });
  await exclude.check(); await page.getByRole("radio", { name: /^Baseline/ }).check();
  await expect(calmer).toHaveAttribute("data-state", "like"); await expect(exclude).toBeChecked();
  await expect(page.getByRole("combobox")).toHaveValue("Synthetic track 100 — artist0");
  expect((await page.locator(".md-meta").allTextContents()).every(text => !text.startsWith("artist0 ·"))).toBe(true);
  const up = page.getByRole("button", { name: /^Thumbs up/ }).first();
  const down = page.getByRole("button", { name: /^Thumbs down/ }).first();
  const exp = page.getByRole("button", { name: "Export my ratings" });
  await expect(exp).toBeDisabled();
  await up.click(); await expect(up).toHaveAttribute("aria-pressed", "true");
  await up.click(); await expect(exp).toBeDisabled();
  await up.click(); await down.click();
  await expect(up).toHaveAttribute("aria-pressed", "false"); await expect(down).toHaveAttribute("aria-pressed", "true");
  const resultId = Number((await page.locator(".md-title").first().textContent()).replace("Synthetic track ", ""));
  const body = await exportJson(page);
  expect(body).toEqual({ format: "music-discovery-ratings", version: 2, ratings: [{
    track_id: resultId, rating: -1, ranker: "baseline", seed_track_id: 100,
    likes: ["less:energy"], dislikes: [], exclude_seed_artist: true,
  }] });
  await page.getByRole("radio", { name: /^Hybrid/ }).check(); await page.getByRole("radio", { name: /^Baseline/ }).check();
  await expect(down).toHaveAttribute("aria-pressed", "true");
  await ready(page); await expect(exp).toBeDisabled(); await expect(exclude).not.toBeChecked();
  await expect(page.getByRole("radio", { name: /^Hybrid/ })).toBeChecked(); await expect(calmer).toHaveAttribute("data-state", "neutral");
});

test("S1/S2/S3: consent, validation, five blind tasks, export and reload reset", async ({ page }) => {
  await ready(page, "/study.html"); await page.getByRole("button", { name: "I agree, start" }).click();
  const show = page.getByRole("button", { name: "Show the two lists" });
  await expect(show).toBeDisabled(); await page.getByRole("combobox").fill("not-a-synthetic-track"); await expect(show).toBeDisabled();
  await pickSeed(page); await page.getByRole("button", { name: /^Calmer/ }).click(); await show.click();
  await expect(page.getByRole("heading", { name: "List A", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "List B", exact: true })).toBeVisible();
  await expect(page.locator(".md-result")).toHaveCount(10); await expect(page.locator(".md-why")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Next task" })).toBeDisabled();
  const up = page.getByRole("button", { name: /^Thumbs up/ }).first(); const down = page.getByRole("button", { name: /^Thumbs down/ }).first();
  await up.click(); await down.click(); await expect(up).toHaveAttribute("aria-pressed", "false"); await expect(down).toHaveAttribute("aria-pressed", "true");
  await down.click(); await expect(down).toHaveAttribute("aria-pressed", "false");
  await page.getByRole("checkbox", { name: /^New to me/ }).first().check();
  for (let number = 1; number <= 5; number += 1) {
    if (number > 1) {
      await expect(page.getByRole("heading", { name: "Task " + number + " of 5", exact: true })).toBeVisible();
      await expect(page.getByRole("combobox")).toHaveValue(""); await pickSeed(page); await show.click();
    }
    for (const button of await page.getByRole("button", { name: /^Thumbs up/ }).all()) await button.click();
    const next = page.getByRole("button", { name: number === 5 ? "Finish" : "Next task", exact: true });
    await expect(next).toBeDisabled(); await page.getByRole("radio", { name: "No preference", exact: true }).check();
    await expect(next).toBeEnabled(); await next.click();
  }
  await expect(page.getByRole("heading", { name: "All done, thank you" })).toBeVisible();
  const body = await exportJson(page);
  expect(body.session_id).toMatch(/^[a-f0-9]{32}$/); expect(body.tasks).toHaveLength(5);
  for (const task of body.tasks) {
    expect(task.seed_track_id).toBe(100); expect(task.preferred).toBe("none");
    expect(task.lists.map(list => list.label)).toEqual(["A", "B"]);
    expect(task.lists.map(list => list.ranker).sort()).toEqual(["baseline", "hybrid"]);
    for (const list of task.lists) {
      expect(list.results).toHaveLength(5); expect(list.results.every(result => result.rating === 1)).toBe(true);
    }
  }
  expect(body.tasks[0].lists[0].results[0].new_to_me).toBe(true); expect(body.tasks[0].likes).toEqual(["less:energy"]);
  await ready(page, "/study.html"); await expect(page.getByRole("button", { name: "I agree, start" })).toBeVisible();
});

for (const path of ["/", "/study.html"]) {
  test("E1: " + path + " shows catalog failure and recovers source links without losing ratings", async ({ page, baseURL, expectedFailures }) => {
    expectedFailures.add(baseURL + "/catalog.json"); expectedFailures.add(baseURL + "/sources.json");
    await page.route("**/catalog.json", route => route.fulfill({ status: 503, body: "" })); await page.goto(path);
    await expect(page.getByRole("alert")).toContainText("HTTP 503"); await expect(page.locator("[aria-busy]")).toHaveCount(0);
    await page.unroute("**/catalog.json"); await page.route("**/sources.json", route => route.fulfill({ status: 503, body: "" })); await page.reload();
    const retry = page.getByRole("button", { name: "Retry source links" }); await expect(retry).toBeEnabled();
    if (path !== "/") await page.getByRole("button", { name: "I agree, start" }).click();
    await pickSeed(page); if (path !== "/") await page.getByRole("button", { name: "Show the two lists" }).click();
    const up = page.getByRole("button", { name: /^Thumbs up/ }).first(); await up.click();
    await expect(page.getByRole("link", { name: "License", exact: true })).toHaveCount(10);
    await page.unroute("**/sources.json"); await retry.click();
    await expect(page.locator(".md-source-status")).toHaveText("Source links recovered.");
    await expect(retry).toBeHidden(); await expect(up).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("link", { name: "Source page (FMA)", exact: true })).toHaveCount(10); await ready(page, path);
  });
}

test("N1: local popups, deep link and browser Back", async ({ page, baseURL }) => {
  await ready(page); await pickSeed(page);
  for (const name of ["Source page (FMA)", "License"]) {
    const event = page.waitForEvent("popup"); await page.getByRole("link", { name, exact: true }).first().click();
    const popup = await event;
    try {
      await expect(popup.getByRole("heading", { name: "Synthetic local reference" })).toBeVisible(); expect(new URL(popup.url()).origin).toBe(baseURL);
    } finally { await popup.close(); }
  }
  await ready(page, "/study.html"); await expect(page.getByRole("button", { name: "I agree, start" })).toBeVisible();
  await page.goBack(); await expect(page.getByRole("heading", { name: "Music discovery demo", exact: true })).toBeVisible();
});
