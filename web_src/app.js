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
const MAX_PARTICIPATION_PLAYERS = 16;
const MAX_CONTROL_PLAYERS = 16;
const PARTICIPATION_POLL_INTERVAL = 1000;
const SAFE_PUBLIC_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const CAPABILITY_TOKEN = /^[A-Za-z0-9_-]{32,256}$/;
const ADMIN_TOKEN = /^[A-Za-z0-9_-]{32,256}$/;
const ADMIN_STORAGE_KEY = "llmolympic.control.admin";
const CONTROL_MODES = ["play", "series", "round_robin", "championship"];
const CONTROL_ACTIVE_STATUSES = new Set([
  "prepared",
  "starting",
  "running",
  "cancel_requested",
  "finalizing",
]);
const CONTROL_STOPPABLE_STATUSES = new Set(["prepared", "starting", "running"]);
const CONTROL_STATUS_LABELS = {
  prepared: "等待确认",
  starting: "正在启动",
  running: "运行中",
  cancel_requested: "已请求停止",
  finalizing: "正在归档",
  cancelled: "已停止",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
};
const CONTROL_JOB_STATUSES = new Set(Object.keys(CONTROL_STATUS_LABELS));
const CONTROL_WARNING_COPY = {
  large_tournament: "这项循环赛包含较多对局；请再次核对 Provider 硬预算和预计耗时。",
  resume_uses_frozen_configuration: "恢复任务会沿用 checkpoint 中冻结的项目、参赛者、裁判与随机种子；若 checkpoint 含 Provider 硬预算，也会沿用该冻结预算。",
};
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
  admin_invalid: "管理凭证无效或已经失效。请重新使用服务输出的完整管理链接。",
  control_unauthorized: "管理凭证无效或已经失效。请重新使用服务输出的完整管理链接。",
  catalog_unavailable: "暂时无法读取可用项目与 Provider Profile。",
  job_conflict: "任务状态已经变化，请刷新后再操作。",
  job_state_conflict: "任务状态已经变化，请刷新后再操作。",
  job_not_found: "这个任务不存在，或已经不再可用。",
  job_not_stoppable: "这个任务当前不能停止，请刷新状态。",
  job_queue_full: "本机任务队列已满，请等待其他任务结束后重试。",
  job_capacity: "本机已有一项待确认或运行中的任务，请先完成或停止它。",
  idempotency_conflict: "同一操作标识对应了不同内容，请重新提交。",
  budget_required: "使用 Provider Profile 时必须填写完整的五项硬预算。",
  control_unavailable: "本机比赛控制服务暂时不可用。",
  control_overloaded: "本机比赛控制服务正忙，请稍后重试。",
  large_tournament_confirmation_required: "循环赛规模超过默认保护门槛；请勾选规模确认后重新生成预览。",
  profile_unavailable: "所选 Provider Profile 当前不可用，请检查服务环境后重试。",
  preview_stale: "这份准备态预览已经失效，请重新创建任务。",
  resume_unavailable: "无法从这份赛事 checkpoint 恢复，请刷新任务状态或检查本机存档。",
  controller_restarted: "本机控制服务曾重启；为避免重复调用或计费，这项任务不会自动重跑。",
  worker_failed: "比赛进程异常退出，未完成的结果不会写入正式存档或 ELO。",
  worker_interrupted: "比赛进程被系统中断，未完成的结果不会写入正式存档或 ELO。",
  worker_missing: "本机控制服务已经失去这项比赛进程的所有权。",
  worker_protocol_incomplete: "比赛进程未返回完整的受认证完成状态。",
  worker_shutdown_timeout: "比赛进程在服务关闭时未及时退出，已被安全终止。",
  worker_start_failed: "比赛进程启动失败；请检查本机 Profile、依赖和配置。",
  worker_start_interrupted: "比赛进程在启动阶段被中断。",
  worker_start_timeout: "比赛进程未在限定时间内就绪，已被安全停止。",
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

let inMemoryAdminToken = null;

function clearAdminToken() {
  inMemoryAdminToken = null;
  try {
    window.sessionStorage.removeItem(ADMIN_STORAGE_KEY);
  } catch (_error) {
    // Private browsing can deny Web Storage; there is nothing else to clear.
  }
}

function readAdminToken() {
  if (inMemoryAdminToken) return inMemoryAdminToken;
  try {
    const stored = window.sessionStorage.getItem(ADMIN_STORAGE_KEY);
    if (stored && ADMIN_TOKEN.test(stored)) {
      inMemoryAdminToken = stored;
      return stored;
    }
    if (stored) window.sessionStorage.removeItem(ADMIN_STORAGE_KEY);
  } catch (_error) {
    // Missing Web Storage is handled as a missing credential.
  }
  return null;
}

function captureAdminToken() {
  if (typeof window === "undefined" || !window.location.hash) return readAdminToken();
  let recognized = false;
  let token = null;
  try {
    const parameters = new URLSearchParams(window.location.hash.slice(1));
    const keys = Array.from(parameters.keys());
    const candidates = parameters.getAll("admin");
    recognized = parameters.has("admin");
    if (keys.length === 1 && keys[0] === "admin" && candidates.length === 1) {
      token = ADMIN_TOKEN.test(candidates[0]) ? candidates[0] : null;
    }
  } catch (_error) {
    recognized = window.location.hash.startsWith("#admin=");
  }
  if (!recognized) return readAdminToken();

  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
  clearAdminToken();
  if (!token) return null;
  inMemoryAdminToken = token;
  try {
    window.sessionStorage.setItem(ADMIN_STORAGE_KEY, token);
  } catch (_error) {
    // Keep the token only in this page when Web Storage is unavailable.
  }
  return token;
}

async function fetchControlJSON(
  path,
  adminToken,
  { body = null, idempotencyKey = null, method = "GET", signal } = {},
) {
  if (!adminToken || !ADMIN_TOKEN.test(adminToken)) throw new PublicError("control_unauthorized", 401);
  const headers = {
    Accept: "application/json",
    Authorization: `Bearer ${adminToken}`,
  };
  if (body !== null) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  let response;
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
    || snapshot.players.length < 2
    || snapshot.players.length > MAX_PARTICIPATION_PLAYERS
    || new Set(snapshot.players).size !== snapshot.players.length
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

function Header({ controlEnabled = false, health, participation = false }) {
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
          h("span", { className: "brand-sub" }, participation
            ? "HUMAN PARTICIPATION"
            : controlEnabled
              ? "LOCAL CONTROL + OBSERVER"
              : "ARCHIVE OBSERVER"),
        ),
      ),
      h(
        "div",
        { className: "status-cluster" },
        controlEnabled && !participation
          ? h(AppLink, { className: "button topbar-new", href: "/new" }, "新建比赛")
          : null,
        h("span", { className: "status-pill local" }, participation
          ? "本机 · 安全参赛"
          : controlEnabled
            ? "本机 · 比赛控制"
            : "本机 · 只读观战"),
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
  if (mode === "championship") return "淘汰锦标赛";
  return "单场对局";
}

function liveStatusLabel(status) {
  if (status === "completed") return "已完成并存档";
  if (status === "interrupted") return "直播已中断";
  return "正在进行";
}

