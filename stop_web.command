#!/bin/bash
# LLM Olympics 本机 Web 控制台关闭器（macOS 双击运行）。

set -u

pause_on_error() {
    if [ -t 0 ]; then
        echo ""
        read -r -p "按回车关闭窗口..." _unused
    fi
}

fail() {
    echo "关闭失败：$1" >&2
    pause_on_error
    exit 1
}

if [ "$(/usr/bin/uname -s)" != "Darwin" ]; then
    fail "这个 .command 关闭器仅支持 macOS。"
fi

PROJECT_DIR="$(cd "$(/usr/bin/dirname "$0")" && pwd -P)" || exit 1
PROJECT_HASH="$(/usr/bin/printf '%s' "$PROJECT_DIR" | /usr/bin/shasum -a 256 | /usr/bin/cut -c1-12)" \
    || fail "无法生成项目服务标识。"
LABEL="com.llmolympic.web.$PROJECT_HASH"
SERVICE_TARGET="gui/$(/usr/bin/id -u)/$LABEL"
TEMP_ROOT="${TMPDIR:-/tmp}"
STATE_DIR="${TEMP_ROOT%/}/llmolympic-web-$PROJECT_HASH"
STDERR_LOG="$STATE_DIR/web.stderr.log"
ADMIN_TOKEN_FILE="$STATE_DIR/admin.token"

if ! /bin/launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
    if [ -f "$ADMIN_TOKEN_FILE" ]; then
        /bin/rm -f "$ADMIN_TOKEN_FILE" \
            || fail "服务已停止，但无法删除过期的本机管理凭证：$ADMIN_TOKEN_FILE"
    fi
    echo "LLM Olympics Web 服务已经停止（未发现由 start_web.command 管理的实例）。"
    if /usr/sbin/lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
        echo "注意：端口 8000 仍被其他进程占用，本关闭器不会停止它。"
    fi
    exit 0
fi

/bin/launchctl bootout "$SERVICE_TARGET" >/dev/null 2>&1 \
    || fail "launchd 无法卸载项目服务。日志：$STDERR_LOG"

attempts=0
while /bin/launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; do
    if [ "$attempts" -ge 20 ]; then
        fail "服务仍处于注册状态。日志：$STDERR_LOG"
    fi
    /bin/sleep 0.1
    attempts=$((attempts + 1))
done

if [ -f "$ADMIN_TOKEN_FILE" ]; then
    /bin/rm -f "$ADMIN_TOKEN_FILE" \
        || fail "服务已停止，但无法删除本机管理凭证：$ADMIN_TOKEN_FILE"
fi

echo "LLM Olympics Web 服务已关闭。"
echo "日志保留在：$STATE_DIR"
if /usr/sbin/lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "注意：端口 8000 仍被其他进程占用；该进程并非由本启动器管理。"
fi
