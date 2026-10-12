"""FairParlay collector: this week's NFL prices from the sportsbooks and Kalshi -> docs\\data\\legs.json

    py collect.py                 # pull game lines (6 credits) + player props (~6 per game) within the credit budget
    py collect.py --dry-run       # show what it would pull and what it would cost, no API calls
    py collect.py --demo          # write a realistic sample legs.json without a key (for trying the app)
    py collect.py --kalshi-probe  # list Kalshi's NFL series (free) - for adding spreads/totals/props later

Every two-way market (home/away, over/under, cover/not) is de-vigged book by book, then averaged across books
with Pinnacle counted three times, giving one fair probability per leg. One-sided anytime-touchdown prices are
corrected with a mapping fitted on 3,863 historical DraftKings TD prices (the books kept ~9% on them).
Kalshi's game-winner contracts are attached to the moneyline legs as another place to buy the same thing.

Settings (optional, .env in this folder; ODDS_API_KEY also read from ..\\props-test\\.env; env vars override):
    ODDS_API_KEY=...        RUN_CAP=150 (credits per run)   RESERVE=40 (never go below this many remaining)
    PROP_MARKETS=player_pass_yds,player_pass_tds,player_rush_yds,player_receptions,player_reception_yds,player_anytime_td
    BOOKS=draftkings,fanduel,betmgm,williamhill_us,fanatics,betrivers,hardrockbet,ballybet,espnbet,pinnacle
                            (the books shown; any 10 count as one region for credits; pinnacle is the sharp
                            reference for fair prices and is not shown as a place to bet. BOOKS= empty falls
                            back to whole regions: us,eu for game lines and PROP_REGIONS for props)
    PROP_REGIONS=us         DAYS=8 (how far ahead)          KALSHI_SERIES=KXNFLGAME
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = HERE / "docs" / "data" / "legs.json"
OAPI = "https://api.the-odds-api.com/v4"
KAPI = "https://api.elections.kalshi.com/trade-api/v2"
SPORT = "americanfootball_nfl"
FEATURED = ["h2h", "spreads", "totals"]
FEATURED_REGIONS = ["us", "eu"]                          # eu brings Pinnacle, the sharp reference
DEFAULT_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,fanatics,betrivers,hardrockbet,ballybet,espnbet,pinnacle"
NOT_PLAYERS = ("d/st", "defense", "no scorer", "no touchdown", "special teams")
DEFAULT_PROPS = "player_pass_yds,player_pass_tds,player_rush_yds,player_receptions,player_reception_yds,player_anytime_td"
SHARP = {"pinnacle": 3.0}                                # consensus weight; every other book counts once
TD_A, TD_B = -0.221, 0.899                               # logit(fair) = a + b*logit(implied), fitted on DK anytime-TD prices
STATE = {"used": None, "left": None, "spent": 0}
BOOK_NAMES = {"draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM", "williamhill_us": "Caesars",
              "betrivers": "BetRivers", "fanatics": "Fanatics", "espnbet": "ESPN BET", "hardrockbet": "Hard Rock",
              "ballybet": "Bally Bet", "betparx": "betPARX", "fliff": "Fliff", "novig": "Novig", "prophetx": "ProphetX",
              "pinnacle": "Pinnacle", "bovada": "Bovada", "betonlineag": "BetOnline", "mybookieag": "MyBookie",
              "lowvig": "LowVig", "betus": "BetUS", "windcreek": "Wind Creek", "kalshi": "Kalshi"}
MARKET_NAMES = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total", "player_pass_yds": "Pass Yds",
                "player_pass_tds": "Pass TDs", "player_rush_yds": "Rush Yds", "player_receptions": "Receptions",
                "player_reception_yds": "Rec Yds", "player_anytime_td": "Anytime TD"}
STAT_OF = {"h2h": "margin", "spreads": "margin", "totals": "total", "player_pass_yds": "pass_yds",
           "player_pass_tds": "pass_tds", "player_rush_yds": "rush_yds", "player_receptions": "receptions",
           "player_reception_yds": "reception_yds", "player_anytime_td": "anytime_td"}
ABBR = {"Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
        "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
        "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
        "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
        "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA",
        "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
        "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
        "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS"}
ALIAS = {"WSH": "WAS", "LA": "LAR", "JAC": "JAX", "KAN": "KC", "GNB": "GB", "SFO": "SF", "TAM": "TB", "NWE": "NE",
         "NOR": "NO", "LVR": "LV", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU"}
POS_GROUP = {"QB": "QB", "RB": "RB", "FB": "RB", "HB": "RB", "WR": "WR", "TE": "TE"}


def say(m):
    print(m, flush=True)


# ------------------------------------------------------------------ settings

def load_env() -> dict:
    cfg = {}
    for p in (HERE.parent / "props-test" / ".env", HERE / ".env"):          # later files win
        if p.exists():
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("ODDS_API_KEY", "RUN_CAP", "RESERVE", "PROP_MARKETS", "PROP_REGIONS", "DAYS", "KALSHI_SERIES", "BOOKS"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    cfg.setdefault("RUN_CAP", "150")
    cfg.setdefault("RESERVE", "40")
    cfg.setdefault("PROP_MARKETS", DEFAULT_PROPS)
    cfg.setdefault("PROP_REGIONS", "us")
    cfg.setdefault("BOOKS", DEFAULT_BOOKS)
    cfg.setdefault("DAYS", "8")
    cfg.setdefault("KALSHI_SERIES", "KXNFLGAME")
    return cfg


# ------------------------------------------------------------------ odds math

def implied(am: float) -> float:
    am = float(am)
    return 100 / (am + 100) if am > 0 else -am / (-am + 100)


def decimal(am: float) -> float:
    am = float(am)
    return 1 + am / 100 if am > 0 else 1 + 100 / -am


def american(p: float) -> int:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return int(round(-100 * p / (1 - p))) if p > 0.5 else int(round(100 * (1 - p) / p))


def devig(am_a: float, am_b: float) -> float:
    """Fair probability of side A from a two-way market (vig removed proportionally)."""
    a, b = implied(am_a), implied(am_b)
    return a / (a + b)


def td_fair(am: float) -> float:
    """Fair 'yes' probability from a one-sided anytime-TD price."""
    p = implied(am)
    z = TD_A + TD_B * math.log(p / (1 - p))
    return 1 / (1 + math.exp(-z))


def consensus(probs: dict, robust: bool = False) -> tuple:
    """{book: fair prob} -> (fair prob, number of books, sharp book present).
    Weighted mean with Pinnacle counted three times; the median instead when `robust` (one-sided TD prices,
    where books disagree wildly on long shots and one stale price would drag the mean)."""
    if not probs:
        return None, 0, False
    if robust and len(probs) >= 3:
        vals = sorted(probs.values())
        mid = len(vals) // 2
        fair = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    else:
        w = {b: SHARP.get(b, 1.0) for b in probs}
        fair = sum(probs[b] * w[b] for b in probs) / sum(w.values())
    return round(fair, 4), len(probs), any(b in SHARP for b in probs)


# ------------------------------------------------------------------ http

def get(url: str, odds_api: bool = False, tries: int = 4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "fairparlay/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                if odds_api:
                    STATE["used"] = int(r.headers.get("x-requests-used") or 0)
                    STATE["left"] = int(r.headers.get("x-requests-remaining") or 0)
                    STATE["spent"] += int(r.headers.get("x-requests-last") or 0)
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429:
                time.sleep(5 + 5 * attempt)
                continue
            if e.code in (401, 403) and odds_api:
                raise RuntimeError(f"The Odds API refused the key ({e.code}): {body}") from None
            if e.code == 422 and odds_api:
                say(f"  the API rejected the request ({body}); check PROP_MARKETS")
                return None
            say(f"  HTTP {e.code} {url.split('?')[0][-60:]}: {body}")
            return None
        except Exception as e:                           # noqa: BLE001
            say(f"  retry ({e})")
            time.sleep(3)
    return None


def odds_url(path: str, key: str, **params) -> str:
    return f"{OAPI}{path}?" + urllib.parse.urlencode({"apiKey": key, "oddsFormat": "american", **params})


# ------------------------------------------------------------------ rosters (player -> team, position)

def norm_name(n: str) -> str:
    n = re.sub(r"[^a-z ]", "", n.lower().replace(".", "").replace("'", ""))
    parts = [p for p in n.split() if p not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return " ".join(parts)


def season_year(now: dt.datetime) -> int:
    return now.year if now.month >= 3 else now.year - 1


def load_roster(now: dt.datetime) -> dict:
    """{normalized name: (team, position group)} from nflverse's roster file (refreshed every 3 days)."""
    import csv
    DATA.mkdir(exist_ok=True)
    yr = season_year(now)
    p = DATA / f"roster_{yr}.csv"
    if not p.exists() or (time.time() - p.stat().st_mtime) > 3 * 86400:
        try:
            urllib.request.urlretrieve(f"https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{yr}.csv", p)
        except Exception as e:                           # noqa: BLE001
            say(f"  roster download failed ({e}); player teams may be missing")
            if not p.exists():
                return {}
    out, week = {}, {}
    with p.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pos = POS_GROUP.get(row.get("position", ""))
            if not pos or row.get("status") == "CUT":
                continue
            name = norm_name(row.get("full_name") or "")
            wk = int(row.get("week") or 0)
            if name and wk >= week.get(name, -1):
                week[name] = wk
                out[name] = (ALIAS.get(row["team"], row["team"]), pos)
    return out


