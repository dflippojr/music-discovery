import { readFile } from "node:fs/promises";
import { test, expect, ready, pickSeed, expectNoOverflow } from "./fixtures.mjs";

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

test("D4: score detail, feedback log across reload, JSONL download and clear", async ({ page, isMobile }) => {
  await ready(page); await pickSeed(page);
  const first = page.locator(".md-result").first();
  const detail = first.locator("details.md-score");
  await expect(page.locator(".md-score[open]")).toHaveCount(0);
  await expect(first.locator(".md-term").first()).toBeHidden();
  const summary = detail.locator("summary");
  if (isMobile) await summary.tap(); else { await summary.focus(); await page.keyboard.press("Enter"); }
  await expect(detail).toHaveAttribute("open", "");
  const title = await first.locator(".md-title").textContent();
  const terms = page.getByRole("list", { name: "Score terms for " + title });
  await expect(terms.getByText("Sounds like the seed", { exact: true })).toBeVisible();
  await expect(terms.locator(".md-term-value").first()).toHaveText(/^(\+|−)?\d+\.\d\d$/);
  const width = await page.evaluate(() => innerWidth);
  await page.evaluate(() => { document.styleSheets[0].insertRule(":root { font-size: 200%; }"); });
  await expectNoOverflow(page, width);
  await page.evaluate(() => { document.styleSheets[0].deleteRule(0); });

  const status = page.locator(".md-log-status");
  const download = page.getByRole("button", { name: "Download feedback log" });
  await expect(status).toHaveText("0 entries in this browser's feedback log."); await expect(download).toBeDisabled();
  await first.getByRole("button", { name: /^Thumbs up/ }).click();
  await page.locator(".md-result").nth(3).getByRole("button", { name: /^Thumbs down/ }).click();
  await expect(status).toHaveText("2 entries in this browser's feedback log.");
  const ids = (await page.locator(".md-title").allTextContents()).map(text => Number(text.replace("Synthetic track ", "")));

  await ready(page);
  await expect(status).toHaveText("2 entries in this browser's feedback log.");
  await expect(page.getByRole("button", { name: "Export my ratings" })).toBeDisabled();
  const event = page.waitForEvent("download"); await download.click();
  const file = await event;
  expect(file.suggestedFilename()).toBe("music-discovery-feedback.jsonl");
  const events = (await readFile(await file.path(), "utf8")).trimEnd().split("\n").map(line => JSON.parse(line));
  expect(events.map(e => [e.track_id, e.rank, e.rating, e.ranker, e.seed_track_ids])).toEqual([
    [ids[0], 1, 1, "hybrid", [100]], [ids[3], 4, -1, "hybrid", [100]],
  ]);
  expect(events[0].shown).toEqual(ids.map((track_id, i) => ({ track_id, rank: i + 1 })));
  expect(events.every(e => e.explanation_terms.length >= 1)).toBe(true);

  const clear = page.getByRole("button", { name: "Clear feedback log" });
  await clear.click(); await page.getByRole("button", { name: "Keep the log" }).click();
  await expect(clear).toBeFocused(); await expect(page.locator(".md-confirm")).toBeHidden(); await expect(download).toBeEnabled();
  await clear.click(); await page.getByRole("button", { name: "Yes, clear the log" }).click();
  await expect(status).toHaveText("Feedback log cleared."); await expect(download).toBeDisabled();
  await ready(page); await expect(status).toHaveText("0 entries in this browser's feedback log.");
});

test("S1/S2/S3: consent, validation, five blind tasks, export and reload reset", async ({ page }) => {
  await ready(page, "/study.html"); await page.getByRole("button", { name: "I agree, start" }).click();
  const show = page.getByRole("button", { name: "Show the two lists" });
  await expect(show).toBeDisabled(); await page.getByRole("combobox").fill("not-a-synthetic-track"); await expect(show).toBeDisabled();
  await pickSeed(page); await page.getByRole("button", { name: /^Calmer/ }).click(); await show.click();
  await expect(page.getByRole("heading", { name: "List A", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "List B", exact: true })).toBeVisible();
  await expect(page.locator(".md-result")).toHaveCount(10); await expect(page.locator(".md-why, .md-score")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Next task" })).toBeDisabled();
  const up = page.getByRole("button", { name: /^Thumbs up/ }).first(); const down = page.getByRole("button", { name: /^Thumbs down/ }).first();
  await up.click(); await down.click(); await expect(up).toHaveAttribute("aria-pressed", "false"); await expect(down).toHaveAttribute("aria-pressed", "true");
  await down.click(); await expect(down).toHaveAttribute("aria-pressed", "false");
  await page.getByRole("checkbox", { name: /^New to me/ }).first().check();
  await ready(page, "/study.html"); await page.getByRole("button", { name: "Resume my session" }).click();
  await expect(page.getByRole("heading", { name: "Task 1 of 5", exact: true })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: /^New to me/ }).first()).toBeChecked();
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
