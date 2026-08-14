"use strict";

(() => {
  globalThis.__LLMOLYMPIC_ENABLE_TEST_HOOKS__ = true;
  globalThis.__LLMOLYMPIC_SKIP_BOOTSTRAP__ = true;

  if (typeof require === "function") {
    require("../llmolympic/web/static/assets/app.js");
  } else {
    load("llmolympic/web/static/assets/app.js");
  }

  const observer = globalThis.__LLMOLYMPIC_OBSERVER_TEST__;
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const equal = (actual, expected, message) => {
    assert(JSON.stringify(actual) === JSON.stringify(expected), `${message}: ${JSON.stringify(actual)}`);
  };

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

  if (typeof console !== "undefined" && console.log) {
    console.log("observer client state tests passed");
  } else if (typeof print === "function") {
    print("observer client state tests passed");
  }
})();
