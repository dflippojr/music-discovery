import { test, expect, ready, pickSeed, expectNoOverflow } from "./fixtures.mjs";

for (const path of ["/", "/study.html"]) {
  test("breakpoints: " + path + " has no overflow at 320/375/768/1440 CSS px", async ({ page }) => {
    for (const width of [320, 375, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 }); await ready(page, path);
      await expectNoOverflow(page, width);
      if (path === "/study.html") await page.getByRole("button", { name: "I agree, start" }).click();
      await pickSeed(page); await expectNoOverflow(page, width);
      if (path === "/study.html") await page.getByRole("button", { name: "Show the two lists" }).click();
      await expect(page.locator(".md-result")).toHaveCount(10); await expectNoOverflow(page, width);
    }
  });
}

test("live resize: open search and partial form retain state, focus and usable menu", async ({ page }) => {
  for (const path of ["/", "/study.html"]) {
    await page.setViewportSize({ width: 1440, height: 900 }); await ready(page, path);
    if (path === "/study.html") await page.getByRole("button", { name: "I agree, start" }).click();
    else await page.getByRole("radio", { name: /^Baseline/ }).check();
    const calmer = page.getByRole("button", { name: /^Calmer/ }); await calmer.click();
    const input = page.getByRole("combobox"); await input.fill("Synthetic track 10");
    await expect(page.getByRole("option").first()).toBeVisible();
    const active = await input.getAttribute("aria-activedescendant");
    const initialScroll = await page.evaluate(() => scrollY);
    for (const width of [768, 375, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 }); await expectNoOverflow(page, width);
      await expect(input).toHaveValue("Synthetic track 10"); await expect(input).toBeFocused();
      await expect(input).toHaveAttribute("aria-expanded", "true"); await expect(input).toHaveAttribute("aria-activedescendant", active);
      await expect(calmer).toHaveAttribute("data-state", "like");
      if (path === "/") await expect(page.getByRole("radio", { name: /^Baseline/ })).toBeChecked();
      expect(await page.evaluate(() => scrollY)).toBe(initialScroll);
    }
    await input.press("Escape"); await expect(input).toHaveAttribute("aria-expanded", "false");
    await input.press("ArrowDown"); await input.press("Enter");
    await expect(input).toHaveValue(/^Synthetic track 10/); await expect(input).toHaveAttribute("aria-expanded", "false");
    await input.press("Tab"); await expect(page.getByRole("button", { name: "Pick a random track" })).toBeFocused();
    await page.getByRole("button", { name: "Pick a random track" }).click(); await expect(input).toHaveValue(/^Synthetic track /);
  }
});
