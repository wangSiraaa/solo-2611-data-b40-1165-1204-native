"""case.json 端到端契约：两个真实 edition、归属翻转、断链对照与手算小表。

期望数字全部来自显式手算（见各断言注释），不由待测排序函数生成。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from league.ranking import compute_standings
from league.schedule_service import ScheduleService
from league.standings_repo import StandingsRepository

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_case():
    with open(os.path.join(HERE, "case.json"), encoding="utf-8") as f:
        return json.load(f)


class TestCaseJsonContract(unittest.TestCase):
    def setUp(self):
        self.case = load_case()
        self.tmp = tempfile.TemporaryDirectory()
        self.sdb = os.path.join(self.tmp.name, "sched.db")
        self.rdb = os.path.join(self.tmp.name, "repo.db")
        self.svc = ScheduleService(self.sdb)
        c = self.case
        registry = {m["match_id"]: m["round"] for m in c["before_fixture"]}
        slots = {r: {"s": c["slot_capacity"]} for r in sorted(set(registry.values()))}
        self.svc.initialize(c["clubs"], registry, slots,
                            {t: list(c["blackouts"]) for t in c["clubs"]})
        self.before = c["before_fixture"]
        self.after = [
            ({**m, "home": m["away"], "away": m["home"]}
             if m["match_id"] in set(c["flipped_match_ids"]) else dict(m))
            for m in self.before
        ]
        self.assertEqual(self.svc.check_candidate(self.before), [])
        self.assertEqual(self.svc.check_candidate(self.after), [])
        self.ed_before = self.svc.publish(self.before)["edition_id"]
        self.ed_after = self.svc.publish(self.after)["edition_id"]
        self.repo = StandingsRepository(self.rdb, self.sdb)
        self.entries = [
            {"result_id": f"r-{row['match_id']}", "match_id": row["match_id"],
             "state": "played", "score_home": row["home_goals"],
             "score_away": row["away_goals"]}
            for row in c["scores"]
        ]
        self.repo.apply_result_batch("initial-results", self.entries)

    def tearDown(self):
        self.repo.close()
        self.svc.close()
        self.tmp.cleanup()

    def test_both_editions_legal_and_flip_one_round_of_roles(self):
        # 恰好翻转三场，且三场分布在全部三轮各一场（每轮主客归属真实改变一轮）
        flipped = set(self.case["flipped_match_ids"])
        rounds_touched = {self.svc.locate_match(self.ed_before, m)["round"]
                          for m in flipped}
        self.assertEqual(rounds_touched, {1, 2, 3})
        for mid in flipped:
            b = self.svc.locate_match(self.ed_before, mid)
            a = self.svc.locate_match(self.ed_after, mid)
            self.assertEqual((b["home"], b["away"]), (a["away"], a["home"]))

    def test_fixed_scores_attribution_changes_with_edition(self):
        snap_b = self.repo.standings("rb", self.ed_before, (1, 3))
        snap_a = self.repo.standings("ra", self.ed_after, (1, 3))
        # 手算 before 名次：A/B/C 各 6 分（各 2 胜 1 负），D 0 分。
        # 相互小表（ab 1:0、ac 3:0、bc 2:0，主客按 before）：
        #   A: pts3 GD-2 GF1；B: pts3 GD+1 GF2；C: pts3 GD+1 GF3 -> C>B>A
        # 注意全季 GD 为 B+3 > C+2，若误用全季净胜分将错排成 B>C。
        self.assertEqual(snap_b["order"], ["C", "B", "A", "D"])
        node = next(n for n in snap_b["tie_tree"]
                    if set(n["teams"]) == {"A", "B", "C"})
        self.assertEqual(node["metrics"]["B"]["mini_gf"], 2)
        self.assertEqual(node["metrics"]["C"]["mini_gf"], 3)
        self.assertEqual([c["teams"][0] for c in node["children"]],
                         ["C", "B", "A"])
        # after：ab/ac/bc 主客翻转，同槽位比分归属相反；cd/bd/ad 不变
        # 逐场独立复算（见测试提交说明）：A/B/C 各 6 分、D 0 分
        #   A 胜 ac(3:0)、ad(客 10:0)，负 ab -> 6 分，GF13 GD+12
        #   B 胜 ab(主 1:0)、bd，负 bc -> 6 分，GF3 GD+1
        #   C 胜 cd、bc(主 2:0)，负 ac -> 6 分，GF3 GD0
        by_team_a = {r["team"]: r for r in snap_a["ranked"]}
        self.assertEqual(by_team_a["A"]["match_points"], 6)
        self.assertEqual(by_team_a["B"]["match_points"], 6)
        self.assertEqual(by_team_a["C"]["match_points"], 6)
        self.assertEqual(by_team_a["D"]["match_points"], 0)
        self.assertEqual((by_team_a["A"]["goals_for"],
                          by_team_a["A"]["goal_diff"]), (13, 12))
        # 三队 6 分小表全同（互赛各一胜一负 3 分、GD0），退全表 GD：A>B? 实际 A+12,B+1,C0
        self.assertEqual(snap_a["order"][0], "A")
        self.assertNotEqual(snap_a["order"], snap_b["order"])

    def test_stale_projection_after_request_shows_concrete_errors(self):
        results = self.repo.active_results()
        snap_after = self.repo.standings("ra2", self.ed_after, (1, 3),
                                         promotion_places=1, relegation_places=1)
        # 断链消费者：请求身份是 after，内部却冻结 before 投影
        stale = compute_standings(self.case["clubs"], self.before, results,
                                  {}, (1, 3), 1, 1)
        snap_before = self.repo.standings("rb2", self.ed_before, (1, 3),
                                          promotion_places=1, relegation_places=1)
        # 断链结果恰等于显式旧 edition（但请求身份不同 -> 语义非法）
        self.assertEqual(stale["order"], snap_before["order"])
        # 具体错误可定位：A 的进球在正确 after 下为 13，断链为 11
        good_a = next(r for r in snap_after["ranked"] if r["team"] == "A")
        bad_a = next(r for r in stale["ranked"] if r["team"] == "A")
        self.assertEqual((good_a["goals_for"], bad_a["goals_for"]), (13, 11))
        # 资格错误且未伪造名次：正确升级 A，断链升级 C
        self.assertEqual(snap_after["promotion"]["teams"], ["A"])
        self.assertEqual(stale["promotion"]["teams"], ["C"])
        # 显式请求旧 edition 依然合法
        self.assertEqual(snap_before["edition_id"], self.ed_before)
        self.assertEqual(snap_after["edition_id"], self.ed_after)


if __name__ == "__main__":
    unittest.main()
