"""判分接口。

规则判分内嵌在各 Game 的 ``score()`` 中（阶段一/二）；
创意类项目（阶段三）由 LLM 评审团打分，此处仅预留抽象，不做实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class JudgeVerdict(BaseModel):
    submission: str
    score: float  # 0.0 ~ 1.0
    rationale: str = ""  # 评审理由


class LLMJudgePanel(ABC):
    """LLM 评审团：多个模型作为评委，按 rubric 对创意类提交打分并汇总。

    阶段三只定义接口，具体实现（评委配置、汇总策略、防共谋）留待后续。
    """

    @abstractmethod
    async def judge(self, *, task: str, submission: str, criteria: dict) -> JudgeVerdict:
        """对一份提交给出评分与理由。"""
