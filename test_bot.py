import asyncio
import importlib.util
import json
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import gcal
from bot import (
    AIConfig, CalendarWatcher, ChatLine, ConfigError, Slot, State, compose, load_config, pick_message, plan_day,
    recent_chat,
)

HAS_ANTHROPIC = importlib.util.find_spec("anthropic") is not None

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
        self.assertEqual(cfg.ai.model, "claude-opus-5-5")
        self.assertTrue(cfg.ai.read_recent_chat)
        self.assertIn("Her name is", cfg.ai.about_us)
        self.assertTrue(cfg.ai.write_texts)
        self.assertEqual(cfg.calendar.calendar_id, "primary")
        self.assertTrue(cfg.calendar.notify_me)
        self.assertEqual(cfg.calendar.credentials_path.name, "google-credentials.json")

    def test_ai_off_by_default(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = ["hi"]')
            cfg = load_config(path)
            self.assertFalse(cfg.ai.write_texts)
            self.assertIsNone(cfg.calendar)

    def test_bad_config(self):
        cases = {
            'recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["7:3", "9:00"]\nmessages = ["hi"]': "couldn't read time",
            'recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = []': "at least one message",
            'recipient = "@x"\ntimezone = "Mars/Base"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = ["hi"]': "Unknown timezone",
            'recipient = "@x"': "at least one [[schedule]]",
            'api_hash = 123456abcdef\nrecipient = "@x"': "straight quotes",
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for text, error in cases.items():
                path.write_text(text)
                with self.assertRaisesRegex(ConfigError, error.replace("[", r"\[").replace("]", r"\]")):
                    load_config(path)


class FakeWriter:
    def __init__(self, draft):
        self.draft = draft
        self.calls = []

    async def write(self, *args):
        self.calls.append(args)
        return self.draft


class FakeTelegram:
    def __init__(self, messages):
        self.messages = messages  # newest first, like Telegram returns them

    async def iter_messages(self, entity, limit):
        for message in self.messages[:limit]:
            yield message


def ai_config(read_recent_chat=False):
    cfg = load_config(Path(__file__).with_name("config.example.toml"))
    cfg.ai = AIConfig(True, "claude-opus-5-5", None, "", read_recent_chat)
    return cfg


class ComposeTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_ai_text(self):
        writer = FakeWriter(SimpleNamespace(send=True, message="morning, good luck on the exam", skip_reason=""))
        text = await compose(ai_config(), writer, None, None, slot(), State(None))
        self.assertEqual(text, "morning, good luck on the exam")

    async def test_ai_says_skip(self):
        writer = FakeWriter(SimpleNamespace(send=False, message="", skip_reason="you two are arguing"))
        self.assertIsNone(await compose(ai_config(), writer, None, None, slot(), State(None)))

    async def test_falls_back_to_list_when_ai_fails(self):
        text = await compose(ai_config(), FakeWriter(None), None, None, slot(messages=["hi"]), State(None))
        self.assertEqual(text, "hi")

    async def test_without_ai_uses_list(self):
        self.assertEqual(await compose(ai_config(), None, None, None, slot(messages=["hi"]), State(None)), "hi")

    async def test_passes_chat_to_ai(self):
        now = datetime.now(timezone.utc)
        telegram = FakeTelegram([
            SimpleNamespace(id=3, date=now - timedelta(minutes=5), out=False, message="", sticker=True),
            SimpleNamespace(id=2, date=now - timedelta(minutes=9), out=True, message="how'd it go?"),
            SimpleNamespace(id=1, date=now - timedelta(days=5), out=False, message="too old"),
        ])
        writer = FakeWriter(SimpleNamespace(send=True, message="x", skip_reason=""))
        await compose(ai_config(read_recent_chat=True), writer, telegram, "her", slot(), State(None))
        chat = writer.calls[0][3]
        self.assertEqual([(line.who, line.text) for line in chat], [("me", "how'd it go?"), ("her", "[sticker]")])

    async def test_recent_chat_is_oldest_first(self):
        now = datetime.now(timezone.utc)
        telegram = FakeTelegram([
            SimpleNamespace(id=2, date=now, out=False, message="second"),
            SimpleNamespace(id=1, date=now - timedelta(hours=1), out=True, message="first"),
        ])
        chat = await recent_chat(telegram, "her", TZ)
        self.assertEqual([(line.id, line.text) for line in chat], [(1, "first"), (2, "second")])


class MockClaude:
    """An Anthropic client whose HTTP requests are answered locally."""

    def __init__(self, status=200, body=None):
        import anthropic
        import httpx2

        self.requests = []

        def handler(request):
            self.requests.append(request)
            return httpx2.Response(status, json=body)

        self.client = anthropic.AsyncAnthropic(
            api_key="test-key",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        )

    @staticmethod
    def reply(output, stop_reason="end_turn"):
        content = [{"type": "text", "text": json.dumps(output)}] if output else []
        return {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


@unittest.skipUnless(HAS_ANTHROPIC, "anthropic not installed")
class WriterTests(unittest.IsolatedAsyncioTestCase):
    def writer(self, status=200, body=None):
        from ai import Writer

        self.mock = MockClaude(status, body)
        self.requests = self.mock.requests
        return Writer(self.mock.client, "claude-opus-5-5", "Her name is Sam.")

    reply = staticmethod(MockClaude.reply)

    async def write(self, writer):
        chat = [ChatLine(1, at(22, 40, DAY - timedelta(days=1)), "her", "big exam tomorrow 😭")]
        return await writer.write("good morning", ["morning babe"], at(8, 5), chat, ["morning!"])

    async def test_request_and_response(self):
        writer = self.writer(body=self.reply({"send": True, "message": '"good luck today!!"', "skip_reason": ""}))
        draft = await self.write(writer)
        self.assertTrue(draft.send)
        self.assertEqual(draft.message, "good luck today!!")

        [request] = self.requests
        sent = json.loads(request.content)
        self.assertEqual(sent["model"], "claude-opus-5-5")
        self.assertEqual(sent["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", request.headers["anthropic-beta"])
        self.assertEqual(sent["output_config"]["effort"], "medium")
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        self.assertIn("Her name is Sam.", json.dumps(sent["system"]))
        prompt = sent["messages"][0]["content"]
        self.assertIn("Thursday, October 1, 8:05am", prompt)
        self.assertIn("[Wed Sep 30 22:40] her: big exam tomorrow 😭", prompt)
        self.assertIn("- morning!", prompt)

    async def test_skip(self):
        writer = self.writer(body=self.reply({"send": False, "message": "", "skip_reason": "she's upset"}))
        draft = await self.write(writer)
        self.assertFalse(draft.send)
        self.assertEqual(draft.skip_reason, "she's upset")

    async def test_refusal_falls_back(self):
        with self.assertLogs("telebot", "WARNING"):
            self.assertIsNone(await self.write(self.writer(body=self.reply(None, stop_reason="refusal"))))

    async def test_api_error_falls_back(self):
        body = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
        with self.assertLogs("telebot", "WARNING"):
            self.assertIsNone(await self.write(self.writer(status=500, body=body)))

    async def test_bad_key_falls_back(self):
        body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        with self.assertLogs("telebot", "ERROR"):
            self.assertIsNone(await self.write(self.writer(status=401, body=body)))


def tomorrow(hhmm=None):
    day = (datetime.now(TZ) + timedelta(days=1)).date().isoformat()
    return f"{day}T{hhmm}" if hhmm else day


@unittest.skipUnless(HAS_ANTHROPIC, "anthropic not installed")
class PlanFinderTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_and_response(self):
        from ai import PlanFinder

        change = {"action": "add", "event_id": "", "title": "Dinner at Luigi's", "start": "2026-10-03T19:00",
                  "end": "", "location": "Luigi's", "quote": "luigi's saturday at 7?"}
        mock = MockClaude(body=MockClaude.reply({"changes": [change]}))
        finder = PlanFinder(mock.client, "claude-opus-5-5", "Her name is Sam.")
        upcoming = [
            gcal.Event("abc", "Movie night", "2026-10-05T20:00", "2026-10-05T22:00", ours=True),
            gcal.Event("xyz", "Dentist", "2026-10-06T09:00", "", ours=False),
        ]
        chat = [ChatLine(10, at(17), "her", "hi"), ChatLine(11, at(18), "her", "luigi's saturday at 7?")]
        [result] = await finder.find(at(18, 5), upcoming, chat, first_new_id=11)
        self.assertEqual((result.action, result.title, result.start), ("add", "Dinner at Luigi's", "2026-10-03T19:00"))

        sent = json.loads(mock.requests[0].content)
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        prompt = sent["messages"][0]["content"]
        self.assertIn("Now: Thursday, October 1, 2026, 6:05pm (America/New_York)", prompt)
        self.assertIn("- Movie night: 2026-10-05T20:00 to 2026-10-05T22:00 [id: abc]", prompt)
        self.assertIn("- Dentist: 2026-10-06T09:00\n", prompt)  # not ours, so no id to change it by
        self.assertLess(prompt.index("hi"), prompt.index("--- new messages ---"))
        self.assertLess(prompt.index("--- new messages ---"), prompt.index("luigi's saturday"))

    async def test_failure_returns_none(self):
        from ai import PlanFinder

        mock = MockClaude(status=500, body={"type": "error", "error": {"type": "api_error", "message": "boom"}})
        with self.assertLogs("telebot", "WARNING"):
            self.assertIsNone(await PlanFinder(mock.client, "m", "").find(at(8), [], [ChatLine(1, at(7), "her", "x")], 1))


class EventBodyTests(unittest.TestCase):
    now = at(12)

    def body(self, start, end="", title="Dinner"):
        return gcal.event_body(title, start, end, "", "dinner?", TZ, self.now)

    def test_timed_event_defaults_to_an_hour(self):
        body = self.body("2026-10-03T19:00")
        self.assertEqual(body["start"], {"dateTime": "2026-10-03T19:00:00", "timeZone": "America/New_York"})
        self.assertEqual(body["end"]["dateTime"], "2026-10-03T20:00:00")
        self.assertEqual(body["extendedProperties"], {"private": {"telebot": "1"}})
        self.assertEqual(gcal.describe(body), "Dinner, Sat Oct 3, 7:00pm")

    def test_end_before_start_is_ignored(self):
        self.assertEqual(self.body("2026-10-03T19:00", "2026-10-03T18:00")["end"]["dateTime"], "2026-10-03T20:00:00")

    def test_all_day_end_is_exclusive(self):
        body = self.body("2026-10-09", "2026-10-11", title="Trip")
        self.assertEqual((body["start"], body["end"]), ({"date": "2026-10-09"}, {"date": "2026-10-12"}))
        self.assertEqual(gcal.describe(body), "Trip, Fri Oct 9 to Sun Oct 11")

    def test_today_all_day_is_fine_but_past_is_not(self):
        self.body("2026-10-01")
        for start in ("2026-09-30", "2026-10-01T09:00"):
            with self.assertRaisesRegex(ValueError, "past"):
                self.body(start)

    def test_nonsense(self):
        with self.assertRaisesRegex(ValueError, "start time"):
            self.body("saturday")
        with self.assertRaisesRegex(ValueError, "title"):
            self.body("2026-10-03", title=" ")


class FakeResponse:
    def __init__(self, data):
        self.data = data
        self.content = json.dumps(data).encode() if data is not None else b""

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.reply)


class GoogleCalendarTests(unittest.TestCase):
    def test_upcoming(self):
        session = FakeSession({"items": [
            {"id": "a", "summary": "Dinner", "start": {"dateTime": "2026-10-03T23:00:00Z"},
             "end": {"dateTime": "2026-10-04T00:00:00Z"}, "extendedProperties": {"private": {"telebot": "1"}}},
            {"id": "b", "summary": "Her exam", "start": {"date": "2026-10-05"}, "end": {"date": "2026-10-06"}},
            {"id": "c", "summary": "Trip", "start": {"date": "2026-10-09"}, "end": {"date": "2026-10-12"}},
        ]})
        events = gcal.GoogleCalendar(session, "me@gmail.com", TZ).upcoming()
        self.assertEqual(events, [
            gcal.Event("a", "Dinner", "2026-10-03T19:00", "2026-10-03T20:00", ours=True),
            gcal.Event("b", "Her exam", "2026-10-05", "", ours=False),
            gcal.Event("c", "Trip", "2026-10-09", "2026-10-11", ours=False),
        ])
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("GET", "https://www.googleapis.com/calendar/v3/calendars/me%40gmail.com/events"))
        self.assertEqual(kwargs["params"]["singleEvents"], "true")

    def test_add_update_cancel(self):
        session = FakeSession({"id": "new1"})
        calendar = gcal.GoogleCalendar(session, "primary", TZ)
        self.assertEqual(calendar.add({"summary": "x"}), "new1")
        calendar.update("abc", {"summary": "y"})
        session.reply = None
        calendar.cancel("abc")
        base = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        self.assertEqual([(m, u) for m, u, _ in session.calls], [("POST", base), ("PATCH", f"{base}/abc"), ("DELETE", f"{base}/abc")])
        self.assertEqual(session.calls[0][2]["json"], {"summary": "x"})


class FakeCalendar:
    def __init__(self, upcoming=()):
        self.events = list(upcoming)
        self.added, self.updated, self.cancelled = [], [], []

    def upcoming(self):
        return self.events

    def add(self, body):
        self.added.append(body)
        return "new1"

    def update(self, event_id, body):
        self.updated.append((event_id, body))

    def cancel(self, event_id):
        self.cancelled.append(event_id)


class FakeFinder:
    def __init__(self, changes):
        self.changes = changes
        self.calls = []

    async def find(self, now, upcoming, chat, first_new_id):
        self.calls.append((chat, first_new_id))
        return self.changes


class ChatWithNotes(FakeTelegram):
    def __init__(self, messages):
        super().__init__(messages)
        self.notes = []

    async def send_message(self, to, text):
        self.notes.append((to, text))


def change(action="add", event_id="", title="", start="", end="", location="", quote=""):
    return SimpleNamespace(action=action, event_id=event_id, title=title, start=start, end=end, location=location, quote=quote)


class CalendarWatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = ai_config()
        now = datetime.now(timezone.utc)
        self.telegram = ChatWithNotes([
            SimpleNamespace(id=12, date=now, out=False, message="yay ❤️"),
            SimpleNamespace(id=11, date=now - timedelta(minutes=1), out=True, message="luigi's tomorrow at 7?"),
            SimpleNamespace(id=10, date=now - timedelta(hours=5), out=False, message="hungry"),
        ])
        self.mine = gcal.Event("ours1", "Movie night", tomorrow("20:00"), tomorrow("22:00"), ours=True)
        self.theirs = gcal.Event("x", "Work thing", tomorrow("09:00"), "", ours=False)
        self.calendar = FakeCalendar([self.mine, self.theirs])
        self.state = State(None)

    def watcher(self, changes):
        self.finder = FakeFinder(changes)
        return CalendarWatcher(self.cfg, self.telegram, "her", self.finder, self.calendar, self.state)

    async def test_adds_plan_and_tells_me(self):
        self.state.calendar_seen = 10
        watcher = self.watcher([change(title="Dinner at Luigi's", start=tomorrow("19:00"), quote="luigi's tomorrow at 7?")])
        with self.assertLogs("telebot", "INFO"):
            [line] = await watcher.check()
        self.assertIn("Added to your calendar: Dinner at Luigi's", line)
        self.assertEqual(self.calendar.added[0]["summary"], "Dinner at Luigi's")
        self.assertEqual(self.finder.calls[0][1], 11)  # only messages after 10 are new
        self.assertEqual(self.state.calendar_seen, 12)
        [(to, note)] = self.telegram.notes
        self.assertEqual(to, "me")
        self.assertIn("Dinner at Luigi's", note)

        # Nothing new since, so Claude isn't asked again.
        self.assertEqual(await watcher.check(), [])
        self.assertEqual(len(self.finder.calls), 1)

    async def test_small_talk_skips_claude(self):
        now = datetime.now(timezone.utc)
        self.telegram.messages[:0] = [
            SimpleNamespace(id=14, date=now, out=False, message="lol"),
            SimpleNamespace(id=13, date=now, out=True, message="love you ❤️"),
        ]
        self.state.calendar_seen = 12
        await self.watcher([change(title="Dinner", start=tomorrow("19:00"))]).check()
        self.assertEqual(self.finder.calls, [])
        self.assertEqual(self.state.calendar_seen, 14)

    def test_plan_hints(self):
        from bot import PLAN_HINTS

        for text in ("dinner sat?", "7pm works", "tmrw?", "can't make it anymore", "next Thursday", "the 12th"):
            self.assertTrue(PLAN_HINTS.search(text), text)
        for text in ("lol", "love you", "haha same", "omg 😭", "goodnight babe"):
            self.assertFalse(PLAN_HINTS.search(text), text)

    async def test_claude_unavailable_retries_next_time(self):
        watcher = self.watcher(None)
        self.assertEqual(await watcher.check(), [])
        self.assertEqual(self.state.calendar_seen, 0)

    async def test_nothing_to_do_is_quiet(self):
        await self.watcher([]).check()
        self.assertEqual(self.telegram.notes, [])
        self.assertEqual(self.state.calendar_seen, 12)

    async def test_moves_and_cancels_only_its_own_events(self):
        watcher = self.watcher([
            change("update", "ours1", start=tomorrow("21:00"), quote="can we do 9 instead"),
            change("cancel", "x", quote="work thing got cancelled"),
        ])
        with self.assertLogs("telebot", "INFO"):
            [line] = await watcher.check()
        self.assertIn("Updated on your calendar: Movie night", line)
        event_id, body = self.calendar.updated[0]
        self.assertEqual((event_id, body["summary"]), ("ours1", "Movie night"))  # kept the old title
        self.assertTrue(body["start"]["dateTime"].endswith("T21:00:00"))
        self.assertEqual(self.calendar.cancelled, [])

    async def test_cancel(self):
        with self.assertLogs("telebot", "INFO"):
            [line] = await self.watcher([change("cancel", "ours1", quote="movie's off")]).check()
        self.assertIn("Removed from your calendar: Movie night", line)
        self.assertEqual(self.calendar.cancelled, ["ours1"])

    async def test_bad_change_is_skipped(self):
        watcher = self.watcher([change(title="Brunch", start="sometime"), change(title="Brunch", start=tomorrow("11:00"))])
        with self.assertLogs("telebot", "INFO"):
            lines = await watcher.check()
        self.assertEqual(len(lines), 1)

    async def test_dry_run_changes_nothing(self):
        self.state.calendar_seen = 12
        watcher = self.watcher([change(title="Dinner", start=tomorrow("19:00"))])
        with self.assertLogs("telebot", "INFO"):
            self.assertEqual(len(await watcher.check(dry_run=True)), 1)
        self.assertEqual(self.finder.calls[0][1], 10)  # reads the whole recent chat
        self.assertEqual((self.calendar.added, self.telegram.notes, self.state.calendar_seen), ([], [], 12))

    async def test_waits_for_the_conversation_to_pause(self):
        watcher = self.watcher([])
        watcher.quiet_seconds = 0.05
        for message_id in (13, 14):
            await watcher._on_message(SimpleNamespace(message=SimpleNamespace(id=message_id)))
        watcher.ignore.add(15)
        await watcher._on_message(SimpleNamespace(message=SimpleNamespace(id=15)))  # the bot's own text
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.finder.calls), 1)


if __name__ == "__main__":
    unittest.main()
