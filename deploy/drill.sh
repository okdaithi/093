#!/usr/bin/env bash
# Fault drills for headliner's failure handling. Run as root on the NUC:
#
#   sudo bash deploy/drill.sh [fetch-dns|watchdog-dns|viewer|notify|all]
#
# Each drill breaks one thing in a controlled, reversible way, checks that
# headliner reacts as designed, and prints PASS or FAIL. Nothing touches the
# real database or the real watchdog state (scratch copies live in
# /var/lib/headliner/drill), and nothing is deleted. The only service touched is
# headliner-web.service (stopped for ~20 s by the "viewer" drill, and restarted
# on exit even if the drill is interrupted). "notify" sends a real test issue
# to GitHub (opened, commented on, closed within seconds) and so emails you.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo bash $0 ${*:-all}" >&2
    exit 2
fi

headliner_bin=/opt/headliner/venv/bin/headliner
python_bin=/opt/headliner/venv/bin/python
drill_dir=/var/lib/headliner/drill
sources_file=/etc/headliner/sources.yaml
failures=0

install -d -o headliner -g headliner -m 0750 "${drill_dir}"

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; failures=$((failures + 1)); }

# Overwrite (never delete) a scratch file.
reset_file() { : >"$1"; }

now_minus_minutes() {
    "${python_bin}" -c 'import sys; from datetime import UTC, datetime, timedelta; print((datetime.now(UTC) - timedelta(minutes=int(sys.argv[1]))).isoformat())' "$1"
}

seed_state() {
    # seed_state FILE CHECK...: the named checks have been failing for 20 minutes.
    local file="$1"
    shift
    "${python_bin}" - "${file}" "$(now_minus_minutes 20)" "$@" <<'PY'
import json, sys
path, since, *names = sys.argv[1:]
checks = {n: {"level": "fail", "detail": "drill", "since": since, "notified_level": "ok"} for n in names}
open(path, "w").write(json.dumps({"checks": checks, "pending": []}))
PY
}

pending_kinds() {
    "${python_bin}" - "$1" <<'PY'
import json, sys
events = json.load(open(sys.argv[1])).get("pending", [])
print(",".join(f"{e['kind']}:{e['check']}" for e in events))
PY
}

watchdog() {
    # watchdog STATE [extra args]: one watchdog tick against scratch state.
    local state="$1"
    shift
    "${headliner_bin}" watchdog --sources "${sources_file}" \
        --db /var/lib/headliner/headlines.db --state "${state}" --no-heal --quiet "$@"
}

drill_fetch_dns() {
    echo "== fetch-dns: fetch with no network at all (empty network namespace)"
    local sources="${drill_dir}/sources.yaml" db="${drill_dir}/drill.db"
    cat >"${sources}" <<'YAML'
settings:
  user_agent: "headliner-drill/1.0 (+contact: drill@example.org)"
sources:
  - {name: Drill A, url: "https://feeds.bbci.co.uk/news/rss.xml", type: rss}
  - {name: Drill B, url: "https://www.theguardian.com/au/rss", type: rss}
YAML
    chown headliner:headliner "${sources}"
    local code=0
    unshare -n -- runuser -u headliner -- "${headliner_bin}" fetch \
        --sources "${sources}" --db "${db}" --quiet || code=$?
    if [[ "${code}" -eq 3 ]]; then
        pass "fetch exited 3 (network down) without requesting any feed"
    else
        fail "fetch exited ${code}, expected 3"
    fi
    local rows
    rows="$("${python_bin}" - "${db}" <<'PY'
import sqlite3, sys
sql = "select count(*) from fetch_log where error like 'network down%'"
print(sqlite3.connect(sys.argv[1]).execute(sql).fetchone()[0])
PY
)"
    if [[ "${rows}" -ge 2 ]]; then
        pass "each source logged a 'network down' error (${rows} rows)"
    else
        fail "expected 2 'network down' rows in the drill database, found ${rows}"
    fi
}

