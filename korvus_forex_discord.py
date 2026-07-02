#!/usr/bin/env python3
"""
==============================================================================
 KORVUS FOREX -> DISCORD  .  Post-Release actuals poster   (BlackCrownVxJ LLC)
==============================================================================
 Posts a branded embed to the Eclipse-X #daily-news channel the moment a US
 economic release fills in its ACTUAL value. It reads straight from the same
 calendar engine the /forex page uses (korvus_calendar.get_calendar), so the
 number Discord shows is always the number the site shows. No third-party API,
 no Make polling, no cache drift.

 WHAT IT POSTS
   One embed per newly released event, USD + High/Medium impact only:
     .  the actual, with forecast and previous
     .  a beat/miss color: green above forecast, red below, pink in line
     .  a link to your calendar page (login-gated, so non-members bounce home)
     .  the Market Trader role pinged ONCE a day (the first release of the day)

 HOW IT RUNS
   The engine loop calls post_new_actuals() every pass (see korvus_engine.py).
   You can also run it by hand:
     python korvus_forex_discord.py             one real pass
     python korvus_forex_discord.py --dry-run   show what it WOULD post, send nothing
     python korvus_forex_discord.py --test       send one test embed to the webhook
     python korvus_forex_discord.py --seed       mark today's releases seen, post nothing

 CONFIG (.env)
   FOREX_DISCORD_WEBHOOK   Discord webhook URL for #daily-news   (required)
   FOREX_ROLE_ID           role id to ping        (default: Market Trader)
   FOREX_CALENDAR_URL      calendar page link     (default: /forex)
   FOREX_LOGO_URL          optional crown icon URL for the embed  (optional)
   FOREX_STATE_FILE        dedup state path       (default: next to this file)

 It never raises into the engine: any failure is logged and swallowed so the
 news loop keeps running.
==============================================================================
"""
import os
import json
import argparse
import datetime as dt

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

# Reuse the engine's own calendar + number helpers so Discord and the site agree.
from korvus_calendar import get_calendar, _num, _fmt_actual, _canon, _ts

# ----------------------------------------------------------------------------
# Config
WEBHOOK      = os.getenv("FOREX_DISCORD_WEBHOOK", "").strip()
ROLE_ID      = os.getenv("FOREX_ROLE_ID", "1493201812239552512").strip()
CALENDAR_URL = os.getenv("FOREX_CALENDAR_URL", "https://korvus.industries/forex").strip()
LOGO_URL     = os.getenv("FOREX_LOGO_URL", "").strip()
STATE_FILE   = os.getenv(
    "FOREX_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "forex_posted.json"),
)

# Brand
PINK   = 16723592     # #FF2E88  crown pink, used for "in line" or no forecast
GREEN  = 3066993      # #2ECC71  actual came in above forecast
RED    = 15158332     # #E74C3C  actual came in below forecast
AUTHOR = "ECLIPSE-X \u00B7 DATA RELEASE"
FOOTER = "BlackCrownVxJ LLC \u00B7 Post-Release Analysis"

WANTED_CCY    = {"USD"}
WANTED_IMPACT = {"High", "Medium"}
IMPACT_DOT    = {"High": "\U0001F534", "Medium": "\U0001F7E0", "Low": "\u26AA", "Holiday": "\u26AA"}


# ----------------------------------------------------------------------------
# state (dedup + one ping per day)
def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("posted", [])
    s.setdefault("last_ping_date", "")
    s.setdefault("last_agenda_date", "")
    s.setdefault("initialized", False)
    return s


def _save_state(s):
    s["posted"] = s["posted"][-500:]        # keep the dedup list from growing forever
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception as e:
        print(f"  [forex-discord] could not save state: {e}")


# ----------------------------------------------------------------------------
# helpers
def _et_now():
    return dt.datetime.now(ET) if ET else dt.datetime.now(dt.timezone.utc)


def _et_today_str():
    return _et_now().strftime("%Y-%m-%d")


def _event_key(e):
    date10 = (e.get("date") or "")[:10]
    return f"{date10}|{(e.get('country') or '').upper()}|{_canon(e.get('title'))}"


def _event_dt_et(e):
    d = _ts(e.get("date"))
    if d is None:
        return None
    if d.tzinfo and ET:
        d = d.astimezone(ET)
    return d


def _is_today(e):
    d = _event_dt_et(e)
    if d is None:
        return (e.get("date") or "")[:10] == _et_today_str()
    return d.strftime("%Y-%m-%d") == _et_today_str()


def _et_time(e):
    d = _event_dt_et(e)
    if d is None:
        return ""
    return d.strftime("%I:%M %p ET").lstrip("0")


def _beat(actual, forecast):
    """Return (embed_color, one_line_note) comparing actual to forecast."""
    a, f = _num(actual), _num(forecast)
    if a is None or f is None:
        return PINK, ""
    if a > f:
        return GREEN, "\U0001F4C8 Above forecast"
    if a < f:
        return RED, "\U0001F4C9 Below forecast"
    return PINK, "\u2796 In line with forecast"


