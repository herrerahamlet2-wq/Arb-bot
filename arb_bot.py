#!/usr/bin/env python3
"""
arb_bot.py — arbitrage + hedge finder for the sportsbooks YOU can use.

Odds come from The Odds API (free key: https://the-odds-api.com — 500 credits/month free).

COMMANDS
  python arb_bot.py books                      # list bookmaker keys available (to fill MY_BOOKS)
  python arb_bot.py scan                       # one scan: arbs + value bets across MY_BOOKS
  python arb_bot.py watch --every 15           # live mode: scans + Telegram commands + hedge alerts on your bets
  python arb_bot.py best --team Cowboys --market player_pass_yds --player Prescott
                                               # best price on every side of a market (incl. props)
  python arb_bot.py check --team Cowboys --market player_pass_yds --player Prescott --line 219.5 --side Over --odds -110
                                               # price you see on Courtside vs best other side at your books
  python arb_bot.py hedge --stake 385 --odds -135 --hedge-odds +150
  python arb_bot.py hedge --stake 385 --payout 670.19 --hedge-odds -110

CREDIT COST (The Odds API): each /odds call costs  (#markets) x (#regions, or 1 per 10 books).
  Default scan = 3 markets x 1 (<=10 books) = 3 credits per sport. Event list calls are free.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("Install requests first:  pip install requests")

# ─────────────────────────── CONFIG — EDIT THIS ───────────────────────────
API_KEY = os.getenv("ODDS_API_KEY", "PASTE_YOUR_KEY_HERE")

# Books you can actually bet at. Run `python arb_bot.py books` to see exact keys.
MY_BOOKS = ["hardrockbet", "kalshi", "novig", "prophetx"]

# Flip to True when you're up in New York and can use FanDuel.
UP_NORTH = False
if UP_NORTH:
    MY_BOOKS.append("fanduel")

# ── Value (+EV) betting ── exchanges = "true" odds; flag soft-book prices that beat them.
# Sharp books = "true odds". Pinnacle is the pros' benchmark, so it counts double.
# They're only used to judge prices — the bot never tells you to bet there.
SHARP_WEIGHTS = {"pinnacle": 2.0, "novig": 1.0, "prophetx": 1.0, "kalshi": 1.0}
SHARP_BOOKS = list(SHARP_WEIGHTS)
REFERENCE_BOOKS = ["pinnacle"]   # fetched for pricing only (not bettable for you)
STEAM_MOVE = 0.02                # sharp win-chance move (2 points) between scans that counts as "steam"
MIN_EV_PCT = float(os.getenv("MIN_EV_PCT", "3"))          # only flag bets at least this % better than fair
BETTING_BANKROLL = float(os.getenv("BETTING_BANKROLL", "500"))  # your total betting money
KELLY_FRACTION = 0.25     # bet a quarter of the "math-perfect" size (safer)
MAX_BET_PCT = 2.0         # never suggest more than this % of your bankroll on one value bet
EV_INCLUDE_LIVE = False   # live exchange prices are often stale — skip by default
MAX_FAVORITE = float(os.getenv("MAX_FAVORITE", "-175"))   # never suggest bets shorter than this
UNIT_TO_WIN = float(os.getenv("UNIT_TO_WIN", "100"))       # you bet to win this much

# Exchanges take a cut of winnings. Set these to what your apps actually charge
# (fraction of profit). Prices are adjusted before checking for arbs.
FEES = {"kalshi": 0.035, "novig": 0.0, "prophetx": 0.0}
# Courtside isn't in the odds feed (sweepstakes app) — compare it by hand in the calculator page.

# Sports to scan. Entries ending in * match every active key with that prefix
# (tennis keys change per tournament, e.g. tennis_atp_paris_masters).
SPORTS = ["americanfootball_nfl", "americanfootball_ncaaf", "basketball_nba", "basketball_ncaab",
          "icehockey_nhl", "tennis_atp*", "tennis_wta*",
          # big soccer leagues (fits the $30 odds plan). For every league use "soccer_*".
          "soccer_epl", "soccer_italy_serie_a", "soccer_spain_la_liga", "soccer_germany_bundesliga",
          "soccer_france_ligue_one", "soccer_uefa_champs_league", "soccer_usa_mls"]

# Sports the automatic scan looks at for arbs/value/steam (the rest still work in
# /check, /tennis, /bet and hedge alerts). Comma list; "" = everything in SPORTS.
SCAN_SPORTS = [x for x in os.getenv("SCAN_SPORTS", "americanfootball_nfl").split(",") if x]

MARKETS = os.getenv("MARKETS", "h2h,spreads,totals")
BANKROLL = 100.0          # total $ to split across the sides of one arb
MIN_PROFIT_PCT = 0.5      # only report arbs at or above this guaranteed return
SUSPICIOUS_PCT = 8.0      # arbs above this are usually stale lines / errors — flagged
WATCH_EVERY_MIN = 20      # minutes between scans in `watch` mode (mind your credits)

# Optional phone alerts via Telegram (create a bot with @BotFather, get chat id from @userinfobot)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")  # paste your BotFather token here
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# ───────────────────────────────────────────────────────────────────────────

BASE = "https://api.the-odds-api.com/v4"
_remaining = None


# ── odds math ──────────────────────────────────────────────────────────────
def am_to_dec(a: float) -> float:
    a = float(a)
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def dec_to_am(d: float) -> str:
    if d >= 2:
        return f"+{round((d - 1) * 100)}"
    return f"{round(-100 / (d - 1))}"


def parse_odds(s: str) -> float:
    """Accept American (+150, -135) or decimal (2.5). Returns decimal."""
    s = str(s).strip()
    v = float(s)
    if s.startswith(("+", "-")) or abs(v) >= 100:
        return am_to_dec(v)
    return v


# ── API ────────────────────────────────────────────────────────────────────
def api_get(path, **params):
    global _remaining
    if not API_KEY or API_KEY == "PASTE_YOUR_KEY_HERE":
        sys.exit("Set your API key: edit API_KEY in the file or set ODDS_API_KEY.")
    params["apiKey"] = API_KEY
    r = requests.get(f"{BASE}{path}", params=params, timeout=20)
    _remaining = r.headers.get("x-requests-remaining", _remaining)
    if r.status_code == 401:
        sys.exit("API key rejected (401). Check the key.")
    if r.status_code == 429:
        sys.exit("Out of API credits or rate-limited (429).")
    if r.status_code == 422:
        print(f"  ! {path}: {r.json().get('message', 'bad request')}")
        return []
    r.raise_for_status()
    return r.json()


def book_params():
    if MY_BOOKS:
        return {"bookmakers": ",".join(dict.fromkeys(MY_BOOKS + REFERENCE_BOOKS))}
    return {"regions": "us"}


def resolve_sports():
    active = [s["key"] for s in api_get("/sports") if not s.get("has_outrights")]  # free call; skip futures
    keys = []
    for want in SPORTS:
        if want.endswith("*"):
            keys += [k for k in active if k.startswith(want[:-1])]
        elif want in active:
            keys.append(want)
    return list(dict.fromkeys(keys))


# ── arbitrage detection ────────────────────────────────────────────────────
def group_key(market_key, outcome, home):
    """Return (group, side-label) so opposite sides of the same line pair up."""
    name, point = outcome["name"], outcome.get("point")
    desc = outcome.get("description")  # player name on props
    if desc is not None:
        return (market_key, desc, point), name
    if market_key.startswith("h2h"):
        return (market_key,), name
    if market_key.startswith("spreads"):
        rel = point if name == home else -point  # home -3.5 pairs with away +3.5
        return (market_key, rel), name
    return (market_key, point), name  # totals & everything else: same number


def collect_prices(event, books=None):
    """best[group][side] = (decimal, book_title); also every side seen at any book."""
    books = set(books or [])
    best, all_sides = {}, {}
    for bk in event.get("bookmakers", []):
        mine = not books or bk["key"] in books
        for m in bk.get("markets", []):
            for o in m.get("outcomes", []):
                if o.get("price") is None:
                    continue
                g, side = group_key(m["key"], o, event.get("home_team"))
                all_sides.setdefault(g, set()).add(side)
                if not mine:
                    continue
                d = am_to_dec(o["price"]) if abs(o["price"]) >= 100 else float(o["price"])
                d = 1 + (d - 1) * (1 - FEES.get(bk["key"], 0))  # price after exchange fee
                cur = best.setdefault(g, {}).get(side)
                if cur is None or d > cur[0]:
                    best[g][side] = (d, bk["title"], bk.get("last_update", ""))
    return best, all_sides


def split_stakes(decs, bankroll):
    inv = sum(1 / d for d in decs)
    raw = [bankroll * (1 / d) / inv for d in decs]
    rounded = [round(x) for x in raw]
    worst = min(s * d for s, d in zip(rounded, decs)) - sum(rounded)
    return raw, rounded, worst, (1 / inv - 1) * 100


def find_arbs(events, min_pct=MIN_PROFIT_PCT):
    arbs = []
    for ev in events:
        best, all_sides = collect_prices(ev, MY_BOOKS)
        for g, sides in best.items():
            needed = all_sides[g]
            if len(needed) < 2 or set(sides) != needed:
                continue
            labels = sorted(sides)
            decs = [sides[s][0] for s in labels]
            if len({sides[s][1] for s in labels}) < 2:
                continue  # same book on every side = not a real arb
            raw, rounded, worst, pct = split_stakes(decs, BANKROLL)
            if pct >= min_pct:
                arbs.append({
                    "event": f"{ev['away_team']} @ {ev['home_team']}",
                    "sport": ev.get("sport_title", ev.get("sport_key")),
                    "start": ev.get("commence_time", ""),
                    "group": g, "pct": pct, "worst": worst,
                    "legs": [(s, sides[s][0], sides[s][1], st) for s, st in zip(labels, rounded)],
                })
    return sorted(arbs, key=lambda a: -a["pct"])


def raw_dec(price):
    return am_to_dec(price) if abs(price) >= 100 else float(price)


def is_live(ev):
    try:
        return datetime.fromisoformat(ev.get("commence_time", "").replace("Z", "+00:00")) < datetime.now(timezone.utc)
    except ValueError:
        return False


def fair_probs(event):
    """No-vig win chance for every side: weighted average of sharp books (Pinnacle counts double)."""
    per = {}
    for bk in event.get("bookmakers", []):
        if bk["key"] not in SHARP_BOOKS:
            continue
        for m in bk.get("markets", []):
            for o in m.get("outcomes", []):
                if o.get("price") is None:
                    continue
                g, side = group_key(m["key"], o, event.get("home_team"))
                per.setdefault(g, {}).setdefault(bk["key"], {})[side] = raw_dec(o["price"])
    fair = {}
    for g, books in per.items():
        sets = []
        for bk_key, sides in books.items():
            if len(sides) < 2:
                continue
            tot = sum(1 / d for d in sides.values())
            sets.append((SHARP_WEIGHTS.get(bk_key, 1.0), {s_: (1 / d) / tot for s_, d in sides.items()}))
        if not sets:
            continue
        common = set.intersection(*(set(x) for _, x in sets))
        wsum = sum(w for w, _ in sets)
        fair[g] = {s_: sum(w * x[s_] for w, x in sets) / wsum for s_ in common}
    return fair


def suggest_stake(p, d):
    edge = p * d - 1
    kelly = edge / (d - 1) if d > 1 else 0
    pct = min(max(kelly * KELLY_FRACTION, 0), MAX_BET_PCT / 100)
    return round(BETTING_BANKROLL * pct)


LAST_FAIR = {}   # (event id, market group, side) -> sharp win chance at previous scan


def find_ev(events):
    picks = {}
    for ev in events:
        if is_live(ev) and not EV_INCLUDE_LIVE:
            continue
        fair = fair_probs(ev)
        moves = {}
        for g, sides in fair.items():
            for side, p_now in sides.items():
                k = (ev.get("id"), g, side)
                if k in LAST_FAIR:
                    moves[(g, side)] = (LAST_FAIR[k], p_now)
                LAST_FAIR[k] = p_now
        for bk in ev.get("bookmakers", []):
            if bk["key"] in SHARP_BOOKS or (MY_BOOKS and bk["key"] not in MY_BOOKS):
                continue
            for m in bk.get("markets", []):
                for o in m.get("outcomes", []):
                    if o.get("price") is None:
                        continue
                    g, side = group_key(m["key"], o, ev.get("home_team"))
                    p = fair.get(g, {}).get(side)
                    if not p or p < 0.05:
                        continue
                    d = raw_dec(o["price"])
                    if d < am_to_dec(MAX_FAVORITE):
                        continue  # shorter than your max favorite (e.g. -175)
                    ev_pct = (p * d - 1) * 100
                    key = (ev["id"] if "id" in ev else ev["home_team"], g, side)
                    if ev_pct >= MIN_EV_PCT and (key not in picks or ev_pct > picks[key]["ev"]):
                        mv = moves.get((g, side))
                        steam = mv if mv and mv[1] - mv[0] >= STEAM_MOVE else None
                        picks[key] = {"event": f"{ev['away_team']} @ {ev['home_team']}",
                                      "sport": ev.get("sport_title", ev.get("sport_key")),
                                      "group": g, "side": side, "dec": d, "book": bk["title"],
                                      "p": p, "ev": ev_pct, "stake": suggest_stake(p, d),
                                      "home": ev.get("home_team"), "steam": steam,
                                      "sharp": "Pinnacle" if any(b["key"] == "pinnacle" for b in ev.get("bookmakers", []))
                                      else "exchanges"}
    # steam first, then biggest edge
    return sorted(picks.values(), key=lambda x: (x["steam"] is None, -x["ev"]))


def fmt_ev(x):
    flag = "  ⚠ CHECK — edge this big is often a stale line" if x["ev"] >= 15 else ""
    pt = x["group"][-1] if len(x["group"]) > 1 and x["group"][-1] is not None else None
    line = f"{x['side']}" + (f" {pt}" if pt is not None and not x["group"][0].startswith("spreads") else "")
    if x["group"][0].startswith("spreads"):
        rel = x["group"][1]
        own = rel if x["side"] == x.get("home") else -rel
        line = f"{x['side']} {own:+g}"
    head = (f"⚡ STEAM — sharp money moved, {x['book']} hasn't caught up" if x.get("steam") else "📈 VALUE BET")
    out = [f"{head} +{x['ev']:.1f}% — {x['sport']}: {x['event']}{flag}",
           f"   Bet: {line} {dec_to_am(x['dec'])} @ {x['book']}   (fair price {dec_to_am(1 / x['p'])})"]
    if x.get("steam"):
        a, b = x["steam"]
        out.append(f"   Sharp line moved: {dec_to_am(1 / a)} → {dec_to_am(1 / b)} since last scan. Move fast — books catch up quickly.")
    return "\n".join(out + [
        f"   Win chance ({x.get('sharp', 'exchanges')}): {x['p'] * 100:.0f}%",
        f"   Your unit: risk ${UNIT_TO_WIN / (x['dec'] - 1):.2f} to win ${UNIT_TO_WIN:.0f}"
        f"  (math-optimal size: ${x['stake']})",
        "   Not guaranteed — profitable long-run, can lose any single bet.",
    ])


def fmt_line(g):
    mk = g[0]
    if len(g) == 3:
        return f"{mk} {g[1]} {g[2]}"
    if len(g) == 2:
        return f"{mk} {'+' if mk.startswith('spreads') and g[1] > 0 else ''}{g[1]}"
    return mk


def fmt_arb(a):
    live = ""
    try:
        if datetime.fromisoformat(a["start"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
            live = " [LIVE]"
    except ValueError:
        pass
    flag = "  ⚠ CHECK — likely stale/error line" if a["pct"] >= SUSPICIOUS_PCT else ""
    out = [f"💰 {a['pct']:.2f}% ARB{live} — {a['sport']}: {a['event']}{flag}",
           f"   Market: {fmt_line(a['group'])}"]
    for side, d, book, stake in a["legs"]:
        out.append(f"   • ${stake:>4} on {side} {dec_to_am(d)} @ {book}  → returns ${stake * d:.2f}")
    out.append(f"   Guaranteed profit (rounded stakes): ${a['worst']:.2f} on ${sum(l[3] for l in a['legs'])}")
    return "\n".join(out)


def scan(verbose=True):
    sports = resolve_sports()
    if verbose:
        print(f"Scanning {len(sports)} sport(s) at {len(MY_BOOKS) or 'all US'} book(s)…")
    found, values = [], []
    for sk in sports:
        events = api_get(f"/sports/{sk}/odds", markets=MARKETS, oddsFormat="american", **book_params())
        found += find_arbs(events)
        values += find_ev(events)
    if verbose:
        print(f"Credits remaining: {_remaining}\n")
    return found, sorted(values, key=lambda x: -x["ev"])


# ── alerts ─────────────────────────────────────────────────────────────────
def telegram_chat_id():
    """Use TELEGRAM_CHAT_ID if set; otherwise find it from whoever pressed Start on the bot."""
    global TELEGRAM_CHAT_ID
    if TELEGRAM_CHAT_ID or not TELEGRAM_TOKEN:
        return TELEGRAM_CHAT_ID
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=10).json()
        for u in reversed(r.get("result", [])):
            msg = u.get("message") or u.get("edited_message") or {}
            if msg.get("chat", {}).get("id"):
                TELEGRAM_CHAT_ID = str(msg["chat"]["id"])
                break
    except requests.RequestException:
        pass
    return TELEGRAM_CHAT_ID


def telegram(text):
    if not TELEGRAM_TOKEN or not telegram_chat_id():
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        return r.ok
    except requests.RequestException as e:
        print(f"  ! Telegram failed: {e}")
        return False


# ── commands ───────────────────────────────────────────────────────────────
def cmd_books(_):
    events = api_get("/sports/upcoming/odds", regions="us,us2,us_ex,eu", markets="h2h", oddsFormat="american")
    seen = {}
    for ev in events:
        for bk in ev.get("bookmakers", []):
            seen[bk["key"]] = bk["title"]
    print("Bookmaker keys seen (copy the ones you can use into MY_BOOKS):")
    for k, t in sorted(seen.items(), key=lambda x: x[1].lower()):
        print(f"  {k:<22} {t}")
    print(f"\nCredits remaining: {_remaining}")


def cmd_scan(_):
    arbs, values = scan()
    if not arbs:
        print("No arbs right now across your books. (Normal — they're rare and short-lived.)")
    for a in arbs:
        print(fmt_arb(a) + "\n")
        telegram(fmt_arb(a))
    if not values:
        print(f"No value bets at +{MIN_EV_PCT:g}% or better right now.")
    for x in values[:10]:
        print(fmt_ev(x) + "\n")
        telegram(fmt_ev(x))


def cmd_test(_):
    if not TELEGRAM_TOKEN:
        print("No Telegram token set.")
    elif telegram("✅ Arb bot connected. Arb alerts will show up here."):
        print("Sent! Check Telegram.")
    else:
        print("Couldn't send. Open your bot in Telegram, press Start (or send it 'hi'), then run test again.")


# ── LIVE MODE: scanning loop + Telegram commands + hedge alerts on your bets ──
STATE_FILE = os.getenv("STATE_FILE", "state.json")
PROP_WORDS = {
    "pass": "player_pass_yds", "passing": "player_pass_yds",
    "rush": "player_rush_yds", "rushing": "player_rush_yds",
    "rec": "player_reception_yds", "receiving": "player_reception_yds",
    "receptions": "player_receptions", "catches": "player_receptions",
    "points": "player_points", "pts": "player_points",
    "rebounds": "player_rebounds", "reb": "player_rebounds",
    "assists": "player_assists", "ast": "player_assists",
    "threes": "player_threes", "3s": "player_threes",
    "shots": "player_shots_on_goal", "sog": "player_shots_on_goal",
}
FILLER = {"goals", "goal", "pts", "yds", "yards", "yd", "ml", "moneyline", "to", "win", "@", "on", "at", "for", "bet"}
HELP = """🤖 Commands:
/bet Texas -115 100 — moneyline (team, odds, $ stake)
/bet Texas -3.5 -110 100 — spread (team, spread, odds, $ stake)
/bet Texas over 47.5 -110 50 — game total (use under for unders)
/bet Lazio over 2.5 -120 50 — soccer goals total
/bet Dak Prescott over 219.5 pass Cowboys -110 50 — player prop (add the team)
/tennis Sinner Alcaraz — match card: model vs market, H2H, surface form, hold/break, total games
/check Lazio over 3 +112 — BEFORE you bet: true odds, your edge, GOOD/BAD price
/bets — your open bets + live hedge numbers
/won 2 · /lost 2 · /push 2 — grade bet #2 (I grade moneylines & totals myself)
/result 2 +45.50 — record what you actually made (e.g. after a hedge)
🏆 Best bet of the week: sent automatically (max 1/week) when steam + 5%+ edge + -110 to -140 all line up
/pick — best value bet of the day across all sports (also sent automatically at noon)
/stats — record, profit, ROI, and whether you're beating the closing line
/del 2 — delete bet #2 (mistakes only — it won't count in stats)
/scan — scan right now

