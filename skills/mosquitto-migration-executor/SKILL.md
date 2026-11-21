---
name: mosquitto-migration-executor
description: Executes a planned migration from Eclipse Mosquitto to the ConnectMQ managed MQTT broker step by step. Verifies the ConnectMQ broker, configures a Mosquitto bridge to ConnectMQ, copies retained messages, validates message flow on both brokers, moves clients in waves with go/no-go checks, handles rollback and decommissions the old broker, keeping a migration log. Use when a Mosquitto-to-ConnectMQ plan exists and the team is ready to set up the bridge, run a wave, cut over, roll back or troubleshoot connection errors during the move.
license: MIT
metadata:
  source-broker: mosquitto
  target-broker: connectmq
  companion-skill: mosquitto-migration-planner
---

# Mosquitto to ConnectMQ: Migration Executor

You carry out a Mosquitto to ConnectMQ migration safely, one verified step at a time. You change configuration,
run the scripts, read the results, and stop for a human decision at every go/no-go point.

Before starting, read:

- `migration/plan.md` (from the planner skill). If it does not exist, offer to run the
  **mosquitto-migration-planner** skill first; do not improvise waves without a plan.
- `references/connectmq-target.md` for limits and reason codes.
- `references/mosquitto-bridge.md` for the bridge configuration.
- `references/cutover-and-rollback.md` for wave rules, go/no-go and rollback.

## Rules

- **Ask before every production change** (bridge config reload, ACL change, client wave, stopping a broker), and say
  what will change and how to undo it.
- Never put passwords in files that get committed. Use environment variables (`CONNECTMQ_USERNAME`,
  `CONNECTMQ_PASSWORD`, `CONNECTMQ_CLIENT_ID`, `SOURCE_USERNAME`, `SOURCE_PASSWORD`) or a secrets store.
- After each step append an entry to `migration/log.md`: time, step, command, result, numbers, decision.
- If a check fails, stop, diagnose using the reason-code table, and propose a fix or a rollback. Do not continue to
  the next wave with a failing check.

## Scripts

All in `scripts/` (Python 3.9+, `pip install "paho-mqtt>=2.0"`):

| Script | Purpose |
| --- | --- |
| `verify_connectmq.py` | Connect, QoS 0/1/2, retained, will, session, ACL deny checks |
| `copy_retained.py` | Copy retained messages from Mosquitto to ConnectMQ (or export/import JSON) |
| `compare_streams.py` | Subscribe on both brokers and compare topics, counts and payloads |
| `relay.py` | External bridge when the Mosquitto config cannot be changed |
| `client_id_map.py` | Old -> new client ID mapping (if not done in planning) |

## Phase 1: Prepare ConnectMQ

1. Confirm the ConnectMQ plan in the plan matches the console (limits, ACL availability).
2. Create the credentials listed in the plan. One extra credential (or a `_bridge` suffix on an existing one) for
   the bridge, one for tests.
3. Enter ACL rules from `migration/acl-rules.md` in order, set the no-match policy, and **add rules for the bridge
   client first** (it must publish forward topics and subscribe to reverse topics). Enable ACL only after the rules
   are in.
4. Smoke test:

   ```bash
   export CONNECTMQ_USERNAME=... CONNECTMQ_PASSWORD=... CONNECTMQ_CLIENT_ID=...
   python scripts/verify_connectmq.py --url mqtts://BROKER_HOST:8883 --prefix <a topic branch the test credential may use>
   ```

   Add `--expect-deny-publish` / `--expect-deny-subscribe` for topics the ACL must block.
   If the persistent-session check fails, check the plan's session expiry before relying on offline queueing.

## Phase 2: Bridge

1. Write the bridge config from `references/mosquitto-bridge.md` using the forward/reverse topics of the plan.
2. Validate the config on a test Mosquitto or with `mosquitto -c new.conf -v` in a container before touching
   production. Then reload production Mosquitto (`systemctl reload mosquitto`, or `kill -HUP` on the PID; a full
   restart drops all clients).
3. Check the bridge connected: Mosquitto log shows `Connecting bridge connectmq` without errors; the ConnectMQ
   console shows the bridge client ID online.
4. Copy retained state: `python scripts/copy_retained.py --source mqtt://OLD_HOST:1883 --filter '#' --export migration/retained-<date>.json --target mqtts://BROKER_HOST:8883 --limit <plan retained limit>`.
   (Use `--dry-run` first and filter out `$SYS`, test and junk topics.)
5. Validate for at least one business cycle: `python scripts/compare_streams.py --source ... --target ... --filter '<forward filters>' --duration 600`.
   Expect MATCH. Investigate any "only on source" topic (ACL deny on the bridge, filter typo, oversized payload).

## Phase 3: Waves

For each wave in the plan:

1. Run the go/no-go checklist from `references/cutover-and-rollback.md` and get an explicit "go" from the user.
2. Change the clients of the wave: host `BROKER_HOST`, port 8883 (TLS on), new username/password, client ID from
   `migration/client-map.csv`. Show the user the exact config diff for their client type (code, env, device config).
3. Watch for 15 minutes: ConnectMQ console connected count rises by the wave size, source count falls, no
   `0x85/0x86/0x87/0x8A/0x97` in client logs, `compare_streams.py` still MATCH.
4. Soak for the agreed time, log the numbers, and only then propose the next wave.
5. On a rollback trigger, follow the rollback actions and log the cause.

Typical client changes:

```text
host: old-broker.local        -> BROKER_HOST
port: 1883                    -> 8883 (TLS, verify hostname, system CA store)
username/password: old        -> ConnectMQ credential
client_id: sensor-0042        -> <ISSUED_ID>_sensor-0042
keepalive: >600               -> <= 600
reconnect: fixed 1 s          -> exponential backoff with jitter, max 60 s
```

## Phase 4: Cutover and decommission

Follow "Final cutover" in `references/cutover-and-rollback.md`: final retained copy, remove the bridge `connection`
block and reload, watch for stragglers in the Mosquitto log (`New client connected`), keep Mosquitto available
for the agreed rollback window, then decommission. Remove `$SYS` based monitoring and old credentials.

## Troubleshooting quick reference

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Bridge log `Connection Refused: not authorised` / `0x86` | Wrong `remote_username`/`remote_password` | Copy from console |
| Bridge connects and drops at once | `remote_clientid` not `<ISSUED_ID>_<suffix>` (0x85), or same ID used twice (0x8E) | Fix `remote_clientid`, one bridge instance only |
| Bridge connects, nothing arrives on ConnectMQ | ACL denies bridge publish, or `topic` direction wrong (`in` vs `out`) | Add bridge ACL rule; `out` = Mosquitto -> ConnectMQ |
| Messages loop / duplicate | Same pattern `both` while devices exist on both sides, or overlapping in/out | Split topics by direction; avoid `both` |
| TLS error on bridge | No CA configured | `bridge_capath /etc/ssl/certs` (Linux) or `bridge_cafile` with the public root CA |
| `$SYS/broker/connection/...` denied | Bridge notifications | `notifications false` |
| Devices `0x8A` banned | Wrong credentials retried in a loop from one NAT | Stop the wave, fix config, wait for the ban to expire |
| `0x97` on connect | Plan connections or minutes exhausted | Upgrade plan or slow the wave |
| `0x97` on publish | Payload above plan max size, or per-client rate | Reduce payload/rate, or custom plan |
