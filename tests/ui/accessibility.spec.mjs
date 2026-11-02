import { test, expect, ready, pickSeed, expectNoOverflow } from "./fixtures.mjs";

// `again`: an earlier pass in this tab rated tracks, which the study keeps for resuming (#136).
async function results(page, path, again = false) {
  await ready(page, path);
  if (path === "/study.html" && again) await page.getByRole("button", { name: "Start over" }).click();
  if (path === "/study.html") await page.getByRole("button", { name: "I agree, start" }).click();
  await pickSeed(page);
  if (path === "/study.html") await page.getByRole("button", { name: "Show the two lists" }).click();
  await expect(page.locator(".md-result")).toHaveCount(10);
}

async function expectTargets(locator) {
  const boxes = await locator.evaluateAll(elements => elements.map(element => {
    const { x, y, width, height } = element.getBoundingClientRect();
    return { x, y, width, height };
  }));
  expect(boxes.length).toBeGreaterThan(0);
  for (const box of boxes) {
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
  }
  for (let i = 1; i < boxes.length; i += 1) {
    const a = boxes[i - 1], b = boxes[i];
    const gap = Math.max(b.x - a.x - a.width, a.x - b.x - b.width,
      b.y - a.y - a.height, a.y - b.y - b.height);
    expect(gap).toBeGreaterThanOrEqual(8);
  }
}

async function activate(locator, isMobile, position) {
  if (isMobile) await locator.tap({ position });
  else await locator.click({ position });
}

// Deliberately hit the label's empty corner, beyond its text/native glyph.
async function activateLabel(locator, isMobile) {
  const { width, height } = await locator.evaluate(element => {
    const box = element.getBoundingClientRect();
    return { width: box.width, height: box.height };
  });
  await activate(locator, isMobile, { x: width - 3, y: height - 3 });
}

