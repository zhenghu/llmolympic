#!/bin/bash
# LLM Olympics 本机 Web 控制台启动器（macOS 双击运行）。

set -u
umask 077

pause_on_error() {
    if [ -t 0 ]; then
        echo ""
        read -r -p "按回车关闭窗口..." _unused
    fi
}

fail() {
    echo "启动失败：$1" >&2
    pause_on_error
    exit 1
}

if [ "$(/usr/bin/uname -s)" != "Darwin" ]; then
    fail "这个 .command 启动器仅支持 macOS。"
fi

PROJECT_DIR="$(cd "$(/usr/bin/dirname "$0")" && pwd -P)" || exit 1
cd "$PROJECT_DIR" || fail "无法进入项目目录：$PROJECT_DIR"
PYTHON="$PROJECT_DIR/.venv/bin/python"
HOST="127.0.0.1"
PORT="8000"
BASE_URL="http://$HOST:$PORT"

if [ ! -x "$PYTHON" ]; then
    fail "未找到项目虚拟环境。请先在项目目录运行 python3 -m venv .venv 并安装依赖。"
fi

if ! "$PYTHON" -c "import fastapi, uvicorn, websockets" >/dev/null 2>&1; then
    fail "Web 依赖未安装。请在项目目录运行：.venv/bin/python -m pip install -e '.[dev,web]'"
fi

DATABASE="$("$PYTHON" -c 'from llmolympic.core.storage import database_path; print(database_path().resolve())')" \
    || fail "无法解析 SQLite 数据库路径。"
PROJECT_HASH="$(/usr/bin/printf '%s' "$PROJECT_DIR" | /usr/bin/shasum -a 256 | /usr/bin/cut -c1-12)" \
    || fail "无法生成项目服务标识。"
LABEL="com.llmolympic.web.$PROJECT_HASH"
SERVICE_TARGET="gui/$(/usr/bin/id -u)/$LABEL"
TEMP_ROOT="${TMPDIR:-/tmp}"
STATE_DIR="${TEMP_ROOT%/}/llmolympic-web-$PROJECT_HASH"
PLIST_PATH="$STATE_DIR/$LABEL.plist"
STDOUT_LOG="$STATE_DIR/web.stdout.log"
STDERR_LOG="$STATE_DIR/web.stderr.log"
ADMIN_TOKEN_FILE="$STATE_DIR/admin.token"

/bin/mkdir -p "$STATE_DIR" || fail "无法创建服务状态目录：$STATE_DIR"
/bin/chmod 700 "$STATE_DIR" || fail "无法保护服务状态目录。"

health_ready() {
    response="$(/usr/bin/curl --fail --silent --max-time 1 "$BASE_URL/api/v1/health" 2>/dev/null)" \
        || return 1
    case "$response" in
        *'"api_version":"v1"'*) return 0 ;;
        *) return 1 ;;
    esac
}

wait_for_health() {
    attempts=0
    while [ "$attempts" -lt 40 ]; do
        if health_ready; then
            return 0
        fi
        /bin/sleep 0.25
        attempts=$((attempts + 1))
    done
    return 1
}

open_console() {
    if [ ! -f "$ADMIN_TOKEN_FILE" ]; then
        fail "服务已运行，但没有生成管理凭证。请查看日志：$STDERR_LOG"
    fi
    IFS= read -r ADMIN_TOKEN < "$ADMIN_TOKEN_FILE" \
        || fail "无法读取本机管理凭证：$ADMIN_TOKEN_FILE"
    case "$ADMIN_TOKEN" in
        *[!A-Za-z0-9_-]*|'') fail "本机管理凭证格式无效；请重新启动服务。" ;;
    esac
    if [ "${#ADMIN_TOKEN}" -ne 43 ]; then
        fail "本机管理凭证长度无效；请重新启动服务。"
    fi
    ADMIN_URL="$BASE_URL/#admin=$ADMIN_TOKEN"
    /usr/bin/open "$ADMIN_URL" \
        || fail "服务已运行，但无法打开默认浏览器。请重新双击本启动器。"
    unset ADMIN_TOKEN ADMIN_URL
    echo "LLM Olympics Web 控制台已启动：$BASE_URL/"
    echo "数据库：$DATABASE"
    echo "关闭服务：双击 stop_web.command"
}

