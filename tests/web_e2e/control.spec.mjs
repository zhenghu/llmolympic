import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { expect, test as base } from "@playwright/test";

const ADMIN_STORAGE_KEY = "llmolympic.control.admin";
const URL_SAFE_CAPABILITY = /^[A-Za-z0-9_-]{43}$/;
const PARTICIPATION_PATH = /^\/participate\/([A-Za-z0-9._:-]+)\/([A-Za-z0-9._:-]+)$/;
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const CONTROL_START_TIMEOUT_MS = 20_000;
const CONTROL_TEST_TIMEOUT_MS = 60_000;

// Admin and seat capabilities are real credentials. Disabling traces keeps
// Authorization headers, sessionStorage, and DOM link attributes out of
// retained Playwright artifacts. Neither test ever navigates to a secret URL.
base.use({ screenshot: "only-on-failure", trace: "off", video: "off" });

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

function redactSensitiveText(value, secrets = []) {
  let safe = String(value)
    .replace(/#(?:admin|capability)=[A-Za-z0-9_-]+/gu, "#credential=<redacted>")
    .replace(/Bearer\s+[A-Za-z0-9_-]+/giu, "Bearer <redacted>");
  for (const secret of secrets) {
    if (secret) safe = safe.split(secret).join("<redacted>");
  }
  return safe.slice(-2_000);
}

function monitorBrowserErrors(page, errors, secrets) {
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(`console: ${redactSensitiveText(message.text(), secrets)}`);
    }
  });
  page.on("pageerror", (error) => {
    errors.push(`pageerror: ${redactSensitiveText(error.message, secrets)}`);
  });
}

async function reserveLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close(() => reject(new Error("could not reserve a loopback port")));
        return;
      }
      server.close((error) => {
        if (error) reject(error);
        else resolve(address.port);
      });
    });
  });
}

async function waitForExit(child, timeoutMs = 5_000) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    new Promise((resolve) => setTimeout(resolve, timeoutMs)),
  ]);
}

async function stopControlServer(server) {
  if (server.child.exitCode === null && server.child.signalCode === null) {
    server.child.kill("SIGTERM");
    await waitForExit(server.child);
  }
  if (server.child.exitCode === null && server.child.signalCode === null) {
    server.child.kill("SIGKILL");
    await waitForExit(server.child, 2_000);
  }
  await rm(server.root, { force: true, recursive: true });
}