for (const path of ["/", "/study.html"]) {
  test("#50: " + path + " reflows readable ratings at 200% text size", async ({ page, isMobile }) => {
    // A wider fallback font also exercises the space needed by Linux system fonts.
    for (const [pass, { width, fontFamily }] of [{ width: 320 }, { width: 375 }, { width: 320, fontFamily: "Verdana, sans-serif" }].entries()) {
      await page.setViewportSize({ width, height: 667 });
      await results(page, path, pass > 0);
      // Change the loaded stylesheet through CSSOM so WebKit also keeps the CSP intact.
      await page.evaluate(() => { document.styleSheets[0].insertRule(":root { font-size: 200%; }"); });
      if (fontFamily) await page.evaluate(family => {
        document.styleSheets[0].insertRule(".md-demo { font-family: " + family + "; }");
      }, fontFamily);
      await expect(page.locator("html")).toHaveCSS("font-size", "32px");
      await expectNoOverflow(page, width);
      const buttons = page.locator(".md-rate");
      await expect(buttons).toHaveCount(20);
      // Every whole word must fit on one line inside the visible button.
      const readable = await buttons.evaluateAll(elements => elements.every(button => {
        const label = button.querySelector("span:not(.md-sr)").firstChild;
        const box = button.getBoundingClientRect();
        return [...label.textContent.matchAll(/\S+/g)].every(match => {
          const range = document.createRange();
          range.setStart(label, match.index); range.setEnd(label, match.index + match[0].length);
          const rects = [...range.getClientRects()];
          return rects.length === 1 && rects[0].left >= box.left && rects[0].right <= box.right;
        }) && button.scrollWidth <= button.clientWidth;
      }));
      expect(readable, "Rating words fit without fragmentation or clipping").toBe(true);
      const titleFits = await page.locator(".md-title").evaluateAll(elements => elements.every(element => {
        const text = element.firstChild;
        return [...text.textContent.matchAll(/\S+/g)].every(match => {
          const range = document.createRange();
          range.setStart(text, match.index); range.setEnd(text, match.index + match[0].length);
          return range.getClientRects().length === 1;
        });
      }));
      expect(titleFits, "Synthetic titles keep whole words at enlarged text size").toBe(true);
      const up = page.getByRole("button", { name: /^Thumbs up/ }).first();
      const down = page.getByRole("button", { name: /^Thumbs down/ }).first();
      await activate(up, isMobile); await expect(up).toHaveAttribute("aria-pressed", "true");
      await activate(down, isMobile);
      await expect(up).toHaveAttribute("aria-pressed", "false");
      await expect(down).toHaveAttribute("aria-pressed", "true");
      await down.focus(); await down.press("Space");
      await expect(down).toHaveAttribute("aria-pressed", "false");
    }
  });

  test("#231: " + path + " keeps results reflowed and targets sized at enlarged text", async ({ page, browserName }) => {
    const study = path === "/study.html";
    const open = async (width, size) => {
      await page.setViewportSize({ width, height: 667 });
      await ready(page, path);
      if (study) await page.getByRole("button", { name: "I agree, start" }).click();
      await page.evaluate(size => { document.styleSheets[0].insertRule(":root { font-size: " + size + "%; }"); }, size);
      await expect(page.locator("html")).toHaveCSS("font-size", 16 * size / 100 + "px");
    };
    const show = async () => {
      await pickSeed(page);
      if (study) await page.getByRole("button", { name: "Show the two lists" }).click();
      await expect(page.locator(".md-result")).toHaveCount(10);
    };
    const firstTitle = page.locator(".md-title").first();
    const setTitle = text => firstTitle.evaluate((title, text) => { title.textContent = text; }, text);

    // 275% is where a rating button must put its icon over its label to fit. WebKit's wider
    // text overflows this page above 200% (4 px at 225%), so only Chromium goes that far.
    const sizes = [[320, 200], [375, 200], ...(browserName === "chromium" ? [[320, 275]] : [])];
    for (const [width, size] of sizes) {
      await open(width, size);
      await page.getByRole("combobox").fill("Synthetic track 10");
      await expect(page.getByRole("option")).toHaveCount(8);
      await expectTargets(page.getByRole("option"));
      await show();
      await expectNoOverflow(page, width);

      // One title no line can hold: breaking it keeps the page in bounds.
      const unbroken = "Synthetic" + "x".repeat(80);
      await setTitle(unbroken);
      await expectNoOverflow(page, width);

      const results = await page.locator(".md-result").evaluateAll(elements => elements.map(result => {
        const inside = (box, outer) => box.left >= outer.left - 0.5 && box.right <= outer.right + 0.5;
        const outer = result.getBoundingClientRect();
        const head = result.querySelector(".md-result-head").getBoundingClientRect();
        const rank = result.querySelector(".md-rank").getBoundingClientRect();
        const text = result.querySelector(".md-result-head > div").getBoundingClientRect();
        const title = result.querySelector(".md-title").getBoundingClientRect();
        const buttons = [...result.querySelectorAll(".md-rate")];
        const rates = buttons.map(button => button.getBoundingClientRect());
        const wholeWords = buttons.every(button => {
          const label = button.querySelector("span:not(.md-sr)").firstChild;
          return [...label.textContent.matchAll(/\S+/g)].every(match => {
            const range = document.createRange();
            range.setStart(label, match.index); range.setEnd(label, match.index + match[0].length);
            const lines = [...range.getClientRects()];
            return lines.length === 1 && inside(lines[0], outer);
          });
        });
        return {
          titleInside: inside(title, outer),
          titleUnderRank: text.top >= rank.bottom,
          titleFillsRow: Math.abs(text.width - head.width) <= 0.5,
          ratesInside: rates.every(box => inside(box, outer)),
          ratesStacked: rates[1].top >= rates[0].bottom,
          ratesTall: rates.every(box => box.height >= 44),
          wholeWords,
        };
      }));
      for (const result of results) expect(result).toEqual({
        titleInside: true, titleUnderRank: true, titleFillsRow: true,
        ratesInside: true, ratesStacked: true, ratesTall: true, wholeWords: true,
      });

      for (const links of await page.locator(".md-links").all()) await expectTargets(links.locator("a"));
      const labels = page.locator(".md-radio label");
      await expectTargets(labels);
      const filled = await labels.evaluateAll(elements => elements.every(label =>
        Math.abs(label.getBoundingClientRect().right - label.parentElement.getBoundingClientRect().right) <= 0.5));
      expect(filled, "Each radio label fills its row, so the whole row is a target").toBe(true);

      // A link's own text never decides its target: the shortest label keeps the full width.
      const shortLink = page.locator(".md-links a").first();
      await shortLink.evaluate(link => { link.textContent = "L"; });
      expect((await shortLink.boundingBox()).width).toBeGreaterThanOrEqual(44);

      // Even if the title cannot break at all, it must not widen its result or the page.
      await page.evaluate(() => { document.styleSheets[0].insertRule(".md-title { overflow-wrap: normal; overflow: hidden; }"); });
      await expectNoOverflow(page, width);
      const bounds = await page.locator(".md-result").first().boundingBox();
      const box = await firstTitle.boundingBox();
      expect(box.x + box.width).toBeLessThanOrEqual(bounds.x + bounds.width + 0.5);
    }

    // With room beside the rank, a long title wraps there instead of dropping under it.
    await open(768, 200);
    await show();
    await setTitle(Array(40).fill("Synthetic").join(" "));
    const placed = await page.locator(".md-result-head").first().evaluate(head => {
      const rank = head.querySelector(".md-rank").getBoundingClientRect();
      const text = head.querySelector("div").getBoundingClientRect();
      return { beside: text.left >= rank.right, sameRow: text.top < rank.bottom };
    });
    expect(placed).toEqual({ beside: true, sameRow: true });

    // Text just large enough to stack the two links is where their row gap is tightest.
    await open(320, 150);
    await show();
    const stacked = await page.locator(".md-links").first().evaluate(box => {
      const [a, b] = [...box.querySelectorAll("a")].map(link => link.getBoundingClientRect());
      return b.top >= a.bottom;
    });
    expect(stacked, "This size must stack the links, or it no longer exercises the gap").toBe(true);
    for (const links of await page.locator(".md-links").all()) await expectTargets(links.locator("a"));
  });

  test("#51/#52: " + path + " has usable link and native-control touch areas", async ({ page, isMobile, baseURL }) => {
    for (const [pass, viewport] of [{ width: 375, height: 667 }, { width: 667, height: 375 }, { width: 320, height: 667 }].entries()) {
      await page.setViewportSize(viewport); await results(page, path, pass > 0);
      await expectNoOverflow(page, viewport.width);
      for (const links of await page.locator(".md-links").all()) await expectTargets(links.locator("a"));
      for (const name of ["Source page (FMA)", "License"]) {
        const link = page.getByRole("link", { name, exact: true }).first();
        await expect(link).toHaveAttribute("target", "_blank");
        await expect(link).toHaveAttribute("rel", "noopener noreferrer");
        const event = page.waitForEvent("popup"); await activate(link, isMobile);
        const popup = await event;
        try {
          await expect(popup.getByRole("heading", { name: "Synthetic local reference" })).toBeVisible();
          expect(new URL(popup.url()).origin).toBe(baseURL);
        } finally { await popup.close(); }
      }
      if (path === "/") {
        const exclude = page.getByRole("checkbox", { name: "Exclude the seed artist" });
        const label = page.locator(".md-check"); await expectTargets(label);
        await activateLabel(label, isMobile); await expect(exclude).toBeChecked();
        expect((await page.locator(".md-meta").allTextContents()).every(text => !text.startsWith("artist0 ·"))).toBe(true);
        await exclude.focus(); await exclude.press("Space"); await expect(exclude).not.toBeChecked();
      } else {
        const novelty = page.getByRole("checkbox", { name: /^New to me/ });
        await expectTargets(page.locator(".md-new"));
        await activateLabel(page.locator(".md-new").first(), isMobile);
        await expect(novelty.first()).toBeChecked(); await expect(novelty.nth(1)).not.toBeChecked();
        await novelty.first().focus(); await novelty.first().press("Space"); await expect(novelty.first()).not.toBeChecked();
        await expectTargets(page.locator(".md-radio label"));
        const next = page.getByRole("button", { name: "Next task", exact: true });
        for (const name of ["List A", "List B", "No preference"]) {
          const radio = page.getByRole("radio", { name, exact: true });
          const id = await radio.getAttribute("id");
          await activateLabel(page.locator('label[for="' + id + '"]'), isMobile);
          await expect(radio).toBeChecked(); await expect(next).toBeDisabled();
          await expect(page.locator('input[type="radio"]:checked')).toHaveCount(1);
        }
        for (const button of await page.getByRole("button", { name: /^Thumbs up/ }).all()) await button.click();
        await expect(next).toBeEnabled(); await next.click();
        await expect(page.getByRole("heading", { name: "Task 2 of 5", exact: true })).toBeVisible();
      }
    }
  });

  test("#53: " + path + " has separated, scrollable suggestions with touch and keyboard selection", async ({ page, isMobile }) => {
    for (const viewport of [{ width: 375, height: 667 }, { width: 667, height: 375 }, { width: 320, height: 667 }]) {
      await page.setViewportSize(viewport); await ready(page, path);
      if (path === "/study.html") await page.getByRole("button", { name: "I agree, start" }).click();
      const input = page.getByRole("combobox");
      await input.fill("Synthetic track 10");
      const options = page.getByRole("option"); await expect(options).toHaveCount(8);
      await expectTargets(options); await expectNoOverflow(page, viewport.width);
      expect(await page.locator(".md-suggestions").evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
      for (const index of [1, 7]) {
        const option = options.nth(index), label = await option.textContent();
        await activate(option, isMobile); await expect(input).toHaveValue(label);
        await expect(input).toHaveAttribute("aria-expanded", "false");
        await input.fill("Synthetic track 10");
      }
      await input.press("ArrowDown");
      await expect(input).toHaveAttribute("aria-activedescendant", await options.nth(1).getAttribute("id"));
      await input.press("ArrowUp");
      await expect(input).toHaveAttribute("aria-activedescendant", await options.first().getAttribute("id"));
      await input.press("Escape"); await expect(input).toHaveAttribute("aria-expanded", "false");
      await input.press("ArrowDown");
      await expect(input).toHaveAttribute("aria-activedescendant", await options.nth(1).getAttribute("id"));
      await input.press("Enter");
      await expect(input).toHaveValue("Synthetic track 101 — artist1");
    }
  });
}