def _qualifies(e):
    if (e.get("country") or "").upper() not in WANTED_CCY:
        return False
    if e.get("impact") not in WANTED_IMPACT:
        return False
    if not _is_today(e):
        return False
    # a real, sane released number, reusing the site's own validation
    return _fmt_actual(e.get("actual"), e.get("forecast"), e.get("previous")) is not None


def _build_embed(e):
    disp_actual = _fmt_actual(e.get("actual"), e.get("forecast"), e.get("previous")) \
        or (e.get("actual") or "")
    forecast = (e.get("forecast") or "").strip() or "n/a"
    previous = (e.get("previous") or "").strip() or "n/a"
    color, beat = _beat(e.get("actual"), e.get("forecast"))
    dot  = IMPACT_DOT.get(e.get("impact"), "\u26AA")
    ccy  = (e.get("country") or "").upper()
    when = _et_time(e)

    header = f"{dot} {e.get('impact')} Impact  \u00B7  `{ccy}`"
    if when:
        header += f"  \u00B7  {when}"

    lines = [
        header,
        "",
        f"**Actual:  {disp_actual}**",
        f"Forecast:  {forecast}",
        f"Previous:  {previous}",
    ]
    if beat:
        lines += ["", beat]
    lines += ["", f"[View the full calendar \u203A]({CALENDAR_URL})"]

    embed = {
        "author": {"name": AUTHOR},
        "title": f"\U0001F4E1 {(e.get('title') or '').upper()}",
        "description": "\n".join(lines),
        "color": color,
        "footer": {"text": FOOTER},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if LOGO_URL:
        embed["author"]["icon_url"] = LOGO_URL
        embed["thumbnail"] = {"url": LOGO_URL}
    return embed


def _send(embed, ping):
    payload = {"embeds": [embed]}
    if ping and ROLE_ID:
        payload["content"] = f"<@&{ROLE_ID}>"
        payload["allowed_mentions"] = {"roles": [ROLE_ID]}
    else:
        payload["content"] = ""
        payload["allowed_mentions"] = {"parse": []}
    try:
        r = requests.post(WEBHOOK, json=payload, timeout=20)
    except Exception as e:
        print(f"  [forex-discord] webhook error: {e}")
        return False
    if r.status_code not in (200, 204):
        print(f"  [forex-discord] webhook {r.status_code}: {r.text[:180]}")
        return False
    return True


# ----------------------------------------------------------------------------
# 1 AM agenda: today's scheduled USD High/Medium releases, one heads-up ping.
# Same get_calendar() and same USD + High/Medium filter as the actuals poster,
# so the morning list and the live actuals are guaranteed to be the same events.
def _qualifies_agenda(e):
    return (
        (e.get("country") or "").upper() in WANTED_CCY
        and e.get("impact") in WANTED_IMPACT
        and _is_today(e)
    )


def _build_agenda_embed(events):
    events = sorted(events, key=lambda x: x.get("date") or "")
    lines = ["Scheduled economic releases for today's session.", ""]
    if events:
        for e in events:
            dot = IMPACT_DOT.get(e.get("impact"), "\u26AA")
            ccy = (e.get("country") or "").upper()
            when = _et_time(e) or "TBD"
            fc = (e.get("forecast") or "").strip() or "n/a"
            pv = (e.get("previous") or "").strip() or "n/a"
            lines.append(f"{dot} **{e.get('title')}**  `{ccy}`")
            lines.append(f"{when}  \u00B7  Forecast {fc}  \u00B7  Previous {pv}")
            lines.append("")
        lines.append("`\U0001F534 High   \U0001F7E0 Medium`")
    else:
        lines.append("No major USD releases scheduled today.")
    lines += ["", f"[View the live calendar \u203A]({CALENDAR_URL})"]

    now = _et_now()
    today_label = f"{now.strftime('%B')} {now.day}, {now.year}"
    embed = {
        "author": {"name": "ECLIPSE-X \u00B7 FOREX INTELLIGENCE"},
        "title": f"\U0001F4C8 Economic Calendar \u00B7 {today_label}",
        "description": "\n".join(lines),
        "color": PINK,
        "footer": {"text": "BlackCrownVxJ LLC \u00B7 Live Market Intelligence"},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if LOGO_URL:
        embed["author"]["icon_url"] = LOGO_URL
        embed["thumbnail"] = {"url": LOGO_URL}
    return embed


def post_daily_agenda(force=False, dry_run=False):
    """Post today's release schedule once, in the early-morning ET window. This
    is the one message a day that pings @Market Trader. force=True ignores the
    time and once-a-day guards (for --agenda testing)."""
    if not WEBHOOK and not dry_run:
        return
    state = _load_state()
    today = _et_today_str()
    if not force:
        if not (1 <= _et_now().hour < 5):        # only fire after 1 AM ET
            return
        if state.get("last_agenda_date") == today:   # and only once that day
            return

    try:
        events = get_calendar()
    except Exception as e:
        print(f"  [forex-agenda] calendar error: {e}")
        return
    todays = [e for e in events if _qualifies_agenda(e)]

    if dry_run:
        print(f"  [forex-agenda][dry] would post agenda with {len(todays)} event(s)")
        for e in sorted(todays, key=lambda x: x.get("date") or ""):
            print(f"      {_et_time(e) or 'TBD':>9}  {e.get('impact'):6} {e.get('title')}")
        return

    if _send(_build_agenda_embed(todays), ping=True):
        state["last_agenda_date"] = today
        _save_state(state)
        print(f"  [forex-agenda] posted today's agenda ({len(todays)} event(s))")


# ----------------------------------------------------------------------------
# main entry, called by the engine every pass
def post_new_actuals(dry_run=False):
    if not WEBHOOK and not dry_run:
        print("  [forex-discord] no FOREX_DISCORD_WEBHOOK set, skipping")
        return

    try:
        events = get_calendar()
    except Exception as e:
        print(f"  [forex-discord] calendar error: {e}")
        return

    state = _load_state()
    seen  = set(state["posted"])
    fresh = [e for e in events if _qualifies(e)]

    # First real deploy: mark everything already released today as seen so we do
    # not backfill a whole day at once. Only NEW prints post from here on. This
    # also protects against a flood if the state file is ever lost and rebuilt.
    if not state["initialized"] and not dry_run:
        for e in fresh:
            seen.add(_event_key(e))
        state["posted"] = list(seen)
        state["initialized"] = True
        _save_state(state)
        print(f"  [forex-discord] first run: seeded {len(fresh)} released event(s), "
              f"posting new releases from here")
        return

    to_post = [e for e in fresh if _event_key(e) not in seen]
    if not to_post:
        print("  [forex-discord] no new actuals")
        return

    to_post.sort(key=lambda e: e.get("date") or "")   # oldest release first
    posted = 0
    for e in to_post:
        # Actuals post quietly. The single daily @Market Trader ping rides on the
        # 1 AM agenda (post_daily_agenda), so the channel is not pinged all day.
        if dry_run:
            _c, note = _beat(e.get("actual"), e.get("forecast"))
            print(f"  [forex-discord][dry] {e.get('title')}  "
                  f"actual={e.get('actual')} forecast={e.get('forecast')}  "
                  f"{note or 'neutral'}")
            continue
        if _send(_build_embed(e), ping=False):
            seen.add(_event_key(e))
            posted += 1

    if not dry_run:
        state["posted"] = list(seen)
        _save_state(state)
        print(f"  [forex-discord] posted {posted} new actual(s)")


def _test_webhook():
    if not WEBHOOK:
        print("  [forex-discord] no FOREX_DISCORD_WEBHOOK set")
        return
    embed = {
        "author": {"name": AUTHOR},
        "title": "\U0001F4E1 KORVUS FEED TEST",
        "description": ("If you can read this in #daily-news, the Korvus forex poster "
                        "is wired up correctly.\n\n"
                        f"[View the full calendar \u203A]({CALENDAR_URL})"),
        "color": PINK,
        "footer": {"text": FOOTER},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if LOGO_URL:
        embed["author"]["icon_url"] = LOGO_URL
        embed["thumbnail"] = {"url": LOGO_URL}
    print("  [forex-discord] test embed sent" if _send(embed, ping=False)
          else "  [forex-discord] test failed")


def _seed_only():
    try:
        events = get_calendar()
    except Exception as e:
        print(f"  [forex-discord] calendar error: {e}")
        return
    fresh = [e for e in events if _qualifies(e)]
    state = _load_state()
    seen  = set(state["posted"])
    for e in fresh:
        seen.add(_event_key(e))
    state["posted"] = list(seen)
    state["initialized"] = True
    _save_state(state)
    print(f"  [forex-discord] seeded {len(fresh)} released event(s), posted nothing")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Korvus forex -> Discord poster")
    ap.add_argument("--dry-run", action="store_true", help="show what actuals would post, send nothing")
    ap.add_argument("--test", action="store_true", help="send one test embed to the webhook")
    ap.add_argument("--seed", action="store_true", help="mark today's releases seen, post nothing")
    ap.add_argument("--agenda", action="store_true", help="post today's 1 AM agenda now (ignores the time guard)")
    ap.add_argument("--agenda-dry", action="store_true", help="show today's agenda, send nothing")
    args = ap.parse_args()
    if args.test:
        _test_webhook()
    elif args.seed:
        _seed_only()
    elif args.agenda:
        post_daily_agenda(force=True)
    elif args.agenda_dry:
        post_daily_agenda(force=True, dry_run=True)
    else:
        post_new_actuals(dry_run=args.dry_run)
