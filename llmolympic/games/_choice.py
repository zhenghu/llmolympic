"""选择题插件共享的选项生成、渲染与宽容解析。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from random import Random

from llmolympic.core.game import IllegalMoveError

LETTERS = ("A", "B", "C", "D")

_LETTER_ANSWER_RE = re.compile(
    r"(?:(?:答案|谜底|选择|选项|我选|我猜)(?:是|为)?\s*[:：]?\s*)?"
    r"[\(\[（]?\s*([A-D])\s*[\)\]）]?[.。]?",
    re.IGNORECASE,
)
_LABELED_OPTION_RE = re.compile(
    r"[\(\[]?\s*([A-D])\s*[\)\]]?\s*[.。:：、-]\s*(.+)",
    re.IGNORECASE,
)
_TEXT_PREFIX_RE = re.compile(
    r"^(?:(?:答案|谜底|选择|选项|我选|我猜)(?:是|为)?\s*[:：]?\s*)",
    re.IGNORECASE,
)


def _normalized(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).strip()
    value = value.strip("`*_~")
    value = value.rstrip("。.!！?？")
    return " ".join(value.casefold().split())


def make_options(
    rng: Random,
    correct: str,
    distractors: Sequence[str],
) -> tuple[list[str], str]:
    """打乱一个正确项和三个干扰项，返回选项与正确字母。"""

    options = [correct, *distractors]
    if len(options) != len(LETTERS) or len(set(options)) != len(LETTERS):
        raise ValueError("选择题必须包含 1 个正确项和 3 个互不重复的干扰项")
    rng.shuffle(options)
    return options, LETTERS[options.index(correct)]


def render_options(options: Sequence[str]) -> str:
    if len(options) != len(LETTERS):
        raise ValueError("选择题必须恰好包含 4 个选项")
    return "\n".join(
        f"{letter}. {option}" for letter, option in zip(LETTERS, options, strict=True)
    )


def parse_choice(
    move: str,
    options: Sequence[str],
    *,
    aliases: Mapping[str, Iterable[str]] | None = None,
) -> str:
    """把常见包装形式或完整选项文本解析为唯一的 A–D。

    不做任意子串搜索，因此 ``A 或 B``、解释段落等含糊输出会被拒绝。
    """

    if len(options) != len(LETTERS):
        raise ValueError("选择题必须恰好包含 4 个选项")
    raw = unicodedata.normalize("NFKC", move).strip().strip("`*_~")
    if not raw:
        raise IllegalMoveError("答案不能为空")

    candidates: set[str] = set()
    text_answer = _TEXT_PREFIX_RE.sub("", raw, count=1)
    for candidate_text in {raw, text_answer}:
        letter_match = _LETTER_ANSWER_RE.fullmatch(candidate_text)
        if letter_match is not None:
            candidates.add(letter_match.group(1).upper())

    normalized_answers = {_normalized(raw), _normalized(text_answer)}
    accepted_by_letter: dict[str, set[str]] = {}
    for letter, option in zip(LETTERS, options, strict=True):
        accepted = {_normalized(option)}
        if aliases is not None:
            accepted.update(_normalized(alias) for alias in aliases.get(letter, ()))
        accepted_by_letter[letter] = accepted
        if normalized_answers & accepted:
            candidates.add(letter)

    labeled_match = _LABELED_OPTION_RE.fullmatch(text_answer)
    if labeled_match is not None:
        labeled_letter = labeled_match.group(1).upper()
        labeled_text = _normalized(labeled_match.group(2))
        matching_text_letters = {
            letter
            for letter, accepted in accepted_by_letter.items()
            if labeled_text in accepted
        }
        if matching_text_letters == {labeled_letter}:
            candidates.add(labeled_letter)
        elif matching_text_letters:
            raise IllegalMoveError("选项字母与后面的答案内容不一致")

    if len(candidates) == 1:
        return candidates.pop()
    if len(candidates) > 1:
        raise IllegalMoveError("答案同时指向多个选项，请只提交一个明确答案")
    raise IllegalMoveError(f"无效选项: {move!r}，请回答 A/B/C/D 或完整选项内容")
