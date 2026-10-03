"""The Claude parts: writing texts in your style, and spotting plans in the chat for your calendar."""

import logging
from datetime import datetime
from typing import Literal

import anthropic
from pydantic import BaseModel

log = logging.getLogger("telebot")

WRITER_PROMPT = """\
You write texts that get sent automatically from my Telegram account to my girlfriend. \
She'll read them as coming from me, so they have to sound like me typing on my phone, \
not like an AI or a greeting card.

- Match how I actually text: length, capitalization, punctuation, emoji, slang. My real \
messages in the chat are the best guide; the examples show the vibe for this time of day.
- One short text. Casual and specific beats flowery and generic.
- If the recent chat gives you something real to mention (her exam, a trip, something she \
was stressed about), work it in naturally. Don't force it.
- Never make up facts, plans, or promises. Don't say "can't wait for dinner tonight" unless \
the chat says there's a dinner tonight.
- Don't repeat something I've said recently.

Set `send` to false and explain in `skip_reason` when a text like this would land badly \
right now. For example: we're in the middle of an argument, she's upset and a cheery text \
would seem tone-deaf, she asked for space, or she said something I haven't replied to and \
this text would come across as ignoring it. Otherwise set `send` to true and leave \
`skip_reason` empty. Either way, write the best text you can in `message`.

About us:
{about_us}"""

BRIEF_PROMPT = """\
You help me be a thoughtful boyfriend. Every morning I get a short private brief, and you \
write two parts of it.

`ask_about`: things from our recent chat worth following up on today, like asking how her \
shift or exam went, wishing her luck for something, or remembering something she was \
excited or worried about. Short and specific, at most 3. Use an empty list if nothing \
stands out. Don't invent anything that isn't in the chat.

`date_ideas`: only when I ask for them. Then suggest 3 specific, doable date ideas for the \
coming week that fit what she's into, what we've talked about, the season, and where we \
live. One line each. When I don't ask, use an empty list.

About us:
{about_us}"""

REPLY_PROMPT = """\
You write a quick holding reply that gets sent from my Telegram account to my girlfriend \
when she has texted and I haven't been able to answer for a while. She'll read it as coming \
from me, so it has to sound like me typing on my phone.

The only goal is to let her know I'm not ignoring her and will reply properly soon:
- One short text, in my style. My real messages in the chat show how I text: length, \
capitalization, punctuation, emoji, slang.
- Keep it true and vague. Don't answer her questions, agree to plans, make promises, give \
opinions, or say what I'm doing or where I am. I'll handle all of that myself when I'm back.
- A light reaction is fine if it fits (like "haha" to something funny), as long as it's \
still a holding reply.

Set `send` to false and say why in `skip_reason` when a holding reply would be the wrong \
move: she's upset, worried or hurt; something might be wrong or urgent; she's asked \
something important or personal that needs a real answer from me; or a vague reply would \
come across as cold or dismissive. Otherwise set `send` to true and leave `skip_reason` \
empty. Either way, write the best text you can in `message`.

About us:
{about_us}"""

