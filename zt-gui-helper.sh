#!/bin/bash
set -euo pipefail

export PATH=/usr/sbin:/usr/bin:/sbin:/bin

usage() {
    cat >&2 <<'EOF'
Usage: zt-gui-helper <action> [args]
Actions:
  start                     - Start ZeroTier One service
  stop                      - Stop ZeroTier One service
  disable                   - Disable and stop ZeroTier One service
  copy-token <username>     - Copy authtoken to user's home
  setup <username>          - Start service and copy token
EOF
    exit 2
}

validate_username() {
    local user="$1"
    # Must match ^[a-z_][a-z0-9_-]*$ and exist in passwd
    if ! [[ "$user" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
        echo "Error: Invalid username format: $user" >&2
        return 1
    fi
    if ! getent passwd "$user" >/dev/null 2>&1; then
        echo "Error: User does not exist: $user" >&2
        return 1
    fi
    return 0
}

action_start() {
    if systemctl enable --now zerotier-one.service; then
        echo "ZeroTier One service started."
    else
        echo "Error: Failed to start ZeroTier One service." >&2
        return 1
    fi
}

action_stop() {
    if systemctl stop zerotier-one.service; then
        echo "ZeroTier One service stopped."
    else
        echo "Error: Failed to stop ZeroTier One service." >&2
        return 1
    fi
}

action_disable() {
    if systemctl disable --now zerotier-one.service; then
        echo "ZeroTier One service disabled."
    else
        echo "Error: Failed to disable ZeroTier One service." >&2
        return 1
    fi
}

action_copy_token() {
    local user="$1"

    validate_username "$user" || return 1

    # Get invoking user's UID from PKEXEC_UID
    if [[ -z "${PKEXEC_UID:-}" ]]; then
        echo "Error: Not invoked via pkexec or PKEXEC_UID not set." >&2
        return 1
    fi

    # Get the UID for the given username
    local user_uid
    user_uid=$(id -u "$user")

    # Verify invoking user matches the target user
    if [[ "$PKEXEC_UID" != "$user_uid" ]]; then
        echo "Error: User $user does not match invoking user (UID $PKEXEC_UID vs $user_uid)." >&2
        return 1
    fi

    local home_dir
    home_dir=$(getent passwd "$user" | cut -d: -f6)
    if [[ -z "$home_dir" ]]; then
        echo "Error: Could not determine home directory for user $user." >&2
        return 1
    fi

    local dest="${home_dir}/.zeroTierOneAuthToken"
    local src="/var/lib/zerotier-one/authtoken.secret"

    # Wait up to 15s for authtoken to exist
    local waited=0
    while [[ ! -f "$src" ]] && [[ $waited -lt 30 ]]; do
        sleep 0.5
        waited=$((waited + 1))
    done

    if [[ ! -f "$src" ]]; then
        echo "Error: ZeroTier authtoken not found at $src after 15 seconds." >&2
        return 1
    fi

    # Write the file as the target user (not root) so a symlink planted in
    # their home can't redirect a root-owned write elsewhere.
    if runuser -u "$user" -- sh -c 'umask 077; rm -f "$1" && cat > "$1"' sh "$dest" < "$src"; then
        echo "Auth token copied to $dest"
    else
        echo "Error: Failed to copy auth token." >&2
        return 1
    fi
}

action_setup() {
    local user="$1"

    validate_username "$user" || return 1

    action_start || return 1
    action_copy_token "$user" || return 1
}

# Main script
if [[ $# -lt 1 ]]; then
    usage
fi

case "$1" in
    start)
        action_start
        ;;
    stop)
        action_stop
        ;;
    disable)
        action_disable
        ;;
    copy-token)
        if [[ $# -lt 2 ]]; then
            usage
        fi
        action_copy_token "$2"
        ;;
    setup)
        if [[ $# -lt 2 ]]; then
            usage
        fi
        action_setup "$2"
        ;;
    *)
        usage
        ;;
esac
