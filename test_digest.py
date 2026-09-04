"""Tests for parsing, event replay, and rendering. No credentials needed."""

import datetime as dt
import sqlite3
import unittest

import main
from slack_source import normalize, parse_message


CONFIG = {
    "timezone": "Asia/Seoul",
    "slack": {
        "feed_channel": "C1",
        "post_channel": "C2",
        "header": "오늘의 팀 일정",
    },
    "display_names": {
        "mina.park@example.com": "Mina Park",
        "alex.kim@example.com": "Alex Kim",
        "sam.lee@example.com": "Sam Lee",
        "jamie.cho@example.com": "Jamie Cho",
    },
    "flex_names": {
        "mina.park@example.com": ["Mina Park"],
        "alex.kim@example.com": ["Alex Kim"],
        "sam.lee@example.com": ["Sam(Samuel) Lee"],
        "jamie.cho@example.com": ["JaMie Cho"],
        # robin.yu@example.com deliberately absent
    },
}

# Stands in for the channel membership the roster is normally derived from.
ROSTER = {
    "builtAt": "2026-08-31T10:00:00",
    "members": [
        {"id": "U1", "name": "Mina Park", "email": "mina.park@example.com"},
        {"id": "U2", "name": "Alex Kim", "email": "alex.kim@example.com"},
        {"id": "U3", "name": "Sam Lee", "email": "sam.lee@example.com"},
        {"id": "U4", "name": "Gyeongwoo Park", "email": "jamie.cho@example.com"},
        {"id": "U5", "name": "Robin Yu", "email": "robin.yu@example.com"},
    ],
}

POSTED = dt.date(2026, 8, 31)


class ParseTest(unittest.TestCase):
    def parse(self, text, posted=POSTED):
        return parse_message(text, posted)

    def test_all_day(self):
        e = self.parse(":palm_tree: [Shinhoo Kim] - 8월 21일 하루종일 휴가입니다.")
        self.assertEqual(
            (e["kind"], e["dateFrom"], e["dateTo"], e["span"]),
            ("grant", "2026-08-21", "2026-08-21", "종일"),
        )

    def test_korean_time_range(self):
        e = self.parse(":palm_tree: [Yong Kim] - 8월 26일 오전 10:00 ~ 오후 3:00 휴가입니다.")
        self.assertEqual(e["span"], "10:00-15:00")

    def test_multi_day_range(self):
        e = self.parse(":palm_tree: [Suhwan Kim] - 8월 5일 ~ 8월 6일 휴가입니다.")
        self.assertEqual((e["dateFrom"], e["dateTo"], e["span"]),
                         ("2026-08-05", "2026-08-06", "종일"))

    def test_english_all_day(self):
        e = self.parse(":palm_tree: [Young Suk Cho] - August 14 all day Vacation.")
        self.assertEqual((e["dateFrom"], e["span"]), ("2026-08-14", "종일"))

    def test_english_time_range_with_narrow_space(self):
        e = self.parse(
            ":palm_tree: [Kangho Lee] - August 11 2:00 PM ~ 6:00 PM Vacation."
        )
        self.assertEqual(e["span"], "14:00-18:00")

    def test_cancellation(self):
        e = self.parse(
            ":exclamation: [DooWon Lee] - 8월 28일 ~ 8월 31일 휴가가 취소되었습니다."
        )
        self.assertEqual(e["kind"], "cancel")

    def test_year_rolls_forward_for_a_month_far_behind(self):
        # Posted in December, referring to January: that is next year.
        e = self.parse(":palm_tree: [X] - 1월 5일 하루종일 휴가입니다.",
                       posted=dt.date(2026, 12, 20))
        self.assertEqual(e["dateFrom"], "2027-01-05")

    def test_year_stays_for_a_month_ahead(self):
        e = self.parse(":palm_tree: [Sang Uk Han] - 12월 24일 하루종일 휴가입니다.")
        self.assertEqual(e["dateFrom"], "2026-12-24")

    def test_non_leave_message_is_ignored(self):
        self.assertIsNone(self.parse("hello team"))
        self.assertIsNone(self.parse(":palm_tree: [X] - 공지사항입니다."))


