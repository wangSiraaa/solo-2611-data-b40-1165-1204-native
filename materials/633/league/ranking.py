"""可解释排名引擎（纯函数，不依赖任何存储）。

排名规则（本题虚构联赛）
========================
1. 仅 ``state == 'played'`` 的结果进入排名；``cancelled``/``pending`` 不当 0:0，
   只是缺席。赛程产品中的 home/away 决定比分归属，本引擎对“投影”完全中立：
   传入什么 home/away，就按谁是主场解释 ``score_home/score_away``。
2. 胜 3 分、平 1 分、负 0 分。全季范围内每支球队保留一行，一场没踢也是一行。
3. 总积分 = 比赛积分 - penalty_points（可为负）。判罚只在总积分扣一次，不进小表。
4. 总积分相同的球队集合，只取集合内部彼此的 played 比赛，依次比较
   小表积分、小表净胜分、小表进球数（均降序），用这三个指标*整体分组*。
5. 若该步把集合拆成多个真子组，对每个仍同值的子组用**子组自身**的相互比赛
   重新计算同样三指标后递归判定，禁止沿用大集合的小表数值。
6. 若一次比较完全不能拆分，退到所请求轮次范围内的全表净胜分、进球数，
   再按球队 ID 的 ASCII 顺序，至此终止。

输出除最终名次外，还保留每一层使用的球队集合、实际 match_id 与指标，
不做“只给最后一个排序键”的黑箱排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

WIN_POINTS = 3
DRAW_POINTS = 1


@dataclass
class TeamLine:
    team: str
    played: int
    wins: int
    draws: int
    losses: int
    goals_for: int
    goals_against: int
    goal_diff: int
    match_points: int
    penalty_points: int
    total_points: int
    played_match_ids: list[str] = field(default_factory=list)


@dataclass
class TieNode:
    """递归小表的一层解释。

    leaf=True 时该节点就是单个最终位置；否则记录本层集合、使用的内部比赛、
    各队三项小表指标，以及按三指标整体分组得到的子节点。
    若本层无法拆分则走 fallback：全表 GD/GF + ASCII，split_used=False。
    """

    teams: list[str]
    level: int
    leaf: bool = False
    match_ids: list[str] = field(default_factory=list)
    metrics: dict[str, dict[str, int]] = field(default_factory=dict)
    criterion: str | None = None  # 'mini_table' 或 'full_table_fallback'
    split_used: bool = False
    fallback_metrics: dict[str, dict[str, int]] | None = None
    children: list["TieNode"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "teams": list(self.teams),
            "level": self.level,
            "leaf": self.leaf,
        }
        if not self.leaf:
            d["criterion"] = self.criterion
            d["split_used"] = self.split_used
            d["match_ids"] = list(self.match_ids)
            d["metrics"] = {t: dict(m) for t, m in self.metrics.items()}
            if self.fallback_metrics is not None:
                d["fallback_metrics"] = {
                    t: dict(m) for t, m in self.fallback_metrics.items()
                }
            d["children"] = [c.to_dict() for c in self.children]
        return d


def _mini_metrics(
    teams: Sequence[str],
    fixtures_by_id: Mapping[str, Mapping[str, Any]],
    played: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """计算给定球队集合内部相互 played 比赛的小表三指标。

    指标键：``mini_points`` / ``mini_gd`` / ``mini_gf``；同时返回实际使用的
    match_id 列表（ASCII 排序，保证解释稳定）。
    """
    members = set(teams)
    m = {t: {"mini_points": 0, "mini_gf": 0, "mini_ga": 0,
             "mini_gd": 0, "played_internal": 0} for t in teams}
    used: list[str] = []
    for mid, fx in fixtures_by_id.items():
        if mid not in played:
            continue
        home, away = fx["home"], fx["away"]
        if home not in members or away not in members:
            continue
        res = played[mid]
        sh, sa = res["score_home"], res["score_away"]
        used.append(mid)
        m[home]["mini_gf"] += sh
        m[home]["mini_ga"] += sa
        m[away]["mini_gf"] += sa
        m[away]["mini_ga"] += sh
        if sh > sa:
            m[home]["mini_points"] += WIN_POINTS
            m[home]["played_internal"] += 1
            m[away]["played_internal"] += 1
        elif sh < sa:
            m[away]["mini_points"] += WIN_POINTS
            m[home]["played_internal"] += 1
            m[away]["played_internal"] += 1
        else:
            m[home]["mini_points"] += DRAW_POINTS
            m[away]["mini_points"] += DRAW_POINTS
            m[home]["played_internal"] += 1
            m[away]["played_internal"] += 1
    for t in teams:
        m[t]["mini_gd"] = m[t]["mini_gf"] - m[t]["mini_ga"]
    return m, sorted(used)


def _resolve_group(
    teams: Sequence[str],
    level: int,
    fixtures_by_id: Mapping[str, Mapping[str, Any]],
    played: Mapping[str, Mapping[str, Any]],
    full_lines: Mapping[str, TeamLine],
) -> TieNode:
    """对一个总积分相同的球队集合递归判定唯一完整顺序。"""
    ordered = sorted(teams)  # 展示与兜底均以 ASCII 为稳定基线
    if len(ordered) == 1:
        return TieNode(teams=ordered, level=level, leaf=True)

    metrics, used_matches = _mini_metrics(ordered, fixtures_by_id, played)
    node = TieNode(teams=ordered, level=level, criterion="mini_table",
                   match_ids=used_matches,
                   metrics={t: {k: metrics[t][k] for k in
                                ("mini_points", "mini_gd", "mini_gf", "played_internal")}
                            for t in ordered})

    # 用三指标整体分组（三指标全相同的球队落同组），按指标降序排列各组。
    keyed: dict[tuple[int, int, int], list[str]] = {}
    for t in ordered:
        key = (metrics[t]["mini_points"], metrics[t]["mini_gd"], metrics[t]["mini_gf"])
        keyed.setdefault(key, []).append(t)
    ordered_keys = sorted(keyed, reverse=True)
    subgroups = [sorted(g) for g in (keyed[k] for k in ordered_keys)]

    if len(subgroups) > 1:
        # 真拆分：每个仍同值的子组必须用自身相互比赛重算后递归。
        node.split_used = True
        for grp in subgroups:
            if len(grp) == 1:
                node.children.append(TieNode(teams=grp, level=level + 1, leaf=True))
            else:
                node.children.append(
                    _resolve_group(grp, level + 1, fixtures_by_id, played, full_lines)
                )
        return node

    # 一次比较完全不能拆分：全表 GD/GF（请求轮次范围内），再 ASCII，终止。
    node.split_used = False
    node.criterion = "full_table_fallback"
    fallback = {
        t: {"full_gd": full_lines[t].goal_diff,
            "full_gf": full_lines[t].goals_for}
        for t in ordered
    }
    node.fallback_metrics = fallback
    final_order = sorted(
        ordered,
        key=lambda t: (-full_lines[t].goal_diff,
                       -full_lines[t].goals_for,
                       t),  # 字符串升序即 ASCII 顺序
    )
    node.children = [TieNode(teams=[t], level=level + 1, leaf=True)
                     for t in final_order]
    return node


def compute_standings(
    teams: Sequence[str],
    fixtures: Sequence[Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    penalties: Mapping[str, int],
    rounds: tuple[int, int] | Sequence[int],
    promotion_places: int = 0,
    relegation_places: int = 0,
) -> dict[str, Any]:
    """计算一次固定切面下的完整可解释排名。

    参数
    ----
    fixtures:
        某个真实（或故意断链的）赛程投影，每项含
        ``match_id/round/home/away/slot``。
    results:
        ``match_id -> {state, score_home, score_away, ...}``；只读取其中
        ``state == 'played'`` 且轮次在范围内的条目。
    penalties:
        切面下每队生效中的 penalty_points 总和（非负）。
    rounds:
        ``(from_round, through_round)`` 闭区间。
    """
    if not isinstance(rounds, tuple):
        from_r, through_r = rounds
    else:
        from_r, through_r = rounds
    if from_r < 1 or through_r < from_r:
        raise ValueError("轮次范围非法")
    n = len(teams)
    if promotion_places < 0 or relegation_places < 0 or \
            promotion_places + relegation_places > n:
        raise ValueError("promotion_places/relegation_places 非负且和不超过球队数")

    fixtures_by_id = {fx["match_id"]: fx for fx in fixtures}
    in_range = {mid: fx for mid, fx in fixtures_by_id.items()
                if from_r <= fx["round"] <= through_r}
    played: dict[str, dict[str, Any]] = {}
    for mid, fx in in_range.items():
        res = results.get(mid)
        if res is not None and res.get("state") == "played":
            played[mid] = res

    lines: dict[str, TeamLine] = {}
    for t in teams:
        lines[t] = TeamLine(team=t, played=0, wins=0, draws=0, losses=0,
                            goals_for=0, goals_against=0, goal_diff=0,
                            match_points=0,
                            penalty_points=int(penalties.get(t, 0)),
                            total_points=0)

    ignored: list[dict[str, Any]] = []
    for mid, fx in in_range.items():
        res = results.get(mid)
        if res is None or res.get("state") != "played":
            ignored.append({"match_id": mid,
                            "state": None if res is None else res.get("state"),
                            "reason": "not_played"})
            continue
        home, away = fx["home"], fx["away"]
        sh, sa = res["score_home"], res["score_away"]
        h, a = lines[home], lines[away]
        h.played += 1
        a.played += 1
        h.goals_for += sh
        h.goals_against += sa
        a.goals_for += sa
        a.goals_against += sh
        h.played_match_ids.append(mid)
        a.played_match_ids.append(mid)
        if sh > sa:
            h.wins += 1
            a.losses += 1
            h.match_points += WIN_POINTS
        elif sh < sa:
            a.wins += 1
            h.losses += 1
            a.match_points += WIN_POINTS
        else:
            h.draws += 1
            a.draws += 1
            h.match_points += DRAW_POINTS
            a.match_points += DRAW_POINTS

    for t in teams:
        ln = lines[t]
        ln.goal_diff = ln.goals_for - ln.goals_against
        ln.total_points = ln.match_points - ln.penalty_points

    # 总积分从高到低；同分集合进入递归小表。
    total_groups: dict[int, list[str]] = {}
    for t in teams:
        total_groups.setdefault(lines[t].total_points, []).append(t)

    tree_children: list[TieNode] = []
    for total in sorted(total_groups, reverse=True):
        grp = sorted(total_groups[total])
        if len(grp) == 1:
            tree_children.append(TieNode(teams=grp, level=1, leaf=True))
        else:
            tree_children.append(
                _resolve_group(grp, 1, fixtures_by_id, played, lines)
            )

    def flatten(nodes: Sequence[TieNode]) -> list[str]:
        out: list[str] = []
        for nd in nodes:
            if nd.leaf:
                out.extend(nd.teams)
            else:
                out.extend(flatten(nd.children))
        return out

    order = flatten(tree_children)
    ranked = []
    for pos, t in enumerate(order, start=1):
        ln = lines[t]
        ranked.append({
            "position": pos,
            "team": t,
            "played": ln.played,
            "wins": ln.wins,
            "draws": ln.draws,
            "losses": ln.losses,
            "goals_for": ln.goals_for,
            "goals_against": ln.goals_against,
            "goal_diff": ln.goal_diff,
            "match_points": ln.match_points,
            "penalty_points": ln.penalty_points,
            "total_points": ln.total_points,
            "played_match_ids": list(ln.played_match_ids),
        })

    promotion = order[:promotion_places]
    relegation = order[n - relegation_places:] if relegation_places else []
    return {
        "rounds": [from_r, through_r],
        "ranked": ranked,
        "order": order,
        "tie_tree": [n.to_dict() for n in tree_children],
        "promotion": {"places": promotion_places, "teams": promotion,
                      "evidence_positions": list(range(1, promotion_places + 1))},
        "relegation": {"places": relegation_places,
                       "teams": relegation,
                       "evidence_positions": list(range(n - relegation_places + 1, n + 1))},
        "ignored_matches": ignored,
        "all_teams_retained": sorted(teams),
    }
