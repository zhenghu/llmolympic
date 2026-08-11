"""Mock Provider：离线演示与测试用，不依赖任何 API key。

策略：
- ``random``：选择题随机选 A-D，棋类随机选择合法走法，其他题随机输出整数；
- ``fixed``：选择题答 A，棋类使用确定性合法走法，其他题答 42；
- ``illegal``：永远输出非法走法 "Z"，用于测试重试/判负逻辑。
- ``strict`` / ``balanced`` / ``lenient``：创意项目的确定性算法评委。
"""

from __future__ import annotations

import json
import random
import re

from llmolympic.providers.base import Provider, _stable_route_id

_CHOICE_RE = re.compile(r"^A[.、)]", re.MULTILINE)
_GOMOKU_ROW_RE = re.compile(
    r"^\s*(1[0-5]|[1-9])\s+((?:[.XO]\s+){14}[.XO])\s*$",
    re.MULTILINE,
)
_CHESS_LEGAL_RE = re.compile(r"^LEGAL_MOVES_UCI:\s*([^\n]*)$", re.MULTILINE)
_UCI_TOKEN_RE = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?")
_JUDGE_INPUT_RE = re.compile(r"<judge-input>\s*(.*)\s*</judge-input>", re.DOTALL)
_JUDGE_MARKER = "LLMOLYMPIC_JUDGE_REQUEST_V1"
_CREATIVE_SUBMISSION_MARKER = "CREATIVE_WRITING_SUBMISSION_V1"
_JUDGE_BASE_SCORES = {
    "strict": 3.5,
    "balanced": 6.0,
    "lenient": 8.5,
}
_CREATIVE_RESPONSES = {
    "fixed": "雨停以后，我把未寄出的信折成小船，让它沿着清晨的街道驶向那盏仍亮着的灯。",
    "strict": "旧钟敲响第十三声时，我关掉屋里所有的灯，只留下窗边那颗缓慢发芽的种子。",
    "balanced": "风从空荡的站台穿过，卷起一张旧车票；我追上它，也追上了那个迟到多年的春天。",
    "lenient": "月光落进杯中，像一封没有署名的回信；我轻轻喝下，终于听见远方故乡的潮声。",
}
_CHESS_FIXED_PREFERENCES = (
    "e2e4",
    "e7e5",
    "g1f3",
    "b8c6",
    "f1c4",
    "g8f6",
    "e1g1",
)


def _gomoku_empty_cells(prompt: str) -> list[str]:
    """从 Gomoku 文本棋盘提取当前所有合法空位。"""
    if "gomoku" not in prompt.lower():
        return []

    cells: list[str] = []
    for match in _GOMOKU_ROW_RE.finditer(prompt):
        row = int(match.group(1))
        symbols = match.group(2).split()
        for column, symbol in enumerate(symbols):
            if symbol == ".":
                cells.append(f"{chr(ord('A') + column)}{row}")
    return cells


def _chess_legal_moves(prompt: str) -> list[str]:
    """Extract the explicit machine-readable legal-move line from a chess prompt."""

    if "国际象棋（chess）" not in prompt:
        return []
    match = _CHESS_LEGAL_RE.search(prompt)
    if match is None:
        return []
    tokens = match.group(1).split()
    if not tokens or any(_UCI_TOKEN_RE.fullmatch(token) is None for token in tokens):
        return []
    return tokens


def _judge_payload(prompt: str) -> dict | None:
    """Extract the machine-delimited judge request without evaluating free text."""

    if _JUDGE_MARKER not in prompt:
        return None
    match = _JUDGE_INPUT_RE.search(prompt)
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _submission_items(value: object) -> list[tuple[str, str]]:
    """Accept the canonical label->text object plus a conservative list fallback."""

    if isinstance(value, dict):
        return [
            (label, text)
            for label, text in value.items()
            if isinstance(label, str) and label and isinstance(text, str)
        ]
    if not isinstance(value, list):
        return []
    items: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        text = item.get("submission", item.get("content"))
        if isinstance(label, str) and label and isinstance(text, str):
            items.append((label, text))
    return items


def _criterion_names(value: object) -> list[str]:
    if isinstance(value, dict):
        return [name for name in value if isinstance(name, str) and name]
    if isinstance(value, list):
        return [name for name in value if isinstance(name, str) and name]
    return []


def _mock_judge_response(strategy: str, payload: dict) -> str:
    """Return a deterministic, strictly JSON-encoded multi-submission verdict."""

    submissions = _submission_items(payload.get("submissions"))
    criteria = _criterion_names(payload.get("criteria"))
    if not submissions or not criteria:
        return "Z"

    base = _JUDGE_BASE_SCORES[strategy]
    scores: dict[str, dict[str, float]] = {}
    rationales: dict[str, str] = {}
    for label, submission in submissions:
        # Reward enough detail to make the three deterministic judge personalities
        # useful in offline demos without pretending that Mock is an LLM.
        detail_adjustment = min(max(len(submission) - 20, 0), 180) / 180
        score = round(min(10.0, base + detail_adjustment), 4)
        scores[label] = {criterion: score for criterion in criteria}
        rationales[label] = f"{strategy} mock judge: deterministic offline score"

    return json.dumps(
        {"scores": scores, "rationales": rationales},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class MockProvider(Provider):
    name = "mock"

    def __init__(self, strategy: str = "random", seed: int | None = None) -> None:
        if strategy not in ("random", "fixed", "illegal", *tuple(_JUDGE_BASE_SCORES)):
            raise ValueError(f"未知 mock 策略: {strategy!r}")
        self.strategy = strategy
        self._rng = random.Random(seed)  # noqa: S311 - mock 策略需可复现，不用于安全令牌

    def route_id_for(self, model: str) -> str:
        del model  # Mock 的伪模型标签不会改变实际算法路由。
        return _stable_route_id(
            family="mock-v1",
            target=self.strategy,
            model="",
        )

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        prompt = messages[-1]["content"]
        if self.strategy == "illegal":
            return "Z"

        judge_payload = _judge_payload(prompt)
        if judge_payload is not None and self.strategy in _JUDGE_BASE_SCORES:
            return _mock_judge_response(self.strategy, judge_payload)

        if _CREATIVE_SUBMISSION_MARKER in prompt:
            if self.strategy == "random":
                images = ("月光", "潮声", "旧车票", "纸船", "远方的灯")
                first, second = self._rng.sample(images, 2)
                return f"我在清晨拾起{first}留下的影子，把它写进信里，寄给仍在等待{second}的人。"
            return _CREATIVE_RESPONSES.get(self.strategy, _CREATIVE_RESPONSES["fixed"])

        chess_moves = _chess_legal_moves(prompt)
        if chess_moves:
            if self.strategy == "fixed":
                return next(
                    (
                        move
                        for move in _CHESS_FIXED_PREFERENCES
                        if move in chess_moves
                    ),
                    chess_moves[0],
                )
            return self._rng.choice(chess_moves)

        gomoku_cells = _gomoku_empty_cells(prompt)
        if gomoku_cells:
            if self.strategy == "fixed":
                return "H8" if "H8" in gomoku_cells else gomoku_cells[0]
            return self._rng.choice(gomoku_cells)

        is_choice = bool(_CHOICE_RE.search(prompt))
        if self.strategy == "fixed":
            return "A" if is_choice else "42"
        return self._rng.choice("ABCD") if is_choice else str(self._rng.randint(0, 99))

    async def achat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        return self.chat(
            messages,
            model=model,
            request_timeout=request_timeout,
            **params,
        )
