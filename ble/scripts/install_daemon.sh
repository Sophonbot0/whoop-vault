#!/usr/bin/env bash
# Instala o user service systemd para o whoop_ble daemon.
# W6 fix: substitui @@ROOT@@ no .tpl pelo path real antes de copiar.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="${PROJECT_ROOT}/ble/scripts/whoop-ble.service.tpl"
TARGET_DIR="${HOME}/.config/systemd/user"
TARGET="${TARGET_DIR}/whoop-ble.service"

mkdir -p "${TARGET_DIR}"
mkdir -p "${PROJECT_ROOT}/logs"

sed "s|@@ROOT@@|${PROJECT_ROOT}|g" "${TEMPLATE}" > "${TARGET}"

echo
echo "=== unit instalado em ${TARGET} (ROOT=${PROJECT_ROOT}) ==="
echo
echo "Activar manualmente:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now whoop-ble.service"
echo "  systemctl --user status whoop-ble.service"
echo "  journalctl --user -u whoop-ble.service -f"
echo
echo "Para que o serviço continue após logout:"
echo "  loginctl enable-linger \$USER"
