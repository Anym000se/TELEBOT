import importlib.util
import json
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot import AIConfig, ConfigError, Slot, State, compose, load_config, pick_message, plan_day, recent_chat

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

    def test_ai_off_by_default(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('recipient = "@x"\n[[schedule]]\nname = "a"\nbetween = ["07:00", "09:00"]\nmessages = ["hi"]')
            self.assertIsNone(load_config(path).ai)

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
    cfg.ai = AIConfig("claude-opus-5-5", None, "", read_recent_chat)
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
            SimpleNamespace(date=now - timedelta(minutes=5), out=False, message="", sticker=True),
            SimpleNamespace(date=now - timedelta(minutes=9), out=True, message="how'd it go?"),
            SimpleNamespace(date=now - timedelta(days=5), out=False, message="too old"),
        ])
        writer = FakeWriter(SimpleNamespace(send=True, message="x", skip_reason=""))
        await compose(ai_config(read_recent_chat=True), writer, telegram, "her", slot(), State(None))
        chat = writer.calls[0][3]
        self.assertEqual([(who, text) for _, who, text in chat], [("me", "how'd it go?"), ("her", "[sticker]")])

    async def test_recent_chat_is_oldest_first(self):
        now = datetime.now(timezone.utc)
        telegram = FakeTelegram([
            SimpleNamespace(date=now, out=False, message="second"),
            SimpleNamespace(date=now - timedelta(hours=1), out=True, message="first"),
        ])
        chat = await recent_chat(telegram, "her", TZ)
        self.assertEqual([text for _, _, text in chat], ["first", "second"])


@unittest.skipUnless(HAS_ANTHROPIC, "anthropic not installed")
class WriterTests(unittest.IsolatedAsyncioTestCase):
    def writer(self, status=200, body=None):
        import anthropic
        import httpx2

        from ai import Writer

        self.requests = []

        def handler(request):
            self.requests.append(request)
            return httpx2.Response(status, json=body)

        writer = Writer("claude-opus-5-5", "test-key", "Her name is Sam.")
        writer.client = anthropic.AsyncAnthropic(
            api_key="test-key",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        )
        return writer

    @staticmethod
    def reply(draft, stop_reason="end_turn"):
        content = [{"type": "text", "text": json.dumps(draft)}] if draft else []
        return {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    async def write(self, writer):
        chat = [(at(22, 40, DAY - timedelta(days=1)), "her", "big exam tomorrow 😭")]
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
        self.assertIn("[Wed 22:40] her: big exam tomorrow 😭", prompt)
        self.assertIn("- morning!", prompt)

    async def test_skip(self):
        writer = self.writer(body=self.reply({"send": False, "message": "", "skip_reason": "she's upset"}))
        draft = await self.write(writer)
        self.assertFalse(draft.send)
        self.assertEqual(draft.skip_reason, "she's upset")

    async def test_refusal_falls_back(self):
        self.assertIsNone(await self.write(self.writer(body=self.reply(None, stop_reason="refusal"))))

    async def test_api_error_falls_back(self):
        body = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
        with self.assertLogs("telebot", "WARNING"):
            self.assertIsNone(await self.write(self.writer(status=500, body=body)))

    async def test_bad_key_falls_back(self):
        body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        with self.assertLogs("telebot", "ERROR"):
            self.assertIsNone(await self.write(self.writer(status=401, body=body)))


if __name__ == "__main__":
    unittest.main()
