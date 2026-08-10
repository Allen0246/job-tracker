# job tracker

A personal pipeline that crawls IT job postings — from [profession.hu](https://www.profession.hu)
and a curated list of target companies' own career sites — scores them against a tunable
set of criteria, and emails a daily digest of new matches, each posting appearing
exactly once, ever.

```
crawl (11 sources) -> Postgres (raw + parsed) -> filter (scored, data-driven) -> email digest
```

## Sources

profession.hu is a broad aggregator; everything else is a specific company's own careers
site, crawled through whatever ATS (applicant tracking system) vendor happens to host it.
One adapter module per *vendor* (`tracker/<vendor>.py`), not per company — a new company on
an already-supported vendor is just a new row in that vendor's `config/companies_<vendor>.yaml`.

| Source | Vendor module | Companies |
|---|---|---|
| profession.hu | `crawler.py` | the whole board (category + region filtered) |
| Eightfold | `eightfold.py` | Morgan Stanley, Ericsson, Vodafone |
| SuccessFactors | `successfactors.py` | SAP, ONE Hungary, HCLTech |
| Radancy | `radancy.py` | Citi, BlackRock |
| Ashby | `ashby.py` | SEON |
| Workday | `workday.py` | Cloudera |
| Algolia | `algolia.py` | MSCI |
| Avature | `avature.py` | Siemens (all brands/subsidiaries, incl. evosoft) |
| iCIMS | `icims.py` | One Identity |
| Phenom | `phenom.py` | GE HealthCare |
| EPAM (own platform) | `epam.py` | EPAM |

Each vendor adapter fetches whatever that vendor's own search/filter surface allows
server-side (Budapest/Hungary scoping, when the vendor supports it — client-side filtering
only where it doesn't, e.g. Radancy). Two card shapes exist:

- **Thin-card, two-stage** (Eightfold, SuccessFactors, Radancy, Ashby, Workday, Avature,
  iCIMS, Phenom): the list page's own markup doesn't carry enough text to score reliably,
  so every non-disqualified posting gets a second request for its detail page before being
  scored for real.
- **Single-stage** (profession.hu, Algolia, EPAM): the list response already embeds the
  full description, so scoring happens immediately with no follow-up request.

## Why scrape HTML instead of using an API

profession.hu exposes a "Profession Connect" REST/XML API (`dev-connect.profession.hu`),
but it's built for the *employer* side: publishing job ads (company_id, template_id,
paid placement add-ons). There is no query, filter, pagination, or search surface in
that API — it cannot be read from as a job seeker. The public `/allasok` listing pages,
by contrast, are fully server-rendered HTML with no client-side JS gating the content,
which makes them the actual read path. None of the target companies' own career sites
expose a public job-search API either — the closest thing is Algolia's (MSCI's) public
search-only API key, embedded client-side by design.

The one genuinely useful thing pulled from profession.hu's API side is the public
value-set endpoint (`rest.profession.hu/parameter/xml-import-values`), which publishes the
canonical ID enumerations (category, region, language, experience, etc.) — confirmed
to be the *same* ID space used by the public filter URLs. `config/sources.yaml` is
built from that.

## Why fetch coarse and filter in Python

Each site's own filters only do structural cuts — category, region, remote/hybrid,
experience bracket, and (where the vendor supports it) location. Keyword logic
("Python", "not senior", "backend") runs entirely against text already stored in
Postgres, source-agnostic. That means:

- Changing what counts as a match is a re-run over history, never a re-crawl.
- Old postings can be backtested against a new rule the moment it's written.
- `config/keywords.yaml` is data (weighted keywords + disqualifiers), not code — no
  deploy needed to tune it.

First pass scores only the listing card's own fields (title, tech stack, task summary,
benefits, experience bracket). A posting that clears the stack and direction gates, and
isn't outright disqualified on seniority, gets a follow-up request for its detail page
(thin-card sources) and is re-scored with the real description before being counted as
a match. "Not outright disqualified" is deliberately looser than "confirmed junior":
card-level experience text is often blank or wrong, so a posting that's merely ambiguous
on seniority still gets a look at the real description rather than being auto-rejected
on thin card data.

