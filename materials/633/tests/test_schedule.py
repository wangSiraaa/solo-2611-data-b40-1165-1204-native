"""赛程服务：普通赛程、全部定向拒绝场景、edition 不可变与持久化、oracle 双向对照。"""

from __future__ import annotations

import os
import tempfile
import unittest

from league.schedule_service import ScheduleService, CandidateRejected
from oracle import enumerate_legal_schedules

from tests.fixtures import (
    TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY, FIXTURE4,
    TEAMS6, REGISTRY6, ROUND_SLOTS6, FIXTURE6,
    mutate, replace,
)


class ScheduleTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "sched.db")
        self.svc = ScheduleService(self.db)
        self.svc.initialize(TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def codes(self, cand):
        return sorted(i.code for i in self.svc.check_candidate(cand))

    def assertHas(self, codes, *expected):
        for e in expected:
            self.assertIn(e, codes, f"缺少问题码 {e}；实际 {codes}")


class TestHappyPath(ScheduleTestBase):
    def test_valid_candidate_publishes_and_queries(self):
        issues = self.svc.check_candidate(FIXTURE4)
        self.assertEqual(issues, [])
        ed = self.svc.publish(FIXTURE4)
        self.assertEqual(ed["edition_id"], 1)
        self.assertEqual(len(ed["matches"]), 6)

        # 按轮查询
        r1 = self.svc.query_round(1, 1)
        self.assertEqual({m["match_id"] for m in r1}, {"ab", "cd"})
        # 按球队查询，显式主客身份与对手
        a = self.svc.query_team(1, "A")
        self.assertEqual([m["round"] for m in a], [1, 2, 3])
        self.assertEqual([(m["role"], m["opponent"]) for m in a],
                         [("home", "B"), ("away", "C"), ("away", "D")])
        # 定位单场
        loc = self.svc.locate_match(1, "bc")
        self.assertEqual((loc["round"], loc["home"], loc["away"]), (3, "B", "C"))

    def test_publish_atomic_no_partial_edition(self):
        bad = mutate(FIXTURE4, 0, home="A", away="A")
        with self.assertRaises(CandidateRejected):
            self.svc.publish(bad)
        self.assertEqual(self.svc.list_editions(), [])

    def test_editions_immutable_and_independently_queryable(self):
        self.svc.publish(FIXTURE4)
        flipped = replace(FIXTURE4,
                          {0: {"home": "B", "away": "A"},
                             2: {"home": "A", "away": "C"},
                             5: {"home": "C", "away": "B"}})
        self.assertEqual(self.svc.check_candidate(flipped), [])
        ed2 = self.svc.publish(flipped)
        self.assertEqual(ed2["edition_id"], 2)
        # 旧 edition 主客不变
        self.assertEqual(self.svc.locate_match(1, "ab")["home"], "A")
        self.assertEqual(self.svc.locate_match(2, "ab")["home"], "B")
        # 旧 edition 仍可按轮/队查询
        self.assertEqual(len(self.svc.query_round(1, 2)), 2)

    def test_reopen_persistence(self):
        self.svc.publish(FIXTURE4)
        self.svc.close()
        svc2 = ScheduleService(self.db)
        self.assertEqual(svc2.get_config()["match_registry"], REGISTRY4)
        self.assertEqual(len(svc2.list_editions()), 1)
        self.assertEqual(svc2.locate_match(1, "ab")["home"], "A")
        # 初始化固定后不允许重新初始化
        with self.assertRaises(ValueError):
            svc2.initialize(TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY)
        insp = svc2.inspect()
        self.assertEqual(insp["config"]["teams"], TEAMS4)
        self.assertEqual(insp["editions"][0]["match_count"], 6)
        svc2.close()


class TestRejections(ScheduleTestBase):
    def test_missing_pair_and_duplicate_pair(self):
        # 把 cd 改成又一场 A-B（同时丢失 C-D），并制造重复对阵
        bad = mutate(FIXTURE4, 1, home="A", away="B")
        codes = self.codes(bad)
        self.assertHas(codes, "duplicate_pair", "missing_pair")

    def test_round_missing_team(self):
        # 第 1 轮 cd 换成 B-D（B 重复、C 缺席），其余轮次补齐唯一性问题依旧报整轮缺队
        bad = mutate(FIXTURE4, 1, home="B", away="D")
        codes = self.codes(bad)
        self.assertHas(codes, "round_missing_team", "round_team_twice",
                       "duplicate_pair", "missing_pair")

    def test_three_consecutive_home_or_away(self):
        # 重排主客使 A 三轮全 home（ab、ac、ad），不破坏配对/轮次
        bad = replace(FIXTURE4,
                      {0: {"home": "A", "away": "B"},
                         2: {"home": "A", "away": "C"},
                         4: {"home": "A", "away": "D"}})
        codes = self.codes(bad)
        self.assertHas(codes, "three_consecutive_role")
        issue = next(i for i in self.svc.check_candidate(bad)
                     if i.code == "three_consecutive_role")
        self.assertEqual(issue.location["team"], "A")
        self.assertEqual(issue.location["role"], "home")

    def test_home_away_imbalance(self):
        # D 全部主场（cd、bd、ad 翻向 D home）
        bad = replace(FIXTURE4,
                      {1: {"home": "D", "away": "C"},
                         3: {"home": "D", "away": "B"},
                         4: {"home": "D", "away": "A"}})
        self.assertHas(self.codes(bad), "home_away_imbalance")

    def test_slot_capacity_exceeded(self):
        svc = ScheduleService(os.path.join(self.tmp.name, "cap.db"))
        slots = {1: {"s": 1}, 2: {"s": 2}, 3: {"s": 2}}
        svc.initialize(TEAMS4, REGISTRY4, slots, BLACKOUTS_EMPTY)
        codes = sorted(i.code for i in svc.check_candidate(FIXTURE4))
        self.assertHas(codes, "slot_capacity_exceeded")
        svc.close()

    def test_team_blackout(self):
        svc = ScheduleService(os.path.join(self.tmp.name, "blk.db"))
        svc.initialize(TEAMS4, REGISTRY4, ROUND_SLOTS4,
                       {**BLACKOUTS_EMPTY, "A": ["s"]})
        issues = svc.check_candidate(FIXTURE4)
        self.assertTrue(all(i.code == "team_blackout" for i in issues))
        self.assertEqual(len(issues), 3)  # A 的三场全部冲突
        svc.close()

    def test_slot_not_allowed(self):
        bad = mutate(FIXTURE4, 0, slot="zz")
        self.assertHas(self.codes(bad), "slot_not_allowed")

    def test_team_slot_repeat_same_round(self):
        # 第 1 轮把 cd 改成 A-C：A 在同轮同 slot 出现两次（同时触发轮次重复/配对问题，
        # team_slot_repeat 作为 slot 侧独立可定位问题仍须给出）
        bad = mutate(FIXTURE4, 1, home="A", away="C")
        self.assertHas(self.codes(bad), "team_slot_repeat",
                       "round_team_twice", "duplicate_pair", "missing_pair")

    def test_self_match_unknown_and_round_mismatch(self):
        bad = mutate(FIXTURE4, 0, home="A", away="A")
        self.assertHas(self.codes(bad), "self_match")
        bad2 = mutate(FIXTURE4, 0, round=2)
        self.assertHas(self.codes(bad2), "round_mismatch")
        bad3 = mutate(FIXTURE4, 0, match_id="ghost")
        self.assertHas(self.codes(bad3), "unknown_match_id", "missing_match_id")

    def test_duplicate_match_id_row(self):
        bad = [dict(FIXTURE4[0])] + FIXTURE4[1:]
        bad[0]["match_id"] = "cd"  # 与第 2 行同 match_id
        self.assertHas(self.codes(bad), "duplicate_match_id")


class TestSixTeams(unittest.TestCase):
    def test_six_team_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            svc = ScheduleService(os.path.join(d, "s6.db"))
            svc.initialize(TEAMS6, REGISTRY6, ROUND_SLOTS6,
                           {t: [] for t in TEAMS6})
            self.assertEqual(svc.check_candidate(FIXTURE6), [])
            ed = svc.publish(FIXTURE6)
            self.assertEqual(len(ed["matches"]), 15)
            for r in range(1, 6):
                self.assertEqual(len(svc.query_round(ed["edition_id"], r)), 3)
            svc.close()


class TestOracleCrossCheck(unittest.TestCase):
    def test_enumerated_legals_all_publish_and_everything_else_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            svc = ScheduleService(os.path.join(d, "o.db"))
            svc.initialize(TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY)
            legals = enumerate_legal_schedules(
                TEAMS4, REGISTRY4, ROUND_SLOTS4,
                {t: set() for t in TEAMS4})
            self.assertGreater(len(legals), 0)
            # oracle 合法 => 服务必须放行，并真的能发布
            for cand in legals:
                self.assertEqual(svc.check_candidate(cand), [], cand)
            # 抽样破坏：随机改一条主客制造自对阵/失衡，服务必须拒绝
            import random
            rng = random.Random(633)
            checked = 0
            for cand in legals[:20]:
                bad = [dict(x) for x in cand]
                i = rng.randrange(len(bad))
                bad[i]["away"] = bad[i]["home"]
                if svc.check_candidate(bad) == []:
                    self.fail(f"oracle 外候选被误放行: {bad}")
                checked += 1
            self.assertEqual(checked, 20)
            svc.close()

    def test_blackout_narrows_enumeration(self):
        legals = enumerate_legal_schedules(
            TEAMS4, REGISTRY4, ROUND_SLOTS4,
            {**{t: set() for t in TEAMS4}, "A": {"s"}})
        # 唯一 slot s 对 A 全轮禁用 => 不存在任何合法完整赛程
        self.assertEqual(legals, [])


if __name__ == "__main__":
    unittest.main()
