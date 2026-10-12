# FairParlay — the true price of your parlay

A web app for NFL bettors. Pick legs from this week's slate; it shows the real probability of the
ticket (same-game correlation included), the fair payout, what every sportsbook would pay, and how much
each one keeps. It is a comparison tool, not a prediction tool: the fair prices come from the books
themselves with the vig stripped out, averaged with Pinnacle weighted as the sharp reference.

    collect.py      pulls this week's prices (The Odds API + Kalshi) -> docs\data\legs.json
    corr.py         estimates how legs move together, from every NFL game since 2023 -> docs\data\corr.json
    docs\index.html the app (static page; reads the two JSON files)
    github-workflow-collect.yml     the schedule that runs collect.py in GitHub's cloud (free, PC off);
                                    it lives at .github/workflows/collect.yml in the repo
    worker.js       optional: Cloudflare worker that keeps prices fresh every few minutes (see "Live prices")

## Try it now (no key needed)

    py collect.py --demo       writes a sample legs.json
    serve.bat                  opens http://localhost:8000/ with the app

## Go live on GitHub Pages (automated, free)

1. On github.com create a new **public** repository called `fairparlay` (same account as panelready).
2. Upload everything in this folder except `data\`, `.env` and `github-workflow-collect.yml` (drag the
   folder contents onto "uploading an existing file").
   Then "Add file -> Create new file", type the name `.github/workflows/collect.yml` exactly, paste in the
   contents of `github-workflow-collect.yml`, and commit.
3. Settings -> Secrets and variables -> Actions -> **New repository secret**: name `ODDS_API_KEY`,
   value = your key (type it there yourself; it never appears in the code).
4. Settings -> Pages -> Source "Deploy from a branch", branch `main`, folder **/docs** -> Save.
   The app is then at `https://<account>.github.io/fairparlay/` (same pattern as the PanelReady site).
5. Actions tab -> "Collect prices" -> **Run workflow** once to get the first real data. After that it runs
   itself Thu 6pm, Sat noon, Sun 9am & noon, Mon 5pm (ET), and refreshes correlations Tuesday mornings.

The bot commits `docs/data/legs.json` back to the repo after every run, which is what the page serves.
GitHub pauses scheduled runs after 60 days without a commit from a human; it emails first, and the
"Enable workflow" button turns it back on.

## Live prices (Cloudflare worker, free)

Without this, prices refresh when the GitHub workflow runs (a few times a week). With it, game lines refresh
every 8 minutes around kickoffs (20 otherwise), props every 90 minutes near kickoff, Kalshi every 15, and the
page re-pulls every 30 seconds, marks prices that moved with an arrow, and can watch a ticket for a target
payout and alert the browser when a book gets there. All inside The Odds API's 20,000/month plan (about 650
credits a day) and Cloudflare's free plan. The file is `worker.js`; everything is done in the Cloudflare
dashboard, no command line:

1. cloudflare.com -> Sign up (email only) -> **Workers & Pages** -> Create -> **Create Worker** ->
   name it `fairparlay-feed` -> Deploy. Then **Edit code**, delete the sample, paste the whole of `worker.js`,
   Deploy.
2. **Storage & Databases -> KV** -> Create a namespace called `fairparlay`. Back in the worker:
   Settings -> **Bindings** -> Add -> KV namespace -> variable name `FEED` -> pick `fairparlay` -> Deploy.
3. Settings -> **Variables and Secrets** -> Add -> type *Secret* -> name `ODDS_API_KEY` -> paste your key -> Deploy.
   (Optional: a secret `RUN_KEY` with any word lets you force a pull by opening `/run?key=thatword`.)
4. Settings -> **Triggers** -> Cron Triggers -> Add -> `*/5 * * * *` -> Save.
5. Open `https://fairparlay-feed.<your subdomain>.workers.dev/status` — after the first tick (within 5 minutes)
   it shows credits left and the events it is tracking; `/feed` is the data the app reads.
6. In `docs/index.html` set `const LIVE_URL="https://fairparlay-feed.<your subdomain>.workers.dev";` and commit.
   The page uses the worker when it answers and falls back to the GitHub copy when it doesn't.

