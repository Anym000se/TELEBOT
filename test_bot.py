import asyncio
import importlib.util
from unittest import mock
import json
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import gcal
from bot import (
    AIConfig, AutoReplier, AutoReplyConfig, BriefConfig, CalendarWatcher, ChatLine, ConfigError, ImportantDate,
    MorningBrief, Reminders, RemindersConfig, Slot, State, compose, dates_coming_up, describe_date,
    first_unanswered, in_quiet_hours, load_config, next_occurrence, pick_message, ping, plan_day, recent_chat,
)

HAS_ANTHROPIC = importlib.util.find_spec("anthropic") is not None
try:
    HAS_GOOGLE = importlib.util.find_spec("google.oauth2") is not None
except ModuleNotFoundError:
    HAS_GOOGLE = False

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
        self.assertEqual(cfg.ai.read_last_messages, 10)
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

    def test_api_key_found_in_the_wrong_place(self):
        base = 'recipient = "@x"\n[ai]\nenabled = true\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = ["hi"]\n'
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for text in ('api_key = "sk-ant-top"\n' + base, base + 'api_key = "sk-ant-top"\n'):
                path.write_text(text)
                self.assertEqual(load_config(path).ai.api_key, "sk-ant-top")

    def test_api_key_mistakes_are_explained(self):
        base = 'recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = ["hi"]\n'
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('api_hash = "sk-ant-api03-abc"\n' + base)
            with self.assertRaisesRegex(ConfigError, "Move it to api_key"):
                load_config(path)
            path.write_text(base + '[ai]\n# api_key = "sk-ant-api03-' + "x" * 40 + '"\n')
            self.assertIn("Delete the #", load_config(path).ai.key_hint)
            path.write_text(base + '[ai]\n# api_key = "sk-ant-..."\n')  # the untouched example line
            self.assertEqual(load_config(path).ai.key_hint, "")

    def test_bad_config(self):
        cases = {
            'recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["7:3", "9:00"]\nmessages = ["hi"]': "couldn't read time",
            'recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = []': "at least one message",
            'recipient = "@x"\ntimezone = "Mars/Base"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = ["hi"]': "Unknown timezone",
            'recipient = "@x"': "at least one [[schedule]]",
            'recipient = "@x"\n[ai]\nread_last_messages = "5"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = ["hi"]': "1 to 100",
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

    async def test_preview_shows_held_back_draft(self):
        writer = FakeWriter(SimpleNamespace(send=False, message="have fun at the concert", skip_reason="she's busy"))
        text = await compose(ai_config(), writer, None, None, slot(), State(None), preview=True)
        self.assertTrue(text.startswith("have fun at the concert"))
        self.assertIn("wouldn't send this right now: she's busy", text)

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
        chat = await recent_chat(telegram, "her", TZ, limit=10)
        self.assertEqual([(line.id, line.text) for line in chat], [(1, "first"), (2, "second")])
        chat = await recent_chat(telegram, "her", TZ, limit=1)
        self.assertEqual([line.text for line in chat], ["second"])  # only the newest


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
class SDKTests(unittest.TestCase):
    def test_version_check(self):
        import anthropic

        import ai

        real = anthropic.__version__
        try:
            anthropic.__version__ = "0.49.0"
            self.assertEqual(ai.sdk_too_old(), "0.49.0")
            anthropic.__version__ = "1.11.0"
            self.assertIsNone(ai.sdk_too_old())
        finally:
            anthropic.__version__ = real

    def test_credentials_without_credentials_attribute(self):
        import ai

        self.assertTrue(ai.has_credentials(SimpleNamespace(api_key="sk-ant-x", auth_token=None)))
        self.assertFalse(ai.has_credentials(SimpleNamespace(api_key=None, auth_token=None)))


@unittest.skipUnless(HAS_ANTHROPIC, "anthropic not installed")
class PlanFinderTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_and_response(self):
        from ai import PlanFinder

        change = {"action": "add", "event_id": "", "title": "Dinner at Luigi's", "start": "2026-10-03T19:00",
                  "end": "", "time_was_said": True, "location": "Luigi's", "quote": "luigi's saturday at 7?"}
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
        self.assertIn("time_was_said", json.dumps(sent["output_config"]["format"]["schema"]))
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


class FindGoogleCredentialsTests(unittest.TestCase):
    def test_finds_misnamed_or_original_file(self):
        from bot import find_google_credentials

        for name in ("google-credentials.json", "google-credentials.json.json", "google-credentials",
                     "client_secret_123-abc.apps.googleusercontent.com.json"):
            with TemporaryDirectory() as tmp:
                folder = Path(tmp)
                (folder / name).write_text("{}")
                with self.assertLogs("telebot", "INFO") if name != "google-credentials.json" else _no_logs():
                    found = find_google_credentials(folder / "google-credentials.json", downloads=folder / "nope")
                self.assertEqual(found.name, name)

    def test_points_at_downloads(self):
        from bot import find_google_credentials

        with TemporaryDirectory() as tmp, TemporaryDirectory() as downloads:
            (Path(downloads) / "client_secret_1.json").write_text("{}")
            with self.assertRaisesRegex(ConfigError, "(?s)still in your Downloads folder.*client_secret_1.json"):
                find_google_credentials(Path(tmp) / "google-credentials.json", downloads=Path(downloads))
            (Path(downloads) / "client_secret_1.json").unlink()
            key = {"type": "service_account", "client_email": "telebot@telebot-1.iam.gserviceaccount.com"}
            (Path(downloads) / "telebot-1-0123456789ab.json").write_text(json.dumps(key))
            (Path(downloads) / "unrelated.json").write_text("{}")
            with self.assertRaisesRegex(ConfigError, "(?s)still in your Downloads folder.*telebot-1-0123456789ab.json"):
                find_google_credentials(Path(tmp) / "google-credentials.json", downloads=Path(downloads))
            (Path(downloads) / "telebot-1-0123456789ab.json").unlink()
            with self.assertRaisesRegex(ConfigError, "Download JSON straight away"):
                find_google_credentials(Path(tmp) / "google-credentials.json", downloads=Path(downloads))


class ServiceAccountTests(unittest.TestCase):
    def key(self, folder, **extra):
        path = Path(folder) / "google-credentials.json"
        path.write_text(json.dumps({"type": "service_account", "client_email": "telebot@telebot-1.iam.gserviceaccount.com", **extra}))
        return path

    def test_detects_service_account_key(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(gcal.service_account_email(self.key(tmp)), "telebot@telebot-1.iam.gserviceaccount.com")
            (Path(tmp) / "client.json").write_text('{"installed": {}}')
            self.assertIsNone(gcal.service_account_email(Path(tmp) / "client.json"))
            self.assertIsNone(gcal.service_account_email(Path(tmp) / "missing.json"))

    @unittest.skipUnless(HAS_GOOGLE, "google-auth not installed")
    def test_signs_in_with_the_key(self):
        import rsa
        from google.oauth2 import service_account

        _, private = rsa.newkeys(1024)
        with TemporaryDirectory() as tmp:
            path = self.key(tmp, private_key=private.save_pkcs1().decode(), private_key_id="1",
                            token_uri="https://oauth2.googleapis.com/token", project_id="telebot-1", client_id="1")
            session = gcal.login(path, Path(tmp) / "google-token.json")
            self.assertIsInstance(session.credentials, service_account.Credentials)
            self.assertEqual(session.credentials.service_account_email, "telebot@telebot-1.iam.gserviceaccount.com")
            self.assertFalse((Path(tmp) / "google-token.json").exists())  # nothing to remember

    def test_rejected_key_message(self):
        class Rejected:
            def request(self, *args, **kwargs):
                raise gcal.RefreshError("invalid_grant: Invalid JWT Signature.")

        with self.assertRaisesRegex(gcal.SignedOut, "make a new key for telebot@"):
            gcal.GoogleCalendar(Rejected(), "me@gmail.com", TZ, robot="telebot@telebot-1.iam.gserviceaccount.com").upcoming()

    def test_needs_real_calendar_id(self):
        from bot import CalendarConfig, open_calendar

        with TemporaryDirectory() as tmp:
            path = self.key(tmp)
            cfg = ai_config()
            cfg.calendar = CalendarConfig("primary", True, path, Path(tmp) / "google-token.json")
            with self.assertRaisesRegex(ConfigError, "(?s)calendar_id.*Gmail address.*share your calendar with telebot@"):
                open_calendar(cfg)


class _no_logs:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code
        self.content = json.dumps(data).encode() if data is not None else b""
        self.text = self.content.decode()

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, reply=None, status_code=200):
        self.calls = []
        self.reply = reply
        self.status_code = status_code

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.reply, self.status_code)


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

    def test_expired_sign_in(self):
        class ExpiredSession:
            def request(self, *args, **kwargs):
                raise gcal.RefreshError("invalid_grant: Token has been expired or revoked.")

        with self.assertRaisesRegex(gcal.SignedOut, "every 7 days"):
            gcal.GoogleCalendar(ExpiredSession(), "primary", TZ).upcoming()

    def test_errors_say_what_google_said(self):
        session = FakeSession({"error": {"code": 403, "message": "Google Calendar API has not been used in project 123"}}, 403)
        with self.assertRaisesRegex(gcal.CalendarError, "has not been used in project 123 \\(HTTP 403\\)"):
            gcal.GoogleCalendar(session, "primary", TZ).upcoming()

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

    def upcoming(self, days=60):
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

    async def send_message(self, to, text, schedule=None):
        self.notes.append((to, text))


