# TELEBOT

Texts your girlfriend on Telegram so you don't have to remember. 🙃

It logs in as **you**, not as a bot, so to her the texts look exactly like ones you typed. Every day it sends a good-morning text, a goodnight text, and sometimes a "thinking of you" text in between. Each one goes out at a random time in its window.

With **AI mode** on, Claude writes each text instead of picking one from a list:

- It reads the last few days of your chat, so it can say "good luck on the exam!!" instead of a generic "have a good day"
- It copies how you actually text: length, capitalization, emoji, slang
- It **skips the text** if it would land badly, for example if you two are mid-argument, she's upset, or she said something you haven't answered yet

Without AI it picks from message lists you write. Either way it:

- Sends at **random times** inside each window, so it isn't suspiciously 8:00:00 every morning
- **Doesn't double-text:** if you already texted her in the last 90 minutes, it skips that one
- **Doesn't repeat itself**
- **Shows "typing…"** for a few seconds before each message
- Is **safe to restart:** it remembers what it already sent today

## Setup

You need Python 3.11+.

1. **Install:**
   ```sh
   pip install -r requirements.txt
   ```
2. **Get Telegram API keys:** go to https://my.telegram.org, log in with your phone number, open **API development tools**, and create an app (any name works). Copy the `api_id` and `api_hash`.
3. **Get an Anthropic API key** (for AI mode): create one at https://platform.claude.com and add some credit. Then set it as an environment variable:
   ```sh
   export ANTHROPIC_API_KEY=sk-ant-...
   ```
   You can also put it in `config.toml` as `api_key` under `[ai]`. To skip AI entirely, set `enabled = false` under `[ai]`.
4. **Configure:**
   ```sh
   cp config.example.toml config.toml
   ```
   Open `config.toml`. Paste in your Telegram keys, set `recipient` to her `@username` and `timezone` to yours. Then:
   - Fill in **`about_us`** (her name, what you call her, how you text). This is what makes the AI sound like you and not a greeting card.
   - Edit the **message lists** so they sound like you. With AI on they're used as style examples, and as a backup if Claude can't be reached.
5. **Preview the schedule** (this doesn't connect or send anything):
   ```sh
   python bot.py --plan
   ```
6. **Send yourself a test text** to your Saved Messages. The first time, it asks for your phone number and the login code Telegram sends you:
   ```sh
   python bot.py --now "good morning" --to me
   ```
   With AI on, this reads your real chat with her and sends the result to **you**, so it's a preview of what she'd get. Run it a few times and tweak `about_us` until it sounds right.
7. **Start it:**
   ```sh
   python bot.py
   ```

## Keeping it running

It only sends while `python bot.py` is running, so put it somewhere that stays on, like a home server, a Raspberry Pi, or a cheap VPS. The quickest way is to start it inside `tmux` or `screen` and detach. With systemd, put this in `/etc/systemd/system/telebot.service`:

```ini
[Unit]
Description=TELEBOT
After=network-online.target

[Service]
WorkingDirectory=/path/to/TELEBOT
ExecStart=/usr/bin/python3 bot.py
Environment=ANTHROPIC_API_KEY=sk-ant-...
Restart=on-failure
User=youruser

[Install]
WantedBy=multi-user.target
```

Log in once by hand (step 6) before starting the service, then run `sudo systemctl enable --now telebot`.

## Commands

| Command | What it does |
| --- | --- |
| `python bot.py` | Runs the schedule forever |
| `python bot.py --plan` | Shows the planned send times for the next 3 days |
| `python bot.py --now "goodnight"` | Sends her one text for that slot right now (and counts it as today's) |
| `--to me` | Sends to your Saved Messages instead, for testing. Doesn't count toward the real schedule |
| `--config other.toml` | Uses a different config file |

## Good to know

- `telebot.session` is your logged-in Telegram session. **Anyone who has it can use your account.** Don't share it or commit it (it's already in `.gitignore`).
- **Privacy:** with `read_recent_chat = true`, the last few days of your chat with her (up to 40 messages) go to Anthropic's API each time a text is written. Set it to `false` and Claude only sees `about_us` and the examples.
- **Cost:** with the default model, each text costs roughly 1–3 cents, so about $1–3 a month at three texts a day. `claude-sonnet-5-5` costs about half as much.
- If Claude can't be reached or your key is wrong, it logs the problem and sends a text from your list instead.
- Keep the volume reasonable. A few texts a day is fine, but Telegram flags accounts that look like they're spamming.
- To add a slot (like a lunch check-in), add another `[[schedule]]` block to `config.toml`.
- Run the tests with `python -m unittest`.
