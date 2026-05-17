[Unit]
Description=Whoop 5.0 BLE raw data collector (no cloud)
After=bluetooth.target network.target
Wants=bluetooth.target

[Service]
Type=simple
WorkingDirectory=@@ROOT@@
EnvironmentFile=@@ROOT@@/.env
Environment=PYTHONPATH=@@ROOT@@/ble
ExecStart=@@ROOT@@/.venv/bin/python -m whoop_ble.daemon
Restart=always
RestartSec=10
StandardOutput=append:@@ROOT@@/logs/whoop-ble.log
StandardError=append:@@ROOT@@/logs/whoop-ble.err

[Install]
WantedBy=default.target
