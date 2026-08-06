# profession.hu job tracker

A personal pipeline that crawls IT job postings from [profession.hu](https://www.profession.hu),
scores them against a tunable set of criteria, and emails a daily digest of new
matches — each posting appearing exactly once, ever.

```
crawl (HTML) -> Postgres (raw + parsed) -> filter (scored, data-driven) -> email digest
```

## Why scrape HTML instead of using their API

profession.hu exposes a "Profession Connect" REST/XML API (`dev-connect.profession.hu`),
but it's built for the *employer* side: publishing job ads (company_id, template_id,
paid placement add-ons). There is no query, filter, pagination, or search surface in
that API — it cannot be read from as a job seeker. The public `/allasok` listing pages,
by contrast, are fully server-rendered HTML with no client-side JS gating the content,
which makes them the actual read path.

The one genuinely useful thing pulled from the API side is the public value-set
endpoint (`rest.profession.hu/parameter/xml-import-values`), which publishes the
canonical ID enumerations (category, region, language, experience, etc.) — confirmed
to be the *same* ID space used by the public filter URLs. `config/sources.yaml` is
built from that.

## Why fetch coarse and filter in Python

The site's own URL filters only do structural cuts — category, region, remote/hybrid,
experience bracket. Keyword logic ("Python", "not senior", "backend") runs entirely
against text already stored in Postgres. That means:

- Changing what counts as a match is a re-run over history, never a re-crawl.
- Old postings can be backtested against a new rule the moment it's written.
- `config/keywords.yaml` is data (weighted keywords + disqualifiers), not code — no
  deploy needed to tune it.

First pass scores only the listing card's own fields (title, tech stack, task summary,
benefits, experience bracket — all present directly in the card markup, no extra
request). A posting that clears the stack and direction gates, and isn't outright
disqualified on seniority, gets a follow-up request for its detail page and is
re-scored with the real description before being counted as a match. "Not outright
disqualified" is deliberately looser than "confirmed junior": card-level experience
text is often blank or wrong, so a posting that's merely ambiguous on seniority still
gets a look at the real description rather than being auto-rejected on thin card data.

## Politeness

This scrapes a site whose listing database is its commercial product, so the crawler
is deliberately conservative:

- **robots.txt is checked at runtime** (`urllib.robotparser`), not just read once by
  hand — `/allasok` is confirmed allowed, and the check stays live if that ever
  changes.
- **Rate-limited with jitter** (`config/sources.yaml: politeness`) — a fixed delay
  range between requests, not a fixed interval, so the traffic pattern doesn't look
  like a tight loop.
- **Honest User-Agent** naming the tool and a contact email, so anyone reviewing
  server logs can tell what it is and reach a human about it.
- **Retry/backoff on 429 and 5xx**, respecting `Retry-After` when present, rather than
  hammering a server that's already asking for a slowdown.

## Data model

- `postings` — one row per numeric posting ID (the ID suffix on every `/allas/...`
  URL). `first_seen`/`last_seen` track whether a role is still listed: after each
  crawl, any posting previously active but absent from the newly-seen ID set is marked
  `delisted_at` and drops out of scoring and digests from then on. A crawl that comes
  back with zero postings refuses to delist anything — a silent failure (site outage,
  blocked request) must not be allowed to wipe out every active posting. `content_hash`
  (over the raw card HTML) detects edits, e.g. a salary being added later. Raw HTML is
  kept alongside parsed fields so a parser fix can re-parse history instead of losing
  it if the site redesigns.
- `sent_log` — a separate table (`posting_id`, `digest_date`), not a boolean column on
  `postings`. Dedup is permanent: once sent, a posting is never sent again, even after
  a filter tweak. Without this, retuning the keyword config would re-flood the inbox
  with everything that suddenly qualifies, weeks-old postings included.

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
the Raspberry Pi; nothing in the code changes either way.

## Tuning what counts as a match

Edit `config/keywords.yaml` — weighted positive/negative keywords for stack and
direction, a disqualifier list for each gate, and an experience-text allowlist for
seniority. No code change, no redeploy: the next `python -m tracker.filter` run
re-scores every stored posting against the new rules.

Two disqualifier lists go beyond plain keyword scoring:

- `direction.disqualifiers` hard-excludes testing roles (QA, manual test, SDET, test
  automation) even though "automation" is itself a positive direction keyword — a
  weighted score alone can't tell "DevOps automation" from "QA automation engineer,"
  so specific phrases override the score regardless of how high it is.
- `level.disqualifiers` combines word-based terms (senior, lead, principal, ...) with a
  numeric check: any "N years" / "N-M years" / "N éves" mention in the combined text
  above `max_years_experience` hard-disqualifies, catching postings that read as
  junior in the title but require, say, 5 years' experience in the body.

## Scope

Currently targets category 10 (IT programozás, fejlesztés) in two crawl targets —
Budapest and nationwide-remote — merged by upserting on posting ID, since profession.hu's
URL filters AND-combine and can't express "Budapest OR remote" in one request. See
`config/sources.yaml` for the full filter ID legend.
