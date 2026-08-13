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

test("observer opens a completed live card and follows its archive link", async ({ page }) => {
  const browserErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "实时观战" })).toBeVisible();
  const liveCard = page.locator(".live-card").filter({ hasText: XSS_SENTINEL });
  await expect(liveCard).toHaveCount(1);
  await expect(liveCard.getByText("已完成并存档", { exact: true })).toBeVisible();
  await expectNoWcagViolations(page);

  await liveCard.click();
  await expect(page).toHaveURL(/\/live\/[A-Fa-f0-9]{32}$/);
  await expect(page.getByRole("heading", { name: new RegExp("安全对手") })).toBeVisible();
  await expect(page.getByText("比赛已完成并存档", { exact: true })).toBeVisible();
  await expect(page.getByText("WebSocket 同源直播", { exact: true })).toBeVisible();
  const archiveLink = page.getByRole("link", { name: "打开存档回放" });
  await expect(archiveLink).toBeVisible();

  expect(await page.locator('img[src="x"]').count()).toBe(0);
  expect(await page.evaluate(() => globalThis.__LLMOLYMPIC_XSS__)).toBeUndefined();
  await expect(page.getByText(XSS_SENTINEL, { exact: true }).first()).toBeVisible();
  await expectNoWcagViolations(page);

  await archiveLink.click();
  await expect(page).toHaveURL(new RegExp(`/matches/${MATCH_ID}$`));
  await expect(page.getByText("WebSocket 同源回放", { exact: true })).toBeVisible();
  expect(browserErrors).toEqual([]);
});

test("completed live detail falls back to REST polling without executing archive text", async ({
  page,
}) => {
  const response = await page.request.get("/api/v1/live?game=math_quiz&limit=1");
  expect(response.ok()).toBeTruthy();
  const payload = await response.json();
  expect(payload.matches).toHaveLength(1);
  const liveId = payload.matches[0].live_id;

  const browserErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  await page.addInitScript(() => {
    delete globalThis.WebSocket;
  });
  await page.goto(`/live/${encodeURIComponent(liveId)}`);

  await expect(page.getByText("比赛已完成并存档", { exact: true })).toBeVisible();
  await expect(page.getByText("REST 只读轮询", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "打开存档回放" })).toBeVisible();
  expect(await page.locator('img[src="x"]').count()).toBe(0);
  expect(await page.evaluate(() => globalThis.__LLMOLYMPIC_XSS__)).toBeUndefined();
  await expect(page.getByText(XSS_SENTINEL, { exact: true }).first()).toBeVisible();
  await expectNoWcagViolations(page);
  expect(browserErrors).toEqual([]);
});
