"""赛程数据生产服务（SQLite 持久化）。

职责边界
--------
* 初始化时固定：球队集合、``match_id`` 集合以及每个 ``match_id`` 所属的 ``round``。
  这些是赛季的稳定骨架，发布新 edition 不会改动它们。
* 接受*候选完整赛程*，独立做全关系校验，逐条返回可定位的问题；全部条件成立才发布。
  不做逐场发布、不做自动排程搜索。
* 每次合法发布生成一个不可变 edition，固定全季主客与 slot 关系及当时的可用性规则
  （slot 容量、球队禁用集合）；旧 edition 之后仍可独立查询，修订只产生新 edition。
* 只服务赛程事实：轮次、双方、主客身份、slot。不含任何比分、积分或排名推断。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Iterable, Mapping, Sequence

REQUIRED_FIELDS = ("match_id", "round", "home", "away", "slot")
MAX_SLOT_KINDS = 10
ROLE_RUN_LIMIT = 3  # 不允许连续三轮同为 home 或同为 away


class CandidateRejected(ValueError):
    """候选赛程未通过校验，携带全部可定位问题，不产生任何发布副作用。"""

    def __init__(self, issues: Sequence["Issue"]):
        self.issues = list(issues)
        super().__init__(f"candidate rejected with {len(self.issues)} issue(s)")


@dataclass(frozen=True)
class Issue:
    """单条可定位的校验问题。

    ``code`` 为稳定机器码，``location`` 给出问题锚点（轮次/match_id/球队/slot 等），
    ``message`` 为人工可读说明。
    """

    code: str
    location: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ParsedRow:
    index: int
    match_id: Any
    round_no: Any
    home: Any
    away: Any
    slot: Any
    valid: bool


class ScheduleService:
    """基于单个 SQLite 文件的赛程服务，关闭重开后状态完整可复算。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        first_time = not os.path.exists(db_path)
        # check_same_thread=False 让调用方自行决定线程模型；本服务自身保持短事务。
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if first_time:
            self._create_schema()

    # ------------------------------------------------------------------ 基础结构
    def _create_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE team (
                team_id TEXT PRIMARY KEY,
                ord     INTEGER NOT NULL
            );
            -- 稳定骨架：match_id 与其 round 在初始化后固定
            CREATE TABLE match_registry (
                match_id TEXT PRIMARY KEY,
                round_no INTEGER NOT NULL,
                ord      INTEGER NOT NULL
            );
            -- 每轮声明的可用 slot 与容量
            CREATE TABLE round_slot (
                round_no INTEGER NOT NULL,
                slot     TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                PRIMARY KEY (round_no, slot)
            );
            -- 球队禁用 slot 集合
            CREATE TABLE team_blackout (
                team_id TEXT NOT NULL,
                slot    TEXT NOT NULL,
                PRIMARY KEY (team_id, slot)
            );
            -- 已发布 edition 及其可用性规则快照
            CREATE TABLE edition (
                edition_id       INTEGER PRIMARY KEY,
                created_at       TEXT NOT NULL,
                slot_snapshot    TEXT NOT NULL,
                blackout_snapshot TEXT NOT NULL
            );
            CREATE TABLE edition_match (
                edition_id INTEGER NOT NULL REFERENCES edition(edition_id),
                match_id   TEXT NOT NULL,
                round_no   INTEGER NOT NULL,
                home       TEXT NOT NULL,
                away       TEXT NOT NULL,
                slot       TEXT NOT NULL,
                PRIMARY KEY (edition_id, match_id)
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------ 初始化
    def initialize(
        self,
        teams: Sequence[str],
        match_registry: Mapping[str, int],
        round_slots: Mapping[int, Mapping[str, int]],
        blackouts: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        """固定球队、match_id/round 骨架与可用性规则。

        参数
        ----
        teams:
            4 或 6 支球队，ID 唯一。
        match_registry:
            ``match_id -> round``，round 必须落在 1..N-1。
        round_slots:
            ``round -> {slot: capacity}``；全赛季 slot 种类至多 10 个，容量为正整数。
        blackouts:
            ``team -> 不可用 slot 集合``。
        """
        issues: list[Issue] = []

        if not isinstance(teams, Sequence) or isinstance(teams, (str, bytes)):
            raise ValueError("teams must be a sequence of team ids")
        if len(teams) not in (4, 6):
            issues.append(Issue("bad_team_count", {"n": len(teams)},
                                "赛季必须有 4 或 6 支球队"))
        if len(set(teams)) != len(teams):
            dup = sorted({t for t in teams if list(teams).count(t) > 1})
            issues.append(Issue("duplicate_team_id", {"teams": dup}, "球队 ID 必须唯一"))
        team_set = set(teams)
        n_rounds = len(teams) - 1 if len(teams) in (4, 6) else None

        all_slots: set[str] = set()
        for r, slots in (round_slots or {}).items():
            if not isinstance(r, int) or r < 1 or (n_rounds is not None and r > n_rounds):
                issues.append(Issue("bad_round_decl", {"round": r}, "轮次声明超出 1..N-1"))
                continue
            if len(slots) > MAX_SLOT_KINDS:
                issues.append(Issue("too_many_slots", {"round": r, "count": len(slots)},
                                    "每轮 slot 种类至多 10 个"))
            for slot, cap in slots.items():
                if not isinstance(cap, int) or cap <= 0:
                    issues.append(Issue("bad_slot_capacity",
                                        {"round": r, "slot": slot, "capacity": cap},
                                        "slot 容量必须为正整数"))
                all_slots.add(slot)
        if len(all_slots) > MAX_SLOT_KINDS:
            issues.append(Issue("too_many_slot_kinds", {"slots": sorted(all_slots)},
                                "全赛季 slot 种类至多 10 个"))

        for mid, r in match_registry.items():
            if not isinstance(r, int) or r < 1 or (n_rounds is not None and r > n_rounds):
                issues.append(Issue("bad_registry_round",
                                    {"match_id": mid, "round": r},
                                    "match_registry 的 round 必须落在 1..N-1"))
        expected_matches = len(teams) * (len(teams) - 1) // 2 if n_rounds else 0
        if len(match_registry) != expected_matches:
            issues.append(Issue("bad_registry_size",
                                {"matches": len(match_registry), "expected": expected_matches},
                                "单循环需要 N*(N-1)/2 个 match_id"))

        for team, blk in (blackouts or {}).items():
            if team not in team_set:
                issues.append(Issue("blackout_unknown_team", {"team": team},
                                    "禁用声明引用了未知球队"))
            unknown = set(blk) - all_slots
            if unknown:
                issues.append(Issue("blackout_unknown_slot",
                                    {"team": team, "slots": sorted(unknown)},
                                    "禁用声明引用了未声明的 slot"))

        if self.is_initialized():
            existing = self.get_config()
            raise ValueError(
                f"数据库 {self.db_path} 已初始化，配置固定不可更改：{existing}"
            )
        if issues:
            raise CandidateRejected(issues)

        cur = self.conn.cursor()
        cur.execute("INSERT INTO meta(key, value) VALUES('n_teams', ?)", (str(len(teams)),))
        cur.executemany(
            "INSERT INTO team(team_id, ord) VALUES(?, ?)",
            [(t, i) for i, t in enumerate(teams)],
        )
        cur.executemany(
            "INSERT INTO match_registry(match_id, round_no, ord) VALUES(?, ?, ?)",
            [(mid, r, i) for i, (mid, r) in enumerate(match_registry.items())],
        )
        for r, slots in round_slots.items():
            for slot, cap in slots.items():
                cur.execute(
                    "INSERT INTO round_slot(round_no, slot, capacity) VALUES(?, ?, ?)",
                    (r, slot, cap),
                )
        for team, blk in (blackouts or {}).items():
            for slot in set(blk):
                cur.execute(
                    "INSERT INTO team_blackout(team_id, slot) VALUES(?, ?)",
                    (team, slot),
                )
        self.conn.commit()

    def is_initialized(self) -> bool:
        row = self.conn.execute("SELECT 1 FROM meta WHERE key='n_teams'").fetchone()
        return row is not None

    # ------------------------------------------------------------------ 只读配置
    def get_config(self) -> dict[str, Any]:
        teams = [r["team_id"] for r in
                 self.conn.execute("SELECT team_id FROM team ORDER BY ord")]
        registry = {r["match_id"]: r["round_no"] for r in
                    self.conn.execute("SELECT match_id, round_no FROM match_registry ORDER BY ord")}
        slots: dict[int, dict[str, int]] = {}
        for r in self.conn.execute("SELECT round_no, slot, capacity FROM round_slot ORDER BY round_no, slot"):
            slots.setdefault(r["round_no"], {})[r["slot"]] = r["capacity"]
        blackouts: dict[str, list[str]] = {}
        for r in self.conn.execute("SELECT team_id, slot FROM team_blackout ORDER BY team_id, slot"):
            blackouts.setdefault(r["team_id"], []).append(r["slot"])
        return {"teams": teams, "match_registry": registry,
                "round_slots": slots, "blackouts": blackouts}

    @property
    def teams(self) -> list[str]:
        return [r["team_id"] for r in
                self.conn.execute("SELECT team_id FROM team ORDER BY ord")]

    # ------------------------------------------------------------------ 候选校验
    def check_candidate(self, candidate: Sequence[Mapping[str, Any]]) -> list[Issue]:
        """对候选完整赛程做独立全关系校验，返回全部问题（空列表即合法可发布）。

        校验不依赖任何已发布 edition，也不触碰已公开数据。
        """
        cfg = self.get_config()
        teams: list[str] = cfg["teams"]
        team_set = set(teams)
        registry: dict[str, int] = cfg["match_registry"]
        round_slots: dict[int, dict[str, int]] = cfg["round_slots"]
        blackouts: dict[str, set[str]] = {t: set(s) for t, s in cfg["blackouts"].items()}
        issues: list[Issue] = []

        if not isinstance(candidate, Sequence):
            return [Issue("bad_candidate_shape", {}, "候选必须是比赛字典的序列")]

        parsed: list[_ParsedRow] = []
        seen_ids: dict[Any, int] = {}
        for i, row in enumerate(candidate):
            if not isinstance(row, Mapping):
                issues.append(Issue("malformed_row", {"index": i}, "每行必须为字典"))
                continue
            missing = [f for f in REQUIRED_FIELDS if f not in row]
            if missing:
                issues.append(Issue("malformed_row", {"index": i, "missing": missing},
                                    "缺少必需字段"))
                continue
            mid, rnd, home, away, slot = (row[f] for f in REQUIRED_FIELDS)
            row_ok = True

            if mid in seen_ids:
                issues.append(Issue("duplicate_match_id",
                                    {"match_id": mid, "indexes": [seen_ids[mid], i]},
                                    "同一 match_id 在候选中重复出现"))
            else:
                seen_ids[mid] = i
            if mid not in registry:
                issues.append(Issue("unknown_match_id", {"index": i, "match_id": mid},
                                    "match_id 不在初始化固定的集合中"))
                row_ok = False
            elif not isinstance(rnd, int) or registry[mid] != rnd:
                issues.append(Issue("round_mismatch",
                                    {"match_id": mid, "declared_round": rnd,
                                     "fixed_round": registry.get(mid)},
                                    "比赛的 round 必须与初始化固定值一致"))
                row_ok = False
            if home not in team_set:
                issues.append(Issue("unknown_team", {"index": i, "match_id": mid,
                                                     "field": "home", "team": home},
                                    "主队 ID 未知"))
                row_ok = False
            if away not in team_set:
                issues.append(Issue("unknown_team", {"index": i, "match_id": mid,
                                                     "field": "away", "team": away},
                                    "客队 ID 未知"))
                row_ok = False
            if home == away and home in team_set:
                issues.append(Issue("self_match", {"index": i, "match_id": mid, "team": home},
                                    "不允许自己对自己"))
                row_ok = False
            if isinstance(rnd, int) and rnd in round_slots:
                if slot not in round_slots[rnd]:
                    issues.append(Issue("slot_not_allowed",
                                        {"match_id": mid, "round": rnd, "slot": slot,
                                         "allowed": sorted(round_slots[rnd])},
                                        "比赛必须放在其轮次允许的 slot"))
                    # slot 合法性与结构有效性解耦：后续容量检查对未知 slot 有守卫
                else:
                    for side, team in (("home", home), ("away", away)):
                        if team in team_set and slot in blackouts.get(team, set()):
                            issues.append(Issue("team_blackout",
                                                {"match_id": mid, "round": rnd,
                                                 "slot": slot, "team": team, "role": side},
                                                f"球队 {team} 在该 slot 禁用"))
                            # 可用性问题不使行失去结构有效性，避免级联出整轮缺队等噪音
            elif isinstance(rnd, int):
                issues.append(Issue("round_not_declared",
                                    {"match_id": mid, "round": rnd},
                                    "该轮次没有任何 slot 声明"))
                row_ok = False
            parsed.append(_ParsedRow(i, mid, rnd, home, away, slot, row_ok))

        good = [p for p in parsed if p.valid]

        # --- 骨架完整性：固定 match_id 集合一条不能少
        present_ids = {p.match_id for p in good}
        for mid in registry:
            if mid not in present_ids:
                issues.append(Issue("missing_match_id", {"match_id": mid,
                                                          "fixed_round": registry[mid]},
                                    "固定 match_id 在候选中缺失"))

        # --- 单循环：每对球队恰好一次
        pair_rows: dict[frozenset[str], list[Any]] = {}
        for p in good:
            pair = frozenset((p.home, p.away))
            pair_rows.setdefault(pair, []).append(p.match_id)
        all_pairs = {frozenset(p) for p in combinations(teams, 2)}
        for pair, mids in pair_rows.items():
            if len(mids) > 1:
                issues.append(Issue("duplicate_pair",
                                    {"pair": sorted(pair), "match_ids": mids},
                                    "同一对球队比赛超过一次"))
        for pair in sorted(all_pairs, key=lambda x: sorted(x)):
            if pair not in pair_rows:
                issues.append(Issue("missing_pair", {"pair": sorted(pair)},
                                    "该对球队在候选中没有比赛"))

        # --- 每轮每队恰好出现一次
        by_round: dict[int, list[_ParsedRow]] = {}
        for p in good:
            by_round.setdefault(p.round_no, []).append(p)
        for rnd in sorted(round_slots):
            rows = by_round.get(rnd, [])
            appearances: dict[str, list[Any]] = {}
            for p in rows:
                appearances.setdefault(p.home, []).append(p.match_id)
                appearances.setdefault(p.away, []).append(p.match_id)
            for team in teams:
                count = len(appearances.get(team, []))
                if count == 0:
                    issues.append(Issue("round_missing_team",
                                        {"round": rnd, "team": team},
                                        "整轮中该球队未出现"))
                elif count > 1:
                    issues.append(Issue("round_team_twice",
                                        {"round": rnd, "team": team,
                                         "match_ids": appearances[team]},
                                        "同一轮该球队出现超过一次"))
            expected_games = len(teams) // 2
            if len(rows) != expected_games:
                issues.append(Issue("round_game_count",
                                    {"round": rnd, "games": len(rows),
                                     "expected": expected_games},
                                    f"每轮必须恰好 {expected_games} 场"))

        # --- slot 容量
        slot_use: dict[tuple[int, Any], list[Any]] = {}
        for p in good:
            slot_use.setdefault((p.round_no, p.slot), []).append(p.match_id)
        for (rnd, slot), mids in sorted(slot_use.items(), key=lambda x: (x[0][0], str(x[0][1]))):
            cap = round_slots.get(rnd, {}).get(slot)
            if cap is not None and len(mids) > cap:
                issues.append(Issue("slot_capacity_exceeded",
                                    {"round": rnd, "slot": slot, "used": len(mids),
                                     "capacity": cap, "match_ids": mids},
                                    f"slot 容量 {cap}，实际排入 {len(mids)} 场"))

        # --- 同队同 slot 不能重复：同一轮内同一球队不得在同一 slot 出现两次
        # （与“每轮每队恰好一次”互补的显式 slot 侧约束，独立给出可定位问题）
        team_slot_round: dict[tuple[str, Any, int], list[Any]] = {}
        for p in good:
            for team in (p.home, p.away):
                team_slot_round.setdefault((team, p.slot, p.round_no), []).append(p.match_id)
        for (team, slot, rnd), mids in sorted(
                team_slot_round.items(),
                key=lambda kv: (kv[0][0], str(kv[0][1]), kv[0][2])):
            if len(mids) > 1:
                issues.append(Issue("team_slot_repeat",
                                    {"team": team, "slot": slot, "round": rnd,
                                     "match_ids": mids},
                                    "同轮内同一球队在同一 slot 重复出现"))

        # --- 主客次数差至多 1
        home_count = {t: 0 for t in teams}
        away_count = {t: 0 for t in teams}
        for p in good:
            home_count[p.home] += 1
            away_count[p.away] += 1
        for team in teams:
            if abs(home_count[team] - away_count[team]) > 1:
                issues.append(Issue("home_away_imbalance",
                                    {"team": team, "home": home_count[team],
                                     "away": away_count[team]},
                                    "home/away 次数之差每队至多 1"))

        # --- 按 round 顺序不能连续三轮同角色
        roles: dict[str, dict[int, str]] = {t: {} for t in teams}
        for p in good:
            roles[p.home][p.round_no] = "home"
            roles[p.away][p.round_no] = "away"
        rounds = sorted(round_slots)
        for team in teams:
            seq = [roles[team].get(r) for r in rounds]
            for k in range(len(seq) - ROLE_RUN_LIMIT + 1):
                window = seq[k:k + ROLE_RUN_LIMIT]
                if window[0] in ("home", "away") and len(set(window)) == 1:
                    issues.append(Issue("three_consecutive_role",
                                        {"team": team, "role": window[0],
                                         "rounds": rounds[k:k + ROLE_RUN_LIMIT]},
                                        f"球队连续三轮同为 {window[0]}"))

        return issues

    # ------------------------------------------------------------------ 发布
    def publish(self, candidate: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """校验并原子发布一个新 edition；失败抛 :class:`CandidateRejected`。"""
        issues = self.check_candidate(candidate)
        if issues:
            raise CandidateRejected(issues)

        cfg = self.get_config()
        cur = self.conn.cursor()
        try:
            next_id = (cur.execute("SELECT COALESCE(MAX(edition_id), 0) + 1 FROM edition")
                       .fetchone()[0])
            cur.execute(
                "INSERT INTO edition(edition_id, created_at, slot_snapshot, blackout_snapshot)"
                " VALUES(?, ?, ?, ?)",
                (next_id, datetime.now(timezone.utc).isoformat(),
                 json.dumps(cfg["round_slots"], sort_keys=True),
                 json.dumps(cfg["blackouts"], sort_keys=True)),
            )
            cur.executemany(
                "INSERT INTO edition_match(edition_id, match_id, round_no, home, away, slot)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                [(next_id, r["match_id"], r["round"], r["home"], r["away"], r["slot"])
                 for r in candidate],
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return self.get_edition(next_id)

    # ------------------------------------------------------------------ 查询
    def _require_edition(self, edition_id: int) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM edition WHERE edition_id = ?", (edition_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"edition {edition_id} 不存在")

    def list_editions(self) -> list[dict[str, Any]]:
        out = []
        for r in self.conn.execute(
            "SELECT edition_id, created_at, slot_snapshot, blackout_snapshot"
            " FROM edition ORDER BY edition_id"
        ):
            out.append({
                "edition_id": r["edition_id"],
                "created_at": r["created_at"],
                "round_slots": json.loads(r["slot_snapshot"]),
                "blackouts": json.loads(r["blackout_snapshot"]),
            })
        return out

    def get_edition(self, edition_id: int) -> dict[str, Any]:
        self._require_edition(edition_id)
        head = self.conn.execute(
            "SELECT edition_id, created_at, slot_snapshot, blackout_snapshot"
            " FROM edition WHERE edition_id = ?", (edition_id,)
        ).fetchone()
        matches = [dict(r) for r in self.conn.execute(
            "SELECT match_id, round_no AS round, home, away, slot"
            " FROM edition_match WHERE edition_id = ? ORDER BY round_no, match_id",
            (edition_id,),
        )]
        return {
            "edition_id": head["edition_id"],
            "created_at": head["created_at"],
            "round_slots": json.loads(head["slot_snapshot"]),
            "blackouts": json.loads(head["blackout_snapshot"]),
            "matches": matches,
        }

    def query_round(self, edition_id: int, round_no: int) -> list[dict[str, Any]]:
        """按轮查询：返回该轮全部比赛（不依赖比分/排名）。"""
        self._require_edition(edition_id)
        return [dict(r) for r in self.conn.execute(
            "SELECT match_id, round_no AS round, home, away, slot"
            " FROM edition_match WHERE edition_id = ? AND round_no = ?"
            " ORDER BY match_id",
            (edition_id, round_no),
        )]

    def query_team(self, edition_id: int, team: str) -> list[dict[str, Any]]:
        """按球队查询：返回该队全季比赛，并显式标注主客身份，按轮排序。"""
        self._require_edition(edition_id)
        if team not in set(self.teams):
            raise KeyError(f"未知球队 {team}")
        rows = [dict(r) for r in self.conn.execute(
            "SELECT match_id, round_no AS round, home, away, slot"
            " FROM edition_match WHERE edition_id = ?"
            " AND (home = ? OR away = ?) ORDER BY round_no, match_id",
            (edition_id, team, team),
        )]
        for r in rows:
            r["role"] = "home" if r["home"] == team else "away"
            r["opponent"] = r["away"] if r["role"] == "home" else r["home"]
        return rows

    def locate_match(self, edition_id: int, match_id: str) -> dict[str, Any]:
        """定位某场比赛的轮次、真实双方与主客身份、slot。"""
        self._require_edition(edition_id)
        row = self.conn.execute(
            "SELECT match_id, round_no AS round, home, away, slot"
            " FROM edition_match WHERE edition_id = ? AND match_id = ?",
            (edition_id, match_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"edition {edition_id} 中没有 match_id={match_id}")
        return dict(row)

    def inspect(self) -> dict[str, Any]:
        """公开赛程仓全部状态：固定骨架、可用性规则与各 edition 概要。"""
        return {
            "config": self.get_config(),
            "editions": [
                {**e, "match_count": self.conn.execute(
                    "SELECT COUNT(*) AS c FROM edition_match"
                    " WHERE edition_id = ?", (e["edition_id"],)).fetchone()["c"]}
                for e in self.list_editions()
            ],
        }

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ScheduleService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