async function startControlServer() {
  const executable = process.env.LLMOLYMPIC_WEB_E2E_CLI;
  if (!executable) throw new Error("LLMOLYMPIC_WEB_E2E_CLI is required");

  const root = await mkdtemp(join(tmpdir(), "llmolympic-web-control-e2e-"));
  const database = join(root, "control.db");
  const tokenFile = join(root, "admin.token");
  const port = await reserveLoopbackPort();
  const baseURL = `http://127.0.0.1:${port}`;
  const child = spawn(
    executable,
    [
      "web",
      "--db",
      database,
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--control-token-file",
      tokenFile,
    ],
    {
      env: {
        ...process.env,
        COLUMNS: "500",
        FORCE_COLOR: "0",
        NO_COLOR: "1",
      },
      // stdout could contain a capability if the CLI regressed. It is never
      // retained or surfaced by this fixture.
      stdio: ["ignore", "ignore", "pipe"],
    },
  );
  child.stderr.setEncoding("utf8");
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr = `${stderr}${chunk}`.slice(-8_000);
  });

  const deadline = Date.now() + CONTROL_START_TIMEOUT_MS;
  let adminToken = null;
  let lastStatus = null;
  try {
    while (Date.now() < deadline) {
      if (child.exitCode !== null || child.signalCode !== null) break;
      if (adminToken === null) {
        try {
          const candidate = (await readFile(tokenFile, "utf8")).trim();
          if (URL_SAFE_CAPABILITY.test(candidate)) adminToken = candidate;
        } catch (error) {
          if (error && error.code !== "ENOENT") throw error;
        }
      }
      if (adminToken !== null) {
        try {
          const response = await fetch(`${baseURL}/api/v1/control/catalog`, {
            headers: { Authorization: `Bearer ${adminToken}` },
            signal: AbortSignal.timeout(750),
          });
          lastStatus = response.status;
          if (response.ok) {
            return { adminToken, baseURL, child, database, root };
          }
        } catch (_error) {
          // The server may still be binding the loopback socket.
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error(
      `local Web control server did not become ready` +
        (lastStatus === null ? "" : ` (HTTP ${lastStatus})`) +
        `\nstderr: ${redactSensitiveText(stderr, [adminToken])}` +
        "\nserver stdout withheld because it may contain a capability",
    );
  } catch (error) {
    await stopControlServer({ child, root });
    throw error;
  }
}

const test = base.extend({
  controlServer: async ({}, use) => {
    const server = await startControlServer();
    try {
      await use(server);
    } finally {
      await stopControlServer(server);
    }
  },
});

async function openAdminPage(browser, server, pathname = "/new") {
  const secrets = [server.adminToken];
  const errors = [];
  const context = await browser.newContext({
    baseURL: server.baseURL,
    colorScheme: "light",
    reducedMotion: "reduce",
    viewport: { height: 900, width: 1280 },
  });
  try {
    await context.addInitScript(
      ({ key, origin, token }) => {
        if (window.location.origin === origin) window.sessionStorage.setItem(key, token);
      },
      { key: ADMIN_STORAGE_KEY, origin: server.baseURL, token: server.adminToken },
    );
  } catch (_error) {
    await context.close();
    throw new Error("browser could not stage a local admin credential");
  }
  const page = await context.newPage();
  monitorBrowserErrors(page, errors, secrets);
  await page.goto(pathname);
  expect(await page.evaluate(() => window.location.hash)).toBe("");
  return { context, errors, page, secrets };
}

async function configureMathQuiz(page, { humanName = null } = {}) {
  await expect(page.getByRole("heading", { name: "新建比赛 / 任务" })).toBeVisible();
  await page.getByLabel("比赛项目").selectOption("math_quiz");
  await page.locator("#player-0-kind").selectOption(humanName ? "human" : "mock");
  if (humanName) {
    await page.locator("#player-0-name").fill(humanName);
  } else {
    await page.locator("#player-0-strategy").selectOption("fixed");
  }
  await page.locator("#player-1-kind").selectOption("mock");
  await page.locator("#player-1-strategy").selectOption("illegal");
  await page.getByLabel("每场回合数").fill("1");
  await page.getByLabel("随机种子").fill("4242");
}

async function configureMathQuizChampionship(page) {
  await expect(page.getByRole("heading", { name: "新建比赛 / 任务" })).toBeVisible();
  await page.locator("#control-mode").selectOption("championship");
  await page.getByLabel("比赛项目").selectOption("math_quiz");
  await expect(page.locator(".roster-card")).toHaveCount(4);
  const strategies = ["random", "fixed", "illegal", "balanced"];
  for (const [index, strategy] of strategies.entries()) {
    await expect(page.locator(`#player-${index}-kind option[value="human"]`)).toHaveCount(0);
    await page.locator(`#player-${index}-kind`).selectOption("mock");
    await page.locator(`#player-${index}-strategy`).selectOption(strategy);
  }
  await page.getByLabel("每场回合数").fill("1");
  await page.getByLabel("随机种子").fill("5150");
}

async function prepareAndStart(page) {
  await page.getByRole("button", { name: "生成准备态预览" }).click();
  await expect(page.getByRole("heading", { name: "确认后才会启动" })).toBeVisible();
  await expect(page.getByText("等待确认", { exact: true })).toBeVisible();
  await expectNoWcagViolations(page);
  await page.getByRole("button", { name: "确认并启动" }).click();
  await expect(page).toHaveURL(/\/jobs\/[A-Za-z0-9._:-]+$/);
}

async function waitForCompletedJob(page) {
  await expect(page.locator(".job-status.status-completed")).toBeVisible({
    timeout: 25_000,
  });
  const archiveLink = page.getByRole("link", { name: "打开存档回放" });
  await expect(archiveLink).toBeVisible();
  const archivePath = await archiveLink.getAttribute("href");
  if (!archivePath || !/^\/matches\/[A-Za-z0-9._:-]+$/.test(archivePath)) {
    throw new Error("completed Web job did not expose a safe archive path");
  }
  return { archiveLink, archivePath };
}

async function extractParticipationCredential(page) {
  const link = page.locator(".seat-link-list a").first();
  const href = await link.first().getAttribute("href");
  if (!href) throw new Error("human Web job did not expose a participation entry");
  let parsed;
  try {
    parsed = new URL(href, "http://127.0.0.1");
  } catch (_error) {
    throw new Error("human Web job exposed an invalid participation entry");
  }
  const match = parsed.pathname.match(PARTICIPATION_PATH);
  const parameters = new URLSearchParams(parsed.hash.slice(1));
  const token = parameters.get("capability");
  if (
    !match
    || parameters.size !== 1
    || !URL_SAFE_CAPABILITY.test(token || "")
  ) {
    throw new Error("human Web job exposed an invalid participation entry");
  }
  return {
    capability: token,
    pathname: parsed.pathname,
    seatId: match[2],
    sessionId: match[1],
  };
}

test("full Web control prepares, confirms, runs, archives, and replays a mock match", async ({
  browser,
  controlServer,
}) => {
  test.setTimeout(CONTROL_TEST_TIMEOUT_MS);
  const admin = await openAdminPage(browser, controlServer);
  try {
    await configureMathQuiz(admin.page);
    await expectNoWcagViolations(admin.page);
    await prepareAndStart(admin.page);

    const { archiveLink, archivePath } = await waitForCompletedJob(admin.page);
    await expectNoWcagViolations(admin.page);

    const detailResponse = await admin.page.request.get(`/api/v1${archivePath}`);
    expect(detailResponse.ok()).toBeTruthy();
    const detail = await detailResponse.json();
    expect(detail.match).toEqual(expect.objectContaining({
      game: "math_quiz",
      players: ["mock:fixed", "mock:illegal"],
      rated: true,
    }));

    await archiveLink.click();
    await expect(admin.page).toHaveURL(new RegExp(`${archivePath}$`));
    await expect(admin.page.getByText("WebSocket 同源回放", { exact: true })).toBeVisible();
    await expectNoWcagViolations(admin.page);
    expect(admin.errors).toEqual([]);
  } finally {
    await admin.context.close();
  }
});

test("full Web control starts a browser Human seat in a second context and archives it", async ({
  browser,
  controlServer,
}) => {
  test.setTimeout(CONTROL_TEST_TIMEOUT_MS);
  const humanName = "Web 控制 E2E";
  const admin = await openAdminPage(browser, controlServer);
  let participantContext = null;
  try {
    await configureMathQuiz(admin.page, { humanName });
    await expectNoWcagViolations(admin.page);
    await prepareAndStart(admin.page);

    const seatLink = admin.page.locator(".seat-link-list a").first();
    await expect(seatLink).toBeVisible({ timeout: 10_000 });
    const participation = await extractParticipationCredential(admin.page);
    admin.secrets.push(participation.capability);

    const participantErrors = [];
    participantContext = await browser.newContext({
      baseURL: controlServer.baseURL,
      colorScheme: "light",
      reducedMotion: "reduce",
      viewport: { height: 900, width: 1280 },
    });
    try {
      await participantContext.addInitScript(
        ({ key, origin, token }) => {
          if (window.location.origin === origin) window.sessionStorage.setItem(key, token);
        },
        {
          key: `llmolympic.participation.${participation.sessionId}.${participation.seatId}`,
          origin: controlServer.baseURL,
          token: participation.capability,
        },
      );
    } catch (_error) {
      throw new Error("browser could not stage a local participation credential");
    }
    const participantPage = await participantContext.newPage();
    monitorBrowserErrors(
      participantPage,
      participantErrors,
      [controlServer.adminToken, participation.capability],
    );
    await participantPage.goto(participation.pathname);
    await expect(participantPage).toHaveURL(
      /\/participate\/[A-Za-z0-9._:-]+\/[A-Za-z0-9._:-]+$/,
    );
    expect(await participantPage.evaluate(() => window.location.hash)).toBe("");
    await expect(participantPage.getByRole("heading", { name: humanName })).toBeVisible();
    await expect(participantPage.locator(".participation-prompt")).toContainText("数学问答");
    await expectNoWcagViolations(participantPage);

    await participantPage.getByLabel("输入动作或答案").fill("2");
    const submission = participantPage.waitForResponse(
      (response) => response.request().method() === "POST"
        && response.url().includes("/submissions"),
    );
    await participantPage.getByRole("button", { name: "提交本轮输入" }).click();
    expect((await submission).status()).toBe(202);
    await expect(
      participantPage.getByRole("heading", { name: "比赛已完成" }),
    ).toBeVisible({ timeout: 20_000 });
    await expect.poll(
      () => participantPage.evaluate(() => window.sessionStorage.length),
    ).toBe(0);

    const { archiveLink, archivePath } = await waitForCompletedJob(admin.page);
    const detailResponse = await admin.page.request.get(`/api/v1${archivePath}`);
    expect(detailResponse.ok()).toBeTruthy();
    const detail = await detailResponse.json();
    expect(detail.match).toEqual(expect.objectContaining({
      game: "math_quiz",
      players: [humanName, "mock:illegal"],
      rated: true,
    }));
    if (JSON.stringify(detail).includes(participation.capability)) {
      throw new Error("archive unexpectedly retained a participation capability");
    }

    await archiveLink.click();
    await expect(admin.page).toHaveURL(new RegExp(`${archivePath}$`));
    await expect(admin.page.getByText("WebSocket 同源回放", { exact: true })).toBeVisible();
    await expectNoWcagViolations(admin.page);
    expect(admin.errors).toEqual([]);
    expect(participantErrors).toEqual([]);
  } finally {
    if (participantContext !== null) await participantContext.close();
    await admin.context.close();
  }
});

test("full Web control runs a four-player championship and exposes its final live bracket", async ({
  browser,
  controlServer,
}) => {
  test.setTimeout(CONTROL_TEST_TIMEOUT_MS);
  const admin = await openAdminPage(browser, controlServer);
  try {
    await configureMathQuizChampionship(admin.page);
    await expectNoWcagViolations(admin.page);
    await prepareAndStart(admin.page);

    await expect(admin.page.locator(".job-status.status-completed")).toBeVisible({
      timeout: 40_000,
    });
    const archiveLinks = admin.page.locator(".archive-button-list a");
    await expect(archiveLinks).toHaveCount(6);
    const liveLink = admin.page.getByRole("link", { name: "打开实时观战" });
    await expect(liveLink).toBeVisible();
    const livePath = await liveLink.getAttribute("href");
    if (!livePath || !/^\/live\/[A-Za-z0-9._:-]+$/.test(livePath)) {
      throw new Error("completed championship did not expose a safe live path");
    }

    const jobId = new URL(admin.page.url()).pathname.split("/").pop();
    const jobResponse = await admin.page.request.get(
      `${controlServer.baseURL}/api/v1/control/jobs/${encodeURIComponent(jobId)}`,
      { headers: { Authorization: `Bearer ${controlServer.adminToken}` } },
    );
    expect(jobResponse.ok()).toBeTruthy();
    const { job } = await jobResponse.json();
    expect(job).toEqual(expect.objectContaining({
      championship_id: expect.any(String),
      final_kind: "championship",
      resumable: false,
      status: "completed",
    }));
    expect(job.spec).toEqual(expect.objectContaining({
      game: "math_quiz",
      mode: "championship",
      resume_championship_id: null,
    }));
    expect(job.spec.players).toHaveLength(4);
    expect(job.final_match_ids).toHaveLength(6);

    const liveResponse = await admin.page.request.get(`/api/v1${livePath}`);
    expect(liveResponse.ok()).toBeTruthy();
    const liveDetail = await liveResponse.json();
    expect(liveDetail.match).toEqual(expect.objectContaining({
      final_id: job.championship_id,
      final_kind: "championship",
      mode: "championship",
      status: "completed",
    }));
    expect(liveDetail.match.championship_bracket).toEqual(expect.objectContaining({
      champion: expect.any(String),
      championship_id: job.championship_id,
      pairing_count: 3,
      player_count: 4,
    }));
    expect(liveDetail.match.championship_bracket.pairings).toHaveLength(3);

    await liveLink.click();
    await expect(admin.page).toHaveURL(new RegExp(`${livePath}$`));
    await expect(admin.page.getByRole("heading", { name: "淘汰赛对阵" })).toBeVisible();
    await expect(admin.page.locator(".championship-pairing")).toHaveCount(3);
    await expect(admin.page.locator(".championship-champion")).toContainText(
      liveDetail.match.championship_bracket.champion,
    );
    await expect(admin.page.locator(".archive-actions a")).toHaveCount(6);
    await expectNoWcagViolations(admin.page);
    expect(admin.errors).toEqual([]);
  } finally {
    await admin.context.close();
  }
});
