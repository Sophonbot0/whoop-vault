# BLE raw extraction — Whoop 5.0 sem cloud

> **Objectivo:** extrair *todos* os dados raw possíveis da pulseira Whoop 5.0
> directamente por Bluetooth, sem depender da app oficial nem dos servidores
> da Whoop. Continua a funcionar **após o fim da subscrição** e mesmo que a
> Whoop bana a conta — o hardware da pulseira não é desligado remotamente.

## TL;DR

```bash
# 1. desemparelha a pulseira da app oficial Whoop (no telemóvel)
# 2. scan
.venv/bin/python ble/scripts/scan.py
# 3. grava o MAC no .env (linha sugerida pelo scan)
echo 'WHOOP_BLE_MAC=XX:XX:XX:XX:XX:XX' >> .env
# 4. live HR (sanity check, usa standard GATT)
.venv/bin/python -m whoop_ble.cli hr-stream
# 5. drain do buffer historical (~14 dias)
.venv/bin/python ble/scripts/drain_history.py
# 6. daemon contínuo
.venv/bin/python -m whoop_ble.daemon
# (opcional) instalar como systemd --user:
bash ble/scripts/install_daemon.sh
```

## O que está a ser extraído

Tudo é persistido na mesma SQLite do projecto (`data/whoop.db`) em tabelas
com prefixo `ble_`:

| Tabela              | Conteúdo                                    | Fonte                       |
|---------------------|---------------------------------------------|-----------------------------|
| `ble_hr_standard`   | BPM + intervalos R-R (ms)                   | GATT 0x2A37 (standard)      |
| `ble_realtime`      | HR + RR + bateria                           | custom 0x28 REALTIME_DATA   |
| `ble_events`        | wrist on/off, charge, sleep, button         | custom 0x30 EVENT           |
| `ble_metadata`      | temperatura cutânea, SpO₂, freq. respiratória | custom 0x31 METADATA      |
| `ble_accel`         | acelerómetro raw (XYZ, g)                   | custom 0x2B REALTIME_RAW    |
| `ble_imu`           | IMU completo (accel + giro)                 | custom 0x33 IMU stream      |
| `ble_historical`    | dump completo do buffer interno (~14 dias)  | custom 0x2F HISTORICAL_DATA |

Os dumps raw do historical também são preservados em
`exports/ble-historical/{date}.jsonl` (1 frame por linha, payload em hex)
para arqueologia futura.

## Por que isto não passa pela cloud Whoop

- O cliente fala **directamente** com o GATT da pulseira (`bleak` sobre BlueZ).
- Não há tokens OAuth, não há login, não há tráfego HTTP para `*.whoop.com`.
- Funciona offline. Funciona em modo avião com o BT ligado.
- Se a tua conta Whoop for banida, isto continua a recolher dados.

## Pré-requisito crítico: unpair da app oficial

A Whoop usa **bonding BLE exclusivo** — só uma central pode estar ligada de cada vez.
Tens de remover o emparelhamento na app Whoop:

1. Abre a app Whoop no telemóvel
2. Settings → Hardware → Strap → Forget device
3. Confirma
4. Faz scan local (`scripts/scan.py`) — agora a pulseira aparece advertindo

> ⚠️ Não desliga só o Bluetooth do telemóvel — a app pode tentar reconectar
> em background. Faz "forget device" mesmo.

## Protocolo (UUIDs)

```
Service               fd4b0001-cce1-4033-93ce-002d5875f58a
CMD_TO_STRAP          fd4b0002  (write-no-response)
CMD_FROM_STRAP        fd4b0003  (notify; ACK + respostas)
EVENTS_FROM_STRAP     fd4b0004  (notify; HR ~1Hz, wrist, bateria)
DATA_FROM_STRAP       fd4b0005  (notify; chunks de historical dump)

Standard GATT
0x180D / 0x2A37       Heart Rate Service / Measurement
0x180F / 0x2A19       Battery
0x180A                Device Information
```

## Formato do frame custom

```
0xAA | length(2 LE) | CRC8(header) | type | seq | cmd | payload | CRC32(4 LE)
```

CRC32 com `xor_output = 0xF43F44AC` (não-standard — implementado em
`whoop_ble/crc.py`, testado em `tests/test_crc.py`).

Packet types: `0x23 COMMAND`, `0x24 RESPONSE`, `0x28 REALTIME_DATA`,
`0x2B REALTIME_RAW_DATA`, `0x2F HISTORICAL_DATA`, `0x30 EVENT`,
`0x31 METADATA`, `0x33 IMU_STREAM`.

## Commands relevantes

| ID | Nome                          | Notas                                                     |
|----|-------------------------------|-----------------------------------------------------------|
| 3  | TOGGLE_REALTIME_HR            | activa stream HR via custom service                       |
| 10 | SET_CLOCK                     | **5.0 usa payload de 5 bytes** (uint32 epoch + tz flag)   |
| 14 | TOGGLE_GENERIC_HR_PROFILE     | activa o standard HR broadcast (0x2A37)                   |
| 22 | SEND_HISTORICAL_DATA          | inicia drain do buffer (~14 dias)                         |
| 26 | GET_BATTERY_LEVEL             | leitura pontual                                           |
| 81 | START_RAW_DATA                | accel raw                                                 |
| 82 | STOP_RAW_DATA                 |                                                           |
| 107| ENABLE_OPTICAL_DATA           | PPG raw                                                   |
| 108| TOGGLE_OPTICAL_MODE           |                                                           |

## Limitações honestas

- Os **scores oficiais Whoop** (Recovery, Strain, Sleep Performance) são
  calculados **server-side** em modelos proprietários. Não são bit-replicáveis
  a partir do raw, apenas aproximáveis. Esses ficam fora deste pipeline.
- O *layout* exacto dos payloads `0x31 METADATA` e `0x28 REALTIME_DATA` no
  firmware 5.0 ainda não está 100% confirmado. Os decoders fazem
  *best-effort* e preservam sempre o `payload_hex` para reanálise.
- A pulseira é **agressiva a desconectar** — daí o reconnect exponencial
  no daemon.

## Riscos

- A Whoop pode detectar uso não oficial (e.g. analytics no firmware) e
  revogar o acesso à conta cloud. Isto é **irrelevante** para este
  pipeline, que não usa a cloud.
- O firmware pode ser actualizado num future update *via app oficial* —
  mas se a app está desemparelhada, isso não acontece.
- BLE bonding é exclusivo: se voltares a parear com a app Whoop, este
  cliente perde a ligação até fazeres unpair de novo.

## Sanity checks que NÃO requerem hardware

```bash
# Os testes do parser/CRC correm offline:
cd ble && ../.venv/bin/python -m pytest tests/ -v
# Esperado: 26 passed
```

Se os testes passarem, a parte protocolar está correcta — só falta o
hardware aceitar a conexão (= unpair da app).

## Referências

- `Sivasai2207/WHOOP-Reverse-Engineering-5.0` — único repo público que
  confirma extracção 5.0 unsubscribed (Kotlin)
- `jogolden/whoomp` — referência canónica 4.0, 65+ commands mapeados
- `andyguzmaneth/whoop4-ble` — Python, historical drain
- `NikoKoll/WhoopBLE` — Swift, sequência completa de enable
- `project-whoopsie/whoopsie-protocol` — frame format formal
