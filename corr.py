"""How do NFL parlay legs move together?  Estimates the correlations the app uses to price same-game parlays.

    py corr.py            # downloads nflverse data if needed, writes docs\\data\\corr.json

Method. For every player-game since 2023 (weeks 4+, so each player has at least three prior games), the
"surprise" in each stat is actual minus the player's exponentially weighted average of his earlier games
this season, scaled by how noisy that stat is for a player of that size. For the game itself the surprise
is final margin minus the closing spread and final total minus the closing total line. Surprises are
rank-transformed to normal scores, and the correlation between two normal scores is exactly the number a
Gaussian copula needs. Binary legs (anytime touchdown) use the polyserial correction so the estimate is
the latent correlation, not the attenuated point-biserial one.

Pairs are grouped by relationship ("QB passing yards vs WR receiving yards, same team"), so the app can
look up any two legs in a game: same player, same team, opposite teams, player vs own-team margin, player
vs game total, margin vs total.  Every number comes with its sample size; the app ignores anything thin.
"""
import json
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm, rankdata

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = HERE / "docs" / "data" / "corr.json"
SEASONS = [2023, 2024, 2025, 2026]
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
HALF_LIFE = 4                  # games
MIN_PRIOR = 3                  # prior games before a player-game counts
MIN_N = 150                    # pairs below this many observations are not trusted
STATS = {                      # leg stat -> nflverse column(s)
    "pass_yds": "passing_yards", "pass_tds": "passing_tds", "rush_yds": "rushing_yards",
    "receptions": "receptions", "reception_yds": "receiving_yards",
}
USAGE = {"QB": ("attempts", 12), "RB": ("carries", 5), "WR": ("targets", 2.5), "TE": ("targets", 2.5)}
POS = {"QB", "RB", "WR", "TE"}


def say(m):
    print(m, flush=True)


def download(name: str, dest: Path):
    if dest.exists():
        return
    say(f"  downloading {name}")
    urllib.request.urlretrieve(f"{NFLVERSE}/{name}", dest)


def load():
    DATA.mkdir(exist_ok=True)
    frames = []
    for y in SEASONS:
        p = DATA / f"stats_player_week_{y}.csv"
        try:
            download(f"stats_player/stats_player_week_{y}.csv", p)
        except Exception as e:                            # noqa: BLE001  (future season not published yet)
            say(f"  {y}: {e}")
            continue
        frames.append(pd.read_csv(p, low_memory=False))
    download("schedules/games.csv", DATA / "games.csv")
    s = pd.concat(frames, ignore_index=True)
    s = s[(s.season_type == "REG") & s.position.isin(POS)].copy()
    s["td"] = ((s.rushing_tds.fillna(0) + s.receiving_tds.fillna(0)) > 0).astype(int)
    g = pd.read_csv(DATA / "games.csv")
    g = g[g.season.isin(SEASONS) & g.result.notna()].copy()
    return s, g


def ew_prior(values: np.ndarray) -> np.ndarray:
    """Exponentially weighted mean of the values strictly before each index (nan until MIN_PRIOR games)."""
    out = np.full(len(values), np.nan)
    decay = 0.5 ** (1 / HALF_LIFE)
    num = den = 0.0
    for i, v in enumerate(values):
        if i >= MIN_PRIOR:
            out[i] = num / den
        num = num * decay + v
        den = den * decay + 1
    return out


def surprises(s: pd.DataFrame) -> pd.DataFrame:
    """One row per player-game with the normal-score surprise of each stat (nan where the player wasn't used enough)."""
    s = s.sort_values(["player_id", "season", "week"])
    cols = list(STATS.values()) + ["td"]
    for c in cols + [u for u, _ in USAGE.values()]:
        s[c] = s[c].fillna(0.0).astype(float)
    exp = {c: np.full(len(s), np.nan) for c in cols + ["usage"]}
    idx = 0
    for (_, _), grp in s.groupby(["player_id", "season"], sort=False):
        n = len(grp)
        pos = grp.position.iloc[0]
        ucol = USAGE[pos][0]
        for c in cols:
            exp[c][idx:idx + n] = ew_prior(grp[c].to_numpy())
        exp["usage"][idx:idx + n] = ew_prior(grp[ucol].to_numpy())
        idx += n
    out = s[["player_id", "player_display_name", "position", "season", "week", "game_id", "team", "opponent_team"]].copy()
    thresholds = s.position.map(lambda p: USAGE[p][1]).to_numpy()
    used = exp["usage"] >= thresholds
    for stat, col in STATS.items():
        mu = exp[col]
        resid = s[col].to_numpy() - mu
        ok = used & ~np.isnan(mu) & (mu > 0)
        # noise grows with the player's size: scale residuals by a fitted power of the expectation
        z = np.full(len(s), np.nan)
        if ok.sum() > 500:
            lm, lr = np.log(mu[ok]), np.log(np.abs(resid[ok]) + 1e-6)
            b = np.polyfit(lm, lr, 1)
            scale = np.exp(np.polyval(b, np.log(np.maximum(mu, 1e-3))))
            z[ok] = resid[ok] / scale[ok]
            z[ok] = norm.ppf((rankdata(z[ok]) - 0.5) / ok.sum())      # normal scores
        out[stat] = z
    p_td = exp["td"]
    ok = used & ~np.isnan(p_td)
    out["td"] = np.where(ok, s.td.to_numpy(), np.nan)                 # binary, handled separately
    out["td_p"] = np.where(ok, np.clip(p_td, 0.02, 0.98), np.nan)
    return out.reset_index(drop=True)