I check your bets every scan, text you when a hedge locks profit, and grade them when games end."""


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("offset", 0)
    st.setdefault("chat_id", "")
    st.setdefault("bets", [])
    st.setdefault("next_id", 1)
    st.setdefault("history", [])
    return st


def save_state(st, msg="Update bets"):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)
    if os.getenv("GITHUB_ACTIONS") == "true":  # keep bets between runs
        subprocess.run(["git", "add", STATE_FILE], capture_output=True)
        if subprocess.run(["git", "commit", "-q", "-m", msg], capture_output=True).returncode == 0:
            subprocess.run(["git", "pull", "-q", "--rebase"], capture_output=True)
            subprocess.run(["git", "push", "-q"], capture_output=True)


def tg_send(chat_id, text):
    if not (TELEGRAM_TOKEN and chat_id):
        print(text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": chat_id, "text": text}, timeout=10)
    except requests.RequestException as e:
        print(f"  ! Telegram failed: {e}")


def parse_bet(text, need_stake=True):
    toks = text.replace("$", " ").split()
    b = {"odds": None, "stake": None, "side": None, "line": None, "market": "h2h"}
    words, i = [], 0
    while i < len(toks):
        t, tl = toks[i], toks[i].lower()
        if re.fullmatch(r"[+-]\d{3,5}", t) and b["odds"] is None:
            b["odds"] = int(t)
        elif re.fullmatch(r"[+-]\d{1,2}(\.5)?", t) or tl in ("pk", "pick"):
            b["market"], b["line"] = "spreads", (0.0 if tl in ("pk", "pick") else float(t))
        elif tl in ("over", "under", "o", "u") and i + 1 < len(toks) and re.fullmatch(r"\d+(\.\d+)?", toks[i + 1]):
            b["side"], b["line"] = ("Over" if tl in ("over", "o") else "Under"), float(toks[i + 1])
            i += 1
        elif re.fullmatch(r"[ou]\d+(\.\d+)?", tl):
            b["side"], b["line"] = ("Over" if tl[0] == "o" else "Under"), float(tl[1:])
        elif tl in PROP_WORDS:
            b["market"] = PROP_WORDS[tl]
        elif re.fullmatch(r"\d+(\.\d+)?", t):
            b["stake"] = float(t)
        elif tl not in FILLER:
            words.append(t)
        i += 1
    if b["side"] and b["market"] in ("h2h", "spreads"):
        b["market"] = "totals"
    if b["market"] not in ("h2h", "spreads") and (not b["side"] or b["line"] is None):
        return None  # totals/props need over/under + a number
    if b["odds"] is None or (need_stake and not b["stake"]) or not words:
        return None
    b["words"] = words
    return b


def find_event(words):
    """Best-matching upcoming/live event for the words given. Uses free /events calls."""
    ws = [w.lower() for w in words if len(w) >= 3]
    now = datetime.now(timezone.utc)
    best = None
    for sk in resolve_sports():
        for ev in api_get(f"/sports/{sk}/events"):
            teams = f"{ev['home_team']} {ev['away_team']}".lower()
            hits = [w for w in ws if w in teams]
            if not hits:
                continue
            t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            score = (len(hits), -abs((t - now).total_seconds()))
            if best is None or score > best[0]:
                best = (score, sk, ev, hits)
    return best


def resolve(b):
    """Turn parsed words into a specific game + side. Returns dict, or an error string."""
    found = find_event(b["words"])
    if not found:
        return f"Couldn't find a game for '{' '.join(b['words'])}'. Try the team's full name."
    _, sk, ev, hits = found
    rest = [w for w in b["words"] if w.lower() not in hits]
    if b["market"] == "h2h":
        if any(w.lower() in ("draw", "tie") for w in b["words"]):
            side = "Draw"
        else:
            side = max([ev["home_team"], ev["away_team"]],
                       key=lambda t: sum(w in t.lower() for w in hits))
        player, label = None, f"{side} ML" if side != "Draw" else "Draw"
    elif b["market"] == "spreads":
        side = max([ev["home_team"], ev["away_team"]],
                   key=lambda t: sum(w in t.lower() for w in hits))
        player, label = None, f"{side} {b['line']:+g}"
    elif b["market"] == "totals":
        if not b["side"]:
            return "Totals need over/under and a number, e.g. /bet Texas over 47.5 -110 50"
        side, player, label = b["side"], None, f"{b['side']} {b['line']:g} (game total)"
    else:
        if not rest:
            return "Props need the player's name and team, e.g. /bet Dak Prescott over 219.5 pass Cowboys -110 50"
        side, player = b["side"], " ".join(rest)
        label = f"{player} {b['side']} {b['line']:g} {b['market'].replace('player_', '').replace('_', ' ')}"
    return {"sport": sk, "ev": ev, "side": side, "player": player, "label": label, "home": ev["home_team"]}


def add_bet(st, text):
    b = parse_bet(text)
    if not b:
        return "Couldn't read that. Example: /bet Texas -115 100"
    r = resolve(b)
    if isinstance(r, str):
        return r
    sk, ev, side, player, label = r["sport"], r["ev"], r["side"], r["player"], r["label"]
    payout = b["stake"] * am_to_dec(b["odds"])
    start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
    bet = {"id": st["next_id"], "sport": sk, "event_id": ev["id"],
           "event": f"{ev['away_team']} @ {ev['home_team']}", "commence": ev["commence_time"],
           "market": b["market"], "side": side, "player": player, "line": b["line"], "home": r["home"],
           "odds": b["odds"], "stake": b["stake"], "payout": round(payout, 2),
           "label": label, "last_lock": None, "status": "",
           "placed": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "placed_live": start <= datetime.now(timezone.utc),
           "close_p": None, "result": None, "profit": None}
    if sk.startswith("tennis"):
        try:
            import tennis
            bet["surface"] = tennis.surface_for_event(ev.get("sport_title") or sk)
        except Exception:
            pass
    st["next_id"] += 1
    st["bets"].append(bet)
    return (f"✅ Bet #{bet['id']} saved: {label} {b['odds']:+d} ${b['stake']:g} "
            f"(pays ${payout:.2f})\n{bet['event']}\nI'll message you when a hedge locks profit.")


def check_price(text):
    b = parse_bet(text, need_stake=False)
    if not b:
        return "Example: /check Lazio over 3 +112  (team/player, line if any, the odds you see)"
    r = resolve(b)
    if isinstance(r, str):
        return r
    ev = r["ev"]
    data = api_get(f"/sports/{r['sport']}/events/{ev['id']}/odds", markets=b["market"],
                   oddsFormat="american", **book_params())
    probe = {"market": b["market"], "line": b["line"], "player": r["player"], "side": r["side"], "home": r["home"]}
    best, all_sides = collect_prices(data, MY_BOOKS) if data else ({}, {})
    g = bet_group(probe, best) if best else None
    my_d = am_to_dec(b["odds"])
    out = [f"🔎 {r['label']} {b['odds']:+d}", f"{ev['away_team']} @ {ev['home_team']}"]
    if b["odds"] < 0 and b["odds"] < MAX_FAVORITE:
        out.append(f"⛔ Shorter than your {MAX_FAVORITE:+.0f} limit — your rule says pass.")
    p = fair_probs(data).get(g, {}).get(r["side"]) if g else None
    if p:
        edge = (p * my_d - 1) * 100
        verdict = ("✅ GOOD price — real edge" if edge >= MIN_EV_PCT else
                   "🟡 FAIR price — no real edge, only bet if you have a strong read" if edge > -2 else
                   "❌ BAD price — you're overpaying")
        out.append(f"True odds (exchanges): {dec_to_am(1 / p)} · {p * 100:.0f}% to win")
        out.append(f"Your price is {edge:+.1f}% vs true → {verdict}")
    elif g:
        nv = []
        for bk in data.get("bookmakers", []):
            one = collect_prices({"bookmakers": [bk], "home_team": data.get("home_team")})[0].get(g, {})
            if r["side"] in one and len(one) >= 2:
                tot = sum(1 / v[0] for v in one.values())
                nv.append((1 / one[r["side"]][0]) / tot)
        if nv:
            p2 = sum(nv) / len(nv)
            edge = (p2 * my_d - 1) * 100
            out.append(f"Exchanges don't post this line. Sportsbook-only estimate (less reliable): "
                       f"{dec_to_am(1 / p2)} · {p2 * 100:.0f}% · your price {edge:+.1f}%")
        else:
            out.append("No price on this exact line at your books, so I can't judge it.")
    else:
        out.append("This exact line isn't posted at your books, so I can't judge it.")
    if g and r["side"] in best.get(g, {}):
        d, book, _ = best[g][r["side"]]
        tag = "  ← better than yours!" if d > my_d + 1e-9 else ""
        out.append(f"Best at your books: {dec_to_am(d)} @ {book}{tag}")
    out.append(f"To win ${UNIT_TO_WIN:.0f}: risk ${UNIT_TO_WIN / (my_d - 1):.2f}")
    return "\n".join(out)


def grade_from_scores(bet, ev):
    if not ev or not ev.get("completed") or not ev.get("scores"):
        return None
    try:
        sc = {x["name"]: float(x["score"]) for x in ev["scores"]}
    except (TypeError, ValueError, KeyError):
        return None
    if bet["market"] == "h2h":
        h, a = sc.get(ev["home_team"]), sc.get(ev["away_team"])
        if h is None or a is None:
            return None
        winner = ev["home_team"] if h > a else ev["away_team"] if a > h else "Draw"
        if winner == "Draw" and bet["side"] != "Draw" and not bet["sport"].startswith("soccer"):
            return "push"
        return "won" if bet["side"] == winner else "lost"
    if bet["market"] == "spreads":
        mine = sc.get(bet["side"])
        opp = [v for k, v in sc.items() if k != bet["side"]]
        if mine is None or not opp:
            return None
        diff = mine + bet["line"] - opp[0]
        return "push" if abs(diff) < 1e-9 else ("won" if diff > 0 else "lost")
    if bet["market"] == "totals":
        tot = sum(sc.values())
        if abs(tot - bet["line"]) < 1e-9:
            return "push"
        return "won" if (tot > bet["line"]) == (bet["side"] == "Over") else "lost"
    return None


def settle(st, bet, result, profit=None):
    if profit is None:
        profit = {"won": bet["payout"] - bet["stake"], "lost": -bet["stake"], "push": 0.0}[result]
    bet["result"], bet["profit"] = result, round(profit, 2)
    if bet in st["bets"]:
        st["bets"].remove(bet)
        st["history"].append(bet)


def find_bet(st, n):
    for b in st["bets"] + st["history"]:
        if b["id"] == n:
            return b
    return None


def stats_text(st):
    done = [b for b in st["history"] if b.get("result")]
    waiting = [b for b in st["history"] if not b.get("result")]
    if not done:
        msg = "No graded bets yet."
        if waiting:
            msg += f" {len(waiting)} waiting for a result: " + ", ".join(f"#{b['id']}" for b in waiting)
        return msg
    w = sum(b["result"] == "won" for b in done)
    l = sum(b["result"] == "lost" for b in done)
    pu = sum(b["result"] == "push" for b in done)
    hedged = sum(b["result"] == "custom" for b in done)
    risked = sum(b["stake"] for b in done)
    profit = sum(b["profit"] for b in done)
    out = ["📊 Your betting record",
           f"Record: {w}-{l}" + (f"-{pu} (pushes)" if pu else "") + (f" · {hedged} hedged/custom" if hedged else ""),
           f"Profit: {'+' if profit >= 0 else '-'}${abs(profit):,.2f} ({profit / UNIT_TO_WIN:+.1f} units)",
           f"Risked: ${risked:,.2f} · ROI {profit / risked * 100:+.1f}%" if risked else ""]
    clv = [b["close_p"] * am_to_dec(b["odds"]) - 1 for b in done + st["bets"]
           if b.get("close_p") and not b.get("placed_live")]
    if clv:
        beat = sum(c > 0 for c in clv)
        out.append(f"Closing line: beat it on {beat} of {len(clv)} pre-game bets · avg {sum(clv) / len(clv) * 100:+.1f}%")
        out.append("(Beating the close over time = real edge, even through losing streaks.)")
    tennis_done = [b for b in done if b.get("sport", "").startswith("tennis")]
    if tennis_done:
        out.append("🎾 Tennis breakdown:")
        groups = {}
        for b in tennis_done:
            kind = "ML" if b["market"] == "h2h" else ("Overs" if b.get("side") == "Over" else
                                                     "Unders" if b.get("side") == "Under" else b["market"])
            for key in (f"{b.get('surface') or 'Unknown'} court", kind):
                groups.setdefault(key, []).append(b)
        for key, bs in sorted(groups.items()):
            p_ = sum(x["profit"] for x in bs)
            r_ = sum(x["stake"] for x in bs)
            out.append(f"   {key}: {sum(x['result'] == 'won' for x in bs)}-{sum(x['result'] == 'lost' for x in bs)}"
                       f" · {'+' if p_ >= 0 else '-'}${abs(p_):,.0f} · ROI {p_ / r_ * 100:+.0f}%" if r_ else "")
    if len(done) < 50:
        out.append(f"Only {len(done)} bets so far — need ~100+ before the record means much.")
    if waiting:
        out.append("Waiting for a result: " + ", ".join(f"#{b['id']}" for b in waiting) + " — send /won, /lost or /push")
    return "\n".join(x for x in out if x)


def bet_group(bet, best):
    for g in best:
        if g[0] != bet["market"]:
            continue
        if bet["market"] == "h2h":
            return g
        if bet["market"] == "totals" and g[1] == bet["line"]:
            return g
        if bet["market"] == "spreads" and bet.get("home"):
            rel = bet["line"] if bet["side"] == bet["home"] else -bet["line"]
            if abs(g[1] - rel) < 1e-9:
                return g
        if bet["player"] and len(g) == 3 and g[2] == bet["line"] and \
                all(w.lower() in str(g[1]).lower() for w in bet["player"].split()):
            return g
    return None


def check_bets(st, odds_cache):
    changed = False
    now = datetime.now(timezone.utc)
    scores_cache = {}
    for bet in list(st["bets"]):
        start = datetime.fromisoformat(bet["commence"].replace("Z", "+00:00"))
        since = (now - start).total_seconds()
        # auto-grade moneylines & totals once the game is likely over (checks at most every 30 min)
        if since > 2 * 3600 and bet["market"] in ("h2h", "totals", "spreads") and \
                time.time() - bet.get("grade_try", 0) > 1800:
            bet["grade_try"] = time.time()
            changed = True
            if bet["sport"] not in scores_cache:
                try:
                    scores_cache[bet["sport"]] = {e["id"]: e for e in
                                                  api_get(f"/sports/{bet['sport']}/scores", daysFrom=1)}
                except requests.RequestException:
                    scores_cache[bet["sport"]] = {}
            res = grade_from_scores(bet, scores_cache[bet["sport"]].get(bet["event_id"]))
            if res:
                settle(st, bet, res)
                icon = {"won": "✅ WON", "lost": "❌ LOST", "push": "↩️ PUSH"}[res]
                tg_send(st["chat_id"], f"{icon} — bet #{bet['id']} {bet['label']} {bet['odds']:+d}: "
                                       f"{'+' if bet['profit'] >= 0 else '-'}${abs(bet['profit']):.2f}\n"
                                       f"Hedged it? Send /result {bet['id']} <what you actually made>")
                continue
        if since > 8 * 3600:
            st["bets"].remove(bet)
            st["history"].append(bet)
            tg_send(st["chat_id"], f"🏁 Bet #{bet['id']} ({bet['label']}) should be over. How did it go?\n"
                                   f"Send /won {bet['id']}, /lost {bet['id']} or /push {bet['id']} "
                                   f"(or /result {bet['id']} <amount> if you hedged).")
            changed = True
            continue
        if start > now + timedelta(hours=12):
            bet["status"] = "game hasn't started yet — I'll watch it when it gets close"
            continue
        data = odds_cache.get(bet["event_id"]) if bet["market"] in ("h2h", "totals", "spreads") else None
        if data is None:
            try:
                data = api_get(f"/sports/{bet['sport']}/events/{bet['event_id']}/odds",
                               markets=bet["market"], oddsFormat="american", **book_params())
            except requests.RequestException:
                data = None
        if not data:
            bet["status"] = "no prices right now (game may be paused or over)"
            continue
        best, all_sides = collect_prices(data, MY_BOOKS)
        g = bet_group(bet, best)
        if not g:
            bet["status"] = "that line isn't posted at your books right now"
            continue
        if now < start:  # last pre-game fair price = closing line
            fp = fair_probs(data).get(g, {}).get(bet["side"])
            if fp:
                bet["close_p"] = round(fp, 4)
                changed = True
        others = [s_ for s_ in all_sides[g] if s_ != bet["side"]]
        if not others or not all(s_ in best[g] for s_ in others):
            bet["status"] = "other side isn't posted at your books right now"
            continue
        P, S = bet["payout"], bet["stake"]
        hedges = [(s_, best[g][s_][0], best[g][s_][1], P / best[g][s_][0]) for s_ in others]
        lock = P - S - sum(h[3] for h in hedges)
        legs = " + ".join(f"${h[3]:.2f} on {h[0]} {dec_to_am(h[1])} @ {h[2]}" for h in hedges)
        bet["status"] = f"hedge {legs} → {'+' if lock >= 0 else '-'}${abs(lock):.2f} either way"
        last = bet.get("last_lock")
        if lock > 0 and (last is None or lock >= last + max(2.0, 0.1 * last)):
            msg = [f"🔒 HEDGE NOW — bet #{bet['id']}: {bet['label']} {bet['odds']:+d} ${S:g}",
                   f"{bet['event']}",
                   f"Bet {legs}",
                   f"→ locks +${lock:.2f} no matter who wins."]
            if len(hedges) == 1:
                d = hedges[0][1]
                fr = S / (d - 1)
                msg.append(f"Or free-roll: ${fr:.2f} on {hedges[0][0]} → +${P - S - fr:.2f} if your bet wins, $0 if not.")
            msg.append("Check the price in the app first — live odds move fast.")
            tg_send(st["chat_id"], "\n".join(msg))
            bet["last_lock"] = round(lock, 2)
            changed = True
    return changed


def active_sports(window_back_h=6, window_ahead_h=6):
    """Only sports with a game live or starting soon (event lists are free)."""
    now = datetime.now(timezone.utc)
    out = []
    keys = resolve_sports()
    if SCAN_SPORTS:
        keys = [k for k in keys if any(k.startswith(x.rstrip("*")) for x in SCAN_SPORTS)]
    for sk in keys:
        for ev in api_get(f"/sports/{sk}/events"):
            t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            if now - timedelta(hours=window_back_h) <= t <= now + timedelta(hours=window_ahead_h):
                out.append(sk)
                break
    return out


def markets_for(sk):
    m = [x for x in MARKETS.split(",") if x]
    if sk.startswith("americanfootball") and "spreads" not in m:
        m.append("spreads")  # NFL + college football: spreads too
    return ",".join(m)


BEST_BET_UNITS = 2
BEST_BET_MIN_EV = 5.0


def best_bet_check(st, x):
    """Weekly best bet: steam + 5%+ edge + -110 to -140 price. At most one per week."""
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    if st.get("best_bet_week") == week or not x.get("steam") or x["ev"] < BEST_BET_MIN_EV or x["ev"] >= 15:
        return False
    am = float(dec_to_am(x["dec"]))
    if not -140 <= am <= -110:
        return False
    st["best_bet_week"] = week
    risk = BEST_BET_UNITS * UNIT_TO_WIN / (x["dec"] - 1)
    tg_send(st["chat_id"], "\n".join([
        f"🏆 BEST BET OF THE WEEK — {BEST_BET_UNITS} units",
        fmt_ev(x),
        f"   ✅ Steam ✅ +{x['ev']:.1f}% edge ✅ {dec_to_am(x['dec'])} in your range",
        f"   {BEST_BET_UNITS}u play: risk ${risk:.2f} to win ${BEST_BET_UNITS * UNIT_TO_WIN:.0f}",
        "   Last check is YOU: if your read disagrees, pass. No other best bet until next week."]))
    return True


def run_cycle(st, seen):
    odds_cache = {}
    best_sent = False
    for sk in active_sports():
        events = api_get(f"/sports/{sk}/odds", markets=markets_for(sk), oddsFormat="american", **book_params())
        for ev in events:
            odds_cache[ev["id"]] = ev
        for a in find_arbs(events):
            key = (a["event"], a["group"], tuple((l[0], l[2], round(l[1], 3)) for l in a["legs"]))
            if key not in seen:
                seen.add(key)
                tg_send(st["chat_id"], fmt_arb(a))
        for x in find_ev(events):
            key = ("ev", x["event"], x["group"], x["side"], x["book"], round(x["dec"], 3))
            if key not in seen:
                seen.add(key)
                if best_bet_check(st, x):
                    best_sent = True
                else:
                    tg_send(st["chat_id"], fmt_ev(x))
    changed = check_bets(st, odds_cache) or best_sent
    print(f"[{datetime.now():%H:%M}] scan done · {len(odds_cache)} games · credits left {_remaining}")
    return changed


def best_picks(n=3):
    """Best value bets starting in the next 24h across ALL sports (costs ~1-3 credits per active sport)."""
    now = datetime.now(timezone.utc)
    picks = []
    for sk in resolve_sports():
        evs = [e for e in api_get(f"/sports/{sk}/events")
               if now < datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) < now + timedelta(hours=24)]
        if not evs:
            continue  # nothing today = no credits spent
        events = api_get(f"/sports/{sk}/odds", markets=markets_for(sk), oddsFormat="american", **book_params())
        for x in find_ev(events):
            if x["ev"] < 15:  # bigger "edges" are usually stale lines
                am = float(dec_to_am(x["dec"]))
                x["in_range"] = -140 <= am <= -110
                x["score"] = x["ev"] + (3 if x.get("steam") else 0) + (1 if x["in_range"] else 0)
                picks.append(x)
    picks.sort(key=lambda x: -x["score"])
    return picks[:n]


def pick_text():
    picks = best_picks()
    if not picks:
        return ("🎯 No pick today — nothing at your books beats the sharp price by "
                f"{MIN_EV_PCT:g}%+. Passing IS a sharp move.")
    out = ["🎯 PICK OF THE DAY", fmt_ev(picks[0])]
    if picks[0].get("in_range"):
        out.append("   ✅ In your -110 to -140 range.")
    if len(picks) > 1:
        out.append("\nRunners-up:")
        for x in picks[1:]:
            out.append(f"• {x['side']} {dec_to_am(x['dec'])} @ {x['book']} — {x['event']} (+{x['ev']:.1f}%)")
    out.append("\nBest price ≠ sure winner. Add your own read, then /bet it if you play.")
    return "\n".join(out)


def handle_command(st, text, seen):
    cmd, _, rest = text.strip().partition(" ")
    cmd = cmd.lower().split("@")[0]
    if cmd == "/bet":
        reply = add_bet(st, rest)
        save_state(st, "Add bet")
        return reply
    if cmd == "/check":
        return check_price(rest)
    if cmd == "/tennis":
        import tennis
        return tennis.card(sys.modules[__name__], rest)
    if cmd in ("/won", "/lost", "/push", "/result"):
        nums = re.findall(r"[+-]?\d+(?:\.\d+)?", rest)
        if not nums:
            return f"Which bet? e.g. {cmd} 2" + (" +45.50" if cmd == "/result" else "")
        bet = find_bet(st, int(float(nums[0])))
        if not bet:
            return "I don't have that bet number. Send /bets or /stats to see them."
        if cmd == "/result":
            if len(nums) < 2:
                return "Add the amount you made, e.g. /result 2 +45.50 (or -20 if you lost money)"
            settle(st, bet, "custom", float(nums[1]))
        else:
            settle(st, bet, cmd[1:])
        save_state(st, "Grade bet")
        return f"Got it — bet #{bet['id']} {bet['label']}: {'+' if bet['profit'] >= 0 else '-'}${abs(bet['profit']):.2f}\n\n" + stats_text(st)
    if cmd == "/stats":
        return stats_text(st)
    if cmd == "/pick":
        return pick_text()
    if cmd == "/bets":
        if not st["bets"]:
            return "No open bets. Add one: /bet Texas -115 100"
        lines = []
        for b in st["bets"]:
            lines.append(f"#{b['id']} {b['label']} {b['odds']:+d} ${b['stake']:g} (pays ${b['payout']:.2f})\n"
                         f"   {b['event']}\n   Now: {b['status'] or 'checking at next scan'}")
        return "\n\n".join(lines)
    if cmd == "/del":
        ids = {int(x) for x in re.findall(r"\d+", rest)}
        before = len(st["bets"]) + len(st["history"])
        st["bets"] = [b for b in st["bets"] if b["id"] not in ids]
        st["history"] = [b for b in st["history"] if b["id"] not in ids]
        save_state(st, "Remove bet")
        return f"Deleted {before - len(st['bets']) - len(st['history'])} bet(s)."
    if cmd == "/scan":
        if run_cycle(st, seen):
            save_state(st, "Hedge alert")
        return "Scan done. Anything worth acting on was sent above."
    return HELP


def poll_telegram(st, seen, wait):
    if not TELEGRAM_TOKEN:
        time.sleep(max(1, wait))
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                         params={"offset": st["offset"], "timeout": int(max(1, min(wait, 50)))},
                         timeout=60).json()
    except (requests.RequestException, ValueError):
        time.sleep(5)
        return
    got = False
    for u in r.get("result", []):
        st["offset"] = u["update_id"] + 1
        got = True
        msg = u.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        if not chat:
            continue
        if not st["chat_id"]:
            st["chat_id"] = TELEGRAM_CHAT_ID or chat
        if chat != st["chat_id"]:
            continue  # ignore strangers
        try:
            reply = handle_command(st, msg.get("text", ""), seen)
        except Exception as e:
            reply = f"Something went wrong: {e}"
        tg_send(chat, reply)
    if got:
        save_state(st, "Telegram update")


def cmd_watch(args):
    global TELEGRAM_CHAT_ID
    every = args.every or WATCH_EVERY_MIN
    end = time.time() + args.hours * 3600 if args.hours else float("inf")
    st = load_state()
    if not st["chat_id"]:
        st["chat_id"] = TELEGRAM_CHAT_ID or telegram_chat_id() or ""
    TELEGRAM_CHAT_ID = st["chat_id"]
    seen, next_scan, low_warned = set(), 0.0, False
    print(f"Live mode: scanning every {every} min.")
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    if st.get("last_pick") != today and datetime.now(timezone.utc).hour < 20:  # noon-session run only (UTC)
        try:
            tg_send(st["chat_id"], pick_text())
            st["last_pick"] = today
            save_state(st, "Daily pick")
        except Exception as e:
            print(f"  ! daily pick failed: {e}")
    while time.time() < end:
        if time.time() >= next_scan:
            try:
                if run_cycle(st, seen):
                    save_state(st, "Hedge alert")
            except SystemExit as e:
                tg_send(st["chat_id"], f"⛔ Bot stopped: {e}")
                raise
            except Exception as e:
                print(f"  ! scan error: {e}")
            gap = every
            if _remaining is not None and int(float(_remaining)) < 300:
                gap = every * 4
                if not low_warned:
                    tg_send(st["chat_id"], f"⚠️ Only {_remaining} odds credits left — slowing scans to every {gap} min.")
                    low_warned = True
            next_scan = time.time() + gap * 60
        poll_telegram(st, seen, min(next_scan, end) - time.time())


def cmd_best(args):
    sports = [args.sport] if args.sport else resolve_sports()
    team = args.team.lower()
    for sk in sports:
        for ev in api_get(f"/sports/{sk}/events"):  # free
            if team not in (ev["home_team"] + " " + ev["away_team"]).lower():
                continue
            data = api_get(f"/sports/{sk}/events/{ev['id']}/odds", markets=args.market,
                           oddsFormat="american", **book_params())
            best, _ = collect_prices(data, MY_BOOKS)
            print(f"\n{ev['away_team']} @ {ev['home_team']} — {args.market}")
            for g, sides in sorted(best.items(), key=lambda x: str(x[0])):
                if args.player and (len(g) < 3 or args.player.lower() not in str(g[1]).lower()):
                    continue
                print(f"  {fmt_line(g)}")
                for s, (d, book, _) in sorted(sides.items()):
                    print(f"     {s:<14} {dec_to_am(d):>6}  @ {book}")
            print(f"\nCredits remaining: {_remaining}")
            return
    print(f"No event found for '{args.team}'.")


def cmd_check(args):
    """You saw a price somewhere off-feed (e.g. Courtside). Find the best other side at your books."""
    my_d = parse_odds(args.odds)
    sports = [args.sport] if args.sport else resolve_sports()
    team, side_q = args.team.lower(), args.side.lower()
    for sk in sports:
        for ev in api_get(f"/sports/{sk}/events"):  # free
            if team not in (ev["home_team"] + " " + ev["away_team"]).lower():
                continue
            data = api_get(f"/sports/{sk}/events/{ev['id']}/odds", markets=args.market,
                           oddsFormat="american", **book_params())
            best, all_sides = collect_prices(data, MY_BOOKS)
            fair = fair_probs(data)
            hits = []
            for g, sides in best.items():
                if args.player and (len(g) < 3 or args.player.lower() not in str(g[1]).lower()):
                    continue
                if args.line is not None and g[-1] is not None and abs(abs(float(g[-1])) - abs(args.line)) > 1e-9:
                    continue
                mine = [s for s in all_sides[g] if side_q in s.lower()]
                if len(mine) != 1:
                    continue
                others = [s for s in all_sides[g] if s != mine[0]]
                if all(s in sides for s in others):
                    hits.append((g, mine[0], others, sides))
            print(f"\n{ev['away_team']} @ {ev['home_team']} — {args.market}")
            if not hits:
                print("  Couldn't match that line at your books. Try `best` to see what's posted.")
            for g, mine, others, sides in hits:
                decs = [my_d] + [sides[s][0] for s in others]
                raw, rounded, worst, pct = split_stakes(decs, args.total or BANKROLL)
                print(f"  {fmt_line(g)}")
                print(f"   • ${rounded[0]:>4} on {mine} {dec_to_am(my_d)} @ {args.book}")
                for s, st in zip(others, rounded[1:]):
                    print(f"   • ${st:>4} on {s} {dec_to_am(sides[s][0])} @ {sides[s][1]}")
                print("   → " + (f"ARB {pct:.2f}%: guaranteed ${worst:.2f}" if pct > 0
                                else f"No arb ({pct:.2f}%). Best other side is {dec_to_am(decs[1])} @ {sides[others[0]][1]}"))
                p = fair.get(g, {}).get(mine)
                if p:
                    edge = (p * my_d - 1) * 100
                    verdict = ("GOOD price — value bet" if edge >= MIN_EV_PCT else
                               "fair-ish price" if edge > -2 else "BAD price — you're overpaying")
                    print(f"   Fair price (exchanges): {dec_to_am(1 / p)} · win chance {p * 100:.0f}%")
                    print(f"   Your price is {edge:+.1f}% vs fair → {verdict}"
                          + (f" · suggested bet ${suggest_stake(p, my_d)}" if edge > 0 else ""))
            print(f"\nCredits remaining: {_remaining}")
            return
    print(f"No event found for '{args.team}'.")


def cmd_hedge(args):
    stake = args.stake
    payout = args.payout if args.payout else stake * parse_odds(args.odds)
    dh = parse_odds(args.hedge_odds)
    profit_if_orig = payout - stake

    eq = payout / dh                 # same result either way
    be = stake / (dh - 1)            # hedge win just gets your stake back
    print(f"Open bet: ${stake:.2f} to return ${payout:.2f} (profit ${profit_if_orig:.2f})")
    print(f"Hedge price: {dec_to_am(dh)}\n")
    print(f"{'Plan':<26}{'Hedge $':>10}{'If bet wins':>14}{'If hedge wins':>16}")
    rows = [("No hedge", 0), ("Lock same result", eq), ("Break-even if bet loses", be)]
    if args.amount:
        rows.append(("Your amount", args.amount))
    for name, h in rows:
        print(f"{name:<26}{h:>10.2f}{payout - stake - h:>+14.2f}{h * dh - h - stake:>+16.2f}")
    locked = payout - stake - eq
    print("\n→ " + (f"TRUE ARB: hedge ${eq:.2f} to lock +${locked:.2f} no matter what."
                    if locked > 0 else
                    f"No free money: locking costs you ${-locked:.2f}. Only worth it to cut risk."))


def main():
    p = argparse.ArgumentParser(description="Arb + hedge finder for your books")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("books")
    sub.add_parser("scan")
    sub.add_parser("test")
    w = sub.add_parser("watch"); w.add_argument("--every", type=int, help="minutes between scans")
    w.add_argument("--hours", type=float, help="stop after this many hours")
    b = sub.add_parser("best")
    b.add_argument("--team", required=True)
    b.add_argument("--market", default="h2h", help="h2h, spreads, totals, player_pass_yds, player_points, …")
    b.add_argument("--player")
    b.add_argument("--sport", help="e.g. americanfootball_nfl (default: your SPORTS list)")
    c = sub.add_parser("check", help="check a price from an off-feed app like Courtside")
    c.add_argument("--team", required=True)
    c.add_argument("--side", required=True, help="Over / Under / team name")
    c.add_argument("--odds", required=True, help="the price you see, e.g. -110")
    c.add_argument("--market", default="h2h")
    c.add_argument("--player")
    c.add_argument("--line", type=float, help="the number, e.g. 219.5 or 3.5")
    c.add_argument("--sport")
    c.add_argument("--book", default="Courtside")
    c.add_argument("--total", type=float, help="total $ across both sides")
    h = sub.add_parser("hedge")
    h.add_argument("--stake", type=float, required=True)
    h.add_argument("--odds", help="original odds, e.g. -135 or +250")
    h.add_argument("--payout", type=float, help="total return instead of --odds (parlays)")
    h.add_argument("--hedge-odds", required=True, help="price on the other side, e.g. +150")
    h.add_argument("--amount", type=float, help="also show a hedge of this size")
    args = p.parse_args()
    if args.cmd == "hedge" and not (args.odds or args.payout):
        p.error("hedge needs --odds or --payout")
    {"books": cmd_books, "scan": cmd_scan, "watch": cmd_watch,
     "best": cmd_best, "check": cmd_check, "test": cmd_test, "hedge": cmd_hedge}[args.cmd](args)


if __name__ == "__main__":
    main()
