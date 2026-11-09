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

## Primary Source: SEC filings straight from EDGAR  ✅ ready

Every outlet writes its earnings story from the company's SEC filing. Korvus now
reads that filing itself (`korvus_edgar.py`), so the terminal is no longer waiting
on a reporter, an RSS refresh, and then the 5 min news pass.

```
   EDGAR live feed (8-K / 6-K, every 20s) ─► watchlist filter (NQ/ES heavyweights)
        ─► Exhibit 99.1 press release ─► Claude Haiku ─► korvus.db (source='filing')
        ─► terminal card: "filed with SEC 4:21:19 PM ET · on Korvus +21s"
```

* **Runs itself.** `korvus_engine.py --loop` starts it as a background thread, so
  the existing systemd service supervises it. No new service to install.
* **Operator feature.** Pro sees filings live. Free sees a locked teaser with the
  ticker and impact, which unlocks after the same ~15 min delay as quotes.
* **Every card is a receipt.** It shows the SEC acceptance time and the time
  Korvus had it. That is the marketing claim, and it is always true.
* **SEC fair access.** Declares `EDGAR_USER_AGENT`, spaces requests 0.25s apart
  (SEC cap is 10/s), and backs off on 403/429.

Optional `.env` keys (all have defaults):

| Key | Default | What it does |
|-----|---------|--------------|
| `EDGAR_ENABLED` | `1` | `0` turns the watcher off |
| `EDGAR_USER_AGENT` | `Korvus BlackCrownVxJ LLC BlackCrownVxJ.LLC@outlook.com` | SEC requires a name + contact email |
| `EDGAR_TICKERS` | (empty) | extra tickers on top of the built-in ~30 heavyweights |
| `EDGAR_POLL_SECONDS` | `20` | poll speed while EDGAR is open (6 AM to 10 PM ET, weekdays) |
| `EDGAR_DISCORD_WEBHOOK` | (off) | posts med/high filings to Discord as a delayed public receipt |
| `EDGAR_DISCORD_DELAY_MIN` | `15` | how long Operator gets it before Discord does |

Handy commands:
```bash
python korvus_edgar.py --dry-run           # what's on the wire right now from the watchlist
python korvus_edgar.py --dry-run --all     # same, every 8-K/6-K filer
python korvus_edgar.py --replay https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/0001045810-26-000073-index.htm --no-save
                                           # score NVIDIA's Q2 FY27 earnings 8-K and print it
python tests/test_edgar.py                 # offline parser tests
```

## Korvus Intel: small-stock crawler, paper bot, Obsidian vault  (separate ecosystem)

Its own service (`korvus-intel`), its own database (`korvus_intel.db`), its own
vault folder (`intel/` in the `blackcrown-brain` repo). It never writes to
`korvus.db` or the terminal, and never touches the agents' folders.

```
FMP screener ($80-$120, under $10B) ─► universe ─► EDGAR 8-K/6-K for those CIKs (20s)
   ─► Haiku stock-level read ─► korvus_intel.db ─► paper bot (60s, PAPER ONLY)
   ─► korvus_vault.py renders intel/ every 10 min ─► git push ─► Obsidian Git pulls to the PC
```

**Vault layout** (`BlackCrown-Brains/intel/`): `README.md` (contract for agents),
`Universe.md`, `Filings/YYYY-MM-DD.md`, `Tickers/SYM.md`, `Bot/Strategy.md`,
`Bot/Scoreboard.md`, `Bot/Trades/YYYY-MM.md`, and `data/*.jsonl` for agents and backtests.

**Paper bot v1** (`korvus_paperbot.py`): long on bullish med/high filings that hold
above their pre-filing price, +1.5% target / -1.0% stop / flat 3:55 PM ET. Simulated
fills only. The scoreboard refuses to call a win rate meaningful before 30 trades.

**One-time setup**
1. GitHub > Settings > Developer settings > Fine-grained tokens > new token, repository
   access **only** `Vxmpira/blackcrown-brain`, permission **Contents: Read and write**.
2. Add to the server `.env`: `VAULT_GIT_TOKEN=<token>` (optional tuning keys: `INTEL_*`, `BOT_*`, `VAULT_*`).
3. `venv/bin/python korvus_intel.py --universe` (prints today's list; proves FMP works)
4. `venv/bin/python korvus_vault.py` (first push; check Obsidian for `intel/` within 5 min)
5. `sudo cp korvus-intel.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now korvus-intel`

Checks: `korvus_intel.py --dry-run`, `korvus_intel.py --stats`, `python tests/test_intel.py`.

**Who reads the filings (`INTEL_SCORER`):** `haiku` (default) · `crown` (BlackCrown Intelligence decides;
Haiku covers any filing Crown can't answer) · `shadow` (Haiku decides, Crown reads every filing on the side so
you can compare before trusting it). Crown is called through AnythingLLM's developer API:
`CROWN_API_BASE` (default `https://blackcrown-intelligence.com/api/v1`), `CROWN_API_KEY`, `CROWN_WORKSPACE`
(the workspace slug). Check the link with `venv/bin/python korvus_intel.py --crown-test`.

**Live status for the BlackCrown admin:** korvus-intel writes `intel_status.json` (heartbeat, per-stage
stats, last 80 events) every few seconds. `GET /api/feed/intel-status` serves it to the admin with header
`X-Intel-Key: $INTEL_STATUS_KEY`. Set the same random key on both boxes
(`INTEL_STATUS_KEY` here, `KORVUS_INTEL_KEY` in blackcrown-studio). Test: `python tests/test_intel_status.py`.

## What's still ahead
- **Phase 4:** move the engine + server to your cloud server so it runs 24/7
  without your computer being on.
- **Phase 5:** member logins for the Eclipse-X community (the shared desk).
