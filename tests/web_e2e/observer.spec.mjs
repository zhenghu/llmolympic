import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const MATCH_ID = "web-e2e-match";
const XSS_SENTINEL = '<img src=x onerror="globalThis.__LLMOLYMPIC_XSS__=true">';
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

function summarizedViolations(results) {
  return results.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    targets: violation.nodes.map((node) => node.target),
  }));
}

async function expectNoWcagViolations(page) {
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  expect(summarizedViolations(results), "axe WCAG A/AA violations").toEqual([]);
}

test("installed observer renders, navigates, replays, and treats archive text as text", async ({
  page,
}) => {
  const browserErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "每一场模型较量，都有迹可循。" })).toBeVisible();
  await expect(page.getByText("数据库可用", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "最近对局" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "ELO 排行榜" })).toBeVisible();

  await page.getByLabel("比赛项目").selectOption("math_quiz");
  await expect(page).toHaveURL(/\?game=math_quiz$/);
  const match = page.locator(".match-card").filter({ hasText: XSS_SENTINEL });
  await expect(match).toHaveCount(1);
  await expectNoWcagViolations(page);

  await match.click();
  await expect(page).toHaveURL(new RegExp(`/matches/${MATCH_ID}$`));
  await expect(page.getByRole("heading", { name: new RegExp("安全对手") })).toBeVisible();
  await expect(page.getByText("WebSocket 同源回放", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "下一步" })).toBeEnabled();
  await page.getByRole("button", { name: "下一步" }).click();
  await expect(page.getByLabel("事件 1")).toBeVisible();

  expect(await page.locator('img[src="x"]').count()).toBe(0);
  expect(await page.evaluate(() => globalThis.__LLMOLYMPIC_XSS__)).toBeUndefined();
  await expect(page.getByText(XSS_SENTINEL, { exact: true }).first()).toBeVisible();
  await expectNoWcagViolations(page);
  expect(browserErrors).toEqual([]);
});

test("observer falls back to the read-only REST detail when WebSocket is unavailable", async ({
  page,
}) => {
  await page.addInitScript(() => {
    delete globalThis.WebSocket;
  });
  await page.goto(`/matches/${MATCH_ID}`);

  await expect(page.getByText("REST 安全回退", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "下一步" })).toBeEnabled();
  await expectNoWcagViolations(page);
});
