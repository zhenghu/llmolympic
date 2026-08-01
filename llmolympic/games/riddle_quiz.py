"""猜谜竞答：从版本化结构化题库按 seed 组合线索与干扰项。"""

from __future__ import annotations

import random
import unicodedata
from collections import Counter

from pydantic import Field

from llmolympic.core.game import FORFEIT_MOVE, GameState, IllegalMoveError
from llmolympic.games._choice import LETTERS, make_options, parse_choice, render_options

BANK_VERSION = 1
GENERATOR_VERSION = 1

# 每个类别至少四项，干扰项只从同类别抽取。线索以结构化 feature 保存，
# 生成时再随机选择和排序，避免把静态散文谜面伪装成动态题目。
RIDDLE_BANK: tuple[dict, ...] = (
    {
        "id": "keyboard",
        "answer": "键盘",
        "aliases": ["电脑键盘"],
        "category": "日常物品",
        "features": {
            "外形": "身上有许多按键",
            "反差": "有很多“键”却打不开门锁",
            "动作": "工作时常被手指敲击",
            "用途": "帮助电脑输入文字和命令",
        },
    },
    {
        "id": "map",
        "answer": "地图",
        "aliases": ["地图册"],
        "category": "日常物品",
        "features": {
            "外形": "上面有城市却没有房屋",
            "反差": "上面有河流却没有流水",
            "动作": "常被展开或缩放查看",
            "用途": "帮助人寻找地点和路线",
        },
    },
    {
        "id": "clock",
        "answer": "时钟",
        "aliases": ["钟", "钟表"],
        "category": "日常物品",
        "features": {
            "外形": "有一张脸却没有眼睛",
            "反差": "有几只手却拿不起东西",
            "动作": "昼夜不停地走动",
            "用途": "告诉人们现在的时间",
        },
    },
    {
        "id": "mirror",
        "answer": "镜子",
        "aliases": ["镜面"],
        "category": "日常物品",
        "features": {
            "外形": "通常有平整而光亮的表面",
            "反差": "没有记忆却会立刻模仿你",
            "动作": "你笑时它也对你笑",
            "用途": "利用反射让人看见自己的样子",
        },
    },
    {
        "id": "shadow",
        "answer": "影子",
        "aliases": ["身影"],
        "category": "自然现象",
        "features": {
            "出现": "有光照时常跟在物体旁边",
            "反差": "会模仿动作却没有声音",
            "变化": "方向和长短会随光源改变",
            "消失": "在完全黑暗中看不见",
        },
    },
    {
        "id": "echo",
        "answer": "回声",
        "aliases": ["回音"],
        "category": "自然现象",
        "features": {
            "出现": "在山谷或空旷建筑中更容易听见",
            "反差": "没有嘴却会重复你的话",
            "变化": "总比原来的声音晚一点到达",
            "成因": "由声音遇到障碍后反射形成",
        },
    },
    {
        "id": "rainbow",
        "answer": "彩虹",
        "aliases": ["虹"],
        "category": "自然现象",
        "features": {
            "出现": "常在雨后并有阳光时出现",
            "外形": "像一座跨在天空中的彩色拱桥",
            "反差": "看得见却很难走到它的脚下",
            "成因": "来自光在水滴中的折射和反射",
        },
    },
    {
        "id": "cloud",
        "answer": "云",
        "aliases": ["云朵", "云彩"],
        "category": "自然现象",
        "features": {
            "出现": "漂浮在天空中",
            "外形": "远看像棉花却不能纺线",
            "变化": "形状不断改变并会随风移动",
            "成因": "由大量微小水滴或冰晶组成",
        },
    },
    {
        "id": "pencil",
        "answer": "铅笔",
        "aliases": ["木铅笔"],
        "category": "文具工具",
        "features": {
            "外形": "身体细长，里面藏着一根深色笔芯",
            "变化": "越削越尖，也越用越短",
            "动作": "在纸上行走时留下痕迹",
            "用途": "写错后留下的字通常可以擦掉",
        },
    },
    {
        "id": "eraser",
        "answer": "橡皮",
        "aliases": ["橡皮擦"],
        "category": "文具工具",
        "features": {
            "外形": "常是一块柔软的小方块",
            "变化": "帮助越多，自己的身体越小",
            "动作": "在纸面来回摩擦并产生碎屑",
            "用途": "专门清除铅笔留下的错误",
        },
    },
    {
        "id": "scissors",
        "answer": "剪刀",
        "aliases": ["剪子"],
        "category": "文具工具",
        "features": {
            "外形": "有两片交叉的金属刃",
            "反差": "有两个圆洞却不是眼睛",
            "动作": "张嘴又合嘴时把材料分开",
            "用途": "可以裁剪纸张、布料或线",
        },
    },
    {
        "id": "needle",
        "answer": "针",
        "aliases": ["缝衣针"],
        "category": "文具工具",
        "features": {
            "外形": "身体细长，一端非常尖",
            "反差": "有一只眼睛却看不见东西",
            "动作": "带着线在布料之间穿行",
            "用途": "把分开的布料缝在一起",
        },
    },
)