PLANS_PROMPT = """\
You keep my Google Calendar in sync with plans from my Telegram chat with my girlfriend. \
Read the new messages (below the "new messages" line; anything above it is only context, \
and was already handled) and decide whether anything should be added to my calendar, \
changed, or removed.

Add:
- Plans we've agreed on: dates, dinners, trips, calls, visits, anything with a day or time.
- Things she has coming up that I'd want to remember, like her exam, flight, interview, or \
a party she's going to. Title these so it's clear they're hers, e.g. "Sam's job interview".

Don't add:
- Ideas or suggestions nobody has agreed to yet ("we should go to the beach sometime"). \
If one gets confirmed later, you'll see that message then.
- Anything already on my calendar (listed below), even if it's worded differently.
- Things that already happened.

Use `update` when a plan that's already on the calendar moved or changed, and `cancel` \
when it was called off. You can only update or cancel events that show an id. Put that \
id in `event_id`; leave `event_id` empty for `add`.

How to fill in each change:
- `title`: short, like a calendar entry ("Dinner at Luigi's", "Movie night", "Sam's flight to Denver").
- `start` / `end`: local time as YYYY-MM-DDTHH:MM, or just YYYY-MM-DD for all-day things \
and when no time was mentioned. Work out relative dates ("tomorrow", "next friday", "the \
12th") from the current date. For an all-day thing spanning several days, `end` is the \
last day. Leave `end` empty if it wasn't said.
- `time_was_said`: true only if someone actually said a clock time for it ("2pm", "7:30", \
"noon"). Words like "later", "tonight" or "after work" don't count. Never guess a time: \
when this is false, give `start` and `end` as dates only.
- `location`: if one was mentioned, otherwise empty.
- `quote`: the message the plan came from, copied word for word.
- For `cancel`, only `event_id` and `quote` matter; leave the rest empty.

Most of the time there's nothing to do. Then return an empty list.

About us:
{about_us}"""


class Draft(BaseModel):
    send: bool
    message: str
    skip_reason: str


class CalendarChange(BaseModel):
    action: Literal["add", "update", "cancel"]
    event_id: str
    title: str
    start: str
    end: str
    time_was_said: bool
    location: str
    quote: str


class Brief(BaseModel):
    ask_about: list[str]
    date_ideas: list[str]


class Plans(BaseModel):
    changes: list[CalendarChange]


def new_client(api_key: str | None) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()


# Older SDKs don't support the request options used below.
MIN_SDK = (1, 11)


def sdk_too_old() -> str | None:
    """The installed anthropic version if it's too old for this bot, else None."""
    try:
        if tuple(int(part) for part in anthropic.__version__.split(".")[:2]) < MIN_SDK:
            return anthropic.__version__
    except ValueError:
        pass
    return None


def has_credentials(client: anthropic.AsyncAnthropic) -> bool:
    return bool(client.api_key or client.auth_token or getattr(client, "credentials", None))