# ------------------------------------------------------------------ build legs

def nick(full: str) -> str:
    return full.split()[-1]


def leg(gid, market, stat, side, label, fair, nb, sharp, prices, team=None, player=None, pos=None, point=None, pair=None):
    return {"id": gid, "m": market, "k": stat, "s": side, "lbl": label, "fair": fair, "nb": nb, "sharp": sharp,
            "px": prices, "t": team, "p": player, "pos": pos, "pt": point, "pair": pair}


def featured_legs(ev: dict) -> list:
    eid, home, away = ev["id"], ev["home_team"], ev["away_team"]
    H, A = ABBR.get(home, home[:3].upper()), ABBR.get(away, away[:3].upper())
    legs = []
    h2h, spreads, totals = {}, {}, {}
    for bk in ev.get("bookmakers", []):
        for mk in bk.get("markets", []):
            o = mk.get("outcomes", [])
            if mk["key"] == "h2h":
                pr = {x["name"]: x["price"] for x in o}
                if home in pr and away in pr:
                    h2h[bk["key"]] = (pr[home], pr[away])
            elif mk["key"] == "spreads":
                pr = {x["name"]: (x["price"], x.get("point")) for x in o}
                if home in pr and away in pr and pr[home][1] is not None and pr[home][1] == -pr[away][1]:
                    spreads.setdefault(pr[home][1], {})[bk["key"]] = (pr[home][0], pr[away][0])
            elif mk["key"] == "totals":
                pr = {x["name"]: (x["price"], x.get("point")) for x in o}
                if "Over" in pr and "Under" in pr and pr["Over"][1] == pr["Under"][1] and pr["Over"][1] is not None:
                    totals.setdefault(pr["Over"][1], {})[bk["key"]] = (pr["Over"][0], pr["Under"][0])
    if h2h:
        fair, nb, sharp = consensus({b: devig(h, a) for b, (h, a) in h2h.items()})
        ih, ia = f"{eid}:h2h:{H}", f"{eid}:h2h:{A}"
        legs.append(leg(ih, "h2h", "margin", "over", f"{nick(home)} win", fair, nb, sharp, {b: h for b, (h, a) in h2h.items()}, team=H, pair=ia))
        legs.append(leg(ia, "h2h", "margin", "over", f"{nick(away)} win", round(1 - fair, 4), nb, sharp, {b: a for b, (h, a) in h2h.items()}, team=A, pair=ih))
    for pt, books in sorted(spreads.items()):
        fair, nb, sharp = consensus({b: devig(h, a) for b, (h, a) in books.items()})
        ih, ia = f"{eid}:spreads:{H}:{pt:g}", f"{eid}:spreads:{A}:{-pt:g}"
        legs.append(leg(ih, "spreads", "margin", "over", f"{nick(home)} {pt:+g}", fair, nb, sharp, {b: h for b, (h, a) in books.items()}, team=H, point=pt, pair=ia))
        legs.append(leg(ia, "spreads", "margin", "over", f"{nick(away)} {-pt:+g}", round(1 - fair, 4), nb, sharp, {b: a for b, (h, a) in books.items()}, team=A, point=-pt, pair=ih))
    for pt, books in sorted(totals.items()):
        fair, nb, sharp = consensus({b: devig(o, u) for b, (o, u) in books.items()})
        io, iu = f"{eid}:totals:over:{pt:g}", f"{eid}:totals:under:{pt:g}"
        legs.append(leg(io, "totals", "total", "over", f"Over {pt:g}", fair, nb, sharp, {b: o for b, (o, u) in books.items()}, point=pt, pair=iu))
        legs.append(leg(iu, "totals", "total", "under", f"Under {pt:g}", round(1 - fair, 4), nb, sharp, {b: u for b, (o, u) in books.items()}, point=pt, pair=io))
    return legs