## Politeness

This scrapes sites whose listing databases are their commercial product, so every
source shares one conservative `PoliteSession` (`tracker/fetch.py`):

- **robots.txt is parsed by hand at runtime**, not via stdlib's `urllib.robotparser` —
  confirmed broken two ways against real Eightfold responses: it sends Python's default
  User-Agent internally regardless of the one configured (getting 403'd by sites that
  explicitly allow the tool's own UA), and it matches rules in file order instead of
  "longest matching path wins" (RFC 9309), so the common `Disallow: /` then
  `Allow: /specific/path` idiom silently blocks everything. The hand-rolled parser fixes
  both and stays live — checked on every request, not just read once by hand.
- **One PoliteSession per host** — its own robots.txt and its own throttle clock, never
  reused across hosts.
- **Rate-limited with jitter** (`config/politeness.yaml`) — a fixed delay range between
  requests, not a fixed interval, so the traffic pattern doesn't look like a tight loop.
- **Honest User-Agent** naming the tool and a contact email, so anyone reviewing server
  logs can tell what it is and reach a human about it.
- **Retry/backoff on 429 and 5xx**, respecting `Retry-After` when present, rather than
  hammering a server that's already asking for a slowdown.
- Where a vendor's own search genuinely can't express the filter needed (blocked by
  robots.txt, no location facet, etc.), the gap is accepted and documented in that
  vendor's `config/companies_*.yaml` rather than worked around — no anti-bot evasion.

## Data model

- `postings` — one row per posting ID (source-specific: numeric on profession.hu,
  folded to a stable 63-bit int for vendors with non-numeric IDs like Ashby's UUIDs).
  `source` and `company` distinguish where a row came from and who's hiring, independent
  of each other (profession.hu spans hundreds of companies; a single vendor can host
  several target companies, or several legal entities under one company). `first_seen`/
  `last_seen` track whether a role is still listed: after each crawl, any posting
  previously active but absent from the newly-seen ID set is marked `delisted_at` and
  drops out of scoring and digests from then on. A crawl that comes back with zero
  postings refuses to delist anything — a silent failure (site outage, blocked request)
  must not be allowed to wipe out every active posting. `content_hash` (over the raw
  card HTML) detects edits, e.g. a salary being added later. Raw HTML is kept alongside
  parsed fields so a parser fix can re-parse history instead of losing it if a site
  redesigns.
- `sent_log` — a separate table (`posting_id`, `digest_date`), not a boolean column on
  `postings`. Dedup is permanent: once sent, a posting is never sent again, even after
  a filter tweak. Without this, retuning the keyword config would re-flood the inbox
  with everything that suddenly qualifies, weeks-old postings included.
- `crawl_runs` — one row per source per `run_daily` execution (fetched count,
  success/failure, whether it was a full scan). Lets a source quietly going from "40
  postings" to "0 postings" be caught by querying history instead of grepping logs — the
  failure mode that looks exactly like "nobody is hiring" if nothing tracks a healthy
  baseline.
- `scan_watermarks` — per (source, scope) high-water mark, currently only used by
  Radancy's early-stop pagination (see below).

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate          # .venv/bin/activate on the Pi later
pip install -r requirements.txt

cp .env.example .env            # fill in GMAIL_APP_PASSWORD (needs 2FA enabled:
                                 # https://myaccount.google.com/apppasswords)

docker compose up -d            # Postgres, schema auto-applied on first boot

python -m tracker.run_daily
```

`run_daily.py` takes no arguments and does not schedule itself — that's intentional.
Run it by hand, from Windows Task Scheduler, or from a cron line once this moves to
the Raspberry Pi; nothing in the code changes either way. Each source's crawl is
isolated (one broken adapter logs and moves on, it doesn't abort the run) and gets
its own log file under `logs/`, in addition to the combined `logs/run_daily.log`.

### Turning sources on/off

Every source has an `ENABLE_<SOURCE>` switch in `.env` (unset or `true` = enabled).
Multi-company sources also get one `ENABLE_<COMPANY>` switch per company, one level more
granular — e.g. pause just `BlackRock` without touching `ENABLE_RADANCY` and losing
`Citi` too. The company-level var name is derived from that company's own `name` in its
`config/companies_*.yaml` (uppercased, spaces/punctuation collapsed to `_`). Disabling a
company only skips *crawling* it — a disabled company's already-collected postings are
protected from being wrongly marked delisted (their IDs are folded into the run's
seen-set without being re-fetched), and existing survivors still get scored/emailed
normally. See the toggle block in `.env.example` for the full list.

## Tuning what counts as a match

Edit `config/keywords.yaml` — weighted positive/negative keywords for stack and
direction, a disqualifier list for each gate, and an experience-text allowlist for
seniority. No code change, no redeploy: the next `python -m tracker.filter` run
re-scores every stored posting against the new rules.

A few things beyond plain keyword scoring:

- `direction.disqualifiers` hard-excludes testing roles (QA, manual test, SDET, test
  automation) even though "automation" is itself a positive direction keyword — a
  weighted score alone can't tell "DevOps automation" from "QA automation engineer,"
  so specific phrases override the score regardless of how high it is.
- `level.disqualifiers` combines word-based terms (senior, lead, principal, its Hungarian
  spelling "szenior", ...) with a numeric check: any "N years" / "N-M years" / "N éves"
  mention in the combined text above `max_years_experience` hard-disqualifies, catching
  postings that read as junior in the title but require, say, 5 years' experience in the
  body.
- `level.ok_experience_text` is matched against the full combined text and deliberately
  excludes "intern"/"co-op" — an internship shouldn't qualify just because it reads as
  junior-friendly.
- `level.ok_experience_level_values` is matched *only* against a source's own structured
  experience-level field, never free text — a controlled-vocabulary value (like GE
  HealthCare/Phenom's "Early Career", cleanly distinct there from "Co-op/Intern" and
  "Mid-Career") is a reliable signal in the field it actually came from, but the same
  words as loose marketing copy elsewhere ("our early career program...") aren't.
- Word-boundary matching (not plain substrings) avoids false positives like "lead"
  inside "industry-leading" — confirmed against a real posting that should have passed
  and didn't.

## Digest grouping

profession.hu spans hundreds of small companies, so its postings stay under one
"profession.hu" umbrella section — a section per company there would just be noise.
Every other source groups by the actual hiring company, not the ATS vendor: nobody
reading the digest cares that a posting came through "Avature," they care that it's
Siemens Mobility Kft. (`tracker/digest.py:_group_key`).

## Radancy's early-stop pagination

Radancy (Citi, BlackRock) only surfaces Budapest/Hungary-matched postings, so a normal
"stop when I see an already-known ID" shortcut doesn't fire reliably — most IDs on any
page were never stored to begin with. Instead it tracks a high-water mark (`max_job_id`)
per (company, category) in `scan_watermarks`, confirmed against real pages returning
strictly descending, roughly time-ordered IDs: two consecutive pages entirely below the
watermark stop the scan early. Early-stop runs skip delisting (they didn't see enough of
the listing to know an absent posting is actually gone, not just past where pagination
stopped); a full scan runs at least weekly regardless, to catch anything an early-stop
run's assumptions missed.

## Scope

profession.hu targets category 10 (IT programozás, fejlesztés) in two crawl targets —
Budapest and nationwide-remote — merged by upserting on posting ID, since its URL
filters AND-combine and can't express "Budapest OR remote" in one request. See
`config/sources.yaml` for the full filter ID legend. Company sources target Budapest/
Hungary specifically, server-side where the vendor's own search supports it and
client-side (post-fetch) where it doesn't (documented per company in that vendor's
`config/companies_*.yaml`).
