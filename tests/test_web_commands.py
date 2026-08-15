"""macOS Web 双击启动/关闭脚本的静态安全回归测试。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMAND_FILES = ("start_web.command", "stop_web.command")


@pytest.mark.parametrize("filename", COMMAND_FILES)
def test_web_command_is_executable_and_valid_bash(filename: str) -> None:
    path = PROJECT_ROOT / filename

    assert path.stat().st_mode & 0o100
    completed = subprocess.run(  # noqa: S603
        ["/bin/bash", "-n", str(path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_start_web_command_uses_hardened_project_launchd_service() -> None:
    launcher = (PROJECT_ROOT / "start_web.command").read_text()

    assert 'HOST="127.0.0.1"' in launcher
    assert 'PORT="8000"' in launcher
    assert 'BASE_URL="http://$HOST:$PORT"' in launcher
    assert '"$BASE_URL/api/v1/health"' in launcher
    assert '"-m",\n        "llmolympic",\n        "web",' in launcher
    assert '"--db",' in launcher
    assert '"--host",' in launcher
    assert '"--port",' in launcher
    assert '"--control-token-file",' in launcher
    assert '"LLMOLYMPIC_WEB_ADMIN_TOKEN_FILE"' in launcher
    assert '"RunAtLoad": True' in launcher
    assert '"KeepAlive": False' in launcher
    assert '"WorkingDirectory"' in launcher
    assert '"StandardOutPath"' in launcher
    assert '"StandardErrorPath"' in launcher
    assert "/bin/launchctl bootstrap" in launcher
    assert "/bin/launchctl kickstart -k \"$SERVICE_TARGET\"" in launcher
    assert "/api/v1/health" in launcher
    assert '/usr/bin/open "$ADMIN_URL"' in launcher
    assert '#admin=$ADMIN_TOKEN' in launcher
    assert 'unset ADMIN_TOKEN ADMIN_URL' in launcher
    assert "plistlib.dump" in launcher
    assert "temporary_path.replace(plist_path)" in launcher


def test_stop_web_command_only_unloads_its_exact_launchd_label() -> None:
    launcher = (PROJECT_ROOT / "stop_web.command").read_text()

    assert 'LABEL="com.llmolympic.web.$PROJECT_HASH"' in launcher
    assert 'SERVICE_TARGET="gui/$(/usr/bin/id -u)/$LABEL"' in launcher
    assert '/bin/launchctl print "$SERVICE_TARGET"' in launcher
    assert '/bin/launchctl bootout "$SERVICE_TARGET"' in launcher
    assert '/bin/rm -f "$ADMIN_TOKEN_FILE"' in launcher
    assert not re.search(r"\b(?:kill|killall|pkill)\b", launcher)


def test_web_commands_use_same_path_scoped_service_identity() -> None:
    identities = []
    for filename in COMMAND_FILES:
        launcher = (PROJECT_ROOT / filename).read_text()
        identities.append(
            (
                re.search(r'PROJECT_HASH="(.+)"', launcher).group(1),
                re.search(r'LABEL="(.+)"', launcher).group(1),
                re.search(r'SERVICE_TARGET="(.+)"', launcher).group(1),
            )
        )

    assert identities[0] == identities[1]


def test_web_commands_do_not_depend_on_unportable_shell_helpers() -> None:
    combined = "\n".join(
        (PROJECT_ROOT / filename).read_text() for filename in COMMAND_FILES
    )

    assert "#!/bin/bash" in combined
    assert "flock" not in combined
    assert "readlink -f" not in combined
    assert "timeout " not in combined
    assert "source " not in combined
    assert "eval " not in combined
    assert "pkill" not in combined
    assert "killall" not in combined
