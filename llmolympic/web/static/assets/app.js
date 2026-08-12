"use strict";

(() => {
  const h = React.createElement;
  const {
    Fragment,
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
  } = React;

  const API_VERSION = "v1";
  const MAX_MATCHES = 100;
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
    match_not_found: "对局不存在，或已经不再可用。",
    network_error: "与本机观战服务的连接中断。",
    overloaded: "回放服务正忙，请稍后重试。",
    protocol_error: "页面与服务的回放协议不一致。",
    request_failed: "暂时无法加载数据，请稍后重试。",
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

  function Header({ health }) {
    return h(
      "header",
      { className: "topbar" },
      h(
        "div",
        { className: "topbar-inner" },
        h(
          AppLink,
          { className: "brand", href: "/", "aria-label": "LLM Olympics 观战台首页" },
          h("span", { className: "brand-mark", "aria-hidden": "true" }, "L²"),
          h(
            "span",
            null,
            h("span", { className: "brand-name" }, "LLM Olympics"),
            h("span", { className: "brand-sub" }, "ARCHIVE OBSERVER"),
          ),
        ),
        h(
          "div",
          { className: "status-cluster" },
          h("span", { className: "status-pill local" }, "本机 · 只读"),
          h(StatusPill, { health }),
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
          h("p", { className: "eyebrow" }, "COMPLETED MATCH ARCHIVE"),
          h("h1", { id: "lobby-title" }, "每一场模型较量，都有迹可循。"),
          h("p", { className: "hero-copy" }, "浏览本机已经完成并存档的比赛、ELO 排名与事件回放。这里不是运行中比赛的直播，也不会连接任何模型服务。"),
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
            h("strong", null, EVENT_LABELS[event.type]),
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

  function parseRoute() {
    if (window.location.pathname === "/") {
      const candidate = new URLSearchParams(window.location.search).get("game");
      const game = candidate && /^[a-z][a-z0-9_]{0,63}$/.test(candidate) ? candidate : "";
      return { name: "lobby", game };
    }
    const match = window.location.pathname.match(/^\/matches\/([^/]+)$/);
    if (!match) return { name: "not-found" };
    try {
      const id = decodeURIComponent(match[1]);
      return /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(id)
        ? { name: "match", matchId: id }
        : { name: "not-found" };
    } catch (_error) {
      return { name: "not-found" };
    }
  }

  function App() {
    const [route, setRoute] = useState(parseRoute);
    const [health, setHealth] = useState(null);
    const [games, setGames] = useState([]);
    const [metaRefresh, setMetaRefresh] = useState(0);
    const firstRoute = useRef(true);

    useEffect(() => {
      const onPopState = () => setRoute(parseRoute());
      window.addEventListener("popstate", onPopState);
      return () => window.removeEventListener("popstate", onPopState);
    }, []);

    useEffect(() => {
      document.title = route.name === "lobby"
        ? "LLM Olympics · 观战台"
        : route.name === "match"
          ? "对局回放 · LLM Olympics"
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
    }, [route.game, route.name, route.matchId]);

    useEffect(() => {
      const controller = new AbortController();
      fetchJSON("/api/v1/health", controller.signal)
        .then(setHealth)
        .catch(() => setHealth({ status: "degraded", database_available: false }));
      fetchJSON("/api/v1/games", controller.signal)
        .then((payload) => setGames(Array.isArray(payload.games) ? payload.games : []))
        .catch(() => setGames([]));
      return () => controller.abort();
    }, [metaRefresh]);

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
    } else {
      content = h("main", { className: "page", id: "main-content", tabIndex: -1 },
        h(StateCard, {
          title: "没有这个页面",
          copy: "返回观战大厅，选择一场已经存档的比赛。",
          action: h(AppLink, { className: "button primary", href: "/" }, "返回首页"),
        }),
      );
    }

    const routeAnnouncement = route.name === "lobby"
      ? `已打开观战大厅${route.game ? `，筛选${gameLabel(route.game)}` : ""}`
      : route.name === "match"
        ? "已打开对局回放"
        : "页面不存在";
    const healthAnnouncement = health
      ? (health.status === "ok" ? "数据库可用" : "数据库不可用")
      : "正在检查数据库";

    return h(
      "div",
      { className: "site-shell" },
      h(Header, { health }),
      content,
      h("div", { className: "live-region", "aria-atomic": "true", "aria-live": "polite" }, `${routeAnnouncement}。${healthAnnouncement}`),
      h("footer", { className: "footer" }, "LLM Olympics · 本机只读观战 · 已完成存档回放"),
    );
  }

  if (globalThis.__LLMOLYMPIC_ENABLE_TEST_HOOKS__) {
    globalThis.__LLMOLYMPIC_OBSERVER_TEST__ = Object.freeze({
      classifyReplayClose,
      playbackReducer,
    });
  }

  ReactDOM.createRoot(document.getElementById("root")).render(h(App));
})();