def change(action="add", event_id="", title="", start="", end="", location="", quote="", time_was_said=True):
    return SimpleNamespace(action=action, event_id=event_id, title=title, start=start, end=end, location=location,
                           quote=quote, time_was_said=time_was_said)


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

    async def test_made_up_time_becomes_all_day(self):
        watcher = self.watcher([change(title="Aquarium", start=tomorrow("14:00"), time_was_said=False, quote="aquarium later")])
        with self.assertLogs("telebot", "INFO"):
            await watcher.check()
        body = self.calendar.added[0]
        self.assertEqual((body["start"], body["end"]), ({"date": tomorrow()}, {"date": (date.fromisoformat(tomorrow()) + timedelta(days=1)).isoformat()}))

    async def test_moving_to_another_day_keeps_the_time(self):
        day_after = (date.fromisoformat(tomorrow()) + timedelta(days=1)).isoformat()
        watcher = self.watcher([change("update", "ours1", start=day_after, time_was_said=False, quote="sunday instead?")])
        with self.assertLogs("telebot", "INFO"):
            await watcher.check()
        _, body = self.calendar.updated[0]
        self.assertEqual(body["start"]["dateTime"], f"{day_after}T20:00:00")
        self.assertEqual(body["end"]["dateTime"], f"{day_after}T22:00:00")

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

    async def test_signed_out_tells_me_once(self):
        def signed_out():
            raise gcal.SignedOut("Google signed the bot out")

        self.calendar.upcoming = signed_out
        watcher = self.watcher([])
        with self.assertLogs("telebot", "WARNING"):
            await watcher._check_safely()
            self.state.calendar_seen = 0
            await watcher._check_safely()
        [(to, note)] = self.telegram.notes
        self.assertEqual(to, "me")
        self.assertIn("Calendar sync has stopped", note)

    async def test_waits_for_the_conversation_to_pause(self):
        watcher = self.watcher([])
        watcher.quiet_seconds = 0.05
        with self.assertLogs("telebot", "INFO") as logs:
            for message_id in (13, 14):
                await watcher._on_message(SimpleNamespace(message=SimpleNamespace(id=message_id)))
        self.assertEqual(len(logs.output), 1)  # says it's waiting once, not per message
        watcher.ignore.add(15)
        await watcher._on_message(SimpleNamespace(message=SimpleNamespace(id=15)))  # the bot's own text
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.finder.calls), 1)


