// Browser checks for the standalone page, served from a local static server under the
// CSP dflippojr.dev uses: CSP, console errors, the toggle, axe-core in light and dark,
// 320 px layout, ratings and export. Needs `npm install` and `npx playwright install chromium`
// and a bundle built by `python scripts/build_demo_fixture.py <dir>` (DEMO_BUNDLE).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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

// Coverage of demo.js and ranker.js, merged over every test and written as lcov for
// SonarCloud when COVERAGE_DIR is set.
const sources = { "demo.js": resolve("src/musicdiscovery/demo/demo.js"), "ranker.js": resolve("src/musicdiscovery/demo/ranker.js") };
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

async function open(options = {}) {
  const context = await browser.newContext({ acceptDownloads: true, ...options });
  const page = await context.newPage();
  const problems = [];
  page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
  page.on("pageerror", (e) => problems.push(String(e)));
  page.on("requestfailed", (r) => problems.push(`request failed: ${r.url()}`));
  await page.coverage.startJSCoverage();
  await page.goto(base);
  await page.waitForSelector("#music-discovery-demo input[role=combobox]");
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