def game_surprises(g: pd.DataFrame) -> pd.DataFrame:
    g = g[g.spread_line.notna() & g.total_line.notna()].copy()
    m = (g.result - g.spread_line).to_numpy(float)               # home margin vs the closing spread
    t = (g.total - g.total_line).to_numpy(float)
    out = pd.DataFrame({"game_id": g.game_id.values, "home_team": g.home_team.values, "away_team": g.away_team.values})
    out["margin_home"] = norm.ppf((rankdata(m) - 0.5) / len(m))
    out["total"] = norm.ppf((rankdata(t) - 0.5) / len(t))
    sd = {"margin": float(np.std(m)), "total": float(np.std(t))}
    return out, sd


def corr(a: np.ndarray, b: np.ndarray):
    ok = ~np.isnan(a) & ~np.isnan(b)
    if ok.sum() < 30:
        return None, int(ok.sum())
    return float(np.corrcoef(a[ok], b[ok])[0, 1]), int(ok.sum())


def polyserial(y: np.ndarray, p: np.ndarray, z: np.ndarray):
    """Latent correlation between a binary outcome (probability p each) and a normal score z."""
    ok = ~np.isnan(y) & ~np.isnan(z) & ~np.isnan(p)
    if ok.sum() < 30:
        return None, int(ok.sum())
    y, p, z = y[ok], p[ok], z[ok]
    r = np.corrcoef((y - p) / np.sqrt(p * (1 - p)), z)[0, 1]
    pbar = float(p.mean())
    factor = np.sqrt(pbar * (1 - pbar)) / norm.pdf(norm.ppf(1 - pbar))
    return float(np.clip(r * factor, -0.98, 0.98)), int(ok.sum())


def tetrachoric(y1, p1, y2, p2):
    ok = ~np.isnan(y1) & ~np.isnan(y2) & ~np.isnan(p1) & ~np.isnan(p2)
    if ok.sum() < 30:
        return None, int(ok.sum())
    y1, p1, y2, p2 = y1[ok], p1[ok], y2[ok], p2[ok]
    r = np.corrcoef((y1 - p1) / np.sqrt(p1 * (1 - p1)), (y2 - p2) / np.sqrt(p2 * (1 - p2)))[0, 1]
    f1 = np.sqrt(p1.mean() * (1 - p1.mean())) / norm.pdf(norm.ppf(1 - p1.mean()))
    f2 = np.sqrt(p2.mean() * (1 - p2.mean())) / norm.pdf(norm.ppf(1 - p2.mean()))
    return float(np.clip(r * f1 * f2, -0.98, 0.98)), int(ok.sum())


def pair_stat(df: pd.DataFrame, sa: str, sb: str, suf=("_a", "_b")):
    """Correlation between stat sa of player a and stat sb of player b in a frame of paired player-games."""
    a, b = f"{sa}{suf[0]}", f"{sb}{suf[1]}"
    if sa == "anytime_td" and sb == "anytime_td":
        return tetrachoric(df[f"td{suf[0]}"].to_numpy(float), df[f"td_p{suf[0]}"].to_numpy(float),
                           df[f"td{suf[1]}"].to_numpy(float), df[f"td_p{suf[1]}"].to_numpy(float))
    if sa == "anytime_td":
        return polyserial(df[f"td{suf[0]}"].to_numpy(float), df[f"td_p{suf[0]}"].to_numpy(float), df[b].to_numpy(float))
    if sb == "anytime_td":
        return polyserial(df[f"td{suf[1]}"].to_numpy(float), df[f"td_p{suf[1]}"].to_numpy(float), df[a].to_numpy(float))
    return corr(df[a].to_numpy(float), df[b].to_numpy(float))