def line(message_id, who, minutes_ago, text="hi"):
    return ChatLine(message_id, datetime.now(TZ) - timedelta(minutes=minutes_ago), who, text)


class FirstUnansweredTests(unittest.TestCase):
    def test_cases(self):
        her_after_me = [line(1, "me", 60), line(2, "her", 40), line(3, "her", 35)]
        self.assertEqual(first_unanswered(her_after_me, 0).id, 2)
        self.assertIsNone(first_unanswered([line(1, "her", 40), line(2, "me", 10)], 0))  # you replied
        self.assertIsNone(first_unanswered(her_after_me[:1] + [line(9, "me", 30), line(10, "her", 5)], 9))  # bot already replied
        self.assertEqual(first_unanswered([line(1, "her", 50), line(2, "her", 40)], 0).id, 1)
        self.assertIsNone(first_unanswered([], 0))

    def test_quiet_hours(self):
        overnight = (time(23, 30), time(7, 30))
        self.assertTrue(in_quiet_hours(at(23, 45), overnight))
        self.assertTrue(in_quiet_hours(at(3), overnight))
        self.assertFalse(in_quiet_hours(at(12), overnight))
        self.assertTrue(in_quiet_hours(at(13), (time(12), time(14))))
        self.assertFalse(in_quiet_hours(at(3), None))