async def ask(client, model: str, system: str, prompt: str, schema, if_it_fails: str):
    """One structured request to Claude. Returns the parsed reply, or None (and logs why) if it fails."""
    try:
        response = await client.beta.messages.parse(
            model=model,
            max_tokens=16000,
            output_config={"effort": "medium"},
            # If the request is ever declined, retry it on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        log.error("Anthropic rejected your API key (%s), %s", e.message, if_it_fails)
        return None
    except anthropic.APIStatusError as e:
        log.warning("Claude request failed (%s: %s), %s", e.status_code, e.message, if_it_fails)
        return None
    except anthropic.APIConnectionError:
        log.warning("Couldn't reach Claude, %s", if_it_fails)
        return None

    if response.stop_reason != "end_turn" or response.parsed_output is None:
        log.warning("Claude didn't finish (stop reason: %s), %s", response.stop_reason, if_it_fails)
        return None
    return response.parsed_output


def _clock(when: datetime) -> str:
    return f"{when.hour % 12 or 12}:{when:%M}{'am' if when.hour < 12 else 'pm'}"


def _format_chat(chat, first_new_id: int | None = None) -> list[str]:
    lines = []
    for line in chat:
        if line.id == first_new_id:
            lines.append("--- new messages ---")
        lines.append(f"[{line.when:%a %b} {line.when.day} {line.when:%H:%M}] {line.who}: {line.text}")
    return lines


def _tidy(draft: Draft | None) -> Draft | None:
    """Strip stray quotes; a draft that should be sent but is empty counts as no draft."""
    if draft is None:
        return None
    draft.message = draft.message.strip().strip('"').strip()
    if draft.send and not draft.message:
        return None
    return draft


class Writer:
    def __init__(self, client, model: str, about_us: str):
        self.client = client
        self.model = model
        self.system = WRITER_PROMPT.format(about_us=about_us or "(nothing provided)")

    async def write(self, slot_name: str, examples: list[str], now: datetime, chat, recent: list[str]) -> Draft | None:
        """Ask Claude for a text. Returns None if it couldn't, so the caller can fall back to the list."""
        prompt = self._prompt(slot_name, examples, now, chat, recent)
        return _tidy(await ask(self.client, self.model, self.system, prompt, Draft, "using the message list instead"))

    @staticmethod
    def _prompt(slot_name, examples, now, chat, recent) -> str:
        lines = [
            f'It\'s {now:%A, %B} {now.day}, {_clock(now)}. Write my "{slot_name}" text to her.',
            "",
            "Examples of the kind of thing I send for this:",
            *(f"- {m}" for m in examples),
        ]
        if recent:
            lines += ["", "Texts I've sent for this recently (don't reuse them):", *(f"- {m}" for m in recent[-5:])]
        lines.append("")
        if chat is None:
            lines.append("(I've chosen not to share our chat history.)")
        elif not chat:
            lines.append("(No messages between us in the last few days.)")
        else:
            lines.append("Our recent chat, oldest first:")
            lines += _format_chat(chat)
        return "\n".join(lines)


class PlanFinder:
    def __init__(self, client, model: str, about_us: str):
        self.client = client
        self.model = model
        self.system = PLANS_PROMPT.format(about_us=about_us or "(nothing provided)")

    async def find(self, now: datetime, upcoming, chat, first_new_id: int) -> list[CalendarChange] | None:
        """What to change on the calendar, based on the new messages. None if Claude couldn't be asked."""
        prompt = self._prompt(now, upcoming, chat, first_new_id)
        plans = await ask(self.client, self.model, self.system, prompt, Plans, "will check again after the next message")
        return None if plans is None else plans.changes

    @staticmethod
    def _prompt(now, upcoming, chat, first_new_id) -> str:
        lines = [f"Now: {now:%A, %B} {now.day}, {now.year}, {_clock(now)} ({now.tzinfo})", ""]
        lines.append("Already on my calendar (next 60 days):")
        for event in upcoming:
            span = event.start if not event.end else f"{event.start} to {event.end}"
            ref = f" [id: {event.id}]" if event.ours else ""
            lines.append(f"- {event.title}: {span}{ref}")
        if not upcoming:
            lines.append("(nothing)")
        lines += ["", "Our chat, oldest first:", *_format_chat(chat, first_new_id)]
        return "\n".join(lines)


class Replier:
    def __init__(self, client, model: str, about_us: str):
        self.client = client
        self.model = model
        self.system = REPLY_PROMPT.format(about_us=about_us or "(nothing provided)")

    async def write(self, now: datetime, waited_minutes: int, chat) -> Draft | None:
        """A holding reply to her unanswered messages. None if Claude couldn't be asked."""
        prompt = "\n".join([
            f"It's {now:%A, %B} {now.day}, {_clock(now)}. She's been waiting about {waited_minutes} "
            "minutes for me to reply.",
            "",
            "Our recent chat, oldest first:",
            *_format_chat(chat),
        ])
        return _tidy(await ask(self.client, self.model, self.system, prompt, Draft, "so not replying for you"))


class BriefWriter:
    def __init__(self, client, model: str, about_us: str):
        self.client = client
        self.model = model
        self.system = BRIEF_PROMPT.format(about_us=about_us or "(nothing provided)")

    async def write(self, now: datetime, place: str, chat, plans: list[str], want_ideas: bool) -> Brief | None:
        """Things to ask her about, and date ideas if wanted. None if Claude couldn't be asked."""
        lines = [f"It's {now:%A, %B} {now.day}, {now.year}. We live in {place}.", ""]
        lines += ["On my calendar today:", *(f"- {plan}" for plan in plans)] if plans else ["(Nothing on my calendar today.)"]
        lines += ["", "Date ideas: yes please." if want_ideas else "Date ideas: not today.", ""]
        lines += ["Our recent chat, oldest first:", *_format_chat(chat)] if chat else ["(No recent messages.)"]
        return await ask(self.client, self.model, self.system, "\n".join(lines), Brief, "so the brief skips that part")
