"""测试共用夹具：4/6 队骨架、合法基准赛程与各种定向破坏版本。"""

from __future__ import annotations

import copy

TEAMS4 = ["A", "B", "C", "D"]

# match_id -> 固定 round（初始化骨架）
REGISTRY4 = {
    "ab": 1, "cd": 1,
    "ac": 2, "bd": 2,
    "ad": 3, "bc": 3,
}

ROUND_SLOTS4 = {1: {"s": 2}, 2: {"s": 2}, 3: {"s": 2}}
BLACKOUTS_EMPTY = {t: [] for t in TEAMS4}

# 合法基准：每对一次、每轮每队一次、主客差 <=1、无三连角色、slot 容量 2
FIXTURE4 = [
    {"match_id": "ab", "round": 1, "home": "A", "away": "B", "slot": "s"},
    {"match_id": "cd", "round": 1, "home": "C", "away": "D", "slot": "s"},
    {"match_id": "ac", "round": 2, "home": "C", "away": "A", "slot": "s"},
    {"match_id": "bd", "round": 2, "home": "B", "away": "D", "slot": "s"},
    {"match_id": "ad", "round": 3, "home": "D", "away": "A", "slot": "s"},
    {"match_id": "bc", "round": 3, "home": "B", "away": "C", "slot": "s"},
]


def mutate(rows, index, **changes):
    out = copy.deepcopy(rows)
    out[index].update(changes)
    return out


def replace(rows, per_index):
    out = copy.deepcopy(rows)
    for i, changes in per_index.items():
        out[i].update(changes)
    return out


# 6 队骨架：circle 法单循环，轮次 r 共 3 场，5 轮
TEAMS6 = ["T1", "T2", "T3", "T4", "T5", "T6"]


def _build6():
    # circle method: 固定 T6，其余轮转，先得到每轮完美匹配
    rot = ["T1", "T2", "T3", "T4", "T5"]
    pairs_by_round: dict[int, list[tuple[str, str]]] = {}
    registry: dict[str, int] = {}
    for r in range(1, 6):
        pairs = [("T6", rot[0])] + [(rot[i], rot[5 - i]) for i in range(1, 3)]
        for k, _ in enumerate(pairs):
            registry[f"m{r}{k}"] = r
        pairs_by_round[r] = pairs
        rot = [rot[-1]] + rot[:-1]

    # 逐场定向回溯：主客差 <=1 且无连续三轮同角色
    def orient_feasible() -> dict[tuple[int, int], tuple[str, str]]:
        role_seq = {t: [] for t in TEAMS6}
        home_n = {t: 0 for t in TEAMS6}
        away_n = {t: 0 for t in TEAMS6}
        choice: dict[tuple[int, int], tuple[str, str]] = {}

        def backtrack(r: int, k: int) -> bool:
            if r > 5:
                return all(abs(home_n[t] - away_n[t]) <= 1 for t in TEAMS6)
            nxt_r, nxt_k = (r, k + 1) if k < 2 else (r + 1, 0)
            a, b = pairs_by_round[r][k]
            for home, away in ((a, b), (b, a)):
                seq = role_seq[home]
                if len(seq) >= 2 and seq[-2:] == ["H", "H"]:
                    continue
                seq = role_seq[away]
                if len(seq) >= 2 and seq[-2:] == ["A", "A"]:
                    continue
                choice[(r, k)] = (home, away)
                role_seq[home].append("H")
                role_seq[away].append("A")
                home_n[home] += 1
                away_n[away] += 1
                if backtrack(nxt_r, nxt_k):
                    return True
                role_seq[home].pop()
                role_seq[away].pop()
                home_n[home] -= 1
                away_n[away] -= 1
            return False

        assert backtrack(1, 0), "6 队夹具定向回溯应能找到合法主客安排"
        return choice

    orientations = orient_feasible()
    fixture = []
    for r in range(1, 6):
        for k in range(3):
            home, away = orientations[(r, k)]
            fixture.append({"match_id": f"m{r}{k}", "round": r,
                            "home": home, "away": away, "slot": "s"})
    return registry, fixture


REGISTRY6, FIXTURE6 = _build6()
ROUND_SLOTS6 = {r: {"s": 3} for r in range(1, 6)}
