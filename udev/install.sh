#!/usr/bin/env bash
#
# Install a udev rule that changes the ownership/permissions of the Zigbee
# dongle's serial node so the daemon (and your user) can open it without root.
#
# By default this targets the Sonoff Zigbee 3.0 USB Dongle Plus
# (Silicon Labs CP210x UART bridge, USB id 10c4:ea60).
#
# Usage:
#   ./install.sh [--group GROUP] [--mode MODE] [--user USER]
#                [--vendor VV] [--product PP]
#
# Defaults:
#   --group   dialout
#   --mode    0660
#   --user    $(whoami)
#   --vendor  10c4
#   --product ea60
#
# Access model:
#   * With --user USER  -> the rule sets OWNER=USER, MODE=0600. The user owns
#     the device directly, so access is immediate (no re-login / newgrp).
#   * Without --user    -> the rule sets GROUP=GROUP, MODE=0660 and adds the
#     current user to that group (takes effect in a new login session).
#
set -euo pipefail

GROUP="dialout"
MODE="0660"
USER_NAME="$(id -un)"
USER_EXPLICIT=false
VENDOR="10c4"
PRODUCT="ea60"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --group)   GROUP="$2";   shift 2 ;;
    --mode)    MODE="$2";    shift 2 ;;
    --user)    USER_NAME="$2"; USER_EXPLICIT=true; shift 2 ;;
    --vendor)  VENDOR="$2";  shift 2 ;;
    --product) PRODUCT="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 1 ;;
  esac
done

RULE_NAME="99-zigbee-dongle.rules"
RULE_DIR="/etc/udev/rules.d"
RULE_PATH="${RULE_DIR}/${RULE_NAME}"

# --- privilege escalation ------------------------------------------------- #
if [[ $EUID -eq 0 ]]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  echo "ERROR: this script needs root or sudo to install a udev rule." >&2
  exit 1
fi

echo "==> Zigbee dongle udev rule"
echo "    match : tty with parent USB ${VENDOR}:${PRODUCT}"

# When --user is explicitly given, set OWNER for immediate access (no
# re-login needed). Otherwise fall back to group-based access.
if [[ "$USER_EXPLICIT" == true ]]; then
  echo "    owner : ${USER_NAME}"
  echo "    mode  : 0600"
  RULE_ATTRS="OWNER=\"${USER_NAME}\", MODE=\"0600\""
  RULE_COMMENT="# Grants '${USER_NAME}' direct ownership of the Zigbee dongle"
else
  echo "    group : ${GROUP}"
  echo "    mode  : ${MODE}"
  RULE_ATTRS="GROUP=\"${GROUP}\", MODE=\"${MODE}\""
  RULE_COMMENT="# Grants access via group '${GROUP}' to the Zigbee dongle"
fi
echo "    user  : ${USER_NAME}"

# --- ensure the group exists (only for group-based mode) ------------------ #
if [[ "$USER_EXPLICIT" != true ]]; then
  if ! getent group "${GROUP}" >/dev/null 2>&1; then
    echo "==> Creating group '${GROUP}'"
    ${SUDO} groupadd --system "${GROUP}"
  fi
fi

# --- write the rule ------------------------------------------------------- #
TMP="$(mktemp)"
cat > "${TMP}" <<EOF
# Installed by zigbee_daemon_website/udev/install.sh
${RULE_COMMENT} serial node (USB ${VENDOR}:${PRODUCT}).
SUBSYSTEM=="tty", ATTRS{idVendor}=="${VENDOR}", ATTRS{idProduct}=="${PRODUCT}", ${RULE_ATTRS}
EOF

echo "==> Writing ${RULE_PATH}"
${SUDO} install -d -m 0755 "${RULE_DIR}"
${SUDO} install -m 0644 "${TMP}" "${RULE_PATH}"
rm -f "${TMP}"

# --- add the user to the group (only for group-based mode) ---------------- #
if [[ "$USER_EXPLICIT" != true ]]; then
  if id -nG "${USER_NAME}" | tr ' ' '\n' | grep -qx "${GROUP}"; then
    echo "==> '${USER_NAME}' is already in group '${GROUP}'"
  else
    echo "==> Adding '${USER_NAME}' to group '${GROUP}'"
    ${SUDO} usermod -aG "${GROUP}" "${USER_NAME}"
  fi
fi

# --- reload and re-apply -------------------------------------------------- #
echo "==> Reloading udev rules and re-applying to existing devices"
${SUDO} udevadm control --reload-rules
${SUDO} udevadm trigger --subsystem-match=tty
${SUDO} udevadm settle || true

echo
echo "Done."
echo "  * The rule now pins the dongle's ownership/permissions on every plug."
if [[ "$USER_EXPLICIT" == true ]]; then
  echo "  * '${USER_NAME}' is the owner — access is immediate (no re-login needed)."
else
  echo "  * Group membership takes effect in a NEW login session, or run:  newgrp ${GROUP}"
fi
echo "  * If the dongle is already plugged, re-plug it (or: sudo udevadm trigger)."
echo
if [[ "$USER_EXPLICIT" == true ]]; then
  echo "Verify:  ls -l /dev/ttyUSB0    # should show owner '${USER_NAME}' and mode 0600"
else
  echo "Verify:  ls -l /dev/ttyUSB0    # should show group '${GROUP}' and mode ${MODE}"
fi
