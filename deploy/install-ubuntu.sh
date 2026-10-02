#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: sudo bash deploy/install-ubuntu.sh [--keep-sources]

Installs or updates headliner as a systemd service.

By default /etc/headliner/sources.yaml is brought up to date with the shipped
deploy/sources.yaml, keeping its user_agent line (your contact address). The
previous file is kept as sources.yaml.bak-<timestamp>, the change is shown,
and if the new file does not load the old one is restored.

  --keep-sources    Leave the live sources.yaml untouched (the shipped one is
                    still written next to it as sources.yaml.dist).
  --update-sources  Accepted for compatibility; updating is the default.
USAGE
}

update_sources=true
for arg in "$@"; do
    case "${arg}" in
        --update-sources) update_sources=true ;;
        --keep-sources) update_sources=false ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: ${arg}" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this installer with sudo or as root." >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "${script_dir}/.." && pwd)"

for required_file in requirements.txt pyproject.toml deploy/headliner.service deploy/headliner.timer deploy/headliner-web.service deploy/headliner-backup.service deploy/headliner-backup.timer deploy/sources.yaml; do
    if [[ ! -f "${repo_dir}/${required_file}" ]]; then
        echo "Missing ${required_file}; run this script from a complete repository checkout." >&2
        exit 1
    fi
done

# Redeploys skip apt: refreshing every package index is slow and changes
# nothing once python3-venv is installed.
if ! dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'ok installed'; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3 python3-venv
fi

if ! getent passwd headliner >/dev/null; then
    useradd --system --home-dir /var/lib/headliner --create-home \
        --shell /usr/sbin/nologin headliner
fi

install -d -o root -g root -m 0755 /opt/headliner
venv_python=/opt/headliner/venv/bin/python
# Only a missing or broken venv (e.g. after a Python upgrade) is rebuilt, so a
# redeploy does not go back to PyPI for pip itself.
if ! "${venv_python}" -c 'import pip' >/dev/null 2>&1; then
    python3 -m venv --clear /opt/headliner/venv
    "${venv_python}" -m pip install --upgrade pip
fi
# setuptools matches [build-system] in pyproject.toml. Installing it here lets
# the package build below skip build isolation, which would otherwise fetch
# setuptools from PyPI on every deploy.
"${venv_python}" -m pip install -r "${repo_dir}/requirements.txt" 'setuptools>=68'
# Build from a scratch copy: an in-tree build as root would leave root-owned
# build/ and *.egg-info directories in the checkout, and a stale build/lib
# could carry deleted modules into later installs.
build_dir="$(mktemp -d)"
trap 'rm -rf -- "${build_dir}"' EXIT
cp -r "${repo_dir}/pyproject.toml" "${repo_dir}/README.md" "${repo_dir}/headliner" "${build_dir}/"
"${venv_python}" -m pip install --no-deps --no-build-isolation "${build_dir}"

install -d -o root -g headliner -m 0750 /etc/headliner
if [[ ! -e /etc/headliner/sources.yaml ]]; then
    install -o root -g headliner -m 0640 \
        "${repo_dir}/deploy/sources.yaml" /etc/headliner/sources.yaml
else
    echo "Keeping existing /etc/headliner/sources.yaml"
    # Ship the current defaults alongside; unless --keep-sources, they then
    # replace the live file (user_agent kept, backed up, validated).
    install -o root -g headliner -m 0640 \
        "${repo_dir}/deploy/sources.yaml" /etc/headliner/sources.yaml.dist
    if [[ "${update_sources}" == true ]] \
        && ! cmp -s /etc/headliner/sources.yaml /etc/headliner/sources.yaml.dist; then
        backup="/etc/headliner/sources.yaml.bak-$(date +%Y%m%d-%H%M%S)"
        install -o root -g headliner -m 0640 /etc/headliner/sources.yaml "${backup}"
        live_ua="$(grep -m1 -E '^[[:space:]]*user_agent:' /etc/headliner/sources.yaml || true)"
        merged="$(mktemp)"
        # awk, not sed: the user_agent line is copied verbatim whatever it contains.
        UA_LINE="${live_ua}" awk '
            /^[[:space:]]*user_agent:/ && ENVIRON["UA_LINE"] != "" && !done {
                print ENVIRON["UA_LINE"]; done = 1; next
            }
            { print }
        ' /etc/headliner/sources.yaml.dist >"${merged}"
        install -o root -g headliner -m 0640 "${merged}" /etc/headliner/sources.yaml
        rm -f -- "${merged}"
        check_config='import sys