function LiveMatchCard({ match }) {
  const context = match.mode === "championship" && match.round_number
    ? `第 ${match.round_number}/${match.round_count || "?"} 轮 · 对阵 ${match.round_pairing_number || "?"}/${match.round_pairing_count || "?"} · 第 ${match.leg_number || "?"} 局`
    : match.mode === "round_robin" && match.pairing_number
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
      copy: "启动 play、series、round-robin 或 championship 后，公开事件会自动出现在这里；比赛进程不依赖观战页。",
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

function safeControlText(value, maximum = 512) {
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text
    && Array.from(text).length <= maximum
    && !/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/.test(text)
    ? text
    : null;
}

function safeControlNumber(value, minimum = 0) {
  return typeof value === "number" && Number.isFinite(value) && value >= minimum
    ? value
    : null;
}

function normalizeControlCatalog(payload) {
  const sourceGames = Array.isArray(payload.games) ? payload.games : [];
  const games = sourceGames.flatMap((item) => {
    if (!isObject(item)) return [];
    const name = safeControlText(item.name, 64);
    const modes = Array.isArray(item.supported_modes)
      ? item.supported_modes.filter((mode) => CONTROL_MODES.includes(mode))
      : [];
    if (!name || !/^[a-z][a-z0-9_]{0,63}$/.test(name) || !modes.length) return [];
    const minimum = Number.isInteger(item.min_players) ? item.min_players : 2;
    const maximum = Number.isInteger(item.max_players) ? item.max_players : MAX_CONTROL_PLAYERS;
    return [{
      maxPlayers: Math.max(2, Math.min(MAX_CONTROL_PLAYERS, maximum)),
      minPlayers: Math.max(2, Math.min(MAX_CONTROL_PLAYERS, minimum)),
      name,
      requiresJudgePanel: Boolean(item.requires_judge_panel),
      roundsSupported: Boolean(item.rounds_supported),
      supportedModes: Array.from(new Set(modes)),
    }];
  });
  if (!games.length) throw new PublicError("protocol_error");

  const profiles = (Array.isArray(payload.profiles) ? payload.profiles : []).flatMap((item) => {
    if (!isObject(item)) return [];
    const profileId = safeControlText(item.profile_id, 128);
    const provider = safeControlText(item.provider, 128);
    const displayName = safeControlText(item.display_name, 256);
    const defaultModel = item.default_model === null
      ? null
      : safeControlText(item.default_model, 256);
    if (!profileId || !SAFE_PUBLIC_ID.test(profileId) || !provider || !displayName) return [];
    return [{
      available: item.credential_ready === true || item.available === true,
      defaultModel,
      displayName,
      profileId,
      provider,
    }];
  });
  const strategies = Array.isArray(payload.mock_player_strategies)
    ? payload.mock_player_strategies.filter((value) => safeControlText(value, 64) && SAFE_PUBLIC_ID.test(value))
    : [];
  const judgeStrategies = Array.isArray(payload.mock_judge_strategies)
    ? payload.mock_judge_strategies.filter((value) => safeControlText(value, 64) && SAFE_PUBLIC_ID.test(value))
    : [];
  return {
    games,
    judgeStrategies: Array.from(new Set(judgeStrategies)),
    profiles,
    strategies: Array.from(new Set(strategies)),
  };
}

function safeJobParticipant(value, index) {
  if (typeof value === "string") return safeControlText(value) || `选手 ${index + 1}`;
  if (!isObject(value)) return `选手 ${index + 1}`;
  const displayName = safeControlText(value.display_name) || safeControlText(value.name);
  if (displayName) return displayName;
  const profileId = safeControlText(value.profile_id);
  if (profileId) return `Profile · ${profileId}`;
  const strategy = safeControlText(value.strategy);
  if (strategy) return `Mock · ${strategy}`;
  return `选手 ${index + 1}`;
}

function safeJobJudge(value) {
  if (!isObject(value)) return null;
  if (value.kind === "mock") {
    const strategy = safeControlText(value.strategy, 64);
    return strategy && SAFE_PUBLIC_ID.test(strategy) ? `Mock · ${strategy}` : null;
  }
  if (value.kind === "profile") {
    const profileId = safeControlText(value.profile_id, 64);
    return profileId && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(profileId)
      ? `Profile · ${profileId}`
      : null;
  }
  return null;
}

function safePreparedProfile(value) {
  if (!isObject(value) || !exactKeys(value, [
    "configuration_digest",
    "default_model",
    "display_name",
    "effective_models",
    "profile_id",
    "provider",
  ])) return null;
  const profileId = safeControlText(value.profile_id, 64);
  const displayName = safeControlText(value.display_name, 256);
  const defaultModel = safeControlText(value.default_model, 256);
  const effectiveModels = Array.isArray(value.effective_models)
    ? value.effective_models.map((model) => safeControlText(model, 256))
    : [];
  const provider = safeControlText(value.provider, 32);
  if (
    !profileId
    || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(profileId)
    || !displayName
    || !defaultModel
    || effectiveModels.length < 1
    || effectiveModels.length > 25
    || effectiveModels.some((model) => !model)
    || effectiveModels.join("\u0000")
      !== Array.from(new Set(effectiveModels)).sort().join("\u0000")
    || !["openai", "ollama"].includes(provider)
    || typeof value.configuration_digest !== "string"
    || !/^[0-9a-f]{64}$/.test(value.configuration_digest)
  ) return null;
  const effectiveLabel = effectiveModels.join("、");
  const defaultSuffix = effectiveModels.length === 1 && effectiveModels[0] === defaultModel
    ? ""
    : `（当前默认 ${defaultModel}）`;
  return {
    defaultModel,
    displayName,
    effectiveModels,
    label: `${displayName} · ${provider} / 执行 ${effectiveLabel}${defaultSuffix}`,
    profileId,
    provider,
  };
}

function safeSeedText(value) {
  if (typeof value !== "string" || value.length > 20) return null;
  return /^(?:0|-?[1-9][0-9]*)$/.test(value) ? value : null;
}

function safeRounds(value) {
  return Number.isInteger(value) && value >= 1 && value <= 100 ? value : null;
}

function safeTimeout(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0.001 && value <= 86400
    ? value
    : null;
}

function safeNumericText(value, decimal = false) {
  if (typeof value === "number" && Number.isFinite(value) && value >= 0) return String(value);
  if (typeof value !== "string" || value.length > 32) return null;
  const pattern = decimal
    ? /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$/
    : /^(?:0|[1-9][0-9]*)$/;
  return pattern.test(value) ? value : null;
}

function participationLink(value, index) {
  const source = typeof value === "string"
    ? value
    : isObject(value)
      ? value.url || value.href
      : null;
  if (typeof source !== "string" || source.length > 2048) return null;
  try {
    const url = new URL(source, window.location.origin);
    if (url.origin !== window.location.origin || url.search) return null;
    const route = participationPath(url.pathname);
    if (!route) return null;
    const parameters = new URLSearchParams(url.hash.slice(1));
    const keys = Array.from(parameters.keys());
    const capabilities = parameters.getAll("capability");
    if (
      keys.length !== 1
      || keys[0] !== "capability"
      || capabilities.length !== 1
      || !CAPABILITY_TOKEN.test(capabilities[0])
    ) return null;
    const label = isObject(value)
      ? safeControlText(value.player_name) || safeControlText(value.label)
      : null;
    return {
      href: `${url.pathname}#capability=${encodeURIComponent(capabilities[0])}`,
      label: label || `人类席位 ${index + 1}`,
      seatId: route.seatId,
    };
  } catch (_error) {
    return null;
  }
}

function normalizeControlJob(payload) {
  const value = isObject(payload.job) ? payload.job : payload;
  if (!isObject(value)) throw new PublicError("protocol_error");
  const jobId = safeControlText(value.job_id || value.id, 128);
  const status = safeControlText(value.state || value.status, 64);
  if (!jobId || !SAFE_PUBLIC_ID.test(jobId) || !status || !CONTROL_JOB_STATUSES.has(status)) {
    throw new PublicError("protocol_error");
  }
  const configuration = isObject(value.spec)
    ? value.spec
    : isObject(value.configuration)
    ? value.configuration
    : isObject(value.request)
      ? value.request
      : value;
  const preview = isObject(value.preview) ? value.preview : {};
  const mode = CONTROL_MODES.includes(value.mode) ? value.mode : configuration.mode;
  if (!CONTROL_MODES.includes(mode)) throw new PublicError("protocol_error");
  const resumeTournamentId = typeof configuration.resume_tournament_id === "string"
    && SAFE_PUBLIC_ID.test(configuration.resume_tournament_id)
    ? configuration.resume_tournament_id
    : null;
  const resumeChampionshipId = typeof configuration.resume_championship_id === "string"
    && SAFE_PUBLIC_ID.test(configuration.resume_championship_id)
    ? configuration.resume_championship_id
    : null;
  if (resumeTournamentId && resumeChampionshipId) throw new PublicError("protocol_error");
  const isResume = (mode === "round_robin" && resumeTournamentId !== null)
    || (mode === "championship" && resumeChampionshipId !== null);
  if ((resumeTournamentId && mode !== "round_robin")
    || (resumeChampionshipId && mode !== "championship")) {
    throw new PublicError("protocol_error");
  }
  const configuredGame = safeControlText(value.game, 64)
    || safeControlText(configuration.game, 64)
    || "";
  const frozenGame = safeControlText(preview.frozen_game, 64) || "";
  const game = isResume ? frozenGame : configuredGame;
  if (game && !/^[a-z][a-z0-9_]{0,63}$/.test(game)) throw new PublicError("protocol_error");
  const configuredPlayers = Array.isArray(value.players)
    ? value.players
    : Array.isArray(configuration.players)
      ? configuration.players
      : [];
  const frozenPlayerSource = Array.isArray(preview.frozen_players)
    ? preview.frozen_players.slice(0, MAX_CONTROL_PLAYERS)
    : [];
  const frozenPlayers = frozenPlayerSource.flatMap((player) => {
    const name = safeControlText(player);
    return name ? [name] : [];
  });
  if (frozenPlayers.length !== frozenPlayerSource.length) throw new PublicError("protocol_error");
  const rawPlayers = isResume ? frozenPlayers : configuredPlayers;
  const configuredJudges = (Array.isArray(configuration.judges) ? configuration.judges : [])
    .slice(0, 9)
    .flatMap((judge) => {
      const identity = safeJobJudge(judge);
      return identity ? [identity] : [];
    });
  const frozenJudgeSource = Array.isArray(preview.frozen_judges)
    ? preview.frozen_judges.slice(0, 9)
    : [];
  const frozenJudges = frozenJudgeSource.flatMap((judge) => {
    const identity = safeControlText(judge);
    return identity ? [identity] : [];
  });
  if (frozenJudges.length !== frozenJudgeSource.length) throw new PublicError("protocol_error");
  const judges = isResume ? frozenJudges : configuredJudges;
  const configuredRounds = safeRounds(configuration.rounds);
  const frozenRounds = safeRounds(preview.frozen_rounds);
  const rounds = isResume ? frozenRounds : configuredRounds;
  const configuredSeed = safeSeedText(configuration.seed);
  const frozenSeed = safeSeedText(preview.frozen_seed);
  const seed = isResume ? frozenSeed : configuredSeed;
  const configuredLlmTimeoutSeconds = safeTimeout(configuration.llm_timeout_seconds);
  const frozenLlmTimeoutSeconds = safeTimeout(preview.frozen_llm_timeout_seconds);
  const preparedProfileSource = Array.isArray(preview.prepared_profiles)
    ? preview.prepared_profiles
    : [];
  if (preparedProfileSource.length > 25) throw new PublicError("protocol_error");
  const preparedProfiles = preparedProfileSource.flatMap((profile) => {
    const normalized = safePreparedProfile(profile);
    return normalized ? [normalized] : [];
  });
  if (preparedProfiles.length !== preparedProfileSource.length) {
    throw new PublicError("protocol_error");
  }
  const preparedProfileIds = preparedProfiles.map((profile) => profile.profileId);
  if (preparedProfileIds.join("\n") !== Array.from(new Set(preparedProfileIds)).sort().join("\n")) {
    throw new PublicError("protocol_error");
  }
  const progressSource = isObject(value.progress) ? value.progress : {};
  const budgetSource = isObject(configuration.budget) ? configuration.budget : {};
  const budget = {
    fromCheckpoint: isResume,
    maxEstimatedCostUsd: safeNumericText(budgetSource.max_estimated_cost_usd, true),
    maxInputTokens: safeNumericText(budgetSource.max_input_tokens),
    maxOutputTokensPerCall: safeNumericText(budgetSource.max_output_tokens_per_call),
    maxProviderCalls: safeNumericText(budgetSource.max_provider_calls),
    maxTotalOutputTokens: safeNumericText(budgetSource.max_total_output_tokens),
    usesFrozenBudget: preview.uses_frozen_budget === true,
  };
  const current = safeControlNumber(progressSource.current ?? progressSource.completed ?? value.completed_matches);
  const total = safeControlNumber(progressSource.total ?? value.total_matches);
  const liveId = safeControlText(value.live_id, 128)
    || (isObject(value.live) ? safeControlText(value.live.live_id || value.live.id, 128) : null);
  const finalSource = Array.isArray(value.final_match_ids)
    ? value.final_match_ids
    : isObject(value.result) && Array.isArray(value.result.match_ids)
      ? value.result.match_ids
      : value.final_match_id
        ? [value.final_match_id]
        : [];
  const finalMatchIds = Array.from(new Set(finalSource.filter((id) => (
    typeof id === "string" && SAFE_PUBLIC_ID.test(id)
  ))));
  const linkSource = Array.isArray(value.participation_links)
    ? value.participation_links
    : Array.isArray(value.participation_urls)
      ? value.participation_urls
      : [];
  const participationLinks = linkSource
    .map(participationLink)
    .filter(Boolean);
  const warningSource = Array.isArray(preview.warnings)
    ? preview.warnings
    : Array.isArray(value.warnings)
      ? value.warnings
      : [];
  return {
    budget,
    createdAt: validTimestamp(value.created_at) ? value.created_at : null,
    current,
    errorCode: typeof (value.failure_code || value.error_code) === "string"
      && PUBLIC_ERROR_CODES.has(value.failure_code || value.error_code)
      ? value.failure_code || value.error_code
      : null,
    estimatedMatchCount: safeControlNumber(preview.match_count ?? value.estimated_match_count, 1),
    estimatedProviderCalls: safeControlNumber(preview.estimated_provider_calls ?? value.estimated_provider_calls, 0),
    finalId: typeof value.final_id === "string" && SAFE_PUBLIC_ID.test(value.final_id)
      ? value.final_id
      : null,
    finalKind: ["match", "series", "tournament", "championship"].includes(value.final_kind)
      ? value.final_kind
      : null,
    finalMatchIds,
    finishedAt: validTimestamp(value.finished_at) ? value.finished_at : null,
    game,
    humanTimeoutSeconds: safeTimeout(configuration.human_timeout_seconds),
    isResume,
    jobId,
    judges,
    largeTournamentAllowed: configuration.allow_large_tournament === true,
    liveId: liveId && SAFE_PUBLIC_ID.test(liveId) ? liveId : null,
    llmTimeoutSeconds: isResume ? frozenLlmTimeoutSeconds : configuredLlmTimeoutSeconds,
    mode,
    participationLinks,
    players: rawPlayers.slice(0, MAX_CONTROL_PLAYERS).map(safeJobParticipant),
    preparedProfiles,
    resumable: value.resumable === true,
    rounds,
    seed,
    startedAt: validTimestamp(value.started_at) ? value.started_at : null,
    status,
    total,
    championshipId: [value.championship_id, resumeChampionshipId,
      value.final_kind === "championship" ? value.final_id : null]
      .find((candidate) => typeof candidate === "string" && SAFE_PUBLIC_ID.test(candidate)) || null,
    tournamentId: [value.tournament_id, resumeTournamentId,
      value.final_kind === "tournament" ? value.final_id : null]
      .find((candidate) => typeof candidate === "string" && SAFE_PUBLIC_ID.test(candidate)) || null,
    updatedAt: validTimestamp(value.updated_at) ? value.updated_at : null,
    warnings: warningSource.flatMap((warning) => {
      const code = safeControlText(warning, 128);
      return code && Object.prototype.hasOwnProperty.call(CONTROL_WARNING_COPY, code)
        ? [CONTROL_WARNING_COPY[code]]
        : [];
    }).slice(0, 20),
  };
}

function controlStatusLabel(status) {
  return CONTROL_STATUS_LABELS[status] || "未知状态";
}

function controlJobCanResume(job) {
  return Boolean(
    job
    && ["round_robin", "championship"].includes(job.mode)
    && job.resumable
    && (job.mode === "round_robin" ? job.tournamentId : job.championshipId)
    && ["cancelled", "failed", "interrupted"].includes(job.status)
  );
}

function JobStatus({ status }) {
  return h("span", { className: `job-status status-${status}` },
    h("span", { className: "status-dot", "aria-hidden": "true" }),
    controlStatusLabel(status),
  );
}

function ControlBudgetFacts({ budget, compact = false }) {
  if (budget.fromCheckpoint) {
    return h("p", { className: "notice" }, budget.usesFrozenBudget
      ? "沿用 checkpoint 冻结预算"
      : "该 checkpoint 全为 Mock，无需 Provider 硬预算");
  }
  const rows = [
    ["Provider 调用", budget.maxProviderCalls, "次"],
    ["输入 Token", budget.maxInputTokens, ""],
    ["单次输出 Token", budget.maxOutputTokensPerCall, ""],
    ["累计输出 Token", budget.maxTotalOutputTokens, ""],
    ["预估成本", budget.maxEstimatedCostUsd, " USD"],
  ];
  return h("dl", { className: `budget-preview${compact ? " compact" : ""}` },
    ...rows.map(([label, value, suffix]) => h("div", { key: label },
      h("dt", null, label),
      h("dd", null, value === null ? "未限制" : `${value}${suffix}`),
    )),
  );
}

function ControlJobCard({ job }) {
  const progress = job.total !== null
    ? `${job.current === null ? 0 : job.current} / ${job.total}`
    : null;
  return h(AppLink, {
    "aria-label": `${modeLabel(job.mode)}，${job.game ? gameLabel(job.game) : "未知项目"}，${controlStatusLabel(job.status)}`,
    className: "control-job-card",
    href: `/jobs/${encodeURIComponent(job.jobId)}`,
  },
  h("div", { className: "control-job-main" },
    h("div", { className: "control-job-meta" },
      h("span", { className: "game-badge" }, job.game ? gameLabel(job.game) : "未知项目"),
      h("span", { className: "meta" }, modeLabel(job.mode)),
    ),
    h("strong", null, job.players.length ? job.players.join(" · ") : `任务 ${job.jobId}`),
    h("span", { className: "meta" }, `${dateTime(job.updatedAt || job.createdAt, true)}${progress ? ` · 进度 ${progress}` : ""}`),
  ),
  h(JobStatus, { status: job.status }),
  h("span", { className: "match-arrow", "aria-hidden": "true" }, "→"),
  );
}

function ControlJobsPanel({ adminToken, onAuthLost }) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let controller = null;
    const load = async (initial = false) => {
      if (cancelled || document.visibilityState === "hidden") return;
      if (controller) controller.abort();
      controller = new AbortController();
      if (initial) setState((previous) => ({ ...previous, loading: true }));
      try {
        const payload = await fetchControlJSON("/api/v1/control/jobs?limit=10", adminToken, {
          signal: controller.signal,
        });
        const source = Array.isArray(payload.jobs) ? payload.jobs : [];
        const jobs = source.slice(0, 10).flatMap((item) => {
          try {
            return [normalizeControlJob(item)];
          } catch (_error) {
            return [];
          }
        });
        if (!cancelled) setState({ data: jobs, error: null, loading: false });
      } catch (error) {
        if (cancelled || error.name === "AbortError") return;
        if ([401, 403].includes(error.status)) onAuthLost();
        setState((previous) => ({ ...previous, error, loading: false }));
      }
    };
    load(true);
    const timer = window.setInterval(() => load(false), 2000);
    const onVisibility = () => { if (document.visibilityState === "visible") load(false); };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      if (controller) controller.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [adminToken, onAuthLost, reloadKey]);

  let body;
  if (state.loading && !state.data) {
    body = h(LoadingRows, { count: 2 });
  } else if (state.error && !state.data) {
    body = h(StateCard, {
      action: h("button", {
        className: "button small",
        onClick: () => setReloadKey((value) => value + 1),
        type: "button",
      }, "重试"),
      copy: errorCopy(state.error),
      error: true,
      title: "最近任务加载失败",
    });
  } else if (!state.data || !state.data.length) {
    body = h(StateCard, {
      action: h(AppLink, { className: "button primary", href: "/new" }, "创建第一项任务"),
      copy: "先准备配置并检查预览；只有再次确认后，比赛进程才会启动。",
      title: "还没有 Web 任务",
    });
  } else {
    body = h("div", { className: "control-job-list" },
      ...state.data.map((job) => h(ControlJobCard, { job, key: job.jobId })),
    );
  }
  return h("section", { className: "panel control-jobs-panel", "aria-labelledby": "control-jobs-heading" },
    h("div", { className: "panel-head" },
      h("div", null,
        h("p", { className: "panel-overline" }, "LOCAL CONTROL"),
        h("h2", { id: "control-jobs-heading" }, "最近任务"),
      ),
      h(AppLink, { className: "button primary", href: "/new" }, "+ 新建比赛 / 任务"),
    ),
    h("div", { className: "panel-body" }, body),
  );
}

