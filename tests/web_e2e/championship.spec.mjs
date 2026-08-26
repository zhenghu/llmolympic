import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const ADMIN_STORAGE_KEY = "llmolympic.control.admin";
const ADMIN_TOKEN = "C".repeat(43);
const CHAMPIONSHIP_ID = "championship-web-e2e";
const LIVE_ID = "championship-live-e2e";
const PLAYERS = ["Alpha", "Bravo", "Charlie", "Delta"];
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

// Control capabilities are credentials even when they are deterministic test
// values. Do not retain them in traces or videos.
test.use({ screenshot: "only-on-failure", trace: "off", video: "off" });

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

function championshipContext(roundNumber, roundPairingNumber, pairingNumber, legNumber = 2) {
  return {
    leg_number: legNumber,
    pairing_count: 3,
    pairing_number: pairingNumber,
    round_count: 2,
    round_number: roundNumber,
    round_pairing_count: 4 >> roundNumber,
    round_pairing_number: roundPairingNumber,
  };
}

function championshipPairing({
  pairingNumber,
  players,
  roundNumber,
  roundPairingNumber,
  status,
  winner,
}) {
  return {
    match_ids: [`championship-match-${pairingNumber}-1`, `championship-match-${pairingNumber}-2`],
    pairing_number: pairingNumber,
    players,
    round_number: roundNumber,
    round_pairing_number: roundPairingNumber,
    series_id: `championship-series-${pairingNumber}`,
    status,
    winner,
  };
}

const COMPLETE_PAIRINGS = [
  championshipPairing({
    pairingNumber: 1,
    players: [PLAYERS[0], PLAYERS[1]],
    roundNumber: 1,
    roundPairingNumber: 1,
    status: "committed",
    winner: PLAYERS[0],
  }),
  championshipPairing({
    pairingNumber: 2,
    players: [PLAYERS[2], PLAYERS[3]],
    roundNumber: 1,
    roundPairingNumber: 2,
    status: "committed",
    winner: PLAYERS[2],
  }),
  championshipPairing({
    pairingNumber: 3,
    players: [PLAYERS[0], PLAYERS[2]],
    roundNumber: 2,
    roundPairingNumber: 1,
    status: "committed",
    winner: PLAYERS[0],
  }),
];

function lifecyclePairing(pairing, seq) {
  return {
    context: championshipContext(
      pairing.round_number,
      pairing.round_pairing_number,
      pairing.pairing_number,
    ),
    kind: "pairing_completed",
    pairing: { ...pairing, status: "provisional" },
    seq,
  };
}

function lifecycleCommit({ context, pairingNumbers, seq }) {
  return {
    context,
    kind: "round_committed",
    pairing_numbers: pairingNumbers,
    seq,
  };
}

const COMPLETE_EVENTS = [
  lifecyclePairing(COMPLETE_PAIRINGS[0], 0),
  lifecyclePairing(COMPLETE_PAIRINGS[1], 1),
  lifecycleCommit({
    context: championshipContext(1, 2, 2),
    pairingNumbers: [1, 2],
    seq: 2,
  }),
  lifecyclePairing(COMPLETE_PAIRINGS[2], 3),
  lifecycleCommit({
    context: championshipContext(2, 1, 3),
    pairingNumbers: [3],
    seq: 4,
  }),
];

function championshipSummary({ completed, pairings, current }) {
  const finalMatchIds = completed
    ? pairings.flatMap((pairing) => pairing.match_ids)
    : [];
  return {
    championship_bracket: {
      champion: completed ? PLAYERS[0] : null,
      championship_id: CHAMPIONSHIP_ID,
      pairing_count: 3,
      pairings,
      player_count: 4,
      round_count: 2,
    },
    event_count: completed ? COMPLETE_EVENTS.length : 1,
    final_id: completed ? CHAMPIONSHIP_ID : null,
    final_kind: completed ? "championship" : null,
    final_match_ids: finalMatchIds,
    game: "math_quiz",
    leg_number: current.leg_number,
    live_id: LIVE_ID,
    mode: "championship",
    pairing_count: 3,
    pairing_number: current.pairing_number,
    players: PLAYERS,
    round_count: 2,
    round_number: current.round_number,
    round_pairing_count: current.round_pairing_count,
    round_pairing_number: current.round_pairing_number,
    started_at: "2026-08-25T10:00:00Z",
    status: completed ? "completed" : "running",
    updated_at: "2026-08-25T10:01:00Z",
  };
}

