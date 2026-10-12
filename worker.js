/**
 * FairParlay live feed — a Cloudflare Worker (free plan) that keeps this week's NFL prices fresh.
 *
 * Every 5 minutes (cron) it decides what is worth pulling from The Odds API inside the day's credit budget:
 * game lines for the whole slate (3 credits) every LINES_MIN_NEAR minutes when a kickoff is close, every
 * LINES_MIN_FAR otherwise; player props game by game (up to 6 credits each), oldest first, refreshed every
 * PROPS_TTL_NEAR / PROPS_TTL_FAR minutes; and Kalshi's game contracts (free) every 15 minutes. The raw API
 * responses are stored in KV as text and handed to the app by GET /feed in one JSON document — the app does
 * the de-vigging and parlay math — so nothing here parses the big responses and it stays far inside the free
 * plan's CPU limit, with about two KV writes per tick (free plan: 1,000 writes a day).
 *
 * Setup is in README.md ("Live prices"). Bindings this worker expects:
 *   KV namespace  FEED
 *   secret        ODDS_API_KEY
 *   vars (all optional): BOOKS, PROP_MARKETS, DAYS, DAILY_CAP, RESERVE, LINES_MIN_NEAR, LINES_MIN_FAR,
 *                        PROPS_TTL_NEAR, PROPS_TTL_FAR, PROPS_PER_TICK, KALSHI_SERIES, RUN_KEY
 * Cron trigger:  every 5 minutes  ( *\/5 * * * * )
 */

const OAPI = "https://api.the-odds-api.com/v4";
const KAPI = "https://api.elections.kalshi.com/trade-api/v2";
const SPORT = "americanfootball_nfl";
const FEATURED = "h2h,spreads,totals";
const DEFAULTS = {
  BOOKS: "draftkings,fanduel,betmgm,williamhill_us,fanatics,betrivers,hardrockbet,ballybet,espnbet,pinnacle",
  PROP_MARKETS: "player_pass_yds,player_pass_tds,player_rush_yds,player_receptions,player_reception_yds,player_anytime_td",
  DAYS: "8",              // how far ahead to track games
  DAILY_CAP: "650",       // credits per UTC day (the 20,000/month plan is ~650 a day); 60% lines, 40% props
  RESERVE: "100",         // never go below this many credits remaining on the plan
  LINES_MIN_NEAR: "8",    // minutes between game-line pulls when a kickoff is within 6 hours (or a game is on)
  LINES_MIN_FAR: "20",    // ... otherwise
  PROPS_TTL_NEAR: "90",   // minutes before a game's props are pulled again, near kickoff
  PROPS_TTL_FAR: "240",   // ... otherwise
  PROPS_PER_TICK: "2",    // games whose props are refreshed per tick
  KALSHI_SERIES: "KXNFLGAME",
};
const cfg = (env, k) => (env[k] !== undefined && env[k] !== "" ? String(env[k]) : DEFAULTS[k]);
const MIN = 60e3, HOUR = 3600e3;

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS" };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (url.pathname === "/feed" || url.pathname === "/") return feed(env, cors);
    if (url.pathname === "/status") {
      const st = await state(env);
      delete st.lines; delete st.kalshi;
      return new Response(JSON.stringify(st, null, 1), { headers: { ...cors, "Content-Type": "application/json", "Cache-Control": "no-store" } });
    }
    if (url.pathname === "/run") {                          // manual tick for testing: /run?key=RUN_KEY
      if (!env.RUN_KEY || url.searchParams.get("key") !== env.RUN_KEY) return new Response("forbidden", { status: 403 });
      return new Response(JSON.stringify(await tick(env), null, 1), { headers: { ...cors, "Content-Type": "application/json" } });
    }
    return new Response("FairParlay feed: /feed, /status", { headers: cors });
  },
};

// ---------------------------------------------------------------- storage: "state" (meta + lines + kalshi) and "props"

async function state(env) {
  const st = JSON.parse((await env.FEED.get("state")) || "{}");
  st.events = st.events || [];
  st.propsAt = st.propsAt || {};
  return st;
}

// props are kept as one text value: entries '"<event id>":<raw response>' joined by newlines, so an entry
// can be replaced with a split/join and never parsed here
function propsMap(text) {
  const out = {};
  for (const line of (text || "").split("\n")) if (line) out[line.slice(1, line.indexOf('"', 1))] = line;
  return out;
}

// ---------------------------------------------------------------- the scheduled pull