def prop_legs(ev: dict, roster: dict, home_abbr: str, away_abbr: str) -> list:
    eid, legs = ev["id"], []
    groups = {}                                           # (market, player, point) -> {book: {"Over": am, "Under": am}}
    for bk in ev.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk["key"] not in STAT_OF or mk["key"] in FEATURED:
                continue
            for o in mk.get("outcomes", []):
                player = o.get("description") or ""
                side = {"Over": "Over", "Yes": "Over", "Under": "Under", "No": "Under"}.get(o.get("name"))
                if not player or not side or any(x in player.lower() for x in NOT_PLAYERS):
                    continue                              # team defenses and "no scorer" are not player legs
                key = (mk["key"], player, o.get("point"))
                groups.setdefault(key, {}).setdefault(bk["key"], {})[side] = o["price"]
    for (market, player, point), books in groups.items():
        probs, over_px, under_px = {}, {}, {}
        for b, sides in books.items():
            if "Over" in sides and "Under" in sides:
                probs[b] = devig(sides["Over"], sides["Under"])
            elif "Over" in sides and market == "player_anytime_td":
                probs[b] = td_fair(sides["Over"])
            elif "Over" in sides:
                continue                                  # one-sided over/under line: no honest fair price
            if "Over" in sides:
                over_px[b] = sides["Over"]
            if "Under" in sides:
                under_px[b] = sides["Under"]
        fair, nb, sharp = consensus(probs, robust=(market == "player_anytime_td"))
        if fair is None or not over_px:
            continue
        team, pos = roster.get(norm_name(player), (None, None))
        if team not in (home_abbr, away_abbr):
            team = None                                   # traded / stale roster: don't guess
        stat, mname = STAT_OF[market], MARKET_NAMES[market]
        pn = norm_name(player).replace(" ", "_")
        if market == "player_anytime_td":
            io, iu = f"{eid}:{market}:{pn}:yes", f"{eid}:{market}:{pn}:no"
            legs.append(leg(io, market, stat, "over", f"{player} Anytime TD", fair, nb, sharp, over_px, team=team, player=player, pos=pos, pair=iu))
            if under_px:
                legs.append(leg(iu, market, stat, "under", f"{player} No TD", round(1 - fair, 4), nb, sharp, under_px, team=team, player=player, pos=pos, pair=io))
        else:
            io, iu = f"{eid}:{market}:{pn}:{point:g}:over", f"{eid}:{market}:{pn}:{point:g}:under"
            legs.append(leg(io, market, stat, "over", f"{player} Over {point:g} {mname}", fair, nb, sharp, over_px, team=team, player=player, pos=pos, point=point, pair=iu))
            if under_px:
                legs.append(leg(iu, market, stat, "under", f"{player} Under {point:g} {mname}", round(1 - fair, 4), nb, sharp, under_px, team=team, player=player, pos=pos, point=point, pair=io))
    return legs


