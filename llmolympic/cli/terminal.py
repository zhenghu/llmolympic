"""终端输出的安全边界。

所有来自选手、Provider、档案或数据库的文本都应先转换成 ``Text``，避免
Rich 把其中的 ``[tag]`` 当成标记解析。这里同时移除终端控制字符和可能改变
显示方向的双向文本控制符，并为交互式输出设置长度上限。
"""

from __future__ import annotations

import unicodedata

from rich.text import Text

DEFAULT_DISPLAY_LIMIT = 4_096
NAME_DISPLAY_LIMIT = 256
PROMPT_DISPLAY_LIMIT = 16_384
ARCHIVE_DISPLAY_LIMIT = 100_000

_TRUNCATION_MARKER = "…（已截断）"
_BIDI_CONTROLS = frozenset(
    {
        "\u061c",  # ARABIC LETTER MARK
        "\u200e",  # LEFT-TO-RIGHT MARK
        "\u200f",  # RIGHT-TO-LEFT MARK
        "\u202a",  # LEFT-TO-RIGHT EMBEDDING
        "\u202b",  # RIGHT-TO-LEFT EMBEDDING
        "\u202c",  # POP DIRECTIONAL FORMATTING
        "\u202d",  # LEFT-TO-RIGHT OVERRIDE
        "\u202e",  # RIGHT-TO-LEFT OVERRIDE
        "\u2066",  # LEFT-TO-RIGHT ISOLATE
        "\u2067",  # RIGHT-TO-LEFT ISOLATE
        "\u2068",  # FIRST STRONG ISOLATE
        "\u2069",  # POP DIRECTIONAL ISOLATE
    }
)


def sanitize_terminal_text(
    value: object,
    *,
    max_chars: int = DEFAULT_DISPLAY_LIMIT,
    multiline: bool = False,
) -> str:
    """返回适合终端显示的有界纯文本。

    C0/C1、ESC、孤立代理码以及 Unicode 双向控制符会被替换为可见的替代
    字符。多行题面和 JSON 可以显式保留规范化后的换行；其他控制字符仍会
    被过滤。这里限制的是显示长度，不改变原始事件或持久化档案。
    """

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    raw = str(value)

    safe: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if multiline and char == "\r":
            safe.append("\n")
            if index + 1 < len(raw) and raw[index + 1] == "\n":
                index += 1
        elif multiline and char == "\n":
            safe.append("\n")
        elif char in _BIDI_CONTROLS or unicodedata.category(char) in {"Cc", "Cs"}:
            safe.append("�")
        else:
            safe.append(char)
        index += 1

    cleaned = "".join(safe)
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:max_chars]
    return cleaned[: max_chars - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def literal_text(
    value: object,
    *,
    style: str | None = None,
    max_chars: int = DEFAULT_DISPLAY_LIMIT,
    multiline: bool = False,
) -> Text:
    """把不可信值包装为不会触发 Rich markup 的 ``Text``。"""

    return Text(
        sanitize_terminal_text(value, max_chars=max_chars, multiline=multiline),
        style=style,
    )
