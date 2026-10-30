#!/bin/sh
# Run as root on each monitored server:
#   sudo sh monitor-user-setup.sh "ssh-ed25519 AAAA... monitor@central"
# Creates a dedicated read-only `monitor` account with key-only login and a
# minimal sudo whitelist for commands that need root.
set -eu

PUBKEY="${1:?usage: $0 '<public key>'}"
USER_NAME=monitor

id "$USER_NAME" >/dev/null 2>&1 || useradd -m -s /bin/bash "$USER_NAME"
passwd -l "$USER_NAME" >/dev/null  # no password login

# read logs without sudo
for g in systemd-journal adm; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$USER_NAME"
done

install -d -o "$USER_NAME" -g "$USER_NAME" -m 700 "/home/$USER_NAME/.ssh"
AK="/home/$USER_NAME/.ssh/authorized_keys"
touch "$AK"
grep -qxF "$PUBKEY" "$AK" || echo "$PUBKEY" >> "$AK"
chown "$USER_NAME:$USER_NAME" "$AK"
chmod 600 "$AK"

# Commands that may require root. Only existing binaries are whitelisted.
SUDOERS=/etc/sudoers.d/monitor
: > "$SUDOERS.tmp"
for c in dmesg journalctl smartctl ipmitool ras-mc-ctl mdadm; do
  p=$(command -v "$c" 2>/dev/null || true)
  [ -n "$p" ] && echo "$USER_NAME ALL=(root) NOPASSWD: $p" >> "$SUDOERS.tmp"
done
chmod 440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null && mv "$SUDOERS.tmp" "$SUDOERS"

echo "monitor user ready"