# ------------------------------------------------------------------ kalshi

def dollars(block, key: str):
    if block is None:
        return None
    v = block.get(f"{key}_dollars", block.get(key)) if isinstance(block, dict) else block
    if v in (None, ""):
        return None
    v = float(v)
    return v / 100 if v > 1.0 else v


def kalshi_open_markets(series: str) -> list:
    out, cursor = [], ""
    for _ in range(10):
        q = urllib.parse.urlencode({"series_ticker": series, "status": "open", "limit": 1000, **({"cursor": cursor} if cursor else {})})
        d = get(f"{KAPI}/markets?{q}")
        if not d:
            break
        out += d.get("markets", [])
        cursor = d.get("cursor") or ""
        if not cursor:
            break
    return out


def market_team(m: dict):
    tail = m.get("ticker", "").split("-")[-1]
    if 2 <= len(tail) <= 3 and tail.isalpha():
        return ALIAS.get(tail.upper(), tail.upper())
    return None


def market_date(m: dict):
    mt = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", m.get("ticker", ""))
    if mt:
        try:
            return dt.datetime.strptime(mt.group(1) + mt.group(2) + mt.group(3), "%y%b%d").date()
        except ValueError:
            pass
    return None


def attach_kalshi(games: list, series: str):
    ms = kalshi_open_markets(series)
    say(f"  Kalshi {series}: {len(ms)} open markets")
    hit = 0
    for g in games:
        kick = dt.datetime.strptime(g["commence"], "%Y-%m-%dT%H:%M:%SZ").date()
        for m in ms:
            team, d = market_team(m), market_date(m)
            if not team or not d or abs((d - kick).days) > 1 or team not in (g["home"], g["away"]):
                continue
            tick = m.get("ticker", "")
            if g["home"] not in tick or g["away"] not in tick:
                continue
            bid, ask = dollars(m, "yes_bid"), dollars(m, "yes_ask")
            if ask is None or not (0 < ask < 1):
                continue
            for lg in g["legs"]:
                if lg["m"] == "h2h" and lg["t"] == team:
                    lg["kx"] = {"bid": bid, "ask": ask, "ticker": tick}
                    hit += 1
    say(f"  matched {hit} Kalshi contracts to moneylines")


