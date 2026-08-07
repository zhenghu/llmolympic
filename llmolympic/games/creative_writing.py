"""创意写作：两名选手同时提交短篇作品，由匿名 LLM 评审团判分。"""

from __future__ import annotations

import random

from pydantic import Field

from llmolympic.core.game import FORFEIT_MOVE, GameState, IllegalMoveError
from llmolympic.core.judge import JudgingRequest

MIN_SUBMISSION_CHARS = 20
MAX_SUBMISSION_CHARS = 2_000
TASK_BANK_VERSION = "creative-writing-tasks-v1"
RUBRIC_VERSION = "creative-writing-rubric-v1"

CRITERIA: dict[str, float] = {
    "创意": 0.40,
    "叙事完整性": 0.35,
    "语言表现": 0.25,
}

_TASKS = (
    "写一篇微型故事，主题是“最后一班永远不会到站的列车”。",
    "写一篇微型故事，让一封寄给未来自己的信意外改变了今天。",
    "写一篇微型故事，其中一座城市每天清晨都会忘记一件事。",
    "写一篇微型故事，以“门外站着昨天的我”作为开端。",
    "写一篇微型故事，描写一个只能在雨天营业的记忆商店。",
    "写一篇微型故事，让月亮第一次向地球上的某个人求助。",
)


class CreativeWritingState(GameState):
    """一轮同时作答的创意写作状态。"""

    task: str
    submissions: dict[str, str] = Field(default_factory=dict)


class CreativeWriting:
    """两人创意写作对决；规则层收稿，异步评审团负责终局判分。"""

    name = "creative_writing"
    forfeit_scope = "turn"
    min_players = 2
    max_players = 2
    requires_judge_panel = True
    supported_modes = frozenset({"play"})

    def describe_config(self) -> dict[str, object]:
        return {
            "task_bank_version": TASK_BANK_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "criteria": dict(CRITERIA),
            "min_submission_chars": MIN_SUBMISSION_CHARS,
            "max_submission_chars": MAX_SUBMISSION_CHARS,
        }

    def new_state(self, players: list[str], seed: int) -> CreativeWritingState:
        rng = random.Random(seed)  # noqa: S311 - 可复现题目选择，不用于安全令牌
        return CreativeWritingState(
            players=list(players),
            seed=seed,
            task=rng.choice(_TASKS),
        )

    def current_players(self, state: CreativeWritingState) -> list[str]:
        return [player for player in state.players if player not in state.submissions]

    def prompt_for(self, state: CreativeWritingState, player: str) -> str:
        if player not in self.current_players(state):
            raise ValueError(f"{player} 当前没有待提交的作品")
        return (
            "CREATIVE_WRITING_SUBMISSION_V1\n"
            f"{state.task}\n"
            f"正文长度必须为 {MIN_SUBMISSION_CHARS} 到 {MAX_SUBMISSION_CHARS} 个字符。\n"
            "只输出作品正文，不要解释创作过程，也不要添加标题或字数说明。"
        )

    def apply_move(self, state: CreativeWritingState, player: str, move: str) -> None:
        if player not in self.current_players(state):
            raise IllegalMoveError(f"{player} 当前没有待提交的作品")
        if move == FORFEIT_MOVE:
            state.submissions[player] = ""
            return

        submission = move.strip()
        if len(submission) < MIN_SUBMISSION_CHARS:
            raise IllegalMoveError(f"作品至少需要 {MIN_SUBMISSION_CHARS} 个字符")
        if len(submission) > MAX_SUBMISSION_CHARS:
            raise IllegalMoveError(f"作品不能超过 {MAX_SUBMISSION_CHARS} 个字符")
        state.submissions[player] = submission

    def is_over(self, state: CreativeWritingState) -> bool:
        return not self.current_players(state)

    def judging_request(self, state: CreativeWritingState) -> JudgingRequest:
        if not self.is_over(state):
            raise ValueError("所有选手提交或放弃后才能开始评审")
        submissions = {
            player: submission
            for player, submission in state.submissions.items()
            if submission
        }
        fixed_scores = {
            player: 0.0 for player, submission in state.submissions.items() if not submission
        }
        return JudgingRequest(
            task=state.task,
            criteria=dict(CRITERIA),
            submissions=submissions,
            fixed_scores=fixed_scores,
            rubric_version=RUBRIC_VERSION,
        )

    def score(self, state: CreativeWritingState) -> dict[str, float]:
        """Only an all-forfeit match has a rule score; other results require the panel."""

        request = self.judging_request(state)
        if request.submissions:
            raise RuntimeError("creative_writing 必须由 LLM 评审团判分")
        return dict(request.fixed_scores)
