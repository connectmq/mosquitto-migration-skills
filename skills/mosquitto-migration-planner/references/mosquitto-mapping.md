# Mosquitto to ConnectMQ: Feature and Configuration Mapping

## Configuration mapping

| Mosquitto | ConnectMQ | Notes |
| --- | --- | --- |
| `listener 1883` | `mqtt://HOST:1883` | Tests only; production uses 8883 |
| `listener 8883` + `cafile/certfile/keyfile` | `mqtts://HOST:8883` | Public CA: clients use the OS CA store; drop custom `cafile` from clients unless their TLS stack has no CA store (then ship the public root CA) |
| `listener 9001` + `protocol websockets` | `wss://HOST:8084/mqtt` | Path is always `/mqtt`; browsers must use wss |
| `allow_anonymous true` | not available | Every client needs credentials |
| `password_file` | Credentials in the console | Cannot import hashes; issue new credentials |
| `acl_file` (`user`, `topic`, `pattern`) | Access control rules | `mosquitto_audit.py` converts; deny rules first |
| `%c`, `%u` in `pattern` | `${clientid}`, `${username}` | Values change (issued prefix, generated usernames): expand per client or adopt new IDs in topics |
| Dynamic Security plugin (clients, groups, roles) | Access control rules + credentials | Roles flattened per user; `publishClientReceive` has no equivalent |
| `plugin` / `auth_plugin` (go-auth, HTTP, JWT, DB) | not available | Users move to ConnectMQ credentials |
| `require_certificate true`, `use_identity_as_username` | not available | Move to username/password over TLS |
| `psk_hint`, `psk_file` | not available | Move to TLS + username/password |
| `max_connections` | Plan concurrent connections | Free 5, Basic 100, Scale 1,000 |
| `persistent_client_expiration` | Plan max session expiry | 1 h / 2 h / 24 h; Mosquitto default is never expire |
| `max_queued_messages`, `max_queued_bytes` | Broker-managed | Queue lives only as long as the session |
| `message_size_limit`, `max_packet_size` | Plan max message size | 256 KB on Free/Basic |
| `max_inflight_messages` | Receive maximum set by broker | No change needed |
| `max_keepalive` | 600 s cap | Clients with keep-alive > 600 s are capped |
| `retain_available` | Retained on every plan | Count limited per plan |
| `persistence true` | Managed | Copy retained messages with `copy_retained.py`; sessions are not copied |
| `allow_zero_length_clientid`, `auto_id_prefix` | not available | Clients must send `<ISSUED_ID>_<suffix>` |
| `connection` (bridge) | Keep on Mosquitto | ConnectMQ does not bridge out; see executor bridge guide |
| `$SYS/#` | not available | Use the console dashboard and sessions view |
| `mount_point` | Topic prefix | Add the prefix in the topic tree or with the bridge's local/remote prefix |
| `http_dir` | not available | Host static web apps elsewhere |
| `log_dest`, `log_type` | Console sessions view | Connection history with disconnect reasons |

## Behavioural differences that bite

- **Client ID rules.** Mosquitto accepts any client ID; ConnectMQ only accepts the issued ID or `<ISSUED_ID>_<suffix>`.
  Devices that build the ID from a MAC address keep doing so with the prefix in front.
- **Anonymous clients.** Common on internal Mosquitto deployments. Every such client must be found (Mosquitto log:
  `New client connected ... u''`) and given credentials.
- **Infinite sessions.** Mosquitto keeps a persistent session forever by default. Devices that sleep for days and
  expect queued commands on wake-up will lose them on ConnectMQ after the plan's session expiry. Use retained
  "desired state" topics for configuration instead of queued commands.
- **Multiple `pattern` rules and deny precedence.** In Mosquitto a matching deny always wins regardless of order;
  ConnectMQ is first-match. The converter puts deny rules first; review rules where an allow should beat a broader
  deny.
- **Anonymous ACL section.** `topic` lines before the first `user` line apply to anonymous clients only; they are
  skipped by the converter.
- **`$SYS` consumers.** Monitoring that reads `$SYS` (Telegraf mqtt_consumer, Prometheus exporters, Node-RED flows)
  stops working; plan replacement dashboards.
- **Bridge notification topics.** Mosquitto bridges publish `$SYS/broker/connection/<name>/state`; set
  `notifications false` on bridges to ConnectMQ.
- **Single-threaded ordering.** Mosquitto delivers everything in one thread. ConnectMQ keeps per-client ordering as
  the MQTT spec requires, but interleaving between different publishers can differ. Consumers that infer order
  across devices must use timestamps in the payload.
- **MQTT 3.1 clients** (protocol level 3, very old libraries) must be upgraded to 3.1.1 or 5.
- **Topic case and trailing slashes.** `devices/A` and `devices/a`, `x/` and `x` are different topics everywhere;
  migrating is a good time to find inconsistent publishers (`compare_streams.py` lists topics per side).

## Mosquitto migration sources of information

| Need | Command / file |
| --- | --- |
| Version | `mosquitto -h` |
| Connected clients now | `$SYS/broker/clients/connected` |
| Peak clients since start | `$SYS/broker/clients/maximum` |
| Retained count | `$SYS/broker/retained messages/count` |
| Message rates | `$SYS/broker/load/messages/{received,sent}/1min` |
| Byte rates (traffic fair use) | `$SYS/broker/load/bytes/{received,sent}/1min` |
| Client IDs and usernames | `log_type all` log lines `New client connected from IP as ID (... u'USER')` |
| Dynamic Security config | `mosquitto_ctrl dynsec getClient/listRoles` or the JSON file |

## Sizing example

Peak 80 clients, all always on, 2 dashboards, 1 bridge: 83 concurrent connections -> Basic (100). Session minutes:
83 x 43,200 = 3.59 M per month -> more than Basic's 1 M -> Scale (10 M), or reduce connected time (devices that
publish every 15 minutes can disconnect between reports). Always check both limits.
