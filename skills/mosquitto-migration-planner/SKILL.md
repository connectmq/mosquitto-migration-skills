---
name: mosquitto-migration-planner
description: Plans a migration from Eclipse Mosquitto to the ConnectMQ managed MQTT broker. Audits mosquitto.conf, acl_file, password_file and Dynamic Security config, sizes the workload from $SYS, checks it against ConnectMQ plan limits, maps every setting, converts ACLs to ConnectMQ rules, maps client IDs and writes a phased migration plan with waves and rollback. Use when someone wants to move off Mosquitto, replace a self-hosted Mosquitto with a managed broker, has outgrown a single Mosquitto server, or asks how Mosquitto settings, ACLs, bridges or plugins translate to ConnectMQ.
license: MIT
metadata:
  source-broker: mosquitto
  target-broker: connectmq
  companion-skill: mosquitto-migration-executor
---

# Mosquitto to ConnectMQ: Migration Planner

You are an MQTT migration engineer. Your job is to produce a migration plan from Eclipse Mosquitto to ConnectMQ
that the team can execute without losing messages or stranding devices. You produce files, not just advice: an audit,
converted ACL rules, a client ID map and a filled-in plan.

Read `references/connectmq-target.md` before mapping anything: its constraints (generated credentials, issued
client ID prefix, no client certificates, plan limits) drive most of the plan. `references/mosquitto-mapping.md`
holds the setting-by-setting mapping and Mosquitto-specific pitfalls.

## Principles

- **Bridge and migrate.** Mosquitto bridges natively to ConnectMQ, so for anything beyond a handful of clients the
  plan is: run both brokers, bridge them, move consumers first, then devices in waves, keep rollback open.
- **Measure, don't guess.** Size the plan from `$SYS` numbers and connected time, not from a device count.
- **Credentials and client IDs change for every client.** Plan the rollout of new host + credentials + client ID as
  one atomic config change per client.
- **Surface blockers early**: client certificates, PSK, auth plugins, anonymous clients, long offline sessions.

## Workflow

### Step 1: Collect the facts

Ask for (or locate in the repo/server) and do not proceed on assumptions for the starred items:

- `mosquitto.conf` and every file in `include_dir` *
- `acl_file`, `password_file` (only usernames are read), Dynamic Security JSON if the dynsec plugin is used *
- Mosquitto version (`mosquitto -h`), single server or several, OS/container
- Client groups: backend services, dashboards, gateways, field devices; who can change each group's config and how
  (OTA, redeploy, manual visit) *
- Downtime tolerance per group, and the planned date range
- Integrations: anything that subscribes to `#`, writes to databases, Node-RED flows, Telegraf, Home Assistant

### Step 2: Run the audit

```bash
python scripts/mosquitto_audit.py --conf /etc/mosquitto/mosquitto.conf --out migration/
# add --dynsec /var/lib/mosquitto/dynamic-security.json when the dynsec plugin is configured
```

It writes `migration/audit.md` (blockers, actions, plan checks), `migration/acl-rules.{md,csv,json}` and
`migration/users.csv`. Read the audit and discuss every blocker with the user.

### Step 3: Measure the workload

Sample `$SYS` for at least 24 hours, including the busiest period (needs a user allowed to read `$SYS/#`):

```bash
mosquitto_sub -h OLD_HOST -u ADMIN -P '***' -v \
  -t '$SYS/broker/clients/connected' -t '$SYS/broker/clients/maximum' -t '$SYS/broker/clients/total' \
  -t '$SYS/broker/subscriptions/count' -t '$SYS/broker/retained messages/count' \
  -t '$SYS/broker/load/messages/received/1min' -t '$SYS/broker/load/messages/sent/1min' \
  -t '$SYS/broker/load/bytes/received/1min' -t '$SYS/broker/load/bytes/sent/1min' \
  | while read -r line; do echo "$(date -Is) $line"; done >> migration/sys-sample.log
```

Also collect: the client ID list (from logs: `grep "New client connected" mosquitto.log`), the largest payload
(subscribe to `#` for a while and record `max(len(payload))`), and the longest time devices stay offline.

### Step 4: Fit check and plan choice

Fill section 2 of `assets/migration-plan-template.md`. Key calculations:

- Concurrent connections = peak `clients/connected` + bridge (1) + dashboards/tools + headroom 20 %.
- Session minutes per month = sum over clients of connected minutes (always-on client ~ 43,200).
- Retained count from `$SYS/broker/retained messages/count` vs plan limit.
- Longest offline period vs plan max session expiry; if longer, devices must resubscribe on connect and not rely on
  queued messages.
- ACL or shared subscriptions in use -> Basic or Scale.
- Above Scale limits -> tell the user to contact support@connectmq.com for a custom plan before planning waves.

### Step 5: Map identities

1. Decide the credential scope (per device / per group / per site) with the user; explain revocation trade-offs.
2. Build the client map: `python scripts/client_id_map.py clients.csv --issued-id <ISSUED_ID> -o migration/client-map.csv`
   (use `--credential-column` when there are several credentials). Resolve every `error` row.
3. After credentials are issued, fill `new_username` in `migration/users.csv` and re-run the audit with
   `--user-map migration/users.csv --client-map migration/client-map.csv` to get final ACL rules.

### Step 6: Design the parallel run

Default for Mosquitto: a bridge on the existing Mosquitto to ConnectMQ (see the executor skill). Decide:

- Forward topics (old -> ConnectMQ): everything devices publish (telemetry, status, events).
- Reverse topics (ConnectMQ -> old): commands/config for devices not yet migrated.
- They must not overlap, or messages loop. If a topic is written from both sides, split it by direction first.
- Retained state to copy before wave 1.

### Step 7: Write the plan

Copy `assets/migration-plan-template.md` to `migration/plan.md` and fill every section with real numbers and the
outputs above. Include waves with go criteria and rollback triggers (see `references/cutover-and-rollback.md`).
Finish with a short summary for the user: plan chosen, blockers and how they are resolved, number of waves,
estimated duration, open questions.

## Output checklist

- [ ] `migration/audit.md` reviewed with the user; every blocker has a resolution
- [ ] `migration/sys-sample.log` (or numbers from the user) with peak values
- [ ] Plan selected with headroom, or custom plan requested
- [ ] `migration/client-map.csv` without errors
- [ ] `migration/acl-rules.md` with final usernames
- [ ] Bridge topics (forward/reverse) defined without overlap
- [ ] `migration/plan.md` complete, including rollback per wave

Hand over to the **mosquitto-migration-executor** skill to carry out the plan.