class AutoReplyConfigTests(unittest.TestCase):
    base = 'recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = ["hi"]\n'

    def load(self, extra):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(self.base + extra)
            return load_config(path)

    def test_defaults_and_off(self):
        self.assertIsNone(self.load("").auto_reply)
        reply = self.load("[auto_reply]\nenabled = true\n").auto_reply
        self.assertEqual((reply.after, reply.quiet_hours, reply.notify_me), (timedelta(minutes=30), (time(23, 30), time(7, 30)), True))
        self.assertIsNone(self.load("[auto_reply]\nenabled = true\nquiet_hours = []\n").auto_reply.quiet_hours)

    def test_bad_values(self):
        for extra, error in (("after_minutes = 1", "5 to 720"), ('quiet_hours = ["23:30"]', "quiet_hours")):
            with self.assertRaisesRegex(ConfigError, error):
                self.load(f"[auto_reply]\nenabled = true\n{extra}\n")

    def test_example_config_has_it_off(self):
        self.assertIsNone(load_config(Path(__file__).with_name("config.example.toml")).auto_reply)


class FakeReplier:
    def __init__(self, draft):
        self.draft = draft
        self.calls = []

    async def write(self, now, waited_minutes, chat):
        self.calls.append((waited_minutes, chat))
        return self.draft


class AutoReplierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = ai_config()
        self.cfg.auto_reply = AutoReplyConfig(timedelta(minutes=30), None, True)
        self.state = State(None)
        self.sent = []

    def chat(self, *messages):
        # (id, out, minutes ago, text), oldest first
        now = datetime.now(timezone.utc)
        newest_first = [SimpleNamespace(id=i, out=out, date=now - timedelta(minutes=ago), message=text)
                        for i, out, ago, text in reversed(messages)]
        self.telegram = ChatWithNotes(newest_first)

    def replier(self, draft):
        self.fake = FakeReplier(draft)
        self.auto = AutoReplier(self.cfg, self.telegram, "her", self.fake, self.state, set())
        return self.auto

    async def fake_send(self, client, entity, text):
        self.sent.append((entity, text))
        return SimpleNamespace(id=100 + len(self.sent))

    async def check(self):
        with mock.patch("bot.send", self.fake_send):
            return await self.auto.check()

    async def asyncTearDown(self):
        if getattr(self, "auto", None) and self.auto._timer:
            self.auto._timer.cancel()

    async def test_replies_once_when_she_has_waited(self):
        self.chat((1, True, 90, "see you later"), (2, False, 40, "hows work going"), (3, False, 35, "?"))
        self.replier(SimpleNamespace(send=True, message="sorry busy, will text you properly soon", skip_reason=""))
        with self.assertLogs("telebot", "INFO"):
            self.assertEqual(await self.check(), "sorry busy, will text you properly soon")
        self.assertEqual(self.sent, [("her", "sorry busy, will text you properly soon")])
        self.assertEqual(self.fake.calls[0][0], 40)
        self.assertEqual(self.state.auto_reply_id, 101)
        self.assertIn("I replied for you", self.telegram.notes[0][1])

        # The reply shows up in the chat, then she texts again: no second auto-reply.
        self.telegram.messages[:0] = [SimpleNamespace(id=4, out=False, date=datetime.now(timezone.utc), message="ok!"),
                                      SimpleNamespace(id=101, out=True, date=datetime.now(timezone.utc), message="sorry busy")]
        self.assertIsNone(await self.check())
        self.assertEqual(len(self.sent), 1)

    async def test_waits_until_its_been_long_enough(self):
        self.chat((1, False, 10, "hey"))
        self.replier(SimpleNamespace(send=True, message="x", skip_reason=""))
        self.assertIsNone(await self.check())
        self.assertEqual(self.fake.calls, [])
        self.assertIsNotNone(self.auto._timer)  # checks again when the 30 minutes are up

    async def test_nothing_when_you_replied(self):
        self.chat((1, False, 50, "hey"), (2, True, 45, "hey!"))
        self.replier(SimpleNamespace(send=True, message="x", skip_reason=""))
        self.assertIsNone(await self.check())
        self.assertEqual(self.fake.calls, [])

    async def test_too_old_and_quiet_hours(self):
        self.chat((1, False, 180, "hey"))
        self.replier(SimpleNamespace(send=True, message="x", skip_reason=""))
        with self.assertLogs("telebot", "INFO"):
            self.assertIsNone(await self.check())
        now = datetime.now(TZ)
        self.cfg.auto_reply.quiet_hours = ((now - timedelta(hours=1)).time(), (now + timedelta(hours=1)).time())
        self.chat((1, False, 40, "hey"))
        self.replier(SimpleNamespace(send=True, message="x", skip_reason=""))
        with self.assertLogs("telebot", "INFO"):
            self.assertIsNone(await self.check())
        self.assertEqual((self.fake.calls, self.sent), ([], []))

    async def test_serious_message_is_left_for_you(self):
        self.chat((1, False, 40, "can we talk? im really upset"))
        self.replier(SimpleNamespace(send=False, message="sorry, later?", skip_reason="she's upset and needs you"))
        with self.assertLogs("telebot", "INFO"):
            self.assertIsNone(await self.check())
        self.assertEqual(self.sent, [])
        self.assertIn("this needs you", self.telegram.notes[0][1])
        self.assertIsNone(await self.check())  # Claude isn't asked again for the same stretch
        self.assertEqual(len(self.fake.calls), 1)

    async def test_claude_unreachable_sends_nothing(self):
        self.chat((1, False, 40, "hey"))
        self.replier(None)
        self.assertIsNone(await self.check())
        self.assertEqual((self.sent, self.telegram.notes, self.state.auto_reply_id), ([], [], 0))

    async def test_reads_only_the_latest_messages(self):
        self.cfg.ai.read_last_messages = 2
        self.chat((1, True, 90, "a"), (2, False, 50, "b"), (3, False, 40, "c"))
        self.replier(SimpleNamespace(send=True, message="x", skip_reason=""))
        with self.assertLogs("telebot", "INFO"):
            await self.check()
        self.assertEqual([chat_line.text for chat_line in self.fake.calls[0][1]], ["b", "c"])


