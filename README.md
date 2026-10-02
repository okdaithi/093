# headliner

A small CLI that collects news headlines from a configurable list of sites,
normalises them, and stores them in SQLite. It reads RSS/Atom feeds and, where
no feed exists, scrapes a listing page with CSS selectors you supply.

It fetches **headlines, links, timestamps and feed summaries only**. It does not
download article bodies and is not a way around a paywall.

- Async fetching with a per-domain rate limit and bounded concurrency
- `robots.txt` is checked and cached per domain; disallowed paths are skipped
- Retries on 429/5xx/timeouts with exponential backoff, jitter and `Retry-After`;
  a `Retry-After` longer than 30s skips the source until the next run
- Idempotent storage: one row per article URL; re-running never duplicates a headline
- Headline rewrites are kept: every distinct title an article carries is recorded
- Live blogs are recognised, kept out of the rewrite list by default, and
  available as a running timeline
- One failing source never aborts the run
- An optional read-only web viewer (`headliner web`) for browsing, rewrites,
  search and source health in a browser

## Requirements

Python 3.11 or newer. Runtime dependencies: `httpx`, `feedparser`, `selectolax`,
`pyyaml`.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

With the test tooling:

```bash
pip install -e '.[dev]'
```

If `selectolax` will not build on your platform, use the BeautifulSoup backend
instead — the HTML parser falls back to it automatically:

```bash
pip install -e '.[bs4]'
```

## Deploy on Ubuntu