async function routeLiveDetail(page, summary, events) {
  await page.route(`**/api/v1/live/${LIVE_ID}**`, async (route) => {
    const url = new URL(route.request().url());
    const fromSeq = Number(url.searchParams.get("from_seq") || "0");
    const pageEvents = events.slice(fromSeq);
    await route.fulfill({
      contentType: "application/json",
      json: {
        api_version: "v1",
        events: pageEvents,
        has_more: false,
        match: summary,
        next_seq: fromSeq + pageEvents.length,
      },
      status: 200,
    });
  });
  await page.addInitScript(() => {
    delete globalThis.WebSocket;
  });
}

function controlBudget() {
  return {
    max_estimated_cost_usd: null,
    max_input_tokens: null,
    max_output_tokens_per_call: "4096",
    max_provider_calls: null,
    max_total_output_tokens: null,
  };
}

function originalChampionshipSpec() {
  return {
    allow_large_tournament: false,
    budget: controlBudget(),
    game: "math_quiz",
    human_timeout_seconds: 120,
    judges: [],
    llm_timeout_seconds: 120,
    mode: "championship",
    players: ["random", "fixed", "illegal", "balanced"].map((strategy) => ({
      kind: "mock",
      strategy,
    })),
    resume_championship_id: null,
    resume_tournament_id: null,
    rounds: 1,
    seed: "42",
  };
}

function resumeChampionshipSpec() {
  return {
    allow_large_tournament: false,
    budget: {
      max_estimated_cost_usd: null,
      max_input_tokens: null,
      max_output_tokens_per_call: null,
      max_provider_calls: null,
      max_total_output_tokens: null,
    },
    game: "",
    human_timeout_seconds: 120,
    judges: [],
    llm_timeout_seconds: null,
    mode: "championship",
    players: [],
    resume_championship_id: CHAMPIONSHIP_ID,
    resume_tournament_id: null,
    rounds: null,
    seed: "0",
  };
}

function championshipPreview({ frozen }) {
  return {
    frozen_game: frozen ? "math_quiz" : null,
    frozen_judges: [],
    frozen_llm_timeout_seconds: frozen ? 120 : null,
    frozen_players: frozen ? PLAYERS : [],
    frozen_rounds: frozen ? 1 : null,
    frozen_seed: frozen ? "42" : null,
    human_count: 0,
    match_count: 6,
    pairing_count: 3,
    player_count: 4,
    prepared_profiles: [],
    rated: false,
    requires_provider_budget: false,
    uses_frozen_budget: false,
    warnings: frozen ? ["resume_uses_frozen_configuration"] : [],
  };
}

function controlJob({ jobId, preparedResume = false }) {
  return {
    championship_id: preparedResume ? null : CHAMPIONSHIP_ID,
    created_at: "2026-08-25T10:00:00Z",
    failure_code: null,
    final_id: null,
    final_kind: null,
    final_match_ids: [],
    finished_at: preparedResume ? null : "2026-08-25T10:01:00Z",
    job_id: jobId,
    live_id: null,
    participation_links: [],
    preview: championshipPreview({ frozen: preparedResume }),
    resumable: !preparedResume,
    spec: preparedResume ? resumeChampionshipSpec() : originalChampionshipSpec(),
    started_at: preparedResume ? null : "2026-08-25T10:00:01Z",
    status: preparedResume ? "prepared" : "interrupted",
    tournament_id: null,
    updated_at: "2026-08-25T10:01:00Z",
  };
}

test("completed championship renders authoritative committed bracket and archives", async ({ page }) => {
  const summary = championshipSummary({
    completed: true,
    current: championshipContext(2, 1, 3),
    pairings: COMPLETE_PAIRINGS,
  });
  await routeLiveDetail(page, summary, COMPLETE_EVENTS);

  await page.goto(`/live/${LIVE_ID}`);
  await expect(page.getByRole("heading", { name: "淘汰赛对阵" })).toBeVisible();
  await expect(page.locator(".championship-bracket")).toBeVisible();
  await expect(page.locator('.championship-pairing[data-status="committed"]')).toHaveCount(3);
  await expect(page.locator(".championship-champion")).toContainText(PLAYERS[0]);
  await expect(page.getByText("比赛已完成并存档", { exact: true })).toBeVisible();
  await expect(page.locator(".archive-actions a")).toHaveCount(6);
  await expectNoWcagViolations(page);
});

