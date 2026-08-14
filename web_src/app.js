import React, {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";

const h = React.createElement;

const API_VERSION = "v1";
const MAX_MATCHES = 100;
const MAX_MOVE_CHARACTERS = 4096;
const PARTICIPATION_POLL_INTERVAL = 1000;
const SAFE_PUBLIC_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const CAPABILITY_TOKEN = /^[A-Za-z0-9_-]{32,256}$/;
const EVENT_LABELS = {
  match_started: "比赛开始",
  turn_prompt: "题面 / 局面",
  move_received: "选手提交",
  move_rejected: "提交被拒",
  match_finished: "比赛结束",
};
const GAME_LABELS = {
  chess: "国际象棋",
  creative_writing: "创意写作",
  gomoku: "五子棋",
  knowledge_quiz: "知识问答",
  math_quiz: "数学问答",
  reasoning_quiz: "逻辑推理",
  riddle_quiz: "猜谜竞答",
};
const ERROR_COPY = {
  archive_invalid: "存档未通过安全校验，暂时无法展示。",
  archive_not_replayable: "这份历史存档不支持安全回放。",
  archive_too_large: "存档超过安全回放上限，无法在页面中打开。",
  cross_origin_forbidden: "请从 llmolympic web 输出的本机地址打开观战页。",
  database_busy: "数据库正在忙，请稍后重试。",
  database_unavailable: "无法读取本地数据库。请确认数据库存在且版本兼容。",
  invalid_host: "当前地址不受支持，请使用命令行显示的本机地址。",
  invalid_request: "请求无效，请返回观战大厅后重试。",
  capability_invalid: "参与凭证无效或已经失效。请使用命令行最新输出的完整链接。",
  live_not_found: "这场运行中的比赛不存在，或直播保留期已经结束。",
  live_unavailable: "本机实时事件流暂时不可用，比赛本身不受影响。",
  match_not_found: "对局不存在，或已经不再可用。",
  network_error: "与本机观战服务的连接中断。",
  overloaded: "回放服务正忙，请稍后重试。",
  participation_expired: "这个参与席位已经过期，请重新从命令行开始比赛。",
  participation_not_found: "这个参与席位不存在，或已经不再可用。",
  participation_unavailable: "本机人类输入服务暂时不可用，比赛进程会按既定超时规则处理。",
  protocol_error: "页面与服务的回放协议不一致。",
  request_expired: "本轮提交时间已结束，请等待最新题面。",
  request_failed: "暂时无法加载数据，请稍后重试。",
  request_not_found: "本轮题面已更新，请等待页面同步。",
  request_stale: "本轮题面已更新，请根据最新题面重新提交。",
  request_too_large: "提交内容超过 Web 请求上限，请缩短后重试。",
  submission_conflict: "本轮已经收到另一份提交，请等待比赛继续。",
};
const PUBLIC_ERROR_CODES = new Set(Object.keys(ERROR_COPY));
const RETRYABLE_CLOSE_CODES = new Set([1000, 1006, 1011, 1012, 1013, 4429]);
const TERMINAL_CLOSE_CODES = {
  4400: "invalid_request",
  4403: "cross_origin_forbidden",
  4404: "match_not_found",
};

function classifyReplayClose(code, reason, retryCount, transferComplete) {
  if (transferComplete) return { action: "ready" };
  if (RETRYABLE_CLOSE_CODES.has(code) && retryCount < 3) {
    return { action: "retry", delay: 500 * (2 ** retryCount) };
  }
  const publicCode = PUBLIC_ERROR_CODES.has(reason) ? reason : TERMINAL_CLOSE_CODES[code];
  return publicCode
    ? { action: "error", code: publicCode }
    : { action: "fallback" };
}

function playbackReducer(state, action) {
  const available = Number.isInteger(action.available) && action.available > 0
    ? action.available
    : 0;
  const cursor = Math.max(0, Math.min(state.cursor, available));
  const blocked = Boolean(action.error) || available === 0;

  if (action.type === "reset") {
    return { cursor: 0, playing: Boolean(action.autoPlay) && !action.error };
  }
  if (action.type === "pause") {
    return state.playing ? { cursor, playing: false } : state;
  }
  if (action.type === "toggle") {
    if (blocked) return state.playing ? { cursor, playing: false } : state;
    if (action.transferComplete && cursor >= available) {
      return { cursor: 0, playing: true };
    }
    return { cursor, playing: !state.playing };
  }
  if (action.type === "restart") {
    return blocked ? { cursor: 0, playing: false } : { cursor: 0, playing: true };
  }
  if (action.type === "step-back") {
    return { cursor: Math.max(0, cursor - 1), playing: false };
  }
  if (action.type === "step-forward") {
    return { cursor: Math.min(cursor + 1, available), playing: false };
  }
  if (action.type === "seek") {
    const requested = Number.isFinite(action.cursor) ? action.cursor : cursor;
    return { cursor: Math.max(0, Math.min(requested, available)), playing: false };
  }
  if (action.type === "tick") {
    if (!state.playing) return state;
    if (cursor >= available) {
      return action.transferComplete ? { cursor, playing: false } : state;
    }
    const next = Math.min(cursor + 1, available);
    return {
      cursor: next,
      playing: !(action.transferComplete && next >= available),
    };
  }
  return state;
}

class PublicError extends Error {
  constructor(code, status = 0) {
    super(code);
    this.name = "PublicError";
    this.code = code;
    this.status = status;
  }
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function gameLabel(game) {
  return GAME_LABELS[game] || game || "未知项目";
}

function errorCopy(error) {
  const code = error && error.code ? error.code : "request_failed";
  return ERROR_COPY[code] || ERROR_COPY.request_failed;
}

function dateTime(value, detail = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: detail ? "medium" : undefined,
    timeStyle: detail ? "medium" : "short",
  }).format(date);
}

function formatScore(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}

function winnerNames(match) {
  const values = match.players
    .map((player) => [player, match.scores[player]])
    .filter((item) => typeof item[1] === "number" && Number.isFinite(item[1]));
  if (!values.length) return new Set();
  const top = Math.max(...values.map((item) => item[1]));
  return new Set(values.filter((item) => item[1] === top).map((item) => item[0]));
}

