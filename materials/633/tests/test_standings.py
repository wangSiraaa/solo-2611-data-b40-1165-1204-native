"""排名引擎与排名仓：规则场景、原子批/幂等/身份冲突、快照与持久化、oracle 对照。"""

from __future__ import annotations

import os
import tempfile
import unittest

from league.ranking import compute_standings
from league.standings_repo import StandingsRepository, BatchRejected
from league.schedule_service import ScheduleService

from tests.fixtures import (
    TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY, FIXTURE4,
    mutate,
)
from oracle import oracle_standings


def res(mid, sh, sa, rid=None):
    return {"result_id": rid or f"r-{mid}", "match_id": mid,
            "state": "played", "score_home": sh, "score_away": sa}


class TestRankingEngineScenarios(unittest.TestCase):
    # 全部场景共用 FIXTURE4 的对阵与主客；只改结果/判罚/范围。

    def test_draw_and_basic_points(self):
        # ab 1:1 平 -> A、B 各 1 分；cd 2:0 -> C 3 分
        results = {"ab": {**res("ab", 1, 1)}, "cd": {**res("cd", 2, 0)}}
        out = compute_standings(TEAMS4, FIXTURE4, results, {}, (1, 3))
        by = {r["team"]: r for r in out["ranked"]}
        self.assertEqual(by["A"]["match_points"], 1)
        self.assertEqual(by["B"]["match_points"], 1)
        self.assertEqual(by["C"]["match_points"], 3)
        self.assertEqual(by["D"]["match_points"], 0)
        # 0 场球队仍保留一行
        self.assertEqual([r["team"] for r in out["ranked"] if r["played"] == 0], [])
        # 本轮只踢了 2 场，C、D 之后无比赛，A/B 也可能 0 场：此处 A B C D 都出现
        self.assertEqual(sorted(r["team"] for r in out["ranked"]), TEAMS4)

    def test_cancelled_and_pending_are_not_zero_zero(self):
        results = {
            "ab": {"result_id": "r-ab", "match_id": "ab", "state": "cancelled"},
            "cd": {"result_id": "r-cd", "match_id": "cd", "state": "pending"},
        }
        out = compute_standings(TEAMS4, FIXTURE4, results, {}, (1, 3))
        self.assertTrue(all(r["played"] == 0 for r in out["ranked"]))
        self.assertTrue(all(r["goals_for"] == 0 for r in out["ranked"]))
        ignored = {x["match_id"]: x["state"] for x in out["ignored_matches"]
                   if x["match_id"] in ("ab", "cd")}
        self.assertEqual(ignored, {"ab": "cancelled", "cd": "pending"})

    def test_home_away_flip_changes_attribution(self):
        # 同一条比分事实 1:0，主客翻转后积分归属相反
        results = {"ab": res("ab", 1, 0)}
        before = compute_standings(TEAMS4, FIXTURE4, results, {}, (1, 3))
        flipped = mutate(FIXTURE4, 0, home="B", away="A")
        after = compute_standings(TEAMS4, flipped, results, {}, (1, 3))
        b1 = {r["team"]: r for r in before["ranked"]}
        b2 = {r["team"]: r for r in after["ranked"]}
        self.assertEqual(b1["A"]["match_points"], 3)
        self.assertEqual(b1["B"]["match_points"], 0)
        self.assertEqual(b2["B"]["match_points"], 3)
        self.assertEqual(b2["A"]["match_points"], 0)
        self.assertEqual(b2["A"]["goals_for"], 0)
        self.assertEqual(b2["A"]["goals_against"], 1)

    def test_penalty_only_changes_total_points(self):
        results = {"cd": res("cd", 1, 0)}  # C=3, D=0
        out0 = compute_standings(TEAMS4, FIXTURE4, results, {}, (1, 3))
        out1 = compute_standings(TEAMS4, FIXTURE4, results, {"C": 3}, (1, 3))
        c0 = next(r for r in out0["ranked"] if r["team"] == "C")
        c1 = next(r for r in out1["ranked"] if r["team"] == "C")
        self.assertEqual(c0["match_points"], c1["match_points"], 3)
        self.assertEqual(c0["total_points"], 3)
        self.assertEqual(c1["total_points"], 0)  # 判罚只作用总积分
        # 小表解释中不出现判罚扣减
        node = next(n for n in out1["tie_tree"]
                    if "C" in n["teams"] and not n["leaf"])
        for t, m in node["metrics"].items():
            self.assertNotIn("penalty", str(m).lower())

    def test_negative_total_points_possible(self):
        results = {"ab": {"result_id": "x", "match_id": "ab", "state": "cancelled"}}
        out = compute_standings(TEAMS4, FIXTURE4, results, {"B": 9}, (1, 3))
        b = next(r for r in out["ranked"] if r["team"] == "B")
        self.assertEqual(b["match_points"], 0)
        self.assertEqual(b["total_points"], -9)

    def test_three_way_tie_split_by_mini_table_metrics(self):
        # 4 队域里三队同分集合只可能拆成 1+1+1 或保持 3（递归子组重算见 6 队场景）。
        # 本用例显式手算三队同分时小表三指标的整体分组，期望常量直接写死在断言里。
        #
        # 固定主客（FIXTURE4）：ab A 主；ac C 主；bc B 主。
        # 目标总积分 A=B=C=3、D=9：
        #   ab 1:0  A 胜 B      -> A+3
        #   ac 2:0  C 胜 A      -> C+3
        #   ad 1:0  D 胜 A      -> D+3（A 总积分停在 3）
        #   bc 1:0  B 胜 C      -> B+3
        #   bd 0:2  D 胜 B      -> D+3（B 停在 3）
        #   cd 0:1  D 客胜 C    -> D+3（C 停在 3）
        results = {
            "ab": res("ab", 1, 0),
            "ac": res("ac", 2, 0),
            "ad": res("ad", 1, 0),
            "bc": res("bc", 1, 0),
            "bd": res("bd", 0, 2),
            "cd": res("cd", 0, 1),
        }
        # 手算 {A,B,C} 内部小表（只取 ab/ac/bc 三场）：
        #   A: ab 进1失0；ac 进0失2 -> pts=3, GD=-1, GF=1
        #   B: ab 进0失1；bc 进1失0 -> pts=3, GD= 0, GF=1
        #   C: ac 进2失0；bc 进0失1 -> pts=3, GD=+1, GF=2
        expected_metrics = {
            "A": {"mini_points": 3, "mini_gd": -1, "mini_gf": 1},
            "B": {"mini_points": 3, "mini_gd": 0, "mini_gf": 1},
            "C": {"mini_points": 3, "mini_gd": 1, "mini_gf": 2},
        }
        out = compute_standings(TEAMS4, FIXTURE4, results, {}, (1, 3))
        totals = {r["team"]: r["total_points"] for r in out["ranked"]}
        self.assertEqual({t: totals[t] for t in "ABC"}, {"A": 3, "B": 3, "C": 3})
        self.assertEqual(totals["D"], 9)
        node = next(n for n in out["tie_tree"] if set(n["teams"]) == {"A", "B", "C"})
        self.assertTrue(node["split_used"])
        self.assertEqual(sorted(node["match_ids"]), ["ab", "ac", "bc"])
        for t, want in expected_metrics.items():
            self.assertEqual({k: node["metrics"][t][k] for k in want}, want, t)
        # 三个不同指标键 -> 全拆成叶子，顺序 C > B > A（GD 降序）
        self.assertTrue(all(c["leaf"] for c in node["children"]))
        self.assertEqual([c["teams"][0] for c in node["children"]], ["C", "B", "A"])

    def test_fallback_full_table_and_ascii(self):
        # 三队总积分相同且相互比赛三指标完全同值 -> 全表 GD/GF，再 ASCII
        results = {
            "ab": res("ab", 1, 1),
            "ac": res("ac", 1, 1),
            "bc": res("bc", 1, 1),
        }
        # A/B/C 相互三场全平：各 2 小表分、GD0、GF2；对 D 的比赛缺席。
        out = compute_standings(TEAMS4, FIXTURE4, results, {}, (1, 3))
        node = next(n for n in out["tie_tree"] if set(n["teams"]) == {"A", "B", "C"})
        self.assertFalse(node["split_used"])
        self.assertEqual(node["criterion"], "full_table_fallback")
        # 全表也全同 -> ASCII 终止
        self.assertEqual([c["teams"][0] for c in node["children"]],
                         ["A", "B", "C"])

    def test_round_range_isolation(self):
        results = {m: res(m, 1, 0) for m in ("ab", "cd", "ac", "bd", "ad", "bc")}
        r1 = compute_standings(TEAMS4, FIXTURE4, results, {}, (1, 1))
        self.assertTrue(all(r["played"] == 1 for r in r1["ranked"]))
        ignored = {x["match_id"] for x in r1["ignored_matches"]}
        # 范围外比赛不出现（既不计分也不进 ignored，ignored 只含范围内非 played）
        self.assertEqual(ignored, set())

    def test_promotion_relegation_evidence(self):
        results = {m: res(m, 1, 0) for m in ("ab", "cd", "ac", "bd", "ad", "bc")}
        out = compute_standings(TEAMS4, FIXTURE4, results, {}, (1, 3),
                                promotion_places=1, relegation_places=1)
        self.assertEqual(out["promotion"]["teams"], [out["order"][0]])
        self.assertEqual(out["relegation"]["teams"], [out["order"][-1]])
        self.assertEqual(out["promotion"]["evidence_positions"], [1])
        self.assertEqual(out["relegation"]["evidence_positions"], [4])
        with self.assertRaises(ValueError):
            compute_standings(TEAMS4, FIXTURE4, results, {}, (1, 3),
                              promotion_places=3, relegation_places=2)


