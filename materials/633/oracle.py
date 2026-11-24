"""小域独立 oracle，用于黑盒对照而不是复用待测代码。

两部分
======
1. :func:`enumerate_legal_schedules`
   4 队小域上用回溯枚举满足全部整关系约束的完整赛程（轮次一对球队匹配、
   slot 容量、禁用、主客平衡、无连续三轮同角色）。它不读取
   ``ScheduleService.check_candidate``，因此可以双向对照：
   枚举认为合法的，服务必须放行；枚举外的候选，服务必须拒绝。

2. :func:`oracle_standings`
   用最直白的字典累加 + 手工分组重写一遍排名，作为 ``compute_standings``
   的独立实现；测试里再对关键场景给出*显式手算*的小表数字常量。
"""

from __future__ import annotations

from itertools import permutations, product
from typing import Any, Mapping, Sequence


# ---------------------------------------------------------------- 赛程枚举
def _round_matchings(teams: Sequence[str]) -> list[tuple[tuple[str, str], ...]]:
    """一轮内全部完美匹配（无序）。4 队时共 3 种。"""
    if not teams:
        return [()]
    first, rest = teams[0], teams[1:]
    out: list[tuple[tuple[str, str], ...]] = []
    for i, partner in enumerate(rest):
        others = rest[:i] + rest[i + 1:]
        for sub in _round_matchings(others):
            out.append(((first, partner),) + sub)
    return out


def enumerate_legal_schedules(
    teams: Sequence[str],
    match_registry: Mapping[str, int],
    round_slots: Mapping[int, Mapping[str, int]],
    blackouts: Mapping[str, set[str]],
) -> list[list[dict[str, Any]]]:
    """枚举所有合法完整赛程（以 match_id/round 骨架承载）。

    每场还要枚举主客方向与允许的 slot；实现刻意直白、不做剪枝优化，
    仅服务 4 队小域。
    """
    n_rounds = len(teams) - 1
    round_mids: dict[int, list[str]] = {}
    for mid, r in match_registry.items():
        round_mids.setdefault(r, []).append(mid)
    matchings = _round_matchings(list(teams))

    # 预先为每轮列出 (匹配, 每场的 (mid, home, away, slot) 赋值) 的全部选项
    options_by_round: dict[int, list[list[dict[str, Any]]]] = {}
    for r in range(1, n_rounds + 1):
        mids = round_mids[r]
        slot_choices = list(round_slots[r].keys())
        options: list[list[dict[str, Any]]] = []
        for matching in matchings:
            for mid_perm in permutations(mids):  # match_id 落到具体对阵
                for orientations in product((0, 1), repeat=len(matching)):
                    for slot_assign in product(slot_choices, repeat=len(matching)):
                        rows = []
                        for k, pair in enumerate(matching):
                            a, b = pair
                            home, away = (a, b) if orientations[k] == 0 else (b, a)
                            rows.append({
                                "match_id": mid_perm[k], "round": r,
                                "home": home, "away": away,
                                "slot": slot_assign[k],
                            })
                        options.append(rows)
        options_by_round[r] = options

    legal: list[list[dict[str, Any]]] = []

    def check_global(cand: list[dict[str, Any]]) -> bool:
        pairs = {frozenset((r["home"], r["away"])) for r in cand}
        if len(pairs) != len(teams) * (len(teams) - 1) // 2:
            return False
        # slot 容量 / 禁用；同轮同队同 slot 重复已被完美匹配结构排除，仍显式检查
        slot_use: dict[tuple[int, str], int] = {}
        team_slot_round: dict[tuple[str, str, int], int] = {}
        for r in cand:
            slot_use[(r["round"], r["slot"])] = slot_use.get((r["round"], r["slot"]), 0) + 1
            for t in (r["home"], r["away"]):
                if r["slot"] in blackouts.get(t, set()):
                    return False
                key = (t, r["slot"], r["round"])
                team_slot_round[key] = team_slot_round.get(key, 0) + 1
                if team_slot_round[key] > 1:
                    return False
        for (r, s), used in slot_use.items():
            if used > round_slots[r][s]:
                return False
        # 主客差
        for t in teams:
            h = sum(1 for r in cand if r["home"] == t)
            a = sum(1 for r in cand if r["away"] == t)
            if abs(h - a) > 1:
                return False
        # 连续三轮同角色
        roles = {t: {} for t in teams}
        for r in cand:
            roles[r["home"]][r["round"]] = "H"
            roles[r["away"]][r["round"]] = "A"
        for t in teams:
            seq = "".join(roles[t].get(r, "?") for r in range(1, n_rounds + 1))
            if "HHH" in seq or "AAA" in seq:
                return False
        return True

    def walk(r: int, acc: list[dict[str, Any]]) -> None:
        if r > n_rounds:
            if check_global(acc):
                legal.append([dict(x) for x in acc])
            return
        for rows in options_by_round[r]:
            walk(r + 1, acc + rows)

    walk(1, [])
    return legal


