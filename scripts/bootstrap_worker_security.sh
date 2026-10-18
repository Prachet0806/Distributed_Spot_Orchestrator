#!/usr/bin/env bash
# Establish the fixed workload identity and network boundary on each worker.
set -euo pipefail

if ! id spotjob >/dev/null 2>&1; then
  if getent group 62000 >/dev/null || getent passwd 62000 >/dev/null; then
    echo 'spotjob UID/GID already in use' >&2
    exit 1
  fi
  groupadd --gid 62000 spotjob
  useradd --uid 62000 --gid 62000 --no-create-home --shell /usr/sbin/nologin spotjob
fi
test "$(id -u spotjob)" = 62000
test "$(id -g spotjob)" = 62000
if id -nG spotjob | grep -Eq '(^| )sudo( |$)'; then
  echo 'spotjob must not have sudo access' >&2
  exit 1
fi
usermod -aG spotjob ubuntu

install -d -o root -g spotjob -m 3770 /opt/job_workspace
install -d -o ubuntu -g spotjob -m 2750 /opt/job_workspace/checkpoint
install -d -o ubuntu -g spotjob -m 2750 /opt/job_workspace/checkpoints
install -d -o spotjob -g spotjob -m 2770 /opt/job_workspace/storage
install -d -o spotjob -g spotjob -m 2770 /opt/job_workspace/flags
install -d -o root -g root -m 0755 /opt/spot-worker-staged

# Qualified workloads are CPU batch jobs without external network effects.
# The root-owned interruption monitor is the only worker component that needs IMDS.
command -v iptables >/dev/null
command -v ip6tables >/dev/null
iptables -C OUTPUT -m owner --uid-owner 62000 -j REJECT 2>/dev/null || \
  iptables -I OUTPUT 1 -m owner --uid-owner 62000 -j REJECT
ip6tables -C OUTPUT -m owner --uid-owner 62000 -j REJECT 2>/dev/null || \
  ip6tables -I OUTPUT 1 -m owner --uid-owner 62000 -j REJECT

cat > /etc/systemd/system/spot-worker-interruption.service <<'UNIT'
[Unit]
Description=Spot worker interruption monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 -I /opt/spot-worker-control/control_entry.py spot-monitor
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now spot-worker-interruption.service
systemctl is-active --quiet spot-worker-interruption.service