class NormalizeTest(unittest.TestCase):
    def test_case_and_whitespace_are_folded(self):
        self.assertEqual(normalize("JaMie  Cho"), normalize("Jamie Cho"))
        self.assertEqual(normalize("Kangho Lee"), normalize("Kangho Lee"))

    def test_distinct_people_stay_distinct(self):
        # The whole reason matching is exact rather than fuzzy: a real feed
        # contained two employees whose names differed by one letter.
        self.assertNotEqual(normalize("Minah Park"), normalize("Mina Park"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(main.SCHEMA)

    def add(self, ts, name, kind, date_from, date_to="", span="종일"):
        self.conn.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?)",
            (ts, name, kind, date_from, date_to or date_from, span, ""),
        )

    def collect(self, day=dt.date(2026, 9, 11)):
        return main.collect(CONFIG, self.conn, day, ROSTER)

    def test_grant_shows_up(self):
        self.add("100", "Alex Kim", "grant", "2026-09-11")
        self.assertEqual([e["name"] for e in self.collect()], ["Alex Kim"])

    def test_cancel_after_grant_removes_it(self):
        self.add("100", "Alex Kim", "grant", "2026-09-11")
        self.add("200", "Alex Kim", "cancel", "2026-09-11")
        self.assertEqual(self.collect(), [])

    def test_regrant_after_cancel_counts_again(self):
        self.add("100", "Alex Kim", "grant", "2026-09-11")
        self.add("200", "Alex Kim", "cancel", "2026-09-11")
        self.add("300", "Alex Kim", "grant", "2026-09-11")
        self.assertEqual([e["name"] for e in self.collect()], ["Alex Kim"])

    def test_cancel_arriving_before_its_grant_does_not_suppress(self):
        # Seen in the real feed: cancel at 12:40, grant at 19:56 the same day.
        self.add("100", "Alex Kim", "cancel", "2026-09-11")
        self.add("200", "Alex Kim", "grant", "2026-09-11")
        self.assertEqual([e["name"] for e in self.collect()], ["Alex Kim"])

    def test_multi_day_leave_covers_a_day_inside_the_range(self):
        self.add("100", "Sam(Samuel) Lee", "grant", "2026-09-09", "2026-09-14")
        entries = self.collect()
        self.assertEqual(entries[0]["name"], "Sam Lee")
        self.assertTrue(entries[0]["multiDay"])

    def test_unknown_name_is_skipped(self):
        self.add("100", "Someone Else", "grant", "2026-09-11")
        self.assertEqual(self.collect(), [])

    def test_near_miss_name_is_not_matched(self):
        # One letter apart from a mapped name, and a different person.
        self.add("100", "Minah Park", "grant", "2026-09-11")
        self.assertEqual(self.collect(), [])

    def test_alias_matching_ignores_case(self):
        self.add("100", "jamie cho", "grant", "2026-09-11")
        self.assertEqual([e["name"] for e in self.collect()], ["Jamie Cho"])


class RenderTest(unittest.TestCase):
    def test_quiet_day(self):
        text = main.render(CONFIG, dt.date(2026, 9, 11), [], [])
        self.assertIn("오늘 휴가자 없습니다", text)
        self.assertIn("2026-09-11(금)", text)

    def test_entries_and_multi_day_detail(self):
        entries = [
            {"name": "Alex Kim", "span": "종일", "multiDay": False,
             "dateFrom": "2026-09-11", "dateTo": "2026-09-11"},
            {"name": "Sam Lee", "span": "종일", "multiDay": True,
             "dateFrom": "2026-09-09", "dateTo": "2026-09-14"},
        ]
        text = main.render(CONFIG, dt.date(2026, 9, 11), entries, [])
        self.assertIn("• Alex Kim — 휴가 (종일)", text)
        self.assertIn("• Sam Lee — 휴가 (종일, 2026-09-09~2026-09-14)", text)

    def test_members_without_a_flex_name_are_surfaced(self):
        unmapped = main.unmapped_members(CONFIG, ROSTER)
        self.assertEqual(unmapped, ["Robin Yu"])
        text = main.render(CONFIG, dt.date(2026, 9, 11), [], unmapped)
        self.assertIn("별칭 미등록", text)
        self.assertIn("Robin Yu", text)

    def test_someone_outside_the_channel_is_not_reported(self):
        # A configured flex name whose email left the channel drops out with it.
        roster = {"builtAt": "x", "members": [
            m for m in ROSTER["members"] if m["email"] != "alex.kim@example.com"]}
        self.assertNotIn("Alex Kim", main.alias_index(CONFIG, roster))


if __name__ == "__main__":
    unittest.main(verbosity=2)
