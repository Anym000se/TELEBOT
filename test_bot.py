import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from bot import ConfigError, Slot, State, load_config, pick_message, plan_day

TZ = ZoneInfo("America/New_York")
DAY = date(2026, 10, 1)


def at(hour, minute=0, day=DAY):
    return datetime.combine(day, time(hour, minute), TZ)


def slot(name="morning", start=time(7, 30), end=time(9, 0), chance=1.0, messages=("hi",)):
    return Slot(name, start, end, list(messages), chance)


class PlanDayTests(unittest.TestCase):
    def test_time_is_inside_window(self):
        for day in (DAY + timedelta(days=i) for i in range(60)):
            [(when, _)] = plan_day(day, [slot()], TZ, at(0, 0, day), State(None))
            self.assertTrue(at(7, 30, day) <= when <= at(9, 0, day), when)

    def test_same_plan_after_restart(self):
        first = plan_day(DAY, [slot()], TZ, at(0), State(None))
        second = plan_day(DAY, [slot()], TZ, at(1), State(None))
        self.assertEqual(first[0][0], second[0][0])

    def test_window_already_over(self):
        self.assertEqual(plan_day(DAY, [slot()], TZ, at(10), State(None)), [])

    def test_catches_up_when_started_mid_window(self):
        now = at(8, 59)
        [(when, _)] = plan_day(DAY, [slot()], TZ, now, State(None))
        self.assertTrue(now <= when <= at(9, 0), when)

    def test_window_past_midnight(self):
        s = slot(start=time(23, 30), end=time(0, 30))
        [(when, _)] = plan_day(DAY, [s], TZ, at(0), State(None))
        self.assertTrue(at(23, 30) <= when <= at(0, 30, DAY + timedelta(days=1)), when)

    def test_skips_slot_already_sent_today(self):
        state = State(None)
        state.record("morning", DAY, "hi")
        self.assertEqual(plan_day(DAY, [slot()], TZ, at(0), state), [])
        self.assertEqual(len(plan_day(DAY + timedelta(days=1), [slot()], TZ, at(0), state)), 1)

    def test_chance(self):
        days = [DAY + timedelta(days=i) for i in range(400)]
        fired = sum(bool(plan_day(d, [slot(chance=0.5)], TZ, at(0, 0, d), State(None))) for d in days)
        self.assertTrue(140 < fired < 260, fired)

    def test_sorted_by_time(self):
        slots = [slot("night", time(22), time(23)), slot("morning", time(7), time(8))]
        plan = plan_day(DAY, slots, TZ, at(0), State(None))
        self.assertEqual([s.name for _, s in plan], ["morning", "night"])


class PickMessageTests(unittest.TestCase):
    def test_avoids_recent_repeats(self):
        s = slot(messages=["a", "b", "c", "d"])
        for _ in range(50):
            self.assertIn(pick_message(s, ["x", "a", "b"]), {"c", "d"})

    def test_single_message_still_sends(self):
        self.assertEqual(pick_message(slot(messages=["only"]), ["only"]), "only")


class StateTests(unittest.TestCase):
    def test_persists(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            State(path).record("morning", DAY, "hi")
            reloaded = State(path)
            self.assertTrue(reloaded.sent_on("morning", DAY))
            self.assertEqual(reloaded.recent("morning"), ["hi"])


class ConfigTests(unittest.TestCase):
    def test_example_config_loads(self):
        cfg = load_config(Path(__file__).with_name("config.example.toml"))
        self.assertEqual([s.name for s in cfg.slots], ["good morning", "thinking of you", "goodnight"])
        self.assertEqual(cfg.skip_if_texted_within, timedelta(minutes=90))
        self.assertEqual(cfg.slot("Goodnight").name, "goodnight")

    def test_bad_config(self):
        cases = {
            'recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["7:3", "9:00"]\nmessages = ["hi"]': "couldn't read time",
            'recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = []': "at least one message",
            'recipient = "@x"\ntimezone = "Mars/Base"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = ["hi"]': "Unknown timezone",
            'recipient = "@x"': "at least one [[schedule]]",
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for text, error in cases.items():
                path.write_text(text)
                with self.assertRaisesRegex(ConfigError, error.replace("[", r"\[").replace("]", r"\]")):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
