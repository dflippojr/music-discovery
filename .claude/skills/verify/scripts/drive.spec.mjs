// The main path, one test per feature tag; verify.py --feature <name> greps a tag.
import { readFile } from "node:fs/promises";
import { test, expect, ready, pickSeed } from "../../../../tests/ui/fixtures.mjs";

const evidence = process.env.VERIFY_EVIDENCE ?? ".verify/manual";

async function snapshot(page, testInfo, feature) {
  await page.screenshot({ path: `${evidence}/screenshots/${feature}-${testInfo.project.name}.png` });
}

async function expectExplained(page) {
  const results = page.locator(".md-result");
  await expect(results).toHaveCount(10);
  for (const result of await results.all()) await expect(result.locator(".md-why li").first()).toBeVisible();
}

test("seed: search picks Synthetic track 100 and shows ten results", { tag: "@seed" }, async ({ page }, testInfo) => {
  await ready(page); await pickSeed(page);
  await expect(page.locator(".md-result")).toHaveCount(10);
  await snapshot(page, testInfo, "seed");
});

test("qualities: liking Calmer keeps ten results", { tag: "@qualities" }, async ({ page }, testInfo) => {
  await ready(page); await pickSeed(page);
  const calmer = page.getByRole("button", { name: /^Calmer/ });
  await calmer.click(); await expect(calmer).toHaveAttribute("data-state", "like");
  await expect(page.locator(".md-result")).toHaveCount(10);
  await snapshot(page, testInfo, "qualities");
});

test("rankers: baseline and hybrid each explain every result", { tag: "@rankers" }, async ({ page }, testInfo) => {
  await ready(page); await pickSeed(page);
  await page.getByRole("button", { name: /^Calmer/ }).click();
  for (const ranker of ["Baseline", "Hybrid"]) {
    const radio = page.getByRole("radio", { name: new RegExp("^" + ranker) });
    await radio.check(); await expect(radio).toBeChecked();
    await expectExplained(page);
    await snapshot(page, testInfo, "rankers-" + ranker.toLowerCase());
  }
});

test("ratings: rate one result and export ratings JSON", { tag: "@ratings" }, async ({ page }, testInfo) => {
  await ready(page); await pickSeed(page);
  await page.getByRole("button", { name: /^Calmer/ }).click();
  await page.getByRole("radio", { name: /^Baseline/ }).check();
  const up = page.getByRole("button", { name: /^Thumbs up/ }).first();
  await up.click(); await expect(up).toHaveAttribute("aria-pressed", "true");
  const trackId = Number((await page.locator(".md-title").first().textContent()).replace("Synthetic track ", ""));
  const event = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export my ratings" }).click();
  const body = JSON.parse(await readFile(await (await event).path(), "utf8"));
  expect(body).toEqual({ format: "music-discovery-ratings", version: 2, ratings: [{
    track_id: trackId, rating: 1, ranker: "baseline", seed_track_id: 100,
    likes: ["less:energy"], dislikes: [], exclude_seed_artist: false,
  }] });
  await snapshot(page, testInfo, "ratings");
});
