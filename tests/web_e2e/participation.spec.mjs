import { spawn } from "node:child_process";

import { expect, test } from "@playwright/test";

const HUMAN_NAME = "浏览器 E2E";
const OPPONENT_NAME = "mock:illegal";
const MULTI_HUMAN_NAMES = ["甲", "乙"];
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

function startBrowserMatch({
  expectedSeats = 1,
  game = "gomoku",
  players = `human:${HUMAN_NAME},${OPPONENT_NAME}`,
  rounds = null,
  seed = 4242,
} = {}) {
  const executable = process.env.LLMOLYMPIC_WEB_E2E_CLI;
  const database = process.env.LLMOLYMPIC_WEB_E2E_DB;
  const webURL = process.env.LLMOLYMPIC_WEB_E2E_URL;
  if (!executable || !database || !webURL) {
    throw new Error(
      "LLMOLYMPIC_WEB_E2E_CLI, LLMOLYMPIC_WEB_E2E_DB, and " +
        "LLMOLYMPIC_WEB_E2E_URL are required",
    );
  }
  if (!Number.isInteger(expectedSeats) || expectedSeats < 1) {
    throw new Error("expectedSeats must be a positive integer");
  }

  const args = ["play", "--game", game, "--players", players];
  if (rounds !== null) args.push("--rounds", String(rounds));
  args.push(
    "--seed",
    String(seed),
    "--human-input",
    "web",
    "--web-url",
    webURL,
    "--timeout",
    "30",
    "--db",
    database,
  );

  const child = spawn(
    executable,
    args,
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
  const participations = new Promise((resolve, reject) => {
    resolveLink = resolve;
    rejectLink = reject;
  });
  const linkTimeout = setTimeout(() => {
    if (linkResolved) return;
    linkResolved = true;
    rejectLink(
      new Error(
        "timed out waiting for the CLI participation links\n" +
          failureDetails(stderr),
      ),
    );
  }, 10_000);

  function inspectOutput() {
    if (linkResolved) return;
    const links = [];
    const seenPaths = new Set();
    const seenCapabilities = new Set();
    const matches = compactRichOutput(stdout).matchAll(
      new RegExp(PARTICIPATION_LINK_RE.source, "g"),
    );
    for (const match of matches) {
      const parsed = new URL(match[0]);
      const route = parsed.pathname.match(
        /^\/participate\/([a-f0-9]{32})\/([a-f0-9]{32})$/,
      );
      const capability = new URLSearchParams(parsed.hash.slice(1)).get("capability");
      if (!route || !capability || !CAPABILITY_TOKEN_RE.test(capability)) continue;
      if (seenPaths.has(parsed.pathname) || seenCapabilities.has(capability)) continue;
      seenPaths.add(parsed.pathname);
      seenCapabilities.add(capability);
      links.push({
        capability,
        pathname: parsed.pathname,
        seatId: route[2],
        sessionId: route[1],
      });
    }
    if (links.length < expectedSeats) return;
    linkResolved = true;
    clearTimeout(linkTimeout);
    stdout = "";
    resolveLink(links.slice(0, expectedSeats));
  }

  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    if (linkResolved) return;
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
    participations,
    ...(expectedSeats === 1
      ? { participation: participations.then(([participation]) => participation) }
      : {}),
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

async function openParticipationPage(context, participation, browserErrors) {
  const storageKey =
    `llmolympic.participation.${participation.sessionId}.${participation.seatId}`;
  try {
    await context.addInitScript(
      ({ key, path, value }) => {
        if (window.location.pathname === path) {
          window.sessionStorage.setItem(key, value);
        }
      },
      {
        key: storageKey,
        path: participation.pathname,
        value: participation.capability,
      },
    );
  } catch (_error) {
    throw new Error("browser could not stage a local participation credential");
  }
  const page = await context.newPage();
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  try {
    await page.goto(participation.pathname);
  } catch (_error) {
    throw new Error("browser could not open a local participation page");
  }
  return page;
}

async function crossCapabilityStatus(context, target, foreignCapability) {
  try {
    const response = await context.request.get(
      `/api/v1/participation/${target.sessionId}/${target.seatId}`,
      { headers: { Authorization: `Bearer ${foreignCapability}` } },
    );
    return response.status();
  } catch (_error) {
    throw new Error("browser could not verify cross-seat capability isolation");
  }
}

async function readRawMatchArchive(matchId) {
  const executable = process.env.LLMOLYMPIC_WEB_E2E_CLI;
  const database = process.env.LLMOLYMPIC_WEB_E2E_DB;
  if (!executable || !database) {
    throw new Error("LLMOLYMPIC_WEB_E2E_CLI and LLMOLYMPIC_WEB_E2E_DB are required");
  }
  return new Promise((resolve, reject) => {
    const child = spawn(executable, ["archive", matchId, "--db", database], {
      env: {
        ...process.env,
        COLUMNS: "100000",
        FORCE_COLOR: "0",
        NO_COLOR: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let spawnError = null;
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.once("error", (error) => {
      spawnError = error;
    });
    child.once("close", (code, signal) => {
      if (spawnError || code !== 0) {
        stdout = "";
        reject(
          new Error(
            `archive CLI failed (${spawnError?.message ?? code ?? signal})\n` +
              failureDetails(stderr),
          ),
        );
        return;
      }
      try {
        const archive = JSON.parse(
          stdout.replace(ANSI_ESCAPE_RE, "").replace(/\r?\n/g, ""),
        );
        stdout = "";
        resolve(archive);
      } catch (_error) {
        stdout = "";
        reject(new Error("archive CLI returned invalid JSON"));
      }
    });
  });
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

test("two browser humans use isolated seats and complete one rated archive", async ({
  baseURL,
  browser,
}) => {
  test.setTimeout(60_000);
  const run = startBrowserMatch({
    expectedSeats: 2,
    game: "math_quiz",
    players: "human:甲,human:乙",
    rounds: 1,
  });
  const contexts = [];
  const browserErrors = [[], []];

  try {
    const participations = await run.participations;
    expect(participations.length).toBe(2);
    expect(new Set(participations.map(({ pathname }) => pathname)).size).toBe(2);
    expect(new Set(participations.map(({ capability }) => capability)).size).toBe(2);

    for (let index = 0; index < 2; index += 1) {
      const context = await browser.newContext({
        baseURL,
        colorScheme: "light",
        reducedMotion: "reduce",
        viewport: { height: 900, width: 1280 },
      });
      contexts.push(context);
    }

    const crossCapabilityStatuses = await Promise.all([
      crossCapabilityStatus(
        contexts[0],
        participations[0],
        participations[1].capability,
      ),
      crossCapabilityStatus(
        contexts[1],
        participations[1],
        participations[0].capability,
      ),
    ]);
    expect(crossCapabilityStatuses).toEqual([404, 404]);

    const pages = await Promise.all(
      contexts.map((context, index) =>
        openParticipationPage(
          context,
          participations[index],
          browserErrors[index],
        ),
      ),
    );

    await Promise.all(
      pages.map(async (page, index) => {
        await expect(page).toHaveURL(
          /\/participate\/[a-f0-9]{32}\/[a-f0-9]{32}$/,
        );
        expect(await page.evaluate(() => window.location.hash)).toBe("");
        expect(await page.evaluate(() => window.sessionStorage.length)).toBe(1);
        await expect(
          page.getByRole("heading", { name: MULTI_HUMAN_NAMES[index] }),
        ).toBeVisible();
        await expect(page.locator(".participation-prompt")).toContainText("数学问答");
        await expect(page.locator(".panel-kicker")).toHaveText("输入请求 0");
      }),
    );

    await Promise.all(
      pages.map(async (page, index) => {
        await page.getByLabel("输入动作或答案").fill(index === 0 ? "2" : "0");
        const submitted = page.waitForResponse(
          (response) =>
            response.request().method() === "POST" &&
            response.url().includes("/submissions"),
        );
        await page.getByRole("button", { name: "提交本轮输入" }).click();
        expect((await submitted).status()).toBe(202);
      }),
    );

    await Promise.all(
      pages.map(async (page) => {
        await expect(page.getByRole("heading", { name: "比赛已完成" })).toBeVisible({
          timeout: 15_000,
        });
        await expect
          .poll(() => page.evaluate(() => window.sessionStorage.length))
          .toBe(0);
      }),
    );

    const archiveLinks = pages.map((page) =>
      page.getByRole("link", { name: "打开存档回放" }),
    );
    await Promise.all(archiveLinks.map((link) => expect(link).toBeVisible()));
    const archivePaths = await Promise.all(
      archiveLinks.map((link) => link.getAttribute("href")),
    );
    expect(archivePaths[0]).toMatch(/^\/matches\/[A-Za-z0-9._:-]+$/);
    expect(archivePaths[1]).toBe(archivePaths[0]);

    await waitForSuccessfulExit(run);

    const matchId = archivePaths[0].slice("/matches/".length);
    const rawArchive = await readRawMatchArchive(matchId);
    expect(rawArchive.match_id).toBe(matchId);
    expect(
      rawArchive.players.map(({ kind, name }) => ({ kind, name })),
    ).toEqual([
      { kind: "human", name: "甲" },
      { kind: "human", name: "乙" },
    ]);

    const detailResponse = await contexts[0].request.get(`/api/v1${archivePaths[0]}`);
    expect(detailResponse.ok()).toBeTruthy();
    const detail = await detailResponse.json();
    expect(detail.match.game).toBe("math_quiz");
    expect(detail.match.players).toEqual(MULTI_HUMAN_NAMES);
    expect(detail.match.rated).toBe(true);
    expect(detail.match.scores).toEqual({ 甲: 1, 乙: 0 });
    expect(detail.events).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: "match_started",
          data: expect.objectContaining({ players: MULTI_HUMAN_NAMES }),
        }),
        expect.objectContaining({
          type: "move_received",
          player: "甲",
          data: { move: "2" },
        }),
        expect.objectContaining({
          type: "move_received",
          player: "乙",
          data: { move: "0" },
        }),
      ]),
    );
    const serializedArchive = JSON.stringify(detail);
    expect(
      participations.some(({ capability }) => serializedArchive.includes(capability)),
    ).toBe(false);

    const leaderboardResponse = await contexts[0].request.get(
      "/api/v1/leaderboard?game=math_quiz",
    );
    expect(leaderboardResponse.ok()).toBeTruthy();
    const leaderboard = await leaderboardResponse.json();
    const winner = leaderboard.entries.find((entry) => entry.player === "甲");
    const loser = leaderboard.entries.find((entry) => entry.player === "乙");
    expect(winner).toEqual(
      expect.objectContaining({ games_played: expect.any(Number), wins: expect.any(Number) }),
    );
    expect(winner.games_played).toBeGreaterThanOrEqual(1);
    expect(winner.wins).toBeGreaterThanOrEqual(1);
    expect(winner.rating).toBeGreaterThan(1500);
    expect(loser).toEqual(
      expect.objectContaining({ games_played: expect.any(Number), losses: expect.any(Number) }),
    );
    expect(loser.games_played).toBeGreaterThanOrEqual(1);
    expect(loser.losses).toBeGreaterThanOrEqual(1);
    expect(loser.rating).toBeLessThan(1500);
    expect(browserErrors).toEqual([[], []]);
  } finally {
    await Promise.all(
      contexts.map(async (context) => {
        try {
          await context.close();
        } catch (_error) {
          // Best-effort cleanup must not mask the original assertion failure.
        }
      }),
    );
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