The deployment files install Headliner as a `systemd` oneshot service that runs
four times daily (00:00, 06:00, 12:00 and 18:00 in the server's local timezone),
with up to five minutes of timer jitter. Fetching needs no inbound ports; the
optional [web viewer](#web-viewer) listens on port 8090.

On an Ubuntu 24.04 server, clone the repository and run:

```bash
sudo bash deploy/install-ubuntu.sh
```

The installer creates a restricted `headliner` system account, a Python virtual
environment in `/opt/headliner/venv`, a persistent database at
`/var/lib/headliner/headlines.db`, and installs the service and timer units. It
does not enable the timer automatically. First replace `you@example.com` in
`/etc/headliner/sources.yaml` with a monitored contact address, then enable the
schedule and optionally perform the initial run:

```bash
sudoedit /etc/headliner/sources.yaml
sudo systemctl enable --now headliner.timer
sudo systemctl start headliner.service
```

Inspect timer state with `systemctl list-timers headliner.timer` and logs with
`journalctl -u headliner.service`. To deploy an update, rerun
`sudo bash deploy/install-ubuntu.sh` from the updated checkout; it preserves the
server's existing source configuration and database. The current defaults are
written next to it as `/etc/headliner/sources.yaml.dist`, and the installer
prints a note when the two differ so you can merge changes such as retired
feeds:

```bash
sudo diff -u /etc/headliner/sources.yaml /etc/headliner/sources.yaml.dist
```

To adopt the shipped list wholesale, for example after new sources are added,
rerun the installer with `--update-sources`:

```bash
sudo bash deploy/install-ubuntu.sh --update-sources
```

This replaces the live file with the shipped one but keeps its `user_agent`
line, so your contact address survives. The previous file is saved as
`sources.yaml.bak-<timestamp>` and the diff is printed. If the result doesn't
load, the backup is put back and the installer stops. Other local edits are not
merged, so check the diff if you have them.

## Web viewer

`headliner web` serves a read-only view of the database in a browser:

| Page | Shows |
| --- | --- |
| **Latest** | Headlines newest first, grouped by day, with links to the articles. A `LIVE` badge marks live blogs, and "N titles" marks rewritten articles. |
| **Stories** | Headlines from different outlets grouped into stories (see [Stories](#stories)): most widely covered first or newest first, with outlets per tag ("AU 3 · IE 1") and each outlet's headline. Latest and Search mark grouped headlines "N outlets", linking to the story. |
| **Rewrites** | Each title change as a word-level diff (removed words struck through, added words highlighted), with the old title underneath. Live blogs and minor (punctuation-only) changes are hidden by default, as in `changes`; the page says how many of each were hidden and lets you show them. |
| **Search** | Full-text search over current titles and summaries, with matches highlighted. Tick *Include earlier titles* to search every version (like `search --history`). |
| **Sources** | Each source's tags, item count, last success and last run status: `ok`, `failed` (with the error), `skipped`, `stale` (no success in 13 hours) or `never fetched`. Below it, the last 12 runs with their ok/skipped/failed counts and new and retitled items. |
| **Article** | Every title one article has carried, oldest first, each diffed against the one before. Reached from "N titles" or "all titles". |

Every list filters by tag (any of those ticked), source and time window, and
filters are kept as you move between pages. Times follow the CLI: local time with
the zone named, a header link to switch to UTC, and the UTC timestamp on hover.

The viewer opens the database read-only, so it can't change it, and only answers
`GET`/`HEAD`. Pages are plain HTML and CSS with no JavaScript and no third-party
requests, under a strict Content-Security-Policy. Article links open in a new tab
without sending a referrer. It needs no packages beyond headliner's own.
`/healthz` returns JSON (`status`, `schema`, `articles`, `last_fetch`) for
monitoring.

Run it locally against any database:

```bash
headliner web --db headlines.db --sources sources.yaml   # http://127.0.0.1:8090/
```

`--host` defaults to `127.0.0.1`; `--host 0.0.0.0` serves the whole network.
There is no login, so only listen on networks you trust.

**On the server**, the installer adds `headliner-web.service`, which runs as the
`headliner` user on port 8090, on all interfaces. Enable it once:

```bash
sudo systemctl enable --now headliner-web.service
```

Redeploys restart it on the new code. To reach it over Tailscale with HTTPS:

```bash
sudo tailscale serve --bg --https 8444 http://127.0.0.1:8090
```

To keep it off the home network and serve it only through Tailscale, change
`--host 0.0.0.0` to `--host 127.0.0.1` with `sudo systemctl edit --full
headliner-web.service`. Logs: `journalctl -u headliner-web.service`.

## Quickstart

```bash
# See what the configured sources return, without writing anything
headliner fetch --dry-run

# Fetch for real into ./headlines.db
headliner fetch

# What came in over the last day
headliner list --since 24h

# One source, as CSV
headliner list --source "BBC News" --format csv

# Full-text search over stored titles and summaries
headliner search "interest rates"

# ...including every earlier version of each headline and summary
headliner search "quarantine centre" --history

# Which sources are configured and when each last worked
headliner sources

# Headlines that outlets rewrote after publication, over the last week
headliner changes --since 7d
```

Logs go to stderr, data goes to stdout, so piping works:

```bash
headliner list --since 6h --format json 2>/dev/null | jq '.[].title'
```

## Commands

| Command | What it does |
| --- | --- |
| `headliner fetch` | Fetch every enabled source, parse it, store new headlines |
| `headliner list` | Print stored headlines, newest first |
| `headliner search QUERY` | Search stored titles and summaries |
| `headliner sources` | Show each configured source with its last successful fetch and item count |
| `headliner changes` | List headlines that were rewritten after publication, newest first |
| `headliner migrate` | Upgrade the database schema (runs automatically; `--dry-run` previews) |
| `headliner discover URL...` | Find each site's RSS/Atom feed and print source entries to paste (see [Adding sources](#adding-sources)) |
| `headliner stories` | Stories reported by several outlets, most widely covered first (see [Stories](#stories)) |
| `headliner web` | Serve a read-only web viewer (see [Web viewer](#web-viewer)) |

Shared flags: `--sources PATH` (default `sources.yaml`), `--db PATH` (default
`headlines.db`), `--utc` for UTC times in tables, `--verbose` for DEBUG logging,
`--quiet` for errors only.

**Times.** Everything is stored in UTC. Table output shows local time, taken from
`TZ` or the system timezone, and names the zone in the column header, e.g.
`PUBLISHED (AWST)`. `--utc` shows UTC instead, and `TZ=Europe/Dublin headliner
list` shows any other zone. If a column spans a daylight-saving change, the header
says `local` and each time carries its own abbreviation. JSON and CSV output
always use UTC ISO-8601 timestamps (`…+00:00`), whatever the display settings.
`--since` is a duration, so it doesn't depend on the timezone.

**`fetch`**

| Flag | Effect |
| --- | --- |
| `--only NAME [NAME ...]` | Fetch just these sources, by configured name (case-insensitive). Reaches sources marked `enabled: false`. |
| `--ignore-robots` | Skip the `robots.txt` check. Off by default. |
| `--dry-run` | Fetch and parse, print the results, write nothing. |

**`list`**

| Flag | Effect |
| --- | --- |
| `--since DURATION` | Only items newer than e.g. `30m`, `24h`, `7d`, `2w` |
| `--source NAME` | Restrict to one configured source |
| `--limit N` | Maximum rows (default 50) |
| `--format table\|json\|csv` | Output format (default `table`) |

**`search`** takes `--limit` and `--format`, and needs no config file — it only
reads the database. By default it matches each article's **current** title and
summary. `--history` searches every earlier title and summary too and returns
one result per article, showing its current headline:

- If the current version matches, the result looks the same as a normal search.
- If only an earlier version matched, the table adds a `MATCHED EARLIER TITLE`
  column, and JSON and CSV add `matched_title`. It is `null`/empty when the
  current version matched.

**`sources`** takes `--format table|json`. The table and JSON include each
source's tags.

**`--tag TAG`** works on `fetch`, `list`, `search`, `changes` and `sources`. It
selects the sources carrying that tag, ignoring case. Repeat it to match any of
several tags (`--tag AU --tag IE`). An unknown tag is an error that lists the
tags in use. `search` and `changes` otherwise need no config file, but read it
when `--tag` is given.

**`changes`** takes the same `--since`, `--source`, `--limit` and `--format` flags
as `list`, and needs no config file. Each row is one rewrite: the previous title,
the new one, and when the new one was first seen. Live blogs are hidden by default
(see [Live blogs](#live-blogs)), and so are **minor rewrites**: changes of case,
punctuation, quote style or spacing only, such as a comma moving inside a quote
or "Attorney General" becoming "attorney-general". Words added, removed or
swapped always count. The table ends with a note saying how many of each were
hidden. JSON and CSV mark each row with `is_minor`, and the `fetch` summary
counts them, e.g. `20 retitled (2 live, 3 minor)`.

| Flag | Effect |
| --- | --- |
| `--include-live` | Show live blogs alongside other rewrites |
| `--live-only` | Only live blogs, as a timeline that includes each blog's first headline (`(first seen)`) |
| `--include-minor` | Also show minor rewrites, marked `[minor]` in the table |
| `--oldest-first` | Chronological order. Reads best with `--live-only`. |

### Live blogs

Live blogs re-headline every time they're updated, so they would crowd ordinary
rewrites out of `changes`. An article counts as a live blog when:

- its URL path has a `live` or `liveblog` segment (Guardian `/live/`, BBC
  `/news/live/`, Al Jazeera `/liveblog/`), or
- its title has a `… live:` style marker (`Live: …`, `Australia news live: …`,
  `… live updates: …`), or
- its URL matches the source's own `live_url_pattern` (see below).

Ordinary uses of the word don't count: "live in harmony" and "play live tonight"
are not live blogs. Once any version of an article looks live, it stays flagged,
because live blogs often drop the marker from their final headline.

Nothing is discarded. Every live-blog headline is kept in the title history.
`list` marks live blogs with `[LIVE]` in tables and `is_live` in JSON and CSV.
`fetch` reports live rewrites separately, e.g. `5 retitled (3 live)`.

Each live-blog headline sums up the latest development, so the history doubles as
a running timeline:

```bash
headliner changes --live-only --oldest-first --since 24h
headliner changes --live-only --source "The Guardian AU" --format json
```

**`migrate`** upgrades an older database to the current schema. Every command
does this on open, so running it by hand is only needed to preview an upgrade
with `--dry-run`. See [Schema upgrades](#schema-upgrades).

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Everything succeeded. A source skipped by `robots.txt` counts as success. |
| `1` | At least one source failed. The rest still ran and were stored. |
| `2` | Configuration error, bad arguments, or a fatal problem. |

### Stories

`headliner stories` groups headlines from different outlets that report the same
event: the Christa Pike execution story, say, as told by RTE, the BBC, The Age,
NPR and eight others.

```bash
headliner stories                    # last 24 hours, stories from 2+ outlets
headliner stories --since 3d --min-sources 4 --tag IE
headliner stories --format json      # every member headline, UTC timestamps
```

Each headline becomes a TF-IDF vector of its title words, plus the start of its
summary at lower weight. Rare shared words, such as names and places, count most.
Headlines are taken oldest first. Each joins the story it is closest to: close
to the story as a whole (cosine >= 0.3), or close to one member (cosine >= 0.4).
Otherwise it starts a new story. A story stops growing after 36 hours without a
new member. Nothing is stored; grouping is recomputed on demand (about 0.3s for
1,000 headlines, and the web viewer caches it until the next fetch).

On a day of real data (1,066 headlines from 29 sources), this found 115 stories
reported by two or more outlets. Spot checks found the large ones correct, with
the occasional wrong member, for example a council campaign that shared "mental
health" with an unrelated story. Treat the groups as a reading aid, not a
verdict. Nine's mastheads (The Age, Sydney Morning Herald, WAtoday) often run
the same headline, so they can inflate a story's outlet count.

## Configuration

`sources.yaml` holds two top-level keys, `settings` and `sources`.

### `settings`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `request_timeout` | number > 0 | `15` | Seconds to wait for one HTTP response |
| `rate_limit_seconds` | number ≥ 0 | `1.0` | Minimum gap between requests to the same domain |
| `user_agent` | string | built-in | Sent on every request; include a contact address |
| `max_items_per_source` | integer > 0 | `50` | Cap on headlines kept per source per run |
| `concurrency` | integer > 0 | `5` | How many sources are fetched at once |

A `Crawl-delay` in a site's `robots.txt` is honoured when it asks for longer than
`rate_limit_seconds`: it becomes the minimum gap between requests to that domain,
including across several sources on the same site. Python's `robotparser` only
reads whole-second values, so a fractional `Crawl-delay` is ignored.

### `sources`

Every entry needs `name`, `url` and `type`.

| Key | Applies to | Required | Meaning |
| --- | --- | --- | --- |
| `name` | both | yes | Unique label. Used by `--only`, `--source`, and stored on each row. |
| `url` | both | yes | Feed URL, or the listing page to scrape. Must be `http` or `https`. |
| `type` | both | yes | `rss` (covers RSS **and** Atom) or `html` |
| `enabled` | both | no | `false` skips the source unless named in `--only`. Default `true`. |
| `article_selector` | html | yes | CSS selector for the element wrapping one story |
| `title_selector` | html | yes | CSS selector for the title, relative to the article element |
| `link_selector` | html | yes | CSS selector for the link, relative to the article element |
| `date_selector` | html | no | CSS selector for the timestamp. Reads `datetime`, then `content`, then the text. |
| `summary_selector` | html | no | CSS selector for a short standfirst or teaser |
| `live_url_pattern` | both | no | Regular expression (case-insensitive) searched in each article URL; a match marks it as a live blog, on top of the built-in rules |
| `tags` | both | no | Labels for filtering with `--tag`: a list (`[AU, business]`) or one string. Letters, digits, `-` and `_`. |
| `include_url_pattern` | both | no | Regular expression (case-insensitive); only items whose URL matches are kept, e.g. `"/news/"` to keep a radio station's news out of a site-wide feed. Applied before `max_items_per_source`. |

Anything else is rejected with an error naming the file and the key, so a typo
fails at startup rather than silently doing nothing.

Relative links are resolved against the source `url`.

### A note on the shipped sources

The bundled `sources.yaml` carries 28 RSS feeds and one HTML source, grouped and
tagged by country: `AU` (11), `IE` (11), `UK`, `US`, `DE`, `FR` and `QA`, plus a
`business` tag. News sites change their feed URLs and their markup without
warning. Run `headliner fetch --dry-run` after cloning to see which ones still
work in your environment, and treat a source that fails consistently as needing
its URL or selectors updated rather than as a bug.

Sites that were checked and can't be fetched are listed in the file with the
reason, so they aren't re-tried by accident. News Corp titles (The Australian,
news.com.au, Herald Sun, Daily Telegraph, Courier Mail) and The Irish Sun
disallow all crawlers in robots.txt. The New Daily sits behind a bot challenge.
Business Post's advertised feeds return its HTML homepage.

## Adding sources

`headliner discover` turns a list of site homepages into source entries.

1. **Discover.** Give it the homepages (or section pages) and the tags to apply:

   ```bash
   headliner discover --tag AU https://www.smh.com.au/ https://www.crikey.com.au/ > new.yaml
   ```

   For each site it reads the feeds the homepage advertises
   (`<link rel="alternate" type="application/rss+xml">`), then falls back to the
   default feed paths of common news platforms: WordPress `/feed/`, Nine
   `/rss/feed.xml`, Reach `?service=rss`, Arc XP and others. It checks
   robots.txt before every request, using your configured `user_agent`, and
   only recommends a feed it has fetched and parsed.

2. **Review `new.yaml`.** It is indented to paste under `sources:`. Each entry
   has a comment with the item count and the newest item's time. Tidy the
   suggested `name`s, which come from the feed's title. Three kinds of comment
   lines need a decision:
   - `# CONFIGURED`: the site is already a source; add the tags to that entry
     instead.
   - `# FAILED`: no usable feed, with the reason. For example "robots.txt blocks
     crawlers…", "HTTP 403, likely bot protection", or "advertised feed …: not
     an RSS/Atom feed (got text/html)". Note these in the config so the site
     isn't re-tried by accident.
   - `include_url_pattern`: suggested when you gave a section URL (such as
     `…/news`) and the feed covers the whole site. It keeps only that section's
     items.

3. **Check, then add.** Paste the entries into `sources.yaml` (and
   `deploy/sources.yaml` for the server), then try them without writing:

   ```bash
   headliner fetch --dry-run --tag AU
   ```

4. **Deploy.** On the server, after `git pull`:

   ```bash
   sudo bash deploy/install-ubuntu.sh --update-sources
   ```

`discover` exits `1` when any site had no usable feed, so it can be scripted.

## Adding a new HTML source

Use `type: html` only when the site publishes no feed. Run `headliner discover`
on it first (see [Adding sources](#adding-sources)), and look in the page source
for `<link rel="alternate" type="application/rss+xml">`.

### 1. Find the repeating article element

Open the listing page, right-click a headline and choose **Inspect**. Walk up the
DOM from the headline text until you reach the element that wraps one story and
repeats for every other story. That is your `article_selector`.

Prefer a semantic tag or a stable-looking class (`article`, `li.story-item`,
`div[data-testid="card"]`) over a generated one. Class names like
`css-1a2b3c` are build output and will change on the site's next deploy.

### 2. Find the title and the link inside it

With one article element selected, find the headline text and the `<a>`. Those
selectors are written **relative to the article element**:

```yaml
article_selector: "article.story"
title_selector: "h3"           # not "article.story h3"
link_selector: "a"
```

`link_selector` reads `href`, then `data-href`, then an `<a href>` nested inside
the matched element — so pointing it at a wrapper usually works. Anchors that are
page-internal (`#…`), `javascript:` or `mailto:` are ignored.

Often the title *is* the link, in which case both selectors are `a`.

### 3. Add the optional selectors

A `<time datetime="…">` element makes `date_selector` worthwhile; without a
machine-readable timestamp, `published_at` is frequently `None` and items sort by
when you fetched them. `summary_selector` picks up a standfirst if the listing
shows one.

### 4. Test the selectors in the browser first

In the DevTools console:

```js
document.querySelectorAll("article.story").length        // expect one per story
const a = document.querySelector("article.story")
a.querySelector("h3")?.textContent.trim()                // expect a headline
a.querySelector("a")?.href                               // expect an article URL
```

If the count is 0, the selector is wrong. If it is far higher than the number of
visible stories, it is matching navigation or footer links too — tighten it.

If `querySelectorAll` finds nothing that you can plainly see on the page, the
content is rendered client-side. `headliner` fetches HTML without running
JavaScript, so that site needs a feed or an API instead.

### 5. Verify from the CLI

```bash
headliner fetch --only "Your New Source" --dry-run --verbose
```

`--verbose` logs each row that was dropped and why. Headlines shorter than ten
characters and rows without a usable link are discarded on purpose; that is what
filters out "More", "Video" and similar navigation chrome.

## Storage

SQLite in WAL mode, no ORM. The schema is created on first run by an idempotent
migration and is safe to re-run.

**`headlines`** — `source`, `title`, `url`, `published_at`, `fetched_at`,
`summary`, `content_hash`.

There is one row per article, keyed on the normalised `url` (`UNIQUE`). URL
normalisation lowercases the scheme and host, drops the fragment, strips tracking
parameters (`utm_*`, `at_*`, `fbclid`, `gclid` and friends) and sorts the rest, so the
same article arriving through two different links stores once. `title`,
`summary` and `content_hash` always hold the most recently seen version;
`fetched_at` is when the article was first seen. `is_live` is `1` once any
version of the article has looked like a live blog.

`content_hash` is a sha256 over the normalised URL and the case-folded,
whitespace-collapsed title. Two titles with the same hash count as the same
title, so a change in case or spacing alone is not a rewrite.

**`headline_revisions`** — every distinct title an article has carried:
`headline_id`, `title`, `summary`, `content_hash` and `seen_at` (first seen).
Each article has at least one revision. When a known URL comes back under a new
title, the article's row takes the new title and a revision is added. If a title
flips back to an earlier wording, the row follows it but no revision is added,
so feeds that alternate between two titles do not grow the history every run.
`headliner changes` reads this table.

**`fetch_log`** — one row per source per run: `source`, `started_at`,
`finished_at`, `status` (`ok`, `skipped`, `error`), `items_found`, `items_new`,
`items_changed` (rewrites seen) and `error`. This is what `headliner sources`
reads.

Search uses FTS5 indexes kept current by triggers: `headlines_fts` over current
titles and summaries, and `headline_revisions_fts` over every version for
`search --history`. On a SQLite build without FTS5 the indexes are not created
and search falls back to `LIKE`.

Querying it directly is fine:

```bash
sqlite3 headlines.db "SELECT source, COUNT(*) FROM headlines GROUP BY 1 ORDER BY 2 DESC;"
```


### Schema upgrades

The schema version is stored in `PRAGMA user_version` and upgrades run when a
database is opened. Version 1 introduced title history. Databases created before
it stored a rewritten headline as a second row. The upgrade:

1. copies the database to `<db>.pre-v1.bak` (only if there are rows to merge),
2. records every existing row's title as a revision of the earliest row with the
   same URL,
3. keeps that earliest row, gives it the latest title, and deletes the others,
4. adds the unique index on `url`.

It runs in one transaction.

Version 2 adds `headlines.is_live` and flags existing live blogs, checking each
article's URL and every title in its history against the built-in rules. It only
adds a column, so no backup is taken. A source's own `live_url_pattern` takes
effect for its articles the next time they are fetched.

Version 3 re-normalises every stored URL. Normalisation now also strips `at_*`
tracking parameters, which the BBC adds to every feed link
(`?at_campaign=rss&at_medium=RSS`). Without this, a change to those values would
make every BBC article look new. The upgrade:

1. copies the database to `<db>.pre-v3.bak` (only if a URL changes),
2. rewrites changed URLs; where two rows turn out to be one article, keeps the
   oldest, moves the other's title history onto it and keeps the latest title,
3. recomputes content hashes, which include the URL, for every row and revision,
4. builds the search index over title history.

To see what an upgrade will do first:

```bash
headliner migrate --dry-run --db /path/to/headlines.db
```

## Being a good citizen

The defaults are deliberately conservative: one request per domain per second,
five sources at a time, `robots.txt` obeyed.

**Put a real contact address in `user_agent` before pointing this at anyone's
servers.** The default carries a placeholder. A site operator who sees unwanted
traffic should be able to reach you rather than having to block you.

`--ignore-robots` exists for the case where you own the site or have permission.
It is off by default and logs a warning every time it is used.

## Development

```bash
pip install -e '.[dev]'
pytest                      # no network access; everything is mocked or a fixture
ruff check headliner tests
ruff format headliner tests
mypy --strict headliner
```

Tests parse committed fixtures under `tests/fixtures/` and mock the HTTP layer
with `respx`, so the suite is offline and deterministic.

## Layout

```
headliner/
  cli.py        argparse commands, output formatting, exit codes
  config.py     sources.yaml loading and validation
  models.py     Headline dataclass, hashing, text and URL normalisation
  fetcher.py    async http, robots.txt, retries, rate limiting
  parsers.py    rss/atom via feedparser, html via selectolax or beautifulsoup4
  store.py      sqlite schema, inserts, queries
  discover.py   feed discovery for `headliner discover`
  stories.py    grouping headlines into stories across outlets
  web.py        read-only web viewer for `headliner web` (standard library WSGI)
  static/       the viewer's stylesheet
tests/
  fixtures/     one RSS sample, one HTML sample
sources.yaml
```

## Licence

MIT.
