"""Texts your girlfriend for you, from your own Telegram account.

Setup is in README.md. Quick reference:

    python bot.py                         # run the schedule forever
    python bot.py --plan                  # preview when texts will go out (sends nothing)
    python bot.py --now "good morning"    # send one text from that slot right now
    python bot.py --now "good morning" --to me   # same, but to your Saved Messages

With [ai] turned on in config.toml, Claude writes each text (see ai.py).
"""

import argparse
import asyncio
import json
import logging
import random
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("telebot")


class ConfigError(Exception):
    pass


@dataclass
class Slot:
    name: str
    start: time
    end: time
    messages: list[str]
    chance: float = 1.0

    def window(self, day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """The slot's send window on `day`. An end before the start means it runs past midnight."""
        start = datetime.combine(day, self.start, tz)
        end_day = day if self.end > self.start else day + timedelta(days=1)
        return start, datetime.combine(end_day, self.end, tz)


@dataclass
class AIConfig:
    model: str
    api_key: str | None
    about_us: str
    read_recent_chat: bool


@dataclass
class Config:
    api_id: int | str | None
    api_hash: str | None
    recipient: str
    tz: ZoneInfo
    slots: list[Slot]
    skip_if_texted_within: timedelta | None
    session_path: Path
    state_path: Path
    ai: AIConfig | None = None

    def slot(self, name: str) -> Slot:
        for slot in self.slots:
            if slot.name.lower() == name.lower():
                return slot
        names = ", ".join(f'"{s.name}"' for s in self.slots)
        raise ConfigError(f'No schedule slot named "{name}". You have: {names}')


def _parse_time(value, slot_name: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        raise ConfigError(f'"{slot_name}": couldn\'t read time {value!r}, use "HH:MM" like "07:30"') from None


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found. Copy config.example.toml to {path.name} and fill it in.") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} isn't valid TOML: {e}") from None

    try:
        tz = ZoneInfo(raw.get("timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f'Unknown timezone {raw.get("timezone")!r}, use a name like "America/New_York"') from None

    recipient = str(raw.get("recipient", "")).strip()
    if not recipient:
        raise ConfigError('Set `recipient` to her @username or phone number.')

    slots = []
    for entry in raw.get("schedule", []):
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ConfigError("Every [[schedule]] needs a `name`.")
        between = entry.get("between")
        if not isinstance(between, list) or len(between) != 2:
            raise ConfigError(f'"{name}": `between` should look like ["07:30", "09:00"]')
        start, end = (_parse_time(t, name) for t in between)
        if start == end:
            raise ConfigError(f'"{name}": start and end times are the same')
        messages = [str(m).strip() for m in entry.get("messages", []) if str(m).strip()]
        if not messages:
            raise ConfigError(f'"{name}": add at least one message')
        chance = float(entry.get("chance", 1.0))
        if not 0 < chance <= 1:
            raise ConfigError(f'"{name}": `chance` must be more than 0 and at most 1')
        if any(s.name.lower() == name.lower() for s in slots):
            raise ConfigError(f'Two schedule slots are named "{name}"')
        slots.append(Slot(name, start, end, messages, chance))
    if not slots:
        raise ConfigError("Add at least one [[schedule]] section.")

    ai = None
    ai_raw = raw.get("ai", {})
    if ai_raw.get("enabled", False):
        ai = AIConfig(
            model=str(ai_raw.get("model") or "claude-opus-5-5"),
            api_key=ai_raw.get("api_key") or None,
            about_us=str(ai_raw.get("about_us", "")).strip(),
            read_recent_chat=bool(ai_raw.get("read_recent_chat", True)),
        )

    skip_minutes = float(raw.get("skip_if_i_texted_within_minutes", 0))
    return Config(
        api_id=raw.get("api_id"),
        api_hash=raw.get("api_hash"),
        recipient=recipient,
        tz=tz,
        slots=slots,
        skip_if_texted_within=timedelta(minutes=skip_minutes) if skip_minutes > 0 else None,
        session_path=path.parent / "telebot",
        state_path=path.parent / "state.json",
        ai=ai,
    )


class State:
    """Remembers what was sent, so restarts don't double-text and messages don't repeat.

    With no path it lives in memory only (used for test runs with --to).
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.data = {}
        if path and path.exists():
            self.data = json.loads(path.read_text())
        self.data.setdefault("last_sent", {})
        self.data.setdefault("recent", {})

    def sent_on(self, slot: str, day: date) -> bool:
        return self.data["last_sent"].get(slot, "") >= day.isoformat()

    def recent(self, slot: str) -> list[str]:
        return self.data["recent"].get(slot, [])

    def record(self, slot: str, day: date, message: str | None = None):
        self.data["last_sent"][slot] = day.isoformat()
        if message is not None:
            recent = self.data["recent"].setdefault(slot, [])
            recent.append(message)
            del recent[:-50]
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
            tmp.replace(self.path)


def plan_day(day: date, slots: list[Slot], tz: ZoneInfo, now: datetime, state: State) -> list[tuple[datetime, Slot]]:
    """Pick a random send time for each slot on `day`.

    The randomness is seeded by the date and slot name, so restarting the bot
    gives the same plan instead of re-rolling (and maybe texting twice).
    """
    plan = []
    for slot in slots:
        if state.sent_on(slot.name, day):
            continue
        rng = random.Random(f"{day.isoformat()}/{slot.name}")
        if rng.random() >= slot.chance:
            continue
        start, end = slot.window(day, tz)
        when = start + (end - start) * rng.random()
        if when < now:
            if end <= now:
                continue
            # The bot was off when this should have gone out, but the window is still open.
            when = now + (end - now) * random.random()
        plan.append((when, slot))
    return sorted(plan, key=lambda item: item[0])


def pick_message(slot: Slot, recent: list[str]) -> str:
    # Skip anything sent recently (up to half the list) so it doesn't feel canned.
    n = len(slot.messages) // 2
    avoid = set(recent[-n:]) if n else set()
    return random.choice([m for m in slot.messages if m not in avoid] or slot.messages)


async def sleep_until(when: datetime):
    # Short naps instead of one long sleep, so a laptop waking from sleep doesn't oversleep.
    while (remaining := (when - datetime.now(timezone.utc)).total_seconds()) > 0:
        await asyncio.sleep(min(remaining, 60))


async def connect(cfg: Config):
    try:
        from telethon import TelegramClient
    except ImportError:
        raise ConfigError("Telethon isn't installed. Run: pip install -r requirements.txt") from None

    if not cfg.api_id or not cfg.api_hash or cfg.api_hash == "your_api_hash":
        raise ConfigError("Fill in api_id and api_hash (from https://my.telegram.org > API development tools).")

    client = TelegramClient(str(cfg.session_path), int(cfg.api_id), str(cfg.api_hash))
    await client.start()  # asks for your phone number and login code the first time
    return client


async def find(client, who: str):
    try:
        return await client.get_entity(who)
    except ValueError:
        raise ConfigError(
            f"Couldn't find {who!r} on Telegram. Use her @username, "
            "or a phone number that's saved in your contacts."
        ) from None


async def resolve(client, cfg: Config, recipient: str, writer):
    """Who the texts go to, and her chat (for the AI to read). They differ only for test runs with --to."""
    to = await find(client, recipient)
    if recipient == cfg.recipient:
        return to, to
    reads_chat = writer is not None and cfg.ai.read_recent_chat
    return to, (await find(client, cfg.recipient) if reads_chat else None)


def make_writer(cfg: Config):
    """The AI that writes the texts, or None if [ai] is turned off."""
    if cfg.ai is None:
        return None
    try:
        from ai import Writer
    except ImportError:
        raise ConfigError("AI is on but its packages aren't installed. Run: pip install -r requirements.txt") from None
    writer = Writer(cfg.ai.model, cfg.ai.api_key, cfg.ai.about_us)
    if not writer.has_credentials():
        raise ConfigError(
            "AI is on but there's no Anthropic API key. Set the ANTHROPIC_API_KEY environment "
            "variable, or api_key under [ai] in config.toml (or set enabled = false)."
        )
    if "___" in cfg.ai.about_us:
        log.warning("Tip: fill in about_us under [ai] in config.toml so the texts sound like you two")
    return writer


def _describe(message) -> str:
    for attr, label in (("sticker", "sticker"), ("gif", "GIF"), ("photo", "photo"),
                        ("voice", "voice message"), ("video", "video")):
        if getattr(message, attr, None):
            return f"[{label}]"
    return "[non-text message]"


async def recent_chat(client, her, tz: ZoneInfo) -> list[tuple[datetime, str, str]]:
    """The last few days of your chat with her, oldest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    chat = []
    async for message in client.iter_messages(her, limit=40):
        if message.date < cutoff:
            break
        text = getattr(message, "message", None) or _describe(message)
        chat.append((message.date.astimezone(tz), "me" if message.out else "her", text))
    return chat[::-1]


async def compose(cfg: Config, writer, client, her, slot: Slot, state: State) -> str | None:
    """The text to send for this slot, or None if the AI thinks now's a bad time."""
    recent = state.recent(slot.name)
    if writer is not None:
        chat = await recent_chat(client, her, cfg.tz) if cfg.ai.read_recent_chat else None
        draft = await writer.write(slot.name, slot.messages, datetime.now(cfg.tz), chat, recent)
        if draft is not None:
            if not draft.send:
                log.info('Not sending "%s": %s', slot.name, draft.skip_reason)
                return None
            return draft.message
    return pick_message(slot, recent)


async def texted_recently(client, entity, within: timedelta | None) -> bool:
    if within is None:
        return False
    cutoff = datetime.now(timezone.utc) - within
    async for message in client.iter_messages(entity, limit=30):
        if message.date < cutoff:
            break
        if message.out:
            return True
    return False


async def send(client, entity, text: str):
    for attempt in range(3):
        try:
            # Show "typing..." for a few seconds first, like a person would.
            async with client.action(entity, "typing"):
                await asyncio.sleep(min(2 + len(text) * 0.08, 10))
            await client.send_message(entity, text)
            return
        except ConnectionError:
            if attempt == 2:
                raise
            log.warning("Connection problem, retrying in 30s")
            await asyncio.sleep(30)


async def run_forever(cfg: Config, state: State, writer, recipient: str, skip_within: timedelta | None):
    client = await connect(cfg)
    try:
        to, her = await resolve(client, cfg, recipient, writer)
        log.info("Logged in. Texting %s on schedule%s (Ctrl+C to stop).", recipient, ", AI on" if writer else "")
        # Start from yesterday in case a window that runs past midnight is still open.
        day = datetime.now(cfg.tz).date() - timedelta(days=1)
        while True:
            for when, slot in plan_day(day, cfg.slots, cfg.tz, datetime.now(cfg.tz), state):
                log.info('Next up: "%s" at %s', slot.name, when.strftime("%a %H:%M"))
                await sleep_until(when)
                if datetime.now(cfg.tz) > slot.window(day, cfg.tz)[1]:
                    # The computer was asleep through the whole window. A good-morning text at noon would be weird.
                    log.info('Missed "%s" (computer was probably asleep), skipping it', slot.name)
                    continue
                try:
                    if await texted_recently(client, to, skip_within):
                        log.info('Skipping "%s": you already texted her recently', slot.name)
                        state.record(slot.name, day)
                        continue
                    text = await compose(cfg, writer, client, her, slot, state)
                    if text is None:
                        state.record(slot.name, day)
                        continue
                    await send(client, to, text)
                except Exception:
                    log.exception('Failed to send "%s"', slot.name)
                    continue
                state.record(slot.name, day, text)
                log.info('Sent "%s": %s', slot.name, text)
            day += timedelta(days=1)
            await sleep_until(datetime.combine(day, time(0), cfg.tz))
    finally:
        await client.disconnect()


async def send_now(cfg: Config, state: State, writer, recipient: str, slot: Slot):
    client = await connect(cfg)
    try:
        to, her = await resolve(client, cfg, recipient, writer)
        text = await compose(cfg, writer, client, her, slot, state)
        if text is None:
            return
        await send(client, to, text)
        state.record(slot.name, datetime.now(cfg.tz).date(), text)
        log.info("Sent to %s: %s", recipient, text)
    finally:
        await client.disconnect()


def print_plan(cfg: Config, state: State, days: int = 3):
    now = datetime.now(cfg.tz)
    for offset in range(days):
        day = now.date() + timedelta(days=offset)
        plan = plan_day(day, cfg.slots, cfg.tz, now, state)
        print(day.strftime("%A %b %d"))
        for when, slot in plan:
            print(f"  {when:%H:%M}  {slot.name}")
        if not plan:
            print("  (nothing left)" if offset == 0 else "  (nothing)")


def main():
    parser = argparse.ArgumentParser(description="Automatically text your girlfriend on Telegram.")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="default: config.toml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="show when the next texts will go out, then exit")
    mode.add_argument("--now", metavar="SLOT", help="send one text for this schedule slot right now")
    parser.add_argument("--to", metavar="WHO", help='send to someone else instead, e.g. "me" for your Saved Messages')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
        # Test runs with --to don't touch the real state, so they can't use up today's texts.
        state = State(None if args.to else cfg.state_path)
        recipient = args.to or cfg.recipient
        if args.plan:
            print_plan(cfg, state)
        elif args.now:
            slot = cfg.slot(args.now)
            asyncio.run(send_now(cfg, state, make_writer(cfg), recipient, slot))
        else:
            skip_within = None if args.to else cfg.skip_if_texted_within
            asyncio.run(run_forever(cfg, state, make_writer(cfg), recipient, skip_within))
    except ConfigError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
