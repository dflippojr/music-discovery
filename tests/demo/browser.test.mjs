// Browser checks for the standalone page, served from a local static server under the
// CSP dflippojr.dev uses: CSP, console errors, the toggle, axe-core in light and dark,
// 320 px layout, ratings and export. Needs `npm install` and `npx playwright install chromium`
// and a bundle built by `python scripts/build_demo_fixture.py <dir>` (DEMO_BUNDLE).
import assert from "node:assert/strict";
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";
import { after, before, describe, test } from "node:test";
import libCoverage from "istanbul-lib-coverage";
import libReport from "istanbul-lib-report";
import reports from "istanbul-reports";
import { chromium } from "playwright";
import v8ToIstanbul from "v8-to-istanbul";
import { FETCH_DEADLINE_MS } from "../../src/musicdiscovery/demo/ui.js";

const bundle = resolve(process.env.DEMO_BUNDLE ?? ".demo-fixture");
const axeSource = readFileSync(resolve("node_modules/axe-core/axe.min.js"), "utf8");
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json" };
const CSP = "default-src 'self'; script-src 'self'; style-src 'self'";

// Coverage of the demo and study scripts, merged over every test and written as lcov for
// SonarCloud when COVERAGE_DIR is set.
const sources = Object.fromEntries(["demo.js", "study.js", "study-lib.js", "feedback-log.js", "ui.js", "ranker.js"].map((name) => [name, resolve("src/musicdiscovery/demo", name)]));
const schema = JSON.parse(readFileSync(resolve("src/musicdiscovery/study-ratings.schema.json"), "utf8"));
const coverage = libCoverage.createCoverageMap({});

async function collect(entries) {
  for (const entry of entries) {
    const name = new URL(entry.url).pathname.slice(1);
    if (!sources[name]) continue;
    const converter = v8ToIstanbul(sources[name], 0, { source: entry.source });
    await converter.load();
    converter.applyCoverage(entry.functions);
    coverage.merge(converter.toIstanbul());
  }
}

let server;
let browser;
let base;

before(async () => {
  server = createServer((req, res) => {
    const name = req.url === "/" ? "index.html" : normalize(decodeURIComponent(req.url.slice(1))).replace(/^(\.\.[/\\])+/, "");
    if (name === "stalled-body.json") {
      res.writeHead(200, { "content-type": "application/json" });
      res.write("{"); // Headers arrive, but the synthetic JSON body never ends.
      return;
    }
    try {
      const body = readFileSync(join(bundle, name));
      res.writeHead(200, { "content-type": TYPES[extname(name)] ?? "application/octet-stream", "content-security-policy": CSP });
      res.end(body);
    } catch {
      res.writeHead(404);
      res.end();
    }
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  base = `http://127.0.0.1:${server.address().port}/`;
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
  server?.close();
  if (process.env.COVERAGE_DIR) {
    const context = libReport.createContext({ dir: process.env.COVERAGE_DIR, coverageMap: coverage });
    reports.create("lcovonly", { file: "lcov.info" }).execute(context);
  }
});

async function open(options = {}, { path = "", mount = "#music-discovery-demo input[role=combobox]" } = {}) {
  const context = await browser.newContext({ acceptDownloads: true, ...options });
  const page = await context.newPage();
  const problems = [];
  page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
  page.on("pageerror", (e) => problems.push(String(e)));
  page.on("requestfailed", (r) => problems.push(`request failed: ${r.url()}`));
  await page.coverage.startJSCoverage();
  await page.goto(base + path);
  await page.waitForSelector(mount);
  return { context, page, problems };
}

async function pickSeed(page, text = "Synthetic track 10") {
  const input = page.getByRole("combobox");
  await input.fill(text);
  await page.getByRole("option").first().waitFor();
  await input.press("Enter");
  await page.locator(".md-result").first().waitFor();
}

async function close(page, context) {
  await collect(await page.coverage.stopJSCoverage());
  await context.close();
}

// Only application deadlines use this clock; UI and Playwright timers run normally.
async function deadlineProbe(page, { target, stall, holdRetry = false } = {}) {
  const body = target ? JSON.parse(readFileSync(join(bundle, target), "utf8")) : null;
  await page.addInitScript(({ target, stall, holdRetry, body, deadline }) => {
    const nativeSetTimeout = globalThis.setTimeout;
    const nativeClearTimeout = globalThis.clearTimeout;
    const nativeFetch = globalThis.fetch;
    const timers = new Map();
    const attempts = [];
    let nextTimer = 0;
    const probe = globalThis.deadlineProbe = {
      timers, attempts, cleared: 0,
      expire() {
        for (const callback of [...timers.values()]) callback();
      },
      finish(index, fail = false, stale = false) {
        const attempt = attempts[index];
        if (fail) attempt.reject(new Error("Late failure"));
        else {
          const value = stale ? { sources: body.sources.map(() => "https://example.test/stale") } : body;
          attempt.resolve(attempt.stage === "body" ? value : { ok: true, json: async () => value });
        }
      },
    };
    globalThis.setTimeout = (callback, delay, ...args) => {
      if (delay !== deadline) return nativeSetTimeout(callback, delay, ...args);
      const id = `deadline-${++nextTimer}`;
      timers.set(id, callback);
      return id;
    };
    globalThis.clearTimeout = (id) => {
      if (timers.delete(id)) probe.cleared += 1;
      else nativeClearTimeout(id);
    };
    globalThis.fetch = async (url, options) => {
      if (!target || !String(url).endsWith(target)) return nativeFetch(url, options);
      const index = attempts.length;
      const attempt = { signal: options?.signal, aborted: 0, stage: "request" };
      attempts.push(attempt);
      attempt.signal?.addEventListener("abort", () => { attempt.aborted += 1; });
      if ((index === 0 && stall) || (index === 1 && holdRetry)) {
        const pending = new Promise((resolve, reject) => Object.assign(attempt, { resolve, reject }));
        if (index === 0 && stall === "body") {
          attempt.stage = "body";
          return { ok: true, json: () => pending };
        }
        // Deliberately ignore abort to exercise late success/rejection isolation.
        return pending;
      }
      return nativeFetch(url, options);
    };
  }, { target, stall, holdRetry, body, deadline: FETCH_DEADLINE_MS });
}

const titles = (page) => page.locator(".md-result .md-title").allTextContents();

async function axeViolations(page) {
  // Let colour transitions finish before measuring contrast.
  await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished)));
  await page.evaluate(axeSource);
  const result = await page.evaluate(() => globalThis.axe.run(document));
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
}

