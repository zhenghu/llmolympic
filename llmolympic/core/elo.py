"""标准 ELO 评分（K=32）。

本模块提供纯计算和轻量内存榜；SQLite 榜单与历史记录由
``llmolympic.core.storage.SQLiteStore`` 持久化。
"""

from __future__ import annotations

DEFAULT_RATING = 1500.0
K_FACTOR = 32.0


def expected_score(rating_a: float, rating_b: float) -> float:
    """A 对 B 的期望胜率。"""
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def update_ratings(
    rating_a: float, rating_b: float, score_a: float, k: float = K_FACTOR
) -> tuple[float, float]:
    """按 A 的实际得分（1.0 胜 / 0.5 平 / 0.0 负）更新双方等级分。"""
    expected_a = expected_score(rating_a, rating_b)
    new_a = rating_a + k * (score_a - expected_a)
    new_b = rating_b + k * ((1.0 - score_a) - (1.0 - expected_a))
    return new_a, new_b


class EloTable:
    """简单的内存 ELO 榜单（对两人对局逐场更新）。"""

    def __init__(self) -> None:
        self.ratings: dict[str, float] = {}

    def rating(self, player: str) -> float:
        return self.ratings.get(player, DEFAULT_RATING)

    def record(self, player_a: str, player_b: str, score_a: float) -> None:
        new_a, new_b = update_ratings(self.rating(player_a), self.rating(player_b), score_a)
        self.ratings[player_a] = new_a
        self.ratings[player_b] = new_b
