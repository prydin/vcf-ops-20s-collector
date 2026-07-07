# Developer notes

Design overview and gotchas for `push_metrics.py`. Read this before changing the
script. It is written to be useful to both humans and AI coding agents.

## What this is

A single-file, throw-away script that pushes vCenter real-time metrics onto
**existing** VCF / Aria Operations resources at 20-second resolution. It is not a
management pack and not a supported product. Keep it simple; resist turning it
into a framework.

## Architecture at a glance

```
main()
  load config.yaml
  get_credential_provider().get_secret(...)   # secrets, just-in-time
  acquire_token()          -> Ops auth header (vRealizeOpsToken)
  connect_vcenter()        -> pyVmomi ServiceInstance
  for each vc_object in config (done ONCE, before the loop):
     resolve_counter()     -> vCenter counterId + unit scale
     build_resource_index()-> {identifier tuple -> Ops resource UUID}
  loop every INTERVAL (20s):
     get_objects()         -> vCenter managed objects of the type
     match identity tuple  -> Ops resource UUID (skip if unknown)
     query_latest()        -> newest 20s sample per counter
     push_stats()          -> POST /api/resources/{id}/stats
```

Counter resolution and resource indexing happen **once**, lazily, the first time
both sides are reachable. The per-tick loop otherwise only queries and pushes.
Connections are re-established and the index is rebuilt automatically after a
communication failure (see resilience below), but a *successful* run does not
re-index — if config changes which resources exist, restart the script.
## Key design decisions

- **Match, don't create.** VMWARE resources cannot be created through the Ops
  API (the `adapterkinds` create endpoint is OPENAPI-only; VMWARE returns error
  1514). So the script finds the resource the vCenter adapter already owns and
  pushes onto its UUID. Identity is the resource key: adapter kind + resource
  kind + the identifiers flagged "part of uniqueness".
- **Identity via `moid` + `vc_uuid`.** For `VMWARE:HostSystem` the unique pair is
  `VMEntityObjectID` (`host._moId`, e.g. `host-6455`) and `VMEntityVCID`
  (`content.about.instanceUuid`). These are the defaults in `DEFAULT_IDENTIFIERS`
  and work for VirtualMachine, Datastore, etc. too. `name` exists as a source but
  is **not** unique — don't rely on it.
- **Pluggable credentials.** Secrets come from a `CredentialProvider` selected by
  `CRED_PROVIDER`. Only `env` ships. Add real providers by subclassing and
  registering in `_PROVIDERS`; a production provider should authenticate to the
  secrets manager with a non-secret machine identity (mTLS cert, allow-listed
  host) to avoid a "secret zero" on disk.
- **Unit scaling lives in `resolve_counter()`.** Percentage counters are scaled
  by 0.01; everything else by 1.0. This is the only place unit handling happens.
- **Resilience by retry, not by cleverness.** The whole cycle body is wrapped in
  one try/except over `COMM_ERRORS` (requests errors, `OSError`, pyVmomi
  `vmodl.*` faults). On failure it drops the auth header, the vCenter connection,
  and the mappings, then retries next cycle — so a restart of either vCenter or
  Ops (or an expired Ops token) self-heals within 20s. `si.RetrieveContent()` is
  called every cycle partly as a liveness check for the vCenter session.

## Gotchas

- **vSphere reports percent counters in hundredths.** `cpu.usage` of 5900 means
  59%. Handled by the `scale` in `resolve_counter()`. If a new counter looks 100x
  too large or small, check its `unitInfo.key`.
- **`intervalId=20` requires real-time data.** `query_latest()` only returns data
  for hosts that are connected and producing real-time samples. Disconnected,
  powered-off, or maintenance-mode hosts return `no data` — expected, not a bug.
- **Pushed stats lag the UI.** A successful `push_stats()` (HTTP 2xx) is the real
  confirmation. Values take a couple of minutes to surface in the Ops UI because
  of the analytics pipeline. Don't "fix" perceived data loss by re-pushing.
- **Ops vs vCenter passwords differ.** In the lab, `OPS_PASS` and `VC_PASS` are
  different values. A 401 from `acquire_token()` usually means `OPS_PASS` is
  wrong, not the code.
- **Ops auth header format.** `Authorization: vRealizeOpsToken {token}` — not
  `Bearer`. Auth body needs `authSource` (default `LOCAL`).
- **The index is a snapshot.** `build_resource_index()` pages all resources when
  mappings are (re)built. It is rebuilt after a communication failure, but not
  during normal running — new/removed resources need a restart (or an induced
  reconnect) to appear.
- **Reconnects stack `atexit` handlers.** Each `connect_vcenter()` registers an
  `atexit` `Disconnect`. Under prolonged flapping this list (and server-side
  vCenter sessions) grows. Fine for a short-lived tool; revisit if it ever runs
  as a long-lived service.
- **`vcenter_key` accepts two forms.** Either `group.name` or
  `group.name.rollup`. If a counter "isn't found", it may need the rollup suffix
  to disambiguate.
- **TLS is disabled on purpose (lab).** Both the Ops session (`OPS_VERIFY_TLS`)
  and the vCenter connect (`ssl._create_unverified_context()`) skip verification
  for self-signed certs. Turn this on for anything real.
- **`config.yaml` lives next to the script.** Default path is resolved relative
  to `__file__`; override with `CONFIG_PATH`. (It used to live under `mp/app/`.)
- **`ops_label` / `ops_unit` are informational only.** They are parsed but the
  stats push API does not apply them. Don't expect them to change how Ops
  displays the metric.

## Environment / tooling notes (Windows)

- Run with the project's Python: `py push_metrics.py`.
- This is PowerShell, not cmd: set variables with `$env:VC_PASS="..."`, **not**
  `set VC_PASS=...` (the latter does nothing useful here).
- Deps: `pip install requests pyvmomi pyyaml`.

## If you extend this

- Adding a metric or object type = edit `config.yaml`, no code change.
- Adding a secret source = new `CredentialProvider` subclass + `_PROVIDERS` entry.
- Adding a new identifier source token (beyond `moid`/`vc_uuid`/`name`) = extend
  `source_value()`.
- Keep it a single file. If it needs to grow into a real management pack, that is
  the separate `mp/` project, not this script.