Dials (worker Settings -> Variables): `DAILY_CAP` (650), `LINES_MIN_NEAR` (8) / `LINES_MIN_FAR` (20) minutes
between game-line pulls, `PROPS_TTL_NEAR` (90) / `PROPS_TTL_FAR` (240) minutes between a game's prop pulls,
`PROPS_PER_TICK` (2), `RESERVE` (100). Lines get 60% of the daily cap, props 40%. On a bigger Odds API plan,
raise the cap and lower the minutes. Test the worker logic locally with `node tests\worker_test.mjs`.

## Which books

`BOOKS` (default: DraftKings, FanDuel, BetMGM, Caesars, Fanatics, BetRivers, Hard Rock, Bally Bet, ESPN BET,
Pinnacle) is the list the collector asks for by name. The app shows the ones people know with their brand
colours; Pinnacle is only used to anchor the fair price (it is the sharpest book and is not open to US
customers), and anything else that turns up in the data stays hidden. Any ten named books count as one
region for credits, so this is cheaper than pulling whole regions. To add or drop a book, change `BOOKS`
in `.env` (locally) or as a repository variable (GitHub). Keys are The Odds API's bookmaker keys.

## Credits (The Odds API)

Game lines for the whole slate cost 3 credits per run (3 markets x one group of ten books). Player props are
fetched per game: up to 6 credits per game, and the API only charges for markets a book has actually posted,
so early in the week it is less. The schedule above costs well under 300 credits a week. On the free
500/month plan, set the repository variable `RUN_CAP` to 60 (Settings -> Secrets and variables -> Actions ->
Variables): the collector then pulls game lines for every game and props only for the earliest games until
the cap is hit, and the app marks the rest "props pending". `RESERVE` (default 40) is the floor it never
goes below.

Locally: `py collect.py --dry-run` shows the cost without calling anything; `run.bat` does a real pull
(reads `ODDS_API_KEY` from `.env` here, or from `..\props-test\.env`).

## How the numbers are made

- Fair probability: each two-way market is de-vigged book by book (proportional method), then averaged
  across books with Pinnacle counted three times. Spreads and totals at different lines are separate legs,
  each with the books that post that exact line. One-sided anytime-TD prices use
  logit(fair) = -0.221 + 0.899 x logit(implied), fitted on 3,863 historical DraftKings TD prices
  (the book kept ~9% on them).
- Correlation: `corr.py` turns every player-game since 2023 into a "surprise" versus the player's recent
  average, and every game into a surprise versus the closing spread and total, then measures how the
  surprises of two legs move together, by relationship (QB passing yards vs same-team WR receiving yards
  = +0.32; vs same-team RB rushing yards = -0.09; any player's yards vs the game total = +0.15 to +0.26;
  a player's yards vs his own touchdown = +0.35 to +0.39; two receivers on the same team ~ 0).
  The app prices a ticket with a Gaussian copula on those correlations (60,000 simulated games, fixed seed,
  matches the exact bivariate answer within 0.2 points). Moneyline and spread on the same team share one
  latent margin, so "Ravens win + Ravens -6.5" prices as the cover, and "Ravens win + Falcons +6.5" prices
  as win-by-1-to-6 — both check out against the exact formula.
- Book payouts: straight multiplication of that book's leg prices. Books quote same-game parlays with
  their own correlation model, so the app asks the user to paste the quote and grades it against the true
  probability.
- Kalshi: open KXNFLGAME contracts are matched to the moneylines; the price shown is the ask plus Kalshi's
  taker fee. Our Oct 2026 test (636 quotes, 5 weeks) found Kalshi within a cent of Pinnacle, so it is a
  price source, not an edge source. `py collect.py --kalshi-probe` lists Kalshi's other NFL series for
  adding spreads/totals/props later.

## Tests

    py -m unittest discover tests -v

## What to build next (in order)

1. A week of real data and a few real tickets through it; compare the app's "true odds" with what the books
   quote on SGPs to see how far apart they are.
2. Pro tier: free users see game lines and 2-leg tickets; a PayPal subscription (same setup as PanelReady)
   unlocks props, unlimited legs and the Best Prices tab. Gate it in `index.html` with a license key
   emailed on subscription.
3. Alerts: email when a leg's best price is 4%+ over fair (needs the mailbox bot from PanelReady).
4. Kalshi spreads/totals and player props as price sources once the probe shows the series.
