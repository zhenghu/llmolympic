"use strict";

(() => {
  globalThis.__LLMOLYMPIC_ENABLE_TEST_HOOKS__ = true;
  globalThis.__LLMOLYMPIC_SKIP_BOOTSTRAP__ = true;

  if (typeof URLSearchParams === "undefined") {
    globalThis.URLSearchParams = class URLSearchParamsForTest {
      constructor(source = "") {
        this.entries = String(source).split("&").filter(Boolean).map((entry) => {
          const separator = entry.indexOf("=");
          const key = separator < 0 ? entry : entry.slice(0, separator);
          const value = separator < 0 ? "" : entry.slice(separator + 1);
          return [decodeURIComponent(key.replace(/\+/g, " ")), decodeURIComponent(value.replace(/\+/g, " "))];
        });
      }

      get(key) {
        const entry = this.entries.find(([candidate]) => candidate === key);
        return entry ? entry[1] : null;
      }

      getAll(key) {
        return this.entries.filter(([candidate]) => candidate === key).map((entry) => entry[1]);
      }

      has(key) {
        return this.entries.some(([candidate]) => candidate === key);
      }

      keys() {
        return this.entries.map(([key]) => key)[Symbol.iterator]();
      }
    };
  }

  if (typeof URL === "undefined") {
    globalThis.URL = class URLForTest {
      constructor(source, base) {
        const input = String(source);
        const baseMatch = String(base || "").match(/^(https?):\/\/([^/?#]+)/);
        const absolute = input.match(/^(https?):\/\/([^/?#]+)([^?#]*)(\?[^#]*)?(#.*)?$/);
        const relative = input.match(/^(\/[^?#]*)(\?[^#]*)?(#.*)?$/);
        const match = absolute || (relative && baseMatch
          ? [input, baseMatch[1], baseMatch[2], relative[1], relative[2], relative[3]]
          : null);
        if (!match) throw new TypeError("invalid URL");
        this.origin = `${match[1]}://${match[2]}`;
        this.pathname = match[3] || "/";
        this.search = match[4] || "";
        this.hash = match[5] || "";
      }
    };
  }

  const clientAsset = globalThis.__LLMOLYMPIC_TEST_CLIENT_ASSET__
    || "llmolympic/web/static/assets/app.js";
  if (typeof require === "function") {
    require(globalThis.__LLMOLYMPIC_TEST_CLIENT_ASSET__
      || "../llmolympic/web/static/assets/app.js");
  } else {
    load(clientAsset);
  }

  const observer = globalThis.__LLMOLYMPIC_OBSERVER_TEST__;
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const equal = (actual, expected, message) => {
    assert(JSON.stringify(actual) === JSON.stringify(expected), `${message}: ${JSON.stringify(actual)}`);
  };
  const throwsCode = (operation, expected, message) => {
    let caught = null;
    try {
      operation();
    } catch (error) {
      caught = error;
    }
    assert(caught && caught.code === expected, `${message}: ${caught && caught.code}`);
  };

  equal(
    observer.profileCredentialPath("private.openai_profile"),
    "/api/v1/control/profiles/private.openai_profile/credential",
    "a named Profile credential uses only its encoded control path",
  );
  throwsCode(
    () => observer.profileCredentialPath("bad/profile"),
    "invalid_request",
    "a Profile credential path rejects path injection",
  );
  assert(!observer.profileKeyCanApply(""), "an empty Profile Key cannot be applied");
  assert(observer.profileKeyCanApply("opaque-secret"), "a printable ASCII Profile Key can be applied");
  assert(!observer.profileKeyCanApply(" secret value "), "Profile Key whitespace fails closed");
  assert(!observer.profileKeyCanApply("line\nbreak"), "Profile Key control characters fail closed");
  assert(!observer.profileKeyCanApply("密钥"), "a non-ASCII Profile Key fails closed");
  equal(
    observer.profileKeyValidationError(""),
    "请输入 API Key。",
    "an empty Profile Key has actionable validation copy",
  );
  equal(
    observer.profileKeyValidationError(" leading-space"),
    "API Key 只能包含不带空白的可打印 ASCII 字符。",
    "Profile Key whitespace has actionable validation copy",
  );
  equal(
    observer.profileKeyValidationError("密钥"),
    "API Key 只能包含不带空白的可打印 ASCII 字符。",
    "a non-ASCII Profile Key has actionable validation copy",
  );
  equal(
    observer.profileKeyValidationError("a".repeat(8193)),
    "API Key 不能超过 8192 个字符。",
    "an oversized Profile Key has bounded validation copy",
  );
  equal(
    observer.profileKeyValidationError("opaque-secret"),
    null,
    "a valid Profile Key has no validation error",
  );
  equal(
    observer.profileCredentialRequest("PUT", "opaque-secret"),
    { body: { api_key: "opaque-secret" }, method: "PUT" },
    "applying a Profile Key sends only the dedicated API field",
  );
  equal(
    observer.profileCredentialRequest("DELETE", "must-not-be-sent"),
    { body: null, method: "DELETE" },
    "clearing a Profile Key sends no request body",
  );
  throwsCode(
    () => observer.profileCredentialRequest("PUT", ""),
    "invalid_request",
    "an empty Profile Key request fails before fetch",
  );
  equal(
    observer.profileCredentialSuccess("PUT", {
      available: true,
      displayName: "Ready profile",
    }),
    { announcement: "Ready profile 的 Key 已应用。", focusTarget: "clear" },
    "applying a Profile Key announces success and moves focus to its clear action",
  );
  equal(
    observer.profileCredentialSuccess("DELETE", {
      available: false,
      displayName: "Cleared profile",
    }),
    { announcement: "Cleared profile 的 Key 已清除。", focusTarget: "input" },
    "clearing a runtime-only Profile Key announces success and restores input focus",
  );
  equal(
    observer.profileCredentialSuccess("DELETE", {
      available: true,
      displayName: "Environment profile",
    }),
    {
      announcement: "已清除 Environment profile 的网页 Key；启动环境仍提供 Key。",
      focusTarget: "clear",
    },
    "clearing a Profile Key explains an inherited environment fallback",
  );
  throwsCode(
    () => observer.profileCredentialSuccess("POST", {}),
    "invalid_request",
    "an unsupported Profile Key result fails closed",
  );

  equal(
    observer.classifyReplayClose(1006, "", 0, true),
    { action: "ready" },
    "a verified complete envelope survives an abnormal close",
  );
  equal(
    observer.classifyReplayClose(4429, "overloaded", 0, false),
    { action: "retry", delay: 500 },
    "overload is retried before its public reason becomes terminal",
  );
  equal(
    observer.classifyReplayClose(1006, "", 2, false),
    { action: "retry", delay: 2000 },
    "abnormal closes use bounded exponential retry",
  );
  equal(
    observer.classifyReplayClose(1006, "", 3, false),
    { action: "fallback" },
    "exhausted transport retries fall back to REST",
  );
  equal(
    observer.classifyReplayClose(4403, "archive_too_large", 0, false),
    { action: "error", code: "archive_too_large" },
    "allowlisted business close reasons remain specific",
  );
  equal(
    observer.classifyReplayClose(4403, "private detail", 0, false),
    { action: "error", code: "cross_origin_forbidden" },
    "unknown close reasons are never displayed",
  );

  let playback = observer.playbackReducer(
    { cursor: 0, playing: false },
    { type: "reset", available: 0, autoPlay: true, transferComplete: false, error: false },
  );
  equal(playback, { cursor: 0, playing: true }, "streaming replay can start before events arrive");

  playback = observer.playbackReducer(
    { cursor: 3, playing: true },
    { type: "tick", available: 4, transferComplete: true, error: false },
  );
  equal(playback, { cursor: 4, playing: false }, "playback stops at the verified end");

  playback = observer.playbackReducer(
    playback,
    { type: "toggle", available: 4, transferComplete: true, error: false },
  );
  equal(playback, { cursor: 0, playing: true }, "play at the end restarts from zero");

  playback = observer.playbackReducer(
    { cursor: 2, playing: false },
    { type: "restart", available: 4, transferComplete: true, error: false },
  );
  equal(playback, { cursor: 0, playing: true }, "restart immediately resumes playback");

  playback = observer.playbackReducer(
    { cursor: 2, playing: true },
    { type: "toggle", available: 4, transferComplete: false, error: true },
  );
  equal(playback, { cursor: 2, playing: false }, "an error stops playback");

  const runningLiveSummary = {
    live_id: "live-match-43",
    mode: "play",
    status: "running",
    game: "math_quiz",
    players: ["Alice", "Bob"],
    started_at: "2026-08-13T12:00:00.000000Z",
    updated_at: "2026-08-13T12:00:01.000000Z",
    event_count: 2,
    pairing_number: null,
    pairing_count: null,
    leg_number: null,
    final_kind: null,
    final_id: null,
    final_match_ids: [],
  };
  assert(
    observer.validateLiveSummary(runningLiveSummary, "live-match-43"),
    "a running single-match summary is accepted",
  );

  const completedLiveSummary = {
    ...runningLiveSummary,
    status: "completed",
    event_count: 3,
    final_kind: "match",
    final_id: "archive-match-43",
    final_match_ids: ["archive-match-43"],
  };
  assert(
    observer.validateLiveSummary(completedLiveSummary, "live-match-43"),
    "a completed match with its archive reference is accepted",
  );
  assert(
    observer.validateLiveSummary({
      ...completedLiveSummary,
      live_id: "live-series",
      mode: "series",
      leg_number: 2,
      final_kind: "series",
      final_id: "series-43",
      final_match_ids: ["series-leg-1", "series-leg-2"],
    }, "live-series"),
    "a completed two-leg series is accepted",
  );
  assert(
    observer.validateLiveSummary({
      ...completedLiveSummary,
      live_id: "live-tournament",
      mode: "round_robin",
      pairing_number: 2,
      pairing_count: 3,
      leg_number: 1,
      final_kind: "tournament",
      final_id: "tournament-43",
      final_match_ids: ["m1", "m2", "m3", "m4", "m5", "m6"],
    }, "live-tournament"),
    "a completed tournament with all leg archives is accepted",
  );

  const invalidLiveSummaries = [
    [{ ...runningLiveSummary, live_id: "other" }, "live-match-43", "wrong live id"],
    [{ ...runningLiveSummary, mode: "unknown" }, "live-match-43", "unknown mode"],
    [{ ...runningLiveSummary, status: "finished" }, "live-match-43", "unknown status"],
    [{ ...runningLiveSummary, game: "bad-game" }, "live-match-43", "invalid game id"],
    [{ ...runningLiveSummary, players: ["Alice"] }, "live-match-43", "too few players"],
    [{ ...runningLiveSummary, event_count: 10001 }, "live-match-43", "unbounded event count"],
    [{ ...runningLiveSummary, leg_number: 1 }, "live-match-43", "play placement"],
    [{ ...runningLiveSummary, final_id: "partial-archive" }, "live-match-43", "unfinished final metadata"],
    [{ ...completedLiveSummary, final_kind: "series" }, "live-match-43", "wrong final kind"],
    [{ ...completedLiveSummary, final_match_ids: ["other"] }, "live-match-43", "mismatched match archive"],
    [{
      ...completedLiveSummary,
      live_id: "bad-series",
      mode: "series",
      final_kind: "series",
      final_id: "series-43",
      final_match_ids: ["only-one-leg"],
    }, "bad-series", "incomplete series archives"],
    [{
      ...completedLiveSummary,
      live_id: "bad-tournament",
      mode: "round_robin",
      pairing_number: 4,
      pairing_count: 3,
      leg_number: 1,
      final_kind: "tournament",
      final_id: "tournament-43",
      final_match_ids: ["m1", "m2", "m3", "m4", "m5", "m6"],
    }, "bad-tournament", "pairing outside tournament"],
  ];
  invalidLiveSummaries.forEach(([summary, liveId, reason]) => {
    assert(!observer.validateLiveSummary(summary, liveId), `live summary rejects ${reason}`);
  });

  const championshipPlayers = ["Alice", "Bob", "Carol", "Dora"];
  const championshipPairingOne = {
    round_number: 1,
    round_pairing_number: 1,
    pairing_number: 1,
    players: ["Alice", "Bob"],
    winner: "Alice",
    series_id: "series-championship-1",
    match_ids: ["championship-match-1", "championship-match-2"],
    status: "committed",
  };
  const championshipPairingTwo = {
    round_number: 1,
    round_pairing_number: 2,
    pairing_number: 2,
    players: ["Carol", "Dora"],
    winner: "Dora",
    series_id: "series-championship-2",
    match_ids: ["championship-match-3", "championship-match-4"],
    status: "committed",
  };
  const championshipFinalPairing = {
    round_number: 2,
    round_pairing_number: 1,
    pairing_number: 3,
    players: ["Alice", "Dora"],
    winner: "Alice",
    series_id: "series-championship-3",
    match_ids: ["championship-match-5", "championship-match-6"],
    status: "committed",
  };
  const emptyChampionshipBracket = {
    championship_id: "championship-43",
    player_count: 4,
    round_count: 2,
    pairing_count: 3,
    champion: null,
    pairings: [],
  };
  const provisionalChampionshipBracket = {
    ...emptyChampionshipBracket,
    pairings: [{ ...championshipPairingOne, status: "provisional" }],
  };
  const committedFirstRoundBracket = {
    ...emptyChampionshipBracket,
    pairings: [championshipPairingOne, championshipPairingTwo],
  };
  const finalProvisionalBracket = {
    ...emptyChampionshipBracket,
    pairings: [
      championshipPairingOne,
      championshipPairingTwo,
      { ...championshipFinalPairing, status: "provisional" },
    ],
  };
  const completedChampionshipBracket = {
    ...emptyChampionshipBracket,
    champion: "Alice",
    pairings: [championshipPairingOne, championshipPairingTwo, championshipFinalPairing],
  };

  [
    emptyChampionshipBracket,
    provisionalChampionshipBracket,
    committedFirstRoundBracket,
    finalProvisionalBracket,
    completedChampionshipBracket,
  ].forEach((bracket, index) => {
    assert(
      observer.validateChampionshipBracket(bracket, championshipPlayers),
      `authoritative championship bracket lifecycle ${index} is accepted`,
    );
  });
  [8, 16].forEach((playerCount) => {
    const players = Array.from({ length: playerCount }, (_value, index) => `Player ${index + 1}`);
    assert(
      observer.validateChampionshipBracket({
        championship_id: `championship-${playerCount}`,
        player_count: playerCount,
        round_count: Math.log2(playerCount),
        pairing_count: playerCount - 1,
        champion: null,
        pairings: [],
      }, players),
      `an empty authoritative ${playerCount}-player bracket is accepted`,
    );
  });

  const invalidChampionshipBrackets = [
    [{ ...emptyChampionshipBracket, player_count: 5 }, "non-power-of-two player count"],
    [{ ...emptyChampionshipBracket, round_count: 3 }, "inconsistent round count"],
    [{ ...emptyChampionshipBracket, pairing_count: 4 }, "inconsistent pairing count"],
    [{ ...emptyChampionshipBracket, entrant_ids: ["hidden-a"] }, "hidden entrant identifiers"],
    [{ ...emptyChampionshipBracket, pairings: [championshipPairingTwo] }, "noncanonical pairing prefix"],
    [{ ...emptyChampionshipBracket, pairings: [championshipPairingOne] }, "partial committed round"],
    [{
      ...emptyChampionshipBracket,
      pairings: [
        { ...championshipPairingOne, status: "provisional" },
        championshipPairingTwo,
      ],
    }, "committed pairing after provisional pairing"],
    [{
      ...completedChampionshipBracket,
      pairings: [
        championshipPairingOne,
        championshipPairingTwo,
        { ...championshipFinalPairing, players: ["Bob", "Dora"] },
      ],
    }, "final pairing not sourced from prior winners"],
    [{ ...committedFirstRoundBracket, champion: "Alice" }, "premature champion"],
    [{ ...completedChampionshipBracket, champion: "Dora" }, "champion not equal to final winner"],
    [{
      ...provisionalChampionshipBracket,
      pairings: [{ ...provisionalChampionshipBracket.pairings[0], winner: "Mallory" }],
    }, "winner outside pairing"],
    [{
      ...provisionalChampionshipBracket,
      pairings: [{ ...provisionalChampionshipBracket.pairings[0], entrant_ids: ["secret-a", "secret-b"] }],
    }, "hidden pairing identifiers"],
    [{
      ...provisionalChampionshipBracket,
      pairings: [{ ...provisionalChampionshipBracket.pairings[0], match_ids: ["same", "same"] }],
    }, "duplicate match archives"],
  ];
  invalidChampionshipBrackets.forEach(([bracket, reason]) => {
    assert(
      !observer.validateChampionshipBracket(bracket, championshipPlayers),
      `championship bracket rejects ${reason}`,
    );
  });
  assert(
    !observer.validateChampionshipBracket(emptyChampionshipBracket, ["Alice", "Alice", "Carol", "Dora"]),
    "championship bracket rejects a duplicate public roster",
  );

  const startingChampionshipSummary = {
    ...runningLiveSummary,
    live_id: "live-championship-43",
    mode: "championship",
    players: championshipPlayers,
    event_count: 0,
    pairing_number: 1,
    pairing_count: 3,
    leg_number: 1,
    round_number: 1,
    round_count: 2,
    round_pairing_number: 1,
    round_pairing_count: 2,
    championship_bracket: emptyChampionshipBracket,
  };
  const provisionalChampionshipSummary = {
    ...startingChampionshipSummary,
    event_count: 9,
    leg_number: 2,
    championship_bracket: provisionalChampionshipBracket,
  };
  const secondRoundChampionshipSummary = {
    ...startingChampionshipSummary,
    event_count: 18,
    pairing_number: 3,
    leg_number: 1,
    round_number: 2,
    round_pairing_number: 1,
    round_pairing_count: 1,
    championship_bracket: committedFirstRoundBracket,
  };
  const completedChampionshipSummary = {
    ...secondRoundChampionshipSummary,
    status: "completed",
    event_count: 27,
    leg_number: 2,
    championship_bracket: completedChampionshipBracket,
    final_kind: "championship",
    final_id: "championship-43",
    final_match_ids: completedChampionshipBracket.pairings.flatMap((pairing) => pairing.match_ids),
  };
  const finalProvisionalChampionshipSummary = {
    ...secondRoundChampionshipSummary,
    event_count: 26,
    leg_number: 2,
    championship_bracket: finalProvisionalBracket,
  };
  [
    startingChampionshipSummary,
    provisionalChampionshipSummary,
    secondRoundChampionshipSummary,
    finalProvisionalChampionshipSummary,
    completedChampionshipSummary,
  ].forEach((summary, index) => {
    assert(
      observer.validateLiveSummary(summary, "live-championship-43"),
      `championship summary lifecycle ${index} is accepted`,
    );
  });

  const invalidChampionshipSummaries = [
    [{ ...startingChampionshipSummary, round_count: null }, "partial championship context"],
    [{ ...startingChampionshipSummary, pairing_number: 3 }, "context skipping materialized bracket"],
    [{ ...startingChampionshipSummary, championship_bracket: null }, "missing authoritative bracket"],
    [{ ...startingChampionshipSummary, entrant_ids: ["hidden-a"] }, "hidden entrant identifiers"],
    [{ ...startingChampionshipSummary, players: ["Alice", "Bob", "Alice", "Dora"] }, "duplicate roster"],
    [{
      ...startingChampionshipSummary,
      championship_bracket: completedChampionshipBracket,
    }, "champion before completion"],
    [{ ...completedChampionshipSummary, final_id: "other-championship" }, "wrong final championship id"],
    [{
      ...completedChampionshipSummary,
      final_match_ids: completedChampionshipSummary.final_match_ids.slice().reverse(),
    }, "noncanonical archive order"],
    [{
      ...completedChampionshipSummary,
      championship_bracket: { ...completedChampionshipBracket, champion: null },
    }, "completed bracket without champion"],
  ];
  invalidChampionshipSummaries.forEach(([summary, reason]) => {
    assert(
      !observer.validateLiveSummary(summary, "live-championship-43"),
      `championship summary rejects ${reason}`,
    );
  });
  assert(
    observer.validateLiveSummary({
      ...startingChampionshipSummary,
      championship_bracket: { ...emptyChampionshipBracket, championship_id: "independent-public-id" },
    }, "live-championship-43"),
    "an unfinished live ID is not falsely assumed to encode the independent championship ID",
  );
  assert(
    !observer.validateLiveSummary({
      ...runningLiveSummary,
      championship_bracket: emptyChampionshipBracket,
    }, "live-match-43"),
    "non-championship summaries fail closed if a bracket appears",
  );

  const liveItem = {
    seq: 7,
    context: {
      pairing_number: 2,
      leg_number: 1,
      match_event_seq: 0,
    },
    event: {
      seq: 0,
      type: "match_started",
      timestamp: "2026-08-13T12:00:00.000000Z",
      player: null,
      data: {
        game: "math_quiz",
        seed: 43,
        game_config: { rounds: 1 },
        players: ["Alice", "Bob"],
      },
    },
  };
  assert(observer.validateLiveItem(liveItem, 7), "a valid live broker item is accepted");
  assert(
    !observer.validateLiveItem({ ...liveItem, seq: 8 }, 7),
    "a broker sequence mismatch is rejected",
  );
  assert(
    !observer.validateLiveItem({
      ...liveItem,
      context: { ...liveItem.context, match_event_seq: 1 },
    }, 7),
    "a match event sequence mismatch is rejected",
  );
  assert(
    !observer.validateLiveItem({
      ...liveItem,
      context: { pairing_number: 2, leg_number: null, match_event_seq: 0 },
    }, 7),
    "a tournament pairing without a leg is rejected",
  );
  assert(
    !observer.validateLiveItem({
      ...liveItem,
      event: { ...liveItem.event, type: "provider_secret" },
    }, 7),
    "an unknown public event type is rejected",
  );
  assert(
    !observer.validateLiveItem({
      ...liveItem,
      event: { ...liveItem.event, timestamp: null },
    }, 7),
    "a malformed public event is rejected",
  );

  assert(
    observer.validateLiveItem({ ...liveItem, kind: "match_event" }, 7, {
      ...runningLiveSummary,
      live_id: "live-round-robin",
      mode: "round_robin",
      pairing_number: 2,
      pairing_count: 3,
      leg_number: 1,
    }),
    "old modes accept the discriminated match-event shape with an authoritative summary",
  );
  const championshipContext = {
    round_number: 1,
    round_count: 2,
    round_pairing_number: 1,
    round_pairing_count: 2,
    pairing_number: 1,
    pairing_count: 3,
    leg_number: 1,
    match_event_seq: 0,
  };
  const championshipMatchItem = {
    kind: "match_event",
    seq: 0,
    context: championshipContext,
    event: {
      seq: 0,
      type: "match_started",
      timestamp: "2026-08-13T12:00:00.000000Z",
      player: null,
      data: {
        game: "math_quiz",
        seed: 43,
        game_config: { rounds: 1 },
        players: ["Alice", "Bob"],
      },
    },
  };
  assert(
    observer.validateLiveItem(championshipMatchItem, 0, {
      ...startingChampionshipSummary,
      event_count: 1,
    }),
    "a championship match event is bound to its canonical public pairing",
  );
  assert(
    observer.validateLiveItem({
      ...championshipMatchItem,
      event: {
        ...championshipMatchItem.event,
        data: {
          ...championshipMatchItem.event.data,
          seed: 5725006802337835000,
        },
      },
    }, 0, {
      ...startingChampionshipSummary,
      event_count: 1,
    }),
    "a browser-rounded signed 64-bit championship seed remains a valid public event",
  );
  assert(
    !observer.validateLiveItem({
      ...championshipMatchItem,
      event: {
        ...championshipMatchItem.event,
        data: {
          ...championshipMatchItem.event.data,
          seed: 2 ** 64,
        },
      },
    }, 0, {
      ...startingChampionshipSummary,
      event_count: 1,
    }),
    "an out-of-range public seed is rejected",
  );

  const pairingCompletedItem = {
    kind: "pairing_completed",
    seq: 8,
    context: {
      ...championshipContext,
      leg_number: 2,
      match_event_seq: undefined,
    },
    pairing: { ...championshipPairingOne, status: "provisional" },
  };
  delete pairingCompletedItem.context.match_event_seq;
  assert(
    observer.validateLiveItem(pairingCompletedItem, 8, provisionalChampionshipSummary),
    "a provisional pairing lifecycle item matches the authoritative bracket",
  );

  const roundCommittedItem = {
    kind: "round_committed",
    seq: 17,
    context: {
      round_number: 1,
      round_count: 2,
      round_pairing_number: 2,
      round_pairing_count: 2,
      pairing_number: 2,
      pairing_count: 3,
      leg_number: 2,
    },
    pairing_numbers: [1, 2],
  };
  assert(
    observer.validateLiveItem(roundCommittedItem, 17, secondRoundChampionshipSummary),
    "a whole-round commit lifecycle item matches the authoritative committed bracket",
  );

  const invalidChampionshipItems = [
    [{ ...championshipMatchItem, kind: undefined }, startingChampionshipSummary, "missing discriminator"],
    [{
      ...championshipMatchItem,
      context: { ...championshipMatchItem.context, round_pairing_number: 2 },
    }, startingChampionshipSummary, "noncanonical event context"],
    [{
      ...championshipMatchItem,
      event: {
        ...championshipMatchItem.event,
        data: { ...championshipMatchItem.event.data, players: ["Carol", "Dora"] },
      },
    }, startingChampionshipSummary, "match-start roster outside pairing"],
    [{
      ...championshipMatchItem,
      event: { ...championshipMatchItem.event, player: "Mallory" },
    }, startingChampionshipSummary, "event player outside pairing"],
    [{
      ...championshipMatchItem,
      event: {
        ...championshipMatchItem.event,
        data: { ...championshipMatchItem.event.data, game: "gomoku" },
      },
    }, startingChampionshipSummary, "match project differing from authoritative summary"],
    [{
      ...championshipMatchItem,
      event: {
        ...championshipMatchItem.event,
        type: "match_finished",
        data: {
          forfeited_by: null,
          judging: null,
          reason_code: null,
          scores: { Alice: 1, Mallory: 0 },
          termination: "completed",
        },
      },
    }, startingChampionshipSummary, "finish scores outside authoritative pairing"],
    [{
      ...championshipMatchItem,
      event: {
        ...championshipMatchItem.event,
        type: "match_finished",
        data: {
          forfeited_by: "Mallory",
          judging: null,
          reason_code: "forfeit",
          scores: { Alice: 1, Bob: 0 },
          termination: "technical_loss",
        },
      },
    }, startingChampionshipSummary, "forfeit player outside authoritative pairing"],
    [{
      ...championshipMatchItem,
      event: {
        ...championshipMatchItem.event,
        data: { ...championshipMatchItem.event.data, entrant_ids: ["secret-a", "secret-b"] },
      },
    }, startingChampionshipSummary, "hidden event entrant identifiers"],
    [{
      ...pairingCompletedItem,
      pairing: { ...pairingCompletedItem.pairing, status: "committed" },
    }, provisionalChampionshipSummary, "committed pairing in provisional lifecycle event"],
    [{
      ...pairingCompletedItem,
      pairing: { ...pairingCompletedItem.pairing, series_id: "series-tampered" },
    }, provisionalChampionshipSummary, "pairing payload differing from authoritative bracket"],
    [{
      ...roundCommittedItem,
      pairing_numbers: [2, 1],
    }, secondRoundChampionshipSummary, "noncanonical committed pairing order"],
    [{
      ...roundCommittedItem,
      context: {
        ...roundCommittedItem.context,
        round_pairing_number: 1,
        pairing_number: 1,
      },
      pairing_numbers: [1, 2],
    }, secondRoundChampionshipSummary, "whole-round commit before the round's final pairing"],
    [roundCommittedItem, provisionalChampionshipSummary, "commit before the authoritative round boundary"],
    [{ ...pairingCompletedItem, entrant_id: "hidden" }, provisionalChampionshipSummary, "unknown lifecycle fields"],
  ];
  invalidChampionshipItems.forEach(([item, summary, reason]) => {
    assert(
      !observer.validateLiveItem(item, item.seq, summary),
      `championship live item rejects ${reason}`,
    );
  });
  assert(
    !observer.validateLiveItem(pairingCompletedItem, 8),
    "championship lifecycle items cannot be accepted without authoritative summary state",
  );
  assert(
    !observer.validateLiveItem(pairingCompletedItem, 8, runningLiveSummary),
    "old modes reject championship lifecycle items",
  );
  assert(
    !observer.validateLiveItem(championshipMatchItem, 0, runningLiveSummary),
    "old modes reject championship-only match context",
  );

  let championshipState = observer.championshipReducer(null, {
    type: "summary",
    summary: startingChampionshipSummary,
  });
  equal(championshipState.summary, startingChampionshipSummary, "the reducer keeps the authoritative summary");
  equal(championshipState.bracket, emptyChampionshipBracket, "the reducer starts from the authoritative empty bracket");
  equal(championshipState.lifecycleItems, [], "ordinary championship startup has no lifecycle item");

  const forgedScoreItem = {
    ...championshipMatchItem,
    event: {
      seq: 0,
      type: "match_finished",
      timestamp: "2026-08-13T12:00:01.000000Z",
      player: null,
      data: {
        scores: { Alice: 999, Bob: 0 },
        termination: "completed",
        reason_code: null,
        forfeited_by: null,
        judging: null,
      },
    },
  };
  championshipState = observer.championshipReducer(championshipState, {
    type: "item",
    item: forgedScoreItem,
    summary: { ...startingChampionshipSummary, event_count: 1 },
  });
  assert(
    championshipState.bracket.champion === null && championshipState.bracket.pairings.length === 0,
    "ordinary score events never infer pairing winners or a champion",
  );
  equal(championshipState.lifecycleItems, [], "ordinary match events are not lifecycle authority");

  championshipState = observer.championshipReducer(championshipState, {
    type: "item",
    item: pairingCompletedItem,
    summary: provisionalChampionshipSummary,
  });
  equal(
    championshipState.bracket,
    provisionalChampionshipBracket,
    "a pairing lifecycle item uses the authoritative provisional bracket",
  );
  equal(championshipState.lifecycleItems, [pairingCompletedItem], "the provisional lifecycle item is recorded once");
  const idempotentChampionshipState = observer.championshipReducer(championshipState, {
    type: "item",
    item: pairingCompletedItem,
  });
  equal(
    idempotentChampionshipState,
    championshipState,
    "a repeated page or reconnect item is idempotent",
  );
  throwsCode(
    () => observer.championshipReducer(championshipState, {
      type: "item",
      item: {
        ...pairingCompletedItem,
        pairing: { ...pairingCompletedItem.pairing, winner: "Bob" },
      },
    }),
    "protocol_error",
    "a conflicting duplicate broker sequence fails closed",
  );

  championshipState = observer.championshipReducer(championshipState, {
    type: "item",
    item: roundCommittedItem,
    summary: secondRoundChampionshipSummary,
  });
  assert(
    championshipState.bracket.pairings.every((pairing) => pairing.status === "committed"),
    "a round commit exposes only the authoritative whole-round checkpoint",
  );
  equal(
    championshipState.lifecycleItems,
    [pairingCompletedItem, roundCommittedItem],
    "lifecycle items remain ordered across paginated event batches",
  );

  const finalRoundCommittedItem = {
    kind: "round_committed",
    seq: 26,
    context: {
      round_number: 2,
      round_count: 2,
      round_pairing_number: 1,
      round_pairing_count: 1,
      pairing_number: 3,
      pairing_count: 3,
      leg_number: 2,
    },
    pairing_numbers: [3],
  };
  let terminalChampionshipState = observer.championshipReducer(null, {
    type: "summary",
    summary: finalProvisionalChampionshipSummary,
  });
  terminalChampionshipState = observer.championshipReducer(terminalChampionshipState, {
    type: "item",
    item: finalRoundCommittedItem,
  });
  assert(
    terminalChampionshipState.bracket.pairings.every((pairing) => pairing.status === "committed")
      && terminalChampionshipState.bracket.champion === null
      && terminalChampionshipState.summary.status === "running",
    "the final round checkpoint does not announce a champion before final archive commit",
  );
  assert(
    observer.validateLiveSummary(
      terminalChampionshipState.summary,
      terminalChampionshipState.summary.live_id,
    ),
    "the final committed round remains a valid running summary without a champion",
  );
  const interruptedChampionshipState = observer.championshipReducer(terminalChampionshipState, {
    eventCount: 27,
    status: "interrupted",
    type: "terminal",
  });
  assert(
    interruptedChampionshipState.summary.status === "interrupted"
      && interruptedChampionshipState.bracket.champion === null,
    "an interrupted finalization synchronizes terminal state without exposing a champion",
  );
  const completedTerminalChampionshipState = observer.championshipReducer(
    terminalChampionshipState,
    {
      eventCount: 27,
      finalId: "championship-43",
      finalKind: "championship",
      finalMatchIds: completedChampionshipBracket.pairings.flatMap(
        (pairing) => pairing.match_ids,
      ),
      status: "completed",
      type: "terminal",
    },
  );
  assert(
    completedTerminalChampionshipState.summary.status === "completed"
      && completedTerminalChampionshipState.bracket.champion === "Alice",
    "only the completed terminal signal exposes the authoritative final pairing winner",
  );

  championshipState = observer.championshipReducer(championshipState, {
    type: "summary",
    summary: completedChampionshipSummary,
  });
  equal(
    championshipState.bracket,
    completedChampionshipBracket,
    "a reconnect summary replaces provisional client state with the authoritative completed bracket",
  );
  assert(
    championshipState.bracket.champion === "Alice",
    "the champion is read only from the completed authoritative bracket",
  );

  const participationRequest = {
    request_id: "request-7",
    request_seq: 7,
    match_event_seq: 12,
    state: "pending",
    prompt: '<img src=x onerror="alert(1)">\nH8?',
    created_at: "2026-08-14T12:00:00.000000Z",
    expires_at: "2026-08-14T12:01:00.000000Z",
  };
  const participationSnapshot = {
    api_version: "v1",
    session_id: "session-7",
    seat_id: "seat-1",
    status: "active",
    game: "gomoku",
    player_name: "Alice",
    players: ["Alice", "Bob", "Carol"],
    created_at: "2026-08-14T11:59:00.000000Z",
    updated_at: "2026-08-14T12:00:00.000000Z",
    lease_expires_at: "2026-08-14T12:02:00.000000Z",
    request: participationRequest,
    final_match_id: null,
  };
  assert(
    observer.validateParticipationRequest(participationRequest),
    "a current participation prompt is accepted as plain text",
  );
  assert(
    observer.validateParticipationSnapshot(participationSnapshot, "session-7", "seat-1"),
    "an active three-player capability-scoped participation snapshot is accepted",
  );
  const secondSeatSnapshot = {
    ...participationSnapshot,
    seat_id: "seat-2",
    player_name: "Bob",
  };
  assert(
    observer.validateParticipationSnapshot(secondSeatSnapshot, "session-7", "seat-2"),
    "a second seat accepts only its matching route identity",
  );
  assert(
    !observer.validateParticipationSnapshot(secondSeatSnapshot, "session-7", "seat-1"),
    "a second seat snapshot is rejected on the first seat route",
  );
  assert(
    observer.participationKeepsCapability("active"),
    "an active seat keeps its tab-scoped capability for refresh recovery",
  );
  assert(
    observer.participationComponentKey("session-a", "seat-1")
      !== observer.participationComponentKey("session-b", "seat-1"),
    "changing participation sessions remounts the credential-scoped page",
  );
  assert(
    observer.participationComponentKey("session-a", "seat-1")
      !== observer.participationComponentKey("session-a", "seat-2"),
    "changing participation seats remounts the credential-scoped page",
  );
  ["completed", "interrupted", "expired"].forEach((status) => {
    assert(
      !observer.participationKeepsCapability(status),
      `${status} clears its no-longer-needed capability`,
    );
  });
  [401, 403, 404, 410].forEach((status) => {
    assert(
      observer.participationErrorClearsCapability(status),
      `HTTP ${status} clears an unusable capability`,
    );
  });
  assert(
    !observer.participationErrorClearsCapability(503),
    "a transient input-service failure keeps the capability for retry",
  );
  assert(
    !observer.validateParticipationSnapshot({
      ...participationSnapshot,
      submitted_move: "H8",
    }, "session-7", "seat-1"),
    "GET snapshots fail closed if submitted content appears",
  );
  const invalidParticipationRosters = [
    ["Alice", ...Array.from({ length: 16 }, (_, index) => `player-${index}`)],
    ["Alice", "Bob", "Alice"],
    ["Bob", "Carol"],
  ];
  invalidParticipationRosters.forEach((players) => {
    assert(
      !observer.validateParticipationSnapshot({
        ...participationSnapshot,
        players,
      }, "session-7", "seat-1"),
      `participation snapshots reject invalid roster: ${JSON.stringify(players)}`,
    );
  });
  assert(
    !observer.validateParticipationRequest({
      ...participationRequest,
      submission_id: "a".repeat(32),
    }),
    "request DTOs reject submission identifiers",
  );
  assert(
    observer.validateParticipationSubmission({
      api_version: "v1",
      request_id: "request-7",
      status: "submitted",
    }, "request-7"),
    "a transport-level submitted acknowledgement is accepted",
  );
  assert(
    !observer.validateParticipationSubmission({
      api_version: "v1",
      request_id: "request-7",
      status: "accepted",
    }, "request-7"),
    "the browser never mistakes persistence for game-rule acceptance",
  );
  equal(observer.countCharacters("A😀B"), 3, "character counts use Unicode code points");
  equal(
    observer.remainingCopy("2026-08-14T12:00:30.000Z", Date.parse("2026-08-14T12:00:00.000Z")),
    "剩余 30 秒",
    "the visible request deadline is deterministic",
  );

  const storageValues = new Map();
  const replacedLocations = [];
  globalThis.window = {
    history: {
      replaceState(_state, _title, location) {
        replacedLocations.push(location);
        globalThis.window.location.hash = "";
      },
      state: null,
    },
    location: {
      hash: "",
      origin: "http://localhost:8000",
      pathname: "/new",
      search: "?view=control",
    },
    sessionStorage: {
      getItem(key) {
        return storageValues.has(key) ? storageValues.get(key) : null;
      },
      removeItem(key) {
        storageValues.delete(key);
      },
      setItem(key, value) {
        storageValues.set(key, value);
      },
    },
  };

  const adminToken = "a".repeat(43);
  window.location.hash = `#admin=${adminToken}`;
  equal(observer.captureAdminToken(), adminToken, "a valid admin fragment is captured once");
  equal(replacedLocations, ["/new?view=control"], "the admin fragment is removed from browser history");
  equal(
    storageValues.get("llmolympic.control.admin"),
    adminToken,
    "the admin token is scoped to session storage",
  );
  observer.clearAdminToken();
  assert(!storageValues.has("llmolympic.control.admin"), "authentication loss clears stored admin state");
  equal(observer.captureAdminToken(), null, "cleared admin state is not recoverable from memory");

  storageValues.set("llmolympic.control.admin", adminToken);
  window.location.hash = "#admin=invalid";
  equal(observer.captureAdminToken(), null, "an invalid admin fragment fails closed");
  assert(!storageValues.has("llmolympic.control.admin"), "an invalid fragment clears stale admin state");
  window.location.hash = `#admin=${adminToken}&admin=${"b".repeat(43)}`;
  equal(observer.captureAdminToken(), null, "duplicate admin fragments fail closed");
  assert(!storageValues.has("llmolympic.control.admin"), "duplicate fragments leave no credential behind");

  const mockStrategies = Array.from({ length: 17 }, (_value, index) => `strategy_${index}`);
  const controlCatalogPayload = {
    api_version: "v1",
    games: [{
      max_players: 16,
      min_players: 2,
      name: "math_quiz",
      requires_judge_panel: false,
      rounds_supported: true,
      supported_modes: ["play", "series", "round_robin", "championship"],
    }],
    mock_judge_strategies: ["strict", "balanced", "lenient"],
    mock_player_strategies: mockStrategies,
    profiles: [
      {
        credential_ready: true,
        default_model: "safe-model",
        display_name: "Ready profile",
        profile_id: "ready-profile",
        provider: "openai",
      },
      {
        credential_ready: false,
        default_model: null,
        display_name: "Unavailable profile",
        profile_id: "unavailable-profile",
        provider: "ollama",
      },
    ],
  };
  const controlCatalog = observer.normalizeControlCatalog(controlCatalogPayload);
  assert(controlCatalog.games.length === 1, "a valid control catalog is normalized");
  assert(
    controlCatalog.games[0].supportedModes.includes("championship"),
    "the control catalog retains the backend championship capability",
  );
  assert(controlCatalog.profiles[0].available, "credential readiness becomes only a boolean capability");
  assert(
    !JSON.stringify(controlCatalog).includes("credential_ready"),
    "catalog normalization does not retain credential metadata fields",
  );
  throwsCode(
    () => observer.normalizeControlCatalog({ api_version: "v1", games: [] }),
    "protocol_error",
    "an empty catalog fails closed",
  );
  throwsCode(
    () => observer.normalizeControlCatalog({
      api_version: "v1",
      games: [{ ...controlCatalogPayload.games[0], name: "../command" }],
    }),
    "protocol_error",
    "a catalog with only unsafe game identifiers fails closed",
  );

  const budget = {
    maxEstimatedCostUsd: "1.250000",
    maxInputTokens: "200000",
    maxOutputTokensPerCall: "4096",
    maxProviderCalls: "64",
    maxTotalOutputTokens: "65536",
  };
  const makeForm = (overrides = {}) => ({
    allowLargeTournament: false,
    budget: { ...budget },
    game: "math_quiz",
    judges: [],
    llmTimeoutSeconds: "120",
    mode: "play",
    players: [
      { kind: "mock", name: "", profileId: "", strategy: "strategy_0" },
      { kind: "mock", name: "", profileId: "", strategy: "strategy_1" },
    ],
    rounds: "1",
    seed: "42",
    timeoutSeconds: "300",
    ...overrides,
  });
  const uniquePlayers = (count) => Array.from({ length: count }, (_value, index) => ({
    kind: "mock",
    name: "",
    profileId: "",
    strategy: `strategy_${index}`,
  }));

  assert(
    observer.validateControlForm(makeForm({ players: uniquePlayers(2) }), controlCatalog).players === undefined,
    "play accepts the minimum two players",
  );
  assert(
    observer.validateControlForm(makeForm({ players: uniquePlayers(1) }), controlCatalog).players,
    "play rejects fewer than two players",
  );
  assert(
    observer.validateControlForm(makeForm({ players: uniquePlayers(16) }), controlCatalog).players === undefined,
    "play accepts the platform maximum of sixteen players",
  );
  assert(
    observer.validateControlForm(makeForm({ players: uniquePlayers(17) }), controlCatalog).players,
    "play rejects more than sixteen players",
  );
  assert(
    observer.validateControlForm(makeForm({ mode: "series", players: uniquePlayers(2) }), controlCatalog).players === undefined,
    "series accepts exactly two players",
  );
  assert(
    observer.validateControlForm(makeForm({ mode: "series", players: uniquePlayers(3) }), controlCatalog).players,
    "series rejects a third player",
  );
  assert(
    observer.validateControlForm(makeForm({ mode: "round_robin", players: uniquePlayers(2) }), controlCatalog).players,
    "round robin rejects fewer than three players",
  );
  assert(
    observer.validateControlForm(makeForm({ mode: "round_robin", players: uniquePlayers(3) }), controlCatalog).players === undefined,
    "round robin accepts three distinct players",
  );
  [4, 8, 16].forEach((playerCount) => {
    assert(
      observer.validateControlForm(makeForm({
        mode: "championship",
        players: uniquePlayers(playerCount),
      }), controlCatalog).players === undefined,
      `championship accepts an exact ${playerCount}-player bracket`,
    );
  });
  [2, 3, 5, 6, 7, 9, 15].forEach((playerCount) => {
    assert(
      observer.validateControlForm(makeForm({
        mode: "championship",
        players: uniquePlayers(playerCount),
      }), controlCatalog).players,
      `championship rejects a ${playerCount}-player non-bracket roster`,
    );
  });
  assert(
    observer.validateControlForm(makeForm({
      mode: "championship",
      players: [
        { kind: "human", name: "Alice", profileId: "", strategy: "" },
        ...uniquePlayers(3),
      ],
    }), controlCatalog)["player-0"],
    "championship rejects browser-human seats",
  );

  const unavailableProfileForm = makeForm({
    players: [
      { kind: "profile", name: "", profileId: "unavailable-profile", strategy: "" },
      uniquePlayers(2)[1],
    ],
  });
  assert(
    observer.validateControlForm(unavailableProfileForm, controlCatalog)["player-0"],
    "an unavailable Provider profile cannot be submitted",
  );
  const missingBudgetForm = makeForm({
    budget: {
      maxEstimatedCostUsd: "",
      maxInputTokens: "",
      maxOutputTokensPerCall: "",
      maxProviderCalls: "",
      maxTotalOutputTokens: "",
    },
    players: [
      { kind: "profile", name: "", profileId: "ready-profile", strategy: "" },
      uniquePlayers(2)[1],
    ],
  });
  const missingBudgetErrors = observer.validateControlForm(missingBudgetForm, controlCatalog);
  [
    "maxEstimatedCostUsd",
    "maxInputTokens",
    "maxOutputTokensPerCall",
    "maxProviderCalls",
    "maxTotalOutputTokens",
  ].forEach((key) => {
    assert(missingBudgetErrors[`budget-${key}`], `profile use requires ${key}`);
  });

  const zeroTotalsForm = makeForm({
    budget: {
      maxEstimatedCostUsd: "0",
      maxInputTokens: "0",
      maxOutputTokensPerCall: "1",
      maxProviderCalls: "0",
      maxTotalOutputTokens: "0",
    },
  });
  const zeroTotalsErrors = observer.validateControlForm(zeroTotalsForm, controlCatalog);
  ["maxProviderCalls", "maxInputTokens", "maxTotalOutputTokens", "maxEstimatedCostUsd"].forEach((key) => {
    assert(zeroTotalsErrors[`budget-${key}`] === undefined, `${key} accepts its legal zero value`);
  });
  assert(
    zeroTotalsErrors["budget-maxOutputTokensPerCall"] === undefined,
    "per-call output accepts its minimum value of one",
  );
  equal(
    observer.controlRequestBody(zeroTotalsForm, controlCatalog).budget,
    {
      max_estimated_cost_usd: "0",
      max_input_tokens: "0",
      max_output_tokens_per_call: "1",
      max_provider_calls: "0",
      max_total_output_tokens: "0",
    },
    "legal zero totals remain canonical strings in the control request",
  );
  const zeroPerCallErrors = observer.validateControlForm(makeForm({
    budget: { ...budget, maxOutputTokensPerCall: "0" },
  }), controlCatalog);
  assert(
    zeroPerCallErrors["budget-maxOutputTokensPerCall"].includes("正整数"),
    "per-call output still rejects zero with a precise message",
  );
  const negativeCallsErrors = observer.validateControlForm(makeForm({
    budget: { ...budget, maxProviderCalls: "-1" },
  }), controlCatalog);
  assert(
    negativeCallsErrors["budget-maxProviderCalls"].includes("非负整数"),
    "total call limits reject negative values with a precise message",
  );
  const microCostForm = makeForm({
    budget: { ...budget, maxEstimatedCostUsd: "0.000001" },
  });
  assert(
    observer.validateControlForm(microCostForm, controlCatalog)["budget-maxEstimatedCostUsd"] === undefined,
    "the browser accepts the backend minimum six-decimal cost precision",
  );
  assert(
    observer.controlRequestBody(microCostForm, controlCatalog).budget.max_estimated_cost_usd === "0.000001",
    "six-decimal cost precision is preserved in the control request",
  );

  const requestForm = makeForm({
    llmTimeoutSeconds: "45.5",
    players: [
      { kind: "human", name: "  Alice  ", profileId: "", strategy: "" },
      { kind: "profile", name: "", profileId: "ready-profile", strategy: "" },
    ],
    rounds: "2",
    seed: "-7",
    timeoutSeconds: "60.25",
  });
  equal(
    observer.controlRequestBody(requestForm, controlCatalog),
    {
      allow_large_tournament: false,
      budget: {
        max_estimated_cost_usd: "1.250000",
        max_input_tokens: "200000",
        max_output_tokens_per_call: "4096",
        max_provider_calls: "64",
        max_total_output_tokens: "65536",
      },
      game: "math_quiz",
      human_timeout_seconds: 60.25,
      judges: [],
      llm_timeout_seconds: 45.5,
      mode: "play",
      players: [
        { kind: "human", name: "Alice" },
        { kind: "profile", profile_id: "ready-profile" },
      ],
      rounds: 2,
      seed: "-7",
      resume_championship_id: null,
      resume_tournament_id: null,
    },
    "the browser emits only the canonical control request schema",
  );

  const championshipForm = makeForm({
    mode: "championship",
    players: uniquePlayers(4),
    rounds: "2",
    seed: "23",
  });
  equal(
    observer.controlRequestBody(championshipForm, controlCatalog),
    {
      allow_large_tournament: false,
      budget: {
        max_estimated_cost_usd: "1.250000",
        max_input_tokens: "200000",
        max_output_tokens_per_call: "4096",
        max_provider_calls: "64",
        max_total_output_tokens: "65536",
      },
      game: "math_quiz",
      human_timeout_seconds: 300,
      judges: [],
      llm_timeout_seconds: 120,
      mode: "championship",
      players: [
        { kind: "mock", strategy: "strategy_0" },
        { kind: "mock", strategy: "strategy_1" },
        { kind: "mock", strategy: "strategy_2" },
        { kind: "mock", strategy: "strategy_3" },
      ],
      rounds: 2,
      seed: "23",
      resume_championship_id: null,
      resume_tournament_id: null,
    },
    "championship control emits the canonical fixed-bracket request without browser credentials",
  );

  const capability = "c".repeat(43);
  const controlJobPayload = {
    api_version: "v1",
    job: {
      created_at: "2026-08-15T00:00:00.000000Z",
      job_id: "job-7",
      participation_links: [
        {
          player_name: "Alice",
          url: `http://localhost:8000/participate/session-7/seat-1#capability=${capability}`,
        },
        {
          player_name: "Mallory",
          url: `http://evil.example/participate/session-7/seat-2#capability=${capability}`,
        },
      ],
      preview: { estimated_provider_calls: 2, match_count: 1, warnings: [] },
      spec: {
        budget: {
          max_estimated_cost_usd: "1.250000",
          max_input_tokens: "200000",
          max_output_tokens_per_call: "4096",
          max_provider_calls: "64",
          max_total_output_tokens: "65536",
        },
        game: "math_quiz",
        mode: "play",
        players: requestForm.players,
      },
      status: "running",
      updated_at: "2026-08-15T00:00:01.000000Z",
    },
  };
  const controlJob = observer.normalizeControlJob(controlJobPayload);
  equal(
    controlJob.participationLinks,
    [{
      href: `/participate/session-7/seat-1#capability=${capability}`,
      label: "Alice",
      seatId: "seat-1",
    }],
    "participation links are reduced to same-origin capability routes",
  );

  const creativeJob = observer.normalizeControlJob({
    api_version: "v1",
    job: {
      created_at: "2026-08-15T01:00:00.000000Z",
      job_id: "creative-job-1",
      participation_links: [],
      preview: {
        human_count: 0,
        match_count: 6,
        pairing_count: 3,
        player_count: 3,
        rated: false,
        requires_provider_budget: true,
        uses_frozen_budget: false,
        warnings: ["large_tournament"],
      },
      spec: {
        allow_large_tournament: true,
        budget: {
          max_estimated_cost_usd: "2.500000",
          max_input_tokens: "250000",
          max_output_tokens_per_call: "2048",
          max_provider_calls: "80",
          max_total_output_tokens: "80000",
        },
        game: "creative_writing",
        human_timeout_seconds: 75,
        judges: [
          { kind: "mock", strategy: "strict" },
          { kind: "profile", profile_id: "judge-panel" },
          { kind: "mock", strategy: "lenient" },
        ],
        llm_timeout_seconds: 33.5,
        mode: "round_robin",
        players: [
          { kind: "profile", profile_id: "writer-a" },
          { kind: "mock", strategy: "random" },
          { kind: "profile", profile_id: "writer-b" },
        ],
        resume_tournament_id: null,
        rounds: null,
        seed: "-11",
      },
      status: "prepared",
      updated_at: "2026-08-15T01:00:01.000000Z",
    },
  });
  equal(
    {
      allowLargeTournament: creativeJob.largeTournamentAllowed,
      budgetFromCheckpoint: creativeJob.budget.fromCheckpoint,
      game: creativeJob.game,
      humanTimeoutSeconds: creativeJob.humanTimeoutSeconds,
      isResume: creativeJob.isResume,
      judges: creativeJob.judges,
      llmTimeoutSeconds: creativeJob.llmTimeoutSeconds,
      players: creativeJob.players,
      rounds: creativeJob.rounds,
      seed: creativeJob.seed,
      usesFrozenBudget: creativeJob.budget.usesFrozenBudget,
    },
    {
      allowLargeTournament: true,
      budgetFromCheckpoint: false,
      game: "creative_writing",
      humanTimeoutSeconds: 75,
      isResume: false,
      judges: ["Mock · strict", "Profile · judge-panel", "Mock · lenient"],
      llmTimeoutSeconds: 33.5,
      players: ["Profile · writer-a", "Mock · random", "Profile · writer-b"],
      rounds: null,
      seed: "-11",
      usesFrozenBudget: false,
    },
    "new creative jobs expose only normalized configuration identities and limits",
  );

  const resumeJob = observer.normalizeControlJob({
    api_version: "v1",
    job: {
      created_at: "2026-08-15T02:00:00.000000Z",
      job_id: "resume-job-1",
      participation_links: [],
      preview: {
        frozen_game: "math_quiz",
        frozen_judges: ["Mock · checkpoint-strict", "Profile · checkpoint-judge"],
        frozen_llm_timeout_seconds: 91.25,
        frozen_players: ["甲", "乙", "丙"],
        frozen_rounds: 3,
        frozen_seed: "23",
        human_count: 0,
        match_count: 6,
        pairing_count: 3,
        player_count: 3,
        prepared_profiles: [{
          configuration_digest: "a".repeat(64),
          default_model: "model-b",
          display_name: "Local profile",
          effective_models: ["frozen-model"],
          profile_id: "local",
          provider: "ollama",
        }],
        rated: true,
        requires_provider_budget: true,
        uses_frozen_budget: true,
        warnings: ["resume_uses_frozen_configuration"],
      },
      spec: {
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
        mode: "round_robin",
        players: [],
        resume_tournament_id: "tournament-23",
        rounds: null,
        seed: "0",
      },
      status: "prepared",
      updated_at: "2026-08-15T02:00:01.000000Z",
    },
  });
  equal(
    {
      budgetFromCheckpoint: resumeJob.budget.fromCheckpoint,
      frozenBudgetConfigured: resumeJob.budget.usesFrozenBudget,
      game: resumeJob.game,
      humanTimeoutSeconds: resumeJob.humanTimeoutSeconds,
      isResume: resumeJob.isResume,
      judges: resumeJob.judges,
      llmTimeoutSeconds: resumeJob.llmTimeoutSeconds,
      players: resumeJob.players,
      preparedProfile: resumeJob.preparedProfiles[0],
      rounds: resumeJob.rounds,
      seed: resumeJob.seed,
      tournamentId: resumeJob.tournamentId,
    },
    {
      budgetFromCheckpoint: true,
      frozenBudgetConfigured: true,
      game: "math_quiz",
      humanTimeoutSeconds: 120,
      isResume: true,
      judges: ["Mock · checkpoint-strict", "Profile · checkpoint-judge"],
      llmTimeoutSeconds: 91.25,
      players: ["甲", "乙", "丙"],
      preparedProfile: {
        defaultModel: "model-b",
        displayName: "Local profile",
        effectiveModels: ["frozen-model"],
        label: "Local profile · ollama / 执行 frozen-model（当前默认 model-b）",
        profileId: "local",
        provider: "ollama",
      },
      rounds: 3,
      seed: "23",
      tournamentId: "tournament-23",
    },
    "resume jobs hydrate only the backend-validated frozen checkpoint summary",
  );
  equal(
    resumeJob.warnings,
    ["恢复任务会沿用 checkpoint 中冻结的项目、参赛者、裁判与随机种子；若 checkpoint 含 Provider 硬预算，也会沿用该冻结预算。"],
    "resume warnings describe the frozen budget conditionally",
  );
  ["cancelled", "failed", "interrupted"].forEach((status) => {
    assert(
      observer.controlJobCanResume({ ...resumeJob, resumable: true, status }),
      `${status} round-robin jobs can expose validated checkpoint recovery`,
    );
  });
  assert(
    !observer.controlJobCanResume({ ...resumeJob, resumable: true, status: "running" }),
    "an active job never exposes checkpoint recovery",
  );
  equal(
    observer.resumeControlRequest(resumeJob),
    {
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
      mode: "round_robin",
      players: [],
      resume_championship_id: null,
      resume_tournament_id: "tournament-23",
      rounds: null,
      seed: "0",
    },
    "round-robin recovery sends only its checkpoint identity",
  );

  const championshipJobBase = {
    created_at: "2026-08-15T03:00:00.000000Z",
    championship_id: "championship-43",
    job_id: "championship-job-43",
    live_id: "live-championship-43",
    participation_links: [],
    preview: {
      human_count: 0,
      match_count: 6,
      pairing_count: 3,
      player_count: 4,
      rated: false,
      requires_provider_budget: false,
      uses_frozen_budget: false,
      warnings: [],
    },
    spec: {
      allow_large_tournament: false,
      budget: {
        max_estimated_cost_usd: null,
        max_input_tokens: null,
        max_output_tokens_per_call: null,
        max_provider_calls: null,
        max_total_output_tokens: null,
      },
      game: "math_quiz",
      human_timeout_seconds: 120,
      judges: [],
      llm_timeout_seconds: 120,
      mode: "championship",
      players: [
        { kind: "mock", strategy: "random" },
        { kind: "mock", strategy: "fixed" },
        { kind: "mock", strategy: "illegal" },
        { kind: "mock", strategy: "balanced" },
      ],
      resume_championship_id: null,
      resume_tournament_id: null,
      rounds: 2,
      seed: "23",
    },
    status: "running",
    updated_at: "2026-08-15T03:00:01.000000Z",
  };
  const championshipJob = observer.normalizeControlJob({
    api_version: "v1",
    job: championshipJobBase,
  });
  equal(
    {
      championshipId: championshipJob.championshipId,
      isResume: championshipJob.isResume,
      mode: championshipJob.mode,
      players: championshipJob.players,
      tournamentId: championshipJob.tournamentId,
    },
    {
      championshipId: "championship-43",
      isResume: false,
      mode: "championship",
      players: ["Mock · random", "Mock · fixed", "Mock · illegal", "Mock · balanced"],
      tournamentId: null,
    },
    "a championship control job exposes its public bracket identity without hidden entrant IDs",
  );

  const resumeChampionshipJob = observer.normalizeControlJob({
    api_version: "v1",
    job: {
      ...championshipJobBase,
      championship_id: "championship-checkpoint-43",
      job_id: "resume-championship-job-43",
      live_id: null,
      preview: {
        ...championshipJobBase.preview,
        frozen_game: "math_quiz",
        frozen_judges: [],
        frozen_llm_timeout_seconds: 91.25,
        frozen_players: ["Alice", "Bob", "Carol", "Dora"],
        frozen_rounds: 2,
        frozen_seed: "23",
        uses_frozen_budget: true,
        warnings: ["resume_uses_frozen_configuration"],
      },
      resumable: true,
      spec: {
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
        resume_championship_id: "championship-checkpoint-43",
        resume_tournament_id: null,
        rounds: null,
        seed: "0",
      },
      status: "interrupted",
    },
  });
  equal(
    {
      championshipId: resumeChampionshipJob.championshipId,
      game: resumeChampionshipJob.game,
      isResume: resumeChampionshipJob.isResume,
      players: resumeChampionshipJob.players,
      rounds: resumeChampionshipJob.rounds,
      seed: resumeChampionshipJob.seed,
    },
    {
      championshipId: "championship-checkpoint-43",
      game: "math_quiz",
      isResume: true,
      players: ["Alice", "Bob", "Carol", "Dora"],
      rounds: 2,
      seed: "23",
    },
    "championship recovery hydrates only the validated frozen checkpoint summary",
  );
  assert(
    observer.controlJobCanResume(resumeChampionshipJob),
    "an interrupted resumable championship exposes checkpoint recovery",
  );
  assert(
    !observer.controlJobCanResume({ ...resumeChampionshipJob, status: "running" }),
    "an active championship never exposes checkpoint recovery",
  );
  equal(
    observer.resumeControlRequest(resumeChampionshipJob),
    {
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
      resume_championship_id: "championship-checkpoint-43",
      resume_tournament_id: null,
      rounds: null,
      seed: "0",
    },
    "championship recovery sends only its checkpoint identity",
  );

  const completedChampionshipJob = observer.normalizeControlJob({
    api_version: "v1",
    job: {
      ...championshipJobBase,
      final_id: "championship-43",
      final_kind: "championship",
      final_match_ids: completedChampionshipSummary.final_match_ids,
      finished_at: "2026-08-15T03:01:00.000000Z",
      status: "completed",
    },
  });
  equal(
    {
      championshipId: completedChampionshipJob.championshipId,
      finalId: completedChampionshipJob.finalId,
      finalKind: completedChampionshipJob.finalKind,
      finalMatchIds: completedChampionshipJob.finalMatchIds,
    },
    {
      championshipId: "championship-43",
      finalId: "championship-43",
      finalKind: "championship",
      finalMatchIds: completedChampionshipSummary.final_match_ids,
    },
    "a completed championship job preserves its canonical archive links",
  );
  throwsCode(
    () => observer.normalizeControlJob({ job: { ...controlJobPayload.job, job_id: "../job" } }),
    "protocol_error",
    "an unsafe job identifier fails closed",
  );
  throwsCode(
    () => observer.normalizeControlJob({ job: { ...controlJobPayload.job, status: "owned-by-attacker" } }),
    "protocol_error",
    "an unknown job status fails closed",
  );
  throwsCode(
    () => observer.normalizeControlJob({ job: { ...controlJobPayload.job, spec: { ...controlJobPayload.job.spec, mode: "shell" } } }),
    "protocol_error",
    "an unknown job mode fails closed",
  );
  throwsCode(
    () => observer.normalizeControlJob({
      job: {
        ...controlJobPayload.job,
        preview: {
          ...controlJobPayload.job.preview,
          prepared_profiles: [{
            configuration_digest: "b".repeat(64),
            default_model: "model-b",
            display_name: "Local profile",
            profile_id: "local",
            provider: "ollama",
          }],
        },
      },
    }),
    "protocol_error",
    "a prepared Profile without effective execution models fails closed",
  );

  if (typeof console !== "undefined" && console.log) {
    console.log("observer client state tests passed");
  } else if (typeof print === "function") {
    print("observer client state tests passed");
  }
})();