@unittest.skipUnless(HAS_ANTHROPIC, "anthropic not installed")
class ReplierTests(unittest.IsolatedAsyncioTestCase):
    async def test_request(self):
        from ai import Replier

        mock_claude = MockClaude(body=MockClaude.reply({"send": True, "message": "sorry busy, text u soon", "skip_reason": ""}))
        draft = await Replier(mock_claude.client, "claude-opus-5-5", "Her name is Sam.").write(
            at(14, 10), 34, [ChatLine(5, at(13, 36), "her", "hows work")])
        self.assertEqual(draft.message, "sorry busy, text u soon")
        sent = json.loads(mock_claude.requests[0].content)
        self.assertIn("holding reply", json.dumps(sent["system"]))
        self.assertIn("waiting about 34 minutes", sent["messages"][0]["content"])
        self.assertIn("her: hows work", sent["messages"][0]["content"])


class ImportantDateTests(unittest.TestCase):
    def test_monthly(self):
        together = ImportantDate("our anniversary", date(2026, 8, 2), "month", 3)
        self.assertEqual(next_occurrence(together, date(2026, 10, 1)), (date(2026, 10, 2), 2))
        self.assertEqual(next_occurrence(together, date(2026, 10, 2)), (date(2026, 10, 2), 2))  # today counts
        self.assertEqual(next_occurrence(together, date(2026, 10, 3)), (date(2026, 11, 2), 3))
        self.assertEqual(next_occurrence(together, date(2026, 12, 20)), (date(2027, 1, 2), 5))

    def test_short_months_and_leap_days(self):
        end_of_month = ImportantDate("x", date(2026, 1, 31), "month", 3)
        self.assertEqual(next_occurrence(end_of_month, date(2026, 2, 10)), (date(2026, 2, 28), 1))
        leap = ImportantDate("x", date(2024, 2, 29), "year", 3)
        self.assertEqual(next_occurrence(leap, date(2026, 1, 1)), (date(2026, 2, 28), 2))

    def test_yearly_and_future(self):
        birthday = ImportantDate("her birthday", date(2006, 3, 14), "year", 7)
        self.assertEqual(next_occurrence(birthday, date(2026, 10, 3)), (date(2027, 3, 14), 21))
        later = ImportantDate("trip", date(2026, 12, 1), "year", 7)
        self.assertEqual(next_occurrence(later, date(2026, 10, 3)), (date(2026, 12, 1), 0))

    def test_describe(self):
        together = ImportantDate("our anniversary", date(2026, 8, 2), "month", 3)
        self.assertEqual(describe_date(together, 3), "our anniversary (3 months)")
        self.assertEqual(describe_date(together, 1), "our anniversary (1 month)")
        self.assertEqual(describe_date(together, 12), "our anniversary (1 year)")
        self.assertEqual(describe_date(ImportantDate("her birthday", date(2006, 3, 14), "year", 7), 21), "her birthday (turns 21)")
        self.assertEqual(describe_date(together, 0), "our anniversary")

    def test_coming_up(self):
        together = ImportantDate("our anniversary", date(2026, 8, 2), "month", 3)
        self.assertEqual(dates_coming_up([together], date(2026, 10, 2)), ["🎉 Today: our anniversary (2 months)!"])
        self.assertEqual(dates_coming_up([together], date(2026, 11, 1)),
                         ["🎉 our anniversary (3 months) is tomorrow. Maybe plan something?"])
        self.assertIn("in 3 days (Mon Nov 2)", dates_coming_up([together], date(2026, 10, 30))[0])
        self.assertEqual(dates_coming_up([together], date(2026, 10, 20)), [])


