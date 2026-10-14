# mapscan: MAP compliance monitoring

mapscan protects a brand's pricing. It finds third-party listings of the
brand's products across the web and confirms each one really is that product.
It then tracks the advertised price against the Minimum Advertised Price (MAP)
and captures tamper-evident evidence when a dealer goes below it.

It was built in-house at [Henry Holsters](https://henryholsters.com), a
manufacturer of firearm holsters, to watch a catalogue of about 250 SKUs
across its dealer network. This repository is a public snapshot of that
working system. The code is brand-neutral: point it at your own catalogue
and brand name.

---

## What it does

- **Discovers listings** in three ways:
  - paste a dealer's URL (a brand or search page, or a bare domain);
  - Brave Search web discovery on a monthly budget;
  - eBay Browse API lookups by UPC/GTIN.
- **Verifies identity before trusting a price.** UPCs, SKUs, fuzzy titles and
  "fitment" tokens (gun model, light model) are scored together. An identifier
  match confirms a listing, and a conflicting identifier or fitment rejects
  it. Anything in between goes to a human review queue.
- **Extracts prices cheaply.** It tries five tiers in order: platform JSON
  (e.g. Shopify `.json`), then JSON-LD, then microdata/OpenGraph, then
  per-dealer selector rules, and finally an LLM fallback on pre-trimmed text.
- **Opens and closes violations with a debounce.** A violation opens only after
  N consecutive below-MAP checks, so one-off blips don't create noise. A single
  check at or above MAP closes it.
- **Captures evidence when a violation opens:** the gzipped page HTML, its
  SHA-256 hash, and a screenshot through a fetch vendor.
- **Rechecks adaptively.** It uses conditional requests (`ETag` /
  `If-Modified-Since`) and an adaptive cadence: daily while a listing is
  violating or its price is moving, backing off to weekly when stable. After
  three 404s a listing is retired.
- **Runs a small dashboard.** The dashboard has pages for violations, the
  review queue, sellers, products, scan history, settings and users. An admin
  can exclude a domain, stop tracking a listing, or delete it along with its
  evidence.
- **Crawls politely.** It respects `robots.txt` (including `Crawl-delay`) and
  throttles each domain. It has a public `/about` page that its User-Agent
  links to. Amazon and social platforms are excluded by design.

## Architecture

```
               ┌──────────── FastAPI + Jinja2 dashboard (app/) ───────────┐
 browser ───▶  │ auth, review queue, violations, settings, "scan this URL" │
               └──────────────┬───────────────────────────────────────────┘
                              │ enqueue jobs                ▲ reads
                              ▼                             │
                         Redis (arq)                   PostgreSQL
                              │                             ▲
                              ▼                             │ writes
               ┌──────────── arq worker (worker/) ──────────┴──────────────┐
               │ discovery → slug pre-filter → fetch → extract → match →   │
               │ violation state machine → evidence capture                │
               └───────────────────────────────────────────────────────────┘
```

### How a scan flows

```
Brave Search        ─┐
eBay GTIN lookup    ─┤
paste-a-dealer-URL  ─┼→ candidate URLs → slug pre-filter → fetch (tier 1 → 2)
sitemap expansion   ─┘                                          │
                                                                ▼
                         extract: platform JSON → JSON-LD → microdata/OG
                                  → selector rule → LLM
                                                                │
                                      match: UPC / SKU / title / fitment
                                                                │
                        confirmed ──→ nightly recheck ──→ violation machine
                        candidate ──→ human review queue         │
                        rejected  ──→ never fetched again    evidence capture
```

**Choosing candidates.** A large dealer's sitemap can list more than 35,000
URLs, and fetching each one would be slow, costly and rude. Three free gates
run before any page is fetched:

1. **Sitemap classification by filename.** Product sitemaps are ranked ahead
   of category, listing and "unorderable" ones.
2. **Brand gating on the URL slug.** The slug must contain the brand name or
   one of the catalogue's product-line names, which are derived from the
   catalogue rather than hardcoded. On one real dealer this cut the
   candidates from 1,318 to 10.
3. **An exact SKU in the URL.** Dealers often append the manufacturer's part
   number to the slug or title. When a known SKU appears there, it settles
   the match outright.

**Two scan modes.** If the pasted URL has a path (a brand or search page), the
scan walks that page's `rel="next"` pagination. Many dealers print the price
right on each product card, so a few page fetches can price an entire brand.
A bare domain goes down the sitemap route instead.

**Fetching in tiers.** Tier 1 is plain `httpx` and costs nothing. Tier 2 is a
paid rendering vendor (ScrapingBee or Zyte). A domain is promoted to tier 2
only after it has demonstrably blocked tier 1, and the promotion lapses after
48 hours.

**Clear failure reasons.** "No products found" has several causes, and the
scan reports which one applies:

- no slug carried the brand;
- the URL redirected somewhere unrelated;
- the dealer builds its product grid with JavaScript;
- the dealer is behind bot protection.

### Keeping costs near zero

| Lever | Effect |
|---|---|
| Search planner (`worker/planner.py`) | Searches by UPC only, skips SKUs whose sellers are already covered by sitemaps, backs off per SKU (3 → 60 days), caches results for 21 days, and holds back a 20% budget reserve |
| Monthly search cap stored in Postgres | Never exceeds the Brave plan |
| Slug pre-filter before fetching | A 5,000-URL sitemap becomes ~20 fetches |
| Four free extraction tiers before the LLM | Most pages never reach the model |
| HTML trimmed around prices before the LLM | ~150k-token page → ~3–5k tokens |
| Screenshots only when a violation opens | Dozens of vendor calls, not thousands |
| Price history written only on change | The largest table stays small |

## Tech stack

Python 3.11+, FastAPI, Jinja2, asyncpg (raw SQL, no ORM), arq + Redis,
PostgreSQL (with `pg_trgm`), httpx, selectolax, extruct/lxml, RapidFuzz,
openpyxl, the OpenAI API (structured-output extraction fallback), bcrypt,
itsdangerous and `cryptography` (Fernet).

## Requirements

- Python 3.11 or newer
- PostgreSQL 15+ with the `pg_trgm` extension
- Redis 6+
- Optional API keys:
  - Brave Search (web discovery)
  - eBay developer keyset (GTIN search)
  - OpenAI (LLM extraction fallback)
  - ScrapingBee or Zyte (JS rendering and screenshots)
  - Resend or a Google Workspace service account (invite emails)

Without any API keys, mapscan still works in "paste a dealer URL" mode.

## Setup

```sh
git clone <this repo> mapscan && cd mapscan
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill it in (see below)
```

### Database

```sh
createuser mapscan --pwprompt
createdb mapscan -O mapscan
psql -d mapscan -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"   # as a superuser
psql "$DATABASE_URL" -f schema.sql
for f in migrations/*.sql; do psql "$DATABASE_URL" -f "$f"; done
```

`schema.sql` is the base schema. The numbered migrations apply on top of it,
in order.

### Configuration

All settings are read from the environment (`.env`). The important ones:

| Variable | Example | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://mapscan:…@localhost:5432/mapscan` | Postgres connection |
| `REDIS_URL` | `redis://localhost:6379/0` | arq job queue |
| `SECRET_KEY` | *random 48+ chars* | Signs session cookies |
| `SETTINGS_KEY` | *Fernet key* | Encrypts credentials entered on the Settings page |
| `PUBLIC_BASE_URL` | `https://mapscan.example.com` | Used to build invite links |
| `BRAND_NAME` | `Your Brand` | Shown in the UI and on `/about`; its first word is the default slug brand token |
| `OWN_DOMAINS` | `yourbrand.com` | Your own storefronts, never flagged as sellers |
| `CONTACT_EMAIL` | `you@yourbrand.com` | Shown on `/about` so site owners can opt out |
| `USER_AGENT` | `MAPScan/1.0 (+https://…/about)` | Crawler identity |
| `EVIDENCE_DIR` | `./evidence` | Where HTML and screenshots are stored |
| `MATCH_AUTO_CONFIRM` / `MATCH_AUTO_REJECT` | `85` / `45` | Match score thresholds |
| `VIOLATION_CONSECUTIVE_CHECKS` | `2` | Debounce before a violation opens |

`.env.example` documents every variable.

**Credentials are entered on the Settings page, not in files.** This covers
the Brave, eBay, OpenAI, fetch-vendor and email credentials. Each value is
encrypted with Fernet under `SETTINGS_KEY` and stored in Postgres. Each
setting is looked up in the database first, then the environment, then the
built-in default. The worker picks up a changed key within about 30 seconds,
without a restart.

To generate the two keys:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(48))"                            # SECRET_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # SETTINGS_KEY
```

### Loading your catalogue

The importer reads CSV or XLSX with the columns `UPC, SKU, Description,
Wholesale Price, MAP, MSRP`. `Description` becomes the product title, which
drives matching, so short product names work best.
[`sample-products.csv`](sample-products.csv) shows the format with fake data.

```sh
python -m scripts.import_products sample-products.csv            # dry run + validation report
python -m scripts.import_products sample-products.csv --commit
```

The importer rejects rows where MAP is greater than MSRP (a sign of swapped
columns), warns on UPCs that fail the GS1 check digit, and flags duplicate
UPCs. All three silently corrupt matching otherwise. Real catalogue files are
gitignored.

## Running

```sh
python -m scripts.create_admin you@yourbrand.com   # prints a one-time initial password
uvicorn app.main:app --host 127.0.0.1 --port 8013  # dashboard
arq worker.tasks.WorkerSettings                    # crawler / scheduler
```

Open the dashboard, go to **Scan**, and paste a dealer URL. Then work through
the **Review** queue. Nothing is price-checked until you confirm it.

The worker runs these jobs on a schedule (UTC):

| When | Job |
|---|---|
| 03:20 nightly | `recheck_all`: every confirmed listing that is due |
| 07:00 daily | `discovery_sweep`: within the monthly search budget |
| 05:00 Sundays | `prune_observations`: retention |

[`deploy/`](deploy/) has example systemd units for running both processes as
a dedicated user. They set memory limits and use systemd's sandboxing options.
In production, put the app behind a TLS-terminating reverse proxy and keep it
bound to localhost.

## Auth model

- Accounts are invite-only. An admin creates a user, and the app generates a
  one-time, time-limited setup link. The link is emailed (Resend or the Gmail
  API) or shown to the admin to pass on (`manual` mode, the default).
- Passwords are hashed with bcrypt and checked against a weak-password
  blocklist. Sessions use signed, `HttpOnly` cookies (`itsdangerous`) with a
  configurable lifetime.
- There are two roles: admin and user. Settings, users and domain exclusion
  are admin-only.
- `tools/reset_password.py` mints a reset link from the server's command line
  when no mail provider is configured.

## Tests

The tests are plain Python scripts that print PASS/FAIL. Most run offline with
everything stubbed:

```sh
python tests/test_ebay_notify.py
python tests/test_ebay_token.py
python tests/test_gmail_pem.py
python tests/test_llm_extraction.py
python tests/test_sitemap_classify.py
python tests/test_evidence_artifacts.py
```

`test_planner.py`, `test_prohibited_policy.py` and `test_settings_store.py`
need a real database. The two `.sh` scripts are
end-to-end HTTP tests against a running instance. Each creates a throwaway
admin and cleans up after itself.

## Project layout

```
app/                FastAPI dashboard
  auth.py           sessions, invites, password hashing
  config.py         env-driven settings
  db.py             asyncpg pool + query helpers
  ebay_notify.py    eBay marketplace-account-deletion endpoint
  mail.py           invite delivery: manual | gmail_api | resend | smtp
  main.py           routes
  settings_store.py encrypted credential store (DB → env → default)
  templates/ static/
