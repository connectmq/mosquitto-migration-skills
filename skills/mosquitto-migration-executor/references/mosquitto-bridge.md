# Mosquitto Bridge to ConnectMQ

ConnectMQ does not open bridges itself, so the bridge runs on the existing Mosquitto (Mosquitto 1.6+; MQTT 5 bridge
needs 2.0+). It is one MQTT client connection to ConnectMQ and counts toward the plan's concurrent connections.

## Configuration

Put it in its own file (e.g. `/etc/mosquitto/conf.d/connectmq-bridge.conf`, loaded via `include_dir`) so removing
it at cutover is a single file delete.

```conf
connection connectmq
address BROKER_HOST:8883

# TLS: verify ConnectMQ's public certificate with the system CA store
bridge_capath /etc/ssl/certs
# Windows or minimal containers: bridge_cafile /path/to/public-root-ca.pem
bridge_insecure false

# Credentials issued in the ConnectMQ console
remote_username CONNECTMQ_USERNAME
remote_password CONNECTMQ_PASSWORD
# Must be the issued client ID or <ISSUED_ID>_<suffix>
remote_clientid ISSUED_ID_bridge

# Protocol: mqttv50 on Mosquitto 2.x, else mqttv311
bridge_protocol_version mqttv311
# Non-Mosquitto remote: do not use the private bridge flag
try_private false
# Do not publish $SYS/broker/connection/... state topics to ConnectMQ
notifications false

# Keep the session so QoS 1 messages survive short bridge reconnects
cleansession false
keepalive_interval 60
restart_timeout 5 60
start_type automatic

# Forward: Mosquitto -> ConnectMQ (what devices on Mosquitto publish)
topic devices/+/telemetry out 1
topic devices/+/status out 1
topic devices/+/events out 1

# Reverse: ConnectMQ -> Mosquitto (commands for devices still on Mosquitto)
topic devices/+/commands/# in 1
```

`topic <pattern> <direction> <qos> [<local-prefix> <remote-prefix>]`:

- `out` = local Mosquitto to ConnectMQ, `in` = ConnectMQ to local Mosquitto.
- Avoid `both` during a migration: once some devices publish on ConnectMQ and others on Mosquitto, `both` forwards
  each message in both directions and duplicates or loops are hard to rule out. Use separate `out` and `in` lines.
- Prefixes rewrite topics, e.g. `topic # out 1 "" site1/` publishes `devices/x` as `site1/devices/x` on ConnectMQ.
  Use this when several Mosquitto servers (sites) merge into one ConnectMQ broker.

Retained flags: Mosquitto forwards the retain flag on bridged messages, so retained state published **after** the
bridge starts reaches ConnectMQ. State published before must be copied with `copy_retained.py`.

## ACL rules for the bridge on ConnectMQ

Add these before enabling ACL (replace `ISSUED_ID_bridge` with the bridge's client ID):

| Permission | Action | Topic filter | Subject | Value |
| --- | --- | --- | --- | --- |
| allow | publish | `devices/+/telemetry` | clientid | `ISSUED_ID_bridge` |
| allow | publish | `devices/+/status` | clientid | `ISSUED_ID_bridge` |
| allow | publish | `devices/+/events` | clientid | `ISSUED_ID_bridge` |
| allow | subscribe | `devices/+/commands/#` | clientid | `ISSUED_ID_bridge` |

## Several Mosquitto servers

Each server gets its own bridge with its own client ID suffix (`ISSUED_ID_bridge-site1`, `..._bridge-site2`) and,
when topic trees overlap, its own remote prefix. Never run two bridges with the same `remote_clientid`: they would
take over each other's session in a loop (DISCONNECT `0x8E`).

## Checking the bridge

```bash
# Mosquitto side
journalctl -u mosquitto -f | grep -i bridge
# ConnectMQ side: subscribe with a test credential
mosquitto_sub -h BROKER_HOST -p 8883 --capath /etc/ssl/certs -u USER -P '***' -i ISSUED_ID_check -t 'devices/#' -v
# Compare both sides
python scripts/compare_streams.py --source mqtt://OLD_HOST:1883 --target mqtts://BROKER_HOST:8883 --filter 'devices/#' --duration 300
```

## When the Mosquitto config cannot change

Run the external relay instead (same topics, same loop rules):

```bash
python scripts/relay.py --source mqtt://OLD_HOST:1883 --source-client-id connectmq-relay \
  --target mqtts://BROKER_HOST:8883 \
  --forward 'devices/+/telemetry' --forward 'devices/+/status' --reverse 'devices/+/commands/#'
```

Run it under a supervisor (systemd, Docker `restart: always`). It prints counters every 30 s.

## Removing the bridge at cutover

1. Confirm no device publishes forward topics on Mosquitto any more (`compare_streams.py` shows no source traffic,
   or Mosquitto `$SYS/broker/clients/connected` is only the bridge).
2. Delete `connectmq-bridge.conf` and reload Mosquitto.
3. Remove the bridge ACL rules and credential on ConnectMQ.