class TestRecursiveSubgroupSixTeams(unittest.TestCase):
    """显式构造 4 队同分集合，小表拆成 2+2，两个 2 队子组各自重算。"""

    def setUp(self):
        self.teams = ["P", "Q", "R", "S", "T", "U"]
        # 直接给出一个合法对阵骨架（不经过赛程服务，引擎只需要 fixture 行）
        reg = {}
        pairs = [
            (1, "P", "Q"), (1, "R", "S"), (1, "T", "U"),
            (2, "P", "R"), (2, "Q", "T"), (2, "S", "U"),
            (3, "P", "S"), (3, "Q", "U"), (3, "R", "T"),
            (4, "P", "T"), (4, "Q", "S"), (4, "R", "U"),
            (5, "P", "U"), (5, "Q", "R"), (5, "S", "T"),
        ]
        self.fixtures = []
        for r, h, a in pairs:
            mid = f"{h.lower()}{a.lower()}"
            reg[mid] = r
            self.fixtures.append({"match_id": mid, "round": r,
                                  "home": h, "away": a, "slot": "s"})

    def test_split_two_plus_two_and_recompute(self):
        # 目标：P Q R S 总积分相同；其内部小表把 {P,Q} 与 {R,S} 拆成两个真子组，
        # 每个子组 2 队、自身相互比赛同值 -> 递归层各自 fallback 全表后 ASCII。
        # 内部对阵（主客见 fixtures）：pq, pr, ps, qr, qs, rs
        # 设计小表：
        #   {P,Q} 子组内部：pq 打平；{R,S}：rs 打平
        #   跨子组：P/Q 对 R/S 整体让 P,Q 小表占优（赢跨组战），形成 2+2 拆分
        results = {}
        # 子组内平
        results["pq"] = res("pq", 1, 1)
        results["rs"] = res("rs", 2, 2)
        # 跨组：P、Q 各自战胜 R、S（主客按 fixture）
        # pr/ps: P 主；qr: Q 主（r5 Q-R）；qs: Q 主（r4 Q-S）
        results["pr"] = res("pr", 2, 0)
        results["ps"] = res("ps", 2, 0)
        results["qr"] = res("qr", 2, 0)
        results["qs"] = res("qs", 2, 0)
        # 内部积分现状：P=7、Q=7、R=1、S=1。
        # 外部比赛（对 T/U，不进四队小表）：R、S 两胜补 6 分到 7；P、Q 两负保持 7。
        # 同时让 T、U 自身不落在 7 分：T 赢 P、Q 且平 U -> 10；U 赢 P、Q、负 R、S -> 3。
        results["pt"] = res("pt", 0, 1)   # P 主负 T
        results["pu"] = res("pu", 0, 1)   # P 主负 U
        results["qt"] = res("qt", 0, 1)   # Q 主负 T
        results["qu"] = res("qu", 0, 1)   # Q 主负 U
        results["rt"] = res("rt", 1, 0)   # R 主胜 T
        results["ru"] = res("ru", 1, 0)   # R 主胜 U
        results["st"] = res("st", 1, 0)   # r5 S 主 T，S 胜
        results["su"] = res("su", 1, 0)   # r2 S 主 U，S 胜
        results["tu"] = res("tu", 5, 0)   # T 客胜 U：T=9、U=6，同分集合恰为 {P,Q,R,S}
        # 总积分：P=7 Q=7 R=1+6=7 S=1+6=7；T=9、U=6
        out = compute_standings(self.teams, self.fixtures, results, {}, (1, 5))
        totals = {r["team"]: r["total_points"] for r in out["ranked"]}
        for t in "PQRS":
            self.assertEqual(totals[t], 7, t)
        node = next(n for n in out["tie_tree"]
                    if set(n["teams"]) == {"P", "Q", "R", "S"})
        # 小表整体分组：P/Q 小表 7 分，R/S 小表 1 分 -> 拆成两个 2 队真子组
        self.assertTrue(node["split_used"])
        self.assertEqual(len(node["children"]), 2)
        top, bottom = node["children"]
        self.assertEqual(top["teams"], ["P", "Q"])
        self.assertEqual(bottom["teams"], ["R", "S"])
        # 子组必须用自身相互比赛重算：各自只有一场内部平局，三指标同值，
        # 不得沿用大集合小表（大集合里 P 有 7 分而此处只有 1 分）
        for child in (top, bottom):
            self.assertFalse(child["split_used"])
            self.assertEqual(child["criterion"], "full_table_fallback")
            self.assertEqual(len(child["match_ids"]), 1)
            for t, m in child["metrics"].items():
                self.assertEqual(m["mini_points"], 1)
        self.assertEqual(top["match_ids"], ["pq"])
        self.assertEqual(bottom["match_ids"], ["rs"])


