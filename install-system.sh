#!/bin/bash
# Installs the privileged helper root-owned, so nothing running as your user can modify
# the program that pkexec runs as root. Run with sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER_DIR=/usr/local/libexec/zerotier-gui
HELPER="$HELPER_DIR/zt-gui-helper"
POLICY=/usr/share/polkit-1/actions/io.github.leifrossau.zerotiergui.policy

if [[ $EUID -ne 0 ]]; then
    echo "Run this with sudo: sudo $0 ${1:-}" >&2
    exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$HELPER" "$POLICY"
    rmdir "$HELPER_DIR" 2>/dev/null || true
    echo "Removed the ZeroTier GUI helper and polkit policy."
    exit 0
fi

install -d -o root -g root -m 755 /usr/local/libexec "$HELPER_DIR"
install -o root -g root -m 755 "$SCRIPT_DIR/zt-gui-helper.sh" "$HELPER"
install -o root -g root -m 644 "$SCRIPT_DIR/io.github.leifrossau.zerotiergui.policy" "$POLICY"
echo "Installed $HELPER and $POLICY"