describe("demo page", () => {
  test("exclusion reranks from the keyboard and survives ranker and seed changes with qualities", async () => {
    const { context, page, problems } = await open();
    const checkbox = page.getByRole("checkbox", { name: "Exclude the seed artist" });
    assert.equal(await checkbox.isChecked(), false);
    await checkbox.check();
    assert.match(await page.locator(".md-status").textContent(), /Pick a seed track/);
    await checkbox.uncheck();
    await pickSeed(page, "Synthetic track 107");
    const chip = page.getByRole("button", { name: /^Calmer/ });
    await chip.click();
    const original = await titles(page);
    const catalog = JSON.parse(readFileSync(join(bundle, "catalog.json"), "utf8"));
    const checkArtists = async (seed) => {
      const rows = (await titles(page)).map((title) => catalog.titles.indexOf(title));
      const seedRow = catalog.ids.indexOf(seed);
      assert.equal(rows.length, 10);
      assert.ok(rows.every((row) => row !== seedRow && catalog.artist[row] !== catalog.artist[seedRow]));
      assert.equal(await checkbox.isChecked(), true);
      assert.match(await chip.textContent(), /liked/);
    };
    await checkbox.focus();
    await page.keyboard.press("Space");
    assert.notDeepEqual(await titles(page), original);
    await checkArtists(107);
    await page.getByRole("radio", { name: /Baseline/ }).check();
    await checkArtists(107);
    await pickSeed(page, "Synthetic track 110");
    await checkArtists(110);
    await page.getByRole("radio", { name: /Hybrid/ }).check();
    await checkArtists(110);
    await pickSeed(page, "Synthetic track 107");
    await checkbox.uncheck();
    assert.deepEqual(await titles(page), original);
    assert.match(await chip.textContent(), /liked/);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("exclusion ratings retain separate original contexts in version 2 exports", async () => {
    const { context, page, problems } = await open();
    await pickSeed(page);
    const original = await titles(page);
    const checkbox = page.getByRole("checkbox", { name: "Exclude the seed artist" });
    await checkbox.check();
    const common = (await titles(page)).find((title) => original.includes(title));
    assert.ok(common);
    const item = page.locator(".md-result").filter({ has: page.getByText(common, { exact: true }) });
    await item.getByRole("button", { name: /Thumbs down/ }).click();
    await checkbox.uncheck();
    const up = item.getByRole("button", { name: /Thumbs up/ });
    assert.equal(await item.getByRole("button", { name: /Thumbs down/ }).getAttribute("aria-pressed"), "false");
    await up.click();
    await checkbox.check();
    assert.equal(await item.getByRole("button", { name: /Thumbs down/ }).getAttribute("aria-pressed"), "true");
    await page.getByRole("radio", { name: /Baseline/ }).check();
    await pickSeed(page, "Synthetic track 11");
    const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export my ratings" }).click()]);
    const body = JSON.parse(readFileSync(await download.path(), "utf8"));
    assert.equal(body.version, 2);
    assert.equal(body.ratings.length, 2);
    assert.equal(body.ratings[0].track_id, body.ratings[1].track_id);
    assert.deepEqual(body.ratings.map((r) => [r.exclude_seed_artist, r.rating]), [[true, -1], [false, 1]]);
    for (const record of body.ratings) {
      assert.equal(record.ranker, "hybrid");
      assert.equal(record.seed_track_id, 100);
    }
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("all-one-artist catalogs announce zero results and keep controls usable", async () => {
    const oneArtist = JSON.parse(readFileSync(join(bundle, "exclusion.json"), "utf8")).one_artist.catalog;
    const { context, page, problems } = await open({}, { mount: "#music-discovery-demo" });
    await page.route("**/catalog.json", (route) => route.fulfill({ json: oneArtist }));
    await page.reload();
    await page.getByRole("checkbox", { name: "Exclude the seed artist" }).waitFor();
    await pickSeed(page);
    const original = await titles(page);
    const checkbox = page.getByRole("checkbox", { name: "Exclude the seed artist" });
    await checkbox.check();
    for (const ranker of ["Baseline", "Hybrid"]) {
      await page.getByRole("radio", { name: new RegExp(ranker) }).check();
      assert.deepEqual(await titles(page), []);
      assert.match(await page.locator(".md-status").textContent(), /No recommendations available/);
      assert.deepEqual(await axeViolations(page), []);
    }
    await page.getByRole("button", { name: /^Calmer/ }).click();
    assert.deepEqual(await titles(page), []);
    await checkbox.uncheck();
    assert.equal((await titles(page)).length, 10);
    await page.getByRole("button", { name: /^Calmer/ }).click();
    await page.getByRole("button", { name: /^Calmer/ }).click();
    assert.deepEqual(await titles(page), original);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("opens on the hybrid ranker and the toggle switches both ways without reloading", async () => {
    const { context, page, problems } = await open();
    await page.evaluate(() => {
      globalThis.marker = "same page";
    });
    assert.equal(await page.getByRole("radio", { name: /Hybrid/ }).isChecked(), true);
    await pickSeed(page);
    assert.equal((await titles(page)).length, 10);
    const hybrid = await titles(page);
    await page.getByRole("radio", { name: /Baseline/ }).check();
    assert.match(await page.locator(".md-status").textContent(), /baseline ranker/);
    const baseline = await titles(page);
    assert.equal(baseline.length, 10);
    await page.getByRole("radio", { name: /Hybrid/ }).check();
    assert.deepEqual(await titles(page), hybrid);
    assert.equal(await page.evaluate(() => globalThis.marker), "same page");
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("every result explains itself and links to its source and license", async () => {
    const { context, page } = await open();
    await pickSeed(page);
    const catalog = JSON.parse(readFileSync(join(bundle, "catalog.json"), "utf8"));
    const { sources } = JSON.parse(readFileSync(join(bundle, "sources.json"), "utf8"));
    assert.equal("sources" in catalog, false);
    for (const item of await page.locator(".md-result").all()) {
      assert.ok((await item.locator(".md-why li").count()) >= 1);
      const link = item.getByRole("link", { name: /Source page/ });
      await link.waitFor();
      const row = catalog.titles.indexOf(await item.locator(".md-title").textContent());
      assert.equal(await link.getAttribute("href"), sources[row]);
      assert.match(await item.getByRole("link", { name: "License" }).getAttribute("href"), /^https:/);
    }
    await close(page, context);
  });

  test("results show without the source link until sources.json arrives, then gain it", async () => {
    const context = await browser.newContext();
    const page = await context.newPage();
    let release;
    const gate = new Promise((done) => {
      release = done;
    });
    await page.route("**/sources.json", async (route) => {
      await gate;
      await route.continue();
    });
    await page.goto(base);
    await page.waitForSelector("#music-discovery-demo input[role=combobox]");
    await pickSeed(page);
    const items = page.locator(".md-result");
    assert.equal(await items.count(), 10);
    assert.equal(await page.getByRole("link", { name: /Source page/ }).count(), 0);
    assert.equal(await page.getByRole("link", { name: "License" }).count(), 10);
    assert.match(await page.locator(".md-source-status").textContent(), /Loading source links/);
    assert.equal(await page.locator(".md-source-status").getAttribute("role"), "status");
    release();
    await page.getByRole("link", { name: /Source page/ }).first().waitFor();
    assert.equal(await page.getByRole("link", { name: /Source page/ }).count(), 10);
    await context.close();
  });

  test("results still work if sources.json fails to load", async () => {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.route("**/sources.json", (route) => route.fulfill({ status: 404 }));
    await page.goto(base);
    await page.waitForSelector("#music-discovery-demo input[role=combobox]");
    await pickSeed(page);
    assert.equal(await page.getByRole("link", { name: "License" }).count(), 10);
    assert.equal(await page.getByRole("link", { name: /Source page/ }).count(), 0);
    await context.close();
  });

  test("chips cycle like, dislike, clear from the keyboard and change the results", async () => {
    const { context, page } = await open();
    await pickSeed(page);
    const before = await titles(page);
    const chip = page.getByRole("button", { name: /^Calmer/ });
    await chip.focus();
    await page.keyboard.press("Enter");
    assert.match(await chip.textContent(), /liked/);
    await page.keyboard.press("Enter");
    assert.match(await chip.textContent(), /disliked/);
    await page.keyboard.press("Enter");
    assert.match(await chip.textContent(), /not selected/);
    assert.deepEqual(await titles(page), before);
    await close(page, context);
  });

  for (const scheme of ["light", "dark"]) {
    test(`axe-core finds no violations in ${scheme} mode`, async () => {
      const { context, page, problems } = await open({ colorScheme: scheme });
      await pickSeed(page);
      await page.getByRole("checkbox", { name: "Exclude the seed artist" }).check();
      await page.getByRole("button", { name: /^Calmer/ }).click();
      const folk = page.locator(".md-chip").filter({ has: page.getByText("Folk", { exact: true }) });
      await folk.click();
      await folk.click();
      await page.getByRole("button", { name: /^Thumbs up/ }).first().click();
      await page.locator(".md-score summary").first().click();
      await page.getByRole("button", { name: "Clear feedback log" }).click();
      assert.deepEqual(await axeViolations(page), []);
      assert.deepEqual(problems, []);
      await close(page, context);
    });
  }

  test("ratings stay in the page and export as JSON", async () => {
    const { context, page } = await open();
    const requests = [];
    page.on("request", (r) => requests.push(r.url()));
    await pickSeed(page);
    const exportButton = page.getByRole("button", { name: "Export my ratings" });
    assert.equal(await exportButton.isDisabled(), true);
    const items = page.locator(".md-result");
    await items.nth(0).getByRole("button", { name: /Thumbs up/ }).click();
    await items.nth(1).getByRole("button", { name: /Thumbs down/ }).click();
    assert.equal(await items.nth(0).getByRole("button", { name: /Thumbs up/ }).getAttribute("aria-pressed"), "true");
    const [download] = await Promise.all([page.waitForEvent("download"), exportButton.click()]);
    const body = JSON.parse(readFileSync(await download.path(), "utf8"));
    assert.deepEqual(body.ratings.map((r) => r.rating).sort(), [-1, 1]);
    assert.equal(body.version, 2);
    assert.ok(body.ratings.every((r) => r.exclude_seed_artist === false));
    assert.equal(body.ratings[0].ranker, "hybrid");
    assert.ok(requests.every((url) => url.startsWith(base) || url.startsWith("blob:")), requests.join());
    await close(page, context);
  });

  test("each result can expand, from the keyboard, to show every score term with its signed contribution", async () => {
    const { context, page, problems } = await open();
    await pickSeed(page);
    await page.getByRole("button", { name: /^Calmer/ }).click();
    const details = page.locator(".md-result details.md-score");
    assert.equal(await details.count(), 10);
    assert.deepEqual(await details.evaluateAll((all) => all.filter((el) => el.open).length), 0);
    assert.equal(await page.locator(".md-term").first().isVisible(), false);
    const first = details.first();
    const title = await page.locator(".md-result .md-title").first().textContent();
    const summary = first.locator("summary");
    assert.equal(await summary.textContent(), `How this was scored: ${title}`);
    await summary.focus();
    await page.keyboard.press("Enter");
    assert.equal(await first.evaluate((el) => el.open), true);
    const list = page.getByRole("list", { name: `Score terms for ${title}` });
    const names = await list.locator(".md-term-name").allTextContents();
    for (const name of ["Sounds like the seed", "Shares genres and tags with the seed", "Popular with listeners", "Sound you picked: calmer"]) {
      assert.ok(names.includes(name), names.join());
    }
    const values = (await list.locator(".md-term-value").allTextContents()).map((text) => Number(text.replace("\u2212", "-")));
    assert.deepEqual(values, [...values].sort((a, b) => b - a));
    const total = Number((await first.locator(".md-help").textContent()).match(/^Score ([+\u22120-9.]+),/)[1].replace("\u2212", "-"));
    assert.ok(Math.abs(total - values.reduce((a, b) => a + b, 0)) <= 0.03 * values.length);
    const widths = await list.locator(".md-bar-fill").evaluateAll((fills) => fills.map((el) => el.getBoundingClientRect().width / el.parentElement.getBoundingClientRect().width));
    assert.ok(Math.abs(Math.max(...widths) - 0.5) < 0.01, widths.join());
    assert.equal(await list.locator(".md-bar").first().getAttribute("aria-hidden"), "true");
    await page.keyboard.press("Enter");
    assert.equal(await first.evaluate((el) => el.open), false);

    // Synthetic terms pin the labels, signs and bar directions exactly.
    const synthetic = await page.evaluate(() => {
      const app = document.querySelector("#music-discovery-demo").demo;
      const terms = [
        { name: "genre:Folk", value: -0.5 },
        { name: "audio", value: 1 },
        { name: "tag:punk", value: 0.001 },
        { name: "axis:energy+", value: 0.25 },
        { name: "axis:unknown-", value: 0.2 },
        { name: "variety", value: 0.1 },
        { name: "mystery", value: -0.0004 },
      ];
      const element = app.scoreDetail({ terms }, "Synthetic");
      document.body.append(element);
      element.open = true;
      const rows = [...element.querySelectorAll(".md-term")].map((row) => {
        const fill = row.querySelector(".md-bar-fill");
        const bar = fill.parentElement.getBoundingClientRect();
        const box = fill.getBoundingClientRect();
        const centre = bar.left + bar.width / 2;
        // Which edge of the fill sits on zero: on its left edge it grows right, on its right edge left.
        const edges = [Math.abs(box.left - centre) < 1, Math.abs(box.right - centre) < 1];
        return [row.querySelector(".md-term-name").textContent, row.querySelector(".md-term-value").textContent, fill.dataset.sign,
          Math.round((100 * box.width) / bar.width), edges];
      });
      element.remove();
      return { rows, help: element.querySelector(".md-help").textContent };
    });
    assert.deepEqual(synthetic.rows, [
      ["Sounds like the seed", "+1.00", "positive", 50, [true, false]],
      ["Sound you picked: more energetic", "+0.25", "positive", 13, [true, false]],
      ["Sound you picked: unknown-", "+0.20", "positive", 10, [true, false]],
      ["Adds variety to the list", "+0.10", "positive", 5, [true, false]],
      ["Tag you picked: punk", "0.00", "positive", 0, [true, true]],
      ["mystery", "0.00", "negative", 0, [true, true]],
      ["Genre you picked: Folk", "\u22120.50", "negative", 25, [false, true]],
    ]);
    assert.match(synthetic.help, /^Score \+1\.05, the sum of these terms\./);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("thumbs actions go to a local feedback log that survives reloads, downloads as JSON Lines and clears", async () => {
    const { context, page, problems } = await open();
    const requests = [];
    page.on("request", (r) => requests.push(r.url()));
    const download = page.getByRole("button", { name: "Download feedback log" });
    const clear = page.getByRole("button", { name: "Clear feedback log" });
    const status = page.locator(".md-log-status");
    assert.equal(await download.isDisabled(), true);
    assert.equal(await clear.isDisabled(), true);
    assert.equal(await status.textContent(), "0 entries in this browser's feedback log.");
    await pickSeed(page);
    const items = page.locator(".md-result");
    const shown = await titles(page);
    const why = await Promise.all([0, 2].map((i) => items.nth(i).locator(".md-why li").count()));
    await items.nth(0).getByRole("button", { name: /Thumbs up/ }).click();
    assert.equal(await status.textContent(), "1 entry in this browser's feedback log.");
    await items.nth(2).getByRole("button", { name: /Thumbs down/ }).click();
    await items.nth(2).getByRole("button", { name: /Thumbs down/ }).click();
    assert.equal(await status.textContent(), "3 entries in this browser's feedback log.");

    await page.reload();
    await page.waitForSelector("#music-discovery-demo input[role=combobox]");
    assert.equal(await status.textContent(), "3 entries in this browser's feedback log.");
    assert.equal(await page.getByRole("button", { name: "Export my ratings" }).isDisabled(), true);
    const [file] = await Promise.all([page.waitForEvent("download"), download.click()]);
    assert.equal(file.suggestedFilename(), "music-discovery-feedback.jsonl");
    const text = readFileSync(await file.path(), "utf8");
    assert.match(text, /^(\{.*\}\n){3}$/);
    const events = text.trimEnd().split("\n").map((line) => JSON.parse(line));
    const catalog = JSON.parse(readFileSync(join(bundle, "catalog.json"), "utf8"));
    const ids = shown.map((title) => catalog.ids[catalog.titles.indexOf(title)]);
    const logSchema = JSON.parse(readFileSync(resolve("src/musicdiscovery/feedback-log.schema.json"), "utf8"));
    assert.deepEqual(events.map((e) => [e.track_id, e.rank, e.rating, e.explanation_terms.length]), [
      [ids[0], 1, 1, why[0]], [ids[2], 3, -1, why[1]], [ids[2], 3, 0, why[1]],
    ]);
    for (const event of events) {
      assert.deepEqual(Object.keys(event).sort(), [...logSchema.required].sort());
      assert.deepEqual(event.shown, ids.map((track_id, i) => ({ track_id, rank: i + 1 })));
      assert.deepEqual([event.version, event.ranker, event.seed_track_ids, event.likes, event.dislikes, event.exclude_seed_artist], [1, "hybrid", [100], [], [], false]);
      assert.match(event.time, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
    }
    assert.match(await status.textContent(), /^Downloaded 3 entries as music-discovery-feedback\.jsonl\.$/);

    const confirm = page.locator(".md-confirm");
    assert.equal(await confirm.isHidden(), true);
    await clear.click();
    assert.equal(await confirm.isVisible(), true);
    assert.match(await confirm.textContent(), /Clear all 3 entries from this browser's feedback log\? This cannot be undone\./);
    const keep = page.getByRole("button", { name: "Keep the log" });
    assert.equal(await keep.evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press("Enter");
    assert.equal(await confirm.isHidden(), true);
    assert.equal(await clear.evaluate((el) => el === document.activeElement), true);
    assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem("music-discovery-feedback-log")).length), 3);
    await clear.click();
    await page.getByRole("button", { name: "Yes, clear the log" }).click();
    assert.equal(await status.textContent(), "Feedback log cleared.");
    assert.equal(await status.evaluate((el) => el === document.activeElement), true);
    assert.equal(await download.isDisabled(), true);
    await page.reload();
    await page.waitForSelector("#music-discovery-demo input[role=combobox]");
    assert.equal(await status.textContent(), "0 entries in this browser's feedback log.");
    assert.ok(requests.every((url) => url.startsWith(base) || url.startsWith("blob:")), requests.join());
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  for (const blocked of ["access", "writes"]) {
    test(`the feedback log keeps working for the page when storage ${blocked} are blocked`, async () => {
      const context = await browser.newContext({ acceptDownloads: true });
      const page = await context.newPage();
      const problems = [];
      page.on("pageerror", (e) => problems.push(String(e)));
      await page.addInitScript((blocked) => {
        const deny = () => {
          throw new DOMException("Synthetic denial", blocked === "access" ? "SecurityError" : "QuotaExceededError");
        };
        if (blocked === "access") Object.defineProperty(globalThis, "localStorage", { configurable: true, get: deny });
        else Storage.prototype.setItem = deny;
      }, blocked);
      await page.coverage.startJSCoverage();
      await page.goto(base);
      await page.waitForSelector("#music-discovery-demo input[role=combobox]");
      await pickSeed(page);
      await page.getByRole("button", { name: /^Thumbs up/ }).first().click();
      await page.getByRole("button", { name: /^Thumbs down/ }).nth(1).click();
      const status = page.locator(".md-log-status");
      assert.match(await status.textContent(), /^2 entries in the feedback log\. This browser is not letting the page save it/);
      const [file] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download feedback log" }).click()]);
      assert.equal(readFileSync(await file.path(), "utf8").trimEnd().split("\n").length, 2);
      await page.getByRole("button", { name: "Clear feedback log" }).click();
      await page.getByRole("button", { name: "Yes, clear the log" }).click();
      assert.equal(await page.getByRole("button", { name: "Download feedback log" }).isDisabled(), true);
      assert.deepEqual(problems, []);
      await close(page, context);
    });
  }

  test("reduced motion is respected", async () => {
    const { context, page } = await open({ reducedMotion: "reduce" });
    await pickSeed(page);
    const duration = await page.locator(".md-chip").first().evaluate((el) => getComputedStyle(el).transitionDuration);
    assert.match(duration, /^0s/);
    await close(page, context);
  });
});

// --- links to results ----------------------------------------------------------

const LINK = "seed={seed}&like=genre%3AFolk&dislike=more%3Aenergy&ranker=baseline&exclude=1";

function linkSeed() {
  const catalog = JSON.parse(readFileSync(join(bundle, "catalog.json"), "utf8"));
  return catalog.ids[catalog.titles.indexOf("Synthetic track 107")];
}

const folkChip = (page) => page.locator(".md-chip").filter({ has: page.getByText("Folk", { exact: true }) });
const linkParams = async (page) => new URLSearchParams((await page.evaluate(() => location.hash)).slice(1));
const IGNORED = "Some settings in this link are not available here and were ignored.";

describe("links to results", () => {
  test("a link restores the seed, qualities, ranker and exclusion, matching the same state set by hand", async () => {
    const seed = linkSeed();
    const { context, page, problems } = await open({}, { path: `#${LINK.replace("{seed}", seed)}` });
    await page.locator(".md-result").first().waitFor();
    assert.match(await page.getByRole("combobox").inputValue(), /^Synthetic track 107 /);
    assert.match(await folkChip(page).textContent(), /, liked$/);
    assert.match(await page.getByRole("button", { name: /^More energetic/ }).textContent(), /, disliked$/);
    assert.equal(await page.getByRole("radio", { name: /Baseline/ }).isChecked(), true);
    assert.equal(await page.getByRole("checkbox", { name: "Exclude the seed artist" }).isChecked(), true);
    assert.doesNotMatch(await page.locator(".md-status").textContent(), /ignored/);
    const linked = await titles(page);
    assert.equal(linked.length, 10);

    const manual = await open();
    await pickSeed(manual.page, "Synthetic track 107");
    await folkChip(manual.page).click();
    const energetic = manual.page.getByRole("button", { name: /^More energetic/ });
    await energetic.click();
    await energetic.click();
    await manual.page.getByRole("radio", { name: /Baseline/ }).check();
    await manual.page.getByRole("checkbox", { name: "Exclude the seed artist" }).check();
    assert.deepEqual(await titles(manual.page), linked);
    const params = await linkParams(manual.page);
    assert.deepEqual([...params.keys()], ["seed", "like", "dislike", "ranker", "exclude"]);
    assert.deepEqual([...params.values()], [String(seed), "genre:Folk", "more:energy", "baseline", "1"]);
    assert.deepEqual([...problems, ...manual.problems], []);
    await close(manual.page, manual.context);
    await close(page, context);
  });

  test("changes replace the fragment without adding history, a reload restores the list and clearing the seed clears it", async () => {
    const seed = linkSeed();
    const { context, page, problems } = await open();
    const copy = page.getByRole("button", { name: "Copy link to these results" });
    assert.equal(await page.evaluate(() => location.hash), "");
    assert.equal(await copy.isDisabled(), true);
    await pickSeed(page, "Synthetic track 107");
    assert.equal(await copy.isDisabled(), false);
    const length = await page.evaluate(() => history.length);
    assert.equal((await linkParams(page)).get("seed"), String(seed));
    assert.equal((await linkParams(page)).get("ranker"), "hybrid");
    await page.getByRole("button", { name: /^Calmer/ }).click();
    assert.deepEqual((await linkParams(page)).getAll("like"), ["less:energy"]);
    await page.getByRole("radio", { name: /Baseline/ }).check();
    assert.equal((await linkParams(page)).get("ranker"), "baseline");
    await page.getByRole("checkbox", { name: "Exclude the seed artist" }).check();
    assert.equal((await linkParams(page)).get("exclude"), "1");
    assert.equal(await page.evaluate(() => history.length), length);
    assert.equal(new URL(page.url()).pathname, "/");
    const before = await titles(page);
    await page.reload();
    await page.locator(".md-result").first().waitFor();
    assert.deepEqual(await titles(page), before);
    assert.match(await page.getByRole("button", { name: /^Calmer/ }).textContent(), /, liked$/);
    await page.getByRole("combobox").fill("Synthetic");
    assert.equal(await page.evaluate(() => location.hash), "");
    assert.equal(await copy.isDisabled(), true);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("unknown settings are ignored with one status sentence and the valid ones still apply", async () => {
    const seed = linkSeed();
    const bad = "seed=999999999&like=genre%3ANo%20such%20genre&like=genre%3AFolk&dislike=less%3Atempo&ranker=random&exclude=1";
    const { context, page, problems } = await open({}, { path: `#${bad}` });
    assert.equal(await page.locator(".md-status").textContent(), `Pick a seed track to see recommendations. ${IGNORED}`);
    assert.equal(await page.locator(".md-result").count(), 0);
    assert.equal(await page.getByRole("combobox").inputValue(), "");
    assert.match(await folkChip(page).textContent(), /, liked$/);
    assert.equal(await page.getByRole("radio", { name: /Hybrid/ }).isChecked(), true);
    assert.equal(await page.getByRole("checkbox", { name: "Exclude the seed artist" }).isChecked(), true);
    assert.equal(await page.evaluate(() => location.hash), `#${bad}`);

    const partial = await open({}, { path: `#seed=${seed}&like=tag%3Anope&ranker=hybrid` });
    await partial.page.locator(".md-result").first().waitFor();
    const status = await partial.page.locator(".md-status").textContent();
    assert.ok(status.startsWith("10 recommendations from the hybrid ranker for ") && status.endsWith(` ${IGNORED}`), status);
    assert.deepEqual([...(await linkParams(partial.page)).keys()], ["seed", "ranker"]);

    // An ordinary in-page anchor is not a link to results and is left alone.
    const anchor = await open({}, { path: "#music-discovery-demo" });
    assert.equal(await anchor.page.locator(".md-status").textContent(), "Pick a seed track to see recommendations.");
    assert.equal(await anchor.page.evaluate(() => location.hash), "#music-discovery-demo");
    assert.deepEqual([...problems, ...partial.problems, ...anchor.problems], []);
    await close(partial.page, partial.context);
    await close(anchor.page, anchor.context);
    await close(page, context);
  });

  test("Copy link puts the address on the clipboard, or shows it in a selected field when that fails", async () => {
    const { context, page, problems } = await open({ permissions: ["clipboard-read", "clipboard-write"] });
    await pickSeed(page);
    const copy = page.getByRole("button", { name: "Copy link to these results" });
    await copy.click();
    await page.waitForFunction(() => document.querySelector(".md-status").textContent === "Link copied.");
    const href = await page.evaluate(() => location.href);
    assert.match(href, /#seed=/);
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), href);
    const field = page.getByLabel("Link to these results");
    assert.equal(await field.isVisible(), false);

    await page.evaluate(() => {
      navigator.clipboard.writeText = () => Promise.reject(new DOMException("Denied", "NotAllowedError"));
    });
    await copy.click();
    await field.waitFor();
    assert.equal(await field.inputValue(), href);
    assert.equal(await field.getAttribute("readonly"), "");
    assert.deepEqual(await field.evaluate((el) => [el.selectionStart, el.selectionEnd, document.activeElement === el]), [0, href.length, true]);
    assert.match(await page.locator(".md-status").textContent(), /could not be copied/);
    assert.deepEqual(await axeViolations(page), []);
    await page.setViewportSize({ width: 320, height: 700 });
    const widths = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
    assert.ok(widths[0] <= widths[1], `scrollWidth ${widths[0]} > ${widths[1]}`);
    assert.ok((await copy.boundingBox()).height >= 44);
    await page.getByRole("button", { name: /^Calmer/ }).click();
    assert.equal(await field.isVisible(), false);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("with several demos on a page none reads or writes the fragment", async () => {
    const hash = `#${LINK.replace("{seed}", linkSeed())}`;
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.coverage.startJSCoverage();
    await page.route(base, (route) => {
      const html = readFileSync(join(bundle, "index.html"), "utf8");
      return route.fulfill({ contentType: "text/html", headers: { "content-security-policy": CSP },
        body: html.replace("</main>", '<div id="second-demo" data-music-discovery></div></main>') });
    });
    await page.goto(base + hash);
    await page.locator("#second-demo input[role=combobox]").waitFor();
    const first = page.locator("#music-discovery-demo");
    assert.equal(await first.getByRole("combobox").inputValue(), "");
    assert.equal(await page.getByRole("button", { name: "Copy link to these results" }).count(), 0);
    await first.getByRole("button", { name: "Pick a random track" }).click();
    await first.locator(".md-result").first().waitFor();
    assert.equal(await page.evaluate(() => location.hash), hash);
    await close(page, context);
  });
});

// --- study mode ----------------------------------------------------------------

const study = (options) => open(options, { path: "study.html", mount: "#music-discovery-study button" });

/** Pick a random seed, show the lists, rate every track and choose a preference. */
async function doTask(page, { preference = "List A", last = false } = {}) {
  await page.getByRole("button", { name: "Pick a random track" }).click();
  await page.getByRole("button", { name: "Show the two lists" }).click();
  await page.locator(".md-result").first().waitFor();
  const items = page.locator(".md-result");
  const count = await items.count();
  for (let i = 0; i < count; i += 1) {
    await items.nth(i).getByRole("button", { name: i % 3 ? /Thumbs up/ : /Thumbs down/ }).click();
    if (i % 2 === 0) await items.nth(i).getByLabel(/New to me/).check();
  }
  await page.getByRole("radio", { name: preference }).check();
  await page.getByRole("button", { name: last ? "Finish" : "Next task" }).click();
}

/**
 * Close the page as a user would, running its `beforeunload` handlers. Returns the type of
 * the leave-page prompt it showed (accepting it), or null when it closed without one.
 */
async function leave(page) {
  await collect(await page.coverage.stopJSCoverage());
  const outcome = Promise.race([
    page.waitForEvent("dialog").then(async (dialog) => {
      await dialog.accept();
      return dialog.type();
    }),
    page.waitForEvent("close").then(() => null),
  ]);
  await page.close({ runBeforeUnload: true });
  return outcome;
}

describe("study page", () => {
  test("ignores a link fragment and leaves it unchanged", async () => {
    const hash = `#${LINK.replace("{seed}", linkSeed())}`;
    const { context, page, problems } = await open({}, { path: `study.html${hash}`, mount: "#music-discovery-study button" });
    await page.getByRole("button", { name: "I agree, start" }).click();
    assert.equal(await page.getByRole("combobox").inputValue(), "");
    await page.getByRole("button", { name: "Pick a random track" }).click();
    await page.getByRole("button", { name: "Show the two lists" }).click();
    await page.locator(".md-result").first().waitFor();
    assert.equal(await page.evaluate(() => location.hash), hash);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("shows two unlabelled lists and never names a ranker", async () => {
    const { context, page, problems } = await study();
    assert.match(await page.locator("#music-discovery-study").textContent(), /By sending the file you agree/);
    assert.equal(await page.getByRole("checkbox", { name: "Exclude the seed artist" }).count(), 0);
    await page.getByRole("button", { name: "I agree, start" }).click();
    await page.getByRole("button", { name: "Pick a random track" }).click();
    await page.getByRole("button", { name: "Show the two lists" }).click();
    await page.locator(".md-result").first().waitFor();
    assert.deepEqual(await page.locator("h3.md-study-list").allTextContents(), ["List A", "List B"]);
    assert.equal(await page.locator(".md-result").count(), 10);
    assert.equal(await page.locator(".md-why").count(), 0);
    assert.equal(await page.locator(".md-score, .md-terms").count(), 0);
    assert.equal(await page.getByRole("button", { name: /feedback log/ }).count(), 0);
    const markup = (await page.content()).toLowerCase();
    assert.doesNotMatch(markup, /hybrid|baseline/);
    assert.equal(await page.getByRole("button", { name: "Next task" }).isDisabled(), true);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("a full session exports one valid file with a balanced order and no personal fields", async () => {
    const { context, page, problems } = await study();
    const requests = [];
    page.on("request", (r) => requests.push(r.url()));
    await page.getByRole("button", { name: "I agree, start" }).click();
    const preferences = ["List A", "List B", "No preference", "List A", "List B"];
    for (let i = 0; i < 5; i += 1) {
      assert.equal(await page.getByRole("heading", { name: `Task ${i + 1} of 5` }).count(), 1);
      await doTask(page, { preference: preferences[i], last: i === 4 });
    }
    const exportButton = page.getByRole("button", { name: "Export my ratings" });
    const [download] = await Promise.all([page.waitForEvent("download"), exportButton.click()]);
    const file = await download.path();
    if (process.env.STUDY_EXPORT_DIR) {
      // CI feeds this synthetic session to `musicdiscovery analyze-study`.
      mkdirSync(process.env.STUDY_EXPORT_DIR, { recursive: true });
      copyFileSync(file, resolve(process.env.STUDY_EXPORT_DIR, "synthetic-session.json"));
    }
    const body = JSON.parse(readFileSync(file, "utf8"));
    assert.deepEqual(Object.keys(body).sort(), Object.keys(schema.properties).sort());
    assert.match(body.session_id, /^[0-9a-f]{32}$/);
    assert.equal(body.tasks.length, 5);
    const hybridFirst = body.tasks.filter((t) => t.lists[0].ranker === "hybrid").length;
    assert.ok(hybridFirst === 2 || hybridFirst === 3, `hybrid first in ${hybridFirst} of 5 tasks`);
    const taskKeys = Object.keys(schema.$defs.task.properties).sort();
    const expected = ["A", "B", "none", "A", "B"];
    body.tasks.forEach((task, i) => {
      assert.deepEqual(Object.keys(task).sort(), taskKeys);
      assert.equal(task.task, i + 1);
      assert.deepEqual(task.lists.map((l) => l.label), ["A", "B"]);
      assert.deepEqual(task.lists.map((l) => l.ranker).sort(), ["baseline", "hybrid"]);
      assert.equal(task.preferred, expected[i]);
      for (const list of task.lists) {
        assert.equal(list.results.length, 5);
        for (const result of list.results) {
          assert.deepEqual(Object.keys(result).sort(), ["new_to_me", "rating", "track_id"]);
          assert.ok(result.rating === 1 || result.rating === -1);
        }
      }
    });
    assert.ok(requests.every((url) => url.startsWith(base) || url.startsWith("blob:")), requests.join());
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("a task cannot finish before every track is rated and a list is chosen", async () => {
    const { context, page } = await study();
    await page.getByRole("button", { name: "I agree, start" }).click();
    assert.equal(await page.getByRole("button", { name: "Show the two lists" }).isDisabled(), true);
    await page.getByRole("button", { name: "Pick a random track" }).click();
    await page.getByRole("button", { name: "Show the two lists" }).click();
    await page.getByRole("radio", { name: "List B" }).check();
    assert.match(await page.locator(".md-status").textContent(), /10 tracks still need/);
    const first = page.locator(".md-result").first().getByRole("button", { name: /Thumbs up/ });
    await first.click();
    assert.match(await page.locator(".md-status").textContent(), /9 tracks still need/);
    await first.click(); // clears it again
    assert.match(await page.locator(".md-status").textContent(), /10 tracks still need/);
    assert.equal(await page.getByRole("button", { name: "Next task" }).isDisabled(), true);
    await close(page, context);
  });

  test("warns before leaving once a track is rated, and not before", async () => {
    for (const rate of [false, true]) {
      const { context, page, problems } = await study();
      await page.getByRole("button", { name: "I agree, start" }).click();
      await page.getByRole("button", { name: "Pick a random track" }).click();
      await page.getByRole("button", { name: "Show the two lists" }).click();
      if (rate) await page.locator(".md-result").first().getByRole("button", { name: /Thumbs up/ }).click();
      assert.deepEqual(problems, []);
      assert.equal(await leave(page), rate ? "beforeunload" : null);
      await context.close();
    }
  });

  test("a reloaded session resumes where it was and exports as if it never reloaded", async () => {
    const { context, page, problems } = await study();
    page.on("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "I agree, start" }).click();
    await doTask(page, { preference: "List B" });
    await page.getByRole("button", { name: "Pick a random track" }).click();
    await page.getByRole("button", { name: "Show the two lists" }).click();
    const items = page.locator(".md-result");
    await items.nth(0).getByRole("button", { name: /Thumbs up/ }).click();
    await items.nth(1).getByRole("button", { name: /Thumbs down/ }).click();
    await items.nth(6).getByRole("button", { name: /Thumbs up/ }).click();
    await items.nth(1).getByLabel(/New to me/).check();
    await page.getByRole("radio", { name: "No preference" }).check();
    const shown = () =>
      page.evaluate(() => ({
        lead: document.querySelector("#music-discovery-study .md-lead").textContent,
        tracks: [...document.querySelectorAll(".md-result .md-title, .md-result .md-meta")].map((e) => e.textContent),
        pressed: [...document.querySelectorAll(".md-rate")].map((b) => b.getAttribute("aria-pressed")),
        fresh: [...document.querySelectorAll(".md-new input")].map((c) => c.checked),
        preferred: document.querySelector("input[type=radio]:checked")?.value ?? null,
      }));
    const state = () => page.evaluate(() => {
      const s = document.querySelector("#music-discovery-study").study;
      return { id: s.session.id, startedAt: s.session.startedAt, orders: s.orders, first: s.session.tasks[0] };
    });
    const before = await shown();
    const session = await state();
    assert.equal(before.pressed.filter((p) => p === "true").length, 3);

    await page.reload();
    await page.waitForSelector("#music-discovery-study button");
    await page.getByRole("button", { name: "Resume my session" }).click();
    assert.equal(await page.getByRole("heading", { name: "Task 2 of 5" }).count(), 1);
    assert.deepEqual(await shown(), before);
    assert.deepEqual(await state(), session);

    // Finish task 2 from where it stopped, then the rest of the session.
    for (let i = 0; i < 10; i += 1) {
      if (before.pressed[2 * i] === "false" && before.pressed[2 * i + 1] === "false") await items.nth(i).getByRole("button", { name: /Thumbs down/ }).click();
    }
    await page.getByRole("button", { name: "Next task" }).click();
    for (let i = 2; i < 5; i += 1) await doTask(page, { last: i === 4 });
    const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export my ratings" }).click()]);
    const body = JSON.parse(readFileSync(await download.path(), "utf8"));
    assert.deepEqual(Object.keys(body).sort(), Object.keys(schema.properties).sort());
    assert.equal(body.session_id, session.id);
    assert.equal(body.started_at, session.startedAt);
    assert.deepEqual(body.tasks.map((t) => t.lists.map((l) => l.ranker)), session.orders);
    assert.equal(body.tasks[0].started_at, session.first.startedAt);
    assert.equal(body.tasks[0].completed_at, session.first.completedAt);
    assert.deepEqual(body.tasks.map((t) => t.preferred), ["B", "none", "A", "A", "A"]);
    const second = body.tasks[1].lists.flatMap((l) => l.results);
    assert.deepEqual(second.slice(0, 2).map((r) => r.rating), [1, -1]);
    assert.equal(second[6].rating, 1);
    assert.deepEqual(second.map((r) => r.new_to_me), before.fresh);
    for (const task of body.tasks) for (const list of task.lists) for (const r of list.results) assert.ok(r.rating === 1 || r.rating === -1);

    // Exporting clears the stored session and the warning.
    await page.reload();
    await page.waitForSelector("#music-discovery-study button");
    assert.equal(await page.getByRole("button", { name: "Resume my session" }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "I agree, start" }).count(), 1);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("no warning after the ratings are exported", async () => {
    const { context, page, problems } = await study();
    await page.getByRole("button", { name: "I agree, start" }).click();
    for (let i = 0; i < 5; i += 1) await doTask(page, { last: i === 4 });
    await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export my ratings" }).click()]);
    assert.deepEqual(problems, []);
    assert.equal(await leave(page), null);
    await context.close();
  });

  test("declining to resume starts a fresh session and forgets the old one", async () => {
    const { context, page, problems } = await study();
    page.on("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "I agree, start" }).click();
    await page.getByRole("button", { name: "Pick a random track" }).click();
    await page.getByRole("button", { name: "Show the two lists" }).click();
    await page.locator(".md-result").first().getByRole("button", { name: /Thumbs up/ }).click();
    const id = () => page.evaluate(() => document.querySelector("#music-discovery-study").study.session.id);
    const old = await id();
    await page.reload();
    await page.waitForSelector("#music-discovery-study button");
    assert.match(await page.locator(".md-lead").textContent(), /0 of 5 tasks done/);
    assert.deepEqual(await axeViolations(page), []);
    await page.getByRole("button", { name: "Start over" }).click();
    assert.equal(await page.getByRole("button", { name: "I agree, start" }).count(), 1);
    assert.notEqual(await id(), old);
    await page.reload();
    await page.waitForSelector("#music-discovery-study button");
    assert.equal(await page.getByRole("button", { name: "Resume my session" }).count(), 0);
    await page.getByRole("button", { name: "I agree, start" }).click();
    assert.equal(await page.getByRole("heading", { name: "Task 1 of 5" }).count(), 1);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("a session resumes between tasks and on the last screen", async () => {
    const { context, page, problems } = await study();
    page.on("dialog", (d) => d.accept());
    const reloadAndResume = async () => {
      await page.reload();
      await page.waitForSelector("#music-discovery-study button");
      await page.getByRole("button", { name: "Resume my session" }).click();
    };
    await page.getByRole("button", { name: "I agree, start" }).click();
    const id = await page.evaluate(() => document.querySelector("#music-discovery-study").study.session.id);
    await doTask(page);
    await reloadAndResume();
    assert.equal(await page.getByRole("heading", { name: "Task 2 of 5" }).count(), 1);
    assert.equal(await page.getByRole("button", { name: "Show the two lists" }).isDisabled(), true);
    for (let i = 1; i < 5; i += 1) await doTask(page, { last: i === 4 });
    await reloadAndResume();
    assert.equal(await page.getByRole("heading", { name: "All done, thank you" }).count(), 1);
    const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export my ratings" }).click()]);
    const body = JSON.parse(readFileSync(await download.path(), "utf8"));
    assert.equal(body.session_id, id);
    assert.equal(body.tasks.length, 5);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  test("a stored session that does not fit this page or catalog is not offered", async () => {
    const { context, page, problems } = await study();
    page.on("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "I agree, start" }).click();
    await doTask(page);
    await page.getByRole("button", { name: "Pick a random track" }).click();
    await page.getByRole("button", { name: "Show the two lists" }).click();
    const key = "music-discovery-study-session";
    const good = JSON.parse(await page.evaluate((key) => sessionStorage.getItem(key), key));
    const damaged = {
      "not JSON": "{",
      "another version": { ...good, version: 2 },
      "too few orders": { ...good, orders: good.orders.slice(1) },
      "a bad session id": { ...good, session: { ...good.session, id: "x" } },
      "an unfinished earlier task": { ...good, session: { ...good.session, tasks: [{ ...good.session.tasks[0], preferred: null }] } },
      "a swapped list order": { ...good, orders: good.orders.map((o) => [...o].reverse()) },
      "a track from another catalog": { ...good, current: { ...good.current, task: { ...good.current.task, lists: good.current.task.lists.map((l) => ({ ...l, results: l.results.map((r) => ({ ...r, trackId: -1 })) })) } } },
      "a different seed row": { ...good, current: { ...good.current, seedRow: -1 } },
      "no task start time": { ...good, current: { ...good.current, startedAt: undefined } },
      "a task after the last": { ...good, session: { ...good.session, tasks: Array(5).fill(good.session.tasks[0]) } },
    };
    for (const [name, value] of Object.entries(damaged)) {
      await page.evaluate(([key, text]) => sessionStorage.setItem(key, text), [key, typeof value === "string" ? value : JSON.stringify(value)]);
      await page.reload();
      await page.waitForSelector("#music-discovery-study button");
      assert.equal(await page.getByRole("button", { name: "Resume my session" }).count(), 0, name);
      assert.equal(await page.getByRole("button", { name: "I agree, start" }).count(), 1, name);
    }
    await page.evaluate(([key, text]) => sessionStorage.setItem(key, text), [key, JSON.stringify(good)]);
    await page.reload();
    await page.waitForSelector("#music-discovery-study button");
    assert.equal(await page.getByRole("button", { name: "Resume my session" }).count(), 1);
    assert.deepEqual(problems, []);
    await close(page, context);
  });

  // Blocked site data makes reading `sessionStorage` throw; a full quota makes writes throw.
  for (const [failure, script] of [
    ["blocked", () => Object.defineProperty(window, "sessionStorage", { get() { throw new DOMException("blocked", "SecurityError"); } })],
    ["full", () => { Storage.prototype.setItem = () => { throw new DOMException("full", "QuotaExceededError"); }; }],
  ]) {
    test(`a session completes in memory when storage is ${failure}`, async () => {
      const context = await browser.newContext({ acceptDownloads: true });
      const page = await context.newPage();
      const problems = [];
      page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
      page.on("pageerror", (e) => problems.push(String(e)));
      await page.addInitScript(script);
      await page.coverage.startJSCoverage();
      await page.goto(`${base}study.html`);
      await page.getByRole("button", { name: "I agree, start" }).click();
      for (let i = 0; i < 5; i += 1) await doTask(page, { last: i === 4 });
      const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export my ratings" }).click()]);
      const body = JSON.parse(readFileSync(await download.path(), "utf8"));
      assert.equal(body.tasks.length, 5);
      assert.deepEqual(problems, []);
      await close(page, context);
    });
  }

  for (const scheme of ["light", "dark"]) {
    test(`axe-core finds no violations on the study screens in ${scheme} mode`, async () => {
      const { context, page, problems } = await study({ colorScheme: scheme });
      assert.deepEqual(await axeViolations(page), []);
      await page.getByRole("button", { name: "I agree, start" }).click();
      await page.getByRole("button", { name: "Pick a random track" }).click();
      await page.getByRole("button", { name: /^Calmer/ }).click();
      assert.deepEqual(await axeViolations(page), []);
      await page.getByRole("button", { name: "Show the two lists" }).click();
      await page.locator(".md-result").first().getByRole("button", { name: /Thumbs up/ }).click();
      await page.locator(".md-result").first().getByLabel(/New to me/).check();
      assert.deepEqual(await axeViolations(page), []);
      assert.deepEqual(problems, []);
      await close(page, context);
    });
  }
});


// All routes use the synthetic local bundle, never music services.
describe("deferred source recovery", () => {
  for (const path of ["", "study.html"]) {
    const isStudy = path === "study.html";
    const root = isStudy ? "#music-discovery-study" : "#music-discovery-demo";
    const setup = async (page) => {
      await page.goto(base + path);
      if (isStudy) {
        await page.getByRole("button", { name: "I agree, start" }).click();
        await page.getByRole("button", { name: "Pick a random track" }).click();
        await page.getByRole("button", { name: "Show the two lists" }).click();
      } else await pickSeed(page);
    };
    for (const failure of ["http", "shape"]) {
      test(`${path || "demo"}: ${failure} failure is accessible and retryable`, async () => {
        const context = await browser.newContext({ viewport: { width: 320, height: 700 } });
        const page = await context.newPage();
        const errors = [];
        page.on("pageerror", (error) => errors.push(String(error)));
        await page.coverage.startJSCoverage();
        await deadlineProbe(page);
        let requests = 0;
        await page.route("**/sources.json", (route) => {
          requests += 1;
          if (failure === "http") return route.fulfill({ status: 404 });
          return route.fulfill({ contentType: "application/json", body: '{"sources":null}' });
        });
        await setup(page);
        const retry = page.getByRole("button", { name: "Retry source links" });
        await retry.waitFor();
        assert.match(await page.locator(".md-source-status").textContent(), /unavailable/);
        assert.equal(await page.getByRole("link", { name: "License" }).count(), 10);
        await page.getByRole("button", { name: /Thumbs up/ }).first().click();
        assert.deepEqual(await axeViolations(page), []);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true);
        assert.equal(requests, 1); // no automatic retry
        await retry.click();
        await page.waitForFunction((root) => document.querySelector(root)[root.includes("study") ? "study" : "demo"].sources.state === "failed", root);
        assert.equal(requests, 2);
        assert.deepEqual(await page.evaluate(() => ({ timers: deadlineProbe.timers.size, cleared: deadlineProbe.cleared })), { timers: 0, cleared: 3 });
        assert.equal(await retry.isEnabled(), true);
        assert.deepEqual(errors, []);
        await close(page, context);
      });
    }

    test(`${path || "demo"}: retry preserves ratings and session, populating only current results once`, async () => {
      const context = await browser.newContext();
      const page = await context.newPage();
      await page.coverage.startJSCoverage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      let requests = 0;
      let release;
      const gate = new Promise((done) => { release = done; });
      // A configured URL is reused for the retry rather than falling back to sources.json.
      await page.route(base + path, (route) => {
        const html = readFileSync(join(bundle, path || "index.html"), "utf8");
        const marker = isStudy ? "data-music-discovery-study" : "data-music-discovery";
        return route.fulfill({ contentType: "text/html", headers: { "content-security-policy": CSP },
          body: html.replace(marker, `${marker} data-sources="recovery-sources.json"`) });
      });
      const sourceRequests = [];
      page.on("request", (request) => {
        if (request.url().includes("sources.json")) sourceRequests.push(request.url());
      });
      await page.route("**/recovery-sources.json", async (route) => {
        requests += 1;
        if (requests === 1) return route.fulfill({ status: 404 });
        await gate;
        await route.fulfill({ path: join(bundle, "sources.json") });
      });
      await setup(page);
      const retry = page.getByRole("button", { name: "Retry source links" });
      await retry.waitFor();
      const first = page.getByRole("button", { name: /Thumbs up/ }).first();
      await first.click();
      await page.evaluate((root) => {
        const app = document.querySelector(root)[root.includes("study") ? "study" : "demo"];
        globalThis.originalApp = app;
        globalThis.originalSession = app.session?.id;
        globalThis.originalOrders = JSON.stringify(app.orders);
        globalThis.oldLinks = [...document.querySelectorAll("[data-source-row]")];
        for (let i = 0; i < 10; i += 1) app.sources.retry.click();
      }, root);
      await page.waitForFunction(() => document.querySelector(".md-source-status").textContent.includes("Loading"));
      assert.equal(await retry.isDisabled(), true);
      assert.equal(await first.getAttribute("aria-pressed"), "true");
      if (isStudy) {
        // Finish the old task while recovery is in flight, then display the next task.
        for (const button of await page.getByRole("button", { name: /Thumbs up/ }).all()) {
          if (await button.getAttribute("aria-pressed") !== "true") await button.click();
        }
        await page.getByRole("radio", { name: "List B" }).check();
        await page.getByRole("button", { name: "Next task" }).click();
        await page.getByRole("button", { name: "Pick a random track" }).click();
        await page.getByRole("button", { name: "Show the two lists" }).click();
      } else {
        await pickSeed(page, "Synthetic track 110");
        await page.getByRole("button", { name: /^Calmer/ }).click();
      }
      await page.getByRole("button", { name: /Thumbs down/ }).first().click();
      const currentTitles = await titles(page);
      release();
      await page.waitForFunction(() => document.querySelector(".md-source-status").textContent === "Source links recovered.");
      assert.equal(requests, 2);
      assert.deepEqual(sourceRequests, [base + "recovery-sources.json", base + "recovery-sources.json"]);
      assert.equal(await retry.isVisible(), false);
      assert.deepEqual(await titles(page), currentTitles);
      assert.equal(await page.getByRole("button", { name: /Thumbs down/ }).first().getAttribute("aria-pressed"), "true");
      const catalog = JSON.parse(readFileSync(join(bundle, "catalog.json"), "utf8"));
      const { sources } = JSON.parse(readFileSync(join(bundle, "sources.json"), "utf8"));
      for (const item of await page.locator(".md-result").all()) {
        const links = item.getByRole("link", { name: /Source page/ });
        assert.equal(await links.count(), 1);
        const row = catalog.titles.indexOf(await item.locator(".md-title").textContent());
        assert.equal(await links.getAttribute("href"), sources[row]);
      }
      assert.equal(await page.evaluate((root) => {
        const app = document.querySelector(root)[root.includes("study") ? "study" : "demo"];
        return app === globalThis.originalApp && app.session?.id === globalThis.originalSession &&
          JSON.stringify(app.orders) === globalThis.originalOrders &&
          globalThis.oldLinks.every((el) => !el.isConnected && !el.querySelector("[data-source-link]")) &&
          (app.session ? app.session.tasks[0].lists[0].results[0].rating === 1 : [...app.ratings.values()].some((r) => r.rating === 1));
      }, root), true);
      assert.deepEqual(errors, []);
      await close(page, context);
    });
  }
});

// --- result presentation ---------------------------------------------------------

describe("result presentation", () => {
  const cases = [
    { title: "Synthetic title", artist: "Synthetic artist", genre: 0, license: 0, source: "https://example.test/track", expectedTitle: "Synthetic title", meta: "Synthetic artist · Rock" },
    { title: "", artist: "Synthetic artist", genre: -1, license: -1, source: "https://example.test/track", expectedTitle: "Track 100", meta: "Synthetic artist" },
    { title: "Synthetic title", artist: "", genre: 0, license: 0, source: "", expectedTitle: "Synthetic title", meta: "Rock" },
    { title: "", artist: "", genre: -1, license: -1, source: "", expectedTitle: "Track 100", meta: "" },
  ];
  for (const isStudy of [false, true]) {
    test(`${isStudy ? "study" : "demo"} preserves missing metadata, links and rating names`, async () => {
      const { context, page } = await (isStudy ? study() : open());
      await page.waitForFunction(() => document.querySelector(".md-source-status").textContent === "Source links loaded.");
      const actual = await page.evaluate(({ isStudy, cases }) => {
        const root = document.querySelector(isStudy ? "#music-discovery-study" : "#music-discovery-demo");
        const app = root[isStudy ? "study" : "demo"];
        const nodes = [];
        const snapshots = cases.map((sample, index) => {
          Object.assign(app.data, { ids: [100], titles: [sample.title], artists: [sample.artist], artist: [0],
            genres: ["Rock"], genre: [sample.genre], licenses: ["https://example.test/license"], license: [sample.license], sources: [sample.source] });
          const item = isStudy
            ? app.resultItem({ label: "A" }, { row: 0, trackId: 100, rating: null, newToMe: false }, index + 1, () => {})
            : app.resultItem({ row: 0, id: 100, explanations: ["Synthetic explanation"] }, index + 1, { seeds: [101], likes: [], dislikes: [] }, "synthetic");
          const head = item.querySelector(".md-result-head");
          const box = item.querySelector(".md-links");
          nodes.push(item, head, box, ...head.querySelectorAll("*"), ...box.children);
          root.append(item);
          return { title: head.querySelector(".md-title").textContent, meta: head.querySelector(".md-meta").textContent,
            links: [...box.children].map((a) => ({ text: a.textContent, href: a.getAttribute("href"), target: a.target, rel: a.rel, source: a.hasAttribute("data-source-link") })),
            group: item.querySelector(".md-rates").getAttribute("aria-label"),
            buttons: [...item.querySelectorAll(".md-rate")].map((b) => ({ text: b.textContent, pressed: b.getAttribute("aria-pressed") })) };
        });
        return { snapshots, distinct: new Set(nodes).size === nodes.length };
      }, { isStudy, cases });
      assert.equal(actual.distinct, true);
      for (const [index, sample] of cases.entries()) {
        const links = [];
        if (sample.source) links.push({ text: "Source page (FMA)", href: sample.source, target: "_blank", rel: "noopener noreferrer", source: true });
        if (sample.license >= 0) links.push({ text: "License", href: "https://example.test/license", target: "_blank", rel: "noopener noreferrer", source: false });
        assert.deepEqual(actual.snapshots[index], {
          title: sample.expectedTitle, meta: sample.meta,
          links, group: `Rate ${sample.expectedTitle}`,
          buttons: ["Thumbs up", "Thumbs down"].map((label) => ({ text: `${label} for ${sample.expectedTitle}`, pressed: "false" })),
        });
        // Role queries check the accessible names, including the fallback title.
        assert.ok(await page.getByRole("button", { name: `Thumbs up for ${sample.expectedTitle}`, exact: true }).count());
        assert.ok(await page.getByRole("button", { name: `Thumbs down for ${sample.expectedTitle}`, exact: true }).count());
        if (isStudy) assert.ok(await page.getByRole("checkbox", { name: `New to me for ${sample.expectedTitle}`, exact: true }).count());
      }
      await close(page, context);
    });
  }
});

// --- shared catalog mounting -----------------------------------------------------

describe("catalog load failures", () => {
  const pages = [
    { path: "", root: "#music-discovery-demo", name: "demo" },
    { path: "study.html", root: "#music-discovery-study", name: "study" },
  ];
  const failures = [
    { label: "non-OK response", route: (r) => r.fulfill({ status: 503, body: "" }), message: /HTTP 503/ },
    { label: "invalid JSON", route: (r) => r.fulfill({ status: 200, contentType: "application/json", body: "{" }), message: /./ },
  ];
  for (const { path, root, name } of pages) {
    for (const { label, route, message } of failures) {
      test(`${name} shows its alert and clears aria-busy on ${label}`, async () => {
        const context = await browser.newContext();
        const page = await context.newPage();
        await deadlineProbe(page);
        let catalogRequests = 0;
        await page.route("**/catalog.json", (r) => {
          catalogRequests += 1;
          return route(r);
        });
        await page.goto(base + path);
        const alert = page.locator(`${root} [role=alert]`);
        await alert.waitFor();
        assert.match(await alert.textContent(), new RegExp(`^The ${name} could not load its catalog [(]`));
        assert.match(await alert.textContent(), message);
        assert.equal(await page.locator(root).getAttribute("aria-busy"), null);
        assert.equal(catalogRequests, 1);
        assert.deepEqual(await page.evaluate(() => ({ timers: deadlineProbe.timers.size, cleared: deadlineProbe.cleared })), { timers: 0, cleared: 1 });
        await context.close();
      });
    }
  }
});

// The fake clock expires immediately; stalled fetches deliberately ignore their signal.
describe("application fetch deadlines", () => {
  for (const isStudy of [false, true]) {
    const name = isStudy ? "study" : "demo";
    const path = isStudy ? "study.html" : "";
    const root = isStudy ? "#music-discovery-study" : "#music-discovery-demo";
    test(`${name}: expiry aborts a real fetch while its JSON body is stalled`, async () => {
      const context = await browser.newContext();
      const page = await context.newPage();
      await page.coverage.startJSCoverage();
      await deadlineProbe(page);
      await page.route(base + path, (route) => {
        const html = readFileSync(join(bundle, path || "index.html"), "utf8");
        const marker = isStudy ? "data-music-discovery-study" : "data-music-discovery";
        return route.fulfill({ contentType: "text/html", headers: { "content-security-policy": CSP },
          body: html.replace(marker, `${marker} data-sources="stalled-body.json"`) });
      });
      const response = page.waitForResponse((r) => r.url() === base + "stalled-body.json");
      await page.goto(base + path);
      await response;
      const aborted = page.waitForEvent("requestfailed", { predicate: (r) => r.url() === base + "stalled-body.json" });
      await page.evaluate(() => deadlineProbe.expire());
      assert.match((await aborted).failure().errorText, /ABORTED/);
      await page.getByRole("button", { name: "Retry source links" }).waitFor();
      assert.match(await page.locator(".md-source-status").textContent(), /unavailable/);
      assert.equal(await page.locator(root).getAttribute("aria-busy"), null);
      assert.deepEqual(await page.evaluate(() => ({ timers: deadlineProbe.timers.size, cleared: deadlineProbe.cleared })),
        { timers: 0, cleared: 2 });
      await close(page, context);
    });

    for (const stall of ["request", "body"]) {
      test(`${name}: stalled catalog ${stall} times out and ignores late completion`, async () => {
        const context = await browser.newContext();
        const page = await context.newPage();
        await page.coverage.startJSCoverage();
        await deadlineProbe(page, { target: "catalog.json", stall });
        await page.goto(base + path);
        await page.waitForFunction(() => deadlineProbe.attempts.length === 1);
        assert.equal(await page.locator(root).getAttribute("aria-busy"), "true");
        await page.evaluate(() => deadlineProbe.expire());
        const alert = page.locator(`${root} [role=alert]`);
        await alert.waitFor();
        assert.equal(await alert.textContent(), `The ${name} could not load its catalog (Request timed out after 30 seconds).`);
        assert.equal(await page.locator(root).getAttribute("aria-busy"), null);
        assert.deepEqual(await page.evaluate(() => ({ count: deadlineProbe.attempts.length,
          aborted: deadlineProbe.attempts[0].signal.aborted, events: deadlineProbe.attempts[0].aborted,
          timers: deadlineProbe.timers.size, cleared: deadlineProbe.cleared })),
        { count: 1, aborted: true, events: 1, timers: 0, cleared: 1 });
        // Flush the losing promise to prove it cannot replace the alert.
        await page.evaluate(async () => {
          deadlineProbe.finish(0);
          await new Promise((resolve) => setTimeout(resolve, 0));
        });
        assert.equal(await alert.count(), 1);
        assert.equal(await page.evaluate(({ root, name }) => !!document.querySelector(root)[name], { root, name }), false);
        await close(page, context);
      });

      for (const lateFailure of [false, true]) {
        test(`${name}: stalled source ${stall} recovers once despite late ${lateFailure ? "failure" : "success"}`, async () => {
          const context = await browser.newContext();
          const page = await context.newPage();
          const errors = [];
          page.on("pageerror", (error) => errors.push(String(error)));
          await page.coverage.startJSCoverage();
          await deadlineProbe(page, { target: "sources.json", stall, holdRetry: true });
          await page.goto(base + path);
          if (isStudy) await page.getByRole("button", { name: "I agree, start" }).click();
          await page.getByRole("button", { name: "Pick a random track" }).click();
          await page.getByRole("button", { name: /^Calmer/ }).click();
          if (isStudy) await page.getByRole("button", { name: "Show the two lists" }).click();
          await page.getByRole("button", { name: /Thumbs up/ }).first().click();
          if (isStudy) {
            await page.getByRole("checkbox", { name: /New to me/ }).first().check();
            await page.getByRole("radio", { name: "List B" }).check();
          }
          assert.equal(await page.getByRole("link", { name: "License" }).count(), 10);
          assert.equal(await page.locator(root).getAttribute("aria-busy"), null);
          await page.evaluate(() => deadlineProbe.expire());
          const retry = page.getByRole("button", { name: "Retry source links" });
          await retry.waitFor();
          assert.equal(await retry.isEnabled(), true);
          assert.deepEqual(await page.evaluate(() => ({ aborted: deadlineProbe.attempts[0].signal.aborted,
            timers: deadlineProbe.timers.size, cleared: deadlineProbe.cleared })),
          { aborted: true, timers: 0, cleared: 2 }); // catalog success and source expiry
          const snapshot = () => page.evaluate(({ root, name }) => {
            const app = document.querySelector(root)[name];
            return { seed: app.seed.row, qualities: app.picker.qualities, session: app.session,
              orders: app.orders, ratings: app.ratings ? [...app.ratings] : null,
              results: [...document.querySelectorAll(".md-result")].map((el) => {
                const clone = el.cloneNode(true);
                clone.querySelectorAll("[data-source-link]").forEach((link) => link.remove());
                return clone.outerHTML;
              }), preferred: document.querySelector('input[type="radio"]:checked')?.value };
          }, { root, name });
          const beforeRecovery = await snapshot();
          await page.evaluate(({ root, name }) => {
            const app = document.querySelector(root)[name];
            globalThis.originalApp = app;
            app.sources.retry.click();
            globalThis.retryReady = app.sources.ready;
            for (let i = 0; i < 10; i += 1) {
              if (app.sources.load(app.sources.url) !== retryReady) throw new Error("Duplicate attempt");
              app.sources.retry.click();
            }
          }, { root, name });
          assert.equal(await retry.isDisabled(), true);
          assert.equal(await page.evaluate(() => deadlineProbe.attempts.length), 2);
          assert.equal(await page.evaluate(() => deadlineProbe.attempts[1].signal.aborted), false);
          // Old success arrives while the new request is still in flight.
          if (!lateFailure) await page.evaluate(async () => {
            deadlineProbe.finish(0, false, true);
            await new Promise((resolve) => setTimeout(resolve, 0));
          });
          assert.match(await page.locator(".md-source-status").textContent(), /Loading/);
          assert.equal(await page.getByRole("link", { name: /Source page/ }).count(), 0);
          await page.evaluate(async () => {
            deadlineProbe.finish(1);
            await retryReady;
          });
          // Old rejection arrives after recovery and must not reset the state.
          if (lateFailure) await page.evaluate(async () => {
            deadlineProbe.finish(0, true);
            await new Promise((resolve) => setTimeout(resolve, 0));
          });
          assert.equal(await page.locator(".md-source-status").textContent(), "Source links recovered.");
          assert.equal(await retry.isVisible(), false);
          assert.deepEqual(await snapshot(), beforeRecovery);
          const catalog = JSON.parse(readFileSync(join(bundle, "catalog.json"), "utf8"));
          const { sources } = JSON.parse(readFileSync(join(bundle, "sources.json"), "utf8"));
          for (const item of await page.locator(".md-result").all()) {
            const links = item.getByRole("link", { name: /Source page/ });
            assert.equal(await links.count(), 1);
            const row = catalog.titles.indexOf(await item.locator(".md-title").textContent());
            assert.equal(await links.getAttribute("href"), sources[row]);
          }
          assert.deepEqual(await page.evaluate(({ root, name }) => ({
            sameApp: document.querySelector(root)[name] === originalApp,
            attempts: deadlineProbe.attempts.length, timers: deadlineProbe.timers.size,
            cleared: deadlineProbe.cleared, state: originalApp.sources.state }), { root, name }),
          { sameApp: true, attempts: 2, timers: 0, cleared: 3, state: "loaded" });
          assert.deepEqual(errors, []);
          await close(page, context);
        });
      }
    }
  }
});