async function tick(env) {
  const now = Date.now();
  const today = new Date(now).toISOString().slice(0, 10);
  const st = await state(env);
  if (st.day !== today) { st.day = today; st.spentLines = 0; st.spentProps = 0; }
  const log = { at: new Date(now).toISOString(), did: [] };
  const key = env.ODDS_API_KEY;
  if (!key) { st.error = "no ODDS_API_KEY secret"; await env.FEED.put("state", JSON.stringify(st)); return { ...log, error: st.error }; }
  const cap = +cfg(env, "DAILY_CAP"), reserve = +cfg(env, "RESERVE");
  const capLines = Math.round(cap * 0.6), capProps = cap - capLines;
  const books = cfg(env, "BOOKS"), props = cfg(env, "PROP_MARKETS"), nMarkets = props.split(",").length;
  const horizon = now + +cfg(env, "DAYS") * 86400e3;
  const isNear = () => st.events.some(e => e.t - now < 6 * HOUR && now - e.t < 4 * HOUR);
  const linesEvery = +cfg(env, isNear() ? "LINES_MIN_NEAR" : "LINES_MIN_FAR") * MIN;
  const room = (spent, c, cost) => spent + cost <= c && (st.left === undefined || st.left - cost >= reserve);
  let changed = false;

  // 1) game lines for the whole slate (3 credits for up to ten named books)
  if (!st.linesAt || now - st.linesAt >= linesEvery - 30e3) {
    if (room(st.spentLines, capLines, 3)) {
      const r = await odds(`${OAPI}/sports/${SPORT}/odds?apiKey=${key}&bookmakers=${books}&markets=${FEATURED}&oddsFormat=american`);
      if (r.ok) {
        st.lines = r.text; st.linesAt = now; st.left = r.left; st.spentLines += r.last; changed = true;
        st.events = JSON.parse(r.text).map(e => ({ id: e.id, t: Date.parse(e.commence_time) }));
        log.did.push(`lines: ${st.events.length} events (${r.last} credits)`);
      } else {
        st.error = r.error; log.did.push(`lines failed: ${r.error}`);
        if ([401, 403, 429].includes(r.status)) { await env.FEED.put("state", JSON.stringify(st)); return log; }
      }
    } else log.did.push("lines skipped: budget");
  }

  // 2) props: oldest first, among games not yet finished and inside the horizon
  const near = isNear();
  const ttl = +cfg(env, near ? "PROPS_TTL_NEAR" : "PROPS_TTL_FAR") * MIN;
  const live = st.events.filter(e => e.t > now - 4 * HOUR && e.t < horizon);
  const stale = live.filter(e => !st.propsAt[e.id] || now - st.propsAt[e.id] > ttl)
                    .sort((a, b) => (st.propsAt[a.id] || 0) - (st.propsAt[b.id] || 0) || a.t - b.t)
                    .slice(0, +cfg(env, "PROPS_PER_TICK"));
  if (stale.length) {
    const map = propsMap(await env.FEED.get("props"));
    let touched = false;
    for (const e of stale) {
      if (!room(st.spentProps, capProps, nMarkets)) { log.did.push("props skipped: budget"); break; }
      const r = await odds(`${OAPI}/sports/${SPORT}/events/${e.id}/odds?apiKey=${key}&bookmakers=${books}&markets=${props}&oddsFormat=american`);
      if (r.ok) {
        map[e.id] = `${JSON.stringify(e.id)}:${r.text.replace(/\n/g, "")}`;
        st.propsAt[e.id] = now; st.left = r.left; st.spentProps += r.last; touched = true;
        log.did.push(`props ${e.id} (${r.last} credits)`);
      } else {
        st.propsAt[e.id] = now - ttl / 2;                     // don't hammer a failing event
        log.did.push(`props ${e.id} failed: ${r.error}`);
      }
    }
    for (const id of Object.keys(map)) if (!live.some(e => e.id === id)) { delete map[id]; touched = true; }
    if (touched) { await env.FEED.put("props", Object.values(map).join("\n")); changed = true; }
  }
  for (const id of Object.keys(st.propsAt)) if (!st.events.some(e => e.id === id)) delete st.propsAt[id];

  // 3) Kalshi game contracts (free)
  if (!st.kalshiAt || now - st.kalshiAt > 15 * MIN) {
    try {
      const r = await fetch(`${KAPI}/markets?series_ticker=${cfg(env, "KALSHI_SERIES")}&status=open&limit=1000`, { headers: { Accept: "application/json" } });
      if (r.ok) { st.kalshi = await r.text(); st.kalshiAt = now; changed = true; log.did.push("kalshi"); }
    } catch (e) { log.did.push(`kalshi failed: ${e.message}`); }
  }
  st.tickAt = now; st.near = near; st.error = undefined;
  if (changed || !st.lines) await env.FEED.put("state", JSON.stringify(st));
  return { ...log, near, left: st.left, spentToday: st.spentLines + st.spentProps };
}

async function odds(url) {
  try {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    const text = await r.text();
    if (!r.ok) return { ok: false, status: r.status, error: `HTTP ${r.status} ${text.slice(0, 120)}` };
    return { ok: true, text, left: +(r.headers.get("x-requests-remaining") || 0), last: +(r.headers.get("x-requests-last") || 0) };
  } catch (e) {
    return { ok: false, status: 0, error: e.message };
  }
}

// ---------------------------------------------------------------- the feed (string assembly, no parsing of the big parts)

async function feed(env, cors) {
  const st = await state(env);
  const props = (await env.FEED.get("props")) || "";
  const body = `{"generated":${JSON.stringify(st.linesAt ? new Date(st.linesAt).toISOString() : null)},` +
    `"credits":{"remaining":${st.left ?? null},"spent_today":${(st.spentLines || 0) + (st.spentProps || 0)}},` +
    `"props_at":${JSON.stringify(st.propsAt)},"books":${JSON.stringify(cfg(env, "BOOKS").split(","))},` +
    `"lines":${st.lines || "[]"},"props":{${props.split("\n").filter(Boolean).join(",")}},"kalshi":${st.kalshi || "{}"}}`;
  return new Response(body, { headers: { ...cors, "Content-Type": "application/json", "Cache-Control": "public, max-age=30" } });
}
