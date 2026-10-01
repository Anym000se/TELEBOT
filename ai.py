"""Has Claude write each text in your style, based on your recent chat with her."""

import logging
from datetime import datetime

import anthropic
from pydantic import BaseModel

log = logging.getLogger("telebot")

SYSTEM_PROMPT = """\
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
`skip_reason` empty.

About us:
{about_us}"""


class Draft(BaseModel):
    send: bool
    message: str
    skip_reason: str


class Writer:
    def __init__(self, model: str, api_key: str | None, about_us: str):
        self.model = model
        self.system = SYSTEM_PROMPT.format(about_us=about_us or "(nothing provided)")
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()

    def has_credentials(self) -> bool:
        return bool(self.client.api_key or self.client.auth_token or self.client.credentials)

    async def write(
        self,
        slot_name: str,
        examples: list[str],
        now: datetime,
        chat: list[tuple[datetime, str, str]] | None,
        recent: list[str],
    ) -> Draft | None:
        """Ask Claude for a text. Returns None if it couldn't, so the caller can fall back to the list."""
        prompt = self._prompt(slot_name, examples, now, chat, recent)
        try:
            response = await self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                output_config={"effort": "medium"},
                # If the request is ever declined, retry it on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=self.system,
                messages=[{"role": "user", "content": prompt}],
                output_format=Draft,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            log.error("Anthropic rejected your API key (%s), using the message list instead", e.message)
            return None
        except anthropic.APIStatusError as e:
            log.warning("Claude request failed (%s: %s), using the message list instead", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError:
            log.warning("Couldn't reach Claude, using the message list instead")
            return None

        if response.stop_reason != "end_turn" or response.parsed_output is None:
            log.warning("Claude didn't finish a text (stop reason: %s), using the message list instead", response.stop_reason)
            return None
        draft = response.parsed_output
        draft.message = draft.message.strip().strip('"').strip()
        if draft.send and not draft.message:
            return None
        return draft

    @staticmethod
    def _prompt(slot_name, examples, now, chat, recent) -> str:
        clock = f"{now.hour % 12 or 12}:{now:%M}{'am' if now.hour < 12 else 'pm'}"
        lines = [
            f'It\'s {now:%A, %B} {now.day}, {clock}. Write my "{slot_name}" text to her.',
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
            lines += [f"[{when.strftime('%a %H:%M')}] {who}: {text}" for when, who, text in chat]
        return "\n".join(lines)