class ExtrasConfigTests(unittest.TestCase):
    base = 'recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = ["hi"]\n'

    def load(self, extra):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(self.base + extra)
            return load_config(path)

    def test_example_config(self):
        cfg = load_config(Path(__file__).with_name("config.example.toml"))
        self.assertEqual(cfg.reminders, RemindersConfig(timedelta(minutes=60), True))
        self.assertEqual(cfg.brief, BriefConfig(time(8), 3))
        self.assertEqual(cfg.dates, [])

    def test_dates(self):
        cfg = self.load('[[dates]]\nname = "our anniversary"\ndate = "2026-08-02"\nevery = "month"\n')
        self.assertEqual(cfg.dates, [ImportantDate("our anniversary", date(2026, 8, 2), "month", 3)])

    def test_mistakes(self):
        cases = {
            "[reminders]\nenabled = true\n": "need calendar sync",
            '[morning_brief]\nenabled = true\ndate_ideas_on = "thurs"\n': "date_ideas_on",
            '[[dates]]\nname = "x"\ndate = "02/08/2026"\n': "should look like",
            '[[dates]]\nname = "x"\ndate = "2026-08-02"\nevery = "week"\n': '"month" or "year"',
        }
        for extra, error in cases.items():
            with self.assertRaisesRegex(ConfigError, error):
                self.load(extra)
        self.assertIsNone(self.load('[morning_brief]\nenabled = true\ndate_ideas_on = ""\n').brief.date_ideas_on)


class PingTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedules_so_the_phone_buzzes(self):
        calls = []

        class Client:
            async def send_message(self, to, text, schedule=None):
                calls.append((to, text, schedule))

        later = datetime.now(TZ) + timedelta(hours=1)
        await ping(Client(), "hi", at=later)
        await ping(Client(), "now")
        self.assertEqual(calls[0], ("me", "hi", later))
        self.assertGreater(calls[1][2], datetime.now(timezone.utc))  # still scheduled, just a few seconds out

    async def test_falls_back_to_a_normal_note(self):
        calls = []

        class Client:
            async def send_message(self, to, text, schedule=None):
                if schedule is not None:
                    raise RuntimeError("SCHEDULE_DATE_INVALID")
                calls.append(text)

        with self.assertLogs("telebot", "WARNING"):
            await ping(Client(), "hi")
        self.assertEqual(calls, ["hi"])


def in_minutes(minutes):
    return (datetime.now(TZ) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


class RemindersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = ai_config()
        self.cfg.reminders = RemindersConfig(timedelta(minutes=60), True)
        self.pings = []

    async def fake_ping(self, client, text, at=None):
        self.pings.append((text, at))

    async def check(self, events, state):
        with mock.patch("bot.ping", self.fake_ping):
            return await Reminders(self.cfg, None, FakeCalendar(events), state).check()

    async def test_reminds_once_before_plans(self):
        state = State(None)
        events = [
            gcal.Event("a", "Aquarium with Raya", in_minutes(65), "", ours=True),  # reminder due in 5 min
            gcal.Event("b", "Dinner", in_minutes(300), "", ours=True),  # too far off, next check gets it
            gcal.Event("c", "Work meeting", in_minutes(65), "", ours=False),  # not a plan from the chat
            gcal.Event("d", "Her exam", tomorrow(), "", ours=True),  # all day: that's the brief's job
            gcal.Event("e", "Coffee", in_minutes(20), "", ours=True),  # added late: remind right away
        ]
        with self.assertLogs("telebot", "INFO"):
            await self.check(events, state)
        texts = [text for text, _ in self.pings]
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].startswith("⏰ Aquarium with Raya at ") and texts[0].endswith("in 1 hour"))
        self.assertRegex(texts[1], r"Coffee at .*, in (19|20) min$")

        self.pings.clear()
        await self.check(events, state)
        self.assertEqual(self.pings, [])  # no repeats

        events[0] = gcal.Event("a", "Aquarium with Raya", in_minutes(70), "", ours=True)  # moved
        with self.assertLogs("telebot", "INFO"):
            await self.check(events, state)
        self.assertEqual(len(self.pings), 1)

    async def test_everything_on_the_calendar(self):
        self.cfg.reminders.only_plans_from_chat = False
        with self.assertLogs("telebot", "INFO"):
            await self.check([gcal.Event("c", "Work meeting", in_minutes(65), "", ours=False)], State(None))
        self.assertEqual(len(self.pings), 1)


