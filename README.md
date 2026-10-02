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
- Idempotent storage: re-running never duplicates a headline
- One failing source never aborts the run

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
with up to five minutes of timer jitter. No inbound ports or web server are
required.

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

# Which sources are configured and when each last worked
headliner sources
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

Shared flags: `--sources PATH` (default `sources.yaml`), `--db PATH` (default
`headlines.db`), `--verbose` for DEBUG logging, `--quiet` for errors only.

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
reads the database.

**`sources`** takes `--format table|json`.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Everything succeeded. A source skipped by `robots.txt` counts as success. |
| `1` | At least one source failed. The rest still ran and were stored. |
| `2` | Configuration error, bad arguments, or a fatal problem. |

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
`rate_limit_seconds`.

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

Anything else is rejected with an error naming the file and the key, so a typo
fails at startup rather than silently doing nothing.

Relative links are resolved against the source `url`.

### A note on the shipped sources

The bundled `sources.yaml` carries ten RSS feeds and one HTML source. News sites
change their feed URLs and their markup without warning. Run
`headliner fetch --dry-run` after cloning to see which ones still work in your
environment, and treat a source that fails consistently as needing its URL or
selectors updated rather than as a bug.

Two entries are flagged in the file itself: the AP feed is a community mirror
rather than a first-party feed, and Reuters has been winding down public RSS.

## Adding a new HTML source

Use `type: html` only when the site publishes no feed. Check the obvious places
first — `/rss`, `/feed`, `/rss.xml`, `/atom.xml` — and look in the page source
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

`content_hash` is a sha256 over the normalised URL and the case-folded title,
with a `UNIQUE` constraint, and inserts use `ON CONFLICT DO NOTHING`. URL
normalisation lowercases the scheme and host, drops the fragment, strips tracking
parameters (`utm_*`, `fbclid`, `gclid` and friends) and sorts the rest, so the
same article arriving through two different links stores once.

**`fetch_log`** — one row per source per run: `source`, `started_at`,
`finished_at`, `status` (`ok`, `skipped`, `error`), `items_found`, `items_new`
and `error`. This is what `headliner sources` reads.

Search uses an FTS5 index over titles and summaries, kept current by triggers.
On a SQLite build without FTS5 the index is not created and search falls back to
`LIKE`.

Querying it directly is fine:

```bash
sqlite3 headlines.db "SELECT source, COUNT(*) FROM headlines GROUP BY 1 ORDER BY 2 DESC;"
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
tests/
  fixtures/     one RSS sample, one HTML sample
sources.yaml
```

## Licence

MIT.
