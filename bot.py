"""Texts your girlfriend for you, from your own Telegram account.

Setup is in README.md. Quick reference:

    python bot.py                         # run the schedule forever
    python bot.py --plan                  # preview when texts will go out (sends nothing)
    python bot.py --now "good morning"    # send one text from that slot right now
    python bot.py --now "good morning" --to me   # same, but to your Saved Messages
    python bot.py --check-calendar        # show what it would put on your calendar (changes nothing)

With [ai] turned on in config.toml, Claude writes each text (see ai.py). With [calendar]
turned on, plans you make in the chat go on your Google Calendar (see gcal.py).
"""

import argparse
import asyncio
import json
import logging
import random
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import gcal

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
    write_texts: bool
    model: str
    api_key: str | None
    about_us: str
    read_recent_chat: bool
    read_last_messages: int = 10
    key_hint: str = ""  # what's wrong, if the key looks mistyped in config.toml


@dataclass
class CalendarConfig:
    calendar_id: str
    notify_me: bool
    credentials_path: Path
    token_path: Path


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
    ai: AIConfig
    calendar: CalendarConfig | None = None

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


def _find_api_key(raw: dict) -> str | None:
    """The Anthropic key from [ai], or from wherever it got pasted by mistake.

    A line added at the bottom of the file belongs to the last section, so look there too.
    """
    for place in (raw.get("ai", {}), raw, raw.get("calendar", {}), *raw.get("schedule", [])):
        if isinstance(place, dict) and str(place.get("api_key", "")).strip():
            return str(place["api_key"]).strip()
    return None


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found. Copy config.example.toml to {path.name} and fill it in.") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"{path} has a mistake: {e}. Check that line. Text like api_hash, recipient, timezone and "
            'api_key needs "straight quotes" around it; numbers like api_id don\'t.'
        ) from None

    if str(raw.get("api_hash", "")).strip().startswith("sk-ant"):
        raise ConfigError(
            "api_hash has your Anthropic key in it (it starts with sk-ant-). Move it to api_key under "
            "[ai]. api_hash is the Telegram one from my.telegram.org."
        )

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

    ai_raw = raw.get("ai", {})
    read_last = ai_raw.get("read_last_messages", 10)
    if isinstance(read_last, bool) or not isinstance(read_last, int) or not 1 <= read_last <= 100:
        raise ConfigError("read_last_messages under [ai] should be a number from 1 to 100 (no quotes).")
    api_key = _find_api_key(raw)
    key_hint = ""
    if not api_key and re.search(r"^\s*#\s*api_key\s*=\s*\S*sk-ant-[\w-]{20,}", path.read_text(errors="replace"), re.M):
        key_hint = ' Your key is in config.toml, but its line starts with "#", which switches it off. Delete the #.'
    ai = AIConfig(
        write_texts=bool(ai_raw.get("enabled", False)),
        model=str(ai_raw.get("model") or "claude-opus-5-5"),
        api_key=api_key,
        about_us=str(ai_raw.get("about_us", "")).strip(),
        read_recent_chat=bool(ai_raw.get("read_recent_chat", True)),
        read_last_messages=read_last,
        key_hint=key_hint,
    )

    calendar = None
    calendar_raw = raw.get("calendar", {})
    if calendar_raw.get("enabled", False):
        calendar = CalendarConfig(
            calendar_id=str(calendar_raw.get("calendar_id") or "primary"),
            notify_me=bool(calendar_raw.get("notify_me", True)),
            credentials_path=path.parent / "google-credentials.json",
            token_path=path.parent / "google-token.json",
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
        calendar=calendar,
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
        self.save()

    @property
    def calendar_seen(self) -> int:
        """The newest message already checked for plans."""
        return self.data.get("calendar_seen", 0)

    @calendar_seen.setter
    def calendar_seen(self, message_id: int):
        self.data["calendar_seen"] = message_id
        self.save()

    def save(self):
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


async def resolve(client, cfg: Config, recipient: str, needs_her: bool):
    """Who the texts go to, and her chat (to read). They differ only for test runs with --to."""
    to = await find(client, recipient)
    if recipient == cfg.recipient:
        return to, to
    return to, (await find(client, cfg.recipient) if needs_her else None)


def make_ai(cfg: Config):
    """Claude, as (text writer, plan finder). Each is None if the feature using it is off."""
    if not cfg.ai.write_texts and cfg.calendar is None:
        return None, None
    try:
        import ai
    except ImportError:
        raise ConfigError("AI packages aren't installed. Run: python -m pip install -r requirements.txt") from None
    if old_version := ai.sdk_too_old():
        raise ConfigError(
            f"Your anthropic package is too old ({old_version}). Update it with: "
            "python -m pip install --upgrade -r requirements.txt"
        )
    client = ai.new_client(cfg.ai.api_key)
    if not ai.has_credentials(client):
        raise ConfigError(
            "There's no Anthropic API key (AI texts and the calendar both need one). Put it under "
            '[ai] in config.toml as: api_key = "sk-ant-..."' + cfg.ai.key_hint
        )
    if "___" in cfg.ai.about_us:
        log.warning("Tip: fill in about_us under [ai] in config.toml so Claude knows who's who")
    writer = ai.Writer(client, cfg.ai.model, cfg.ai.about_us) if cfg.ai.write_texts else None
    finder = ai.PlanFinder(client, cfg.ai.model, cfg.ai.about_us) if cfg.calendar else None
    return writer, finder


def _is_google_file(path: Path) -> bool:
    """A Desktop app client file or a service-account key, under the name Google gave it."""
    if path.name.startswith("client_secret_"):
        return True
    return path.is_file() and path.stat().st_size < 20_000 and gcal.service_account_email(path) is not None


def find_google_credentials(expected: Path, downloads: Path | None = None) -> Path:
    """The Google key file, even if it was saved under the wrong name or left in Downloads."""
    if expected.is_file():
        return expected
    folder = expected.parent
    # Windows hides file extensions, so renaming it to "google-credentials.json" by hand can give
    # "google-credentials.json.json". Or it may still have the name Google gave it.
    others = [f for f in sorted(folder.glob("*.json")) if _is_google_file(f)]
    for candidate in (folder / f"{expected.name}.json", folder / expected.stem, *others):
        if candidate.is_file():
            log.info("Using %s for the Google sign-in", candidate.name)
            return candidate
    downloads = downloads or Path.home() / "Downloads"
    waiting = sorted(
        (f for f in downloads.glob("*.json") if _is_google_file(f)), key=lambda f: f.stat().st_mtime, reverse=True
    )
    if waiting:
        how = f'It\'s still in your Downloads folder. Move it with:\n  move "{waiting[0]}" "{expected.resolve()}"'
    else:
        how = (
            "Go to https://console.cloud.google.com/auth/clients, create a Desktop app client, and click "
            f"Download JSON straight away. Then put the file in {folder.resolve()}"
        )
    raise ConfigError(f"Calendar sync is on but {expected.name} isn't in {folder.resolve()}.\n{how}")


def open_calendar(cfg: Config) -> gcal.GoogleCalendar | None:
    """Signs in to Google Calendar (in a browser, the first time). None if [calendar] is off."""
    if cfg.calendar is None:
        return None
    credentials_path = find_google_credentials(cfg.calendar.credentials_path)
    robot = gcal.service_account_email(credentials_path)
    share = f'In Google Calendar, share your calendar with {robot} and pick "Make changes to events".'
    if robot and cfg.calendar.calendar_id == "primary":
        raise ConfigError(
            "You're using a service account, so set calendar_id under [calendar] in config.toml to your "
            f'Gmail address, like: calendar_id = "you@gmail.com". {share}'
        )
    try:
        session = gcal.login(credentials_path, cfg.calendar.token_path)
    except ImportError:
        raise ConfigError("Google packages aren't installed. Run: python -m pip install -r requirements.txt") from None
    except ValueError:
        raise ConfigError(
            f"{cfg.calendar.credentials_path.name} doesn't look right. In Google Cloud, create a client of "
            'type "Desktop app" and download its JSON again.'
        ) from None
    except gcal.CalendarError as e:
        raise ConfigError(str(e)) from None
    calendar = gcal.GoogleCalendar(session, cfg.calendar.calendar_id, cfg.tz, robot)
    try:
        calendar.upcoming(days=1)  # find problems now rather than at the first plan
    except gcal.CalendarError as e:
        raise ConfigError(f"Google Calendar isn't working yet. Google says: {e}" + (f"\n{share}" if robot else "")) from None
    return calendar


def _describe(message) -> str:
    for attr, label in (("sticker", "sticker"), ("gif", "GIF"), ("photo", "photo"),
                        ("voice", "voice message"), ("video", "video")):
        if getattr(message, attr, None):
            return f"[{label}]"
    return "[non-text message]"


class ChatLine(NamedTuple):
    id: int
    when: datetime
    who: str  # "me" or "her"
    text: str


async def recent_chat(client, her, tz: ZoneInfo, limit: int) -> list[ChatLine]:
    """Your latest `limit` messages with her (never more than 3 days back), oldest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    chat = []
    async for message in client.iter_messages(her, limit=limit):
        if message.date < cutoff:
            break
        text = getattr(message, "message", None) or _describe(message)
        chat.append(ChatLine(message.id, message.date.astimezone(tz), "me" if message.out else "her", text))
    return chat[::-1]


async def compose(cfg: Config, writer, client, her, slot: Slot, state: State, preview: bool = False) -> str | None:
    """The text to send for this slot, or None if the AI thinks now's a bad time.

    In a preview (a test run to yourself) you get the draft either way, with Claude's reason if it would hold it back.
    """
    recent = state.recent(slot.name)
    if writer is not None:
        chat = await recent_chat(client, her, cfg.tz, cfg.ai.read_last_messages) if cfg.ai.read_recent_chat else None
        draft = await writer.write(slot.name, slot.messages, datetime.now(cfg.tz), chat, recent)
        if draft is not None:
            if not draft.send:
                log.info('Not sending "%s": %s', slot.name, draft.skip_reason)
                if preview and draft.message:
                    return f"{draft.message}\n\n(Preview only. Claude wouldn't send this right now: {draft.skip_reason})"
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
            return await client.send_message(entity, text)
        except ConnectionError:
            if attempt == 2:
                raise
            log.warning("Connection problem, retrying in 30s")
            await asyncio.sleep(30)


# Messages worth asking Claude about for the calendar: anything mentioning a day, a time, or a
# plan. It's deliberately generous; its only job is to skip the "lol"s and "love you"s for free.
PLAN_HINTS = re.compile(
    r"\d|\b(today|tonight|tm?rw|tomorrow|weekend|week|month|morning|afternoon|evening|noon|midnight"
    r"|(mon|tues?|wed(nes)?|thu(rs?)?|fri|sat(ur)?|sun)(day)?"
    r"|jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec|january|february|march|april|june|july"
    r"|august|september|october|november|december|am|pm|o'?clock"
    r"|dinner|lunch|brunch|breakfast|drinks|date|movies?|party|flight|trip|appointment|meeting"
    r"|exam|interview|birthday|concert|reservation|tickets|booked|plans?"
    r"|cancel\w*|reschedul\w*|postpon\w*|instead|can'?t make|rain ?check)\b",
    re.IGNORECASE,
)


class CalendarWatcher:
    """Watches your chat with her and keeps your Google Calendar in sync with the plans you make."""

    # Wait for the conversation to pause before reading it, so "dinner friday?" / "yes! 7?" /
    # "perfect" becomes one event instead of three guesses.
    quiet_seconds = 180

    def __init__(self, cfg: Config, client, her, finder, calendar: gcal.GoogleCalendar, state: State):
        self.cfg, self.client, self.her = cfg, client, her
        self.finder, self.calendar, self.state = finder, calendar, state
        self.ignore: set[int] = set()  # ids of the bot's own texts, which never contain plans
        self._timer = None
        self._told_signed_out = False
        self._waiting = False  # a check is scheduled for when the chat goes quiet
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def start(self):
        from telethon import events

        self.client.add_event_handler(self._on_message, events.NewMessage(chats=self.her))
        self._schedule(0)  # catch up on anything said while the bot was off

    async def _on_message(self, event):
        if event.message.id in self.ignore:
            return
        if not self._waiting:
            self._waiting = True
            log.info("Calendar: new message, will check it for plans once the chat is quiet for %d minutes",
                     self.quiet_seconds // 60)
        self._schedule(self.quiet_seconds)

    def _schedule(self, delay: float):
        if self._timer:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().call_later(delay, self._fire)

    def _fire(self):
        self._waiting = False
        task = asyncio.create_task(self._check_safely())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _check_safely(self):
        try:
            await self.check()
        except gcal.SignedOut as e:
            log.warning("%s", e)
            if not self._told_signed_out:  # once is enough; the texts keep going either way
                self._told_signed_out = True
                await self.client.send_message("me", f"📅 Calendar sync has stopped. {e}")
        except gcal.CalendarError as e:
            log.warning("Google Calendar problem, will try again after the next message: %s", e)
        except Exception:
            log.exception("Calendar check failed, will try again after the next message")

    async def check(self, dry_run: bool = False) -> list[str]:
        """Look for plans in new messages and update the calendar. Returns what changed, one line each.

        With dry_run it reads the whole recent chat and changes nothing.
        """
        async with self._lock:
            chat = await recent_chat(self.client, self.her, self.cfg.tz, self.cfg.ai.read_last_messages)
            new = chat if dry_run else [line for line in chat if line.id > self.state.calendar_seen]
            if not new:
                return []
            if not dry_run and not any(PLAN_HINTS.search(line.text) for line in new):
                log.info("Calendar: no days, times or plans in %d new message(s), nothing to check", len(new))
                self.state.calendar_seen = chat[-1].id
                return []
            log.info("Calendar: reading %d new message(s) for plans", len(new))
            upcoming = await asyncio.to_thread(self.calendar.upcoming)
            now = datetime.now(self.cfg.tz)
            changes = await self.finder.find(now, upcoming, chat, new[0].id)
            if changes is None:
                return []  # Claude couldn't be reached. These messages get checked again next time.
            ours = {event.id: event for event in upcoming if event.ours}
            done = []
            for change in changes:
                try:
                    line = await self._apply(change, ours, now, dry_run)
                except Exception as e:
                    log.warning("Couldn't %s %r on the calendar: %s", change.action, change.title or change.event_id, e)
                    continue
                if line:
                    log.info("%s", line.replace("\n", " "))
                    done.append(line)
            if not done:
                log.info("Calendar: nothing to add or change")
            if not dry_run:
                self.state.calendar_seen = chat[-1].id
                if done and self.cfg.calendar.notify_me:
                    await self.client.send_message("me", "\n\n".join(done))
            return done

    async def _apply(self, change, ours: dict, now: datetime, dry_run: bool) -> str | None:
        if change.action != "add" and change.event_id not in ours:
            log.info("Ignoring a calendar %s for an event the bot didn't create", change.action)
            return None
        quote = f'\n"{change.quote}"' if change.quote else ""
        if change.action == "cancel":
            if not dry_run:
                await asyncio.to_thread(self.calendar.cancel, change.event_id)
            return f"🗑️ Removed from your calendar: {ours[change.event_id].title}{quote}"

        title, start, end = change.title, change.start, change.end
        if not getattr(change, "time_was_said", True):
            start, end = start[:10], end[:10]  # no time was mentioned, so it's an all-day event
        if change.action == "update":
            # Keep whatever the update didn't mention.
            old = ours[change.event_id]
            title = title or old.title
            if not start:
                start, end = old.start, end or old.end
            elif "T" not in start and "T" in old.start:
                # Moved to another day without a new time ("can we do sunday instead"): keep the old time.
                day = start
                start = day + old.start[10:]
                end = day + old.end[10:] if "T" in old.end else ""
        body = gcal.event_body(title, start, end, change.location, change.quote, self.cfg.tz, now)
        if change.action == "add":
            if not dry_run:
                await asyncio.to_thread(self.calendar.add, body)
            return f"📅 Added to your calendar: {gcal.describe(body)}{quote}"
        if not dry_run:
            await asyncio.to_thread(self.calendar.update, change.event_id, body)
        return f"📅 Updated on your calendar: {gcal.describe(body)}{quote}"


async def run_forever(cfg: Config, state: State, writer, finder, calendar, recipient: str, skip_within: timedelta | None):
    client = await connect(cfg)
    try:
        watching = calendar is not None and finder is not None
        needs_her = (writer is not None and cfg.ai.read_recent_chat) or watching
        to, her = await resolve(client, cfg, recipient, needs_her)
        watcher = None
        if watching:
            watcher = CalendarWatcher(cfg, client, her, finder, calendar, state)
            watcher.start()
        extras = [name for name, on in (("AI texts", writer), ("calendar sync", watcher)) if on]
        log.info(
            "Logged in. Texting %s on schedule%s (Ctrl+C to stop).",
            recipient, f" with {' and '.join(extras)}" if extras else "",
        )
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
                    text = await compose(cfg, writer, client, her, slot, state, preview=recipient != cfg.recipient)
                    if text is None:
                        state.record(slot.name, day)
                        continue
                    sent = await send(client, to, text)
                    if watcher and sent is not None:
                        watcher.ignore.add(sent.id)
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
        to, her = await resolve(client, cfg, recipient, writer is not None and cfg.ai.read_recent_chat)
        text = await compose(cfg, writer, client, her, slot, state, preview=recipient != cfg.recipient)
        if text is None:
            return
        await send(client, to, text)
        state.record(slot.name, datetime.now(cfg.tz).date(), text)
        log.info("Sent to %s: %s", recipient, text)
    finally:
        await client.disconnect()


async def check_calendar(cfg: Config, finder, calendar: gcal.GoogleCalendar, chat_with: str):
    client = await connect(cfg)
    try:
        her = await find(client, chat_with)
        watcher = CalendarWatcher(cfg, client, her, finder, calendar, State(None))
        lines = await watcher.check(dry_run=True)
        print("\nFrom the last few days of chat, it would make these calendar changes:\n")
        print("\n\n".join(lines) if lines else "(nothing)")
        print("\nThis was a preview. Nothing was changed.")
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
    mode.add_argument("--check-calendar", action="store_true",
                      help="show what it would put on your calendar from the last few days of chat (changes nothing)")
    parser.add_argument("--to", metavar="WHO", help='send to someone else instead, e.g. "me" for your Saved Messages')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("telethon", "httpx", "httpx2", "google_auth_oauthlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
        # Test runs with --to don't touch the real state, so they can't use up today's texts.
        state = State(None if args.to else cfg.state_path)
        recipient = args.to or cfg.recipient
        if args.plan:
            print_plan(cfg, state)
        elif args.now:
            slot = cfg.slot(args.now)
            writer, _ = make_ai(cfg)
            asyncio.run(send_now(cfg, state, writer, recipient, slot))
        elif args.check_calendar:
            if cfg.calendar is None:
                raise ConfigError("Calendar sync is off. Set enabled = true under [calendar] in config.toml.")
            _, finder = make_ai(cfg)
            asyncio.run(check_calendar(cfg, finder, open_calendar(cfg), args.to or cfg.recipient))
        else:
            writer, finder = make_ai(cfg)
            calendar = None
            if args.to and cfg.calendar:
                log.info("Calendar sync is off during test runs with --to")
            elif cfg.calendar:
                calendar = open_calendar(cfg)
            skip_within = None if args.to else cfg.skip_if_texted_within
            asyncio.run(run_forever(cfg, state, writer, finder, calendar, recipient, skip_within))
    except ConfigError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
