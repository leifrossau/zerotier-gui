#!/bin/bash
set -euo pipefail

# Determine script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# User directories
SHARE_DIR="$HOME/.local/share/zerotier-gui"
BIN_DIR="$HOME/.local/bin"
APPLICATIONS_DIR="$HOME/.local/share/applications"

uninstall() {
    echo "Uninstalling ZeroTier GUI..."
    rm -rf "$SHARE_DIR"
    rm -f "$BIN_DIR/zerotier-gui"
    rm -f "$APPLICATIONS_DIR/zerotier-gui.desktop"

    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPLICATIONS_DIR" 2>/dev/null || true
    fi

    echo "ZeroTier GUI uninstalled."
    exit 0
}

# Check for --uninstall flag
if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
fi

echo "Installing ZeroTier GUI..."

# Create directories
mkdir -p "$SHARE_DIR"
mkdir -p "$BIN_DIR"
mkdir -p "$APPLICATIONS_DIR"

# Copy Python files
if [[ -f "$SCRIPT_DIR/zerotier_gui.py" ]]; then
    cp "$SCRIPT_DIR/zerotier_gui.py" "$SHARE_DIR/"
else
    echo "Warning: zerotier_gui.py not found in $SCRIPT_DIR" >&2
fi

for module in zt_api.py zt_central.py zt_discover.py zt_tray.py; do
    if [[ -f "$SCRIPT_DIR/$module" ]]; then
        cp "$SCRIPT_DIR/$module" "$SHARE_DIR/"
    else
        echo "Warning: $module not found in $SCRIPT_DIR" >&2
    fi
done

# The privileged helper is installed root-owned by install-system.sh, never in your home folder.
rm -f "$SHARE_DIR/zt-gui-helper.sh"

# Create launcher script
cat > "$BIN_DIR/zerotier-gui" <<'EOF'
#!/bin/sh
exec python3 ~/.local/share/zerotier-gui/zerotier_gui.py "$@"
EOF
chmod 755 "$BIN_DIR/zerotier-gui"

# Install desktop file
cp "$SCRIPT_DIR/zerotier-gui.desktop" "$APPLICATIONS_DIR/"

# Update desktop database if available
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPLICATIONS_DIR" 2>/dev/null || true
fi

echo "Installation complete."
if [[ ! -x /usr/local/libexec/zerotier-gui/zt-gui-helper ]]; then
    echo ""
    echo "To let the app start/stop ZeroTier, also install the system helper once:"
    echo "  sudo $SCRIPT_DIR/install-system.sh"
fi
echo ""
echo "Dependencies (install via pacman):"
echo "  pacman -S python-gobject gtk4 libadwaita zerotier-one polkit"
