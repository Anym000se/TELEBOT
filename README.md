# TELEBOT

Texts your girlfriend on Telegram so you don't have to remember. 🙃

It logs in as **you**, not as a bot, so to her the texts look exactly like ones you typed. Every day it sends a good-morning text, a goodnight text, and sometimes a "thinking of you" text in between. Each one goes out at a random time in its window and comes from a list of messages you write.

- **Random times** inside each window, so it isn't suspiciously 8:00:00 every morning
- **Doesn't double-text:** if you already texted her in the last 90 minutes, it skips that one
- **Doesn't repeat itself:** recently used messages are skipped
- **Shows "typing…"** for a few seconds before each message
- **Safe to restart:** it remembers what it already sent today

## Setup

You need Python 3.11+.

1. **Install:**
   ```sh
   pip install -r requirements.txt
   ```
2. **Get Telegram API keys:** go to https://my.telegram.org, log in with your phone number, open **API development tools**, and create an app (any name works). Copy the `api_id` and `api_hash`.
3. **Configure:**
   ```sh
   cp config.example.toml config.toml
   ```
   Open `config.toml`. Paste in your keys, set `recipient` to her `@username` and `timezone` to yours, and **rewrite the messages so they sound like you**.
4. **Preview the schedule** (this doesn't connect or send anything):
   ```sh
   python bot.py --plan
   ```
5. **Send yourself a test text** to your Saved Messages. The first time, it asks for your phone number and the login code Telegram sends you:
   ```sh
   python bot.py --now "good morning" --to me
   ```
6. **Start it:**
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
Restart=on-failure
User=youruser

[Install]
WantedBy=multi-user.target
```

Log in once by hand (step 5) before starting the service, then run `sudo systemctl enable --now telebot`.

## Commands

| Command | What it does |
| --- | --- |
| `python bot.py` | Runs the schedule forever |
| `python bot.py --plan` | Shows the planned send times for the next 3 days |
| `python bot.py --now "goodnight"` | Sends her one message from that slot right now (and counts it as today's) |
| `--to me` | Sends to your Saved Messages instead, for testing. Doesn't count toward the real schedule |
| `--config other.toml` | Uses a different config file |

## Good to know

- `telebot.session` is your logged-in Telegram session. **Anyone who has it can use your account.** Don't share it or commit it (it's already in `.gitignore`).
- Keep the volume reasonable. A few texts a day is fine, but Telegram flags accounts that look like they're spamming.
- To add a slot (like a lunch check-in), add another `[[schedule]]` block to `config.toml`.
- Run the tests with `python -m unittest`.
