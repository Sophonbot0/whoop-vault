# whoop_ble — extracção raw da Whoop 5.0 por BLE

Cliente Python autónomo. **Não usa a cloud Whoop nem a app oficial.**
Funciona com a pulseira directamente via Bluetooth Low Energy (BLE).

## Estrutura

- `whoop_ble/` — pacote Python (frame parser, comandos, decoders, cliente, daemon)
- `scripts/` — entrypoints (scan, drain_history, install_daemon)
- `tests/` — pytest sobre frame/crc/decoders

## Quickstart

```bash
# 1. unpair a pulseira da app oficial Whoop (no telemóvel: settings → forget device)
# 2. scan
../.venv/bin/python scripts/scan.py
# 3. anotar o MAC e gravar em ../.env como WHOOP_BLE_MAC=XX:XX:XX:XX:XX:XX
# 4. live HR
../.venv/bin/python -m whoop_ble.cli hr-stream
# 5. drain do buffer historical (~14 dias)
../.venv/bin/python scripts/drain_history.py
# 6. daemon contínuo (manualmente)
../.venv/bin/python -m whoop_ble.daemon
```

## Requirements

- Linux com BlueZ (testado com hci0)
- Python 3.10+
- `bleak>=0.21`

Ver `../docs/ble-raw-extraction.md` para o documento completo.
