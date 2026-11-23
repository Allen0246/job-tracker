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

- **robots.txt is checked at runtime** before every request, not just read once by
  hand — `/allasok` and `/allas/...` are confirmed allowed, and the check stays live if
  that ever changes. It's parsed in `tracker/fetch.py` rather than with stdlib
  `urllib.robotparser`, because profession.hu's rules are mostly `*` wildcards
  (`Disallow: */jelentkezes/`), which stdlib silently ignores, and stdlib applies the
  first matching rule instead of the most specific one (RFC 9309).
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

## Setup (Windows / local development)

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt

cp .env.example .env            # fill in GMAIL_APP_PASSWORD (needs 2FA enabled:
                                 # https://myaccount.google.com/apppasswords)

docker compose up -d            # Postgres, schema auto-applied on first boot

python -m tracker.run_daily
```

`run_daily.py` takes no arguments and does not schedule itself — that's intentional.
Run it by hand, from Windows Task Scheduler, or from the systemd timer below; nothing
in the code changes either way.

## Running on a Raspberry Pi

Needs Raspberry Pi OS **64-bit** (Bookworm or newer, which ships Python 3.11; the
code needs 3.10+). Every dependency, Postgres image included, has ready-made arm64
builds, so nothing compiles on the Pi.

```bash
sudo timedatectl set-timezone Europe/Budapest   # the timer runs on local time
curl -fsSL https://get.docker.com | sh          # Docker + compose plugin
sudo usermod -aG docker $USER                   # then log out and back in

git clone https://github.com/Allen0246/profession-job-tracker.git
cd profession-job-tracker
cp .env.example .env && nano .env               # same values as on Windows

./deploy/install.sh
```

`install.sh` creates the venv, starts Postgres, and installs a systemd timer
(`deploy/job-tracker.timer`) that runs the pipeline daily at 07:00. If the Pi was
off at 07:00, the run happens as soon as it's back up. Re-run `install.sh` after
changing anything in `deploy/`.

```bash
systemctl list-timers job-tracker.timer        # when's the next run
sudo systemctl start job-tracker.service       # run right now
journalctl -u job-tracker.service -n 100       # output of the last runs
```

Postgres listens on `127.0.0.1` only, so it isn't reachable from the rest of the
home network. Logs also go to `logs/run_daily.log`, rotated at 1 MB.

### Moving existing data from Windows (optional)

A fresh database on the Pi works fine: the first crawl fills it. The only thing
lost is `sent_log`, so postings already emailed would be emailed once more. To
keep history instead:

```bash
# on Windows (start Postgres there first: docker compose up -d)
docker exec -i profession-tracker-db psql -U tracker profession_tracker < sql/remove_company_sources.sql
docker exec profession-tracker-db pg_dump -U tracker --data-only -t postings -t sent_log profession_tracker > dump.sql
# copy dump.sql to the Pi, then, after install.sh has started Postgres:
docker exec -i profession-tracker-db psql -U tracker profession_tracker < dump.sql
```

The first command matters even if you keep running on Windows: a database from the
time the company career-site crawlers existed still holds their postings, and the
filter would try to fetch and parse those pages as if they were profession.hu.

## Tuning what counts as a match

Edit `config/keywords.yaml` — weighted positive/negative keywords for stack and
direction, a disqualifier list for each gate, and an experience-text allowlist for
seniority. No code change, no redeploy: the next `python -m tracker.filter` run
re-scores every stored posting against the new rules.

Terms match as whole words, so `lead` doesn't hit "industry-leading" and `git`
doesn't hit "digital". End a term with `*` (quoted: `"vezető*"`) to also match any
suffix — needed for plurals and Hungarian inflections ("vezetőt", "automatizációs").
Start it with `*` (`"*menedzser"`) to also match anything in front, for Hungarian
compounds written as one word ("projektmenedzser", "szoftvertesztelő").

Each gate has two kinds of disqualifier list: `disqualifiers` is checked against the
whole posting (title, card fields and full description), `title_disqualifiers` against
the job title only. Use the title-only list for words that name a kind of role but also
appear innocently in ordinary descriptions ("work closely with our QA engineer", "we
also run an internship program").

Two disqualifier lists go beyond plain keyword scoring:

- `direction.title_disqualifiers` hard-excludes testing roles (QA, manual test, SDET, test
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