if /bin/launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
    if health_ready && [ -s "$ADMIN_TOKEN_FILE" ]; then
        echo "Web 服务已经在运行。"
        open_console
        exit 0
    fi
    echo "正在重新启动已注册的 Web 服务..."
    /bin/launchctl kickstart -k "$SERVICE_TARGET" >/dev/null 2>&1 \
        || fail "无法重新启动已注册的服务。日志：$STDERR_LOG"
    if wait_for_health; then
        open_console
        exit 0
    fi
    echo "最近的错误日志：" >&2
    /usr/bin/tail -n 30 "$STDERR_LOG" 2>/dev/null || true
    fail "服务未能通过健康检查。日志：$STDERR_LOG"
fi

if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "端口 $PORT 已被其他进程占用。本启动器不会接管或停止它；请先在原终端按 Ctrl-C。"
fi

export LLMOLYMPIC_WEB_LABEL="$LABEL"
export LLMOLYMPIC_WEB_PYTHON="$PYTHON"
export LLMOLYMPIC_WEB_PROJECT_DIR="$PROJECT_DIR"
export LLMOLYMPIC_WEB_DATABASE="$DATABASE"
export LLMOLYMPIC_WEB_HOST="$HOST"
export LLMOLYMPIC_WEB_PORT="$PORT"
export LLMOLYMPIC_WEB_STDOUT_LOG="$STDOUT_LOG"
export LLMOLYMPIC_WEB_STDERR_LOG="$STDERR_LOG"
export LLMOLYMPIC_WEB_PLIST="$PLIST_PATH"
export LLMOLYMPIC_WEB_ADMIN_TOKEN_FILE="$ADMIN_TOKEN_FILE"

"$PYTHON" - <<'PY' || fail "无法生成 launchd 服务配置。"
import os
import plistlib
from pathlib import Path

plist_path = Path(os.environ["LLMOLYMPIC_WEB_PLIST"])
temporary_path = plist_path.with_suffix(".plist.tmp")
payload = {
    "Label": os.environ["LLMOLYMPIC_WEB_LABEL"],
    "ProgramArguments": [
        os.environ["LLMOLYMPIC_WEB_PYTHON"],
        "-m",
        "llmolympic",
        "web",
        "--db",
        os.environ["LLMOLYMPIC_WEB_DATABASE"],
        "--host",
        os.environ["LLMOLYMPIC_WEB_HOST"],
        "--port",
        os.environ["LLMOLYMPIC_WEB_PORT"],
        "--control-token-file",
        os.environ["LLMOLYMPIC_WEB_ADMIN_TOKEN_FILE"],
    ],
    "WorkingDirectory": os.environ["LLMOLYMPIC_WEB_PROJECT_DIR"],
    "RunAtLoad": True,
    "KeepAlive": False,
    "ProcessType": "Interactive",
    "StandardOutPath": os.environ["LLMOLYMPIC_WEB_STDOUT_LOG"],
    "StandardErrorPath": os.environ["LLMOLYMPIC_WEB_STDERR_LOG"],
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
}
with temporary_path.open("wb") as stream:
    plistlib.dump(payload, stream, sort_keys=True)
temporary_path.chmod(0o600)
temporary_path.replace(plist_path)
PY

: > "$STDOUT_LOG"
: > "$STDERR_LOG"
: > "$ADMIN_TOKEN_FILE"
/bin/chmod 600 "$ADMIN_TOKEN_FILE" || fail "无法保护本机管理凭证文件。"

if ! /bin/launchctl bootstrap "gui/$(/usr/bin/id -u)" "$PLIST_PATH" >/dev/null 2>&1; then
    if ! /bin/launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
        fail "launchd 无法注册 Web 服务。日志：$STDERR_LOG"
    fi
fi

if wait_for_health; then
    open_console
    exit 0
fi

echo "最近的错误日志：" >&2
/usr/bin/tail -n 30 "$STDERR_LOG" 2>/dev/null || true
/bin/launchctl bootout "$SERVICE_TARGET" >/dev/null 2>&1 || true
: > "$ADMIN_TOKEN_FILE"
fail "服务未能通过健康检查，已安全卸载。日志：$STDERR_LOG"