function defaultControlPlayer(index, catalog) {
  const strategies = catalog && catalog.strategies.length ? catalog.strategies : ["random", "fixed"];
  return { kind: "mock", name: "", profileId: "", strategy: strategies[index % strategies.length] };
}

function defaultControlJudge(index, catalog) {
  const strategies = catalog && catalog.judgeStrategies.length
    ? catalog.judgeStrategies
    : ["strict", "balanced", "lenient"];
  return { kind: "mock", profileId: "", strategy: strategies[index % strategies.length] };
}

function initialControlForm() {
  return {
    allowLargeTournament: false,
    budget: {
      maxEstimatedCostUsd: "",
      maxInputTokens: "200000",
      maxOutputTokensPerCall: "4096",
      maxProviderCalls: "64",
      maxTotalOutputTokens: "65536",
    },
    game: "",
    judges: [],
    llmTimeoutSeconds: "120",
    mode: "play",
    players: [defaultControlPlayer(0), defaultControlPlayer(1)],
    rounds: "1",
    seed: "42",
    timeoutSeconds: "300",
  };
}

function formNumber(value, { integer = true, maximum = Number.MAX_SAFE_INTEGER, minimum = 0 } = {}) {
  if (typeof value !== "string" || !value.trim()) return null;
  const number = Number(value);
  if (
    !Number.isFinite(number)
    || number < minimum
    || number > maximum
    || (integer && !Number.isSafeInteger(number))
  ) return null;
  return number;
}

function canonicalIntegerText(value, signed = false) {
  return typeof value === "string"
    && (signed ? /^(?:0|-?[1-9][0-9]*)$/ : /^(?:0|[1-9][0-9]*)$/).test(value.trim());
}

function canonicalCostText(value) {
  return typeof value === "string"
    && /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$/.test(value.trim());
}

function controlPlayerIdentity(player) {
  if (player.kind === "human") return `human:${player.name.trim()}`;
  if (player.kind === "mock") return `mock:${player.strategy}`;
  return `profile:${player.profileId}`;
}

function validateControlForm(form, catalog) {
  const errors = {};
  const game = catalog.games.find((item) => item.name === form.game);
  if (!game || !game.supportedModes.includes(form.mode)) errors.game = "所选项目不支持这个比赛模式。";
  const minimumPlayers = form.mode === "championship"
    ? 4
    : form.mode === "round_robin"
      ? 3
      : game ? game.minPlayers : 2;
  const maximumPlayers = ["round_robin", "championship"].includes(form.mode)
    ? MAX_CONTROL_PLAYERS
    : game ? game.maxPlayers : 2;
  if (form.mode === "series" && form.players.length !== 2) {
    errors.players = "双局赛必须正好有两位选手。";
  } else if (form.mode === "championship" && ![4, 8, 16].includes(form.players.length)) {
    errors.players = "淘汰锦标赛必须正好有 4、8 或 16 位选手。";
  } else if (form.players.length < minimumPlayers || form.players.length > maximumPlayers) {
    errors.players = `${modeLabel(form.mode)}需要 ${minimumPlayers}–${maximumPlayers} 位选手。`;
  }
  form.players.forEach((player, index) => {
    const key = `player-${index}`;
    if (!isObject(player) || !["human", "mock", "profile"].includes(player.kind)) {
      errors[key] = "请选择有效的选手类型。";
    } else if (player.kind === "human") {
      if (form.mode !== "play") errors[key] = "只有单场对局支持人类席位。";
      else if (!safeControlText(player.name, 80) || player.name.includes(",")) errors[key] = "请输入 1–80 个字符且不含逗号的人类选手名称。";
    } else if (player.kind === "mock") {
      if (!catalog.strategies.includes(player.strategy)) errors[key] = "请选择可用的 Mock 策略。";
    } else {
      const profile = catalog.profiles.find((item) => item.profileId === player.profileId);
      if (!profile || !profile.available) errors[key] = "请选择当前可用的 Provider Profile。";
    }
  });
  const participantNames = form.players.map(controlPlayerIdentity);
  if (participantNames.some(Boolean) && new Set(participantNames).size !== participantNames.length) {
    errors.players = "每位参赛者必须有不同的名称、策略或 Profile。";
  }
  if (game && game.requiresJudgePanel) {
    if (form.judges.length < 3 || form.judges.length > 9) {
      errors.judges = "这个项目需要 3–9 位裁判。";
    }
    form.judges.forEach((judge, index) => {
      const key = `judge-${index}`;
      if (judge.kind === "mock") {
        if (!catalog.judgeStrategies.includes(judge.strategy)) errors[key] = "请选择可用的 Mock 裁判策略。";
      } else if (judge.kind === "profile") {
        const profile = catalog.profiles.find((item) => item.profileId === judge.profileId);
        if (!profile || !profile.available) errors[key] = "请选择当前可用的 Provider Profile。";
      } else {
        errors[key] = "请选择有效的裁判类型。";
      }
    });
    const judgeNames = form.judges.map((judge) => judge.kind === "profile"
      ? `profile:${judge.profileId}`
      : `mock:${judge.strategy}`);
    if (new Set(judgeNames).size !== judgeNames.length) {
      errors.judges = "每位裁判必须使用不同的策略或 Profile。";
    } else if (judgeNames.some((name) => form.players.some((player) => (
      player.kind === "profile"
        ? name === `profile:${player.profileId}`
        : player.kind === "mock" && name === `mock:${player.strategy}`
    )))) {
      errors.judges = "同一个 Mock 策略或 Profile 不能同时作为选手和裁判。";
    }
  }
  if (game && game.roundsSupported && formNumber(form.rounds, { maximum: 100, minimum: 1 }) === null) {
    errors.rounds = "回合数必须是 1–100 的整数。";
  }
  if (!canonicalIntegerText(form.seed, true)
    || formNumber(form.seed, { maximum: Number.MAX_SAFE_INTEGER, minimum: Number.MIN_SAFE_INTEGER }) === null) {
    errors.seed = "随机种子必须是浏览器可安全表示的整数。";
  }
  if (formNumber(form.timeoutSeconds, { integer: false, maximum: 86400, minimum: 1 }) === null) {
    errors.timeoutSeconds = "人类席位限时必须是 1–86400 秒。";
  }
  if (formNumber(form.llmTimeoutSeconds, { integer: false, maximum: 86400, minimum: 1 }) === null) {
    errors.llmTimeoutSeconds = "模型单步限时必须是 1–86400 秒。";
  }
  const budgetFields = [
    ["maxProviderCalls", "最大 Provider 调用数", true, 0],
    ["maxInputTokens", "最大输入 Token", true, 0],
    ["maxOutputTokensPerCall", "单次最大输出 Token", true, 1],
    ["maxTotalOutputTokens", "累计最大输出 Token", true, 0],
    ["maxEstimatedCostUsd", "最大预估成本", false, 0],
  ];
  const profileUsed = form.players.some((player) => player.kind === "profile")
    || (game && game.requiresJudgePanel && form.judges.some((judge) => judge.kind === "profile"));
  budgetFields.forEach(([key, label, integer, minimum]) => {
    const value = form.budget[key];
    if (value === "" && key !== "maxOutputTokensPerCall" && !profileUsed) return;
    const canonical = integer ? canonicalIntegerText(value) : canonicalCostText(value);
    if (!canonical || formNumber(value, {
      integer,
      maximum: integer ? 1000000000 : 1000000,
      minimum,
    }) === null) {
      const requirement = integer
        ? minimum === 0 ? "非负整数" : "正整数"
        : "非负数字";
      errors[`budget-${key}`] = `${label}必须是${requirement}。`;
    }
  });
  return errors;
}

function controlRequestBody(form, catalog) {
  const game = catalog.games.find((item) => item.name === form.game);
  const entrant = (player) => {
    if (player.kind === "human") return { kind: "human", name: player.name.trim() };
    if (player.kind === "profile") return { kind: "profile", profile_id: player.profileId };
    return { kind: "mock", strategy: player.strategy };
  };
  const judge = (item) => item.kind === "profile"
    ? { kind: "profile", profile_id: item.profileId }
    : { kind: "mock", strategy: item.strategy };
  const optionalInteger = (value) => value === "" ? null : value.trim();
  const optionalCost = (value) => value === "" ? null : value.trim();
  return {
    allow_large_tournament: form.mode === "round_robin" && form.allowLargeTournament,
    budget: {
      max_estimated_cost_usd: optionalCost(form.budget.maxEstimatedCostUsd),
      max_input_tokens: optionalInteger(form.budget.maxInputTokens),
      max_output_tokens_per_call: optionalInteger(form.budget.maxOutputTokensPerCall),
      max_provider_calls: optionalInteger(form.budget.maxProviderCalls),
      max_total_output_tokens: optionalInteger(form.budget.maxTotalOutputTokens),
    },
    game: form.game,
    human_timeout_seconds: formNumber(form.timeoutSeconds, { integer: false, maximum: 86400, minimum: 1 }),
    judges: game && game.requiresJudgePanel ? form.judges.map(judge) : [],
    llm_timeout_seconds: formNumber(form.llmTimeoutSeconds, { integer: false, maximum: 86400, minimum: 1 }),
    mode: form.mode,
    players: form.players.map(entrant),
    rounds: game && game.roundsSupported
      ? formNumber(form.rounds, { maximum: 100, minimum: 1 })
      : null,
    seed: form.seed.trim(),
    resume_championship_id: null,
    resume_tournament_id: null,
  };
}

function ControlField({ children, error, help, id, label }) {
  const describedBy = [help ? `${id}-help` : null, error ? `${id}-error` : null].filter(Boolean).join(" ") || undefined;
  const control = typeof children === "function"
    ? children({ "aria-describedby": describedBy, "aria-invalid": error ? "true" : undefined, id })
    : children;
  return h("div", { className: `control-field${error ? " invalid" : ""}` },
    h("label", { htmlFor: id }, label),
    help ? h("p", { className: "field-help", id: `${id}-help` }, help) : null,
    control,
    error ? h("p", { className: "field-error", id: `${id}-error` }, error) : null,
  );
}

function ControlPlayerEditor({ catalog, error, index, mode, onChange, onRemove, player, removable }) {
  const id = `player-${index}`;
  const availableProfiles = catalog.profiles;
  const kinds = [
    ...(mode === "play" ? [{ label: "人类浏览器席位", value: "human" }] : []),
    { label: "内置 Mock", value: "mock" },
    { label: "Provider Profile", value: "profile" },
  ];
  const chooseKind = (kind) => {
    if (kind === "human") onChange({ kind, name: `人类选手 ${index + 1}`, profileId: "", strategy: "" });
    else if (kind === "profile") onChange({
      kind,
      name: "",
      profileId: (availableProfiles.find((item) => item.available) || {}).profileId || "",
      strategy: "",
    });
    else onChange(defaultControlPlayer(index, catalog));
  };
  return h("fieldset", { className: `roster-card${error ? " invalid" : ""}` },
    h("legend", null, `选手 ${index + 1}`),
    h("div", { className: "roster-card-grid" },
      h(ControlField, { error, id: `${id}-kind`, label: "类型" }, (attributes) => h("select", {
        ...attributes,
        onChange: (event) => chooseKind(event.target.value),
        value: player.kind,
      }, ...kinds.map((item) => h("option", { key: item.value, value: item.value }, item.label)))),
      player.kind === "human" ? h(ControlField, { error, id: `${id}-name`, label: "显示名称" }, (attributes) => h("input", {
        ...attributes,
        autoComplete: "off",
        maxLength: 80,
        onChange: (event) => onChange({ ...player, name: event.target.value }),
        type: "text",
        value: player.name,
      })) : null,
      player.kind === "mock" ? h(ControlField, { error, id: `${id}-strategy`, label: "策略" }, (attributes) => h("select", {
        ...attributes,
        onChange: (event) => onChange({ ...player, strategy: event.target.value }),
        value: player.strategy,
      }, ...catalog.strategies.map((strategy) => h("option", { key: strategy, value: strategy }, strategy)))) : null,
      player.kind === "profile" ? h(ControlField, {
        error,
        help: "不可用 Profile 会保留在列表中，但不能选择。",
        id: `${id}-profile`,
        label: "Provider Profile",
      }, (attributes) => h("select", {
        ...attributes,
        onChange: (event) => onChange({ ...player, profileId: event.target.value }),
        value: player.profileId,
      },
      h("option", { value: "" }, "请选择 Profile"),
      ...availableProfiles.map((profile) => h("option", {
        disabled: !profile.available,
        key: profile.profileId,
        value: profile.profileId,
      }, `${profile.displayName} · ${profile.provider}${profile.defaultModel ? ` · ${profile.defaultModel}` : ""}${profile.available ? "" : "（不可用）"}`)),
      )) : null,
    ),
    removable ? h("button", { className: "button small roster-remove", onClick: onRemove, type: "button" }, `移除选手 ${index + 1}`) : null,
  );
}

function ControlJudgeEditor({ catalog, error, index, judge, onChange, onRemove }) {
  const id = `judge-${index}`;
  const chooseKind = (kind) => {
    if (kind === "profile") onChange({
      kind,
      profileId: (catalog.profiles.find((item) => item.available) || {}).profileId || "",
      strategy: "",
    });
    else onChange(defaultControlJudge(index, catalog));
  };
  return h("fieldset", { className: `roster-card compact${error ? " invalid" : ""}` },
    h("legend", null, `裁判 ${index + 1}`),
    h("div", { className: "roster-card-grid" },
      h(ControlField, { error, id: `${id}-kind`, label: "类型" }, (attributes) => h("select", {
        ...attributes,
        onChange: (event) => chooseKind(event.target.value),
        value: judge.kind,
      },
      h("option", { value: "mock" }, "内置 Mock 裁判"),
      h("option", { value: "profile" }, "Provider Profile"),
      )),
      judge.kind === "mock" ? h(ControlField, { error, id: `${id}-strategy`, label: "策略" }, (attributes) => h("select", {
        ...attributes,
        onChange: (event) => onChange({ ...judge, strategy: event.target.value }),
        value: judge.strategy,
      }, ...catalog.judgeStrategies.map((strategy) => h("option", { key: strategy, value: strategy }, strategy)))) : null,
      judge.kind === "profile" ? h(ControlField, { error, id: `${id}-profile`, label: "Provider Profile" }, (attributes) => h("select", {
        ...attributes,
        onChange: (event) => onChange({ ...judge, profileId: event.target.value }),
        value: judge.profileId,
      },
      h("option", { value: "" }, "请选择 Profile"),
      ...catalog.profiles.map((profile) => h("option", {
        disabled: !profile.available,
        key: profile.profileId,
        value: profile.profileId,
      }, `${profile.displayName}${profile.available ? "" : "（不可用）"}`)),
      )) : null,
    ),
    h("button", { className: "button small roster-remove", onClick: onRemove, type: "button" }, `移除裁判 ${index + 1}`),
  );
}