test("running championship exposes provisional result and current pairing without a champion", async ({ page }) => {
  const provisional = { ...COMPLETE_PAIRINGS[0], status: "provisional" };
  const current = championshipContext(1, 2, 2, 1);
  const summary = championshipSummary({ completed: false, current, pairings: [provisional] });
  await routeLiveDetail(page, summary, [lifecyclePairing(COMPLETE_PAIRINGS[0], 0)]);

  await page.goto(`/live/${LIVE_ID}`);
  await expect(page.locator('.championship-pairing[data-status="provisional"]')).toHaveCount(1);
  const currentPairing = page.locator(".championship-current");
  await expect(currentPairing).toContainText("当前对阵");
  await expect(currentPairing).toContainText(PLAYERS[2]);
  await expect(currentPairing).toContainText(PLAYERS[3]);
  await expect(page.locator(".championship-champion")).toHaveCount(0);
  await expectNoWcagViolations(page);
});

test("materialized or interrupted championship pairing is not mislabeled as current", async ({ page }) => {
  const provisional = { ...COMPLETE_PAIRINGS[0], status: "provisional" };
  const summary = {
    ...championshipSummary({
      completed: false,
      current: championshipContext(1, 1, 1),
      pairings: [provisional],
    }),
    status: "interrupted",
  };
  await routeLiveDetail(page, summary, [lifecyclePairing(COMPLETE_PAIRINGS[0], 0)]);

  await page.goto(`/live/${LIVE_ID}`);
  await expect(page.locator(".live-stage strong").getByText("直播已中断", { exact: true })).toBeVisible();
  await expect(page.locator('.championship-pairing[data-status="provisional"]')).toHaveCount(1);
  await expect(page.locator(".championship-current")).toHaveCount(0);
  await expect(page.locator(".championship-champion")).toHaveCount(0);
  await expectNoWcagViolations(page);
});

test("championship bracket remains usable on a narrow mobile viewport", async ({ page }) => {
  await page.setViewportSize({ height: 844, width: 390 });
  const summary = championshipSummary({
    completed: true,
    current: championshipContext(2, 1, 3),
    pairings: COMPLETE_PAIRINGS,
  });
  await routeLiveDetail(page, summary, COMPLETE_EVENTS);

  await page.goto(`/live/${LIVE_ID}`);
  await expect(page.locator(".championship-bracket")).toBeVisible();
  await expect(page.locator(".championship-pairing")).toHaveCount(3);
  await expect(page.locator(".championship-champion")).toContainText(PLAYERS[0]);
  const documentWidth = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(documentWidth.scroll).toBeLessThanOrEqual(documentWidth.client + 1);
  await expectNoWcagViolations(page);
});

test("interrupted championship prepares a frozen resume request before restart", async ({ page }) => {
  let resumeBody = null;
  await page.route("**/api/v1/control/jobs", async (route) => {
    const request = route.request();
    expect(request.method()).toBe("POST");
    expect(request.headers().authorization).toBe(`Bearer ${ADMIN_TOKEN}`);
    resumeBody = request.postDataJSON();
    await route.fulfill({
      contentType: "application/json",
      json: { api_version: "v1", job: controlJob({ jobId: "championship-resume-prepared", preparedResume: true }) },
      status: 201,
    });
  });
  await page.route("**/api/v1/control/jobs/championship-resume-source", async (route) => {
    expect(route.request().headers().authorization).toBe(`Bearer ${ADMIN_TOKEN}`);
    await route.fulfill({
      contentType: "application/json",
      json: { api_version: "v1", job: controlJob({ jobId: "championship-resume-source" }) },
      status: 200,
    });
  });

  await page.goto(`/jobs/championship-resume-source#admin=${ADMIN_TOKEN}`);
  await expect(page).toHaveURL(/\/jobs\/championship-resume-source$/);
  expect(await page.evaluate(() => window.location.hash)).toBe("");
  expect(await page.evaluate(
    (key) => window.sessionStorage.getItem(key),
    ADMIN_STORAGE_KEY,
  )).toBe(ADMIN_TOKEN);
  await expect(page.getByText("可以从淘汰锦标赛 checkpoint 恢复", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "准备恢复" }).click();

  await expect(page.getByRole("heading", { name: "核对恢复任务" })).toBeVisible();
  await expect(page.getByText("淘汰锦标赛", { exact: true }).first()).toBeVisible();
  expect(resumeBody).toEqual(resumeChampionshipSpec());
  await expectNoWcagViolations(page);
});