from headliner.config import ConfigError, load_config
try:
    load_config(sys.argv[1])
except ConfigError as exc:
    sys.exit(f"ERROR: {exc}")'
        if ! /opt/headliner/venv/bin/python -c "${check_config}" /etc/headliner/sources.yaml; then
            install -o root -g headliner -m 0640 "${backup}" /etc/headliner/sources.yaml
            echo "ERROR: the updated sources.yaml did not load; restored ${backup}" >&2
            exit 1
        fi
        echo "Updated /etc/headliner/sources.yaml from the shipped defaults (user_agent kept)."
        echo "Previous version: ${backup}"
        diff -u "${backup}" /etc/headliner/sources.yaml || true
    elif ! cmp -s /etc/headliner/sources.yaml /etc/headliner/sources.yaml.dist; then
        echo "NOTE: /etc/headliner/sources.yaml differs from the shipped defaults." >&2
        echo "      Review with: diff -u /etc/headliner/sources.yaml /etc/headliner/sources.yaml.dist" >&2
        echo "      To adopt them (keeping your user_agent): rerun without --keep-sources" >&2
    fi
fi

if grep -q 'you@example\.com' /etc/headliner/sources.yaml; then
    echo "WARNING: /etc/headliner/sources.yaml still uses the placeholder contact address." >&2
fi

install -o root -g root -m 0644 \
    "${repo_dir}/deploy/headliner.service" /etc/systemd/system/headliner.service
install -o root -g root -m 0644 \
    "${repo_dir}/deploy/headliner.timer" /etc/systemd/system/headliner.timer
install -o root -g root -m 0644 \
    "${repo_dir}/deploy/headliner-web.service" /etc/systemd/system/headliner-web.service
install -o root -g root -m 0644 \
    "${repo_dir}/deploy/headliner-backup.service" /etc/systemd/system/headliner-backup.service
install -o root -g root -m 0644 \
    "${repo_dir}/deploy/headliner-backup.timer" /etc/systemd/system/headliner-backup.timer

systemctl daemon-reload
# Once fetching is scheduled there is data worth keeping: back it up daily.
if systemctl is-enabled --quiet headliner.timer \
    && ! systemctl is-enabled --quiet headliner-backup.timer; then
    systemctl enable --now headliner-backup.timer
    echo "Enabled daily database backups (headliner-backup.timer, 03:30)."
fi
# A running viewer keeps serving the old code until restarted.
if systemctl is-active --quiet headliner-web.service; then
    systemctl restart headliner-web.service
    echo "Restarted headliner-web.service (web viewer) on the new code."
fi

cat <<'INSTRUCTIONS'
Headliner is installed. Before enabling scheduled fetches:
  1. Edit /etc/headliner/sources.yaml and replace the placeholder contact address.
  2. Enable the timer (runs four times daily): systemctl enable --now headliner.timer
  3. Optionally run once now: systemctl start headliner.service

Inspect runs with: journalctl -u headliner.service

Daily backups (enabled automatically once headliner.timer is enabled):
  systemctl enable --now headliner-backup.timer    # /var/lib/headliner/backups

Optional read-only web viewer (port 8090, home network + localhost):
  systemctl enable --now headliner-web.service
  Tailnet HTTPS: tailscale serve --bg --https 8444 http://127.0.0.1:8090
INSTRUCTIONS