worker/             arq worker
  discovery.py      Brave Search, sitemap discovery and classification
  ebay.py           eBay Browse API (GTIN + keyword)
  evidence.py       gzipped HTML + SHA-256 + screenshot
  extract.py        5-tier price extraction
  fetch.py          tiered fetcher, robots.txt, per-domain throttle
  listing_page.py   brand/search page harvesting + card pricing
  match.py          UPC / SKU / title / fitment scoring
  planner.py        search-budget planner
  tasks.py          jobs + cron schedule
  violations.py     open/resolve state machine with debounce
migrations/         numbered SQL migrations (apply after schema.sql)
scripts/            create_admin, import_products, queue_job
tools/              password reset, eBay token helper
deploy/             example systemd units
tests/
```

## Lessons from running it on real data

Several design choices came from bugs found against live dealer sites:

- **Fuzzy titles alone can't identify a listing.** "IWB Holster for Sig P320
  Compact" and "IWB Holster for Glock 43x" score 86% on `token_set_ratio`.
  Extracting fitment tokens, and treating disjoint ones as a veto, fixed it.
- **The GTIN-13 check digit was inverted** (weights 3,1… instead of 1,3…), which
  rejected most valid UPCs. The original test fixture validated under both
  weightings, so the tests now use values where the two disagree.
- **Two optimisations cancelled each other out.** Price history is written
  only when the price changes, but the debounce counted stored observations.
  So a dealer sitting steadily below MAP never opened a violation, which is
  the one case the tool exists to catch. The streak is now counted per check.
- **Tier-2 promotion was a one-way door.** Bot protection is often triggered
  by request rate, not permanent, so promotions now expire.

## Status

mapscan is in production use at Henry Holsters. This repository is a
point-in-time snapshot: the deployment details, real catalogue data and
credentials have been removed. Issues and pull requests are not actively
monitored.

## License

Copyright (C) 2026 Ben Crum.

Licensed under the [GNU Affero General Public License v3.0](LICENSE). You may use, modify and redistribute this software, but if you distribute it or run a modified version as a network service, you must make your source code available under the same license.