function PreparedJobPreview({ backLabel = "取消预览并返回修改", busy, error, job, onBack, onStart }) {
  const previewRef = useRef(null);
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      if (previewRef.current) previewRef.current.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);
  return h("section", {
    className: "panel prepared-preview",
    "aria-labelledby": "prepared-heading",
    ref: previewRef,
    tabIndex: -1,
  },
    h("div", { className: "panel-head" },
      h("div", null,
        h("p", { className: "panel-overline" }, "PREPARED · NOT STARTED"),
        h("h2", { id: "prepared-heading" }, "确认后才会启动"),
      ),
      h(JobStatus, { status: job.status }),
    ),
    h("div", { className: "prepared-body" },
      h("p", { className: "prepared-lead" }, "配置已经通过服务端准备校验，目前没有比赛进程在运行。请核对下面的范围与预算。"),
      h("dl", { className: "preview-grid" },
        h("div", null, h("dt", null, "模式"), h("dd", null, modeLabel(job.mode))),
        h("div", null, h("dt", null, "项目"), h("dd", null, job.game ? gameLabel(job.game) : "—")),
        h("div", null, h("dt", null, "参赛者"), h("dd", null, job.players.length ? job.players.join("、") : "—")),
        h("div", null, h("dt", null, "裁判"), h("dd", null, job.judges.length ? job.judges.join("、") : "无")),
        h("div", null, h("dt", null, "回合数"), h("dd", null, job.rounds === null ? "项目默认 / 不适用" : String(job.rounds))),
        h("div", null, h("dt", null, "随机种子"), h("dd", { className: "mono" }, job.seed || "—")),
        h("div", null, h("dt", null, "人类席位限时"), h("dd", null, job.humanTimeoutSeconds === null ? "未设置" : `${formatScore(job.humanTimeoutSeconds)} 秒`)),
        h("div", null, h("dt", null, "模型单步限时"), h("dd", null, job.llmTimeoutSeconds === null ? "未设置" : `${formatScore(job.llmTimeoutSeconds)} 秒`)),
        job.preparedProfiles.length ? h("div", null,
          h("dt", null, "已确认 Profile"),
          h("dd", null, job.preparedProfiles.map((profile) => profile.label).join("；")),
        ) : null,
        h("div", null, h("dt", null, "大规模循环赛"), h("dd", null, job.mode === "round_robin" ? job.largeTournamentAllowed ? "已明确允许" : "未允许" : "不适用")),
        h("div", null, h("dt", null, "预计对局"), h("dd", null, job.estimatedMatchCount === null ? "由服务端运行时确定" : `${job.estimatedMatchCount} 场`)),
        h("div", null, h("dt", null, "预计 Provider 调用"), h("dd", null, job.estimatedProviderCalls === null ? "按硬预算限制" : `${job.estimatedProviderCalls} 次`)),
        h("div", null, h("dt", null, "任务编号"), h("dd", { className: "mono" }, job.jobId)),
      ),
      h("section", { "aria-labelledby": "preview-budget-heading" },
        h("h3", { className: "subsection-title", id: "preview-budget-heading" }, "Provider 硬预算"),
        h(ControlBudgetFacts, { budget: job.budget }),
      ),
      job.warnings.length ? h("div", { className: "preview-warnings", role: "status" },
        h("strong", null, "启动前提示"),
        h("ul", null, ...job.warnings.map((warning, index) => h("li", { key: index }, warning))),
      ) : null,
      error ? h("p", { className: "form-submit-error", role: "alert" }, errorCopy(error)) : null,
      h("div", { className: "confirmation-box" },
        h("div", null,
          h("strong", null, "这是会调用模型并写入本机存档的操作"),
          h("p", null, "启动后可在任务页查看进度并请求停止；已经产生的 Provider 调用不能撤回。"),
        ),
        h("div", { className: "form-actions" },
          h("button", { className: "button", disabled: busy, onClick: onBack, type: "button" }, backLabel),
          h("button", { className: "button coral", disabled: busy, onClick: onStart, type: "button" }, busy ? "正在启动…" : "确认并启动"),
        ),
      ),
    ),
  );
}

function NewJobPage({ adminToken, onAuthLost }) {
  const [catalogState, setCatalogState] = useState({ catalog: null, error: null, loading: true });
  const [form, setForm] = useState(initialControlForm);
  const [errors, setErrors] = useState({});
  const [prepared, setPrepared] = useState(null);
  const [prepareError, setPrepareError] = useState(null);
  const [busy, setBusy] = useState(false);
  const errorSummaryRef = useRef(null);
  const prepareKeyRef = useRef(null);
  const startKeyRef = useRef(null);
  const discardKeyRef = useRef(null);

  useEffect(() => {
    const controller = new AbortController();
    setCatalogState({ catalog: null, error: null, loading: true });
    fetchControlJSON("/api/v1/control/catalog", adminToken, { signal: controller.signal })
      .then((payload) => {
        const catalog = normalizeControlCatalog(payload);
        const game = catalog.games.find((item) => item.supportedModes.includes("play")) || catalog.games[0];
        setForm((previous) => ({
          ...previous,
          game: game.name,
          judges: game.requiresJudgePanel
            ? [0, 1, 2].map((index) => defaultControlJudge(index, catalog))
            : [],
          players: [defaultControlPlayer(0, catalog), defaultControlPlayer(1, catalog)],
        }));
        setCatalogState({ catalog, error: null, loading: false });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        if ([401, 403].includes(error.status)) onAuthLost();
        setCatalogState({ catalog: null, error, loading: false });
      });
    return () => controller.abort();
  }, [adminToken, onAuthLost]);

  const catalog = catalogState.catalog;
  const changeForm = (updater) => {
    setForm((previous) => typeof updater === "function" ? updater(previous) : { ...previous, ...updater });
    setErrors({});
    setPrepared(null);
    setPrepareError(null);
    prepareKeyRef.current = null;
    startKeyRef.current = null;
    discardKeyRef.current = null;
  };

  if (catalogState.loading) {
    return h("main", { className: "page control-page", id: "main-content", tabIndex: -1 },
      h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回大厅"),
      h("section", { className: "panel" }, h("div", { className: "panel-body" }, h(LoadingRows, { count: 5 }))),
    );
  }
  if (!catalog) {
    return h("main", { className: "page control-page", id: "main-content", tabIndex: -1 },
      h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回大厅"),
      h(StateCard, {
        copy: errorCopy(catalogState.error),
        error: true,
        title: "无法打开创建向导",
      }),
    );
  }

  const currentGame = catalog.games.find((item) => item.name === form.game);
  const availableGames = catalog.games.filter((item) => item.supportedModes.includes(form.mode));
  const minimumPlayers = form.mode === "championship"
    ? 4
    : form.mode === "round_robin"
      ? 3
      : currentGame ? currentGame.minPlayers : 2;
  const maximumPlayers = ["round_robin", "championship"].includes(form.mode)
    ? MAX_CONTROL_PLAYERS
    : currentGame ? currentGame.maxPlayers : 2;

  const changeMode = (mode) => {
    const fallbackGame = catalog.games.find((item) => item.supportedModes.includes(mode));
    if (!fallbackGame) return;
    changeForm((previous) => {
      const selectedGame = catalog.games.find((item) => (
        item.name === previous.game && item.supportedModes.includes(mode)
      )) || fallbackGame;
      const desiredCount = mode === "championship"
        ? 4
        : mode === "round_robin"
          ? 3
        : mode === "series"
          ? 2
          : Math.max(2, selectedGame.minPlayers);
      return {
        ...previous,
        allowLargeTournament: mode === "round_robin" ? previous.allowLargeTournament : false,
        game: selectedGame.name,
        judges: selectedGame.requiresJudgePanel
          ? (previous.judges.length >= 3 ? previous.judges : [0, 1, 2].map((index) => defaultControlJudge(index, catalog)))
          : [],
        mode,
        players: Array.from({ length: desiredCount }, (_, index) => {
          const existing = previous.players[index];
          return existing && (mode === "play" || existing.kind !== "human")
            ? existing
            : defaultControlPlayer(index, catalog);
        }),
      };
    });
  };
  const changeGame = (name) => {
    const game = catalog.games.find((item) => item.name === name);
    if (!game) return;
    changeForm((previous) => ({
      ...previous,
      game: name,
      judges: game.requiresJudgePanel
        ? (previous.judges.length >= 3 ? previous.judges : [0, 1, 2].map((index) => defaultControlJudge(index, catalog)))
        : [],
    }));
  };
  const updatePlayer = (index, player) => changeForm((previous) => ({
    ...previous,
    players: previous.players.map((item, itemIndex) => itemIndex === index ? player : item),
  }));
  const removePlayer = (index) => changeForm((previous) => ({
    ...previous,
    players: previous.players.filter((_item, itemIndex) => itemIndex !== index),
  }));
  const updateJudge = (index, judge) => changeForm((previous) => ({
    ...previous,
    judges: previous.judges.map((item, itemIndex) => itemIndex === index ? judge : item),
  }));
  const removeJudge = (index) => changeForm((previous) => ({
    ...previous,
    judges: previous.judges.filter((_item, itemIndex) => itemIndex !== index),
  }));

  const prepare = async (event) => {
    event.preventDefault();
    const nextErrors = validateControlForm(form, catalog);
    setErrors(nextErrors);
    setPrepareError(null);
    if (Object.keys(nextErrors).length) {
      window.requestAnimationFrame(() => errorSummaryRef.current && errorSummaryRef.current.focus());
      return;
    }
    setBusy(true);
    try {
      const payload = await fetchControlJSON("/api/v1/control/jobs", adminToken, {
        body: controlRequestBody(form, catalog),
        idempotencyKey: prepareKeyRef.current || (prepareKeyRef.current = createSubmissionId()),
        method: "POST",
      });
      setPrepared(normalizeControlJob(payload));
      startKeyRef.current = null;
      discardKeyRef.current = null;
    } catch (error) {
      if ([401, 403].includes(error.status)) onAuthLost();
      setPrepareError(error);
    } finally {
      setBusy(false);
    }
  };
  const start = async () => {
    if (!prepared) return;
    setBusy(true);
    setPrepareError(null);
    try {
      const payload = await fetchControlJSON(
        `/api/v1/control/jobs/${encodeURIComponent(prepared.jobId)}/start`,
        adminToken,
        {
          body: {},
          idempotencyKey: startKeyRef.current || (startKeyRef.current = createSubmissionId()),
          method: "POST",
        },
      );
      const job = normalizeControlJob(payload);
      navigate(`/jobs/${encodeURIComponent(job.jobId)}`);
    } catch (error) {
      if ([401, 403].includes(error.status)) onAuthLost();
      setPrepareError(error);
      setBusy(false);
    }
  };
  const discard = async () => {
    if (!prepared) return;
    setBusy(true);
    setPrepareError(null);
    try {
      await fetchControlJSON(
        `/api/v1/control/jobs/${encodeURIComponent(prepared.jobId)}/cancel`,
        adminToken,
        {
          body: {},
          idempotencyKey: discardKeyRef.current || (discardKeyRef.current = createSubmissionId()),
          method: "POST",
        },
      );
      setPrepared(null);
      prepareKeyRef.current = null;
      startKeyRef.current = null;
      discardKeyRef.current = null;
    } catch (error) {
      if ([401, 403].includes(error.status)) onAuthLost();
      setPrepareError(error);
    } finally {
      setBusy(false);
    }
  };

  if (prepared) {
    return h("main", { className: "page control-page", id: "main-content", tabIndex: -1 },
      h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回大厅"),
      h("section", { className: "control-hero", "aria-labelledby": "new-title" },
        h("p", { className: "eyebrow" }, "STEP 2 OF 2 · REVIEW"),
        h("h1", { className: "detail-title", id: "new-title" }, "核对启动预览"),
      ),
      h(PreparedJobPreview, {
        busy,
        error: prepareError,
        job: prepared,
        onBack: discard,
        onStart: start,
      }),
    );
  }

  const errorMessages = Array.from(new Set(Object.values(errors)));
  return h("main", { className: "page control-page", id: "main-content", tabIndex: -1 },
    h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回大厅"),
    h("section", { className: "control-hero", "aria-labelledby": "new-title" },
      h("p", { className: "eyebrow" }, "STEP 1 OF 2 · CONFIGURE"),
      h("h1", { className: "detail-title", id: "new-title" }, "新建比赛 / 任务"),
      h("p", { className: "hero-copy" }, "先选择项目、参赛者和硬预算，再生成不会自动运行的准备态预览。"),
    ),
    errorMessages.length ? h("div", {
      className: "form-error-summary",
      ref: errorSummaryRef,
      role: "alert",
      tabIndex: -1,
    },
    h("strong", null, `请修正 ${errorMessages.length} 个配置问题`),
    h("ul", null, ...errorMessages.map((message) => h("li", { key: message }, message))),
    ) : null,
    h("form", { className: "control-form", onSubmit: prepare },
      h("section", { className: "panel form-section", "aria-labelledby": "format-heading" },
        h("div", { className: "panel-head" }, h("h2", { id: "format-heading" }, "1. 比赛形式")),
        h("div", { className: "form-section-body field-grid" },
          h(ControlField, { id: "control-mode", label: "任务模式" }, (attributes) => h("select", {
            ...attributes,
            onChange: (event) => changeMode(event.target.value),
            value: form.mode,
          },
          h("option", { value: "play" }, "单场对局（play）"),
          h("option", { value: "series" }, "双局交换先后手（series）"),
          h("option", { value: "round_robin" }, "双循环赛（round-robin）"),
          h("option", { value: "championship" }, "淘汰锦标赛（championship）"),
          )),
          h(ControlField, { error: errors.game, id: "control-game", label: "比赛项目" }, (attributes) => h("select", {
            ...attributes,
            onChange: (event) => changeGame(event.target.value),
            value: form.game,
          }, ...availableGames.map((game) => h("option", { key: game.name, value: game.name }, gameLabel(game.name))))),
          currentGame && currentGame.roundsSupported ? h(ControlField, {
            error: errors.rounds,
            help: "应用到每一场对局。",
            id: "control-rounds",
            label: "每场回合数",
          }, (attributes) => h("input", {
            ...attributes,
            max: 100,
            min: 1,
            onChange: (event) => changeForm({ rounds: event.target.value }),
            type: "number",
            value: form.rounds,
          })) : null,
          h(ControlField, { error: errors.seed, id: "control-seed", label: "随机种子" }, (attributes) => h("input", {
            ...attributes,
            onChange: (event) => changeForm({ seed: event.target.value }),
            step: 1,
            type: "number",
            value: form.seed,
          })),
        ),
      ),
      h("section", { className: "panel form-section", "aria-labelledby": "players-heading" },
        h("div", { className: "panel-head" },
          h("div", null,
            h("h2", { id: "players-heading" }, "2. 参赛者"),
            h("span", { className: "panel-kicker" }, form.mode === "championship"
              ? "4 / 8 / 16 位"
              : `${minimumPlayers}–${maximumPlayers} 位`),
          ),
          form.mode !== "series" && form.players.length < maximumPlayers
            ? h("button", {
              className: "button small",
              onClick: () => changeForm((previous) => ({
                ...previous,
                players: [...previous.players, defaultControlPlayer(previous.players.length, catalog)],
              })),
              type: "button",
            }, "+ 添加选手")
            : null,
        ),
        h("div", { className: "form-section-body roster-list" },
          errors.players ? h("p", { className: "section-error", role: "alert" }, errors.players) : null,
          ...form.players.map((player, index) => h(ControlPlayerEditor, {
            catalog,
            error: errors[`player-${index}`],
            index,
            key: index,
            mode: form.mode,
            onChange: (value) => updatePlayer(index, value),
            onRemove: () => removePlayer(index),
            player,
            removable: form.mode !== "series" && form.players.length > minimumPlayers,
          })),
        ),
      ),
      currentGame && currentGame.requiresJudgePanel ? h("section", {
        className: "panel form-section",
        "aria-labelledby": "judges-heading",
      },
      h("div", { className: "panel-head" },
        h("div", null,
          h("h2", { id: "judges-heading" }, "3. 创作裁判团"),
          h("span", { className: "panel-kicker" }, "3–9 位，固定后进入任务"),
        ),
        form.judges.length < 9 ? h("button", {
          className: "button small",
          onClick: () => changeForm((previous) => ({
            ...previous,
            judges: [...previous.judges, defaultControlJudge(previous.judges.length, catalog)],
          })),
          type: "button",
        }, "+ 添加裁判") : null,
      ),
      h("div", { className: "form-section-body roster-list" },
        errors.judges ? h("p", { className: "section-error", role: "alert" }, errors.judges) : null,
        ...form.judges.map((judge, index) => h(ControlJudgeEditor, {
          catalog,
          error: errors[`judge-${index}`],
          index,
          judge,
          key: index,
          onChange: (value) => updateJudge(index, value),
          onRemove: () => removeJudge(index),
        })),
      ),
      ) : null,
      h("section", { className: "panel form-section", "aria-labelledby": "limits-heading" },
        h("div", { className: "panel-head" }, h("h2", { id: "limits-heading" }, `${currentGame && currentGame.requiresJudgePanel ? "4" : "3"}. 限时与硬预算`)),
        h("div", { className: "form-section-body" },
          h("div", { className: "field-grid" },
            h(ControlField, { error: errors.timeoutSeconds, help: "只影响人类浏览器席位。", id: "control-human-timeout", label: "人类每步限时（秒）" }, (attributes) => h("input", {
              ...attributes,
              min: 1,
              onChange: (event) => changeForm({ timeoutSeconds: event.target.value }),
              step: "any",
              type: "number",
              value: form.timeoutSeconds,
            })),
            h(ControlField, { error: errors.llmTimeoutSeconds, id: "control-llm-timeout", label: "模型每步限时（秒）" }, (attributes) => h("input", {
              ...attributes,
              min: 1,
              onChange: (event) => changeForm({ llmTimeoutSeconds: event.target.value }),
              step: "any",
              type: "number",
              value: form.llmTimeoutSeconds,
            })),
          ),
          h("fieldset", { className: "budget-fieldset" },
            h("legend", null, "Provider 硬预算"),
            h("p", { className: "field-help" }, "留空表示该维度不设额外上限；单次输出上限必填。预估成本上限要求服务端为每条云端路由配置价格。"),
            h("div", { className: "budget-grid" },
              ...[
                ["maxProviderCalls", "最大调用数", "1", 0],
                ["maxInputTokens", "最大输入 Token", "1", 0],
                ["maxOutputTokensPerCall", "单次最大输出 Token", "1", 1],
                ["maxTotalOutputTokens", "累计最大输出 Token", "1", 0],
                ["maxEstimatedCostUsd", "最大预估成本（USD）", "0.000001", 0],
              ].map(([key, label, step, minimum]) => h(ControlField, {
                error: errors[`budget-${key}`],
                id: `control-budget-${key}`,
                key,
                label,
              }, (attributes) => h("input", {
                ...attributes,
                min: minimum,
                onChange: (event) => changeForm((previous) => ({
                  ...previous,
                  budget: { ...previous.budget, [key]: event.target.value },
                })),
                step,
                type: "number",
                value: form.budget[key],
              }))),
            ),
          ),
          form.mode === "round_robin" ? h("label", { className: "control-checkbox" },
            h("input", {
              checked: form.allowLargeTournament,
              onChange: (event) => changeForm({ allowLargeTournament: event.target.checked }),
              type: "checkbox",
            }),
            h("span", null,
              h("strong", null, "允许超过默认规模门槛的循环赛"),
              h("small", null, "仅解除规模保护；Provider 硬预算仍然生效。"),
            ),
          ) : null,
        ),
      ),
      prepareError ? h("p", { className: "form-submit-error", role: "alert" }, errorCopy(prepareError)) : null,
      h("div", { className: "form-actions sticky-actions" },
        h(AppLink, { className: "button", href: "/" }, "取消"),
        h("button", { className: "button primary", disabled: busy, type: "submit" }, busy ? "正在准备…" : "生成准备态预览"),
      ),
    ),
  );
}

