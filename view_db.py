#!/usr/bin/env python3
"""
KORVUS — quick database viewer
Run:  python view_db.py
Prints the most recent scored items so you can SEE the engine working,
even before the dashboard is wired up.
"""
import os, json, sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "korvus.db")

if not os.path.exists(DB):
    print("No korvus.db yet — run `python korvus_engine.py` first.")
    raise SystemExit

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT * FROM items WHERE processed=1 ORDER BY created_at DESC LIMIT 20"
).fetchall()

if not rows:
    print("Engine has run but nothing is scored yet. Check your API keys in .env.")
    raise SystemExit

print(f"\n  KORVUS feed — {len(rows)} most recent scored items\n" + "="*70)
for r in rows:
    insts = ", ".join(json.loads(r["instruments"] or "[]"))
    arrow = {"bull":"▲ Bullish","bear":"▼ Bearish","neut":"◆ Neutral"}.get(r["direction"], r["direction"])
    print(f"\n[{r['impact'].upper():>4}] {arrow}   conf {r['confidence']}%   ({r['source_name']})")
    print(f"  {r['headline']}")
    print(f"  → {r['summary']}")
    if insts:
        print(f"  instruments: {insts}")
print("\n" + "="*70)
conn.close()