# ---------------------------------------------------------------- 独立排名重写
def _oracle_group_order(
    members: Sequence[str],
    internal_games: list[tuple[str, int, str, int]],
    full_gd: Mapping[str, int],
    full_gf: Mapping[str, int],
    depth: int = 0,
) -> tuple[list[str], dict[str, Any]]:
    """递归小表，返回顺序与解释结构。games=(home,sh,away,sa)。"""
    members = sorted(members)
    if len(members) == 1:
        return members, {"teams": members, "leaf": True}
    mset = set(members)
    tab = {t: {"pts": 0, "gf": 0, "ga": 0} for t in members}
    used: list[str] = []
    for item in internal_games:
        home, sh, away, sa, mid = item
        if home not in mset or away not in mset:
            continue
        used.append(mid)
        tab[home]["gf"] += sh
        tab[home]["ga"] += sa
        tab[away]["gf"] += sa
        tab[away]["ga"] += sh
        if sh == sa:
            tab[home]["pts"] += 1
            tab[away]["pts"] += 1
        elif sh > sa:
            tab[home]["pts"] += 3
        else:
            tab[away]["pts"] += 3
    buckets: dict[tuple[int, int, int], list[str]] = {}
    for t in members:
        key = (tab[t]["pts"], tab[t]["gf"] - tab[t]["ga"], tab[t]["gf"])
        buckets.setdefault(key, []).append(t)
    explain = {
        "teams": members,
        "leaf": False,
        "match_ids": sorted(used),
        "metrics": {t: {"mini_points": tab[t]["pts"],
                        "mini_gd": tab[t]["gf"] - tab[t]["ga"],
                        "mini_gf": tab[t]["gf"]} for t in members},
    }
    ordered_groups = [sorted(buckets[k]) for k in sorted(buckets, reverse=True)]
    if len(ordered_groups) > 1:
        order: list[str] = []
        children = []
        for grp in ordered_groups:
            sub_order, sub_explain = _oracle_group_order(
                grp, internal_games, full_gd, full_gf, depth + 1)
            order.extend(sub_order)
            children.append(sub_explain)
        explain["split_used"] = True
        explain["children"] = children
        return order, explain
    # 无法拆分 -> 全表 GD/GF + ASCII 终止
    final = sorted(members,
                   key=lambda t: (-full_gd[t], -full_gf[t], t))
    explain["split_used"] = False
    explain["fallback"] = {t: {"full_gd": full_gd[t], "full_gf": full_gf[t]}
                           for t in members}
    explain["children"] = [{"teams": [t], "leaf": True} for t in final]
    return final, explain


def oracle_standings(
    teams: Sequence[str],
    fixtures: Sequence[Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    penalties: Mapping[str, int],
    rounds: tuple[int, int],
) -> dict[str, Any]:
    """与 :func:`league.ranking.compute_standings` 独立的第二实现。"""
    lo, hi = rounds
    fx = {f["match_id"]: f for f in fixtures}
    lines = {t: {"pts": 0, "gf": 0, "ga": 0, "played": 0} for t in teams}
    internal: list[tuple[str, int, str, int, str]] = []
    ignored: list[str] = []
    for mid, f in fx.items():
        if not (lo <= f["round"] <= hi):
            continue
        r = results.get(mid)
        if r is None or r["state"] != "played":
            ignored.append(mid)
            continue
        sh, sa = r["score_home"], r["score_away"]
        home, away = f["home"], f["away"]
        lines[home]["gf"] += sh
        lines[home]["ga"] += sa
        lines[away]["gf"] += sa
        lines[away]["ga"] += sh
        lines[home]["played"] += 1
        lines[away]["played"] += 1
        if sh == sa:
            lines[home]["pts"] += 1
            lines[away]["pts"] += 1
        elif sh > sa:
            lines[home]["pts"] += 3
        else:
            lines[away]["pts"] += 3
        internal.append((home, sh, away, sa, mid))

    totals = {t: lines[t]["pts"] - int(penalties.get(t, 0)) for t in teams}
    full_gd = {t: lines[t]["gf"] - lines[t]["ga"] for t in teams}
    full_gf = {t: lines[t]["gf"] for t in teams}

    groups: dict[int, list[str]] = {}
    for t in teams:
        groups.setdefault(totals[t], []).append(t)
    order: list[str] = []
    tree = []
    for total in sorted(groups, reverse=True):
        grp = sorted(groups[total])
        if len(grp) == 1:
            order.extend(grp)
            tree.append({"teams": grp, "leaf": True, "total_points": total})
        else:
            sub_order, explain = _oracle_group_order(grp, internal, full_gd, full_gf)
            order.extend(sub_order)
            explain["total_points"] = total
            tree.append(explain)
    return {
        "order": order,
        "total_points": totals,
        "full_gd": full_gd,
        "full_gf": full_gf,
        "lines": lines,
        "ignored": sorted(ignored),
        "tree": tree,
    }
