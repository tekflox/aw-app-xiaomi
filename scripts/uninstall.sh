#!/usr/bin/env bash
# Reverses install_adb.sh — the revert action replayed from the
# commands:install journal when this app is uninstalled.
#
# Stops the adb server first: it is a background daemon this app started, and
# leaving it running after the app that needs it is gone keeps a socket open to
# the TV for no one.
#
# The TV-authorized keypair under <AW_WORKSPACE_HOME>/data/xiaomi/ is
# deliberately left alone — re-pairing it needs a human standing in front of
# the TV to accept an on-screen prompt, so an uninstall must not throw it away.
set -euo pipefail

adb kill-server >/dev/null 2>&1 || true
sudo apt-get remove -y adb >/dev/null 2>&1 || true

echo "adb removed (keypair in the app data dir kept)"
