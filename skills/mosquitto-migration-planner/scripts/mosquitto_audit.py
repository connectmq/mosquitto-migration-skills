#!/usr/bin/env python3
"""Audits a Mosquitto installation for a migration to ConnectMQ.

Reads mosquitto.conf (following include_dir), the acl_file, the password_file (usernames only, never hashes) and an
optional Dynamic Security plugin JSON, then writes:

  <out>/audit.md          findings: every setting mapped to ConnectMQ, with blockers and actions
  <out>/acl-rules.md/.csv ConnectMQ access control rules converted from acl_file / dynamic-security.json
  <out>/users.csv         old usernames, with an empty column for the new ConnectMQ username (fill in, then pass
                          back with --user-map to get rules for the new usernames)

Examples:
  python mosquitto_audit.py --conf /etc/mosquitto/mosquitto.conf --out migration/
  python mosquitto_audit.py --conf mosquitto.conf --client-map client-map.csv --user-map users.csv --out migration/
  python mosquitto_audit.py --acl aclfile --out migration/                       # ACL only
  python mosquitto_audit.py --dynsec /var/lib/mosquitto/dynamic-security.json --out migration/

--client-map: CSV old_client_id,new_client_id (from client_id_map.py). Pattern rules using %c are expanded into
one rule per client so topics keep the old client ID.
--user-map:   CSV old_username,new_username. Rules for a user get the new ConnectMQ username as subject; pattern
rules with %u are expanded per user.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from connectmq_acl import Rule, check, dedupe, expand_pattern, load_map, render  # noqa: E402

# setting -> (severity, ConnectMQ mapping / action)
MAPPING = {
    "listener": ("info", "ConnectMQ listens on 8883 (TLS), 8084 (wss, /mqtt), 1883 and 8083 (tests only). Clients change host and port."),
    "port": ("info", "Deprecated Mosquitto option; clients move to 8883 (TLS)."),
    "protocol": ("info", "protocol websockets -> wss://HOST:8084/mqtt (path is always /mqtt)."),
    "allow_anonymous": ("action", "ConnectMQ has no anonymous access: every client needs credentials issued in the console."),
    "password_file": ("action", "Usernames/passwords cannot be imported. Issue new ConnectMQ credentials (see users.csv)."),
    "acl_file": ("action", "Converted to ConnectMQ access control rules (acl-rules.md). Per-topic ACL needs Basic or Scale."),
    "plugin": ("blocker", "Mosquitto plugins do not run on ConnectMQ. Review what the plugin does (auth backend, dynsec, custom logic) and replace it."),
    "auth_plugin": ("blocker", "Auth plugins (mosquitto-go-auth, HTTP/JWT/DB backends) are not supported. Move users to ConnectMQ credentials."),
    "plugin_opt_config_file": ("info", "Dynamic Security config: pass it with --dynsec to convert roles to ACL rules."),
    "require_certificate": ("blocker", "Client certificates (mTLS) are not supported. Devices move to username/password over TLS."),
    "use_identity_as_username": ("blocker", "Certificate identity as username is not supported; issue credentials per device."),
    "use_subject_as_username": ("blocker", "Certificate subject as username is not supported; issue credentials per device."),
    "psk_hint": ("blocker", "TLS-PSK is not supported; use TLS with the public server certificate and username/password."),
    "psk_file": ("blocker", "TLS-PSK is not supported."),
    "cafile": ("info", "ConnectMQ uses a public CA certificate: clients can use the OS CA store."),
    "certfile": ("info", "Server certificate is managed by ConnectMQ."),
    "keyfile": ("info", "Server key is managed by ConnectMQ."),
    "max_connections": ("check", "Compare with the plan's concurrent connections (Free 5, Basic 100, Scale 1,000)."),
    "max_inflight_messages": ("info", "ConnectMQ sets receive-maximum per plan; no client change needed."),
    "max_queued_messages": ("check", "Offline queueing is limited by the plan's session expiry (1 h / 2 h / 24 h)."),
    "max_queued_bytes": ("check", "Offline queueing is limited by the plan's session expiry."),
    "persistence": ("info", "ConnectMQ keeps retained messages and sessions; copy retained messages with copy_retained.py."),
    "persistence_location": ("info", "Not applicable."),
    "autosave_interval": ("info", "Not applicable."),
    "persistent_client_expiration": ("check", "ConnectMQ caps session expiry per plan (Free 1 h, Basic 2 h, Scale 24 h). Devices offline longer lose queued messages."),
    "max_packet_size": ("check", "Plan max message size (256 KB on Free/Basic). Find the largest payload on the source."),
    "message_size_limit": ("check", "Plan max message size (256 KB on Free/Basic)."),
    "retain_available": ("check", "Retained messages are available; the plan caps the count (100 / 1,000 / 10,000)."),
    "max_keepalive": ("info", "ConnectMQ caps keep-alive at 600 s."),
    "allow_zero_length_clientid": ("action", "Clients must send a client ID: the issued ID or <ISSUED_ID>_<suffix>."),
    "auto_id_prefix": ("action", "Generated client IDs are not accepted; clients must send <ISSUED_ID>_<suffix>."),
    "connection": ("action", "Bridge: replace with a bridge from this Mosquitto to ConnectMQ during the parallel run, then remove."),
    "sys_interval": ("info", "$SYS is not provided; use the ConnectMQ dashboard and sessions view."),
    "upgrade_outgoing_qos": ("info", "No equivalent; subscribers get min(publish QoS, subscription QoS) as in the spec."),
    "allow_duplicate_messages": ("info", "Not applicable."),
    "queue_qos0_messages": ("check", "Do not rely on QoS 0 queueing for offline clients; use QoS 1."),
    "http_dir": ("info", "Mosquitto's static HTTP server is not provided; host web apps elsewhere."),
    "mount_point": ("action", "Mount points prefix topics per listener; add the prefix to the topic tree or strip it in the bridge."),
    "bind_interface": ("info", "Not applicable."),
}


def read_conf(path: str, seen: set | None = None) -> list[tuple[str, str, str]]:
    """Returns (file, key, value) for every setting, following include_dir."""
    seen = seen or set()
    path = os.path.abspath(path)
    if path in seen:
        return []
    seen.add(path)
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition(" ")
            value = value.strip()
            if key == "include_dir":
                inc = value if os.path.isabs(value) else os.path.join(os.path.dirname(path), value)
                for p in sorted(glob.glob(os.path.join(inc, "*.conf"))):
                    out += read_conf(p, seen)
                continue
            out.append((path, key, value))
    return out


def parse_acl_file(path: str) -> list[tuple[str | None, str, str, str]]:
    """Returns (user or None, kind 'topic'|'pattern', access, topic). user=None before the first 'user' line means
    anonymous clients; pattern lines apply to every client."""
    entries, user = [], None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            kw = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            if kw == "user":
                user = rest.strip()
            elif kw in ("topic", "pattern"):
                tokens = rest.split(None, 1)
                if tokens and tokens[0] in ("read", "write", "readwrite", "deny") and len(tokens) > 1:
                    access, topic = tokens[0], tokens[1].strip()
                else:
                    access, topic = "readwrite", rest.strip()
                entries.append((user if kw == "topic" else "*", kw, access, topic))
    return entries


ACCESS = {"read": ["subscribe"], "write": ["publish"], "readwrite": ["all"], "deny": ["all"]}


def acl_to_rules(entries, user_map, client_map, notes):
    deny, allow = [], []
    anon = [e for e in entries if e[0] is None]
    if anon:
        notes.append(f"{len(anon)} rule(s) for anonymous clients were skipped: ConnectMQ has no anonymous access. "
                     "Give those clients credentials and add rules for them.")
    for user, kind, access, topic in entries:
        if user is None:
            continue
        warnings = []
        t = topic.replace("%c", "${clientid}").replace("%u", "${username}")
        if t.startswith("$SYS"):
            notes.append(f"Skipped $SYS rule '{topic}' ({user}): ConnectMQ does not publish $SYS topics.")
            continue
        for action in ACCESS[access]:
            perm = "deny" if access == "deny" else "allow"
            if kind == "pattern":
                rule = Rule(perm, action, t, "all", "", f"pattern {access} {topic}")
                rules = [rule]
                if "${clientid}" in t:
                    if client_map:
                        rules = expand_pattern(rule, client_map, "${clientid}")
                    else:
                        rule.warnings.append("${clientid} is now the full ConnectMQ ID (<ISSUED>_<old>); pass "
                                             "--client-map to keep old IDs in topics")
                if "${username}" in t:
                    if user_map:
                        rules = [x for r in rules for x in expand_pattern(r, user_map, "${username}", "username")]
                    else:
                        for r in rules:
                            r.warnings.append("${username} is now the generated ConnectMQ username; pass --user-map")
            else:
                new_user = user_map.get(user)
                if not new_user:
                    warnings.append(f"old username '{user}': replace with its ConnectMQ username")
                rules = [Rule(perm, action, t, "username", new_user or user, f"user {user}: topic {access} {topic}",
                              warnings)]
            (deny if perm == "deny" else allow).extend(rules)
    # Mosquitto: a matching deny always wins, so deny rules go first in ConnectMQ's first-match list
    return dedupe(deny + allow)


DYNSEC_ACTIONS = {
    "publishclientsend": "publish",
    "subscribeliteral": "subscribe",
    "subscribepattern": "subscribe",
}


def dynsec_to_rules(path, user_map, notes):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    roles = {r["rolename"]: r for r in data.get("roles", [])}
    groups = {g["groupname"]: g for g in data.get("groups", [])}
    out_rules = []
    default = data.get("defaultACLAccess", {})
    if default:
        notes.append(f"Dynamic Security defaultACLAccess = {default}. ConnectMQ has one no-match policy for publish "
                     "and subscribe; the rules assume 'deny'.")
    anon_group = data.get("anonymousGroup")
    if anon_group:
        notes.append(f"anonymousGroup '{anon_group}' skipped: no anonymous access on ConnectMQ.")

    def role_rules(rolename, subject_type, subject_value, why):
        role = roles.get(rolename)
        if not role:
            return []
        res = []
        for acl in sorted(role.get("acls", []), key=lambda a: -a.get("priority", 0)):
            kind = acl.get("acltype", "").lower()
            topic = acl.get("topic", "").replace("%c", "${clientid}").replace("%u", "${username}")
            if kind not in DYNSEC_ACTIONS:
                notes.append(f"role {rolename}: acltype {acl.get('acltype')} on '{acl.get('topic')}' has no ConnectMQ "
                             "equivalent (publishClientReceive/unsubscribe ACLs are not supported) - review manually.")
                continue
            rule = Rule("allow" if acl.get("allow") else "deny", DYNSEC_ACTIONS[kind], topic, subject_type,
                        subject_value, f"{why} role {rolename} ({acl.get('acltype')}, priority {acl.get('priority', 0)})")
            if "${clientid}" in topic:
                rule.warnings.append("${clientid} is now the full ConnectMQ ID (<ISSUED>_<old>): keep it in the topic "
                                     "tree or replace with explicit per-device topics")
            res.append(rule)
        return res

    for client in data.get("clients", []):
        user = client["username"]
        new_user = user_map.get(user)
        warn = [] if new_user else [f"old username '{user}': replace with its ConnectMQ username"]
        names = [r["rolename"] for r in client.get("roles", [])]
        for g in client.get("groups", []):
            names += [r["rolename"] for r in groups.get(g["groupname"], {}).get("roles", [])]
        for name in names:
            for r in role_rules(name, "username", new_user or user, f"client {user}:"):
                r.warnings += warn
                out_rules.append(r)
        if client.get("clientid"):
            notes.append(f"client {user} is pinned to client ID '{client['clientid']}': map it with client_id_map.py.")
    deny = [r for r in out_rules if r.permission == "deny"]
    allow = [r for r in out_rules if r.permission == "allow"]
    notes.append("Dynamic Security priorities were flattened: deny rules first, then allow rules. Check roles that "
                 "relied on a higher-priority allow overriding a deny.")
    return dedupe(deny + allow), [c["username"] for c in data.get("clients", [])]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", help="mosquitto.conf")
    ap.add_argument("--acl", help="acl_file (default: the one named in mosquitto.conf)")
    ap.add_argument("--passwords", help="password_file (default: the one named in mosquitto.conf)")
    ap.add_argument("--dynsec", help="Dynamic Security plugin JSON")
    ap.add_argument("--client-map", help="CSV old_client_id,new_client_id")
    ap.add_argument("--user-map", help="CSV old_username,new_username")
    ap.add_argument("--out", default="migration", help="Output directory (default ./migration)")
    args = ap.parse_args()
    if not (args.conf or args.acl or args.dynsec):
        ap.error("give --conf, --acl or --dynsec")
    os.makedirs(args.out, exist_ok=True)

    client_map = load_map(args.client_map, "old_client_id", "new_client_id")
    user_map = {k: v for k, v in load_map(args.user_map, "old_username", "new_username").items() if v}

    settings = read_conf(args.conf) if args.conf else []
    conf_dir = os.path.dirname(os.path.abspath(args.conf)) if args.conf else "."

    def resolve(p):
        return p if os.path.isabs(p) else os.path.join(conf_dir, p)

    acl_path = args.acl or next((resolve(v) for _, k, v in settings if k == "acl_file"), None)
    pw_path = args.passwords or next((resolve(v) for _, k, v in settings if k == "password_file"), None)

    findings = {"blocker": [], "action": [], "check": [], "info": [], "unknown": []}
    bridges = []
    for file, key, value in settings:
        if key == "connection":
            bridges.append({"name": value, "file": file, "settings": []})
        elif bridges and key in ("address", "addresses", "topic", "remote_username", "remote_clientid",
                                 "bridge_protocol_version", "cleansession"):
            bridges[-1]["settings"].append(f"{key} {value if 'password' not in key else '***'}")
        sev, text = MAPPING.get(key, ("unknown", "Review manually."))
        if key in ("require_certificate", "use_identity_as_username", "use_subject_as_username", "allow_anonymous",
                   "allow_zero_length_clientid") and value.lower() == "false":
            sev, text = "info", "Disabled on the source; nothing to migrate."
        if key in ("address", "addresses", "topic", "remote_username", "remote_password", "remote_clientid",
                   "bridge_protocol_version", "cleansession", "local_clientid", "notifications", "try_private",
                   "start_type", "restart_timeout", "bridge_cafile", "bridge_capath", "bridge_insecure"):
            continue  # reported with the bridge
        shown = "***" if "pass" in key else value
        findings[sev].append(f"`{key} {shown}` ({os.path.basename(file)}): {text}")

    notes: list[str] = []
    rules: list[Rule] = []
    if acl_path and os.path.exists(acl_path):
        rules += acl_to_rules(parse_acl_file(acl_path), user_map, client_map, notes)
    elif acl_path:
        notes.append(f"acl_file {acl_path} not found; pass --acl.")
    users = []
    if args.dynsec:
        dyn_rules, dyn_users = dynsec_to_rules(args.dynsec, user_map, notes)
        rules += dyn_rules
        users += dyn_users
    if pw_path and os.path.exists(pw_path):
        with open(pw_path, encoding="utf-8", errors="replace") as f:
            users += [line.split(":", 1)[0].strip() for line in f if ":" in line and not line.startswith("#")]
    users = sorted(set(users))

    problems = check(rules)
    if rules:
        for fmt, ext in (("markdown", "md"), ("csv", "csv"), ("json", "json")):
            with open(os.path.join(args.out, f"acl-rules.{ext}"), "w", encoding="utf-8", newline="") as f:
                f.write(render(rules, "deny", fmt, notes + problems))
    if users:
        with open(os.path.join(args.out, "users.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["old_username", "new_username", "credential_scope"])
            for u in users:
                w.writerow([u, user_map.get(u, ""), ""])

    lines = ["# Mosquitto -> ConnectMQ audit", ""]
    if args.conf:
        lines.append(f"Configuration: `{args.conf}` ({len(settings)} settings)")
    lines += [f"ACL file: `{acl_path or '-'}`", f"Password file: `{pw_path or '-'}` ({len(users)} users)", ""]
    titles = {"blocker": "Blockers (must be redesigned)", "action": "Actions (change during migration)",
              "check": "Check against the ConnectMQ plan", "info": "Information", "unknown": "Not recognised (review)"}
    for sev in ("blocker", "action", "check", "info", "unknown"):
        if findings[sev]:
            lines += [f"## {titles[sev]}", ""] + [f"- {x}" for x in findings[sev]] + [""]
    if bridges:
        lines += ["## Existing bridges", "",
                  "Each bridge must be re-created: either keep it on this Mosquitto (it keeps running during the parallel "
                  "run) or move the remote broker's traffic to ConnectMQ.", ""]
        for b in bridges:
            lines += [f"- **{b['name']}** ({os.path.basename(b['file'])}): " + "; ".join(b["settings"])]
        lines.append("")
    lines += ["## Access control", "",
              f"{len(rules)} ConnectMQ rules generated -> `acl-rules.md`." if rules else "No ACL rules found.", ""]
    if notes or problems:
        lines += ["### Notes", ""] + [f"- {n}" for n in notes + problems] + [""]
    lines += ["## Next steps", "",
              "1. Fill `users.csv` new_username after issuing ConnectMQ credentials, then re-run with --user-map.",
              "2. Build the client map: `python client_id_map.py clients.csv --issued-id <ID> -o client-map.csv`.",
              "3. Sample `$SYS` for 24 h (see the planner skill) to size the plan.",
              "4. Resolve every blocker before scheduling the first wave.", ""]
    with open(os.path.join(args.out, "audit.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"wrote {args.out}/audit.md ({sum(len(v) for v in findings.values())} findings, "
          f"{len(findings['blocker'])} blockers), {len(rules)} ACL rules, {len(users)} users")
    return 0


if __name__ == "__main__":
    sys.exit(main())
