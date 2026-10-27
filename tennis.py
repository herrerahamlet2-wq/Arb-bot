"""
tennis.py — the /tennis match card for arb_bot.

Combines:
  • a surface-adjusted Elo model built from Jeff Sackmann's free ATP/WTA match data
    (github.com/JeffSackmann, CC BY-NC-SA 4.0 — personal, non-commercial use)
  • head-to-head, recent form, hold/break on the surface, total-games history
  • market "true odds" (Pinnacle + exchanges) and the best price at your books
  • your price rules (-110 to -140 sweet spot, never past -175)
"""

import csv
import io
import math
import re
import time
import unicodedata
from datetime import datetime, timedelta

import requests

RAW = "https://raw.githubusercontent.com/JeffSackmann/{repo}/master/{tour}_matches_{year}.csv"
YEARS_BACK = 4
SWEET_SPOT = (-140, -110)   # your preferred favorite range
ROUND_ORDER = {"Q1": 0, "Q2": 1, "Q3": 2, "R128": 3, "R64": 4, "R32": 5, "R16": 6,
               "RR": 6, "QF": 7, "SF": 8, "BR": 8, "F": 9}
_CACHE = {"t": 0.0, "data": None}


# ── helpers ────────────────────────────────────────────────────────────────
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", " ", s.lower()).split()


def surface_key(s):
    s = (s or "").title()
    return "Hard" if s in ("Carpet", "") else s


def games_in(score):
    """Total games in a completed match score like '6-4 3-6 7-6(5)'. None if retired/walkover."""
    if not score or any(x in score.upper() for x in ("RET", "W/O", "DEF", "ABD", "UNK")):
        return None
    total, sets = 0, 0
    for a, b in re.findall(r"(\d+)-(\d+)", re.sub(r"\(\d+\)", "", score)):
        a, b = int(a), int(b)
        if a > 7 or b > 7:   # super tiebreak written as 10-8 counts as one game
            total += 1
        else:
            total += a + b
        sets += 1
    return total if sets else None


def num(row, k):
    try:
        return float(row.get(k) or "")
    except ValueError:
        return None


# ── data + Elo ─────────────────────────────────────────────────────────────
def load():
    if _CACHE["data"] and time.time() - _CACHE["t"] < 6 * 3600:
        return _CACHE["data"]
    rows = []
    year = datetime.utcnow().year
    for tour, repo in (("atp", "tennis_atp"), ("wta", "tennis_wta")):
        for yr in range(year - YEARS_BACK, year + 1):
            try:
                r = requests.get(RAW.format(repo=repo, tour=tour, year=yr), timeout=30)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                continue
            for row in csv.DictReader(io.StringIO(r.text)):
                row["tour"] = tour
                rows.append(row)
    if not rows:
        return None
    rows.sort(key=lambda x: (x.get("tourney_date", ""), ROUND_ORDER.get(x.get("round", ""), 5)))
    _CACHE.update(t=time.time(), data=build(rows))
    return _CACHE["data"]