drill_watchdog_dns() {
    echo "== watchdog-dns: watchdog with no network, DNS 'down for 20 minutes'"
    local state="${drill_dir}/state-dns.json" out="${drill_dir}/out-dns.txt"
    seed_state "${state}" dns internet
    reset_file "${out}"
    unshare -n -- "${headliner_bin}" watchdog --sources "${sources_file}" \
        --db /var/lib/headliner/headlines.db --state "${state}" \
        --no-heal --no-notify --quiet >"${out}" || true
    if grep -q '^FAIL  dns' "${out}"; then
        pass "dns check failed with no resolver reachable"
    else
        fail "dns check did not fail (see ${out})"
    fi
    if grep -q '^FAIL  internet' "${out}"; then
        pass "internet check failed with no routing"
    else
        fail "internet check did not fail (see ${out})"
    fi
    if [[ "$(pending_kinds "${state}")" == *"alert:dns"* ]]; then
        pass "a lasting DNS failure queued an alert"
    else
        fail "no dns alert queued (pending: $(pending_kinds "${state}"))"
    fi
}

drill_viewer() {
    echo "== viewer: stop headliner-web.service, expect the watchdog to notice"
    local state="${drill_dir}/state-viewer.json" out="${drill_dir}/out-viewer.txt"
    reset_file "${state}"
    reset_file "${out}"
    trap 'systemctl start headliner-web.service' EXIT
    systemctl stop headliner-web.service
    watchdog "${state}" --no-notify >"${out}" || true
    if grep -q '^FAIL  viewer' "${out}" && grep -q '^FAIL  units' "${out}"; then
        pass "viewer and units checks failed while the viewer was stopped"
    else
        fail "stopped viewer not reported (see ${out})"
    fi
    systemctl start headliner-web.service
    trap - EXIT
    local waited=0
    until curl -fsS -m 2 http://127.0.0.1:8090/healthz >/dev/null 2>&1 || [[ "${waited}" -ge 30 ]]; do
        sleep 2
        waited=$((waited + 2))
    done
    reset_file "${out}"
    watchdog "${state}" --no-notify >"${out}" || true
    if grep -q '^OK    viewer' "${out}"; then
        pass "viewer check recovered after restart"
    else
        fail "viewer did not recover (see ${out})"
    fi
}

drill_notify() {
    echo "== notify: send a real test issue to GitHub (opened, commented, closed)"
    if [[ ! -s /etc/headliner/github-token ]]; then
        fail "no token at /etc/headliner/github-token"
        return
    fi
    local state="${drill_dir}/state-notify.json" out="${drill_dir}/out-notify.txt"
    local stamp
    stamp="$(now_minus_minutes 0)"
    "${python_bin}" - "${state}" "${stamp}" <<'PY'
import json, sys
path, now = sys.argv[1:]
event = {"check": "drill", "level": "fail", "detail": "test alert from deploy/drill.sh", "since": now, "at": now}
open(path, "w").write(json.dumps({"checks": {}, "pending": [{"kind": "alert", **event}, {"kind": "recover", **event}]}))
PY
    reset_file "${out}"
    watchdog "${state}" >"${out}" 2>&1 || true
    if grep -q 'cannot notify' "${out}"; then
        fail "GitHub refused or was unreachable: $(grep 'cannot notify' "${out}" | head -1)"
    elif [[ -z "$(pending_kinds "${state}")" ]]; then
        pass "test issue delivered; check GitHub: 'headliner health: DRILL: test alert (ignore)' (closed)"
    else
        fail "events still pending: $(pending_kinds "${state}")"
    fi
}

case "${1:-all}" in
    fetch-dns) drill_fetch_dns ;;
    watchdog-dns) drill_watchdog_dns ;;
    viewer) drill_viewer ;;
    notify) drill_notify ;;
    all)
        drill_fetch_dns
        drill_watchdog_dns
        drill_viewer
        drill_notify
        ;;
    *)
        echo "usage: sudo bash $0 [fetch-dns|watchdog-dns|viewer|notify|all]" >&2
        exit 2
        ;;
esac

echo
if [[ "${failures}" -eq 0 ]]; then
    echo "All drills passed."
else
    echo "${failures} check(s) FAILED." >&2
    exit 1
fi
