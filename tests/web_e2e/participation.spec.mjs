import { spawn } from "node:child_process";

import { expect, test } from "@playwright/test";

const HUMAN_NAME = "浏览器 E2E";
const OPPONENT_NAME = "mock:illegal";
const PARTICIPATION_LINK_RE =
  /http:\/\/(?:127\.0\.0\.1|localhost|\[::1\]):\d+\/participate\/[a-f0-9]{32}\/[a-f0-9]{32}#capability=[A-Za-z0-9_-]{43}/;
const ANSI_ESCAPE_RE = /\u001b\[[0-?]*[ -/]*[@-~]/g;
const CAPABILITY_RE = /#capability=[A-Za-z0-9_-]*/g;
const CAPABILITY_TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;

// A real capability must never be retained in a failure trace. Fragment capture
// and immediate URL clearing are covered by observer.spec.mjs with a fake token.
test.use({ trace: "off" });

function compactRichOutput(value) {
  return value.replace(ANSI_ESCAPE_RE, "").replace(/[\s│]/gu, "");
}

function failureDetails(stderr) {
  const safeStderr = compactRichOutput(stderr).replace(
    CAPABILITY_RE,
    "#capability=<redacted>",
  );
  const detail = safeStderr ? `\nstderr(compact): ${safeStderr}` : "";
  return `${detail}\nCLI stdout withheld because it may contain a participation capability.`;
}

function startBrowserMatch() {
  const executable = process.env.LLMOLYMPIC_WEB_E2E_CLI;
  const database = process.env.LLMOLYMPIC_WEB_E2E_DB;
  const webURL = process.env.LLMOLYMPIC_WEB_E2E_URL;
  if (!executable || !database || !webURL) {
    throw new Error(
      "LLMOLYMPIC_WEB_E2E_CLI, LLMOLYMPIC_WEB_E2E_DB, and " +
        "LLMOLYMPIC_WEB_E2E_URL are required",
    );
  }

  const child = spawn(
    executable,
    [
      "play",
      "--game",
      "gomoku",
      "--players",
      `human:${HUMAN_NAME},${OPPONENT_NAME}`,
      "--seed",
      "4242",
      "--human-input",
      "web",
      "--web-url",
      webURL,
      "--timeout",
      "30",
      "--db",
      database,
    ],
    {
      env: {
        ...process.env,
        COLUMNS: "500",
        FORCE_COLOR: "0",
        NO_COLOR: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  let stdout = "";
  let stderr = "";
  let linkResolved = false;
  let resolveLink;
  let rejectLink;
  const participation = new Promise((resolve, reject) => {
    resolveLink = resolve;
    rejectLink = reject;
  });
  const linkTimeout = setTimeout(() => {
    if (linkResolved) return;
    linkResolved = true;
    rejectLink(
      new Error(
        "timed out waiting for the CLI participation link\n" +
          failureDetails(stderr),
      ),
    );
  }, 10_000);

  function inspectOutput() {
    if (linkResolved) return;
    const match = compactRichOutput(stdout).match(PARTICIPATION_LINK_RE);
    if (!match) return;
    const parsed = new URL(match[0]);
    const route = parsed.pathname.match(
      /^\/participate\/([a-f0-9]{32})\/([a-f0-9]{32})$/,
    );
    const capability = new URLSearchParams(parsed.hash.slice(1)).get("capability");
    if (!route || !capability || !CAPABILITY_TOKEN_RE.test(capability)) return;
    linkResolved = true;
    clearTimeout(linkTimeout);
    resolveLink({
      capability,
      pathname: parsed.pathname,
      seatId: route[2],
      sessionId: route[1],
    });
  }

  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
    inspectOutput();
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });

  const exited = new Promise((resolve) => {
    child.once("error", (error) => {
      if (!linkResolved) {
        linkResolved = true;
        clearTimeout(linkTimeout);
        rejectLink(error);
      }
      resolve({ code: null, signal: null, error });
    });
    child.once("exit", (code, signal) => {
      if (!linkResolved) {
        linkResolved = true;
        clearTimeout(linkTimeout);
        rejectLink(
          new Error(
            `CLI exited before publishing a participation link (${code ?? signal})\n` +
              failureDetails(stderr),
          ),
        );
      }
      resolve({ code, signal, error: null });
    });
  });

  return {
    child,
    exited,
    participation,
    output: () => failureDetails(stderr),
  };
}

async function waitForSuccessfulExit(run) {
  let timeoutId;
  const timeout = new Promise((_, reject) => {
    timeoutId = setTimeout(
      () => reject(new Error("timed out waiting for the CLI match")),
      20_000,
    );
  });
  let result;
  try {
    result = await Promise.race([run.exited, timeout]);
  } finally {
    clearTimeout(timeoutId);
  }
  if (result.error || result.code !== 0) {
    throw new Error(
      `CLI match failed (${result.error?.message ?? result.code ?? result.signal})\n` +
        run.output(),
    );
  }
}

test("browser submission is rejected, retried, accepted, archived, and rated", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const browserErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));

  const run = startBrowserMatch();
  try {
    const { capability, pathname, seatId, sessionId } = await run.participation;
    const storageKey = `llmolympic.participation.${sessionId}.${seatId}`;
    try {
      await page.addInitScript(
        ({ key, path, value }) => {
          if (window.location.pathname === path) {
            window.sessionStorage.setItem(key, value);
          }
        },
        { key: storageKey, path: pathname, value: capability },
      );
    } catch (_error) {
      throw new Error("browser could not stage the local participation credential");
    }
    try {
      await page.goto(pathname);
    } catch (_error) {
      throw new Error("browser could not open the local participation page");
    }

    await expect(page).toHaveURL(/\/participate\/[a-f0-9]{32}\/[a-f0-9]{32}$/);
    expect(await page.evaluate(() => window.location.hash)).toBe("");
    expect(await page.evaluate(() => window.sessionStorage.length)).toBe(1);
    await expect(page.getByRole("heading", { name: HUMAN_NAME })).toBeVisible();
    await expect(page.locator(".participation-prompt")).toContainText("五子棋");
    await expect(page.locator(".panel-kicker")).toHaveText("输入请求 0");

    const move = page.getByLabel("输入动作或答案");
    await move.fill("Z99");
    const rejectedSubmission = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/submissions"),
    );
    await page.getByRole("button", { name: "提交本轮输入" }).click();
    expect((await rejectedSubmission).status()).toBe(202);

    await expect(page.locator(".panel-kicker")).toHaveText("输入请求 1");
    await expect(page.locator(".participation-prompt")).toContainText(
      "上次输出 'Z99' 未被接受",
    );
    await move.fill("H8");
    const acceptedSubmission = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/submissions"),
    );
    await page.getByRole("button", { name: "提交本轮输入" }).click();
    expect((await acceptedSubmission).status()).toBe(202);

    await expect(page.getByRole("heading", { name: "比赛已完成" })).toBeVisible({
      timeout: 15_000,
    });
    await expect.poll(() => page.evaluate(() => window.sessionStorage.length)).toBe(0);
    const archiveLink = page.getByRole("link", { name: "打开存档回放" });
    await expect(archiveLink).toBeVisible();
    const archivePath = await archiveLink.getAttribute("href");
    expect(archivePath).toMatch(/^\/matches\/[A-Za-z0-9._:-]+$/);

    await waitForSuccessfulExit(run);

    const detailResponse = await page.request.get(
      `/api/v1${archivePath}`,
    );
    expect(detailResponse.ok()).toBeTruthy();
    const detail = await detailResponse.json();
    expect(detail.match.game).toBe("gomoku");
    expect(detail.match.players).toEqual([HUMAN_NAME, OPPONENT_NAME]);
    expect(detail.match.rated).toBe(true);
    expect(detail.match.scores).toEqual({
      [HUMAN_NAME]: 1,
      [OPPONENT_NAME]: 0,
    });
    expect(detail.events).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: "move_rejected",
          player: HUMAN_NAME,
          data: expect.objectContaining({
            move: "Z99",
            reason_code: "illegal_move",
            forfeit: false,
          }),
        }),
        expect.objectContaining({
          type: "move_received",
          player: HUMAN_NAME,
          data: { move: "H8" },
        }),
        expect.objectContaining({
          type: "move_rejected",
          player: OPPONENT_NAME,
          data: expect.objectContaining({
            reason_code: "illegal_move_limit",
            forfeit: true,
            technical_loss: true,
          }),
        }),
      ]),
    );

    const leaderboardResponse = await page.request.get(
      "/api/v1/leaderboard?game=gomoku",
    );
    expect(leaderboardResponse.ok()).toBeTruthy();
    const leaderboard = await leaderboardResponse.json();
    const humanRating = leaderboard.entries.find((entry) => entry.player === HUMAN_NAME);
    const opponentRating = leaderboard.entries.find(
      (entry) => entry.player === OPPONENT_NAME,
    );
    expect(humanRating).toEqual(
      expect.objectContaining({ games_played: expect.any(Number), wins: expect.any(Number) }),
    );
    expect(humanRating.games_played).toBeGreaterThanOrEqual(1);
    expect(humanRating.wins).toBeGreaterThanOrEqual(1);
    expect(humanRating.rating).toBeGreaterThan(1500);
    expect(opponentRating).toEqual(
      expect.objectContaining({ games_played: expect.any(Number), losses: expect.any(Number) }),
    );
    expect(opponentRating.games_played).toBeGreaterThanOrEqual(1);
    expect(opponentRating.losses).toBeGreaterThanOrEqual(1);
    expect(opponentRating.rating).toBeLessThan(1500);

    await archiveLink.click();
    await expect(page).toHaveURL(new RegExp(`${archivePath}$`));
    await expect(page.getByText("WebSocket 同源回放", { exact: true })).toBeVisible();
    expect(browserErrors).toEqual([]);
  } finally {
    if (
      run.child.pid &&
      run.child.exitCode === null &&
      run.child.signalCode === null
    ) {
      run.child.kill("SIGTERM");
      await run.exited;
    }
  }
});
