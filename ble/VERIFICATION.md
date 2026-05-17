# BLE phase 8 — verification report

Executado em: $(date)

## Test suite

```
$ cd ble && ../.venv/bin/python -m pytest tests/ -v
============================== 26 passed in 0.02s ==============================
```

26/26 testes a passar (CRC8, CRC32 custom, frame encode/decode, FrameAssembler
resync, commands com payload 5-byte do 5.0, decoders de realtime/event/metadata/IMU).

## Artefactos entregues

| Caminho                                | Estado |
|----------------------------------------|--------|
| `ble/whoop_ble/__init__.py`            | OK     |
| `ble/whoop_ble/client.py`              | OK     |
| `ble/whoop_ble/db.py`                  | OK (tabelas `ble_*` criadas na DB partilhada) |
| `ble/whoop_ble/crc.py`                 | OK     |
| `ble/whoop_ble/frame.py`               | OK     |
| `ble/whoop_ble/commands.py`            | OK     |
| `ble/whoop_ble/decoders.py`            | OK     |
| `ble/whoop_ble/standard_hr.py`         | OK     |
| `ble/whoop_ble/historical.py`          | OK     |
| `ble/whoop_ble/daemon.py`              | OK     |
| `ble/whoop_ble/cli.py`                 | OK (`python -m whoop_ble.cli hr-stream`) |
| `ble/scripts/scan.py`                  | OK — encontra a pulseira |
| `ble/scripts/drain_history.py`         | OK     |
| `ble/scripts/whoop-ble.service`        | OK     |
| `ble/scripts/install_daemon.sh`        | OK     |
| `ble/tests/test_crc.py`                | 6/6 passed |
| `ble/tests/test_frame.py`              | 9/9 passed |
| `ble/tests/test_commands_decoders.py`  | 11/11 passed |
| `docs/ble-raw-extraction.md`           | OK     |
| `scripts/status.py` (extended)         | OK (secção `[BLE direct tables]`) |
| `README.md` (link)                     | OK     |

## Scan run (live, hci0)

```
[scan] candidatos:
  MAC                    RSSI  Name
  AA:BB:CC:DD:EE:FF       -88  WHOOP 5AGXXXXXXX
```

→ a pulseira está a anunciar BLE e foi detectada.

## O que funciona headless (zero acção humana)

- Testes do parser (`pytest`) — todos passam offline
- BLE scan — funciona com hci0 já configurado
- DB schema (`ble_*` tables) auto-criado no primeiro import
- Status report (`scripts/status.py`) mostra as novas tabelas

## O que requer acção humana

1. **Unpair da app oficial Whoop** (telemóvel: Whoop app → Settings →
   Hardware → Strap → Forget device). Sem isto, qualquer tentativa de
   `connect()` falha porque o bond exclusivo está com a app.
2. **Correr `python ble/scripts/scan.py`** com a pulseira por perto.
3. **Gravar o MAC no `.env`**:
   ```
   echo 'WHOOP_BLE_MAC=AA:BB:CC:DD:EE:FF' >> .env
   ```
4. **Smoke test live HR**:
   ```
   .venv/bin/python -m whoop_ble.cli hr-stream
   ```
   Esperado: linhas `HR N bpm rr=[...]` a aparecer ~1×/s.
5. **Drain do buffer historical** (uma vez por dia):
   ```
   .venv/bin/python ble/scripts/drain_history.py
   ```
6. **Instalar daemon** (opcional, recomendado):
   ```
   bash ble/scripts/install_daemon.sh
   systemctl --user daemon-reload
   systemctl --user enable --now whoop-ble.service
   loginctl enable-linger $USER   # sobrevive ao logout
   ```

## Riscos conhecidos

- O firmware da pulseira pode mudar o layout dos payloads `0x28`/`0x31` num
  update via app oficial. Como a app está desemparelhada, isso não
  acontece automaticamente, mas se voltares a parear, o firmware pode ser
  actualizado e os decoders custom podem precisar de revisão.
- Scores Whoop (Recovery/Strain/Sleep) são **server-side** — não estão
  neste pipeline, e nunca estarão. Só o raw.

## Commits (8 commits novos)

```
fb6f276 ble phase 7: docs/ble-raw-extraction.md + status.py BLE section + README link
e82e753 ble phase 6: continuous daemon + systemd user unit
762167a ble phase 5: historical buffer drain (~14 days)
fa3150d ble phase 4: commands enum + decoders for realtime/event/metadata/IMU
7c8ef69 ble phase 3: frame parser + custom CRC32 + 15 passing pytest
2550276 ble phase 2: WhoopBLE client + standard HR streaming + DB schema
82c32c3 ble phase 1: scan script + package skeleton
+ this verification commit
```
