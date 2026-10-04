#!/usr/bin/env bash
# Installs `adb` (android-tools-adb) — the only binary this app shells out to.
#
# Idempotent: the apps reconciler re-runs this on every boot, and a workspace
# container recreation starts from a fresh image with no adb in it, so this
# re-run IS the app's survival mechanism, not a redundant retry.
#
# Two rules from the installer contract (aw-create-app skill §6b), both of
# which have cost this estate real debugging time:
#
#   1. sudo. The container user is `ubuntu` (uid 1001) with NOPASSWD sudo baked
#      into the image. A bare apt-get dies on
#      /var/lib/apt/lists/lock (13: Permission denied) on every boot, forever,
#      and only ever to a log nobody reads.
#   2. Verify, don't detect. `command -v adb && exit 0` asks the wrong
#      question — it cannot tell a working adb from a file named adb — so the
#      guard below runs adb's own version check, and the install path passes
#      --reinstall so a half-unpacked package is repaired rather than reported
#      as "already the newest version" and skipped.
set -euo pipefail

if adb --version >/dev/null 2>&1; then
  echo "adb already present and answering --version: $(adb --version | head -1)"
  exit 0
fi

sudo apt-get update -qq
sudo apt-get install -y --reinstall adb

adb --version | head -1
