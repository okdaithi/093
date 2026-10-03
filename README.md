# headliner

A small CLI that collects news headlines from a configurable list of sites,
normalises them, and stores them in SQLite. It reads RSS/Atom feeds and, where
no feed exists, scrapes a listing page with CSS selectors you supply. It can
also read a publisher's live front page next to its feed, to catch the stories
the feed leaves out and record where each one sits on the page (see
[Front pages](#front-pages)).

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
- Optional front-page reading per source: headlines in page order, merged with
  the feed's articles rather than duplicated, with how each article was found
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
also prints the deployed Git commit and, when GitHub can provide it, the
associated pull request's title, status, last update, merge time, and link.
This lookup is informational and does not block installation; without GitHub
access the commit is still shown with a note that PR information is unavailable.
It does not enable the timer automatically. First replace `you@example.com` in
`/etc/headliner/sources.yaml` with a monitored contact address, then enable the
schedule and optionally perform the initial run:

```bash
sudoedit /etc/headliner/sources.yaml
sudo systemctl enable --now headliner.timer
sudo systemctl start headliner.service
```

Inspect timer state with `systemctl list-timers headliner.timer` and logs with
`journalctl -u headliner.service`. To deploy an update, rerun
`sudo bash deploy/install-ubuntu.sh` from the updated checkout; it keeps the
database and brings `/etc/headliner/sources.yaml` up to date with the shipped
`deploy/sources.yaml`, so new sources, groups and retired feeds arrive with
each deploy. To keep a hand-edited live file instead, pass `--keep-sources`:
the shipped list is then only written next to it as
`/etc/headliner/sources.yaml.dist`, with a note when the two differ:

```bash
sudo bash deploy/install-ubuntu.sh --keep-sources
sudo diff -u /etc/headliner/sources.yaml /etc/headliner/sources.yaml.dist
```

The update replaces the live file with the shipped one but keeps its `user_agent`
line, so your contact address survives. The previous file is saved as
`sources.yaml.bak-<timestamp>` and the diff is printed. If the result doesn't
load, the backup is put back and the installer stops. Other local edits are not
merged, so check the diff if you have them.

## Web viewer

`headliner web` serves a read-only view of the database in a browser:

| Page | Shows |
| --- | --- |
| **Briefing** | The home page. A health line (all normal, or what needs attention, from the same checks as `/api/status`); the stories reported by the most outlets in the last 12 hours; then, by country, the stories that country's outlets lead on (the larger their share of a story's outlets, the higher it ranks, and each story appears once); and the notable rewrites of the last 24 hours (no live blogs, no punctuation-only changes). |
| **Latest** | Headlines newest first, grouped by day, with links to the articles. A `LIVE` badge marks live blogs, and "N titles" marks rewritten articles. |
| **Stories** | Headlines from different outlets grouped into stories (see [Stories](#stories)): most widely covered first or newest first, with outlets per tag ("AU 3 · IE 1") and each outlet's headline. Latest and Search mark grouped headlines "N outlets", linking to the story. |
| **Rewrites** | Each title change as a word-level diff (removed words struck through, added words highlighted), with the old title underneath. Live blogs and minor (punctuation-only) changes are hidden by default, as in `changes`; the page says how many of each were hidden and lets you show them. |
| **Search** | Full-text search over current titles and summaries, with matches highlighted. Tick *Include earlier titles* to search every version (like `search --history`). |
| **Trends** | Over 7, 14 or 30 days: the article total against the previous period; **rising topics** (headline words much more common in the last 24 hours than over the period, with a sparkline and a search link); the **biggest stories**; **articles per day** per source (heatmap by local day, top 20 sources then the rest, each cell linking to that day's articles, ▲/▼ against the previous period), and **by hour of day**; **rewrites by outlet**, meaning the share of each outlet's articles that were later reworded (punctuation-only changes and live blogs excluded) and the median delay; **feed turnover and reliability**, meaning ok/failed/skipped runs, items per run, how much of each feed is new per run, and how many runs were entirely new. A feed that is entirely new run after run is likely dropping stories between runs, so fetching more often would help. Each source's first-ever run is left out. |
| **Story** | One story: each outlet's headline oldest first, timed from the first report, with words most outlets share faded and words only that outlet used highlighted; then every headline with its times. |
| **Source profile** | `/source?name=…`, linked from source names: state, tags and group; articles per day and by hour over 14 days; how often it rewrites and how fast; multi-outlet stories it reported first this week; and the headline words it uses far more than other outlets. |
| **Sources** | Each source's tags, group, item count, last success and last run status: `ok`, `failed` (with the error), `skipped`, `stale` (no success in 13 hours), `content stale` (fetches fine, but the newest item is over 3 days old: the publisher has probably frozen the feed) or `never fetched`. Below it, the last 12 runs with their ok/skipped/failed counts and new and retitled items. |
| **Article** | Every title one article has carried, oldest first, each diffed against the one before. Reached from "N titles" or "all titles". |

Every list filters by tag (any of those ticked), source and time window, and
filters are kept as you move between pages. Tags are grouped into countries (two
capital letters, such as `AU`) and regions & topics, and the source menu is grouped
by each source's country. On phones the tags and sources panel starts closed; active
filters are always shown as chips underneath, and following a chip removes it. A
story is named after its most typical headline, but never a video, gallery or live
item ("Watch: …") when an ordinary article is in the group. Times follow the CLI:
local time with the zone named, a link to switch to UTC (in the header, and in the
footer on phones), and the UTC timestamp on hover.

A small script (`static/app.js`, served by the viewer itself) adds three
conveniences; every page works the same without it:
- Filters apply as soon as a box is ticked or a menu changed, so there is no
  *Apply* button (it stays on the Search page and without JavaScript).
- The tags and sources panel opens on wide screens.
- **New since your last visit:** articles and stories first fetched after your
  previous visit get an accent bar, a divider marks where the earlier ones start,
  and the Latest tab shows how many are new. The time of your visit is kept in
  your browser (`localStorage`) only; the count comes from `/api/new?since=<ISO
  time>`, which returns `{"latest": N}`.

The viewer opens the database read-only, so it can't change it, and only answers
`GET`/`HEAD`. Pages are plain HTML and CSS plus that one script, with no inline
script and no third-party requests, under a strict Content-Security-Policy
(scripts, styles and requests from the viewer itself only). Article links open in a new tab
without sending a referrer. It needs no packages beyond headliner's own.
`/healthz` returns JSON (`status`, `schema`, `articles`, `last_fetch`) for
monitoring. `/api/status` returns everything a status check needs, readable
without access to the database:
- `status`: `ok`, or `attention` with the reasons listed in `checks`. Reasons
  include a run that is late or had failures, sources that need attention, and
  missing or stale backups.
- the last 8 runs
- per-source states, plus details for any source that isn't `ok`
- database size and counts
- the newest backup

Sources that are only `skipped` (by robots.txt) are listed, but don't count
towards `attention`.

```bash
curl -s https://<host>:8444/api/status | python3 -m json.tool
```

Run it locally against any database:

```bash
headliner web --db headlines.db --sources sources.yaml   # http://127.0.0.1:8090/
```

`--host` defaults to `127.0.0.1`; `--host 0.0.0.0` serves the whole network.
There is no login, so only listen on networks you trust.

**On the server**, the installer adds `headliner-web.service`, which runs as the
`headliner` user on port 8090, on localhost only. Enable it once:

```bash
sudo systemctl enable --now headliner-web.service
```

Redeploys restart it on the new code. To reach it over Tailscale with HTTPS:

```bash
sudo tailscale serve --bg --https 8444 http://127.0.0.1:8090
```

The service listens on `127.0.0.1` only, so Tailscale Serve is the way in. To serve
the home network as well, override the command with `sudo systemctl edit
headliner-web.service` (`ExecStart=` then the same line with `--host 0.0.0.0`; an
override survives redeploys, and a VPN kill switch may still block inbound LAN
traffic). Logs: `journalctl -u headliner-web.service`.

**Keyboard shortcuts:** `j`/`k` move through items, `o` or Enter opens one,
`/` searches, `g` then `b`/`l`/`s`/`t`/`r`/`o` goes to Briefing, Latest, Stories,
Trends, Rewrites or Sources, and `?` lists them. Tables with clickable column
headers (Sources, Trends) sort by that column.

**Which version is running?** The installer records the deployed commit, the
latest merged pull request and the install time in `headliner/_build.json`
inside the installed package. The viewer shows it in every page's footer, in
full under "About this build" on the Sources page, and as `build` in
`/healthz` and `/api/status` (`curl -s http://127.0.0.1:8090/healthz | jq .build`).
The pull request comes from GitHub when it can be reached, otherwise from the
latest "Merge pull request #N" commit in local history. A checkout run in place
shows "Development build".

## Health watchdog

`headliner watchdog` checks the machine headliner runs on and keeps a small state
file, so a lasting problem is reported once, repeated rarely, and cleared when it
ends. The installer enables `headliner-watchdog.timer` (every 10 minutes, as root)
when `headliner.timer` is enabled. Try it by hand:

```bash
sudo /opt/headliner/venv/bin/headliner watchdog --no-heal --exit-status \
    --sources /etc/headliner/sources.yaml --db /var/lib/headliner/headlines.db
sudo cat /var/lib/headliner/watchdog.json        # last results, and events not yet delivered
journalctl -u headliner-watchdog.service -p err  # alerts and repairs
```

| Check | Fails or warns when |
| --- | --- |
| `dns` | none of a few test hosts resolves (and says whether ProtonVPN's own resolver answers) |
| `internet` | TCP to 1.1.1.1 and 9.9.9.9 on 443 fails: no routing at all |
| `vpn` | the WireGuard handshake on `proton0` is over 3 minutes old |
| `fetch` | the last run finished over 7 hours ago, or over half its sources failed |
| `sources` | a source failed its last 3 runs while most others worked (warning) |
| `units` | the fetch/backup timers or the viewer service are not active, or a headliner unit failed |
| `viewer` | `http://127.0.0.1:8090/healthz` does not answer ok |
| `backups`, `disk` | no backup for 36 hours or the data disk is 90% full (warnings) |

A failure is announced after about 10 minutes, a warning after about an hour,
reminders repeat every 6 hours, and a recovery is announced after any alert. When
`dns` has failed for ten minutes the watchdog flushes the DNS caches and restarts
`systemd-resolved` (at most once an hour, `--no-heal` to turn it off). It never
touches the VPN.

### Notifications (GitHub issues)

With a token in `/etc/headliner/github-token` the watchdog tells you through
GitHub issues on `okdaithi/093`, which GitHub emails to you (nothing else is
contacted):

- **One issue per problem**, titled `headliner health: DNS not resolving` and so
  on, with the detail and a first thing to try. Reminders are comments; when the
  check passes again it comments "Recovered after ..." and closes the issue.
  While the network is down nothing can be sent, so events wait in the state
  file's `pending` list and arrive, in order, once it is back.
- **A heartbeat issue**, `headliner heartbeat (machine-written, do not close)`,
  whose body is rewritten every 10 minutes with `last_ok:` (edits send no
  email). The cloud routine "headliner dead-man's switch (hourly)" reads it and
  opens `headliner: NUC silent (no heartbeat)` if `last_ok` is over 45 minutes
  old, which covers a dead machine, network or watchdog; it closes that issue
  when the heartbeat returns.
- **A banner** on every viewer page while an announced problem lasts (or the
  watchdog itself has been silent for 35 minutes), and `watchdog` in
  `/api/status`.

Create the token on GitHub (Settings, Developer settings, Fine-grained tokens):
repository access **only `okdaithi/093`**, permission **Issues: Read and write**,
expiry 90 days. Then install it without it appearing on a command line or in
shell history:

```bash
read -rsp 'Paste token: ' T; echo
printf '%s' "$T" | sudo install -m 0600 -o root -g root /dev/stdin /etc/headliner/github-token
unset T
```

Without the file the watchdog still checks, logs and shows the banner; it just
sends nothing. `--no-notify` turns sending off, `--github-repo` and
`--github-token-file` change where it goes.

### Failure modes and what to do

| What happened | What you see | First steps |
| --- | --- | --- |
| DNS or the VPN path is down | `fetch` exits 3 and logs `network down`; Briefing/`/api/status` say "network down"; issue "DNS not resolving"; after 10 min the watchdog flushes DNS caches and restarts `systemd-resolved` | `resolvectl status`; before reconnecting ProtonVPN run `sudo wg show proton0 latest-handshakes` (a stale handshake means the tunnel stalled); a catch-up fetch runs 10 min after the failed one |
| Raw routing is down | issue "no internet routing" (`dns` and `internet` both fail) | `ip route`, the ProtonVPN connection |
| Some feeds fail | `sources` warning after about an hour (3 runs in a row), shown on the Sources page | open the feed URL; it has probably moved or is blocking us |
| A run did not happen | issue "fetch runs failing or late" | `systemctl status headliner.timer`, `journalctl -u headliner.service -n 50` |
| The viewer or a timer is down | issue "web viewer not answering" or "services not running" | `systemctl status headliner-web.service headliner.timer headliner-backup.timer` |
| The NUC, its network or the watchdog is down | no heartbeat for 45 minutes: the cloud routine opens "headliner: NUC silent (no heartbeat)" (checked hourly, so allow up to about 2 hours) | power, `systemctl status headliner-watchdog.timer`, then the DNS steps above; if only the GitHub token is bad, `journalctl -u headliner-watchdog.service` says "cannot notify via GitHub" |

### Drills

`deploy/drill.sh` breaks one thing at a time, in a controlled and reversible way,
and prints PASS or FAIL. It uses scratch copies under `/var/lib/headliner/drill`,
never the real database or watchdog state, and deletes nothing.

```bash
sudo bash deploy/drill.sh fetch-dns      # fetch with no network: exit 3, 'network down' logged
sudo bash deploy/drill.sh watchdog-dns   # watchdog with no network: dns/internet fail, alert queued
sudo bash deploy/drill.sh viewer         # stops headliner-web for ~20 s; restarted on exit
sudo bash deploy/drill.sh notify         # sends a real test issue (opened, closed) to GitHub
sudo bash deploy/drill.sh all
```

The one chain the script cannot safely fake is the dead-man's switch. To test it
end to end, stop the heartbeat and wait: `sudo systemctl stop
headliner-watchdog.timer`; about 45 minutes later, at the next `:17`, the cloud
routine opens "headliner: NUC silent (no heartbeat)". Then `sudo systemctl start
headliner-watchdog.timer`; at the following `:17` the routine closes it.

## Backups

Title history can't be fetched again, so back the database up.
`headliner backup` uses SQLite's online backup API, which takes a consistent
snapshot even while a fetch is writing. Each copy is:
- private (mode 0600) from its first byte
- checked with `PRAGMA integrity_check` before it is renamed into place, so a
  half-written or corrupt file is never mistaken for a backup

By default copies go into `backups/` next to the database, named
`headlines-<UTC time>.db`. The newest copy from each of the last 7 days and
the last 4 weeks is kept (`--keep-daily`, `--keep-weekly`); others are deleted.

On the server, the installer adds `headliner-backup.timer`, which runs daily at
03:30, and enables it once `headliner.timer` is enabled. Backups land in
`/var/lib/headliner/backups/`. They protect against corruption and bad
upgrades, not against losing the disk; copy them elsewhere if that matters.
To restore:

```bash
sudo systemctl stop headliner.timer headliner-web.service
sudo -u headliner cp /var/lib/headliner/backups/headlines-<time>.db /var/lib/headliner/headlines.db
sudo rm -f /var/lib/headliner/headlines.db-wal /var/lib/headliner/headlines.db-shm
sudo systemctl start headliner.timer headliner-web.service
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
| `headliner frontpages` | Read front pages without storing anything and judge whether each is worth enabling (see [Front pages](#front-pages)) |
| `headliner changes` | List headlines that were rewritten after publication, newest first |
| `headliner migrate` | Upgrade the database schema (runs automatically; `--dry-run` previews) |
| `headliner discover URL...` | Find each site's RSS/Atom feed and print source entries to paste (see [Adding sources](#adding-sources)) |
| `headliner stories` | Stories reported by several outlets, most widely covered first (see [Stories](#stories)) |
| `headliner web` | Serve a read-only web viewer (see [Web viewer](#web-viewer)) |
| `headliner backup` | Copy the database to a verified, private backup and prune old ones (see [Backups](#backups)) |

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
| `--no-front-pages` | Read feeds only; skip every configured front page this run. |
| `--front-pages-only` | Read only the front pages of sources that have one; skip feeds. |

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
source's tags, and a front-page state for sources that have one. JSON keeps its
existing fields and adds an `rss` object (feed state) and a `front_page` object
(`null` when none is configured):

```json
{
  "name": "Irish Times",
  "rss": {"status": "ok", "last_success": "…", "newest": "…"},
  "front_page": {"url": "https://www.irishtimes.com/", "status": "healthy",
                 "headlines": 50, "new": 43, "merged_with_feed": 7,
                 "response_ms": 226, "http_status": 200, "error": null, "…": "…"}
}
```

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
| `3` | Nothing could be fetched because DNS or the network is down (every source failed with a DNS or connection error). Retry later; see below. |

When DNS or the VPN path is down, every feed fails the same way, and fetching
through it only wastes a run. `fetch` therefore first checks that up to three of
the sources' host names resolve. `--wait-network MINUTES` keeps checking (30 s,
1 min, 2 min, then every 4 min) before giving up; the installed service uses 15.
If the network never comes back, no feed is requested, each source gets a
`network down` error in the fetch log, and the exit code is `3`. The Briefing and
`/api/status` then say "network down" (and `"network_down": true`) instead of
"66 source(s) need attention". An exit of `3` also starts
`headliner-retry.service`, which schedules one catch-up fetch
(`headliner-catchup.service`) 10 minutes later.

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
| `type` | both | yes | `rss` (covers RSS **and** Atom), `html`, or `front_page` (no feed: only the front page is read; see [Front pages](#front-pages)) |
| `enabled` | both | no | `false` skips the source unless named in `--only`. Default `true`. |
| `article_selector` | html | yes | CSS selector for the element wrapping one story |
| `title_selector` | html | yes | CSS selector for the title, relative to the article element |
| `link_selector` | html | yes | CSS selector for the link, relative to the article element |
| `date_selector` | html | no | CSS selector for the timestamp. Reads `datetime`, then `content`, then the text. |
| `summary_selector` | html | no | CSS selector for a short standfirst or teaser |
| `live_url_pattern` | both | no | Regular expression (case-insensitive) searched in each article URL; a match marks it as a live blog, on top of the built-in rules |
| `tags` | both | no | Labels for filtering with `--tag`: a list (`[AU, business]`) or one string. Letters, digits, `-` and `_`. |
| `group` | both | no | Publisher group, such as `nine` for The Age, SMH and WAtoday. Mastheads in one group share a newsroom or copy, so a story they all carry counts as one outlet in story counts, rankings and badges. |
| `include_url_pattern` | both | no | Regular expression (case-insensitive); only items whose URL matches are kept, e.g. `"/news/"` to keep a radio station's news out of a site-wide feed. Applied before `max_items_per_source`. |
| `front_page` | all | no | Also read the publisher's live front page: a mapping, see [Front pages](#front-pages) |

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
   sudo bash deploy/install-ubuntu.sh
   ```

`discover` exits `1` when any site had no usable feed, so it can be scripted.

### Adding many sources at once: batch files

For more than a handful of sites, paste them into a batch file under group header
lines. Each header line's words become the tags of the URLs below it:

```text
# batches/2026-10-03-global.txt
IE galway
https://www.galwaybeo.ie/
https://www.galwaydaily.com/news/

asia
https://japantoday.com/                  JP
https://asia.nikkei.com/                 JP business

cn-intel CN
http://www.cicir.ac.cn/NEW/index.html    think-tank lang-zh
```

- **Tags.** Two-letter tags are country codes and are upper-cased (`cn` becomes
  `CN`); all others are lower-cased. Words after a URL add tags to that site
  alone, and a country code there replaces the group's (SCMP is `HK` in a `cn`
  group). Country tags are listed first.
- **Duplicates.** Repeated sites (the same host and path, over http or https)
  are checked once. A feed that two sites lead to is kept for the first, and a
  feed already in the config is reported as configured.
- **Feeds on other hosts.** Publishers often serve feeds from another host
  (`rss.nytimes.com`, `feeds.a.dj.com`). Put the feed URL itself in the batch
  file and it is checked like any other candidate.
- **Sections.** A section URL on a site that is already configured
  (`https://www.rte.ie/news/galway/`) is still searched for a feed of its own.

```bash
headliner discover --batch batches/2026-10-03-global.txt --report report.md > new.yaml
```

`new.yaml` holds the entries grouped by header, ready to paste after tidying
names. `report.md` is a triage table with one row per site: found (with the
feed and item count), already configured, or failed (with the reason). Keep
batch files in `batches/` as a record of what was requested and why sites were
or weren't added.

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

## Front pages

**The feed stays the primary way in.** A front page is an optional second
acquisition path, for sources whose feed is incomplete, late, or missing the
stories the publisher is leading with. Enabling it never changes how the feed
is fetched or stored, and a front page that cannot be read only costs its own
headlines: the feed's items are stored, the run's exit code is the feed's, and
the source stays configured.

```
                ┌──────────────┐
                │ News source  │
                └──────┬───────┘
          ┌────────────┴────────────┐
   RSS/Atom feed               live front page
   (fetch_log)                (front_page_log)
          └────────────┬────────────┘
           normalise (models.Headline)
                       │
     store: one article per URL; a front-page link merges
     into the article it already is (URL variant, the
     publisher's other domain, or the same headline words)
                       │
         list / search / stories / web viewer
```

### Configuring

Add a `front_page` block to any source:

```yaml
# RSS plus the front page, generic extraction (no selectors needed)
- name: Irish Times
  url: https://www.irishtimes.com/arc/outboundfeeds/feed-irish-news/
  type: rss
  front_page:
    url: https://www.irishtimes.com/

# With selectors, for a page the generic extractor reads badly
- name: Example News
  url: https://example.com/feed.xml
  type: rss
  front_page:
    enabled: true
    url: https://example.com/
    article_selector: "article.story"
    title_selector: "h2 a"
    link_selector: "h2 a"
    section_selector: ".kicker"       # optional
    published_selector: "time"        # optional; reads datetime, content, then text
    image_selector: "img"             # optional

# RSS only: the same as leaving the block out
- name: RSS Only Source
  url: https://example.com/feed.xml
  type: rss
  front_page:
    enabled: false

# No feed at all: the front page is the source
- name: Front Page Source
  type: front_page
  url: https://example.com/
```

| Key | Required | Meaning |
| --- | --- | --- |
| `url` | when enabled | The front page. `http` or `https`. For `type: front_page`, give it here or as the source's `url`. |
| `enabled` | no | `false` keeps the block but does not read the page. Default `true`. |
| `article_selector` | no | CSS selector for one story's element. Without it, generic extraction is used. |
| `title_selector`, `link_selector`, `section_selector`, `published_selector`, `image_selector` | no | Looked up inside each `article_selector` match, so they need one. Without `title_selector` the story's heading is used; without `link_selector` its first link. |

Selectors are checked when the file loads: an invalid one is an error naming
the source and key, as is any unknown key. `live_url_pattern`,
`include_url_pattern`, `tags` and `group` apply to front-page headlines too.
The shipped sources enable front pages on ten sources that passed validation:
ABC News AU, The Guardian AU, WAtoday, The West Australian, RTÉ News, Irish
Times, TheJournal.ie, BBC News, Al Jazeera and NPR News.

### How headlines are found

In layers, keeping the order of the page (position 1 is the first story):

1. **Configured selectors**, when given and when they match anything. If they
   match nothing, the run says so and falls back to the generic layers.
2. **Every link on the page, scored** on where it sits and what it looks like:
   inside `<article>` or schema.org `Article` markup, inside a heading, or in a
   story/card/teaser/promo container; an article-like URL (a date, an id, a
   long hyphenated slug); and enough words to be a headline.
3. **JSON-LD** (`NewsArticle`, `Article`, `ItemList`, …) and **microdata** add
   publication dates, sections and images to those links, and stand in for
   them when a page lists its stories only as metadata.

Never taken as headlines: links inside `<nav>`, `<footer>`, `<menu>`, the
page's own `<header>`, `role="navigation"` and similar, or containers whose
class or id says menu, breadcrumb, social, share, subscribe, login, account,
cookie, consent, pagination, newsletter or advert; navigation words ("Home",
"Subscribe", "Log in", "Podcasts", "Weather", "Sport", "Read more", …); tag,
topic, author and section index pages; links to other sites; comment jump
links; audio, video and image files. Kickers, labels, bylines and teasers are
taken off a card's headline, and an empty "overlay" link takes its card's
heading.

Each link's URL is resolved (relative, protocol-relative, `<base href>`) and
normalised as feed links are: tracking parameters (`utm_*`, `fbclid`,
`gclid`, …) and fragments are dropped, other query parameters kept. A story
linked several times on the page (picture, heading, "12 comments", an AMP copy,
live-blog `?update=` links) is kept once, at its highest position, preferring
the link in its heading for the title. Two different headlines are never
merged just for being similar ("kills two" and "kills three" stay apart).

A section is recorded only when the page states one (JSON-LD `articleSection`,
a `section_selector`, or a leading URL segment such as `/world/`), mapped onto
news, world, politics, business, markets, technology, science, sport, culture,
opinion, analysis, or `other` for a stated section outside that list.

### One article, two ways of finding it

A front-page headline is the article already stored when its URL matches, a
variant of it does (http/https, `www.`/`m.`/`amp.` hosts, `/amp` paths, AMP
query switches, doubled slashes, tracking parameters), the publisher's other
domain has the same path (bbc.com and bbc.co.uk), or it has the same headline
words as one of that source's articles from the last week. Matching never
crosses sources.

A match records that the front page found it, at which position and when; the
feed's URL and title stand. Front pages often show a shorter display headline,
which is **not** counted as a rewrite. An article found only on the front page
is stored with the front page's headline (and its later rewrites there are
tracked); if the feed later carries it, the feed's URL and title take over and
its title history restarts from the feed's wording.

Each article records how it was found (`acquisition`): `rss`, `html`,
`front_page`, or several. `list --format json` and the viewer show it; the
viewer marks front-page articles with a small "front page" badge whose tooltip
gives the position, and the article page says "Found on the feed and the front
page (#3 …)". Articles stored before schema 5 are recorded as `rss`.

### Fetching politely and safely

- `robots.txt` is obeyed for the front page and for any redirect to another
  site; a disallowed page is `robots_denied` and never requested.
- The shared per-domain rate limit (`rate_limit_seconds`, or a longer
  `Crawl-delay`) applies, and only one page or feed request per domain is in
  flight at a time. Connections are reused across the run, and `concurrency`
  bounds how many sources are worked on at once.
- `If-None-Match`/`If-Modified-Since` from the last good read are sent; a `304`
  is recorded as `not_modified` and costs no parsing.
- Redirects are followed one hop at a time (at most 5): only `http(s)`, and
  never to a loopback, private, link-local or otherwise non-public address
  (the same check covers the first request). Pages over 5 MB are abandoned.
- Bot protection is never worked around. 401, 403, 429 and 451, and challenge
  pages (Cloudflare, Akamai, PerimeterX, DataDome, Imperva), are `blocked`. A
  500, 502 or 504 is retried once; a `429` or a block never is.
- Pages are only parsed: no script runs, nothing from the page is written to
  disk, and no browser is used. A page that builds its stories with JavaScript is
  reported as `rendering_required`, not silently as empty; the feed carries on.
  A browser-rendering backend could be added behind `fetch_front_page` later.

### States

| State | Meaning |
| --- | --- |
| `healthy` | Read and headlines found (`not_modified` after a 304 also counts) |
| `stale` | Healthy last time, but that was over 13 hours ago |
| `blocked` | 401/403/429/451 or a bot challenge |
| `robots_denied` | robots.txt disallows the page (or a redirect target) |
| `timeout` | No complete response within `request_timeout` |
| `network_error` | DNS, connection or TLS failure |
| `http_error` | Any other 4xx/5xx, or too many redirects |
| `parse_error` | Not HTML, too large, or unparseable |
| `no_headlines` | Read fine, but nothing headline-like on it |
| `rendering_required` | Script-built page; would need a browser |
| `unsafe_url` | Points at a non-public address |
| `disabled` | `enabled: false` |

Every read is logged in `front_page_log` (status, HTTP status, final URL,
response time, headlines, new and merged counts, ETag) and in the journal:

```
INFO source="Irish Times" method="front_page" status="healthy" http=200 headlines=50 ms=226
WARNING source="Sky News" method="front_page" status="blocked" http=403 ms=74 error="HTTP 403"
INFO source="Irish Times" rss=50 front_page=50 overlap=7 new_from_front_page=43
INFO front pages: 10 attempted, 8 healthy, 2 not_modified; 353 headline(s), 0 new, 141 merged with feed articles
```

The Sources page has a "Front page" column, `headliner sources` a `FRONT PAGE`
column and JSON object, and `/api/status` a `front_pages` section (counts by
state and every page that is not healthy). Front-page problems are reported but
never put the status at "attention" or raise a watchdog alert: the feed is what
the run depends on.

### Validating a front page before enabling it

```bash
# A site you are considering (generic extraction, nothing stored)
headliner frontpages --url https://www.example.com/ --show 10

# Every configured front page, as JSON
headliner frontpages --format json

# Just some
headliner frontpages --only "Irish Times" "BBC News"
```

It reads each page (no conditional request), extracts it, opens the first
`--follow N` articles (default 2; robots and rate limits apply, at least 2
seconds apart per domain) and checks they load and name themselves as the
linked URL (`<link rel=canonical>` or `og:url`). A page is **valid** when it was
read, gives at least 5 headlines, at least 80% of its links look like article
URLs, headlines average 20 characters or more, no more than 80% of links were
duplicates, and at least one article could be followed. It reports
`headline_count` (links found), `unique_headline_count`,
`valid_article_url_count`, `accessible_article_count`, `canonical_matches`,
`mean_headline_length`, `duplicate_rate` and `front_page_response_time`, and
exits 1 when any page is invalid. A front page need not carry as many stories
as the feed.

### Troubleshooting

- **`blocked`**: the site refuses automated readers. Leave the front page off;
  the feed still works. Do not use `--ignore-robots` or change the user agent to
  get round it.
- **`rendering_required`** or **`no_headlines`**: the stories are drawn by
  JavaScript, or sit in markup the generic layers don't recognise. Look at the
  page source (not the browser's live DOM); if the headlines are there, add an
  `article_selector` (and `title_selector`) as for an [HTML source](#adding-a-new-html-source).
- **Navigation or section links among the headlines**: check with
  `headliner frontpages --only NAME --show 30`, then add selectors, or
  `include_url_pattern` to keep only article URLs.
- **Kickers in titles** ("EXCLUSIVE Storm hits…"): the page puts the label in
  the heading itself. It only affects front-page-only articles: when the feed
  has the article, the feed's title is used.
- **Many `new` front-page articles**: normal. Front pages carry features,
  explainers and older stories that the feed has already dropped.
- **The viewer says the schema is too old after a deploy**: the installer runs
  `headliner migrate`; otherwise run it as the service user
  (`sudo -u headliner /opt/headliner/venv/bin/headliner migrate --db /var/lib/headliner/headlines.db`).

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

Version 4 stores, on each revision, the title it replaced (`prev_title`) and
whether the change was minor (`is_minor`). Rewrite queries become plain indexed
filters instead of a window function over the whole history: on 100,000
revisions the Rewrites page drops from about 3 seconds to 0.16. It also adds
`fetch_log.newest_item`, the newest publication date in each fetched feed, so
feeds that stop updating can be spotted (see the Sources page). The upgrade
only adds columns derived from stored data, so no backup is taken; it takes
about 4 seconds per 100,000 revisions.

Version 5 adds how each article was found (`headlines.acquisition`: `rss`,
`html` and/or `front_page`), its last front-page position and when it was seen
there, any section and image the front page gave, and the `front_page_log`
table. It only adds columns and a table, so no backup is taken; existing
articles are recorded as found by `rss`. The installer runs the upgrade as the
service user, so the read-only viewer does not refuse the database after a
deploy.

To see what an upgrade will do first:

```bash
headliner migrate --dry-run --db /path/to/headlines.db
```

## Being a good citizen

The defaults are deliberately conservative: one request per domain per second,
five sources at a time, `robots.txt` obeyed. Front pages follow the same rules,
send conditional requests, and never work around bot protection (see
[Fetching politely and safely](#fetching-politely-and-safely)).

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
  frontpage.py  live front pages: extraction, safe fetching, validation
  store.py      sqlite schema, inserts, queries
  discover.py   feed discovery for `headliner discover`
  stories.py    grouping headlines into stories across outlets
  web.py        read-only web viewer for `headliner web` (standard library WSGI)
  static/       the viewer's stylesheet
tests/
  fixtures/     an RSS sample, an HTML listing sample and a front page
sources.yaml
```

## Licence

MIT.