function resumeControlRequest(job) {
  const championship = job.mode === "championship";
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
    mode: championship ? "championship" : "round_robin",
    players: [],
    resume_championship_id: championship ? job.championshipId : null,
    resume_tournament_id: championship ? null : job.tournamentId,
    rounds: null,
    seed: "0",
  };
}

function useControlJob(jobId, adminToken, onAuthLost, reloadKey) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  useEffect(() => {
    let cancelled = false;
    let controller = null;
    let timer = null;
    const schedule = () => {
      if (!cancelled && timer === null) timer = window.setTimeout(() => {
        timer = null;
        load(false);
      }, 1500);
    };
    const load = async (initial) => {
      if (cancelled || document.visibilityState === "hidden") return;
      if (controller) controller.abort();
      controller = new AbortController();
      if (initial) setState((previous) => ({ ...previous, loading: true }));
      try {
        const payload = await fetchControlJSON(
          `/api/v1/control/jobs/${encodeURIComponent(jobId)}`,
          adminToken,
          { signal: controller.signal },
        );
        const data = normalizeControlJob(payload);
        if (!cancelled) setState({ data, error: null, loading: false });
        if (CONTROL_ACTIVE_STATUSES.has(data.status)) schedule();
      } catch (error) {
        if (cancelled || error.name === "AbortError") return;
        if ([401, 403].includes(error.status)) onAuthLost();
        setState((previous) => ({ ...previous, error, loading: false }));
        if (![401, 403, 404].includes(error.status)) schedule();
      }
    };
    const onVisibility = () => {
      if (document.visibilityState !== "visible") return;
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
      load(false);
    };
    load(true);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      if (controller) controller.abort();
      if (timer !== null) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [adminToken, jobId, onAuthLost, reloadKey]);
  return state;
}

