.PHONY: help install init dashboard daemon test clean check-system

help:                ## Show this help
	@awk 'BEGIN{FS=":.*##"; printf "Whoop Vault — make targets\n\n"} \
	     /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 }' \
	     $(MAKEFILE_LIST)

check-system:        ## Verify Linux + BlueZ + Python are present
	@command -v bluetoothctl >/dev/null || { echo "❌ bluetoothctl missing. Install BlueZ: sudo apt install bluez bluez-tools"; exit 1; }
	@command -v python3 >/dev/null || { echo "❌ python3 missing"; exit 1; }
	@python3 -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ required'" || exit 1
	@command -v sqlite3 >/dev/null || { echo "⚠ sqlite3 CLI not in PATH (optional, only for queries)"; }
	@echo "✓ System OK ($$(bluetoothctl --version | head -1))"

install: check-system ## Create venv and install Python deps
	@test -d .venv || python3 -m venv .venv
	@.venv/bin/pip install --upgrade pip -q
	@.venv/bin/pip install -r requirements.txt -q
	@.venv/bin/pip install pytest -q
	@echo "✓ Installed into .venv/"

init: install        ## Initialize DB schema and .env
	@mkdir -p data logs exports/ble-historical-v2
	@PYTHONPATH=ble .venv/bin/python -c "from whoop_ble.db import connect; connect().close()"
	@test -f .env || cp .env.example .env
	@echo "✓ Database created at data/whoop.db"
	@echo "✓ .env ready (edit to set WHOOP_BLE_MAC if you know it, or pair via dashboard)"

dashboard: init      ## Run the web dashboard on http://127.0.0.1:8787
	@echo "Dashboard: http://127.0.0.1:8787/"
	@PYTHONPATH=ble .venv/bin/python -m whoop_ble.dashboard

daemon: init         ## Run the BLE daemon (requires paired strap)
	@PYTHONPATH=ble .venv/bin/python -m whoop_ble.daemon

test: install        ## Run the test suite
	@cd ble && ../.venv/bin/python -m pytest -q

clean:               ## Remove data, db, venv (DESTRUCTIVE)
	@read -p "This will delete .venv data/ logs/ exports/. Continue? [y/N] " ans; \
	  test "$$ans" = "y" && rm -rf .venv data logs exports
