# TELEBOT

Texts your girlfriend on Telegram so you don't have to remember. 🙃

It logs in as **you**, not as a bot, so to her the texts look exactly like ones you typed. Every day it sends a good-morning text, a goodnight text, and sometimes a "thinking of you" text in between. Each one goes out at a random time in its window.

With **AI mode** on, Claude writes each text instead of picking one from a list:

- It reads the last few days of your chat, so it can say "good luck on the exam!!" instead of a generic "have a good day"
- It copies how you actually text: length, capitalization, emoji, slang
- It **skips the text** if it would land badly, for example if you two are mid-argument, she's upset, or she said something you haven't answered yet

With **calendar sync** on, plans you two make in the chat go on your Google Calendar:

- "dinner at luigi's saturday at 7?" / "yes!!" becomes **Dinner at Luigi's, Sat 7:00pm**
- Things she has coming up go on too ("Sam's job interview"), so you remember to ask how it went
- If the plan moves ("can we do 8 instead?") it updates the event, and if it's called off it removes it
- It sends you a note in Saved Messages whenever it changes something, and it only ever touches events it created

With **auto-replies** on (they're off unless you turn them on), the bot covers for you when you're busy:

- If she texts and you haven't answered in 30 minutes, it sends **one** short holding reply in your style, like "sorry got caught up, will text you properly soon"
- It never answers her questions, agrees to plans, or says what you're doing, and it won't send another until you've replied yourself
- If her message is serious (she's upset, something's wrong, or it needs a real answer), it doesn't reply and leaves you a note instead
- It stays quiet overnight (11:30pm to 7:30am by default), and tells you in Saved Messages whenever it replies for you

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
   python -m pip install -r requirements.txt
   ```
   Using `python -m pip` (not just `pip`) makes sure the packages go to the same Python that runs the bot. After getting a newer version of TELEBOT, run it again with `--upgrade` added.
2. **Get Telegram API keys:** go to https://my.telegram.org, log in with your phone number, open **API development tools**, and create an app (any name works). Copy the `api_id` and `api_hash`.
3. **Get an Anthropic API key** (for AI mode and calendar sync): create one at https://platform.claude.com and add some credit. Then set it as an environment variable:
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
5. **Set up Google Calendar** (skip this if you set `enabled = false` under `[calendar]`). It's a one-time setup:
   1. Go to https://console.cloud.google.com, signed in with the Google account whose calendar you want. Click the project picker at the top, then **New project**, name it "TELEBOT", and make sure it's selected.
   2. Open https://console.cloud.google.com/flows/enableapi?apiid=calendar-json.googleapis.com and click **Enable** to turn on the Google Calendar API.
   3. Open https://console.cloud.google.com/auth/branding and click **Get started**. App name "TELEBOT", your email for support and contact, audience **External**, agree to the policy, then **Create**.
   4. Open https://console.cloud.google.com/auth/audience and click **Publish app**, then **Confirm**. Google will call the app "unverified"; that's normal for something only you use. If it says to finish the Branding page first, or you'd rather not publish, add your Gmail under **Test users** instead. Without one or the other, sign-in fails with "Access blocked". In test mode Google signs the bot out every 7 days; the bot messages you in Saved Messages when that happens, and restarting it signs you back in.
   5. Open https://console.cloud.google.com/auth/clients, click **Create client**, choose **Desktop app**, name it "TELEBOT", and click **Create**. In the box that pops up, click **Download JSON**.
   6. Move that file into the TELEBOT folder as `google-credentials.json`. On Windows, from the TELEBOT folder:
      ```
      move %USERPROFILE%\Downloads\client_secret_*.json google-credentials.json
      ```
   7. Run:
      ```sh
      python bot.py --check-calendar
      ```
      A browser window opens. Pick your Google account. On "Google hasn't verified this app", click **Advanced → Go to TELEBOT** (it's your own app, so that warning is about you). If there's a checkbox for calendar access, **tick it**, then click **Continue**. When the page says you can close it, go back to the terminal. It asks for your Telegram login if it hasn't before, then shows what it *would* put on your calendar from your latest messages. Nothing changes yet.
   **If the sign-in won't work** (for example "Access blocked", or Google won't let you add yourself as a test user), use a service account instead. It's a robot Google account that you share your calendar with, so there's no sign-in screen and it never gets signed out:
   1. In the same project, open https://console.cloud.google.com/iam-admin/serviceaccounts, click **Create service account**, name it "telebot", click **Create and continue**, skip the optional steps, and click **Done**.
   2. Click the new account, open the **Keys** tab, and choose **Add key → Create new key → JSON → Create**. A file downloads.
   3. Copy the account's email address. It looks like `telebot@telebot-123456.iam.gserviceaccount.com`.
   4. In Google Calendar on the web, open **Settings**, click your calendar under "Settings for my calendars", then **Share with specific people or groups → Add people and groups**. Paste that address, choose **Make changes to events**, and click **Send**.
   5. Move the key into the TELEBOT folder as `google-credentials.json` (on Windows: `move /Y %USERPROFILE%\Downloads\telebot-*.json google-credentials.json`).
   6. In `config.toml`, set `calendar_id` under `[calendar]` to your Gmail address, e.g. `calendar_id = "you@gmail.com"`.
   7. Run `python bot.py --check-calendar`. No browser this time.
6. **Preview the schedule** (this doesn't connect or send anything):
   ```sh
   python bot.py --plan
   ```
7. **Send yourself a test text** to your Saved Messages. The first time, it asks for your phone number and the login code Telegram sends you:
   ```sh
   python bot.py --now "good morning" --to me
   ```
   With AI on, this reads your real chat with her and sends the result to **you**, so it's a preview of what she'd get. Run it a few times and tweak `about_us` until it sounds right.
8. **Start it:**
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

Log in once by hand (steps 5 and 7) before starting the service, then run `sudo systemctl enable --now telebot`.

The Google sign-in needs a browser. If your server doesn't have one, do step 5 on your own computer, then copy `google-token.json` to the server along with `config.toml` and `telebot.session`.

## Commands

| Command | What it does |
| --- | --- |
| `python bot.py` | Runs the schedule forever |
| `python bot.py --plan` | Shows the planned send times for the next 3 days |
| `python bot.py --now "goodnight"` | Sends her one text for that slot right now (and counts it as today's) |
| `python bot.py --check-calendar` | Shows what it would put on your calendar from the last few days of chat. Changes nothing |
| `python bot.py --check-reply` | Shows what it would auto-reply if she'd been waiting right now. Sends nothing |
| `--to me` | Sends to your Saved Messages instead, for testing. Doesn't count toward the real schedule, and calendar sync and auto-replies stay off |
| `--config other.toml` | Uses a different config file |

## Good to know

- `telebot.session` is your logged-in Telegram session, and `google-token.json` is your Google sign-in. **Anyone who has them can use your accounts.** Don't share or commit them (they're already in `.gitignore`).
- **Privacy:** with `read_recent_chat = true`, your latest messages with her go to Anthropic's API each time a text is written: 10 by default (set by `read_last_messages`), and never more than 3 days back. Set `read_recent_chat = false` and Claude only sees `about_us` and the examples. Calendar sync always reads the same number of latest messages, plus the titles and times of your calendar events for the next 60 days, so it doesn't add duplicates.
- **Cost:** with the default model, each text costs roughly 1–3 cents, so about $1–3 a month at three texts a day. Calendar checks cost about the same each. One runs when you two stop texting for a few minutes and the new messages mention a day, a time, or a plan; small talk is skipped for free. `claude-sonnet-5-5` costs about half as much.
- To turn on auto-replies, set `enabled = true` under `[auto_reply]` in `config.toml` (copy that section from `config.example.toml` if yours doesn't have it), then try `python bot.py --check-reply` first. Auto-replies read the same latest messages as the AI texts, and each one costs about the same as a text.
- Calendar sync waits until the chat has been quiet for 3 minutes, so a back-and-forth like "dinner friday?" / "yes! 7?" / "perfect" becomes one event.
- If Claude can't be reached or your key is wrong, it logs the problem and sends a text from your list instead. A calendar check that fails gets retried after the next message.
- Keep the volume reasonable. A few texts a day is fine, but Telegram flags accounts that look like they're spamming.
- To add a slot (like a lunch check-in), add another `[[schedule]]` block to `config.toml`.
- Run the tests with `python -m unittest`.