def main():
    say("loading nflverse data")
    s, g = load()
    say(f"  {len(s):,} player-games, {len(g):,} games")
    ps = surprises(s)
    gs, sd = game_surprises(g)
    ps = ps.merge(gs, on="game_id", how="inner")
    ps["margin"] = np.where(ps.team == ps.home_team, ps.margin_home, -ps.margin_home)   # own-team margin surprise
    stats = list(STATS) + ["anytime_td"]
    pairs = {}

    def put(key, val):
        rho, n = val
        if rho is not None:
            pairs[key] = {"rho": round(rho, 3), "n": n}

    # 1) a player's stats vs his own team's margin and the game total
    for pos in sorted(POS):
        sub = ps[ps.position == pos]
        for st in stats:
            put(f"{pos}:{st}|margin", pair_stat(sub.assign(**{f"{st}_a": sub.get(st), "td_a": sub.td, "td_p_a": sub.td_p, "margin_b": sub.margin}), st, "margin"))
            put(f"{pos}:{st}|total", pair_stat(sub.assign(**{f"{st}_a": sub.get(st), "td_a": sub.td, "td_p_a": sub.td_p, "total_b": sub.total}), st, "total"))
    put("margin|total", corr(gs.margin_home.to_numpy(), gs.total.to_numpy()))        # home margin vs total

    # 2) same player, two different stats
    for i, sa in enumerate(stats):
        for sb in stats[i + 1:]:
            for pos in sorted(POS):
                sub = ps[ps.position == pos]
                df = sub.assign(**{f"{sa}_a": sub.get(sa), f"{sb}_b": sub.get(sb), "td_a": sub.td, "td_p_a": sub.td_p, "td_b": sub.td, "td_p_b": sub.td_p})
                put(f"same:{pos}:{sa}|{sb}", pair_stat(df, sa, sb))

    # 3) two players: same team, and opposite teams
    cols = ["game_id", "team", "player_id", "position"] + stats[:-1] + ["td", "td_p"]
    a = ps[cols].rename(columns={c: f"{c}_a" for c in cols if c not in ("game_id",)})
    b = ps[cols].rename(columns={c: f"{c}_b" for c in cols if c not in ("game_id",)})
    both = a.merge(b, on="game_id")
    both = both[both.player_id_a < both.player_id_b]
    # canonical order: the player with the alphabetically earlier position is "a" (QB < RB < TE < WR)
    flip = both.position_a > both.position_b
    acols = [c for c in both.columns if c.endswith("_a")]
    swapped = both.copy()
    for c in acols:
        swapped.loc[flip, c], swapped.loc[flip, c[:-2] + "_b"] = both.loc[flip, c[:-2] + "_b"].values, both.loc[flip, c].values
    both = swapped
    same = both[both.team_a == both.team_b]
    opp = both[both.team_a != both.team_b]
    for label, df in (("team", same), ("opp", opp)):
        for pa in sorted(POS):
            for pb in sorted(POS):
                if pa > pb:
                    continue
                sub = df[(df.position_a == pa) & (df.position_b == pb)]
                if len(sub) < 200:
                    continue
                for sa in stats:
                    for sb in stats:
                        if pa == pb and sa > sb:
                            continue                      # same position: stats in alphabetical order
                        put(f"{label}:{pa}:{sa}|{pb}:{sb}", pair_stat(sub, sa, sb))
    trusted = {k: v for k, v in pairs.items() if v["n"] >= MIN_N}
    out = {"generated": pd.Timestamp.now("UTC").strftime("%Y-%m-%dT%H:%MZ"), "seasons": SEASONS, "min_n": MIN_N,
           "sd": {k: round(v, 2) for k, v in sd.items()}, "pairs": trusted}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    say(f"\n{len(trusted)} relationship correlations -> {OUT.relative_to(HERE)}")
    show = ["QB:pass_yds|total", "QB:pass_yds|margin", "RB:rush_yds|margin", "WR:reception_yds|total", "margin|total",
            "same:WR:receptions|reception_yds", "same:WR:reception_yds|anytime_td", "same:QB:pass_yds|pass_tds",
            "team:QB:pass_yds|WR:reception_yds", "team:QB:pass_yds|RB:rush_yds", "team:WR:reception_yds|WR:reception_yds",
            "team:QB:pass_tds|WR:anytime_td", "opp:QB:pass_yds|QB:pass_yds", "opp:RB:rush_yds|RB:rush_yds", "team:RB:anytime_td|WR:anytime_td"]
    for k in show:
        v = pairs.get(k)
        say(f"  {k:<42} rho {v['rho']:+.3f}  n={v['n']:,}" if v else f"  {k:<42} (not enough data)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
