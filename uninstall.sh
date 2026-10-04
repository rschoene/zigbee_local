#!/usr/bin/env bash
#
# uninstall.sh — Remove zigbee-daemon from the system
#
# Usage:
#   sudo ./uninstall.sh            # interactive (asks for confirmation)
#   sudo ./uninstall.sh --batch    # non-interactive (no confirmation)
#
# Reads <install_dir>/.install_meta to determine what was created
# during installation (e.g. whether the service user was created).
#
set -euo pipefail

# ------------------------------------------------------------------ #
# argument parsing
# ------------------------------------------------------------------ #
BATCH=false
for arg in "$@"; do
    case "${arg}" in
        --batch) BATCH=true ;;
        -h|--help)
            echo "Usage: sudo ./uninstall.sh [--batch]"
            echo "  --batch   Non-interactive mode (no confirmation)"
            exit 0
            ;;
        *)
            echo "Unknown option: ${arg}" >&2
            exit 1
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Error: this script must be run as root (sudo ./uninstall.sh)" >&2
    exit 1
fi

# ------------------------------------------------------------------ #
# interactive prompt for install directory
# ------------------------------------------------------------------ #
if [[ "${BATCH}" == false ]]; then
    read -rp "Install directory to remove [default: /opt/zigbee_local]: " input_dir
else
    input_dir=""
fi

INSTALL_DIR="${input_dir:-/opt/zigbee_local}"
META_FILE="${INSTALL_DIR}/.install_meta"
SERVICE_NAME="zigbee-daemon"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CLI_WRAPPER="/usr/bin/zigbee"

# ------------------------------------------------------------------ #
# read install metadata
# ------------------------------------------------------------------ #
SERVICE_USER=""
USER_CREATED=false

if [[ -f "${META_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${META_FILE}"
    echo "==> Read install metadata: user=${SERVICE_USER} created=${USER_CREATED}"
else
    echo "Warning: ${META_FILE} not found; assuming defaults"
    SERVICE_USER="zigbee"
    USER_CREATED=false
fi

# ------------------------------------------------------------------ #
# confirmation
# ------------------------------------------------------------------ #
echo ""
echo "This will remove:"
echo "  - systemd service: ${SERVICE_NAME}"
echo "  - CLI wrapper:     ${CLI_WRAPPER}"
echo "  - Install dir:     ${INSTALL_DIR}"
if [[ "${USER_CREATED}" == "true" ]]; then
    echo "  - System user:     ${SERVICE_USER}"
fi
echo ""

if [[ "${BATCH}" == false ]]; then
    read -rp "Proceed? [y/N]: " confirm
    if [[ ! "${confirm}" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# ------------------------------------------------------------------ #
# 1. stop and disable service
# ------------------------------------------------------------------ #
echo "==> Stopping and disabling ${SERVICE_NAME}"
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
rm -f "${SERVICE_FILE}"
systemctl daemon-reload

# ------------------------------------------------------------------ #
# 2. remove CLI wrapper
# ------------------------------------------------------------------ #
echo "==> Removing CLI wrapper"
rm -f "${CLI_WRAPPER}"

# ------------------------------------------------------------------ #
# 3. remove install directory
# ------------------------------------------------------------------ #
echo "==> Removing ${INSTALL_DIR}"
rm -rf "${INSTALL_DIR}"

# ------------------------------------------------------------------ #
# 4. remove service user (only if we created it)
# ------------------------------------------------------------------ #
if [[ "${USER_CREATED}" == "true" && -n "${SERVICE_USER}" ]]; then
    echo "==> Removing user '${SERVICE_USER}'"
    userdel "${SERVICE_USER}" 2>/dev/null || true
else
    echo "==> Skipping user removal (user '${SERVICE_USER}' was pre-existing)"
fi

echo ""
echo "Uninstall complete."
