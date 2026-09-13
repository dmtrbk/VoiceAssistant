import unittest
import os
import time
from skills.timer import (
    TimerSkill,
    parse_duration_seconds,
    _parse_bare_minutes,
    format_remaining_time,
    TIMERS_FILE,
)
from skills.base import RequestContext


class TestTimer(unittest.TestCase):
    def setUp(self):
        self.skill = TimerSkill()
        if os.path.exists(TIMERS_FILE):
            try:
                os.remove(TIMERS_FILE)
            except OSError:
                pass

    def tearDown(self):
        for t in self.skill.active_timers:
            t.cancel()
        self.skill.active_timers.clear()
        if os.path.exists(TIMERS_FILE):
            try:
                os.remove(TIMERS_FILE)
            except OSError:
                pass

    def test_parse_duration_seconds(self):
        sec, label = parse_duration_seconds("поставь таймер на пять минут")
        self.assertEqual(sec, 300)
        self.assertIn("5 минут", label)

        sec_half, label_half = parse_duration_seconds("засеки полчаса")
        self.assertEqual(sec_half, 1800)

        sec_plain, label_plain = parse_duration_seconds("таймер на 10")
        self.assertEqual(sec_plain, 600)

    def test_parse_bare_minutes(self):
        sec, label = _parse_bare_minutes("пять")
        self.assertEqual(sec, 300)
        sec_num, label_num = _parse_bare_minutes("15")
        self.assertEqual(sec_num, 900)

    def test_format_remaining_time(self):
        self.assertEqual(format_remaining_time(0), "меньше секунды")
        self.assertIn("1 минуту", format_remaining_time(60))
        self.assertIn("1 час", format_remaining_time(3600))

    def test_timer_execution_and_cancellation(self):
        spoken = []
        ctx = RequestContext(raw_text="поставь таймер на 10 минут", speak=spoken.append)
        self.skill.execute(ctx)
        self.assertEqual(len(self.skill.active_timers), 1)
        self.assertTrue(os.path.exists(TIMERS_FILE))

        # Check remaining
        spoken_status = []
        ctx_status = RequestContext(raw_text="сколько осталось до конца таймера", speak=spoken_status.append)
        self.skill.execute(ctx_status)
        self.assertTrue(len(spoken_status) > 0)
        self.assertIn("осталось", spoken_status[0].lower())

        # Cancel
        spoken_cancel = []
        ctx_cancel = RequestContext(raw_text="отмени таймер", speak=spoken_cancel.append)
        self.skill.execute(ctx_cancel)
        self.assertEqual(len(self.skill.active_timers), 0)

    def test_expired_timer_is_announced(self):
        import json

        with open(TIMERS_FILE, "w", encoding="utf-8") as handle:
            json.dump(
                [{"end_time": time.time() - 30, "duration_sec": 60, "label": "5 минут"}],
                handle,
            )
        spoken = []
        self.skill.start_background(spoken.append)
        self.assertEqual(len(self.skill.active_timers), 0)
        self.assertTrue(spoken)
        self.assertIn("пока меня не было", spoken[0].lower())


if __name__ == "__main__":
    unittest.main()
