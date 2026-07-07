# High-Frequency Metric Collector 
A small Python script that reads real-time performance counters from vCenter and
pushes them into VCF / Aria Operations at **20-second resolution** — finer than
the standard 5-minute collection. It attaches the data to the **existing** Ops
resources (e.g. the host objects the vCenter adapter already manages) instead of
creating duplicates.

This is throw-away test tooling, not a supported product.

## How it works

Every 20 seconds the script:

1. Reads the latest real-time sample of each configured counter from vCenter
   (via pyVmomi).
2. Finds the matching Ops resource by its unique identifiers.
3. Pushes the value onto that resource under the configured stat key.

```
vCenter (pyVmomi)  ->  match by identifiers  ->  VCF Operations (/api/.../stats)
```

## Features

- **Config-driven mapping.** A YAML file maps a vCenter object type to an Ops
  resource type and lists the metrics to collect. No code changes needed to add
  metrics or object types.
- **Updates existing resources.** Data lands on the resource the vCenter adapter
  already owns, matched by resource identity — not on a new duplicate object.
- **Identity by UUID, with sane defaults.** Matching uses the vCenter managed
  object id (`moid`) plus the vCenter instance UUID (`vc_uuid`), which uniquely
  identify essentially any vCenter-collected resource. The `identifiers` block is
  optional; omit it to use those defaults.
- **Multiple metrics per object type.** `metrics` is a list.
- **Automatic unit scaling.** Percentage counters (reported by vSphere in
  hundredths of a percent) are converted to real percentages; other counters are
  passed through unchanged.
- **Resilient to outages.** If vCenter or Ops becomes unreachable, the failure is
  logged and the script retries every 20 seconds, reconnecting and re-authenticating
  automatically once the service is back.
- **Pluggable credential providers.** Secrets are fetched at runtime through a
  small provider abstraction, so the source can be swapped without touching the
  rest of the script. An environment-variable provider is included for testing;
  providers for a secrets manager (e.g. CyberArk) can be added later.

## Requirements

- Python 3
- `pip install requests pyvmomi pyyaml`
- Network access to both vCenter and the Ops instance.

## Configuration

By default the script reads `config.yaml` next to the script. Override the path
with the `CONFIG_PATH` environment variable.

### Example `config.yaml`

```yaml
vc_object:
  HostSystem:                     # vCenter managed object type (pyVmomi vim.<Type>)
    target: VMWARE:HostSystem     # Ops resource type as AdapterKind:ResourceKind

    # OPTIONAL. How to match the existing Ops resource so it is updated, not
    # duplicated. Maps an Ops uniqueness identifier to a vCenter source:
    #   moid    -> managed object id (e.g. 'host-6455')
    #   vc_uuid -> vCenter instance UUID
    #   name    -> the object's name
    # Omit to use the defaults shown below.
    identifiers:
      VMEntityObjectID: moid
      VMEntityVCID: vc_uuid

    metrics:                      # one or more metrics (a list)
      - vcenter_key: cpu.usage    # vCenter counter, 'group.name' or 'group.name.rollup'
        ops_key: cpu|usage20s     # stat key written in Ops
        ops_label: CPU|Usage 20s  # optional, informational
        ops_unit: "%"             # optional, informational
```

## Credentials

Secrets are never stored in the script or config. The default `env` provider
reads them from environment variables. Non-secret settings (hosts, users) also
have environment overrides.

| Setting | Env var | Default |
| --- | --- | --- |
| Ops host | `OPS_HOST` | `192.168.1.220` |
| Ops user | `OPS_USER` | `admin` |
| Ops password | `OPS_PASS` | — (required) |
| vCenter host | `VC_HOST` | `vc-01.vcf-lab.local` |
| vCenter user | `VC_USER` | `administrator@vsphere.local` |
| vCenter password | `VC_PASS` | — (required) |
| Config path | `CONFIG_PATH` | `config.yaml` |
| Credential provider | `CRED_PROVIDER` | `env` |

To add a real secrets-manager provider, subclass `CredentialProvider`, register
it in the `_PROVIDERS` table, and select it with `CRED_PROVIDER`. Such a provider
should authenticate to the secrets manager with a non-secret machine identity
(e.g. mutual TLS or an allow-listed host) and verify TLS.

## Running

```powershell
$env:OPS_PASS="<ops-password>"
$env:VC_PASS="<vcenter-password>"
py push_metrics.py
```

The script prints each host and the value it pushes, then repeats every 20
seconds. Press `Ctrl+C` to stop. If vCenter or Ops goes down, it logs the error
and keeps retrying every cycle until the connection is restored.

## Notes

- API-pushed metrics take a couple of minutes to appear in the Ops UI (analytics
  pipeline lag). A successful push is confirmed by the script's output.
- A host may show `no data` if vCenter has no real-time samples for it — usually
  because the host is disconnected, powered off, or in maintenance mode.
- TLS verification is disabled for the lab's self-signed vCenter/Ops certificates.
  Enable it (`OPS_VERIFY_TLS=true`) and use trusted certificates in production.