def kalshi_probe():
    d = get(f"{KAPI}/series?category=Sports") or {}
    found = [s for s in d.get("series", []) if "NFL" in (s.get("ticker", "") + s.get("title", "")).upper()]
    say(f"Kalshi series mentioning NFL: {len(found)}")
    for s in found:
        ms = kalshi_open_markets(s["ticker"])
        ex = ms[0] if ms else {}
        say(f"  {s['ticker']:<26} {str(s.get('title'))[:40]:<40} open={len(ms):4d}  e.g. {ex.get('ticker', '')}  | {str(ex.get('title', ''))[:60]}")


# ------------------------------------------------------------------ main run

def collect(cfg: dict, dry: bool = False) -> dict:
    key = cfg.get("ODDS_API_KEY", "")
    if not key:
        raise RuntimeError("No ODDS_API_KEY (put it in .env here or in ..\\props-test\\.env)")
    now = dt.datetime.now(dt.timezone.utc)
    cap, reserve, days = int(cfg["RUN_CAP"]), int(cfg["RESERVE"]), int(cfg["DAYS"])
    props = [m for m in cfg["PROP_MARKETS"].split(",") if m.strip()]
    pregions = [r for r in cfg["PROP_REGIONS"].split(",") if r.strip()]
    books = [b for b in cfg.get("BOOKS", "").split(",") if b.strip()]
    if books:                                             # named books: every 10 count as one region
        scope = {"bookmakers": ",".join(books)}
        pscope, units = scope, -(-len(books) // 10)
        say(f"books: {', '.join(BOOK_NAMES.get(b, b) for b in books)}")
    else:
        scope, pscope, units = {"regions": ",".join(FEATURED_REGIONS)}, {"regions": ",".join(pregions)}, None
    per_event = len(props) * (units or len(pregions))
    featured_cost = len(FEATURED) * (units or len(FEATURED_REGIONS))
    say(f"budget: {cap} credits this run, keep {reserve} in reserve; game lines cost {featured_cost}, props up to {per_event} per game")
    if dry:
        say("dry run: no requests made")
        return {}
    roster = load_roster(now)
    say(f"  roster: {len(roster)} players")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    write_roster_json(roster)
    events = get(odds_url(f"/sports/{SPORT}/odds", key, markets=",".join(FEATURED), **scope), odds_api=True) or []
    say(f"  game lines: {len(events)} events  (credits used {STATE['used']}, left {STATE['left']})")
    games, skipped = [], 0
    upcoming = sorted((e for e in events if now.strftime("%Y-%m-%dT%H:%M:%SZ") < e["commence_time"] <= (now + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")),
                      key=lambda e: e["commence_time"])
    for ev in upcoming:
        H, A = ABBR.get(ev["home_team"], ev["home_team"][:3].upper()), ABBR.get(ev["away_team"], ev["away_team"][:3].upper())
        g = {"id": ev["id"], "commence": ev["commence_time"], "home": H, "away": A, "home_name": ev["home_team"],
             "away_name": ev["away_team"], "legs": featured_legs(ev), "props": False}
        can = STATE["spent"] + per_event <= cap and (STATE["left"] is None or STATE["left"] - per_event >= reserve)
        if props and can:
            d = get(odds_url(f"/sports/{SPORT}/events/{ev['id']}/odds", key, markets=",".join(props), **pscope), odds_api=True)
            if d:
                g["legs"] += prop_legs(d, roster, H, A)
                g["props"] = True
        elif props:
            skipped += 1
        say(f"  {A}@{H} {ev['commence_time'][:16]}Z: {len(g['legs'])} legs{' (props skipped: budget)' if props and not g['props'] else ''}")
        games.append(g)
    try:
        attach_kalshi(games, cfg["KALSHI_SERIES"])
    except Exception as e:                               # noqa: BLE001
        say(f"  Kalshi unavailable ({e})")
    seen = {b for g in games for lg in g["legs"] for b in lg["px"]}
    order = [b for b in books if b in seen] + sorted(seen - set(books))
    return {"generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sport": "NFL", "games": games,
            "books": {b: BOOK_NAMES.get(b, b.replace("_", " ").title()) for b in order + ["kalshi"]},
            "book_order": order, "sharp": list(SHARP),
            "markets": MARKET_NAMES, "prop_markets": props, "props_skipped": skipped,
            "credits": {"used": STATE["used"], "remaining": STATE["left"], "spent": STATE["spent"]}}


def write_roster_json(roster: dict):
    """docs/data/roster.json: player -> [team, position], read by the app when it builds legs from the live feed."""
    p = OUT.parent / "roster.json"
    p.write_text(json.dumps({k: list(v) for k, v in sorted(roster.items())}, separators=(",", ":")), encoding="utf-8")
    say(f"  roster -> {p.relative_to(HERE)} ({len(roster)} players)")


def write_demo_feed(raw: dict):
    """docs/data/feed_demo.json: what the live worker's /feed returns, built from the demo prices (for testing)."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    feed = {"generated": now, "credits": {"remaining": 0, "spent_today": 0}, "props_at": {k: int(time.time() * 1000) for k in raw["props"]},
            "books": ["draftkings", "fanduel", "betmgm", "williamhill_us", "fanatics", "pinnacle"],
            "lines": raw["lines"], "props": raw["props"], "kalshi": {"markets": raw["kalshi"]}}
    p = OUT.parent / "feed_demo.json"
    p.write_text(json.dumps(feed, separators=(",", ":")), encoding="utf-8")
    (OUT.parent / "roster.json").write_text(json.dumps(raw["roster"], separators=(",", ":")), encoding="utf-8")
    say(f"  demo feed -> {p.relative_to(HERE)}")


def write(payload: dict):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    raw = payload.pop("_raw", None)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    if raw:
        write_demo_feed(raw)
    n = sum(len(g["legs"]) for g in payload["games"])
    say(f"\n{len(payload['games'])} games, {n:,} legs, {len(payload['books'])} price sources -> {OUT.relative_to(HERE)} ({OUT.stat().st_size // 1024} KB)")


# ------------------------------------------------------------------ demo data (no key needed)

def demo_payload() -> dict:
    import random
    rnd = random.Random(7)
    now = dt.datetime.now(dt.timezone.utc)
    sunday = now + dt.timedelta(days=(6 - now.weekday()) % 7 or 7)
    matchups = [("Baltimore Ravens", "Atlanta Falcons", -6.5, 47.5), ("Kansas City Chiefs", "Buffalo Bills", -1.5, 50.5),
                ("Detroit Lions", "Green Bay Packers", -3.0, 48.5), ("Philadelphia Eagles", "Dallas Cowboys", -7.5, 44.5)]
    stars = {"BAL": [("Lamar Jackson", "QB"), ("Derrick Henry", "RB"), ("Zay Flowers", "WR"), ("Mark Andrews", "TE")],
             "ATL": [("Michael Penix Jr.", "QB"), ("Bijan Robinson", "RB"), ("Drake London", "WR"), ("Kyle Pitts", "TE")],
             "KC": [("Patrick Mahomes", "QB"), ("Isiah Pacheco", "RB"), ("Rashee Rice", "WR"), ("Travis Kelce", "TE")],
             "BUF": [("Josh Allen", "QB"), ("James Cook", "RB"), ("Khalil Shakir", "WR"), ("Dalton Kincaid", "TE")],
             "DET": [("Jared Goff", "QB"), ("Jahmyr Gibbs", "RB"), ("Amon-Ra St. Brown", "WR"), ("Sam LaPorta", "TE")],
             "GB": [("Jordan Love", "QB"), ("Josh Jacobs", "RB"), ("Jayden Reed", "WR"), ("Tucker Kraft", "TE")],
             "PHI": [("Jalen Hurts", "QB"), ("Saquon Barkley", "RB"), ("A.J. Brown", "WR"), ("Dallas Goedert", "TE")],
             "DAL": [("Dak Prescott", "QB"), ("Javonte Williams", "RB"), ("CeeDee Lamb", "WR"), ("Jake Ferguson", "TE")]}
    books = ["draftkings", "fanduel", "betmgm", "williamhill_us", "fanatics", "pinnacle"]

    def price_pair(p, hold=0.045):                       # two-sided prices around fair p with the book's hold
        o = {}
        for b in books:
            h = 0.02 if b == "pinnacle" else hold + rnd.uniform(-0.01, 0.015)
            q = min(max(p + rnd.gauss(0, 0.012), 0.03), 0.97)
            o[b] = (american(q * (1 + h)), american((1 - q) * (1 + h)))
        return o

    games = []
    raw = {"lines": [], "props": {}, "kalshi": [], "roster": {}}
    for i, (home, away, spread, total) in enumerate(matchups):
        H, A = ABBR[home], ABBR[away]
        eid = f"demo{i}"
        kick = (sunday.replace(hour=17, minute=0, second=0, microsecond=0) + dt.timedelta(hours=3 * (i % 2))).strftime("%Y-%m-%dT%H:%M:%SZ")
        ev = {"id": eid, "home_team": home, "away_team": away, "commence_time": kick, "bookmakers": []}
        p_home = 1 / (1 + math.exp(-spread * -0.17))
        ml, sp, tot = price_pair(p_home, 0.04), price_pair(0.5), price_pair(0.5)
        for b in books:
            ev["bookmakers"].append({"key": b, "markets": [
                {"key": "h2h", "outcomes": [{"name": home, "price": ml[b][0]}, {"name": away, "price": ml[b][1]}]},
                {"key": "spreads", "outcomes": [{"name": home, "price": sp[b][0], "point": spread}, {"name": away, "price": sp[b][1], "point": -spread}]},
                {"key": "totals", "outcomes": [{"name": "Over", "price": tot[b][0], "point": total}, {"name": "Under", "price": tot[b][1], "point": total}]}]})
        pev = {"id": eid, "home_team": home, "away_team": away, "bookmakers": [{"key": b, "markets": []} for b in books if b != "pinnacle"]}
        lines = {"QB": [("player_pass_yds", 245.5), ("player_pass_tds", 1.5)], "RB": [("player_rush_yds", 72.5), ("player_receptions", 2.5)],
                 "WR": [("player_reception_yds", 68.5), ("player_receptions", 5.5)], "TE": [("player_reception_yds", 44.5), ("player_receptions", 4.5)]}
        roster = {}
        for T in (H, A):
            for name, pos in stars[T]:
                roster[norm_name(name)] = (T, pos)
                for mk, ln in lines[pos]:
                    pr = price_pair(0.5 + rnd.uniform(-0.04, 0.04), 0.06)
                    for bm in pev["bookmakers"]:
                        bm["markets"].append({"key": mk, "outcomes": [{"name": "Over", "description": name, "price": pr[bm["key"]][0], "point": ln},
                                                                      {"name": "Under", "description": name, "price": pr[bm["key"]][1], "point": ln}]})
                if pos != "QB":
                    ptd = {"RB": 0.5, "WR": 0.4, "TE": 0.3}[pos] + rnd.uniform(-0.1, 0.1)
                    for bm in pev["bookmakers"]:
                        q = min(max(ptd + rnd.gauss(0, 0.02), 0.05), 0.9) * 1.1
                        bm["markets"].append({"key": "player_anytime_td", "outcomes": [{"name": "Yes", "description": name, "price": american(q)}]})
        g = {"id": eid, "commence": kick, "home": H, "away": A, "home_name": home, "away_name": away, "props": True,
             "legs": featured_legs(ev) + prop_legs(pev, roster, H, A)}
        kday = dt.datetime.strptime(kick, "%Y-%m-%dT%H:%M:%SZ").strftime("%y%b%d").upper()
        for lg in g["legs"]:
            if lg["m"] == "h2h":
                lg["kx"] = {"bid": round(lg["fair"] - 0.01, 2), "ask": round(lg["fair"] + 0.005, 2), "ticker": f"KXNFLGAME-{kday}{A}{H}-{lg['t']}"}
                raw["kalshi"].append({"ticker": lg["kx"]["ticker"], "yes_bid_dollars": f"{lg['kx']['bid']:.4f}", "yes_ask_dollars": f"{lg['kx']['ask']:.4f}",
                                      "title": f"{home if lg['t'] == H else away} wins?"})
        games.append(g)
        raw["lines"].append(ev)
        raw["props"][eid] = pev
        raw["roster"].update({k: list(v) for k, v in roster.items()})
    allb = books + ["kalshi"]
    return {"generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sport": "NFL", "games": games, "demo": True,
            "books": {b: BOOK_NAMES[b] for b in allb}, "book_order": books, "sharp": list(SHARP), "markets": MARKET_NAMES, "prop_markets": DEFAULT_PROPS.split(","),
            "props_skipped": 0, "credits": {"used": 0, "remaining": 0, "spent": 0}, "_raw": raw}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--kalshi-probe", action="store_true")
    args = ap.parse_args()
    if args.kalshi_probe:
        kalshi_probe()
        return
    if args.demo:
        write(demo_payload())
        return
    cfg = load_env()
    payload = collect(cfg, dry=args.dry_run)
    if payload:
        write(payload)
        say(f"credits: spent {STATE['spent']} this run, {STATE['left']} remaining"
            + (f"; props skipped for {payload['props_skipped']} games (raise RUN_CAP or lower RESERVE)" if payload["props_skipped"] else ""))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        say(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(1)
