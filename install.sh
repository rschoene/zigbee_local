#!/usr/bin/env bash
#
# install.sh — Install zigbee-daemon
#
# Usage:
#   sudo ./install.sh            # interactive (asks for dir + user)
#   sudo ./install.sh --batch    # non-interactive (uses defaults)
#
# What it does:
#   1. Creates a dedicated system user (if not present)
#   2. Copies the project to the install directory
#   3. Creates a Python venv
#   4. Installs dependencies
#   5. Creates a systemd service (runs as the service user)
#   6. Creates /usr/bin/zigbee (CLI wrapper)
#   7. Creates the data directory for runtime state
#   8. Records install metadata in <install_dir>/.install_meta
#   9. Enables and starts the systemd service
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
            echo "Usage: sudo ./install.sh [--batch]"
            echo "  --batch   Non-interactive mode (use defaults)"
            exit 0
            ;;
        *)
            echo "Unknown option: ${arg}" >&2
            exit 1
            ;;
    esac
done

# ------------------------------------------------------------------ #
# checks
# ------------------------------------------------------------------ #
if [[ $EUID -ne 0 ]]; then
    echo "Error: this script must be run as root (sudo ./install.sh)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ------------------------------------------------------------------ #
# interactive prompts (unless --batch)
# ------------------------------------------------------------------ #
if [[ "${BATCH}" == false ]]; then
    read -rp "Install directory [default: /opt/zigbee_local]: " input_dir
    read -rp "Service user name [default: zigbee]: " input_user
else
    input_dir=""
    input_user=""
fi

INSTALL_DIR="${input_dir:-/opt/zigbee_local}"
SERVICE_USER="${input_user:-zigbee}"
SERVICE_NAME="zigbee-daemon"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CLI_WRAPPER="/usr/bin/zigbee"
CONFIG_FILE="${INSTALL_DIR}/config.yaml"
DATA_DIR="${INSTALL_DIR}/data"
META_FILE="${INSTALL_DIR}/.install_meta"

echo ""
echo "  Install dir: ${INSTALL_DIR}"
echo "  Service user: ${SERVICE_USER}"
echo ""

# ------------------------------------------------------------------ #
# 1. create dedicated service user
# ------------------------------------------------------------------ #
USER_CREATED=false
if id "${SERVICE_USER}" &>/dev/null; then
    echo "==> User '${SERVICE_USER}' already exists"
else
    echo "==> Creating system user '${SERVICE_USER}'"
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
    USER_CREATED=true
fi

# ------------------------------------------------------------------ #
# 2. copy project files
# ------------------------------------------------------------------ #
echo "==> Installing to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"

# Copy source (exclude .venv, .git, data, __pycache__)
rsync -a --delete \
    --exclude '.venv' \
    --exclude '.git' \
    --exclude 'data' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    "${SCRIPT_DIR}/" "${INSTALL_DIR}/"

# ------------------------------------------------------------------ #
# 3. create venv and install deps
# ------------------------------------------------------------------ #
echo "==> Creating virtual environment"
python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

# ------------------------------------------------------------------ #
# 4. create data directory and set ownership
# ------------------------------------------------------------------ #
echo "==> Creating data directory"
mkdir -p "${DATA_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}"
chmod 0775 "${DATA_DIR}"

# Add the invoking user (who ran sudo) to the zigbee group so the CLI
# can read/write the SQLite DB.
SUDO_USER_NAME="${SUDO_USER:-}"
if [[ -n "${SUDO_USER_NAME}" ]]; then
    usermod -aG "${SERVICE_USER}" "${SUDO_USER_NAME}"
    echo "    Added '${SUDO_USER_NAME}' to group '${SERVICE_USER}' for DB access"
fi

# ------------------------------------------------------------------ #
# 5. ensure config exists (use shipped config.yaml as-is)
# ------------------------------------------------------------------ #
# Both the systemd service (WorkingDirectory) and the CLI wrapper (cd)
# set the working directory to ${INSTALL_DIR}, so relative paths in
# config.yaml (e.g. ./data/zigbee.db) resolve correctly.
echo "==> Config: ${CONFIG_FILE}"

# ------------------------------------------------------------------ #
# 6. systemd service
# ------------------------------------------------------------------ #
echo "==> Installing systemd service"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Zigbee Daemon (zigpy-based Zigbee coordinator)
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python -m daemon -c ${CONFIG_FILE}
Restart=on-failure
RestartSec=5
# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "${SERVICE_FILE}"

# ------------------------------------------------------------------ #
# 7. CLI wrapper
# ------------------------------------------------------------------ #
echo "==> Installing CLI wrapper at ${CLI_WRAPPER}"
cat > "${CLI_WRAPPER}" <<EOF
#!/usr/bin/env bash
# zigbee — CLI for the zigbee-daemon service
cd ${INSTALL_DIR}
exec ${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/cli/cli.py "\$@"
EOF
chmod 755 "${CLI_WRAPPER}"

# ------------------------------------------------------------------ #
# 8. write install metadata (for uninstall)
# ------------------------------------------------------------------ #
echo "==> Writing install metadata"
cat > "${META_FILE}" <<EOF
user=${SERVICE_USER}
user_created=${USER_CREATED}
install_dir=${INSTALL_DIR}
service=${SERVICE_NAME}
cli_wrapper=${CLI_WRAPPER}
EOF

# ------------------------------------------------------------------ #
# 9. enable and start service
# ------------------------------------------------------------------ #
echo "==> Enabling and starting ${SERVICE_NAME}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo ""
echo "============================================================"
echo "  Installation complete!"
echo ""
echo "  Service:   systemctl status ${SERVICE_NAME}"
echo "  Logs:      journalctl -u ${SERVICE_NAME} -f"
echo "  CLI:       zigbee devices"
echo "  Config:    ${CONFIG_FILE}"
echo "  Data:      ${DATA_DIR}/"
echo ""
echo "  To pair a new device:"
echo "    zigbee permit 60"
echo ""
echo "  NOTE: If the dongle is not readable, install the udev rule:"
echo "    sudo ${INSTALL_DIR}/udev/install.sh --user \$(whoami)"
echo "============================================================"
