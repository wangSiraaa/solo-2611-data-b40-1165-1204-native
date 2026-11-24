"""赛季排名仓：比分事实与判罚事实的持久化、原子批处理与排名切面。

与赛程服务的关系
================
排名仓持有独立的 SQLite 文件，通过只读方式打开赛程库读取固定骨架
（球队、match_id、round）与已发布 edition。赛程服务不会被写入，
也不允许从赛程本身推断任何积分或资格。

事实模型
========
* 每条比分事实以 ``result_id`` 唯一，引用赛程库的 ``match_id``；
  ``state`` 为 played/cancelled/pending，仅 played 携带 0..100 整数比分。
* 可先 pending 后确认；不同有效接收批可对**既有 result_id 完整替换**，
  按已提交接收 seq 顺序生效。同 request_id 同文重试复读、异文拒绝。
* 每批内 result_id 不得重复；同一个 match_id 最多一个有效结果身份，
  重发的新 ID 不会被当成另一场比赛。
* 判罚为按球队的非负 penalty_points（0..9），有 apply/revoke 记录；
  判罚不改比分或赛程，只在总积分扣减。
* 两类批次都原子：任何一条不合法，整批不进入已公开数据。
* 每次排名请求固定一个赛程 edition、一个事实切面和一组轮次范围，
  结果落快照；之后的修改不改写旧快照。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .ranking import compute_standings

VALID_STATES = ("played", "cancelled", "pending")


class BatchRejected(ValueError):
    """接收批未通过校验；携带原因列表，整批无副作用。"""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = list(reasons)
        super().__init__(f"batch rejected: {self.reasons}")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _content_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class StandingsRepository:
    def __init__(self, repo_db_path: str, schedule_db_path: str):
        self.repo_db_path = repo_db_path
        self.schedule_db_path = schedule_db_path
        first_time = not os.path.exists(repo_db_path)
        self.conn = sqlite3.connect(repo_db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # 只读、不可写的赛程库连接，保证两个仓的写入边界互不污染。
        uri = "file:" + os.path.abspath(schedule_db_path) + "?mode=ro"
        self.sched = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.sched.row_factory = sqlite3.Row
        if first_time:
            self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE request_log (
                request_id   TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                seq          INTEGER NOT NULL,
                applied_at   TEXT NOT NULL
            );
            -- 全部历史行保留，active=1 为当前生效身份；同 match 至多一个 active
            CREATE TABLE result (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id    TEXT NOT NULL,
                match_id     TEXT NOT NULL,
                state        TEXT NOT NULL,
                score_home   INTEGER,
                score_away   INTEGER,
                active       INTEGER NOT NULL,
                request_id   TEXT NOT NULL,
                seq          INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX ux_active_result_id ON result(result_id) WHERE active = 1;
            CREATE UNIQUE INDEX ux_active_match ON result(match_id) WHERE active = 1;
            -- 判罚流水：apply 增加、revoke 撤销该队当前全部生效判罚
            CREATE TABLE penalty_event (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                team       TEXT NOT NULL,
                action     TEXT NOT NULL CHECK(action IN ('apply', 'revoke')),
                points     INTEGER,
                request_id TEXT NOT NULL,
                seq        INTEGER NOT NULL
            );
            CREATE TABLE penalty_state (
                team   TEXT PRIMARY KEY,
                points INTEGER NOT NULL
            );
            -- 排名请求快照：固定 edition + 事实切面 + 轮次范围
            CREATE TABLE standings_snapshot (
                snapshot_id INTEGER PRIMARY KEY,
                request_id  TEXT NOT NULL,
                edition_id  INTEGER NOT NULL,
                seq_floor   INTEGER NOT NULL,
                round_from  INTEGER NOT NULL,
                round_through INTEGER NOT NULL,
                promotion_places INTEGER NOT NULL,
                relegation_places INTEGER NOT NULL,
                payload     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------ 赛程骨架只读
    def _sched_teams(self) -> list[str]:
        return [r["team_id"] for r in
                self.sched.execute("SELECT team_id FROM team ORDER BY ord")]

    def _sched_registry(self) -> dict[str, int]:
        return {r["match_id"]: r["round_no"] for r in
                self.sched.execute("SELECT match_id, round_no FROM match_registry")}

    def _edition_fixtures(self, edition_id: int) -> list[dict[str, Any]]:
        row = self.sched.execute(
            "SELECT 1 FROM edition WHERE edition_id = ?", (edition_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"赛程 edition {edition_id} 不存在或未发布")
        return [dict(r) for r in self.sched.execute(
            "SELECT match_id, round_no AS round, home, away, slot"
            " FROM edition_match WHERE edition_id = ? ORDER BY round_no, match_id",
            (edition_id,),
        )]

    # ------------------------------------------------------------ 幂等/序号
    def _check_idempotent(self, request_id: str, kind: str,
                          payload: Any) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT kind, content_hash, seq FROM request_log WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        digest = _content_hash(payload)
        if row["kind"] != kind or row["content_hash"] != digest:
            raise BatchRejected([
                f"request_id={request_id} 已以不同内容/类型接收"
                f"（已存 kind={row['kind']}），同号异文拒绝"
            ])
        return {"request_id": request_id, "kind": kind,
                "seq": row["seq"], "replayed": True}

    def _next_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM request_log").fetchone()
        return int(row["s"])

    # ------------------------------------------------------------ 比分批次
    def apply_result_batch(
        self,
        request_id: str,
        entries: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """原子接收一批比分事实。返回接收回执（含 seq，重试时 replayed=True）。"""
        replay = self._check_idempotent(request_id, "results", list(entries))
        if replay is not None:
            return replay

        reasons: list[str] = []
        registry = self._sched_registry()

        seen_ids: set[str] = set()
        for i, e in enumerate(entries):
            rid = e.get("result_id")
            mid = e.get("match_id")
            state = e.get("state")
            if not rid or not isinstance(rid, str):
                reasons.append(f"entries[{i}] result_id 缺失或非字符串")
                continue
            if rid in seen_ids:
                reasons.append(f"entries[{i}] result_id={rid} 批内重复")
            seen_ids.add(rid)
            if mid not in registry:
                reasons.append(f"entries[{i}] result_id={rid} 引用未知 match_id={mid}")
            if state not in VALID_STATES:
                reasons.append(f"entries[{i}] result_id={rid} state={state!r} 非法")
                continue
            sh, sa = e.get("score_home"), e.get("score_away")
            if state == "played":
                for name, val in (("score_home", sh), ("score_away", sa)):
                    if not isinstance(val, int) or isinstance(val, bool) or not (0 <= val <= 100):
                        reasons.append(
                            f"entries[{i}] result_id={rid} {name}={val!r} 必须是 0..100 整数")
            else:
                if sh is not None or sa is not None:
                    reasons.append(
                        f"entries[{i}] result_id={rid} {state} 状态不允许携带比分")

        # 身份冲突：同 match 已有不同 active result_id（跨批校验）；
        # 以及既有 result_id 不得换绑到另一个 match_id
        for e in entries:
            rid, mid = e.get("result_id"), e.get("match_id")
            if not rid or mid not in registry:
                continue
            row = self.conn.execute(
                "SELECT result_id, match_id FROM result"
                " WHERE match_id = ? AND active = 1",
                (mid,),
            ).fetchone()
            if row is not None and row["result_id"] != rid:
                reasons.append(
                    f"match_id={mid} 已有有效结果身份 {row['result_id']}，"
                    f"新 result_id={rid} 不能作为另一场比赛")
            bound = self.conn.execute(
                "SELECT match_id FROM result WHERE result_id = ? AND active = 1",
                (rid,),
            ).fetchone()
            if bound is not None and bound["match_id"] != mid:
                reasons.append(
                    f"result_id={rid} 已绑定 match_id={bound['match_id']}，"
                    f"完整替换不得换绑到 {mid}")

        if reasons:
            raise BatchRejected(reasons)

        seq = self._next_seq()
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.cursor()
        try:
            cur.execute(
                "INSERT INTO request_log(request_id, kind, content_hash, seq, applied_at)"
                " VALUES(?, 'results', ?, ?, ?)",
                (request_id, _content_hash(list(entries)), seq, now),
            )
            for e in entries:
                rid, mid, state = e["result_id"], e["match_id"], e["state"]
                # 既有同 result_id 完整替换：旧身份失效，新行按本 seq 生效
                cur.execute("UPDATE result SET active = 0 WHERE result_id = ?", (rid,))
                # 若该 match 的旧身份属于本批替换之外的情况，上面身份冲突已拦截；
                # 同 result_id 行本身也必然绑定同一 match。
                cur.execute(
                    "INSERT INTO result(result_id, match_id, state, score_home,"
                    " score_away, active, request_id, seq)"
                    " VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
                    (rid, mid, state,
                     e.get("score_home") if state == "played" else None,
                     e.get("score_away") if state == "played" else None,
                     request_id, seq),
                )
            self.conn.commit()
        except sqlite3.Error as exc:  # 兜底原子性
            self.conn.rollback()
            raise BatchRejected([f"数据库错误，整批回滚: {exc}"]) from exc
        return {"request_id": request_id, "kind": "results",
                "seq": seq, "replayed": False, "entries": len(entries)}

    # ------------------------------------------------------------ 判罚批次
    def apply_penalty_batch(
        self,
        request_id: str,
        events: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """原子接收一批判罚事件（apply/revoke）。"""
        replay = self._check_idempotent(request_id, "penalties", list(events))
        if replay is not None:
            return replay

        reasons: list[str] = []
        teams = set(self._sched_teams())
        current = self.active_penalties()
        projected = dict(current)
        for i, ev in enumerate(events):
            team, action = ev.get("team"), ev.get("action")
            pts = ev.get("points")
            if team not in teams:
                reasons.append(f"events[{i}] 未知球队 {team!r}")
                continue
            if action == "apply":
                if not isinstance(pts, int) or isinstance(pts, bool) or not (0 <= pts <= 9):
                    reasons.append(
                        f"events[{i}] team={team} penalty_points={pts!r} 必须是 0..9 整数")
                    continue
                if projected.get(team, 0) + pts > 9:
                    reasons.append(
                        f"events[{i}] team={team} 判罚累计将达 "
                        f"{projected.get(team, 0) + pts}，超过单队上限 9")
                    continue
                projected[team] = projected.get(team, 0) + pts
            elif action == "revoke":
                # 撤销当前全部生效判罚；对本就为 0 的球队记一条零影响流水
                projected[team] = 0
            else:
                reasons.append(f"events[{i}] team={team} action={action!r} 非法")

        if reasons:
            raise BatchRejected(reasons)

        seq = self._next_seq()
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.cursor()
        try:
            cur.execute(
                "INSERT INTO request_log(request_id, kind, content_hash, seq, applied_at)"
                " VALUES(?, 'penalties', ?, ?, ?)",
                (request_id, _content_hash(list(events)), seq, now),
            )
            for ev in events:
                cur.execute(
                    "INSERT INTO penalty_event(team, action, points, request_id, seq)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (ev["team"], ev["action"], ev.get("points"), request_id, seq),
                )
                if ev["action"] == "apply":
                    cur.execute(
                        "INSERT INTO penalty_state(team, points) VALUES(?, ?)"
                        " ON CONFLICT(team) DO UPDATE SET points = points + excluded.points",
                        (ev["team"], ev["points"]),
                    )
                else:
                    cur.execute(
                        "INSERT INTO penalty_state(team, points) VALUES(?, 0)"
                        " ON CONFLICT(team) DO UPDATE SET points = 0",
                        (ev["team"],),
                    )
            self.conn.commit()
        except sqlite3.Error as exc:
            self.conn.rollback()
            raise BatchRejected([f"数据库错误，整批回滚: {exc}"]) from exc
        return {"request_id": request_id, "kind": "penalties",
                "seq": seq, "replayed": False, "events": len(events)}

    # ------------------------------------------------------------ 当前切面读取
    def active_results(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in self.conn.execute(
            "SELECT result_id, match_id, state, score_home, score_away, request_id, seq"
            " FROM result WHERE active = 1 ORDER BY seq, id"
        ):
            out[r["match_id"]] = {
                "result_id": r["result_id"], "match_id": r["match_id"],
                "state": r["state"], "score_home": r["score_home"],
                "score_away": r["score_away"], "request_id": r["request_id"],
                "seq": r["seq"],
            }
        return out

    def active_penalties(self) -> dict[str, int]:
        return {r["team"]: r["points"] for r in
                self.conn.execute("SELECT team, points FROM penalty_state")}

    def penalty_ledger(self, team: str | None = None) -> list[dict[str, Any]]:
        if team is None:
            rows = self.conn.execute(
                "SELECT team, action, points, request_id, seq FROM penalty_event"
                " ORDER BY seq, id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT team, action, points, request_id, seq FROM penalty_event"
                " WHERE team = ? ORDER BY seq, id", (team,)).fetchall()
        return [dict(r) for r in rows]

    def request_log(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT request_id, kind, content_hash, seq, applied_at"
            " FROM request_log ORDER BY seq")]

    # ------------------------------------------------------------ 排名与快照
    def standings(
        self,
        request_id: str,
        edition_id: int,
        rounds: tuple[int, int],
        promotion_places: int = 0,
        relegation_places: int = 0,
    ) -> dict[str, Any]:
        """固定 edition/事实切面/轮次范围计算排名并持久化快照。"""
        from_r, through_r = rounds
        fixtures = self._edition_fixtures(edition_id)
        results = self.active_results()
        penalties = self.active_penalties()
        teams = self._sched_teams()
        seq_floor = int(self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM request_log").fetchone()["s"])

        payload = compute_standings(
            teams=teams,
            fixtures=fixtures,
            results=results,
            penalties=penalties,
            rounds=(from_r, through_r),
            promotion_places=promotion_places,
            relegation_places=relegation_places,
        )
        payload["request_id"] = request_id
        payload["edition_id"] = edition_id
        payload["seq_floor"] = seq_floor
        payload["promotion_places"] = promotion_places
        payload["relegation_places"] = relegation_places

        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO standings_snapshot(request_id, edition_id, seq_floor,"
            " round_from, round_through, promotion_places, relegation_places,"
            " payload, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, edition_id, seq_floor, from_r, through_r,
             promotion_places, relegation_places,
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             datetime.now(timezone.utc).isoformat()),
        )
        snapshot_id = cur.lastrowid
        self.conn.commit()
        payload["snapshot_id"] = snapshot_id
        return payload

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT payload FROM standings_snapshot WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"snapshot {snapshot_id} 不存在")
        return json.loads(row["payload"])

    def list_snapshots(self) -> list[dict[str, Any]]:
        return [{"snapshot_id": r["snapshot_id"], "request_id": r["request_id"],
                 "edition_id": r["edition_id"], "seq_floor": r["seq_floor"],
                 "rounds": [r["round_from"], r["round_through"]],
                 "promotion_places": r["promotion_places"],
                 "relegation_places": r["relegation_places"],
                 "created_at": r["created_at"]}
                for r in self.conn.execute(
                    "SELECT snapshot_id, request_id, edition_id, seq_floor,"
                    " round_from, round_through, promotion_places,"
                    " relegation_places, created_at"
                    " FROM standings_snapshot ORDER BY snapshot_id")]

    def inspect(self) -> dict[str, Any]:
        """公开当前仓内全部可解释状态，供赛务/统计核对。"""
        return {
            "teams": self._sched_teams(),
            "requests": self.request_log(),
            "active_results": self.active_results(),
            "active_penalties": self.active_penalties(),
            "penalty_ledger": self.penalty_ledger(),
            "snapshots": self.list_snapshots(),
        }

    def close(self) -> None:
        self.conn.close()
        self.sched.close()

    def __enter__(self) -> "StandingsRepository":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