def validate_riddle_bank(bank: tuple[dict, ...] = RIDDLE_BANK) -> None:
    """检查结构化题库的标识、答案、类别规模与线索归属。"""

    ids = [item["id"] for item in bank]
    if len(ids) != len(set(ids)):
        raise ValueError("谜题题库的 id 必须唯一")

    accepted_names: dict[str, str] = {}
    clue_owners: dict[str, set[str]] = {}
    for item in bank:
        if len(item["features"]) < 3:
            raise ValueError(f"谜题 {item['id']!r} 至少需要 3 条结构化线索")
        for name in [item["answer"], *item["aliases"]]:
            normalized = unicodedata.normalize("NFKC", name).strip().casefold()
            previous = accepted_names.setdefault(normalized, item["id"])
            if previous != item["id"]:
                raise ValueError(f"谜题答案或别名 {name!r} 同时属于多个目标")
        for clue in item["features"].values():
            clue_owners.setdefault(clue, set()).add(item["id"])

    category_sizes = Counter(item["category"] for item in bank)
    undersized = sorted(category for category, size in category_sizes.items() if size < 4)
    if undersized:
        raise ValueError(f"以下谜题类别不足 4 项: {', '.join(undersized)}")
    shared_clues = sorted(clue for clue, owners in clue_owners.items() if len(owners) != 1)
    if shared_clues:
        raise ValueError(f"结构化线索必须只属于一个目标: {shared_clues}")


validate_riddle_bank()


class RiddleQuizState(GameState):
    rounds: int
    questions: list[dict]
    cursor: dict[str, int] = Field(default_factory=dict)
    answers: dict[str, list[str]] = Field(default_factory=dict)


def _generate_question(rng: random.Random, target: dict) -> dict:
    peers = [
        item
        for item in RIDDLE_BANK
        if item["category"] == target["category"] and item["id"] != target["id"]
    ]
    if len(peers) < 3:
        raise RuntimeError(f"谜题类别 {target['category']!r} 的干扰项不足")
    distractors = rng.sample(peers, 3)
    options, answer = make_options(
        rng,
        target["answer"],
        [item["answer"] for item in distractors],
    )
    selected_features = rng.sample(list(target["features"].items()), 3)
    clues = [value for _, value in selected_features]
    option_records = [target, *distractors]
    records_by_answer = {item["answer"]: item for item in option_records}
    matching_target_ids = [
        item["id"]
        for item in option_records
        if all(clue in item["features"].values() for clue in clues)
    ]
    if matching_target_ids != [target["id"]]:
        raise RuntimeError("谜题结构化线索没有筛出唯一目标")
    aliases = {
        letter: list(records_by_answer[option]["aliases"])
        for letter, option in zip(LETTERS, options, strict=True)
    }
    return {
        "kind": target["category"],
        "text": "根据三条线索猜出最符合的对象：\n"
        + "\n".join(f"{index}. {clue}" for index, clue in enumerate(clues, start=1)),
        "options": options,
        "answer": answer,
        "aliases": aliases,
        "clues": clues,
        "target_id": target["id"],
        "matching_target_ids": matching_target_ids,
        "source": "generated_from_structured_bank",
        "bank_version": BANK_VERSION,
        "generator_version": GENERATOR_VERSION,
    }


class RiddleQuiz:
    """每位选手回答同一组结构化猜谜题，按正确率计分。"""

    name = "riddle_quiz"
    forfeit_scope = "turn"
    min_players = 1
    max_players = None

    def __init__(self, rounds: int = 5) -> None:
        if rounds < 1:
            raise ValueError("rounds 必须至少为 1")
        if rounds > len(RIDDLE_BANK):
            raise ValueError(f"riddle_quiz 的 rounds 最多为 {len(RIDDLE_BANK)}")
        self.rounds = rounds

    def describe_config(self) -> dict[str, object]:
        return {
            "rounds": self.rounds,
            "source": "generated_from_structured_bank",
            "bank_version": BANK_VERSION,
            "generator_version": GENERATOR_VERSION,
        }

    def new_state(self, players: list[str], seed: int) -> RiddleQuizState:
        rng = random.Random(seed)  # noqa: S311 - 公平复现用种子，不用于安全令牌
        targets = rng.sample(RIDDLE_BANK, self.rounds)
        return RiddleQuizState(
            players=list(players),
            seed=seed,
            rounds=self.rounds,
            questions=[_generate_question(rng, target) for target in targets],
            cursor={player: 0 for player in players},
            answers={player: [] for player in players},
        )

    def current_players(self, state: RiddleQuizState) -> list[str]:
        return [player for player in state.players if state.cursor[player] < state.rounds]

    def prompt_for(self, state: RiddleQuizState, player: str) -> str:
        index = state.cursor[player]
        question = state.questions[index]
        return (
            f"猜谜竞答（{question['kind']}）第 {index + 1}/{state.rounds} 题：\n"
            f"{question['text']}\n{render_options(question['options'])}\n"
            "（输出一个选项字母、完整谜底或同义名称）"
        )

    def apply_move(self, state: RiddleQuizState, player: str, move: str) -> None:
        if player not in self.current_players(state):
            raise IllegalMoveError(f"{player} 当前没有待作答的题")
        if move == FORFEIT_MOVE:
            answer = ""
        else:
            question = state.questions[state.cursor[player]]
            answer = parse_choice(
                move,
                question["options"],
                aliases=question["aliases"],
            )
        state.answers[player].append(answer)
        state.cursor[player] += 1

    def is_over(self, state: RiddleQuizState) -> bool:
        return not self.current_players(state)

    def score(self, state: RiddleQuizState) -> dict[str, float]:
        return {
            player: sum(
                answer == question["answer"]
                for question, answer in zip(
                    state.questions, state.answers[player], strict=True
                )
            )
            / state.rounds
            for player in state.players
        }
