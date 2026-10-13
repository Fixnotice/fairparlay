// node tests/worker_test.mjs   — exercises worker.js with an in-memory KV and a stubbed Odds API
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import worker from "../worker.js";

const demo = JSON.parse(readFileSync(new URL("../docs/data/feed_demo.json", import.meta.url)));
// make the demo games "near": first two kick off in 2 hours, the rest in 2 days
const now = Date.now();
demo.lines.forEach((e, i) => { e.commence_time = new Date(now + (i < 2 ? 2 : 48) * 3600e3).toISOString().replace(/\.\d{3}Z$/, "Z"); });

let credits = 5000, calls = [];
globalThis.fetch = async (url) => {
  url = String(url); calls.push(url.replace(/apiKey=[^&]+/, "apiKey=*"));
  const hdr = (last) => { credits -= last; return { "x-requests-remaining": String(credits), "x-requests-last": String(last) }; };
  if (url.includes("/sports/americanfootball_nfl/odds?")) return new Response(JSON.stringify(demo.lines), { headers: hdr(3) });
  const m = url.match(/events\/([^/]+)\/odds/);
  if (m) return demo.props[m[1]] ? new Response(JSON.stringify(demo.props[m[1]]), { headers: hdr(6) }) : new Response("no event", { status: 404 });
  if (url.includes("kalshi")) return new Response(JSON.stringify(demo.kalshi));
  throw new Error("unexpected url " + url);
};

const kv = new Map(); let writes = 0;
const env = { FEED: { get: async (k) => kv.get(k) ?? null, put: async (k, v) => { writes++; kv.set(k, v); } },
              ODDS_API_KEY: "test", RUN_KEY: "k", PROPS_PER_TICK: "2", LINES_MIN_NEAR: "8" };
const run = () => worker.fetch(new Request("https://w/run?key=k"), env).then(r => r.json());
const feed = () => worker.fetch(new Request("https://w/feed"), env).then(r => r.json());

// tick 1: lines + 2 games' props + kalshi
let log = await run();
assert.equal(log.near, true);
assert.match(log.did[0], /lines: 4 events \(3 credits\)/);
assert.equal(log.did.filter(d => d.startsWith("props demo")).length, 2);
assert.ok(log.did.includes("kalshi"));
assert.equal(writes, 2, "one state write + one props write per tick");
let f = await feed();
assert.equal(f.lines.length, 4);
assert.deepEqual(Object.keys(f.props).sort(), ["demo0", "demo1"]);
assert.equal(f.kalshi.markets.length, 8);
assert.equal(f.credits.spent_today, 15);

// tick 2 right away: lines not due (8 min), the other two games get props
log = await run();
assert.ok(!log.did.some(d => d.startsWith("lines")), "lines pulled again too soon");
assert.deepEqual(log.did.filter(d => d.startsWith("props demo")).map(d => d.split(" ")[1]), ["demo2", "demo3"]);
f = await feed();
assert.equal(Object.keys(f.props).length, 4);
assert.equal(JSON.parse(JSON.stringify(f)).props.demo3.bookmakers.length, 5);

// tick 3: nothing to do -> no writes
const w = writes; log = await run();
assert.equal(writes, w, "idle tick must not write");
assert.equal(log.did.length, 0);

// budget: tiny cap -> lines skipped, props skipped
const env2 = { ...env, DAILY_CAP: "10", LINES_MIN_NEAR: "0" };
kv.set("state", JSON.stringify({ ...JSON.parse(kv.get("state")), spentLines: 6, spentProps: 4, propsAt: {} }));
log = await worker.fetch(new Request("https://w/run?key=k"), env2).then(r => r.json());
assert.ok(log.did.includes("lines skipped: budget"), log.did);
assert.ok(log.did.includes("props skipped: budget"), log.did);

// reserve floor: credits almost gone
credits = 90;
kv.set("state", JSON.stringify({ ...JSON.parse(kv.get("state")), left: 90, spentLines: 0, spentProps: 0, linesAt: 0, propsAt: {} }));
log = await run();
assert.ok(log.did.includes("lines skipped: budget"));

// /status hides the big parts; /run without key is forbidden
const st = await worker.fetch(new Request("https://w/status"), env).then(r => r.json());
assert.ok(!("lines" in st) && "events" in st);
assert.equal((await worker.fetch(new Request("https://w/run"), env)).status, 403);
console.log(`worker ok: ${calls.length} API calls, ${writes} KV writes, feed ${JSON.stringify(f).length} bytes`);
