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

  if (typeof console !== "undefined" && console.log) {
    console.log("observer client state tests passed");
  } else if (typeof print === "function") {
    print("observer client state tests passed");
  }
})();
