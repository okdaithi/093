#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this installer with sudo or as root." >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "${script_dir}/.." && pwd)"

for required_file in requirements.txt pyproject.toml deploy/headliner.service deploy/headliner.timer deploy/sources.yaml; do
    if [[ ! -f "${repo_dir}/${required_file}" ]]; then
        echo "Missing ${required_file}; run this script from a complete repository checkout." >&2
        exit 1
    fi
done

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv

if ! getent passwd headliner >/dev/null; then
    useradd --system --home-dir /var/lib/headliner --create-home \
        --shell /usr/sbin/nologin headliner
fi

install -d -o root -g root -m 0755 /opt/headliner
python3 -m venv /opt/headliner/venv
/opt/headliner/venv/bin/python -m pip install --upgrade pip
/opt/headliner/venv/bin/python -m pip install -r "${repo_dir}/requirements.txt"
/opt/headliner/venv/bin/python -m pip install --no-deps "${repo_dir}"

install -d -o root -g headliner -m 0750 /etc/headliner
if [[ ! -e /etc/headliner/sources.yaml ]]; then
    install -o root -g headliner -m 0640 \
        "${repo_dir}/deploy/sources.yaml" /etc/headliner/sources.yaml
else
    echo "Keeping existing /etc/headliner/sources.yaml"
    # Ship the current defaults alongside so changes (e.g. retired feeds) can
    # be merged by hand; the live file is never overwritten.
    install -o root -g headliner -m 0640 \
        "${repo_dir}/deploy/sources.yaml" /etc/headliner/sources.yaml.dist
    if ! cmp -s /etc/headliner/sources.yaml /etc/headliner/sources.yaml.dist; then
        echo "NOTE: /etc/headliner/sources.yaml differs from the shipped defaults." >&2
        echo "      Review with: diff -u /etc/headliner/sources.yaml /etc/headliner/sources.yaml.dist" >&2
    fi
fi

if grep -q 'you@example\.com' /etc/headliner/sources.yaml; then
    echo "WARNING: /etc/headliner/sources.yaml still uses the placeholder contact address." >&2
fi

install -o root -g root -m 0644 \
    "${repo_dir}/deploy/headliner.service" /etc/systemd/system/headliner.service
install -o root -g root -m 0644 \
    "${repo_dir}/deploy/headliner.timer" /etc/systemd/system/headliner.timer

systemctl daemon-reload

cat <<'INSTRUCTIONS'
Headliner is installed. Before enabling scheduled fetches:
  1. Edit /etc/headliner/sources.yaml and replace the placeholder contact address.
  2. Enable the six-hour timer: systemctl enable --now headliner.timer
  3. Optionally run once now: systemctl start headliner.service

Inspect runs with: journalctl -u headliner.service
INSTRUCTIONS
