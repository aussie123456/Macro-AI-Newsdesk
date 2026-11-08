# Macro & AI Newsdesk

Daily news and markets desk for an AI data-centre capital-markets reader: broad markets and sector charts, a brief,
second- and third-order effects cards, and curated headlines on AI and data centres, US-Iran, geopolitics, US politics,
Korea, Japan, Australia and energy.

This is the GitHub Actions + GitHub Pages edition of the claude.ai dashboard of the same name (migrated 8 Nov 2026).

## How it works

| Piece | What it does |
|---|---|
| `fetch.py` | No LLM, standard library only. Pulls one year of daily closes for 30 instruments from Yahoo Finance and curated Google News headlines for nine themes (preferred-source ranking, aggregator blocklist, de-duplication, 20 per theme). Carries a symbol or theme over from the previous file if its fetch fails. Rewrites the brief's "as of" line so the page says what is fresh. |
| `write.py` | Optional. Asks Claude (Anthropic API) for the ten theme summaries and the 6 to 9 effects cards, from the headlines and price moves, and validates them against the page's schema before writing. Skipped when the `ANTHROPIC_API_KEY` secret is not set; the previous summaries and cards then stay on the page with their date shown. |
| `docs/index.html` | The page. Reads `docs/data/*.json` and re-checks every 30 minutes. The chart window (1M, 3M, 6M, 1Y) is also a URL parameter. |
| `docs/data/` | `prices.json`, `news.json`, `themes.json`, `derivatives.json`, plus `meta.json` written by each run. |
| `.github/workflows/refresh.yml` | Runs daily at 7:15am Sydney. Pulls the last published data from the site first, runs `fetch.py`, then `write.py`, publishes `docs/` to Pages. |

Symbol list, theme queries and source preferences are constants at the top of `fetch.py`.

## Set-up (once)

1. The repo must be **public** (GitHub Pages is not available on private repos on the free plan).
2. Settings > Pages > Source: **GitHub Actions**.
3. Optional: Settings > Secrets and variables > Actions > New repository secret `ANTHROPIC_API_KEY` for the written summaries and effects cards.
4. Actions > Refresh newsdesk > Run workflow for the first publish. The page URL appears on the deploy step.

## Daylight saving

The cron is in UTC. Sydney leaves daylight saving on the first Sunday of April and returns on the first Sunday of October,
so the refresh lands at 6:15am Sydney in winter unless the cron is moved to `15 21 * * *`.

## Local test

`python fetch.py --out out` and `python write.py --out out` write into `out/`.
The page needs a web server (fetch does not work from `file://`): any static server pointed at `docs/` will do.
