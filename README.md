# Korvus Engine — Phase 1

The 24/7 "brain" behind the Korvus terminal. It pulls market news + Reddit
chatter, has Claude (Haiku) summarize and score each item for index-futures
impact, and saves it all to a local database (`korvus.db`).

*by BlackCrownVxJ.LLC*

---

## What you'll have running

```
   News (Alpha Vantage) ─┐
   Reddit ───────────────┼──►  Claude Haiku  ──►  korvus.db  ──►  (Phase 2: dashboard)
   X / Twitter (later) ──┘      summary+score
```

Each saved item gets: a plain-English **summary**, an **impact** rating
(high/med/low), a **direction** (bull/bear/neutral), the **instruments** it
affects (MNQ, MES, …), and a **confidence** score — exactly the fields you see
in the dashboard feed.

---

## Setup (about 15 minutes, one time)

### 1. Install Python 3.10+
Check with `python --version` (or `python3 --version`). If you don't have it,
get it from python.org.

### 2. Install the dependencies
In a terminal, from inside this `korvus-engine` folder:
```bash
pip install -r requirements.txt
```
*(If `pip` isn't found, try `pip3`.)*

### 3. Get your API keys
Copy the template, then open `.env` in any text editor and paste your keys in:
```bash
cp .env.example .env
```

You need these (all have free options to start):

| Key | Where to get it | Cost | Required? |
|-----|-----------------|------|-----------|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com | Haiku ≈ a few $/mo at your volume | **Yes** — this is the brain |
| `BENZINGA_KEY` | https://www.benzinga.com (or AWS Marketplace "Benzinga Basic Financial News API") | Free Basic tier | Recommended — your news feed |
| `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` | https://www.reddit.com/prefs/apps → "create app" → type **script** | Free | Optional |

> **News provider:** `.env` is set to `NEWS_PROVIDER=benzinga` (the free Basic
> tier). When you're ready to upgrade, contact Benzinga for a premium API quote,
> then change that one line to `NEWS_PROVIDER=benzinga_premium` — no code changes.
> You can also set it to `alphavantage` (with `ALPHAVANTAGE_KEY`) for a free
> alternative.

> The Anthropic key is the only hard requirement — the engine will run with
> just that, you'll simply have fewer sources until you add the others.
>
> **Note:** the Anthropic *API* key is separate from a Claude.ai subscription.
> It's billed by usage; set a low spend cap in the console while testing.

#### Getting the Reddit keys (the least obvious one)
1. Go to https://www.reddit.com/prefs/apps while logged in.
2. Click **"create another app…"** at the bottom.
3. Choose **script**. Name it anything (e.g. "korvus"). Redirect URI: `http://localhost`.
4. After creating: the **CLIENT_ID** is the short string just under the app name;
   the **secret** is the longer "secret" field. Paste both into `.env`.

### 4. Run it
One pass (great for the first test):
```bash
python korvus_engine.py
```
You'll see it pull articles, save the new ones, and print Claude's score for
each — like:
```
  ✓ [ med| bull| 62%] Large semi account flags unusual options flow ...
```

See the feed any time:
```bash
python view_db.py
```

Run it continuously (every 5 minutes — this is the "24/7" part, while your
computer is on):
```bash
python korvus_engine.py --loop
```

---

## Tuning it to you

Open `korvus_engine.py` and edit the lists near the top:
- `WATCHED_INSTRUMENTS` — what Claude maps news onto (MNQ, MES, …)
- `AV_TICKERS` — which tickers' news to pull
- `SUBREDDITS` — which Reddit communities to scan
- `POLL_MINUTES` — how often loop mode runs (also settable in `.env`)

---

## Cost reality check
At a 5-minute loop scoring a few dozen new items an hour, Claude Haiku usage
runs to a few dollars a month. Alpha Vantage and Reddit are free at this tier.
The free Alpha Vantage tier is rate-limited (~25 requests/day), which is fine
for testing; upgrade only when you go live.

---

## Phase 2 — see it live in the dashboard  ✅ ready

The dashboard reads your engine's data through a tiny local server.

```bash
# 1. make sure the engine has run at least once so korvus.db exists:
python korvus_engine.py

# 2. start the server (installs flask via requirements.txt):
python korvus_server.py

# 3. open the link it prints:
#    http://localhost:8000
```

The page will show your real Claude-scored news in the feed, refreshing every
30 seconds. The engine heartbeat up top flips to **REAL-TIME** once live items
are flowing. Keep the engine running in `--loop` mode in one terminal and the
server in another, and the desk stays current on its own.

> Opening `korvus_dashboard.html` directly (without the server) still works —
> it just shows sample data and tells you to start the server for the live feed.

---

## Phase 3 — live prices in the panels  ✅ ready

The Futures Board, SMT panel, and Funds Watch now pull live prices through
`korvus_quotes.py`, served at `/api/quotes`.

**Quote provider** (set `QUOTES_PROVIDER` in `.env`):
- `alphavantage` — true live prices. **Requires a PREMIUM Alpha Vantage key**
  (the free key returns end-of-day only). Buy premium only when you go live:
  https://www.alphavantage.co/premium
- `finnhub` — free, ~15-20 min delayed. Get a key at https://finnhub.io
- `off` — panels stay on sample numbers

> Futures are priced via ETF proxies (MNQ→QQQ, MES→SPY, MYM→DIA, etc.) because
> live futures data needs a costly exchange license. Outside US market hours
> (9:30am–4pm ET) the panels show the last close and the footer says
> "MARKET CLOSED". Your precise overnight futures SMT stays on Tradovate.

Test your quote key directly:
```bash
python korvus_quotes.py
```

**Personal watchlists (import):** click **+ Import** on the Funds Watch panel.
Paste tickers — one per line or comma-separated — optionally as
`TICKER, Name, Group`. Saved to `watchlist.json` and used instead of the
default board. "Reset to default" restores the built-in groups. If no import
is done, the default Korvus board is used.

> Reminder: serving live quotes to OTHER members is "redistribution" and needs
> a separate exchange license. Keep the quotes panel as each member's personal
> view; members' execution prices come from their own platform (Tradovate).

---

## What's still ahead
- **Phase 4:** move the engine + server to your cloud server so it runs 24/7
  without your computer being on.
- **Phase 5:** member logins for the Eclipse-X community (the shared desk).