class FakeBriefer:
    def __init__(self, notes):
        self.notes = notes
        self.calls = []

    async def write(self, now, place, chat, plans, want_ideas):
        self.calls.append((place, plans, want_ideas))
        return self.notes


class MorningBriefTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = ai_config()
        self.now = datetime.now(TZ).replace(hour=8, minute=0)
        today = self.now.date()
        self.cfg.brief = BriefConfig(time(8), today.weekday())
        self.cfg.dates = [ImportantDate("our anniversary", today - timedelta(days=60) + timedelta(days=2), "year", 3)]
        self.calendar = FakeCalendar([
            gcal.Event("a", "Aquarium with Raya", f"{today.isoformat()}T14:00", "", ours=True),
            gcal.Event("b", "Her lifeguard shift", today.isoformat(), "", ours=True),
            gcal.Event("c", "Tomorrow thing", f"{(today + timedelta(days=1)).isoformat()}T09:00", "", ours=True),
        ])
        self.telegram = FakeTelegram([SimpleNamespace(id=1, date=datetime.now(timezone.utc), out=False, message="shift tmrw ugh")])

    async def test_full_brief(self):
        briefer = FakeBriefer(SimpleNamespace(ask_about=["how her shift went"], date_ideas=["picnic at the botanic gardens"]))
        text = await MorningBrief(self.cfg, self.telegram, "her", briefer, self.calendar, State(None)).build(self.now)
        self.assertTrue(text.startswith(f"☀️ {self.now:%A}"))
        self.assertIn("• 2:00pm Aquarium with Raya", text)
        self.assertIn("• All day: Her lifeguard shift", text)
        self.assertNotIn("Tomorrow thing", text)
        self.assertIn("Ask her about:\n• how her shift went", text)
        self.assertIn("Date ideas for this week:\n• picnic at the botanic gardens", text)
        place, plans, want_ideas = briefer.calls[0]
        self.assertEqual((place, want_ideas), ("New_York".replace("_", " "), True))
        self.assertEqual(len(plans), 2)

    async def test_dates_show_up(self):
        self.cfg.dates = [ImportantDate("our anniversary", self.now.date() - timedelta(days=365), "year", 3)]
        text = await MorningBrief(self.cfg, self.telegram, "her", None, None, State(None)).build(self.now)
        self.assertIn("🎉 Today: our anniversary (1 year)!", text)

    async def test_nothing_to_say(self):
        self.cfg.dates = []
        briefer = FakeBriefer(SimpleNamespace(ask_about=[], date_ideas=[]))
        self.assertIsNone(await MorningBrief(self.cfg, self.telegram, "her", briefer, FakeCalendar([]), State(None)).build(self.now))

    async def test_claude_unreachable_still_sends_the_rest(self):
        text = await MorningBrief(self.cfg, self.telegram, "her", FakeBriefer(None), self.calendar, State(None)).build(self.now)
        self.assertIn("Aquarium with Raya", text)
        self.assertNotIn("Ask her about", text)


@unittest.skipUnless(HAS_ANTHROPIC, "anthropic not installed")
class BriefWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_request(self):
        from ai import BriefWriter

        mock_claude = MockClaude(body=MockClaude.reply({"ask_about": ["her shift"], "date_ideas": []}))
        notes = await BriefWriter(mock_claude.client, "claude-opus-5-5", "Her name is Sam.").write(
            at(8), "Melbourne", [ChatLine(1, at(7), "her", "shift today ugh")], ["2:00pm Aquarium"], want_ideas=False)
        self.assertEqual(notes.ask_about, ["her shift"])
        prompt = json.loads(mock_claude.requests[0].content)["messages"][0]["content"]
        self.assertIn("We live in Melbourne.", prompt)
        self.assertIn("- 2:00pm Aquarium", prompt)
        self.assertIn("Date ideas: not today.", prompt)
        self.assertIn("her: shift today ugh", prompt)


if __name__ == "__main__":
    unittest.main()
