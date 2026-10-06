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

const bundle = resolve(process.env.DEMO_BUNDLE ?? ".demo-fixture");
const axeSource = readFileSync(resolve("node_modules/axe-core/axe.min.js"), "utf8");
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json" };
const CSP = "default-src 'self'; script-src 'self'; style-src 'self'";

// Coverage of the demo and study scripts, merged over every test and written as lcov for
// SonarCloud when COVERAGE_DIR is set.
const sources = Object.fromEntries(["demo.js", "study.js", "study-lib.js", "ui.js", "ranker.js"].map((name) => [name, resolve("src/musicdiscovery/demo", name)]));
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

const titles = (page) => page.locator(".md-result .md-title").allTextContents();

async function axeViolations(page) {
  await page.waitForTimeout(400); // let colour transitions finish before measuring contrast
  await page.evaluate(axeSource);
  const result = await page.evaluate(() => globalThis.axe.run(document));
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
}

describe("demo page", () => {
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

  test("every result explains itself and links to its source", async () => {
    const { context, page } = await open();
    await pickSeed(page);
    for (const item of await page.locator(".md-result").all()) {
      assert.ok((await item.locator(".md-why li").count()) >= 1);
      assert.match(await item.getByRole("link", { name: /Source page/ }).getAttribute("href"), /^https:/);
    }
    await close(page, context);
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
      await page.getByRole("button", { name: /^Calmer/ }).click();
      const folk = page.locator(".md-chip").filter({ has: page.getByText("Folk", { exact: true }) });
      await folk.click();
      await folk.click();
      await page.getByRole("button", { name: /^Thumbs up/ }).first().click();
      assert.deepEqual(await axeViolations(page), []);
      assert.deepEqual(problems, []);
      await close(page, context);
    });
  }

  test("there is no horizontal scroll at 320 px", async () => {
    const { context, page } = await open({ viewport: { width: 320, height: 700 } });
    await pickSeed(page);
    const widths = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
    assert.ok(widths[0] <= widths[1], `scrollWidth ${widths[0]} > ${widths[1]}`);
    await close(page, context);
  });

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
    assert.equal(body.ratings[0].ranker, "hybrid");
    assert.ok(requests.every((url) => url.startsWith(base) || url.startsWith("blob:")), requests.join());
    await close(page, context);
  });

  test("reduced motion is respected", async () => {
    const { context, page } = await open({ reducedMotion: "reduce" });
    await pickSeed(page);
    const duration = await page.locator(".md-chip").first().evaluate((el) => getComputedStyle(el).transitionDuration);
    assert.match(duration, /^0s/);
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

describe("study page", () => {
  test("shows two unlabelled lists and never names a ranker", async () => {
    const { context, page, problems } = await study();
    assert.match(await page.locator("#music-discovery-study").textContent(), /By sending the file you agree/);
    await page.getByRole("button", { name: "I agree, start" }).click();
    await page.getByRole("button", { name: "Pick a random track" }).click();
    await page.getByRole("button", { name: "Show the two lists" }).click();
    await page.locator(".md-result").first().waitFor();
    assert.deepEqual(await page.locator("h3.md-study-list").allTextContents(), ["List A", "List B"]);
    assert.equal(await page.locator(".md-result").count(), 10);
    assert.equal(await page.locator(".md-why").count(), 0);
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

  test("there is no horizontal scroll at 320 px on any study screen", async () => {
    const { context, page } = await study({ viewport: { width: 320, height: 700 } });
    const fits = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
    assert.equal(await fits(), true);
    await page.getByRole("button", { name: "I agree, start" }).click();
    assert.equal(await fits(), true);
    await page.getByRole("button", { name: "Pick a random track" }).click();
    await page.getByRole("button", { name: "Show the two lists" }).click();
    await page.locator(".md-result").first().waitFor();
    assert.equal(await fits(), true);
    await close(page, context);
  });
});