function JobDetailPage({ adminToken, jobId, onAuthLost }) {
  const [reloadKey, setReloadKey] = useState(0);
  const jobState = useControlJob(jobId, adminToken, onAuthLost, reloadKey);
  const [busyAction, setBusyAction] = useState(null);
  const [mutationError, setMutationError] = useState(null);
  const [cancelArmed, setCancelArmed] = useState(false);
  const [resumePrepared, setResumePrepared] = useState(null);
  const operationKeysRef = useRef(new Map());
  const resumePrepareKeyRef = useRef(null);
  const cancelDialogRef = useRef(null);
  const stopButtonRef = useRef(null);
  const restoreStopFocusRef = useRef(false);
  const job = jobState.data;

  useEffect(() => {
    const target = cancelArmed
      ? cancelDialogRef.current
      : restoreStopFocusRef.current
        ? stopButtonRef.current
        : null;
    restoreStopFocusRef.current = false;
    if (!target) return undefined;
    const frame = window.requestAnimationFrame(() => {
      target.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [cancelArmed]);

  const closeCancelConfirmation = () => {
    restoreStopFocusRef.current = true;
    setCancelArmed(false);
  };

  const operationKey = (targetJobId, action) => {
    const key = `${targetJobId}:${action}`;
    if (!operationKeysRef.current.has(key)) {
      operationKeysRef.current.set(key, createSubmissionId());
    }
    return operationKeysRef.current.get(key);
  };

  const mutate = async (targetJobId, action) => {
    setBusyAction(action);
    setMutationError(null);
    try {
      const payload = await fetchControlJSON(
        `/api/v1/control/jobs/${encodeURIComponent(targetJobId)}/${action}`,
        adminToken,
        { body: {}, idempotencyKey: operationKey(targetJobId, action), method: "POST" },
      );
      const result = normalizeControlJob(payload);
      setCancelArmed(false);
      if (action === "start" && targetJobId !== jobId) {
        navigate(`/jobs/${encodeURIComponent(result.jobId)}`);
      } else {
        setReloadKey((value) => value + 1);
      }
    } catch (error) {
      if ([401, 403].includes(error.status)) onAuthLost();
      setMutationError(error);
    } finally {
      setBusyAction(null);
    }
  };
  const prepareResume = async () => {
    if (!job || (job.mode === "round_robin" ? !job.tournamentId : !job.championshipId)) return;
    setBusyAction("resume");
    setMutationError(null);
    try {
      const payload = await fetchControlJSON("/api/v1/control/jobs", adminToken, {
        body: resumeControlRequest(job),
        idempotencyKey: resumePrepareKeyRef.current || (resumePrepareKeyRef.current = createSubmissionId()),
        method: "POST",
      });
      setResumePrepared(normalizeControlJob(payload));
    } catch (error) {
      if ([401, 403].includes(error.status)) onAuthLost();
      setMutationError(error);
    } finally {
      setBusyAction(null);
    }
  };
  const discardResume = async () => {
    if (!resumePrepared) return;
    setBusyAction("discard-resume");
    setMutationError(null);
    try {
      await fetchControlJSON(
        `/api/v1/control/jobs/${encodeURIComponent(resumePrepared.jobId)}/cancel`,
        adminToken,
        {
          body: {},
          idempotencyKey: operationKey(resumePrepared.jobId, "cancel"),
          method: "POST",
        },
      );
      setResumePrepared(null);
      resumePrepareKeyRef.current = null;
    } catch (error) {
      if ([401, 403].includes(error.status)) onAuthLost();
      setMutationError(error);
    } finally {
      setBusyAction(null);
    }
  };

  if (jobState.loading && !job) {
    return h("main", { className: "page control-page", id: "main-content", tabIndex: -1 },
      h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回大厅"),
      h("section", { className: "panel" }, h("div", { className: "panel-body" }, h(LoadingRows, { count: 4 }))),
    );
  }
  if (!job) {
    return h("main", { className: "page control-page", id: "main-content", tabIndex: -1 },
      h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回大厅"),
      h(StateCard, {
        action: jobState.error && [401, 403, 404].includes(jobState.error.status)
          ? null
          : h("button", { className: "button", onClick: () => setReloadKey((value) => value + 1), type: "button" }, "重试"),
        copy: errorCopy(jobState.error),
        error: true,
        title: "无法打开任务",
      }),
    );
  }
  if (resumePrepared) {
    return h("main", { className: "page control-page", id: "main-content", tabIndex: -1 },
      h("button", { className: "breadcrumb breadcrumb-button", onClick: discardResume, type: "button" }, "← 取消恢复预览并返回原任务"),
      h("section", { className: "control-hero", "aria-labelledby": "resume-title" },
        h("p", { className: "eyebrow" }, `${job.mode === "championship" ? "CHAMPIONSHIP" : "ROUND-ROBIN"} RESUME · REVIEW`),
        h("h1", { className: "detail-title", id: "resume-title" }, "核对恢复任务"),
        h("p", { className: "hero-copy" }, `服务端会从已存储的${job.mode === "championship" ? "淘汰锦标赛" : "循环赛"} checkpoint 恢复冻结配置；此时仍未启动。`),
      ),
      h(PreparedJobPreview, {
        busy: busyAction !== null,
        error: mutationError,
        job: resumePrepared,
        backLabel: "取消恢复预览",
        onBack: discardResume,
        onStart: () => mutate(resumePrepared.jobId, "start"),
      }),
    );
  }

  const active = CONTROL_ACTIVE_STATUSES.has(job.status);
  const canStop = CONTROL_STOPPABLE_STATUSES.has(job.status);
  const canResume = controlJobCanResume(job);
  const progressTotal = job.total !== null ? job.total : job.estimatedMatchCount;
  const progressCurrent = job.current !== null
    ? job.current
    : job.status === "completed" && progressTotal !== null
      ? progressTotal
      : Math.min(job.finalMatchIds.length, progressTotal === null ? job.finalMatchIds.length : progressTotal);
  const facts = [
    ["任务编号", job.jobId],
    ["模式", modeLabel(job.mode)],
    ["项目", job.game ? gameLabel(job.game) : "—"],
    ["参赛者", job.players.length ? job.players.join("、") : "—"],
    ["裁判", job.judges.length ? job.judges.join("、") : "无"],
    ["回合数", job.rounds === null ? "项目默认 / 不适用" : String(job.rounds)],
    ["随机种子", job.seed || "—"],
    ["人类席位限时", job.humanTimeoutSeconds === null ? "未设置" : `${formatScore(job.humanTimeoutSeconds)} 秒`],
    ["模型单步限时", job.llmTimeoutSeconds === null ? "未设置" : `${formatScore(job.llmTimeoutSeconds)} 秒`],
    ["已确认 Profile", job.preparedProfiles.length ? job.preparedProfiles.map((profile) => profile.label).join("；") : "无"],
    ["大规模循环赛", job.mode === "round_robin" ? job.largeTournamentAllowed ? "已明确允许" : "未允许" : "不适用"],
    ["准备时间", dateTime(job.createdAt, true)],
    ["启动时间", dateTime(job.startedAt, true)],
    ["结束时间", dateTime(job.finishedAt, true)],
  ];
  return h("main", { className: "page control-page", id: "main-content", tabIndex: -1 },
    h(AppLink, { className: "breadcrumb", href: "/" }, "← 返回大厅"),
    h("section", { className: "job-hero", "aria-labelledby": "job-title" },
      h("div", null,
        h("p", { className: "eyebrow" }, "LOCAL JOB"),
        h("h1", { className: "detail-title", id: "job-title" }, job.game ? gameLabel(job.game) : "比赛任务"),
        h("p", { className: "hero-copy" }, `${modeLabel(job.mode)} · ${job.players.length ? job.players.join(" 对 ") : job.jobId}`),
      ),
      h("div", { className: "job-stage", "aria-live": "polite" },
        h(JobStatus, { status: job.status }),
        h("span", null, active ? "页面会自动同步任务状态" : `最后更新 ${dateTime(job.updatedAt, true)}`),
      ),
    ),
    jobState.error ? h("div", { className: "participation-alert", role: "alert" },
      h("strong", null, "状态同步暂时中断"),
      h("span", null, errorCopy(jobState.error)),
      h("button", { className: "button small", onClick: () => setReloadKey((value) => value + 1), type: "button" }, "重试"),
    ) : null,
    mutationError ? h("p", { className: "form-submit-error", role: "alert" }, errorCopy(mutationError)) : null,
    job.status === "failed" || job.status === "interrupted" ? h("div", { className: "job-failure", role: "status" },
      h("strong", null, job.status === "failed" ? "任务没有完成" : "任务已中断"),
      h("p", null, job.errorCode ? errorCopy(new PublicError(job.errorCode)) : "查看本机服务日志可获取技术细节；页面不会显示敏感 Provider 错误正文。"),
    ) : null,
    h("div", { className: "job-layout" },
      h("section", { className: "panel", "aria-labelledby": "job-actions-heading" },
        h("div", { className: "panel-head" },
          h("h2", { id: "job-actions-heading" }, "任务进度与入口"),
          h("span", { className: "panel-kicker" }, controlStatusLabel(job.status)),
        ),
        h("div", { className: "job-action-body" },
          progressTotal !== null && progressTotal > 0 ? h("section", {
            className: "job-progress",
            "aria-labelledby": "job-progress-heading",
          },
          h("div", null,
            h("strong", { id: "job-progress-heading" }, "对局进度"),
            h("span", null, `${progressCurrent} / ${progressTotal}`),
          ),
          h("progress", { max: progressTotal, value: Math.min(progressCurrent, progressTotal) }, `${progressCurrent} / ${progressTotal}`),
          ) : null,
          job.status === "prepared" ? h("div", { className: "confirmation-box compact" },
            h("div", null,
              h("strong", null, "此任务已准备，但尚未启动"),
              h("p", null, "启动会调用选定 Provider 并写入本机存档。"),
            ),
            h("button", {
              className: "button coral",
              disabled: busyAction !== null,
              onClick: () => mutate(job.jobId, "start"),
              type: "button",
            }, busyAction === "start" ? "正在启动…" : "确认并启动"),
          ) : null,
          job.liveId ? h("div", { className: "job-link-group" },
            h("div", null, h("strong", null, "实时观战"), h("p", null, "公开事件流不包含管理凭证。")),
            h(AppLink, { className: "button primary", href: `/live/${encodeURIComponent(job.liveId)}` }, "打开实时观战"),
          ) : null,
          job.participationLinks.length ? h("section", { className: "seat-links", "aria-labelledby": "seat-links-heading" },
            h("div", null,
              h("strong", { id: "seat-links-heading" }, "人类浏览器席位"),
              h("p", null, "每个链接只授权一个席位，并在新标签页打开。不要转发给其他人。"),
            ),
            h("div", { className: "seat-link-list" }, ...job.participationLinks.map((link, index) => h("a", {
              className: "button",
              href: link.href,
              key: `${link.seatId}-${index}`,
              referrerPolicy: "no-referrer",
              rel: "noopener noreferrer",
              target: "_blank",
            }, `打开 ${link.label}`))),
          ) : null,
          job.status === "completed" && job.finalMatchIds.length ? h("section", { className: "job-link-group", "aria-labelledby": "archives-heading" },
            h("div", null,
              h("strong", { id: "archives-heading" }, "已完成存档"),
              h("p", null, `${job.finalMatchIds.length} 场对局可安全回放，并已纳入适用的 ELO 结果。`),
            ),
            h("div", { className: "archive-button-list" }, ...job.finalMatchIds.map((matchId, index) => h(AppLink, {
              className: "button primary",
              href: `/matches/${encodeURIComponent(matchId)}`,
              key: matchId,
            }, job.finalMatchIds.length === 1 ? "打开存档回放" : `第 ${index + 1} 场回放`))),
          ) : null,
          canResume ? h("div", { className: "job-link-group resume-callout" },
            h("div", null,
              h("strong", null, `可以从${job.mode === "championship" ? "淘汰锦标赛" : "循环赛"} checkpoint 恢复`),
              h("p", null, "先生成新的准备态任务，再次确认后才会继续未完成赛程。"),
            ),
            h("button", {
              className: "button primary",
              disabled: busyAction !== null,
              onClick: prepareResume,
              type: "button",
            }, busyAction === "resume" ? "正在准备恢复…" : "准备恢复"),
          ) : null,
          canStop && !cancelArmed ? h("button", {
            className: "button danger-outline",
            disabled: busyAction !== null,
            onClick: () => setCancelArmed(true),
            ref: stopButtonRef,
            type: "button",
          }, job.status === "prepared" ? "取消准备态任务" : "请求停止任务") : null,
          canStop && cancelArmed ? h("div", {
            className: "cancel-confirmation",
            role: "group",
            "aria-describedby": "cancel-copy",
            "aria-labelledby": "cancel-heading",
            ref: cancelDialogRef,
            tabIndex: -1,
          },
            h("div", null,
              h("strong", { id: "cancel-heading" }, "确认停止这项任务？"),
              h("p", { id: "cancel-copy" }, "正在进行的 Provider 请求可能会完成；服务会安全收尾并保留已完成结果。"),
            ),
            h("div", { className: "form-actions" },
              h("button", { className: "button", disabled: busyAction !== null, onClick: closeCancelConfirmation, type: "button" }, "返回"),
              h("button", {
                className: "button danger",
                disabled: busyAction !== null,
                onClick: () => mutate(job.jobId, "cancel"),
                type: "button",
              }, busyAction === "cancel" ? "正在请求停止…" : "确认停止"),
            ),
          ) : null,
          !job.liveId && job.status !== "completed" && job.status !== "prepared" && !canResume
            ? h("p", { className: "notice" }, active
              ? "任务正在建立比赛进程；实时观战与人类席位入口会在就绪后出现。"
              : "这个任务没有可用的公开入口或回放。")
            : null,
        ),
      ),
      h("aside", { "aria-label": "任务信息" },
        h("dl", { className: "facts" }, ...facts.map(([label, value]) => h("div", { className: "fact", key: label },
          h("dt", null, label),
          h("dd", null, value),
        ))),
        h("section", { className: "job-budget", "aria-labelledby": "job-budget-heading" },
          h("h2", { id: "job-budget-heading" }, "硬预算"),
          h(ControlBudgetFacts, { budget: job.budget, compact: true }),
        ),
        h("p", { className: "notice" }, "管理页只显示安全 DTO；Provider 密钥、环境变量、路由地址和原始错误正文不会进入浏览器。"),
      ),
    ),
  );
}

function Lobby({ adminToken, health, games, initialGame, onAuthLost, refreshAll }) {
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
        h("p", { className: "eyebrow" }, adminToken ? "LOCAL CONTROL + OBSERVER" : "LIVE + ARCHIVE OBSERVER"),
        h("h1", { id: "lobby-title" }, adminToken ? "从配置到回放，都在一个本机赛场。" : "每一场模型较量，都有迹可循。"),
        h("p", { className: "hero-copy" }, adminToken
          ? "准备并确认新的比赛任务，跟踪运行进度，再进入实时观战或已完成存档。管理凭证只保存在当前浏览器标签页。"
          : "只读观看本机正在运行的比赛，或浏览已经完成并存档的比赛、ELO 排名与事件回放。输入服务输出的完整管理链接后，才可创建比赛。"),
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
    adminToken ? h(ControlJobsPanel, { adminToken, onAuthLost }) : null,
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

const SIGNED_64_JSON_LIMIT = 2 ** 63;

function validSigned64JsonNumber(value) {
  // Public seeds are SQLite signed 64-bit integers. JSON numbers above
  // Number.MAX_SAFE_INTEGER lose low-bit precision in browsers, but remain
  // finite integers suitable for display and event validation.
  return Number.isInteger(value)
    && value >= -SIGNED_64_JSON_LIMIT
    && value <= SIGNED_64_JSON_LIMIT;
}

function validatePublicEvent(event, expectedSeq) {
  if (!(isObject(event)
    && Number.isInteger(event.seq)
    && event.seq === expectedSeq
    && Object.prototype.hasOwnProperty.call(EVENT_LABELS, event.type)
    && validTimestamp(event.timestamp)
    && (event.player === null || typeof event.player === "string")
    && isObject(event.data))) return false;
  const data = event.data;
  if (event.type === "match_started") {
    return exactKeys(data, ["game", "game_config", "players", "seed"])
      && typeof data.game === "string"
      && /^[a-z][a-z0-9_]{0,63}$/.test(data.game)
      && validSigned64JsonNumber(data.seed)
      && isObject(data.game_config)
      && Array.isArray(data.players)
      && data.players.length >= 2
      && data.players.every(validLivePlayer);
  }
  if (event.type === "turn_prompt") {
    return exactKeys(data, ["prompt"]) && typeof data.prompt === "string";
  }
  if (event.type === "move_received") {
    return exactKeys(data, ["move"]) && typeof data.move === "string";
  }
  if (event.type === "move_rejected") {
    return exactKeys(data, ["forfeit", "move", "reason", "reason_code", "technical_loss"])
      && (data.move === null || typeof data.move === "string")
      && (data.reason === null || typeof data.reason === "string")
      && (data.reason_code === null || typeof data.reason_code === "string")
      && typeof data.forfeit === "boolean"
      && typeof data.technical_loss === "boolean";
  }
  if (!exactKeys(data, [
    "forfeited_by",
    "judging",
    "reason_code",
    "scores",
    "termination",
  ])
    || !isObject(data.scores)
    || Object.values(data.scores).some((score) => typeof score !== "number" || !Number.isFinite(score))
    || typeof data.termination !== "string"
    || (data.reason_code !== null && typeof data.reason_code !== "string")
    || (data.forfeited_by !== null && typeof data.forfeited_by !== "string")) return false;
  return data.judging === null || (
    isObject(data.judging)
    && exactKeys(data.judging, ["panel_size", "quorum", "successful_judges"])
    && ["panel_size", "quorum", "successful_judges"].every(
      (key) => Number.isInteger(data.judging[key]) && data.judging[key] >= 0,
    )
  );
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

function validLivePlayer(value) {
  return typeof value === "string"
    && value.length > 0
    && Array.from(value).length <= 512
    && !/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/.test(value);
}

const CHAMPIONSHIP_CONTEXT_KEYS = [
  "round_number",
  "round_count",
  "round_pairing_number",
  "round_pairing_count",
  "pairing_number",
  "pairing_count",
  "leg_number",
];

function validateChampionshipContext(context, matchEvent = false) {
  const keys = matchEvent
    ? [...CHAMPIONSHIP_CONTEXT_KEYS, "match_event_seq"]
    : CHAMPIONSHIP_CONTEXT_KEYS;
  if (!isObject(context) || !exactKeys(context, keys)) return false;
  if (keys.some((key) => !Number.isInteger(context[key]) || context[key] < (key === "match_event_seq" ? 0 : 1))) {
    return false;
  }
  const playerCount = context.pairing_count + 1;
  if (
    ![4, 8, 16].includes(playerCount)
    || context.round_count !== Math.log2(playerCount)
    || context.round_number > context.round_count
    || context.round_pairing_count !== playerCount / (2 ** context.round_number)
    || context.round_pairing_number > context.round_pairing_count
    || context.pairing_number
      !== playerCount - (playerCount / (2 ** (context.round_number - 1)))
        + context.round_pairing_number
    || ![1, 2].includes(context.leg_number)
  ) return false;
  return true;
}

function championshipPairingPlayers(bracket, players, roundNumber, roundPairingNumber) {
  if (roundNumber === 1) {
    const offset = (roundPairingNumber - 1) * 2;
    return players.slice(offset, offset + 2);
  }
  const priorRound = bracket.pairings
    .filter((pairing) => pairing.round_number === roundNumber - 1)
    .sort((left, right) => left.round_pairing_number - right.round_pairing_number);
  const offset = (roundPairingNumber - 1) * 2;
  return priorRound.slice(offset, offset + 2).map((pairing) => pairing.winner);
}

function validateChampionshipPairing(pairing, bracket, players, expectedPairingNumber) {
  if (!isObject(pairing) || !exactKeys(pairing, [
    "match_ids",
    "pairing_number",
    "players",
    "round_number",
    "round_pairing_number",
    "series_id",
    "status",
    "winner",
  ])) return false;
  const context = {
    round_number: pairing.round_number,
    round_count: bracket.round_count,
    round_pairing_number: pairing.round_pairing_number,
    round_pairing_count: bracket.player_count / (2 ** pairing.round_number),
    pairing_number: pairing.pairing_number,
    pairing_count: bracket.pairing_count,
    leg_number: 2,
  };
  const expectedPlayers = championshipPairingPlayers(
    bracket,
    players,
    pairing.round_number,
    pairing.round_pairing_number,
  );
  return validateChampionshipContext(context)
    && pairing.pairing_number === expectedPairingNumber
    && Array.isArray(pairing.players)
    && pairing.players.length === 2
    && pairing.players.every(validLivePlayer)
    && new Set(pairing.players).size === 2
    && expectedPlayers.length === 2
    && pairing.players.every((player, index) => player === expectedPlayers[index])
    && pairing.players.includes(pairing.winner)
    && typeof pairing.series_id === "string"
    && SAFE_PUBLIC_ID.test(pairing.series_id)
    && Array.isArray(pairing.match_ids)
    && pairing.match_ids.length === 2
    && pairing.match_ids.every((matchId) => typeof matchId === "string" && SAFE_PUBLIC_ID.test(matchId))
    && new Set(pairing.match_ids).size === 2
    && ["provisional", "committed"].includes(pairing.status);
}

function validateChampionshipBracket(bracket, players) {
  if (
    !isObject(bracket)
    || !exactKeys(bracket, [
      "champion",
      "championship_id",
      "pairing_count",
      "pairings",
      "player_count",
      "round_count",
    ])
    || typeof bracket.championship_id !== "string"
    || !SAFE_PUBLIC_ID.test(bracket.championship_id)
    || !Array.isArray(players)
    || players.some((player) => !validLivePlayer(player))
    || new Set(players).size !== players.length
    || ![4, 8, 16].includes(bracket.player_count)
    || bracket.player_count !== players.length
    || bracket.round_count !== Math.log2(bracket.player_count)
    || bracket.pairing_count !== bracket.player_count - 1
    || !Array.isArray(bracket.pairings)
    || bracket.pairings.length > bracket.pairing_count
    || (bracket.champion !== null && !players.includes(bracket.champion))
  ) return false;

  const materialized = { ...bracket, pairings: [] };
  const seriesIds = new Set();
  const matchIds = new Set();
  let committedCount = 0;
  let provisionalSeen = false;
  for (let index = 0; index < bracket.pairings.length; index += 1) {
    const pairing = bracket.pairings[index];
    if (!validateChampionshipPairing(pairing, materialized, players, index + 1)) return false;
    if (seriesIds.has(pairing.series_id)
      || pairing.match_ids.some((matchId) => matchIds.has(matchId))) return false;
    seriesIds.add(pairing.series_id);
    pairing.match_ids.forEach((matchId) => matchIds.add(matchId));
    if (pairing.status === "committed") {
      if (provisionalSeen) return false;
      committedCount += 1;
    } else {
      provisionalSeen = true;
    }
    materialized.pairings.push(pairing);
  }

  const boundaries = [0];
  let boundary = 0;
  for (let round = 1; round <= bracket.round_count; round += 1) {
    boundary += bracket.player_count / (2 ** round);
    boundaries.push(boundary);
  }
  if (!boundaries.includes(committedCount)) return false;
  const provisional = bracket.pairings.slice(committedCount);
  const nextRound = boundaries.indexOf(committedCount) + 1;
  if (provisional.some((pairing) => pairing.round_number !== nextRound)) return false;
  if (bracket.champion !== null) {
    return committedCount === bracket.pairing_count
      && bracket.pairings.length === bracket.pairing_count
      && bracket.pairings.at(-1).winner === bracket.champion;
  }
  return true;
}

function validateLiveSummary(summary, liveId) {
  const allowedKeys = new Set([
    "championship_bracket",
    "event_count",
    "final_id",
    "final_kind",
    "final_match_ids",
    "game",
    "leg_number",
    "live_id",
    "mode",
    "pairing_count",
    "pairing_number",
    "players",
    "round_count",
    "round_number",
    "round_pairing_count",
    "round_pairing_number",
    "started_at",
    "status",
    "updated_at",
  ]);
  if (
    !isObject(summary)
    || Object.keys(summary).some((key) => !allowedKeys.has(key))
    || summary.live_id !== liveId
    || !CONTROL_MODES.includes(summary.mode)
    || !["running", "completed", "interrupted"].includes(summary.status)
    || typeof summary.game !== "string"
    || !/^[a-z][a-z0-9_]{0,63}$/.test(summary.game)
    || !Array.isArray(summary.players)
    || summary.players.length < 2
    || summary.players.length > MAX_CONTROL_PLAYERS
    || summary.players.some((player) => !validLivePlayer(player))
    || new Set(summary.players).size !== summary.players.length
    || !validTimestamp(summary.started_at)
    || !validTimestamp(summary.updated_at)
    || Date.parse(summary.updated_at) < Date.parse(summary.started_at)
    || !Number.isInteger(summary.event_count)
    || summary.event_count < 0
    || summary.event_count > 10000
  ) return false;
  const placement = [
    "pairing_number",
    "pairing_count",
    "leg_number",
    "round_number",
    "round_count",
    "round_pairing_number",
    "round_pairing_count",
  ];
  if (placement.some((key) => summary[key] !== null
    && summary[key] !== undefined
    && (!Number.isInteger(summary[key]) || summary[key] < 1))) return false;
  if (summary.pairing_number != null
    && (summary.pairing_count == null || summary.pairing_number > summary.pairing_count)) return false;

  const championshipPlacement = placement.slice(3);
  if (summary.mode === "play" && placement.some((key) => summary[key] != null)) return false;
  if (summary.mode === "series" && (
    summary.pairing_number != null
    || summary.pairing_count != null
    || championshipPlacement.some((key) => summary[key] != null)
  )) return false;
  if (summary.mode === "round_robin" && (
    !Number.isInteger(summary.pairing_count)
    || championshipPlacement.some((key) => summary[key] != null)
  )) return false;
  if (summary.mode !== "championship" && summary.championship_bracket != null) return false;

  let bracket = null;
  if (summary.mode === "championship") {
    const context = Object.fromEntries(CHAMPIONSHIP_CONTEXT_KEYS.map((key) => [key, summary[key]]));
    bracket = summary.championship_bracket;
    if (
      !validateChampionshipContext(context)
      || !validateChampionshipBracket(bracket, summary.players)
      || bracket.player_count !== summary.players.length
      || ![bracket.pairings.length, bracket.pairings.length + 1].includes(summary.pairing_number)
    ) return false;
    if (summary.pairing_number === bracket.pairings.length) {
      const latest = bracket.pairings.at(-1);
      if (!latest
        || summary.leg_number !== 2
        || summary.round_number !== latest.round_number
        || summary.round_pairing_number !== latest.round_pairing_number) return false;
    } else if (bracket.pairings.length
      && bracket.pairings.at(-1).status === "provisional"
      && (summary.round_number !== bracket.pairings.at(-1).round_number
        || summary.round_pairing_number !== bracket.pairings.at(-1).round_pairing_number + 1)) {
      return false;
    }
    if (summary.status !== "completed" && bracket.champion !== null) return false;
  }

  const expectedFinalKind = {
    championship: "championship",
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
    || !SAFE_PUBLIC_ID.test(summary.final_id)
    || !Array.isArray(ids)
    || !ids.length
    || ids.some((id) => typeof id !== "string" || !SAFE_PUBLIC_ID.test(id))
    || new Set(ids).size !== ids.length
  ) return false;
  if (summary.mode === "play") return ids.length === 1 && ids[0] === summary.final_id;
  if (summary.mode === "series") return ids.length === 2;
  if (summary.mode === "round_robin") return ids.length === summary.pairing_count * 2;
  const expectedIds = bracket.pairings.flatMap((pairing) => pairing.match_ids);
  return summary.round_number === summary.round_count
    && summary.round_pairing_number === 1
    && summary.round_pairing_count === 1
    && summary.pairing_number === summary.pairing_count
    && summary.leg_number === 2
    && summary.final_id === bracket.championship_id
    && bracket.champion !== null
    && bracket.pairings.length === bracket.pairing_count
    && bracket.pairings.every((pairing) => pairing.status === "committed")
    && JSON.stringify(ids) === JSON.stringify(expectedIds);
}

function validateChampionshipLifecycleItem(item, summary) {
  if (!summary || summary.mode !== "championship" || !validateChampionshipContext(item.context)) {
    return false;
  }
  const bracket = summary.championship_bracket;
  const context = item.context;
  if (
    context.round_count !== bracket.round_count
    || context.pairing_count !== bracket.pairing_count
    || context.round_pairing_count !== bracket.player_count / (2 ** context.round_number)
  ) return false;
  if (item.kind === "pairing_completed") {
    if (!exactKeys(item, ["context", "kind", "pairing", "seq"])
      || context.leg_number !== 2
      || !isObject(item.pairing)
      || item.pairing.status !== "provisional"
      || item.pairing.round_number !== context.round_number
      || item.pairing.round_pairing_number !== context.round_pairing_number
      || item.pairing.pairing_number !== context.pairing_number) return false;
    const existing = bracket.pairings[context.pairing_number - 1];
    if (existing) {
      return JSON.stringify({ ...existing, status: "provisional" })
        === JSON.stringify(item.pairing);
    }
    if (context.pairing_number !== bracket.pairings.length + 1) return false;
    const candidate = {
      ...bracket,
      champion: null,
      pairings: [...bracket.pairings, item.pairing],
    };
    return validateChampionshipBracket(candidate, summary.players);
  }
  if (item.kind === "round_committed") {
    if (!exactKeys(item, ["context", "kind", "pairing_numbers", "seq"])
      || !Array.isArray(item.pairing_numbers)
      || context.leg_number !== 2
      || context.round_pairing_number !== context.round_pairing_count) return false;
    const first = context.pairing_number - context.round_pairing_number + 1;
    const expected = Array.from(
      { length: context.round_pairing_count },
      (_value, index) => first + index,
    );
    if (JSON.stringify(item.pairing_numbers) !== JSON.stringify(expected)) return false;
    return item.pairing_numbers.every((number) => {
      const pairing = bracket.pairings[number - 1];
      return pairing && pairing.round_number === context.round_number;
    });
  }
  return false;
}

function validateLiveItem(item, expectedSeq, summary = null) {
  if (!isObject(item) || item.seq !== expectedSeq) return false;
  const kind = item.kind === undefined ? "match_event" : item.kind;
  if (kind === "pairing_completed" || kind === "round_committed") {
    return validateChampionshipLifecycleItem(item, summary);
  }
  if (kind !== "match_event"
    || !exactKeys(item, item.kind === undefined
      ? ["context", "event", "seq"]
      : ["context", "event", "kind", "seq"])
    || !isObject(item.context)) return false;

  if (summary && summary.mode === "championship") {
    if (!validateChampionshipContext(item.context, true)) return false;
    const expectedPlayers = championshipPairingPlayers(
      summary.championship_bracket,
      summary.players,
      item.context.round_number,
      item.context.round_pairing_number,
    );
    const orderedPlayers = item.context.leg_number === 2
      ? expectedPlayers.slice().reverse()
      : expectedPlayers;
    const finishedPlayers = item.event.type === "match_finished"
      ? Object.keys(item.event.data.scores || {})
      : [];
    if (expectedPlayers.length !== 2
      || (item.event.type === "match_started"
        && (item.event.data.game !== summary.game
          || JSON.stringify(item.event.data.players) !== JSON.stringify(orderedPlayers)))
      || (item.event.type === "match_finished"
        && (finishedPlayers.length !== expectedPlayers.length
          || finishedPlayers.some((player) => !expectedPlayers.includes(player))
          || (item.event.data.forfeited_by !== null
            && !expectedPlayers.includes(item.event.data.forfeited_by))))
      || (item.event.player !== null && !expectedPlayers.includes(item.event.player))) return false;
  } else {
    const allowedContext = new Set(["leg_number", "match_event_seq", "pairing_number"]);
    if (
      Object.keys(item.context).some((key) => !allowedContext.has(key))
      || !Number.isInteger(item.context.match_event_seq)
      || item.context.match_event_seq < 0
      || (item.context.leg_number != null
        && (!Number.isInteger(item.context.leg_number) || ![1, 2].includes(item.context.leg_number)))
      || (item.context.pairing_number != null
        && (!Number.isInteger(item.context.pairing_number) || item.context.pairing_number < 1))
      || (item.context.pairing_number != null && item.context.leg_number == null)
    ) return false;
    if (summary && (
      (summary.mode === "play"
        && (item.context.pairing_number != null || item.context.leg_number != null))
      || (summary.mode === "series" && item.context.pairing_number != null)
      || (summary.mode === "round_robin"
        && (item.context.pairing_number == null || item.context.leg_number == null))
    )) return false;
  }
  return validatePublicEvent(item.event, item.context.match_event_seq);
}

function publicEventFromLiveItem(item) {
  if ((item.kind || "match_event") !== "match_event") return null;
  return {
    ...item.event,
    context: item.context,
    match_event_seq: item.context.match_event_seq,
    seq: item.seq,
  };
}

function cloneChampionshipBracket(bracket) {
  return {
    ...bracket,
    pairings: bracket.pairings.map((pairing) => ({
      ...pairing,
      match_ids: pairing.match_ids.slice(),
      players: pairing.players.slice(),
    })),
  };
}

function championshipReducer(state, action) {
  if (action.type === "reset") return null;
  if (action.type === "summary") {
    const summary = action.summary;
    if (!validateLiveSummary(summary, summary && summary.live_id)
      || summary.mode !== "championship") throw new PublicError("protocol_error");
    return {
      bracket: cloneChampionshipBracket(summary.championship_bracket),
      contextFromSeq: summary.event_count,
      items: state ? state.items.slice() : [],
      lifecycleItems: state ? state.lifecycleItems.slice() : [],
      summary,
    };
  }
  if (action.type === "terminal") {
    if (!state || !["completed", "interrupted"].includes(action.status)) {
      throw new PublicError("protocol_error");
    }
    let bracket = cloneChampionshipBracket(state.bracket);
    if (action.status === "completed") {
      if (bracket.pairings.length !== bracket.pairing_count
        || !bracket.pairings.every((pairing) => pairing.status === "committed")) {
        throw new PublicError("protocol_error");
      }
      bracket.champion = bracket.pairings.at(-1).winner;
    }
    const summary = {
      ...state.summary,
      championship_bracket: bracket,
      event_count: action.eventCount,
      final_id: action.status === "completed" ? action.finalId : null,
      final_kind: action.status === "completed" ? action.finalKind : null,
      final_match_ids: action.status === "completed" && Array.isArray(action.finalMatchIds)
        ? action.finalMatchIds.slice()
        : [],
      status: action.status,
    };
    if (!validateLiveSummary(summary, summary.live_id)) {
      throw new PublicError("protocol_error");
    }
    return {
      ...state,
      bracket,
      contextFromSeq: summary.event_count,
      summary,
    };
  }
  if (action.type !== "item") throw new PublicError("protocol_error");
  let current = state;
  if (action.summary) current = championshipReducer(current, { type: "summary", summary: action.summary });
  if (!current) throw new PublicError("protocol_error");
  const item = action.item;
  const prior = current.items.find((candidate) => candidate.seq === item.seq);
  if (prior) {
    if (JSON.stringify(prior) !== JSON.stringify(item)) throw new PublicError("protocol_error");
    return current;
  }
  const expectedSeq = action.summary || !current.items.length
    ? item.seq
    : current.items.at(-1).seq + 1;
  const summary = {
    ...current.summary,
    championship_bracket: current.bracket,
  };
  if (!validateLiveItem(item, expectedSeq, summary)) throw new PublicError("protocol_error");
  const context = item.context;
  let bracket = cloneChampionshipBracket(current.bracket);
  if (item.kind === "pairing_completed") {
    if (context.pairing_number === bracket.pairings.length + 1) {
      bracket.pairings.push({
        ...item.pairing,
        match_ids: item.pairing.match_ids.slice(),
        players: item.pairing.players.slice(),
      });
    }
  } else if (item.kind === "round_committed") {
    const committed = new Set(item.pairing_numbers);
    bracket.pairings = bracket.pairings.map((pairing) => (
      committed.has(pairing.pairing_number)
        ? { ...pairing, status: "committed" }
        : pairing
    ));
  }
  const nextSummary = item.seq >= current.contextFromSeq
    ? {
      ...summary,
      ...Object.fromEntries(CHAMPIONSHIP_CONTEXT_KEYS.map((key) => [key, context[key]])),
      championship_bracket: bracket,
    }
    : { ...summary, championship_bracket: bracket };
  if (!validateChampionshipBracket(bracket, summary.players)) {
    throw new PublicError("protocol_error");
  }
  return {
    bracket,
    contextFromSeq: current.contextFromSeq,
    items: [...current.items, item],
    lifecycleItems: ["pairing_completed", "round_committed"].includes(item.kind)
      ? [...current.lifecycleItems, item]
      : current.lifecycleItems.slice(),
    summary: nextSummary,
  };
}

function liveWebsocketURL(liveId, fromSeq) {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws/v1/live/${encodeURIComponent(liveId)}?from_seq=${fromSeq}`;
}

function useLiveStream(liveId, reloadKey) {
  const [state, setState] = useState({
    championship: null,
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
    let streamItems = new Map();
    let championship = null;
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
        championship,
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
      championship = candidate.mode === "championship"
        ? championshipReducer(championship, { summary: candidate, type: "summary" })
        : null;
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
          const prior = streamItems.get(item.seq);
          if (JSON.stringify(prior) !== JSON.stringify(item)) {
            throw new PublicError("protocol_error");
          }
          return;
        }
        if (!validateLiveItem(item, nextSeq, summary)) throw new PublicError("protocol_error");
        streamItems.set(item.seq, item);
        if (summary.mode === "championship") {
          championship = championshipReducer(championship, { item, type: "item" });
          summary = championship.summary;
        }
        const publicEvent = publicEventFromLiveItem(item);
        if (publicEvent) events.push(publicEvent);
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
            let completedSummary;
            if (summary.mode === "championship") {
              championship = championshipReducer(championship, {
                eventCount: envelope.event_count,
                finalId: envelope.final_id,
                finalKind: envelope.final_kind,
                finalMatchIds: envelope.final_match_ids,
                status: "completed",
                type: "terminal",
              });
              completedSummary = championship.summary;
            } else {
              completedSummary = {
                ...summary,
                event_count: envelope.event_count,
                final_id: envelope.final_id,
                final_kind: envelope.final_kind,
                final_match_ids: envelope.final_match_ids,
                status: "completed",
              };
            }
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
            if (summary) {
              if (summary.mode === "championship") {
                championship = championshipReducer(championship, {
                  eventCount: envelope.event_count,
                  status: "interrupted",
                  type: "terminal",
                });
                summary = championship.summary;
              } else {
                summary = {
                  ...summary,
                  event_count: envelope.event_count,
                  status: "interrupted",
                };
                if (!validateLiveSummary(summary, liveId)) {
                  throw new PublicError("protocol_error");
                }
              }
            }
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
      championship: null,
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
            event.context && (event.context.round_number || event.context.pairing_number || event.context.leg_number)
              ? h("span", { className: "event-context" },
                event.context.round_number ? ` · 第 ${event.context.round_number} 轮` : "",
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

function ChampionshipBracket({ championship, completed = false }) {
  if (!championship) return null;
  const { bracket, summary } = championship;
  const currentPairing = summary.status === "running"
    && summary.pairing_number === bracket.pairings.length + 1
    ? summary.pairing_number
    : null;
  const rounds = Array.from({ length: bracket.round_count }, (_value, index) => index + 1);
  const roundLabel = (round) => round === bracket.round_count
    ? "决赛"
    : round === bracket.round_count - 1
      ? "半决赛"
      : `第 ${round} 轮`;

  const pairingCard = (pairing, globalNumber, round, roundPairingNumber) => {
    const current = globalNumber === currentPairing;
    if (!pairing && !current) {
      return h("div", {
        "aria-label": `${roundLabel(round)}第 ${roundPairingNumber} 组，等待晋级`,
        className: "championship-slot",
        key: globalNumber,
      }, h("span", null, "等待晋级"));
    }
    const players = pairing
      ? pairing.players
      : championshipPairingPlayers(bracket, summary.players, round, roundPairingNumber);
    const status = current ? "current" : pairing.status;
    const statusCopy = status === "current"
      ? "当前对阵"
      : status === "committed"
        ? "已写入整轮 checkpoint"
        : "结果待整轮提交";
    return h("article", {
      "aria-label": `${roundLabel(round)}第 ${roundPairingNumber} 组，${statusCopy}`,
      className: `championship-pairing${current ? " championship-current" : ""}`,
      "data-status": status,
      key: globalNumber,
    },
    h("div", { className: "championship-pairing-head" },
      h("span", null, current ? "当前对阵" : `对阵 ${globalNumber}`),
      h("strong", null, statusCopy),
    ),
    h("ol", { className: "championship-players" }, ...players.map((player) => h("li", {
      className: pairing && pairing.winner === player ? "winner" : undefined,
      key: player,
    },
    h("span", null, player),
    pairing && pairing.winner === player
      ? h("strong", { "aria-label": `${player} 晋级` }, "晋级")
      : null,
    ))),
    pairing && completed ? h("div", {
      "aria-label": `对阵 ${globalNumber} 存档`,
      className: "championship-archives",
    }, ...pairing.match_ids.map((matchId, index) => h(AppLink, {
      className: "text-link",
      href: `/matches/${encodeURIComponent(matchId)}`,
      key: matchId,
    }, `第 ${index + 1} 局`))) : null,
    );
  };

  return h("section", {
    className: "panel championship-bracket",
    "aria-labelledby": "championship-bracket-heading",
  },
  h("div", { className: "panel-head" },
    h("div", null,
      h("p", { className: "panel-overline" }, "AUTHORITATIVE BRACKET"),
      h("h2", { id: "championship-bracket-heading" }, "淘汰赛对阵"),
    ),
    h("span", { className: "panel-kicker" }, `${bracket.player_count} 人 · ${bracket.round_count} 轮`),
  ),
  bracket.champion ? h("div", {
    "aria-live": "polite",
    className: "championship-champion",
  },
  h("span", { "aria-hidden": "true" }, "★"),
  h("div", null, h("small", null, "冠军"), h("strong", null, bracket.champion)),
  ) : null,
  h("div", { className: "championship-rounds", role: "list" }, ...rounds.map((round) => {
    const roundPairingCount = bracket.player_count / (2 ** round);
    const firstPairing = bracket.player_count - (bracket.player_count / (2 ** (round - 1))) + 1;
    return h("section", {
      "aria-labelledby": `championship-round-${round}`,
      className: "championship-round",
      key: round,
      role: "listitem",
    },
    h("h3", { id: `championship-round-${round}` }, roundLabel(round)),
    h("div", { className: "championship-round-pairings" },
      ...Array.from({ length: roundPairingCount }, (_value, index) => {
        const globalNumber = firstPairing + index;
        return pairingCard(
          bracket.pairings[globalNumber - 1],
          globalNumber,
          round,
          index + 1,
        );
      }),
    ),
    );
  })),
  h("p", { className: "championship-note" },
    "“待整轮提交”来自已完成的双局对阵；只有 CLI worker 原子写入整轮 checkpoint 后，状态才会变为已提交。",
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
  if (summary.mode === "championship") {
    facts.splice(3, 0, ["当前轮次", `${summary.round_number}/${summary.round_count}`]);
  }
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
  const summary = stream.championship ? stream.championship.summary : stream.summary;

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
        h("h1", { className: "detail-title", id: "live-detail-title" }, summary.mode === "championship"
          ? `${summary.players.length} 位选手淘汰锦标赛`
          : summary.players.join(" 对 ")),
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
    h(ChampionshipBracket, {
      championship: stream.championship,
      completed: stream.phase === "completed",
    }),
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
        h("p", { className: "hero-copy" }, `${snapshot.players.length === 2
          ? snapshot.players.join(" 对 ")
          : `参赛者：${snapshot.players.join("、")}`}。这个 capability 只可读取并提交当前席位的请求；对局公开事件仍可能出现在本机观战页。`),
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
  if (window.location.pathname === "/new") return { name: "new-job" };
  const participation = participationPath(window.location.pathname);
  if (participation) return { name: "participation", ...participation };
  const match = window.location.pathname.match(/^\/matches\/([^/]+)$/);
  const live = window.location.pathname.match(/^\/live\/([^/]+)$/);
  const job = window.location.pathname.match(/^\/jobs\/([^/]+)$/);
  if (!match && !live && !job) return { name: "not-found" };
  try {
    const id = decodeURIComponent((match || live || job)[1]);
    return /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(id)
      ? match
        ? { name: "match", matchId: id }
        : live
          ? { name: "live", liveId: id }
          : { name: "job", jobId: id }
      : { name: "not-found" };
  } catch (_error) {
    return { name: "not-found" };
  }
}

captureAdminToken();
captureParticipationCapability();

function App() {
  const [route, setRoute] = useState(parseRoute);
  const [adminToken, setAdminToken] = useState(readAdminToken);
  const [health, setHealth] = useState(null);
  const [games, setGames] = useState([]);
  const [metaRefresh, setMetaRefresh] = useState(0);
  const firstRoute = useRef(true);
  const onAuthLost = useCallback(() => {
    clearAdminToken();
    setAdminToken(null);
  }, []);

  useEffect(() => {
    const onPopState = () => {
      setAdminToken(captureAdminToken());
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
          : route.name === "new-job"
            ? "新建比赛 · LLM Olympics"
            : route.name === "job"
              ? "任务进度 · LLM Olympics"
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
  }, [route.game, route.jobId, route.liveId, route.matchId, route.name, route.seatId, route.sessionId]);

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
      adminToken,
      games,
      health,
      initialGame: route.game,
      onAuthLost,
      refreshAll: () => setMetaRefresh((value) => value + 1),
    });
  } else if (route.name === "new-job") {
    content = adminToken
      ? h(NewJobPage, { adminToken, onAuthLost })
      : h("main", { className: "page", id: "main-content", tabIndex: -1 }, h(StateCard, {
        action: h(AppLink, { className: "button primary", href: "/" }, "返回观战大厅"),
        copy: "请使用本机 Web 服务输出的完整管理链接。管理凭证不会显示在页面或地址栏中。",
        error: true,
        title: "缺少管理凭证",
      }));
  } else if (route.name === "job") {
    content = adminToken
      ? h(JobDetailPage, { adminToken, jobId: route.jobId, key: route.jobId, onAuthLost })
      : h("main", { className: "page", id: "main-content", tabIndex: -1 }, h(StateCard, {
        action: h(AppLink, { className: "button primary", href: "/" }, "返回观战大厅"),
        copy: "任务详情需要当前标签页中的管理凭证。请重新使用服务输出的完整管理链接。",
        error: true,
        title: "无法打开管理任务",
      }));
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
        : route.name === "new-job"
          ? "已打开新建比赛向导"
          : route.name === "job"
            ? "已打开任务进度"
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
    h(Header, { controlEnabled: Boolean(adminToken), health, participation: route.name === "participation" }),
    content,
    h("div", { className: "live-region", "aria-atomic": "true", "aria-live": "polite" }, `${routeAnnouncement}${healthAnnouncement ? `。${healthAnnouncement}` : ""}`),
    h("footer", { className: "footer" }, route.name === "participation"
      ? "LLM Olympics · 本机人类输入 · 凭证仅保存在当前浏览器标签页"
      : adminToken
        ? "LLM Olympics · 本机比赛控制 · 管理凭证仅保存在当前浏览器标签页"
        : "LLM Olympics · 本机只读观战 · 实时事件与已完成存档"),
  );
}

if (globalThis.__LLMOLYMPIC_ENABLE_TEST_HOOKS__) {
  globalThis.__LLMOLYMPIC_OBSERVER_TEST__ = Object.freeze({
    captureAdminToken,
    championshipReducer,
    classifyReplayClose,
    clearAdminToken,
    controlJobCanResume,
    controlRequestBody,
    countCharacters,
    normalizeControlCatalog,
    normalizeControlJob,
    participationComponentKey,
    participationErrorClearsCapability,
    participationKeepsCapability,
    playbackReducer,
    remainingCopy,
    resumeControlRequest,
    validateChampionshipBracket,
    validateControlForm,
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
