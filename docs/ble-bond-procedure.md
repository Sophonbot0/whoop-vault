# Bond BLE WHOOP 5.0 → Linux (BlueZ + MediaTek hci0)

**Conseguido 2026-05-17 11:57 AM** após várias sessões anteriores falharem em todas as 5 IO capabilities. A causa real do bloqueio não era o SMP — era a ordem de operações do `bluetoothctl`.

## Pré-requisitos
- Strap em **pairing mode** (mantém o botão pressionado no strap até começar a piscar — confirmação do João).
- Strap **desemparelhada de qualquer central** (Android app, outro Linux). Há rejeições silenciosas quando um peer activo segura a sessão.
- BlueZ ≥ 5.85, kernel com `ll-privacy past-sender past-receiver`. Adaptador qualquer (MediaTek BT 5.4 confirmado).

## Sequência exacta que funciona

```bash
# Em vez de pair com IO-cap rebuscadas, segue ISTO:
bluetoothctl <<'EOF'
power on
agent off
agent NoInputNoOutput
default-agent
pairable on
remove AA:BB:CC:DD:EE:FF
scan on
EOF
# ESPERAR pelo "[NEW] Device AA:BB:CC:DD:EE:FF WHOOP …" — strap tem de estar advertising
# NÃO fazer scan off. Manter discovery activa.
bluetoothctl <<'EOF'
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
pair AA:BB:CC:DD:EE:FF
EOF
# Aguardar "Pairing successful".
bluetoothctl info AA:BB:CC:DD:EE:FF
# Espera: Paired: yes / Bonded: yes / Trusted: yes / Connected: yes
```

## Razão pela qual as tentativas anteriores falhavam

Os scripts expect anteriores faziam `scan off` antes de `pair`. BlueZ remove o device do cache (`[DEL] Device …`) e `pair` responde `Device AA:BB:CC:DD:EE:FF not available`. Não havia falha SMP — não havia sequer tentativa SMP. O `Failed to pair: AuthenticationFailed` em 5 IO caps era artefacto do mesmo bug (5x o mesmo erro de ordem).

Outras coisas que **não eram** o problema (validadas durante debug):
- IO capability — `NoInputNoOutput` (default agent) chegou.
- Privacy / ControllerMode — defaults BlueZ.
- `JustWorksRepairing` — não necessário.
- `bluetoothd --experimental` — não necessário.
- Reset bond Android — não necessário (strap pode bondar com novo central sem touch no telefone).

## Persistência

Após bond, a LTK fica em:
```
/var/lib/bluetooth/50:2E:91:D5:45:F3/AA:BB:CC:DD:EE:FF/info
```
Sobrevive reboots. Para invalidar e re-pair: `bluetoothctl remove AA:BB:CC:DD:EE:FF`.

## Após o bond — correr daemon

```bash
cd ~/projects/whoop-vault
WHOOP_BLE_MAC=AA:BB:CC:DD:EE:FF PYTHONPATH=ble \
  .venv/bin/python -m whoop_ble.daemon
```

Verificar entrada de dados:
```bash
sqlite3 data/whoop.db "SELECT COUNT(*) FROM ble_r52_frames;"
```

Primeiras frames apareceram ~30s após daemon start, na char `fd4b0004` (encrypted — confirma que o bond resolveu o SMP gating).

## Próximos passos pendentes

- [ ] Ajustar parser de frame para o layout extended real (packet_type 0x20 / 0x23 que estão a chegar não correspondem aos esperados — provável off-by-one no `??` byte do header extended).
- [ ] Daemon a popular as 9 tabelas decoded (`ble_realtime`, `ble_events`, `ble_metadata`, etc.) — actualmente só `ble_r52_frames` (catch-all raw).
- [ ] Historical buffer drain (~14 dias de dados antigos via cmd específico).
- [ ] Systemd user unit a auto-iniciar daemon após boot.