class TestOracleParity(unittest.TestCase):
    def test_engine_matches_independent_oracle(self):
        results = {
            "ab": res("ab", 2, 1), "cd": res("cd", 0, 0),
            "ac": res("ac", 3, 1), "bd": res("bd", 1, 1),
            "ad": {"result_id": "x", "match_id": "ad", "state": "cancelled"},
            "bc": res("bc", 0, 2),
        }
        penalties = {"C": 2}
        mine = compute_standings(TEAMS4, FIXTURE4, results, penalties, (1, 3))
        ora = oracle_standings(TEAMS4, FIXTURE4, results, penalties, (1, 3))
        self.assertEqual(mine["order"], ora["order"])
        for t in TEAMS4:
            row = next(r for r in mine["ranked"] if r["team"] == t)
            self.assertEqual(row["total_points"], ora["total_points"][t])
            self.assertEqual(row["goal_diff"], ora["full_gd"][t])
            self.assertEqual(row["goals_for"], ora["full_gf"][t])
        self.assertEqual({x["match_id"] for x in mine["ignored_matches"]},
                         set(ora["ignored"]))


class TestStandingsRepository(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sdb = os.path.join(self.tmp.name, "sched.db")
        self.rdb = os.path.join(self.tmp.name, "repo.db")
        self.svc = ScheduleService(self.sdb)
        self.svc.initialize(TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY)
        self.svc.publish(FIXTURE4)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def _repo(self):
        return StandingsRepository(self.rdb, self.sdb)

    def test_atomic_batch_and_idempotency(self):
        good = [res("ab", 1, 0), res("cd", 2, 2)]
        with self._repo() as repo:
            rec = repo.apply_result_batch("req-1", good)
            self.assertEqual(rec["seq"], 1)
            # 同号同文复读
            rec2 = repo.apply_result_batch("req-1", good)
            self.assertTrue(rec2["replayed"])
            self.assertEqual(rec2["seq"], 1)
            # 同号异文拒绝
            with self.assertRaises(BatchRejected):
                repo.apply_result_batch("req-1", [res("ab", 2, 0)])
            self.assertEqual(len(repo.request_log()), 1)

    def test_invalid_batch_leaves_no_trace(self):
        bad = [
            res("ab", 1, 0),
            {"result_id": "r-bad", "match_id": "cd", "state": "played",
             "score_home": 101, "score_away": 0},
            {"result_id": "r-cancel", "match_id": "ac", "state": "cancelled",
             "score_home": 1, "score_away": None},
        ]
        with self._repo() as repo:
            with self.assertRaises(BatchRejected) as ctx:
                repo.apply_result_batch("bad-req", bad)
            self.assertEqual(len(ctx.exception.reasons), 2)  # 越界分数 + 取消带比分
            self.assertEqual(repo.request_log(), [])
            self.assertEqual(repo.active_results(), {})

    def test_unknown_match_and_duplicate_result_id_in_batch(self):
        with self._repo() as repo:
            entries = [res("ab", 1, 0),
                       {**res("ab", 2, 0), "result_id": "r-ab-2"}]
            entries[1]["result_id"] = "r-ab"  # 批内 result_id 重复
            with self.assertRaises(BatchRejected) as ctx:
                repo.apply_result_batch("dup", entries)
            self.assertTrue(any("批内重复" in x for x in ctx.exception.reasons))
            ghost = [{**res("zz", 1, 0)}]
            with self.assertRaises(BatchRejected):
                repo.apply_result_batch("ghost", ghost)

    def test_one_active_identity_per_match_and_replacement(self):
        with self._repo() as repo:
            repo.apply_result_batch("b1", [
                {"result_id": "r-ab-v1", "match_id": "ab",
                 "state": "pending"}])
            # 新 ID 指向已有 match -> 拒绝（不能当另一场比赛）
            with self.assertRaises(BatchRejected):
                repo.apply_result_batch("b2", [res("ab", 1, 0, rid="r-ab-v2")])
            # 同 result_id 不得换绑到另一个 match_id
            with self.assertRaises(BatchRejected) as ctx:
                repo.apply_result_batch("rebind", [{
                    "result_id": "r-ab-v1", "match_id": "cd",
                    "state": "pending"}])
            self.assertTrue(any("换绑" in x for x in ctx.exception.reasons))
            # 同 result_id 完整替换：pending -> played，按 seq 生效
            rec = repo.apply_result_batch("b3", [
                res("ab", 1, 0, rid="r-ab-v1")])
            self.assertEqual(rec["seq"], 2)
            active = repo.active_results()
            self.assertEqual(set(active), {"ab"})
            self.assertEqual(active["ab"]["state"], "played")
            self.assertEqual(active["ab"]["result_id"], "r-ab-v1")

    def test_penalties_apply_revoke_and_ledger(self):
        with self._repo() as repo:
            repo.apply_penalty_batch("p1", [{"team": "A", "action": "apply",
                                             "points": 4}])
            repo.apply_penalty_batch("p2", [{"team": "A", "action": "apply",
                                             "points": 2}])
            self.assertEqual(repo.active_penalties(), {"A": 6})
            with self.assertRaises(BatchRejected):  # 超 9 拒绝，整批回滚
                repo.apply_penalty_batch("p3", [{"team": "A", "action": "apply",
                                                 "points": 5}])
            self.assertEqual(repo.active_penalties(), {"A": 6})
            repo.apply_penalty_batch("p4", [{"team": "A", "action": "revoke"}])
            self.assertEqual(repo.active_penalties(), {"A": 0})
            # 流水：apply 4、apply 2、被拒的 p3 无流水、revoke 共 3 条
            self.assertEqual(len(repo.penalty_ledger("A")), 3)

    def test_snapshot_immutable_and_persistence(self):
        with self._repo() as repo:
            repo.apply_result_batch("init", [res("ab", 1, 0)])
            snap1 = repo.standings("rank-1", 1, (1, 3),
                                   promotion_places=1, relegation_places=1)
            order1 = list(snap1["order"])
            sid = snap1["snapshot_id"]
            # 之后事实变化
            repo.apply_result_batch("more", [res("cd", 5, 0)])
            snap2 = repo.standings("rank-2", 1, (1, 3))
            # 旧快照不被改写
            self.assertEqual(repo.get_snapshot(sid)["order"], order1)
            self.assertNotEqual(snap2["order"], order1)
        # 关闭重开：edition、事实、快照全部可复算
        with self._repo() as repo2:
            old = repo2.get_snapshot(sid)
            self.assertEqual(old["order"], order1)
            self.assertEqual(len(repo2.request_log()), 2)
            self.assertEqual(repo2.active_results()["ab"]["state"], "played")

    def test_unknown_edition_rejected(self):
        with self._repo() as repo:
            with self.assertRaises(KeyError):
                repo.standings("x", 99, (1, 3))

    def test_pending_to_played_lifecycle_and_zero_game_rows(self):
        with self._repo() as repo:
            repo.apply_result_batch("p", [{
                "result_id": "r-ab", "match_id": "ab", "state": "pending"}])
            s0 = repo.standings("s0", 1, (1, 3))
            self.assertTrue(all(r["played"] == 0 for r in s0["ranked"]))
            # 同 result_id 完整替换为 played 平局
            repo.apply_result_batch("c", [{
                "result_id": "r-ab", "match_id": "ab", "state": "played",
                "score_home": 2, "score_away": 2}])
            s1 = repo.standings("s1", 1, (1, 3))
            for t in ("A", "B"):
                row = next(r for r in s1["ranked"] if r["team"] == t)
                self.assertEqual((row["played"], row["match_points"]), (1, 1))
            # C/D 没有任何 played 场，仍保留一行
            self.assertEqual(
                sorted(r["team"] for r in s1["ranked"] if r["played"] == 0),
                ["C", "D"])
            # 旧切面不被后续替换改写
            self.assertTrue(
                all(r["played"] == 0 for r in
                    repo.get_snapshot(s0["snapshot_id"])["ranked"]))

    def test_boolean_score_rejected_and_schedule_db_is_readonly(self):
        import sqlite3
        with self._repo() as repo:
            with self.assertRaises(BatchRejected):
                repo.apply_result_batch("bool", [{
                    "result_id": "r-cd", "match_id": "cd", "state": "played",
                    "score_home": True, "score_away": 0}])
            with self.assertRaises(sqlite3.OperationalError):
                repo.sched.execute(
                    "INSERT INTO team(team_id, ord) VALUES('X', 9)")
            self.assertEqual(
                set(repo.inspect()),
                {"teams", "requests", "active_results", "active_penalties",
                 "penalty_ledger", "snapshots"})


if __name__ == "__main__":
    unittest.main()