def build(rows):
    elo, selo, n, sn = {}, {}, {}, {}
    pre = []   # model prob for the winner before each match (for sanity/backtest)
    for r in rows:
        w, l, s = r["winner_name"], r["loser_name"], surface_key(r.get("surface"))
        ew, el = elo.get(w, 1500.0), elo.get(l, 1500.0)
        sw, sl = selo.get((w, s), 1500.0), selo.get((l, s), 1500.0)
        pw = 1 / (1 + 10 ** ((el - ew) / 400))
        ps = 1 / (1 + 10 ** ((sl - sw) / 400))
        pre.append((pw + ps) / 2)
        kw, kl = 250 / (n.get(w, 0) + 5) ** 0.4, 250 / (n.get(l, 0) + 5) ** 0.4
        elo[w], elo[l] = ew + kw * (1 - pw), el - kl * (1 - pw)
        skw, skl = 250 / (sn.get((w, s), 0) + 5) ** 0.4, 250 / (sn.get((l, s), 0) + 5) ** 0.4
        selo[(w, s)], selo[(l, s)] = sw + skw * (1 - ps), sl - skl * (1 - ps)
        n[w], n[l] = n.get(w, 0) + 1, n.get(l, 0) + 1
        sn[(w, s)], sn[(l, s)] = sn.get((w, s), 0) + 1, sn.get((l, s), 0) + 1
    names = {}
    for r in rows:
        for k in ("winner_name", "loser_name"):
            names[tuple(norm(r[k]))] = r[k]
    hits = sum(p > 0.5 for p in pre[len(pre) // 2:])
    return {"rows": rows, "elo": elo, "selo": selo, "n": n, "names": names,
            "latest": rows[-1].get("tourney_date", ""),
            "accuracy": hits / max(1, len(pre) - len(pre) // 2)}


def find_player(data, name):
    want = norm(name)
    if not want:
        return None
    if tuple(want) in data["names"]:
        return data["names"][tuple(want)]
    last = want[-1]
    cands = [full for key, full in data["names"].items() if key and key[-1] == last]
    if len(cands) == 1:
        return cands[0]
    cands = [c for c in cands if all(w in norm(c) for w in want)] or cands
    # most matches played = most likely the one meant
    return max(cands, key=lambda c: data["n"].get(c, 0)) if cands else None


def model_prob(data, a, b, surface):
    ea, eb = data["elo"].get(a, 1500.0), data["elo"].get(b, 1500.0)
    sa, sb = data["selo"].get((a, surface), 1500.0), data["selo"].get((b, surface), 1500.0)
    return (1 / (1 + 10 ** ((eb - ea) / 400)) + 1 / (1 + 10 ** ((sb - sa) / 400))) / 2


def detect_surface(data, title, override=None):
    if override:
        return override, "you said"
    words = set(norm(title))
    best = None
    for r in reversed(data["rows"]):
        tn = norm(r.get("tourney_name"))
        key = [w for w in tn if w not in ("masters", "open", "atp", "wta", "cup", "championships")]
        if key and all(w in words for w in key):
            if True:
                best = surface_key(r.get("surface"))
                break
    return (best, "from tournament") if best else ("Hard", "guess — add hard/clay/grass to be sure")


def player_stats(data, p, surface, line=None):
    latest = datetime.strptime(data["latest"], "%Y%m%d") if data["latest"] else datetime.utcnow()
    since = (latest - timedelta(days=365)).strftime("%Y%m%d")
    w = l = sw = sl = 0
    form = []
    sv = brk_lost = opp_sv = opp_brk = 0.0
    games, overs = [], 0
    for r in data["rows"]:
        if p not in (r["winner_name"], r["loser_name"]):
            continue
        won = r["winner_name"] == p
        recent = r.get("tourney_date", "") >= since
        on_s = surface_key(r.get("surface")) == surface
        form.append("W" if won else "L")
        if not recent:
            continue
        w, l = w + won, l + (not won)
        if on_s:
            sw, sl = sw + won, sl + (not won)
            me, op = ("w_", "l_") if won else ("l_", "w_")
            g, f, s_ = num(r, me + "SvGms"), num(r, me + "bpFaced"), num(r, me + "bpSaved")
            og, of, os_ = num(r, op + "SvGms"), num(r, op + "bpFaced"), num(r, op + "bpSaved")
            if None not in (g, f, s_, og, of, os_) and g and og:
                sv, brk_lost = sv + g, brk_lost + (f - s_)
                opp_sv, opp_brk = opp_sv + og, opp_brk + (of - os_)
            gm = games_in(r.get("score"))
            if gm is not None and (r.get("best_of") or "3") == "3":
                games.append(gm)
                if line is not None and gm > line:
                    overs += 1
    return {"w": w, "l": l, "sw": sw, "sl": sl, "form": "".join(form[-10:][::-1]),
            "hold": (1 - brk_lost / sv) if sv else None, "brk": (opp_brk / opp_sv) if opp_sv else None,
            "games": (sum(games) / len(games)) if games else None, "n_games": len(games),
            "over_rate": (overs / len(games)) if games and line is not None else None}


def h2h(data, a, b, surface):
    out = {"a": 0, "b": 0, "sa": 0, "sb": 0, "games": [], "last": None}
    for r in data["rows"]:
        if {r["winner_name"], r["loser_name"]} != {a, b}:
            continue
        aw = r["winner_name"] == a
        out["a" if aw else "b"] += 1
        if surface_key(r.get("surface")) == surface:
            out["sa" if aw else "sb"] += 1
        g = games_in(r.get("score"))
        if g is not None:
            out["games"].append(g)
        out["last"] = f"{r['winner_name'].split()[-1]} {r.get('score', '')} ({r.get('tourney_name', '')} {r.get('tourney_date', '')[:4]})"
    return out


def pct(x):
    return f"{x * 100:.0f}%" if x is not None else "—"


# ── the card ───────────────────────────────────────────────────────────────
def card(bot, text):
    words = [w for w in text.split() if w.lower() not in ("vs", "v", "-", "@")]
    override = next((w.title() for w in words if w.lower() in ("hard", "clay", "grass")), None)
    words = [w for w in words if w.lower() not in ("hard", "clay", "grass")]
    if not words:
        return "Example: /tennis Sinner Alcaraz  (add hard/clay/grass to set the surface)"

    # 1) find the match in the odds feed (event lists are free)
    found, ws = None, [w.lower() for w in words if len(w) >= 3]
    for sk in [s for s in bot.resolve_sports() if s.startswith("tennis")]:
        for ev in bot.api_get(f"/sports/{sk}/events"):
            names = f"{ev['home_team']} {ev['away_team']}".lower()
            hits = sum(w in names for w in ws)
            if hits and (found is None or hits > found[0]):
                found = (hits, sk, ev)
    if not found:
        return f"Couldn't find an upcoming tennis match for '{' '.join(words)}'."
    _, sk, ev = found
    A, B = ev["home_team"], ev["away_team"]
    title = ev.get("sport_title") or sk

    # 2) odds: moneyline + total games (2 credits)
    odds = bot.api_get(f"/sports/{sk}/events/{ev['id']}/odds", markets="h2h,totals",
                       oddsFormat="american", **bot.book_params()) or {}
    best, _ = bot.collect_prices(odds, bot.MY_BOOKS) if odds else ({}, {})
    fair = bot.fair_probs(odds) if odds else {}
    ml_best, ml_fair = best.get(("h2h",), {}), fair.get(("h2h",), {})

    # 3) stats
    data = load()
    out = [f"🎾 {A} vs {B}", f"{title}"]
    pa = pb = None
    surface = override or "Hard"
    if data:
        a, b = find_player(data, A), find_player(data, B)
        surface, how = detect_surface(data, title, override)
        out[1] += f" · {surface} ({how})"
        if a and b:
            p = model_prob(data, a, b, surface)
            pa, pb = p, 1 - p
    else:
        out.append("⚠️ Couldn't load the tennis stats database right now — prices only.")
        a = b = None

    # 4) moneyline section
    out.append("")
    out.append("💵 MONEYLINE")
    for name, pm in ((A, pa), (B, pb)):
        line = [f"{name.split()[-1]}:"]
        if name in ml_best:
            d, book, _ = ml_best[name]
            line.append(f"best {bot.dec_to_am(d)} @ {book}")
            am = float(bot.dec_to_am(d))
            if SWEET_SPOT[0] <= am <= SWEET_SPOT[1]:
                line.append("✅ your -110/-140 range")
            elif am < bot.MAX_FAVORITE:
                line.append("⛔ past -175")
        else:
            d = None
            line.append("not posted at your books")
        mk = ml_fair.get(name)
        if mk:
            line.append(f"| market {pct(mk)} ({bot.dec_to_am(1 / mk)})")
        if pm is not None:
            line.append(f"| model {pct(pm)} ({bot.dec_to_am(1 / pm)})")
        if d and mk:
            line.append(f"| edge vs market {(mk * d - 1) * 100:+.1f}%")
        out.append(" ".join(line))

    # verdict on the side
    if pa is not None and ml_fair and all(n in ml_best for n in (A, B)):
        picks = []
        for name, pm in ((A, pa), (B, pb)):
            d = ml_best[name][0]
            em, ek = pm * d - 1, ml_fair.get(name, 0) * d - 1
            picks.append((name, em, ek))
        name, em, ek = max(picks, key=lambda x: x[1] + x[2])
        if em > 0.03 and ek > 0:
            v = f"✅ Model AND market both like {name.split()[-1]} at this price."
        elif em > 0.03:
            v = f"🟡 Model likes {name.split()[-1]} but the market doesn't — your read is the tiebreaker."
        elif ek > 0.02:
            v = f"🟡 Price beats the market on {name.split()[-1]}, model is neutral."
        else:
            v = "❌ No edge on either side at these prices."
        out.append(v)

    # 5) stats section
    if data and a and b:
        h = h2h(data, a, b, surface)
        sa_, sb_ = player_stats(data, a, surface), player_stats(data, b, surface)
        out += ["", "📊 FORM & MATCHUP",
                f"H2H: {a.split()[-1]} {h['a']}-{h['b']}" + (f" (on {surface} {h['sa']}-{h['sb']})" if h['a'] + h['b'] else "")
                + (f" · last: {h['last']}" if h["last"] else ""),
                f"Last 10: {a.split()[-1]} {sa_['form'] or '—'} · {b.split()[-1]} {sb_['form'] or '—'}",
                f"12 mo: {a.split()[-1]} {sa_['w']}-{sa_['l']} ({surface} {sa_['sw']}-{sa_['sl']}) · "
                f"{b.split()[-1]} {sb_['w']}-{sb_['l']} ({surface} {sb_['sw']}-{sb_['sl']})",
                f"Hold/Break on {surface}: {a.split()[-1]} {pct(sa_['hold'])}/{pct(sa_['brk'])} · "
                f"{b.split()[-1]} {pct(sb_['hold'])}/{pct(sb_['brk'])}"]
        # serve-heavy matchup = tiebreaks = overs
        if sa_["hold"] and sb_["hold"]:
            both = (sa_["hold"] + sb_["hold"]) / 2
            if both >= 0.84:
                out.append("   Both hold serve a lot → tiebreak risk → leans OVER on games.")
            elif both <= 0.72:
                out.append("   Lots of breaks expected → swingy sets, can go either way on games.")
    elif data:
        out.append("")
        out.append("📊 Couldn't match one of the players in the stats data (new/young player or name mismatch).")

    # 6) total games
    tot = [g for g in best if g[0] == "totals"]
    if tot:
        g = min(tot, key=lambda g: abs((fair.get(g, {}).get("Over") or 0.5) - 0.5))
        line = g[1]
        out += ["", f"🔢 TOTAL GAMES {line:g}"]
        for side in ("Over", "Under"):
            if side in best[g]:
                d, book, _ = best[g][side]
                mk = fair.get(g, {}).get(side)
                extra = f" | market {pct(mk)} | edge {(mk * d - 1) * 100:+.1f}%" if mk else ""
                out.append(f"{side}: best {bot.dec_to_am(d)} @ {book}{extra}")
        if data and a and b:
            sa_, sb_ = player_stats(data, a, surface, line), player_stats(data, b, surface, line)
            h = h2h(data, a, b, surface)
            for nm, st in ((a, sa_), (b, sb_)):
                if st["games"] is not None:
                    out.append(f"{nm.split()[-1]}'s best-of-3 {surface} matches: avg {st['games']:.1f} games, "
                               f"over {line:g} in {pct(st['over_rate'])} (of {st['n_games']})")
            if h["games"]:
                out.append(f"H2H avg: {sum(h['games']) / len(h['games']):.1f} games over {len(h['games'])} matches")

    out += ["", "🧠 Your part: news, injuries, motivation, mental state — the numbers can't see those."]
    if data:
        d = data["latest"]
        out.append(f"Stats through {d[4:6]}/{d[6:8]}/{d[:4]} (last {YEARS_BACK} yrs, main tour). "
                   f"Model picked the winner {data['accuracy'] * 100:.0f}% of the time recently.")
    return "\n".join(out)


def surface_for_event(title):
    """Best guess at a tournament's surface (used to tag tennis bets for /stats)."""
    data = _CACHE["data"] or load()
    if not data:
        return None
    return detect_surface(data, title)[0]
