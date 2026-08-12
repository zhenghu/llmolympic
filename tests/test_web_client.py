from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JAVASCRIPT_TEST = PROJECT_ROOT / "tests" / "web_client_state.js"
JAVASCRIPTCORE = Path(
    "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
)


def test_observer_client_state_machine_executes() -> None:
    engine = shutil.which("node")
    if engine is None and JAVASCRIPTCORE.is_file():
        engine = str(JAVASCRIPTCORE)
    if engine is None:
        pytest.skip("Node.js or JavaScriptCore is required for the browser-client unit test")

    completed = subprocess.run(  # noqa: S603
        [engine, str(JAVASCRIPT_TEST)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "observer client state tests passed" in completed.stdout