async function fetchJSON(path, signal) {
  let response;
  try {
    response = await fetch(path, {
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    throw new PublicError("network_error");
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = null;
  }
  if (!response.ok) {
    const code = payload && payload.error && payload.error.code;
    throw new PublicError(typeof code === "string" ? code : "request_failed", response.status);
  }
  if (!isObject(payload) || payload.api_version !== API_VERSION) {
    throw new PublicError("protocol_error");
  }
  return payload;
}

const participationCapabilities = new Map();

function participationCredentialKey(sessionId, seatId) {
  return `llmolympic.participation.${sessionId}.${seatId}`;
}

function participationComponentKey(sessionId, seatId) {
  return `${encodeURIComponent(sessionId)}/${encodeURIComponent(seatId)}`;
}

function participationPath(pathname) {
  const match = pathname.match(/^\/participate\/([^/]+)\/([^/]+)$/);
  if (!match) return null;
  try {
    const sessionId = decodeURIComponent(match[1]);
    const seatId = decodeURIComponent(match[2]);
    return SAFE_PUBLIC_ID.test(sessionId) && SAFE_PUBLIC_ID.test(seatId)
      ? { seatId, sessionId }
      : null;
  } catch (_error) {
    return null;
  }
}

function captureParticipationCapability() {
  if (typeof window === "undefined") return;
  const route = participationPath(window.location.pathname);
  if (!route || !window.location.hash) return;
  const storageKey = participationCredentialKey(route.sessionId, route.seatId);
  let capability = null;
  try {
    const parameters = new URLSearchParams(window.location.hash.slice(1));
    const keys = Array.from(parameters.keys());
    const candidates = parameters.getAll("capability");
    if (keys.length === 1 && keys[0] === "capability" && candidates.length === 1) {
      capability = CAPABILITY_TOKEN.test(candidates[0]) ? candidates[0] : null;
    }
  } catch (_error) {
    capability = null;
  }

  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
  participationCapabilities.delete(storageKey);
  try {
    window.sessionStorage.removeItem(storageKey);
  } catch (_error) {
    // Private browsing can deny Web Storage; the in-memory fallback still works.
  }
  if (!capability) return;
  participationCapabilities.set(storageKey, capability);
  try {
    window.sessionStorage.setItem(storageKey, capability);
  } catch (_error) {
    // Keep the capability only in this page when Web Storage is unavailable.
  }
}

function readParticipationCapability(sessionId, seatId) {
  const storageKey = participationCredentialKey(sessionId, seatId);
  const captured = participationCapabilities.get(storageKey);
  if (captured) return captured;
  try {
    const stored = window.sessionStorage.getItem(storageKey);
    if (stored && CAPABILITY_TOKEN.test(stored)) {
      participationCapabilities.set(storageKey, stored);
      return stored;
    }
  } catch (_error) {
    // Missing Web Storage is handled as a missing credential.
  }
  return null;
}

function clearParticipationCapability(sessionId, seatId) {
  const storageKey = participationCredentialKey(sessionId, seatId);
  participationCapabilities.delete(storageKey);
  try {
    window.sessionStorage.removeItem(storageKey);
  } catch (_error) {
    // Nothing else to clear.
  }
}

function participationKeepsCapability(status) {
  return status === "active";
}

function participationErrorClearsCapability(status) {
  return [401, 403, 404, 410].includes(status);
}

function exactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = keys.slice().sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function validTimestamp(value) {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

function validateParticipationRequest(request) {
  if (!isObject(request) || !exactKeys(request, [
    "created_at",
    "expires_at",
    "match_event_seq",
    "prompt",
    "request_id",
    "request_seq",
    "state",
  ])) return false;
  return SAFE_PUBLIC_ID.test(request.request_id)
    && Number.isInteger(request.request_seq)
    && request.request_seq >= 0
    && Number.isInteger(request.match_event_seq)
    && request.match_event_seq >= 0
    && ["pending", "submitted", "consumed", "accepted", "rejected", "expired", "cancelled"].includes(request.state)
    && typeof request.prompt === "string"
    && Array.from(request.prompt).length <= 65536
    && validTimestamp(request.created_at)
    && validTimestamp(request.expires_at)
    && Date.parse(request.expires_at) > Date.parse(request.created_at);
}

function validateParticipationSnapshot(snapshot, sessionId, seatId) {
  if (!isObject(snapshot) || !exactKeys(snapshot, [
    "api_version",
    "created_at",
    "final_match_id",
    "game",
    "lease_expires_at",
    "player_name",
    "players",
    "request",
    "seat_id",
    "session_id",
    "status",
    "updated_at",
  ])) return false;
  if (
    snapshot.api_version !== API_VERSION
    || snapshot.session_id !== sessionId
    || snapshot.seat_id !== seatId
    || !["active", "completed", "interrupted", "expired"].includes(snapshot.status)
    || typeof snapshot.game !== "string"
    || !/^[a-z][a-z0-9_]{0,63}$/.test(snapshot.game)
    || typeof snapshot.player_name !== "string"
    || !snapshot.player_name
    || Array.from(snapshot.player_name).length > 512
    || !Array.isArray(snapshot.players)
    || snapshot.players.length !== 2
    || new Set(snapshot.players).size !== 2
    || snapshot.players.some((player) => typeof player !== "string"
      || !player
      || Array.from(player).length > 512)
    || !snapshot.players.includes(snapshot.player_name)
    || !validTimestamp(snapshot.created_at)
    || !validTimestamp(snapshot.updated_at)
    || !validTimestamp(snapshot.lease_expires_at)
    || Date.parse(snapshot.updated_at) < Date.parse(snapshot.created_at)
    || Date.parse(snapshot.lease_expires_at) <= Date.parse(snapshot.created_at)
  ) return false;
  if (snapshot.request !== null && !validateParticipationRequest(snapshot.request)) return false;
  if (snapshot.status !== "active" && snapshot.request !== null) return false;
  if (snapshot.status === "completed") {
    return typeof snapshot.final_match_id === "string"
      && SAFE_PUBLIC_ID.test(snapshot.final_match_id);
  }
  return snapshot.final_match_id === null;
}

function validateParticipationSubmission(payload, requestId) {
  return isObject(payload)
    && exactKeys(payload, ["api_version", "request_id", "status"])
    && payload.api_version === API_VERSION
    && payload.request_id === requestId
    && ["submitted", "duplicate"].includes(payload.status);
}

async function fetchParticipationJSON(path, capability, { body = null, method = "GET", signal } = {}) {
  let response;
  const headers = {
    Accept: "application/json",
    Authorization: `Bearer ${capability}`,
  };
  if (body !== null) headers["Content-Type"] = "application/json";
  try {
    response = await fetch(path, {
      body: body === null ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "same-origin",
      headers,
      method,
      referrerPolicy: "no-referrer",
      signal,
    });
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    throw new PublicError("network_error");
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = null;
  }
  if (!response.ok) {
    const candidate = payload && payload.error && payload.error.code;
    const code = PUBLIC_ERROR_CODES.has(candidate) ? candidate : "request_failed";
    throw new PublicError(code, response.status);
  }
  if (!isObject(payload) || payload.api_version !== API_VERSION) {
    throw new PublicError("protocol_error");
  }
  return payload;
}

function countCharacters(value) {
  return Array.from(value).length;
}

function createSubmissionId() {
  if (!globalThis.crypto || typeof globalThis.crypto.getRandomValues !== "function") {
    throw new PublicError("request_failed");
  }
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function navigate(path) {
  if (window.location.pathname + window.location.search === path) return;
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo({ top: 0, behavior: "auto" });
}

function AppLink({ href, className, children, ...props }) {
  return h(
    "a",
    {
      ...props,
      className,
      href,
      onClick: (event) => {
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey
        ) return;
        event.preventDefault();
        navigate(href);
      },
    },
    children,
  );
}

function StatusPill({ health }) {
  const loading = !health;
  const ok = health && health.status === "ok" && health.database_available;
  return h(
    "span",
    { className: `status-pill ${loading ? "" : ok ? "ok" : "bad"}` },
    h("span", { className: "status-dot", "aria-hidden": "true" }),
    loading ? "正在检查" : ok ? "数据库可用" : "数据库不可用",
  );
}

function Header({ health, participation = false }) {
  return h(
    "header",
    { className: "topbar" },
    h(
      "div",
      { className: "topbar-inner" },
      h(
        AppLink,
        { className: "brand", href: "/", "aria-label": "LLM Olympics 首页" },
        h("span", { className: "brand-mark", "aria-hidden": "true" }, "L²"),
        h(
          "span",
          null,
          h("span", { className: "brand-name" }, "LLM Olympics"),
          h("span", { className: "brand-sub" }, participation ? "HUMAN PARTICIPATION" : "ARCHIVE OBSERVER"),
        ),
      ),
      h(
        "div",
        { className: "status-cluster" },
        h("span", { className: "status-pill local" }, participation ? "本机 · 安全参赛" : "本机 · 只读观战"),
        participation ? null : h(StatusPill, { health }),
      ),
    ),
  );
}

function StateCard({ title, copy, error = false, action = null }) {
  return h(
    "div",
    { className: `state-card${error ? " error" : ""}` },
    h("strong", null, title),
    h("p", null, copy),
    action ? h("div", { className: "state-action" }, action) : null,
  );
}

function LoadingRows({ count = 3 }) {
  return h(Fragment, null, ...Array.from({ length: count }, (_, index) => h("div", {
    className: "skeleton",
    key: index,
    "aria-hidden": "true",
  })));
}

function MatchCard({ match }) {
  const winners = winnerNames(match);
  const tie = winners.size > 1;
  const context = match.tournament_id
    ? `循环赛 ${match.pairing_number || "?"}/${match.pairing_count || "?"}`
    : match.series_id
      ? `双局赛 · 第 ${match.leg_number || "?"} 局`
      : match.rated
        ? "已计入 ELO"
        : "未评级";
  return h(
    AppLink,
    {
      className: "match-card",
      href: `/matches/${encodeURIComponent(match.match_id)}`,
      "aria-label": `${gameLabel(match.game)}，${match.players.join(" 对 ")}，查看回放`,
    },
    h(
      "div",
      { className: "match-game" },
      h("span", { className: "game-badge" }, gameLabel(match.game)),
      h("time", { className: "meta", dateTime: match.finished_at }, dateTime(match.finished_at, true)),
    ),
    h(
      "div",
      { className: "versus" },
      ...match.players.map((player) => h(
        "div",
        { className: "player-line", key: player },
        h(
          "span",
          { className: `player-name${winners.has(player) && !tie ? " winner" : ""}` },
          player,
        ),
        h("span", { className: "score" }, formatScore(match.scores[player])),
      )),
      h("span", { className: "meta" }, tie ? `平局 · ${context}` : context),
    ),
    h("span", { className: "match-arrow", "aria-hidden": "true" }, "→"),
  );
}

function RecentMatches({ data, loading, error, limit, onMore, onRetry, game }) {
  let body;
  if (loading) {
    body = h(LoadingRows, { count: 4 });
  } else if (error) {
    body = h(StateCard, {
      title: "最近对局加载失败",
      copy: errorCopy(error),
      error: true,
      action: h("button", { className: "button small", onClick: onRetry }, "重试"),
    });
  } else if (!data || !data.matches.length) {
    body = h(StateCard, {
      title: game ? "这个项目还没有存档" : "还没有已完成对局",
      copy: game
        ? "可以切换到全部项目，或先在命令行完成一场比赛。"
        : "先运行一场比赛；完成并存档后，它会出现在这里。",
    });
  } else {
    body = h(
      Fragment,
      null,
      h("div", { className: "match-list" }, ...data.matches.map((match) => h(MatchCard, {
        key: match.match_id,
        match,
      }))),
      data.matches.length >= limit && limit < MAX_MATCHES
        ? h("button", { className: "button load-more", onClick: onMore }, "显示更多对局")
        : null,
    );
  }
  return h(
    "section",
    { className: "panel", "aria-labelledby": "recent-heading" },
    h(
      "div",
      { className: "panel-head" },
      h("h2", { id: "recent-heading" }, "最近对局"),
      h("span", { className: "panel-kicker" }, "按完成时间排序"),
    ),
    h("div", { className: "panel-body" }, body),
  );
}

function Leaderboard({ data, loading, error, onRetry }) {
  let body;
  if (loading) {
    body = h(LoadingRows, { count: 4 });
  } else if (error) {
    body = h(StateCard, {
      title: "排行榜加载失败",
      copy: errorCopy(error),
      error: true,
      action: h("button", { className: "button small", onClick: onRetry }, "重试"),
    });
  } else if (!data || !data.entries.length) {
    body = h(StateCard, {
      title: "暂无评分数据",
      copy: "未评级对局不会进入 ELO 榜；完成一场可评级的双人对局后再来看看。",
    });
  } else {
    body = h(
      "div",
      { "aria-label": "ELO 排行榜横向滚动区域", className: "table-scroll", tabIndex: 0 },
      h(
        "table",
        null,
        h("thead", null, h("tr", null,
          h("th", { scope: "col" }, "排名"),
          h("th", { scope: "col" }, "选手"),
          h("th", { scope: "col" }, "ELO"),
          h("th", { scope: "col" }, "胜 / 和 / 负"),
        )),
        h("tbody", null, ...data.entries.map((entry, index) => h(
          "tr",
          { key: `${entry.player}-${index}` },
          h("td", { className: "rank" }, String(index + 1).padStart(2, "0")),
          h("td", null,
            h("strong", null, entry.player),
            h("div", { className: "meta" }, `${entry.games_played} 场 · 更新 ${dateTime(entry.updated_at, true)}`),
          ),
          h("td", { className: "rating", title: String(entry.rating) }, entry.rating.toFixed(1)),
          h("td", null, `${entry.wins} / ${entry.draws} / ${entry.losses}`),
        ))),
      ),
    );
  }
  return h(
    "section",
    { className: "panel leaderboard-panel", "aria-labelledby": "leaderboard-heading" },
    h(
      "div",
      { className: "panel-head" },
      h("h2", { id: "leaderboard-heading" }, "ELO 排行榜"),
      h("span", { className: "panel-kicker" }, data && data.game ? gameLabel(data.game) : "总榜"),
    ),
    h("div", { className: "panel-body" }, body),
  );
}

function modeLabel(mode) {
  if (mode === "series") return "双局赛";
  if (mode === "round_robin") return "循环赛";
  return "单场对局";
}

function liveStatusLabel(status) {
  if (status === "completed") return "已完成并存档";
  if (status === "interrupted") return "直播已中断";
  return "正在进行";
}

function LiveMatchCard({ match }) {
  const context = match.mode === "round_robin" && match.pairing_number
    ? `第 ${match.pairing_number}/${match.pairing_count || "?"} 组 · 第 ${match.leg_number || "?"} 局`
    : match.mode === "series" && match.leg_number
      ? `第 ${match.leg_number}/2 局`
      : `${match.event_count} 条事件`;
  return h(
    AppLink,
    {
      className: `live-card ${match.status}`,
      href: `/live/${encodeURIComponent(match.live_id)}`,
      "aria-label": `${gameLabel(match.game)}，${match.players.join(" 对 ")}，${liveStatusLabel(match.status)}`,
    },
    h("span", { className: "live-pulse", "aria-hidden": "true" }),
    h("span", { className: "game-badge" }, gameLabel(match.game)),
    h("span", { className: "live-card-copy" },
      h("strong", null, match.players.join(" 对 ")),
      h("span", { className: "meta" }, `${modeLabel(match.mode)} · ${context}`),
    ),
    h("span", { className: "live-card-status" }, liveStatusLabel(match.status)),
    h("span", { className: "match-arrow", "aria-hidden": "true" }, "→"),
  );
}

function LiveMatches({ data, loading, error, onRetry }) {
  const matches = data && Array.isArray(data.matches) ? data.matches : [];
  let body;
  if (loading && !data) {
    body = h(LoadingRows, { count: 1 });
  } else if (error && !data) {
    body = h(StateCard, {
      title: "实时事件流暂不可用",
      copy: errorCopy(error),
      error: true,
      action: h("button", { className: "button small", onClick: onRetry }, "重试"),
    });
  } else if (!matches.length) {
    body = h(StateCard, {
      title: "当前没有运行中的比赛",
      copy: "启动 play、series 或 round-robin 后，公开事件会自动出现在这里；比赛进程不依赖观战页。",
    });
  } else {
    body = h("div", { className: "live-list" }, ...matches.map((match) => h(LiveMatchCard, {
      key: match.live_id,
      match,
    })));
  }
  return h(
    "section",
    { className: "panel live-panel", "aria-labelledby": "live-heading" },
    h("div", { className: "panel-head" },
      h("h2", { id: "live-heading" }, "实时观战"),
      h("span", { className: "panel-kicker" }, "每 2 秒发现本机运行"),
    ),
    h("div", { className: "panel-body" }, body),
  );
}

function Lobby({ health, games, initialGame, refreshAll }) {
  const [game, setGame] = useState(initialGame);
  const [limit, setLimit] = useState(20);
  const [refreshKey, setRefreshKey] = useState(0);
  const [matches, setMatches] = useState(null);
  const [leaderboard, setLeaderboard] = useState(null);
  const [matchesError, setMatchesError] = useState(null);
  const [leaderboardError, setLeaderboardError] = useState(null);
  const [matchesLoading, setMatchesLoading] = useState(true);
  const [leaderboardLoading, setLeaderboardLoading] = useState(true);
  const [liveData, setLiveData] = useState(null);
  const [liveError, setLiveError] = useState(null);
  const [liveLoading, setLiveLoading] = useState(true);

  const retry = useCallback(() => setRefreshKey((value) => value + 1), []);

  useEffect(() => {
    setGame(initialGame);
    setLimit(20);
  }, [initialGame]);

  useEffect(() => {
    const controller = new AbortController();
    const query = game ? `&game=${encodeURIComponent(game)}` : "";
    setMatchesLoading(true);
    setLeaderboardLoading(true);
    setMatchesError(null);
    setLeaderboardError(null);
    fetchJSON(`/api/v1/matches?limit=${limit}${query}`, controller.signal)
      .then(setMatches)
      .catch((error) => { if (error.name !== "AbortError") setMatchesError(error); })
      .finally(() => { if (!controller.signal.aborted) setMatchesLoading(false); });
    fetchJSON(`/api/v1/leaderboard?limit=50${query}`, controller.signal)
      .then(setLeaderboard)
      .catch((error) => { if (error.name !== "AbortError") setLeaderboardError(error); })
      .finally(() => { if (!controller.signal.aborted) setLeaderboardLoading(false); });
    return () => controller.abort();
  }, [game, limit, refreshKey]);

  useEffect(() => {
    let cancelled = false;
    let controller = null;
    const load = () => {
      if (cancelled || document.visibilityState === "hidden") return;
      if (controller) controller.abort();
      controller = new AbortController();
      const query = game ? `&game=${encodeURIComponent(game)}` : "";
      setLiveLoading(true);
      fetchJSON(`/api/v1/live?limit=20${query}`, controller.signal)
        .then((payload) => {
          if (!cancelled) {
            setLiveData(payload);
            setLiveError(null);
          }
        })
        .catch((error) => {
          if (!cancelled && error.name !== "AbortError") setLiveError(error);
        })
        .finally(() => { if (!cancelled) setLiveLoading(false); });
    };
    load();
    const timer = window.setInterval(load, 2000);
    const onVisibility = () => { if (document.visibilityState === "visible") load(); };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      if (controller) controller.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [game, refreshKey]);

  const chooseGame = (value) => {
    setGame(value);
    setLimit(20);
    const url = new URL(window.location.href);
    if (value) url.searchParams.set("game", value);
    else url.searchParams.delete("game");
    window.history.replaceState({}, "", url.pathname + url.search);
  };

  const refresh = () => {
    retry();
    refreshAll();
  };

  const matchCount = matches && matches.matches ? matches.matches.length : 0;
  return h(
    "main",
    { className: "page", id: "main-content", tabIndex: -1 },
    h(
      "section",
      { className: "hero", "aria-labelledby": "lobby-title" },
      h(
        "div",
        null,
        h("p", { className: "eyebrow" }, "LIVE + ARCHIVE OBSERVER"),
        h("h1", { id: "lobby-title" }, "每一场模型较量，都有迹可循。"),
        h("p", { className: "hero-copy" }, "只读观看本机正在运行的比赛，或浏览已经完成并存档的比赛、ELO 排名与事件回放。页面不会连接模型服务或提交落子。"),
      ),
      h("div", { className: "hero-stat", "aria-label": `当前显示 ${matchCount} 场对局` },
        h("strong", null, matchesLoading ? "—" : String(matchCount).padStart(2, "0")),
        h("span", null, game ? `${gameLabel(game)} · 当前列表` : "场 · 当前列表"),
      ),
    ),
    h(
      "div",
      { className: "toolbar" },
      h(
        "div",
        { className: "filter-wrap" },
        h("label", { htmlFor: "game-filter" }, "比赛项目"),
        h(
          "select",
          { id: "game-filter", value: game, onChange: (event) => chooseGame(event.target.value) },
          h("option", { value: "" }, "全部项目"),
          ...(games || []).map((item) => h("option", { value: item.name, key: item.name }, gameLabel(item.name))),
        ),
      ),
      h("button", { className: "button", type: "button", onClick: refresh }, "↻", " 刷新数据"),
    ),
    h(LiveMatches, {
      data: liveData,
      error: liveError,
      loading: liveLoading,
      onRetry: retry,
    }),
    health && health.status === "degraded"
      ? h(StateCard, { title: "数据库暂不可用", copy: ERROR_COPY.database_unavailable, error: true, action: h("button", { className: "button small", onClick: refresh }, "重新检查") })
      : h(
        "div",
        { className: "dashboard-grid" },
        h(RecentMatches, {
          data: matches,
          error: matchesError,
          game,
          limit,
          loading: matchesLoading,
          onMore: () => setLimit((value) => Math.min(MAX_MATCHES, value + 20)),
          onRetry: retry,
        }),
        h(Leaderboard, { data: leaderboard, error: leaderboardError, loading: leaderboardLoading, onRetry: retry }),
      ),
  );
}

function validateSummary(summary, matchId) {
  return isObject(summary)
    && summary.match_id === matchId
    && typeof summary.game === "string"
    && Array.isArray(summary.players)
    && isObject(summary.scores);
}

function validatePublicEvent(event, expectedSeq) {
  return isObject(event)
    && Number.isInteger(event.seq)
    && event.seq === expectedSeq
    && Object.prototype.hasOwnProperty.call(EVENT_LABELS, event.type)
    && typeof event.timestamp === "string"
    && (event.player === null || typeof event.player === "string")
    && isObject(event.data);
}

function websocketURL(matchId, fromSeq) {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws/v1/matches/${encodeURIComponent(matchId)}?from_seq=${fromSeq}`;
}

function useArchiveReplay(matchId, reloadKey) {
  const [state, setState] = useState({
    error: null,
    eventCount: null,
    events: [],
    phase: "connecting",
    retries: 0,
    source: "websocket",
    summary: null,
    transferComplete: false,
  });

  useEffect(() => {
    let cancelled = false;
    let socket = null;
    let retryTimer = null;
    let retryCount = 0;
    let terminal = false;
    let transferComplete = false;
    let summary = null;
    let eventCount = null;
    let events = [];
    let nextSeq = 0;
    let publishFrame = null;
    let pendingPatch = {};

    const commit = () => {
      publishFrame = null;
      if (cancelled) return;
      const patch = pendingPatch;
      pendingPatch = {};
      setState((previous) => ({
        ...previous,
        ...patch,
        eventCount,
        events: events.slice(),
        retries: retryCount,
        summary,
        transferComplete,
      }));
    };

    const publish = (patch = {}, immediate = false) => {
      if (cancelled) return;
      pendingPatch = { ...pendingPatch, ...patch };
      if (immediate) {
        if (publishFrame !== null) window.cancelAnimationFrame(publishFrame);
        commit();
      } else if (publishFrame === null) {
        publishFrame = window.requestAnimationFrame(commit);
      }
    };

    const fail = (code) => {
      terminal = true;
      publish({ error: new PublicError(code), phase: "error" }, true);
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
    };

    const useRestFallback = async () => {
      if (cancelled || terminal) return;
      try {
        const detail = await fetchJSON(`/api/v1/matches/${encodeURIComponent(matchId)}`);
        if (!validateSummary(detail.match, matchId) || !Array.isArray(detail.events)) {
          throw new PublicError("protocol_error");
        }
        detail.events.forEach((event, index) => {
          if (!validatePublicEvent(event, index)) throw new PublicError("protocol_error");
        });
        summary = detail.match;
        events = detail.events.slice();
        nextSeq = events.length;
        eventCount = events.length;
        transferComplete = true;
        terminal = true;
        publish({ error: null, phase: "ready", source: "rest" }, true);
      } catch (error) {
        if (cancelled || (error && error.name === "AbortError")) return;
        terminal = true;
        publish({ error: error instanceof PublicError ? error : new PublicError("network_error"), phase: "error" }, true);
      }
    };

    const connect = () => {
      if (cancelled || terminal) return;
      if (!("WebSocket" in window)) {
        useRestFallback();
        return;
      }
      publish({ error: null, phase: retryCount ? "retrying" : "connecting", source: "websocket" }, true);
      socket = new WebSocket(websocketURL(matchId, nextSeq));

      socket.onopen = () => publish({ phase: "receiving" }, true);
      socket.onmessage = (message) => {
        if (cancelled || terminal) return;
        let envelope;
        try {
          envelope = JSON.parse(message.data);
        } catch (_error) {
          fail("protocol_error");
          return;
        }
        if (!isObject(envelope) || envelope.api_version !== API_VERSION || typeof envelope.type !== "string") {
          fail("protocol_error");
          return;
        }
        if (envelope.type === "archive") {
          if (!validateSummary(envelope.match, matchId) || !Number.isInteger(envelope.event_count) || envelope.event_count < 0) {
            fail("protocol_error");
            return;
          }
          if (eventCount !== null && eventCount !== envelope.event_count) {
            fail("protocol_error");
            return;
          }
          if (summary && JSON.stringify(summary) !== JSON.stringify(envelope.match)) {
            fail("protocol_error");
            return;
          }
          summary = envelope.match;
          eventCount = envelope.event_count;
          publish({ phase: "receiving" }, true);
          return;
        }
        if (envelope.type === "event") {
          if (envelope.match_id !== matchId || !isObject(envelope.event)) {
            fail("protocol_error");
            return;
          }
          const seq = envelope.event.seq;
          if (Number.isInteger(seq) && seq < nextSeq) {
            if (JSON.stringify(events[seq]) !== JSON.stringify(envelope.event)) fail("protocol_error");
            return;
          }
          if (!validatePublicEvent(envelope.event, nextSeq)) {
            fail("protocol_error");
            return;
          }
          events.push(envelope.event);
          nextSeq += 1;
          publish({ phase: "receiving" });
          return;
        }
        if (envelope.type === "complete") {
          if (
            envelope.match_id !== matchId
            || !Number.isInteger(envelope.event_count)
            || envelope.event_count !== eventCount
            || nextSeq !== eventCount
          ) {
            fail("protocol_error");
            return;
          }
          transferComplete = true;
          publish({ phase: "ready" }, true);
          return;
        }
        fail("protocol_error");
      };

      socket.onclose = (event) => {
        if (cancelled || terminal) return;
        const decision = classifyReplayClose(
          event.code,
          event.reason,
          retryCount,
          transferComplete,
        );
        if (decision.action === "ready") {
          terminal = true;
          publish({ phase: "ready" }, true);
          return;
        }
        if (decision.action === "retry") {
          retryCount += 1;
          publish({ phase: "retrying" }, true);
          retryTimer = window.setTimeout(connect, decision.delay);
          return;
        }
        if (decision.action === "error") {
          terminal = true;
          publish({ error: new PublicError(decision.code), phase: "error" }, true);
          return;
        }
        useRestFallback();
      };
    };

    setState({
      error: null,
      eventCount: null,
      events: [],
      phase: "connecting",
      retries: 0,
      source: "websocket",
      summary: null,
      transferComplete: false,
    });
    connect();
    return () => {
      cancelled = true;
      terminal = true;
      if (publishFrame !== null) window.cancelAnimationFrame(publishFrame);
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "page_changed");
    };
  }, [matchId, reloadKey]);

  return state;
}

function validateLiveSummary(summary, liveId) {
  if (
    !isObject(summary)
    || summary.live_id !== liveId
    || !["play", "series", "round_robin"].includes(summary.mode)
    || !["running", "completed", "interrupted"].includes(summary.status)
    || typeof summary.game !== "string"
    || !/^[a-z][a-z0-9_]{0,63}$/.test(summary.game)
    || !Array.isArray(summary.players)
    || summary.players.length < 2
    || summary.players.some((player) => typeof player !== "string" || !player)
    || !Number.isInteger(summary.event_count)
    || summary.event_count < 0
    || summary.event_count > 10000
  ) return false;
  const placement = ["pairing_number", "pairing_count", "leg_number"];
  if (placement.some((key) => summary[key] !== null
    && summary[key] !== undefined
    && (!Number.isInteger(summary[key]) || summary[key] < 1))) return false;
  if (summary.mode === "play" && placement.some((key) => summary[key] != null)) return false;
  if (summary.mode === "series" && (summary.pairing_number != null || summary.pairing_count != null)) return false;
  if (summary.mode === "round_robin" && !Number.isInteger(summary.pairing_count)) return false;
  if (summary.pairing_number != null
    && (summary.pairing_count == null || summary.pairing_number > summary.pairing_count)) return false;

  const expectedFinalKind = {
    play: "match",
    round_robin: "tournament",
    series: "series",
  }[summary.mode];
  const ids = summary.final_match_ids;
  if (summary.status !== "completed") {
    return summary.final_kind == null
      && summary.final_id == null
      && Array.isArray(ids)
      && ids.length === 0;
  }
  if (
    summary.final_kind !== expectedFinalKind
    || typeof summary.final_id !== "string"
    || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(summary.final_id)
    || !Array.isArray(ids)
    || !ids.length
    || ids.some((id) => typeof id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(id))
    || new Set(ids).size !== ids.length
  ) return false;
  if (summary.mode === "play") return ids.length === 1 && ids[0] === summary.final_id;
  if (summary.mode === "series") return ids.length === 2;
  return ids.length === summary.pairing_count * 2;
}

function validateLiveItem(item, expectedSeq) {
  if (!(isObject(item)
    && item.seq === expectedSeq
    && isObject(item.context)
    && Number.isInteger(item.context.match_event_seq)
    && item.context.match_event_seq >= 0
    && (item.context.leg_number == null
      || (Number.isInteger(item.context.leg_number) && item.context.leg_number >= 1))
    && (item.context.pairing_number == null
      || (Number.isInteger(item.context.pairing_number) && item.context.pairing_number >= 1))
    && (item.context.pairing_number == null || item.context.leg_number != null)
    && validatePublicEvent(item.event, item.context.match_event_seq))) return false;
  const allowedContext = new Set(["leg_number", "match_event_seq", "pairing_number"]);
  return Object.keys(item.context).every((key) => allowedContext.has(key));
}

function publicEventFromLiveItem(item) {
  return {
    ...item.event,
    context: item.context,
    match_event_seq: item.context.match_event_seq,
    seq: item.seq,
  };
}

function liveWebsocketURL(liveId, fromSeq) {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws/v1/live/${encodeURIComponent(liveId)}?from_seq=${fromSeq}`;
}

function useLiveStream(liveId, reloadKey) {
  const [state, setState] = useState({
    error: null,
    events: [],
    finalId: null,
    finalKind: null,
    finalMatchIds: [],
    phase: "connecting",
    retries: 0,
    source: "websocket",
    summary: null,
  });

  useEffect(() => {
    let cancelled = false;
    let socket = null;
    let retryTimer = null;
    let pollTimer = null;
    let retryCount = 0;
    let terminal = false;
    let summary = null;
    let nextSeq = 0;
    let events = [];
    let finalKind = null;
    let finalId = null;
    let finalMatchIds = [];
    let publishFrame = null;
    let pendingPatch = {};

    const commit = () => {
      publishFrame = null;
      if (cancelled) return;
      const patch = pendingPatch;
      pendingPatch = {};
      setState((previous) => ({
        ...previous,
        ...patch,
        events: events.slice(),
        finalId,
        finalKind,
        finalMatchIds: finalMatchIds.slice(),
        retries: retryCount,
        summary,
      }));
    };

    const publish = (patch = {}, immediate = false) => {
      if (cancelled) return;
      pendingPatch = { ...pendingPatch, ...patch };
      if (immediate) {
        if (publishFrame !== null) window.cancelAnimationFrame(publishFrame);
        commit();
      } else if (publishFrame === null) {
        publishFrame = window.requestAnimationFrame(commit);
      }
    };

    const fail = (code) => {
      terminal = true;
      publish({ error: new PublicError(code), phase: "error" }, true);
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
    };

    const acceptSummary = (candidate) => {
      if (!validateLiveSummary(candidate, liveId) || candidate.event_count < nextSeq) {
        throw new PublicError("protocol_error");
      }
      summary = candidate;
      if (candidate.status === "completed") {
        finalKind = candidate.final_kind;
        finalId = candidate.final_id;
        finalMatchIds = Array.isArray(candidate.final_match_ids)
          ? candidate.final_match_ids.slice()
          : [];
      }
    };

    const appendItems = (items) => {
      if (!Array.isArray(items)) throw new PublicError("protocol_error");
      items.forEach((item) => {
        if (Number.isInteger(item && item.seq) && item.seq < nextSeq) {
          const prior = events[item.seq];
          if (JSON.stringify(prior) !== JSON.stringify(publicEventFromLiveItem(item))) {
            throw new PublicError("protocol_error");
          }
          return;
        }
        if (!validateLiveItem(item, nextSeq)) throw new PublicError("protocol_error");
        events.push(publicEventFromLiveItem(item));
        nextSeq += 1;
      });
    };

    const schedulePoll = (delay = 1000) => {
      if (!cancelled && !terminal) pollTimer = window.setTimeout(poll, delay);
    };

    const poll = async () => {
      if (cancelled || terminal) return;
      publish({ error: null, phase: "polling", source: "rest" }, true);
      try {
        const detail = await fetchJSON(
          `/api/v1/live/${encodeURIComponent(liveId)}?from_seq=${nextSeq}&limit=256`,
        );
        acceptSummary(detail.match);
        appendItems(detail.events);
        if (!Number.isInteger(detail.next_seq) || detail.next_seq !== nextSeq) {
          throw new PublicError("protocol_error");
        }
        if (typeof detail.has_more !== "boolean"
          || detail.has_more !== (nextSeq < summary.event_count)) {
          throw new PublicError("protocol_error");
        }
        if (summary.status === "completed" && nextSeq === summary.event_count) {
          terminal = true;
          publish({ phase: "completed", source: "rest" }, true);
          return;
        }
        if (summary.status === "interrupted" && nextSeq === summary.event_count) {
          terminal = true;
          publish({ phase: "interrupted", source: "rest" }, true);
          return;
        }
        publish({ phase: "polling", source: "rest" });
        schedulePoll(detail.has_more ? 0 : 1000);
      } catch (error) {
        if (cancelled || (error && error.name === "AbortError")) return;
        const publicError = error instanceof PublicError ? error : new PublicError("network_error");
        if (["live_not_found", "invalid_request", "protocol_error"].includes(publicError.code)) {
          fail(publicError.code);
          return;
        }
        publish({ error: publicError, phase: "polling", source: "rest" }, true);
        schedulePoll(2000);
      }
    };

    const connect = () => {
      if (cancelled || terminal) return;
      if (!("WebSocket" in window)) {
        poll();
        return;
      }
      publish({ error: null, phase: retryCount ? "retrying" : "connecting", source: "websocket" }, true);
      socket = new WebSocket(liveWebsocketURL(liveId, nextSeq));
      socket.onopen = () => publish({ phase: "live" }, true);
      socket.onmessage = (message) => {
        if (cancelled || terminal) return;
        let envelope;
        try {
          envelope = JSON.parse(message.data);
          if (!isObject(envelope) || envelope.api_version !== API_VERSION) {
            throw new PublicError("protocol_error");
          }
          if (envelope.type === "live_snapshot") {
            acceptSummary(envelope.match);
            if (!Number.isInteger(envelope.next_seq) || envelope.next_seq !== nextSeq) {
              throw new PublicError("protocol_error");
            }
            publish({ phase: "live" }, true);
            return;
          }
          if (envelope.type === "live_event") {
            if (envelope.live_id !== liveId) throw new PublicError("protocol_error");
            appendItems([envelope.item]);
            publish({ phase: "live" });
            return;
          }
          if (envelope.type === "live_complete") {
            if (!summary
              || envelope.live_id !== liveId
              || envelope.event_count !== nextSeq) {
              throw new PublicError("protocol_error");
            }
            const completedSummary = {
              ...summary,
              final_id: envelope.final_id,
              final_kind: envelope.final_kind,
              final_match_ids: envelope.final_match_ids,
              status: "completed",
            };
            if (!validateLiveSummary(completedSummary, liveId)) {
              throw new PublicError("protocol_error");
            }
            finalKind = completedSummary.final_kind;
            finalId = completedSummary.final_id;
            finalMatchIds = completedSummary.final_match_ids.slice();
            terminal = true;
            summary = completedSummary;
            publish({ phase: "completed" }, true);
            return;
          }
          if (envelope.type === "live_interrupted") {
            if (envelope.live_id !== liveId || envelope.event_count !== nextSeq) {
              throw new PublicError("protocol_error");
            }
            terminal = true;
            if (summary) summary = { ...summary, status: "interrupted" };
            publish({ phase: "interrupted" }, true);
            return;
          }
          throw new PublicError("protocol_error");
        } catch (error) {
          fail(error instanceof PublicError ? error.code : "protocol_error");
        }
      };
      socket.onclose = (event) => {
        if (cancelled || terminal) return;
        const decision = classifyReplayClose(event.code, event.reason, retryCount, false);
        if (decision.action === "retry") {
          retryCount += 1;
          publish({ phase: "retrying" }, true);
          retryTimer = window.setTimeout(connect, decision.delay);
        } else if (decision.action === "error") {
          fail(decision.code === "match_not_found" ? "live_not_found" : decision.code);
        } else {
          poll();
        }
      };
    };

    setState({
      error: null,
      events: [],
      finalId: null,
      finalKind: null,
      finalMatchIds: [],
      phase: "connecting",
      retries: 0,
      source: "websocket",
      summary: null,
    });
    connect();
    return () => {
      cancelled = true;
      terminal = true;
      if (publishFrame !== null) window.cancelAnimationFrame(publishFrame);
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      if (pollTimer !== null) window.clearTimeout(pollTimer);
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "page_changed");
    };
  }, [liveId, reloadKey]);

  return state;
}

function scoreRows(summary) {
  if (!summary) return null;
  const known = new Set(summary.players);
  const ordered = [
    ...summary.players.map((player) => [player, summary.scores[player]]),
    ...Object.entries(summary.scores).filter(([player]) => !known.has(player)),
  ];
  return ordered.map(([player, score]) => h(
    "div",
    { className: "score-row", key: player },
    h("strong", null, player),
    h("span", { className: "score-value" }, formatScore(score)),
  ));
}

function DataChips({ values }) {
  const entries = Object.entries(values || {});
  if (!entries.length) return null;
  return h("div", { className: "data-list" }, ...entries.map(([key, value]) => h(
    "span",
    { className: "data-chip", key },
    `${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`,
  )));
}

function EventBody({ event }) {
  const data = event.data;
  if (event.type === "match_started") {
    return h(Fragment, null,
      h("p", { className: "event-text" }, `${gameLabel(data.game)} · seed ${data.seed}\n${(data.players || []).join(" · ")}`),
      h(DataChips, { values: data.game_config }),
    );
  }
  if (event.type === "turn_prompt") return h("pre", { className: "event-text" }, data.prompt || "（空题面）");
  if (event.type === "move_received") return h("pre", { className: "event-text" }, data.move || "（空提交）");
  if (event.type === "move_rejected") {
    return h(Fragment, null,
      h("pre", { className: "event-text" }, data.reason || data.reason_code || "提交不符合规则"),
      h("div", { className: "data-list" },
        data.move ? h("span", { className: "data-chip" }, `提交：${data.move}`) : null,
        data.reason_code ? h("span", { className: "data-chip" }, data.reason_code) : null,
        data.forfeit ? h("span", { className: "data-chip danger-chip" }, "判负") : null,
        data.technical_loss ? h("span", { className: "data-chip danger-chip" }, "技术失败") : null,
      ),
    );
  }
  const judging = data.judging;
  return h(Fragment, null,
    h("div", { className: "data-list" }, ...Object.entries(data.scores || {}).map(([player, score]) => h("span", { className: "data-chip", key: player }, `${player} · ${formatScore(score)}`))),
    h("div", { className: "data-list" },
      h("span", { className: "data-chip" }, `结束：${data.termination || "completed"}`),
      data.reason_code ? h("span", { className: "data-chip" }, data.reason_code) : null,
      data.forfeited_by ? h("span", { className: "data-chip danger-chip" }, `${data.forfeited_by} 判负`) : null,
      judging ? h("span", { className: "data-chip" }, `匿名评审 ${judging.successful_judges}/${judging.panel_size} · quorum ${judging.quorum}`) : null,
    ),
  );
}

function Timeline({ events, cursor }) {
  const visible = events.slice(0, cursor);
  const hidden = Math.max(0, visible.length - 180);
  const rendered = hidden ? visible.slice(hidden) : visible;
  if (!visible.length) {
    return h(StateCard, { title: "回放尚未开始", copy: "连接建立后，点击播放或下一步查看事件。" });
  }
  return h(
    "div",
    { className: "timeline", "aria-label": "对局事件时间线" },
    hidden ? h("p", { className: "timeline-note" }, `为保持页面流畅，已折叠更早的 ${hidden} 条事件。`) : null,
    ...rendered.map((event) => h(
      "article",
      { className: "event-card", key: event.seq },
      h("div", { className: "event-seq", "aria-label": `事件 ${event.seq + 1}` }, String(event.seq + 1).padStart(2, "0")),
      h(
        "div",
        { className: "event-content" },
        h("div", { className: "event-head" },
          h("strong", null,
            EVENT_LABELS[event.type],
            event.context && (event.context.pairing_number || event.context.leg_number)
              ? h("span", { className: "event-context" },
                event.context.pairing_number ? ` · 第 ${event.context.pairing_number} 组` : "",
                event.context.leg_number ? ` · 第 ${event.context.leg_number} 局` : "",
              )
              : null,
          ),
          h("time", { dateTime: event.timestamp }, dateTime(event.timestamp)),
        ),
        event.player ? h("p", { className: "event-player" }, event.player) : null,
        h(EventBody, { event }),
      ),
    )),
  );
}

function ReplayViewer({ replay }) {
  const reducedMotion = useMemo(() => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches, []);
  const [playback, setPlayback] = useState({ cursor: 0, playing: !reducedMotion });
  const [speed, setSpeed] = useState(1);
  const { cursor, playing } = playback;

  const updatePlayback = (action) => setPlayback((previous) => playbackReducer(previous, {
    ...action,
    available: replay.events.length,
    error: replay.phase === "error",
    transferComplete: replay.transferComplete,
  }));

  useEffect(() => {
    setPlayback(playbackReducer(
      { cursor: 0, playing: false },
      {
        autoPlay: !reducedMotion,
        available: replay.events.length,
        error: replay.phase === "error",
        transferComplete: replay.transferComplete,
        type: "reset",
      },
    ));
  }, [replay.summary && replay.summary.match_id, reducedMotion]);

  useEffect(() => {
    if (!playing) return undefined;
    if (cursor >= replay.events.length) {
      if (replay.transferComplete) updatePlayback({ type: "pause" });
      return undefined;
    }
    const timer = window.setTimeout(() => {
      updatePlayback({ type: "tick" });
    }, 700 / speed);
    return () => window.clearTimeout(timer);
  }, [cursor, playing, replay.events.length, replay.transferComplete, speed]);

  useEffect(() => {
    if (replay.phase === "error") updatePlayback({ type: "pause" });
  }, [replay.phase]);

  const total = replay.eventCount === null ? replay.events.length : replay.eventCount;
  const finished = replay.transferComplete && cursor >= total;
  const status = replay.phase === "error"
    ? "回放失败"
    : replay.phase === "retrying"
      ? `连接中断，正在续播（${replay.retries}/3）`
      : replay.phase === "connecting"
        ? "正在连接回放服务"
        : finished
          ? "回放完成"
          : cursor >= replay.events.length && !replay.transferComplete
            ? "正在缓冲事件"
            : playing
              ? "正在播放"
              : "已暂停";

  const togglePlaying = () => {
    updatePlayback({ type: "toggle" });
  };

  const restart = () => {
    updatePlayback({ type: "restart" });
  };

  const onKeyDown = (event) => {
    if (["BUTTON", "INPUT", "SELECT", "TEXTAREA", "A"].includes(event.target.tagName)) return;
    if (event.key === " ") {
      event.preventDefault();
      togglePlaying();
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      updatePlayback({ type: "step-forward" });
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      updatePlayback({ type: "step-back" });
    } else if (event.key === "Home") {
      event.preventDefault();
      updatePlayback({ cursor: 0, type: "seek" });
    } else if (event.key === "End") {
      event.preventDefault();
      updatePlayback({ cursor: replay.events.length, type: "seek" });
    }
  };

  return h(
    "section",
    { className: "panel replay-panel", "aria-labelledby": "replay-heading", onKeyDown, tabIndex: 0 },
    h("div", { className: "panel-head" },
      h("h2", { id: "replay-heading" }, "事件回放"),
      h("span", {
        "aria-atomic": "true",
        "aria-live": "polite",
        className: `replay-status${replay.phase === "error" ? " error" : finished ? " done" : ""}`,
      }, status),
    ),
    h("div", { className: "controls", "aria-label": "回放控制" },
      h("div", { className: "control-row" },
        h("button", { className: "button primary small", type: "button", disabled: !replay.events.length || replay.phase === "error", onClick: togglePlaying }, playing && !finished ? "暂停" : "播放"),
        h("button", { className: "button small", type: "button", disabled: cursor <= 0, onClick: () => updatePlayback({ type: "step-back" }) }, "上一步"),
        h("button", { className: "button small", type: "button", disabled: cursor >= replay.events.length, onClick: () => updatePlayback({ type: "step-forward" }) }, "下一步"),
        h("button", { className: "button small", type: "button", disabled: !cursor || replay.phase === "error", onClick: restart }, "重新播放"),
        h("span", { className: "speed-group", "aria-label": "回放速度" }, ...[0.5, 1, 2, 4].map((value) => h("button", {
          className: `speed-button${speed === value ? " active" : ""}`,
          key: value,
          onClick: () => setSpeed(value),
          type: "button",
          "aria-pressed": speed === value,
        }, `${value}×`))),
      ),
      h("div", { className: "progress-row" },
        h("input", {
          "aria-label": "回放进度",
          disabled: !replay.events.length,
          max: Math.max(1, replay.events.length),
          min: 0,
          onChange: (event) => updatePlayback({ cursor: Number(event.target.value), type: "seek" }),
          type: "range",
          value: cursor,
        }),
        h("span", { className: "progress-count" }, `${cursor} / ${total || "—"}`),
      ),
    ),
    h(Timeline, { cursor, events: replay.events }),
  );
}

function LiveViewer({ stream }) {
  const [cursor, setCursor] = useState(0);
  const [following, setFollowing] = useState(true);

  useEffect(() => {
    if (following) setCursor(stream.events.length);
  }, [following, stream.events.length]);

  const terminal = stream.phase === "completed" || stream.phase === "interrupted";
  const status = stream.phase === "completed"
    ? "比赛已完成并存档"
    : stream.phase === "interrupted"
      ? "比赛进程或直播发布已中断"
      : stream.phase === "retrying"
        ? `连接中断，正在续播（${stream.retries}/3）`
        : stream.phase === "polling"
          ? "WebSocket 不可用，正在只读轮询"
          : stream.phase === "connecting"
            ? "正在连接实时事件流"
            : following
              ? "正在跟随直播"
              : `已暂停显示，仍在接收（积压 ${stream.events.length - cursor} 条）`;

  const pauseOrFollow = () => {
    if (following) {
      setFollowing(false);
    } else {
      setCursor(stream.events.length);
      setFollowing(true);
    }
  };

  return h(
    "section",
    { className: "panel replay-panel", "aria-labelledby": "live-timeline-heading", tabIndex: 0 },
    h("div", { className: "panel-head" },
      h("h2", { id: "live-timeline-heading" }, "实时事件"),
      h("span", {
        "aria-atomic": "true",
        "aria-live": "polite",
        className: `replay-status${stream.phase === "interrupted" ? " error" : terminal ? " done" : " live"}`,
      }, status),
    ),
    h("div", { className: "controls", "aria-label": "直播显示控制" },
      h("div", { className: "control-row" },
        h("button", {
          className: "button primary small",
          disabled: !stream.events.length || terminal,
          onClick: pauseOrFollow,
          type: "button",
        }, following ? "暂停显示" : "跟随直播"),
        h("button", {
          className: "button small",
          disabled: cursor <= 0,
          onClick: () => { setFollowing(false); setCursor((value) => Math.max(0, value - 1)); },
          type: "button",
        }, "上一条"),
        h("button", {
          className: "button small",
          disabled: cursor >= stream.events.length,
          onClick: () => {
            const next = Math.min(stream.events.length, cursor + 1);
            setCursor(next);
            setFollowing(next === stream.events.length);
          },
          type: "button",
        }, "下一条"),
        h("button", {
          className: "button small",
          disabled: cursor >= stream.events.length,
          onClick: () => { setCursor(stream.events.length); setFollowing(true); },
          type: "button",
        }, "回到最新"),
        h("span", { className: "progress-count" }, `${cursor} / ${stream.events.length}`),
      ),
    ),
    h(Timeline, { cursor, events: stream.events }),
  );
}

function MatchFacts({ summary, replay }) {
  const facts = [
    ["项目", gameLabel(summary.game)],
    ["完成时间", dateTime(summary.finished_at, true)],
    ["随机种子", String(summary.seed)],
    ["评级", summary.rated ? "已计入 ELO" : "未评级"],
    ["事件", replay.eventCount === null ? "正在读取" : `${replay.eventCount} 条`],
    ["传输", replay.source === "rest" ? "REST 安全回退" : "WebSocket 同源回放"],
  ];
  if (summary.series_id) facts.push(["双局赛", `第 ${summary.leg_number || "?"} 局`]);
  if (summary.tournament_id) facts.push(["循环赛", `对阵 ${summary.pairing_number || "?"}/${summary.pairing_count || "?"}`]);
  return h(
    "aside",
    { "aria-label": "对局信息" },
    h("dl", { className: "facts" }, ...facts.map(([label, value]) => h("div", { className: "fact", key: label }, h("dt", null, label), h("dd", null, value)))),
    h("p", { className: "notice" }, "只读回放来自已完成存档。页面不会调用模型、修改数据库或显示 Provider 路由与凭据信息。"),
  );
}

function MatchDetailPage({ matchId }) {
  const [reloadKey, setReloadKey] = useState(0);
  const replay = useArchiveReplay(matchId, reloadKey);
  const summary = replay.summary;

  if (replay.phase === "error" && !summary) {
    return h(
      "main",
      { className: "page", id: "main-content", tabIndex: -1 },
      h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回观战大厅"),
      h(StateCard, {
        title: "无法打开这场回放",
        copy: errorCopy(replay.error),
        error: true,
        action: h("button", { className: "button", onClick: () => setReloadKey((value) => value + 1) }, "重新获取"),
      }),
    );
  }

  if (!summary) {
    return h(
      "main",
      { className: "page", id: "main-content", tabIndex: -1 },
      h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回观战大厅"),
      h("section", { className: "panel" }, h("div", { className: "panel-body" }, h(LoadingRows, { count: 4 }))),
    );
  }

  return h(
    "main",
    { className: "page", id: "main-content", tabIndex: -1 },
    h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回观战大厅"),
    h(
      "section",
      { className: "detail-hero", "aria-labelledby": "detail-title" },
      h("div", null,
        h("p", { className: "eyebrow" }, `${gameLabel(summary.game)} · ARCHIVE REPLAY`),
        h("h1", { className: "detail-title", id: "detail-title" }, summary.players.join(" 对 ")),
        h("p", { className: "id-line" }, summary.match_id),
      ),
      h("div", { className: "scoreboard", "aria-label": "最终比分" }, scoreRows(summary)),
    ),
    replay.error ? h(StateCard, {
      title: "回放连接异常",
      copy: errorCopy(replay.error),
      error: true,
      action: h("button", { className: "button", onClick: () => setReloadKey((value) => value + 1) }, "重新获取"),
    }) : null,
    h("div", { className: "replay-layout" },
      h(ReplayViewer, { replay }),
      h(MatchFacts, { replay, summary }),
    ),
  );
}

function LiveFacts({ stream, summary }) {
  const facts = [
    ["项目", gameLabel(summary.game)],
    ["运行模式", modeLabel(summary.mode)],
    ["开始时间", dateTime(summary.started_at, true)],
    ["最新事件", `${summary.event_count} 条`],
    ["传输", stream.source === "rest" ? "REST 只读轮询" : "WebSocket 同源直播"],
    ["状态", liveStatusLabel(summary.status)],
  ];
  if (summary.pairing_number) facts.push(["当前对阵", `${summary.pairing_number}/${summary.pairing_count || "?"}`]);
  if (summary.leg_number) facts.push(["当前局", String(summary.leg_number)]);
  return h(
    "aside",
    { "aria-label": "运行中比赛信息" },
    h("dl", { className: "facts" }, ...facts.map(([label, value]) => h(
      "div",
      { className: "fact", key: label },
      h("dt", null, label),
      h("dd", null, value),
    ))),
    h("p", { className: "notice" }, "只读事件来自本机比赛进程的公开 sidecar。观战页不会调用模型、提交动作、更新 ELO 或读取 Provider 路由与凭据。"),
  );
}

function LiveDetailPage({ liveId }) {
  const [reloadKey, setReloadKey] = useState(0);
  const stream = useLiveStream(liveId, reloadKey);
  const summary = stream.summary;

  if (stream.phase === "error" && !summary) {
    return h(
      "main",
      { className: "page", id: "main-content", tabIndex: -1 },
      h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回观战大厅"),
      h(StateCard, {
        title: "无法打开实时观战",
        copy: errorCopy(stream.error),
        error: true,
        action: h("button", { className: "button", onClick: () => setReloadKey((value) => value + 1) }, "重新连接"),
      }),
    );
  }

  if (!summary) {
    return h(
      "main",
      { className: "page", id: "main-content", tabIndex: -1 },
      h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回观战大厅"),
      h("section", { className: "panel" }, h("div", { className: "panel-body" }, h(LoadingRows, { count: 4 }))),
    );
  }

  const archiveLinks = stream.phase === "completed" && stream.finalMatchIds.length
    ? h(
      "div",
      { className: "archive-actions" },
      h("strong", null, "完整档案已安全提交"),
      ...stream.finalMatchIds.map((matchId, index) => h(
        AppLink,
        { className: "button primary small", href: `/matches/${encodeURIComponent(matchId)}`, key: matchId },
        stream.finalMatchIds.length === 1 ? "打开存档回放" : `打开第 ${index + 1} 场存档`,
      )),
    )
    : null;

  return h(
    "main",
    { className: "page", id: "main-content", tabIndex: -1 },
    h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回观战大厅"),
    h("section", { className: "detail-hero live-detail-hero", "aria-labelledby": "live-detail-title" },
      h("div", null,
        h("p", { className: "eyebrow" }, `${gameLabel(summary.game)} · LIVE EVENTS`),
        h("h1", { className: "detail-title", id: "live-detail-title" }, summary.players.join(" 对 ")),
        h("p", { className: "id-line" }, summary.live_id),
      ),
      h("div", { className: `live-stage ${summary.status}` },
        h("span", { className: "live-pulse", "aria-hidden": "true" }),
        h("strong", null, liveStatusLabel(summary.status)),
        h("span", null, `${summary.event_count} 条公开事件`),
      ),
    ),
    stream.error ? h(StateCard, {
      title: "实时连接异常",
      copy: errorCopy(stream.error),
      error: true,
      action: h("button", { className: "button", onClick: () => setReloadKey((value) => value + 1) }, "重新连接"),
    }) : null,
    stream.phase === "interrupted" ? h(StateCard, {
      title: "这次直播已中断",
      copy: "比赛进程可能退出，或实时发布因本机资源限制而安全降级；已收到的公开事件仍可查看。",
      error: true,
    }) : null,
    archiveLinks,
    h("div", { className: "replay-layout" },
      h(LiveViewer, { stream }),
      h(LiveFacts, { stream, summary }),
    ),
  );
}

function participationEndpoint(sessionId, seatId) {
  return `/api/v1/participation/${encodeURIComponent(sessionId)}/${encodeURIComponent(seatId)}`;
}

function useParticipation(sessionId, seatId, capability, reloadKey) {
  const [state, setState] = useState({
    error: null,
    phase: capability ? "loading" : "missing-capability",
    snapshot: null,
  });

  useEffect(() => {
    if (!capability) {
      setState({ error: null, phase: "missing-capability", snapshot: null });
      return undefined;
    }
    let cancelled = false;
    let controller = null;
    let loading = false;
    let timer = null;

    const schedule = (delay) => {
      if (cancelled || timer !== null) return;
      timer = window.setTimeout(() => {
        timer = null;
        load(false);
      }, delay);
    };
    const load = async (initial) => {
      if (cancelled || loading) return;
      loading = true;
      controller = new AbortController();
      if (initial) {
        setState((previous) => ({ ...previous, error: null, phase: "loading" }));
      }
      try {
        const payload = await fetchParticipationJSON(
          participationEndpoint(sessionId, seatId),
          capability,
          { signal: controller.signal },
        );
        if (!validateParticipationSnapshot(payload, sessionId, seatId)) {
          throw new PublicError("protocol_error");
        }
        if (cancelled) return;
        setState({ error: null, phase: "ready", snapshot: payload });
        if (participationKeepsCapability(payload.status)) {
          schedule(PARTICIPATION_POLL_INTERVAL);
        } else {
          clearParticipationCapability(sessionId, seatId);
        }
      } catch (error) {
        if (cancelled || (error && error.name === "AbortError")) return;
        const publicError = error instanceof PublicError
          ? error
          : new PublicError("network_error");
        if (participationErrorClearsCapability(publicError.status)) {
          clearParticipationCapability(sessionId, seatId);
        }
        setState((previous) => ({ ...previous, error: publicError, phase: "error" }));
        if (![401, 403, 404, 410].includes(publicError.status)) schedule(2000);
      } finally {
        loading = false;
      }
    };
    const onVisibilityChange = () => {
      if (document.visibilityState !== "visible" || cancelled || loading) return;
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
      load(false);
    };

    load(true);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      if (timer !== null) window.clearTimeout(timer);
      if (controller) controller.abort();
    };
  }, [capability, reloadKey, seatId, sessionId]);

  return state;
}

function remainingCopy(expiresAt, now) {
  const milliseconds = Date.parse(expiresAt) - now;
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return "提交时间已结束";
  const seconds = Math.ceil(milliseconds / 1000);
  if (seconds < 60) return `剩余 ${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `剩余 ${minutes} 分 ${seconds % 60} 秒`;
}

function participationStatusCopy(snapshot, submission) {
  if (snapshot.status === "completed") return "比赛已完成，提交入口已经关闭。";
  if (snapshot.status === "interrupted") return "比赛已中断，提交入口已经关闭。";
  if (snapshot.status === "expired") return "参与席位已过期，提交入口已经关闭。";
  if (!snapshot.request) return "已连接，正在等待轮到你。";
  if (snapshot.request.state === "submitted" || snapshot.request.state === "consumed") {
    return "本轮提交已送达，正在等待游戏规则校验。";
  }
  if (snapshot.request.state === "accepted") return "本轮提交已通过游戏规则校验。";
  if (snapshot.request.state === "rejected") return "本轮提交未通过规则校验，正在等待新的输入请求。";
  if (snapshot.request.state === "expired") return "本轮输入已经超时，正在等待比赛继续。";
  if (snapshot.request.state === "cancelled") return "本轮输入已取消，正在等待比赛继续。";
  if (submission.requestId === snapshot.request.request_id) {
    if (submission.phase === "submitting") return "正在把本轮提交送到比赛进程。";
    if (submission.phase === "submitted") return "提交已送达，正在等待游戏规则校验。";
    if (submission.phase === "duplicate") return "已确认同一份提交先前送达，正在等待比赛继续。";
    if (submission.phase === "error") return "提交未获确认；可以用同一内容安全重试。";
  }
  return `轮到你了，这是输入请求 ${snapshot.request.request_seq}。`;
}

function ParticipationPage({ seatId, sessionId }) {
  const [capability] = useState(() => readParticipationCapability(sessionId, seatId));
  const [reloadKey, setReloadKey] = useState(0);
  const participation = useParticipation(sessionId, seatId, capability, reloadKey);
  const [move, setMove] = useState("");
  const [now, setNow] = useState(Date.now);
  const [submission, setSubmission] = useState({
    error: null,
    phase: "idle",
    requestId: null,
    submissionId: null,
  });
  const inputRef = useRef(null);
  const snapshot = participation.snapshot;
  const request = snapshot && snapshot.status === "active" ? snapshot.request : null;
  const requestId = request ? request.request_id : null;

  useEffect(() => {
    setMove("");
    setSubmission({ error: null, phase: "idle", requestId: null, submissionId: null });
    if (!requestId) return undefined;
    const frame = window.requestAnimationFrame(() => {
      if (inputRef.current) inputRef.current.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [requestId]);

  useEffect(() => {
    if (!request) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [requestId]);

  if (!capability) {
    return h(
      "main",
      { className: "page participation-page", id: "main-content", tabIndex: -1 },
      h(StateCard, {
        title: "缺少参与凭证",
        copy: "请使用 play 命令输出的完整本机参与链接。为了保护席位，凭证不会显示在页面或地址栏中。",
        error: true,
      }),
    );
  }

  if (participation.phase === "loading" && !snapshot) {
    return h(
      "main",
      { className: "page participation-page", id: "main-content", tabIndex: -1 },
      h("section", { className: "panel" },
        h("div", { className: "panel-body" }, h(LoadingRows, { count: 3 })),
      ),
    );
  }

  if (!snapshot) {
    return h(
      "main",
      { className: "page participation-page", id: "main-content", tabIndex: -1 },
      h(StateCard, {
        title: "无法打开参与席位",
        copy: errorCopy(participation.error),
        error: true,
        action: participation.error && [401, 403].includes(participation.error.status)
          ? null
          : h("button", {
            className: "button",
            onClick: () => setReloadKey((value) => value + 1),
            type: "button",
          }, "重新连接"),
      }),
    );
  }

  const characterCount = countCharacters(move);
  const overLimit = characterCount > MAX_MOVE_CHARACTERS;
  const locallyExpired = request ? Date.parse(request.expires_at) <= now : false;
  const requestClosed = request && request.state !== "pending";
  const locallySubmitted = request
    && submission.requestId === request.request_id
    && ["submitted", "duplicate"].includes(submission.phase);
  const canSubmit = Boolean(request)
    && request.state === "pending"
    && !locallyExpired
    && !overLimit
    && submission.phase !== "submitting"
    && !locallySubmitted;
  const closedButtonCopy = request && {
    accepted: "本轮已接受",
    cancelled: "本轮已取消",
    consumed: "正在校验",
    expired: "本轮已超时",
    rejected: "本轮未接受",
    submitted: "本轮已提交",
  }[request.state];
  const statusCopy = participationStatusCopy(snapshot, submission);

  const submit = async () => {
    if (!canSubmit || !request) return;
    let submissionId = submission.requestId === request.request_id
      && submission.submissionId
      ? submission.submissionId
      : null;
    if (!submissionId) {
      try {
        submissionId = createSubmissionId();
      } catch (error) {
        setSubmission({
          error: error instanceof PublicError ? error : new PublicError("request_failed"),
          phase: "error",
          requestId: request.request_id,
          submissionId: null,
        });
        return;
      }
    }
    setSubmission({
      error: null,
      phase: "submitting",
      requestId: request.request_id,
      submissionId,
    });
    try {
      const payload = await fetchParticipationJSON(
        `${participationEndpoint(sessionId, seatId)}/requests/${encodeURIComponent(request.request_id)}/submissions`,
        capability,
        {
          body: { move, submission_id: submissionId },
          method: "POST",
        },
      );
      if (!validateParticipationSubmission(payload, request.request_id)) {
        throw new PublicError("protocol_error");
      }
      setSubmission({
        error: null,
        phase: payload.status,
        requestId: request.request_id,
        submissionId,
      });
      setMove("");
      setReloadKey((value) => value + 1);
    } catch (error) {
      const publicError = error instanceof PublicError ? error : new PublicError("network_error");
      setSubmission({
        error: publicError,
        phase: "error",
        requestId: request.request_id,
        submissionId,
      });
      if ([404, 409, 410].includes(publicError.status)) {
        setReloadKey((value) => value + 1);
      }
    }
  };
  const onSubmit = (event) => {
    event.preventDefault();
    submit();
  };
  const onMoveKeyDown = (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      submit();
    }
  };

  const terminalTitle = snapshot.status === "completed"
    ? "比赛已完成"
    : snapshot.status === "interrupted"
      ? "比赛已中断"
      : snapshot.status === "expired"
        ? "参与席位已过期"
        : null;
  const terminalCopy = snapshot.status === "completed"
    ? "你的浏览器输入阶段已经结束。"
    : snapshot.status === "interrupted"
      ? "比赛进程未能继续；已确认的提交不会被重新发送。"
      : "这个本机参与链接已经超过有效期。";

  return h(
    "main",
    { className: "page participation-page", id: "main-content", tabIndex: -1 },
    h("section", { className: "participation-hero", "aria-labelledby": "participation-title" },
      h("div", null,
        h("p", { className: "eyebrow" }, `${gameLabel(snapshot.game)} · HUMAN INPUT`),
        h("h1", { className: "detail-title", id: "participation-title" }, snapshot.player_name),
        h("p", { className: "hero-copy" }, `${snapshot.players.join(" 对 ")}。题面和提交都只在本机比赛进程与这个席位之间传输。`),
      ),
      h("div", { className: `participation-connection ${snapshot.status}` },
        h("span", { className: "status-dot", "aria-hidden": "true" }),
        h("strong", null, snapshot.status === "active" ? "本机席位已连接" : terminalTitle),
        h("span", null, snapshot.status === "active" ? "每秒同步最新题面" : "输入已关闭"),
      ),
    ),
    participation.error ? h("div", { className: "participation-alert", role: "alert" },
      h("strong", null, "连接暂时中断"),
      h("span", null, errorCopy(participation.error)),
      h("button", {
        className: "button small",
        onClick: () => setReloadKey((value) => value + 1),
        type: "button",
      }, "立即重试"),
    ) : null,
    terminalTitle ? h("section", {
      "aria-labelledby": "participation-terminal-heading",
      className: "panel participation-terminal",
    },
    h("div", { className: "state-card" },
      h("h2", { id: "participation-terminal-heading" }, terminalTitle),
      h("p", null, terminalCopy),
      snapshot.final_match_id ? h("div", { className: "state-action" },
        h(AppLink, {
          className: "button primary",
          href: `/matches/${encodeURIComponent(snapshot.final_match_id)}`,
        }, "打开存档回放"),
      ) : null,
    ),
    ) : h("div", { className: "participation-layout" },
      h("section", { className: "panel participation-panel", "aria-labelledby": "prompt-heading" },
        h("div", { className: "panel-head" },
          h("h2", { id: "prompt-heading" }, request ? "当前题面" : "等待题面"),
          request ? h("span", { className: "panel-kicker" }, `输入请求 ${request.request_seq}`) : null,
        ),
        h("div", { className: "participation-prompt-wrap" },
          request
            ? h("pre", { className: "participation-prompt" }, request.prompt || "（空题面）")
            : h(StateCard, {
              title: "还没轮到你",
              copy: "页面会自动同步；出现新题面后，输入框会获得焦点。",
            }),
        ),
      ),
      h("aside", { className: "participation-sidebar", "aria-label": "提交控制" },
        h("section", { className: "panel" },
          h("div", { className: "panel-head" },
            h("h2", null, "你的提交"),
            request && request.state === "pending" ? h("time", {
              className: `participation-deadline${locallyExpired ? " expired" : ""}`,
              dateTime: request.expires_at,
            }, remainingCopy(request.expires_at, now)) : request
              ? h("span", { className: "participation-deadline" }, {
                accepted: "规则已接受",
                cancelled: "本轮已取消",
                consumed: "正在校验",
                expired: "本轮已超时",
                rejected: "规则未接受",
                submitted: "已送达",
              }[request.state])
              : null,
          ),
          h("form", { className: "participation-form", onSubmit },
            h("label", { htmlFor: "participation-move" }, "输入动作或答案"),
            h("p", { className: "field-help", id: "participation-move-help" },
              "支持多行纯文本。按 Command + Enter（Windows/Linux 为 Ctrl + Enter）提交。",
            ),
            h("textarea", {
              "aria-describedby": "participation-move-help participation-character-count",
              "aria-invalid": overLimit ? "true" : undefined,
              disabled: !request || requestClosed || locallySubmitted,
              id: "participation-move",
              onChange: (event) => setMove(event.target.value),
              onKeyDown: onMoveKeyDown,
              placeholder: request ? "输入本轮动作…" : "等待题面…",
              ref: inputRef,
              rows: 8,
              value: move,
            }),
            h("div", { className: "field-meta" },
              h("span", {
                className: overLimit ? "character-count over" : "character-count",
                id: "participation-character-count",
              }, `${characterCount} / ${MAX_MOVE_CHARACTERS} 字符`),
              h("span", null, "提交后不能在网页中撤回"),
            ),
            h("button", {
              className: "button primary participation-submit",
              disabled: !canSubmit,
              type: "submit",
            }, submission.phase === "submitting"
              ? "正在提交…"
              : requestClosed || locallySubmitted
                ? closedButtonCopy || "本轮已提交"
                : locallyExpired
                  ? "本轮已截止"
                  : "提交本轮输入"),
            submission.error ? h("p", { className: "submission-error", role: "alert" },
              errorCopy(submission.error),
            ) : null,
          ),
        ),
        h("section", {
          "aria-atomic": "true",
          "aria-live": "polite",
          className: `participation-status ${submission.phase}`,
        },
        h("strong", null, request ? "席位状态" : "等待中"),
        h("p", null, statusCopy),
        ),
        h("p", { className: "notice" },
          "页面不会显示参与凭证，也不会读取 Provider 路由、模型配置或其他选手的提交。",
        ),
      ),
    ),
  );
}

function parseRoute() {
  if (window.location.pathname === "/") {
    const candidate = new URLSearchParams(window.location.search).get("game");
    const game = candidate && /^[a-z][a-z0-9_]{0,63}$/.test(candidate) ? candidate : "";
    return { name: "lobby", game };
  }
  const participation = participationPath(window.location.pathname);
  if (participation) return { name: "participation", ...participation };
  const match = window.location.pathname.match(/^\/matches\/([^/]+)$/);
  const live = window.location.pathname.match(/^\/live\/([^/]+)$/);
  if (!match && !live) return { name: "not-found" };
  try {
    const id = decodeURIComponent((match || live)[1]);
    return /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(id)
      ? match
        ? { name: "match", matchId: id }
        : { name: "live", liveId: id }
      : { name: "not-found" };
  } catch (_error) {
    return { name: "not-found" };
  }
}

captureParticipationCapability();

function App() {
  const [route, setRoute] = useState(parseRoute);
  const [health, setHealth] = useState(null);
  const [games, setGames] = useState([]);
  const [metaRefresh, setMetaRefresh] = useState(0);
  const firstRoute = useRef(true);

  useEffect(() => {
    const onPopState = () => {
      captureParticipationCapability();
      setRoute(parseRoute());
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    document.title = route.name === "lobby"
      ? "LLM Olympics · 观战台"
      : route.name === "match"
        ? "对局回放 · LLM Olympics"
        : route.name === "live"
          ? "实时观战 · LLM Olympics"
          : route.name === "participation"
            ? "比赛参与 · LLM Olympics"
          : "页面不存在 · LLM Olympics";
    if (firstRoute.current) {
      firstRoute.current = false;
      return undefined;
    }
    const frame = window.requestAnimationFrame(() => {
      const main = document.getElementById("main-content");
      if (main) main.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [route.game, route.liveId, route.matchId, route.name, route.seatId, route.sessionId]);

  useEffect(() => {
    if (route.name === "participation") {
      setGames([]);
      setHealth(null);
      return undefined;
    }
    const controller = new AbortController();
    fetchJSON("/api/v1/health", controller.signal)
      .then(setHealth)
      .catch(() => setHealth({ status: "degraded", database_available: false }));
    fetchJSON("/api/v1/games", controller.signal)
      .then((payload) => setGames(Array.isArray(payload.games) ? payload.games : []))
      .catch(() => setGames([]));
    return () => controller.abort();
  }, [metaRefresh, route.name]);

  let content;
  if (route.name === "lobby") {
    content = h(Lobby, {
      games,
      health,
      initialGame: route.game,
      refreshAll: () => setMetaRefresh((value) => value + 1),
    });
  } else if (route.name === "match") {
    content = h(MatchDetailPage, { matchId: route.matchId });
  } else if (route.name === "live") {
    content = h(LiveDetailPage, { liveId: route.liveId });
  } else if (route.name === "participation") {
    content = h(ParticipationPage, {
      key: participationComponentKey(route.sessionId, route.seatId),
      seatId: route.seatId,
      sessionId: route.sessionId,
    });
  } else {
    content = h("main", { className: "page", id: "main-content", tabIndex: -1 },
      h(StateCard, {
        title: "没有这个页面",
        copy: "请返回首页，或使用命令行输出的完整本机链接。",
        action: h(AppLink, { className: "button primary", href: "/" }, "返回首页"),
      }),
    );
  }

  const routeAnnouncement = route.name === "lobby"
    ? `已打开观战大厅${route.game ? `，筛选${gameLabel(route.game)}` : ""}`
    : route.name === "match"
      ? "已打开对局回放"
      : route.name === "live"
        ? "已打开实时观战"
        : route.name === "participation"
          ? "已打开比赛参与席位"
        : "页面不存在";
  const healthAnnouncement = route.name === "participation"
    ? ""
    : health
    ? (health.status === "ok" ? "数据库可用" : "数据库不可用")
    : "正在检查数据库";

  return h(
    "div",
    { className: "site-shell" },
    h(Header, { health, participation: route.name === "participation" }),
    content,
    h("div", { className: "live-region", "aria-atomic": "true", "aria-live": "polite" }, `${routeAnnouncement}${healthAnnouncement ? `。${healthAnnouncement}` : ""}`),
    h("footer", { className: "footer" }, route.name === "participation"
      ? "LLM Olympics · 本机人类输入 · 凭证仅保存在当前浏览器标签页"
      : "LLM Olympics · 本机只读观战 · 实时事件与已完成存档"),
  );
}

if (globalThis.__LLMOLYMPIC_ENABLE_TEST_HOOKS__) {
  globalThis.__LLMOLYMPIC_OBSERVER_TEST__ = Object.freeze({
    classifyReplayClose,
    countCharacters,
    participationComponentKey,
    participationErrorClearsCapability,
    participationKeepsCapability,
    playbackReducer,
    remainingCopy,
    validateParticipationRequest,
    validateParticipationSnapshot,
    validateParticipationSubmission,
    validateLiveItem,
    validateLiveSummary,
  });
}

if (!globalThis.__LLMOLYMPIC_SKIP_BOOTSTRAP__) {
  createRoot(document.getElementById("root")).render(h(App));
}
