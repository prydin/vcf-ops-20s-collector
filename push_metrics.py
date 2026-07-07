#!/usr/bin/env python3
"""Throw-away script: collect metrics from vCenter (pyVmomi) and push them to
existing VCF / Aria Operations resources at 20-second resolution.

What makes this "usable" is config.yaml: it maps a vCenter managed-object type
to a target ops resource type, describes how to UNIQUELY identify the matching
ops resource (so we update it instead of creating duplicates), and lists the
metrics to push. See config.yaml for the documented schema.

Uniqueness
----------
An ops resource's identity is its resource key: adapter kind + resource kind +
the resource identifiers flagged "part of uniqueness". For VMWARE:HostSystem
those are:
    VMEntityObjectID  = the vCenter managed object id (host._moId, e.g. host-6455)
    VMEntityVCID      = the vCenter instance UUID       (content.about.instanceUuid)
We look up the existing resource by that pair and push stats onto its id, so the
high-frequency metric lands on the same host object the vCenter adapter manages.

Usage (PowerShell):
    # Secrets come from a pluggable credential provider (see CredentialProvider).
    # The default 'env' provider reads them from environment variables:
    $env:OPS_HOST="192.168.1.220"; $env:OPS_USER="admin"; $env:OPS_PASS="..."
    $env:VC_HOST="vc-01.vcf-lab.local"; $env:VC_USER="administrator@vsphere.local"
    $env:VC_PASS="..."
    # Select a different provider in production (once implemented):
    #   $env:CRED_PROVIDER="cyberark"
    py push_metrics.py

Requires: requests, pyvmomi, pyyaml  (pip install requests pyvmomi pyyaml)
"""

import os
import sys
import ssl
import time
import atexit
import urllib3
import requests
import yaml
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, vmodl

# --- VCF Operations config (override via env vars) -------------------------
OPS_HOST = os.environ.get("OPS_HOST", "192.168.1.220")
OPS_USER = os.environ.get("OPS_USER", "admin")
OPS_AUTH_SOURCE = os.environ.get("OPS_AUTH_SOURCE", "LOCAL")
OPS_BASE = f"https://{OPS_HOST}/suite-api"
# vROps / VCF Operations suite-api commonly uses self-signed certs.
OPS_VERIFY_TLS = os.environ.get("OPS_VERIFY_TLS", "false").lower() == "true"

# --- vCenter config (override via env vars) --------------------------------
VC_HOST = os.environ.get("VC_HOST", "vc-01.vcf-lab.local")
VC_USER = os.environ.get("VC_USER", "administrator@vsphere.local")
VC_PORT = int(os.environ.get("VC_PORT", "443"))

# --- runtime ----------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.yaml")
)
INTERVAL = 20  # seconds; matches ESXi real-time sampling

# Default uniqueness identifiers, applied when a vc_object omits 'identifiers'.
# vCenter-collected resources are uniquely identified by their managed object id
# plus the vCenter instance UUID, so this works for HostSystem, VirtualMachine,
# Datastore, etc.
DEFAULT_IDENTIFIERS = {
    "VMEntityObjectID": "moid",
    "VMEntityVCID": "vc_uuid",
}


# ===========================================================================
# Credential providers
# ---------------------------------------------------------------------------
# Secrets are fetched at runtime through a small provider abstraction so the
# source can be swapped (env for testing -> CyberArk CCP / Conjur / etc. in
# production) without touching the rest of the script. Production providers
# should authenticate to the secrets manager with a non-secret machine identity
# (mTLS client cert, allow-listed host/OS user, workload JWT) to avoid storing a
# "secret zero" on disk.
# ===========================================================================
class CredentialProvider:
    """Base interface: return the secret value for a logical name."""

    def get_secret(self, name: str) -> str:
        raise NotImplementedError


class EnvCredentialProvider(CredentialProvider):
    """Reads secrets from environment variables. For local testing only.

    Logical secret names are mapped to env var names; unknown names fall back to
    the upper-cased logical name.
    """

    ENV_VARS = {
        "ops_password": "OPS_PASS",
        "vcenter_password": "VC_PASS",
    }

    def get_secret(self, name: str) -> str:
        env_name = self.ENV_VARS.get(name, name.upper())
        value = os.environ.get(env_name)
        if not value:
            raise RuntimeError(
                f"Secret '{name}' not available (expected env var '{env_name}')"
            )
        return value


# Registry of available providers, keyed by the CRED_PROVIDER env var value.
_PROVIDERS = {
    "env": EnvCredentialProvider,
}


def get_credential_provider() -> CredentialProvider:
    """Instantiate the provider selected by the CRED_PROVIDER env var."""
    kind = os.environ.get("CRED_PROVIDER", "env").lower()
    try:
        return _PROVIDERS[kind]()
    except KeyError:
        raise RuntimeError(
            f"Unknown credential provider '{kind}'. "
            f"Available: {', '.join(sorted(_PROVIDERS))}"
        )


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()
session.verify = OPS_VERIFY_TLS
session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
})


# ===========================================================================
# VCF Operations side
# ===========================================================================
def acquire_token(password):
    """POST /api/auth/token/acquire -> set Authorization header."""
    resp = session.post(
        f"{OPS_BASE}/api/auth/token/acquire",
        json={"username": OPS_USER, "password": password, "authSource": OPS_AUTH_SOURCE},
    )
    resp.raise_for_status()
    session.headers["Authorization"] = f"vRealizeOpsToken {resp.json()['token']}"
    print("Authenticated to VCF Operations.")


def build_resource_index(adapter_kind, resource_kind, ident_names):
    """Index existing ops resources by their uniqueness identifier values.

    Returns a dict mapping a tuple of identifier values (ordered by ident_names)
    to the ops resource UUID. This lets us resolve a vCenter object to the
    existing ops resource it corresponds to, so we update instead of duplicate.
    """
    index = {}
    page = 0
    while True:
        resp = session.get(
            f"{OPS_BASE}/api/resources",
            params={
                "adapterKind": adapter_kind,
                "resourceKind": resource_kind,
                "page": page,
                "pageSize": 1000,
            },
        )
        resp.raise_for_status()
        resources = resp.json().get("resourceList", [])
        if not resources:
            break
        for res in resources:
            values = {
                i["identifierType"]["name"]: i.get("value")
                for i in res["resourceKey"].get("resourceIdentifiers", [])
            }
            key = tuple(values.get(name) for name in ident_names)
            index[key] = res["identifier"]
        page += 1
    return index


def push_stats(resource_id, samples_by_key):
    """POST /api/resources/{id}/stats with one stat-content entry per metric.

    samples_by_key: {ops_key: (timestamp_ms, value)}
    """
    stat_content = [
        {"statKey": ops_key, "timestamps": [ts], "data": [float(value)]}
        for ops_key, (ts, value) in samples_by_key.items()
    ]
    if not stat_content:
        return
    resp = session.post(
        f"{OPS_BASE}/api/resources/{resource_id}/stats",
        json={"stat-content": stat_content},
    )
    resp.raise_for_status()


# ===========================================================================
# vCenter side
# ===========================================================================
def connect_vcenter(password):
    """Connect to vCenter (ignoring self-signed cert) and return ServiceInstance."""
    ctx = ssl._create_unverified_context()
    si = SmartConnect(host=VC_HOST, user=VC_USER, pwd=password, port=VC_PORT, sslContext=ctx)
    atexit.register(Disconnect, si)
    print(f"Connected to vCenter {VC_HOST}.")
    return si


def get_objects(content, vc_type_name):
    """Return all managed objects of the named vim type (e.g. 'HostSystem')."""
    vc_type = getattr(vim, vc_type_name)
    view = content.viewManager.CreateContainerView(content.rootFolder, [vc_type], True)
    objects = list(view.view)
    view.Destroy()
    return objects


def resolve_counter(content, vcenter_key):
    """Resolve a 'group.name' or 'group.name.rollup' key to (counter_id, scale).

    scale is 0.01 for percentage counters (vSphere reports them in hundredths of
    a percent) and 1.0 otherwise.
    """
    for c in content.perfManager.perfCounter:
        group_name = f"{c.groupInfo.key}.{c.nameInfo.key}"
        full = f"{group_name}.{c.rollupType}"
        if vcenter_key in (full, group_name):
            scale = 0.01 if c.unitInfo.key == "percent" else 1.0
            return c.key, scale
    raise RuntimeError(f"vCenter counter '{vcenter_key}' not found")


def query_latest(content, entity, counter_ids):
    """Query the latest real-time (20s) sample for each counter on an entity.

    Returns {counter_id: (timestamp_ms, raw_value)} for counters that had data.
    """
    metric_ids = [
        vim.PerformanceManager.MetricId(counterId=cid, instance="")
        for cid in counter_ids
    ]
    spec = vim.PerformanceManager.QuerySpec(
        entity=entity, metricId=metric_ids, intervalId=INTERVAL, maxSample=1
    )
    result = content.perfManager.QueryPerf(querySpec=[spec])
    if not result or not result[0].sampleInfo:
        return {}
    ts_ms = int(result[0].sampleInfo[-1].timestamp.timestamp() * 1000)
    out = {}
    for series in result[0].value:
        if series.value:
            out[series.id.counterId] = (ts_ms, series.value[-1])
    return out


def source_value(source, obj, vc_uuid):
    """Resolve a config identifier source token to a value for a vCenter object."""
    if source == "moid":
        return obj._moId
    if source == "vc_uuid":
        return vc_uuid
    if source == "name":
        return obj.name
    raise RuntimeError(f"Unknown identifier source '{source}' in config")


# ===========================================================================
# Communication failures we treat as transient: log and retry on the next cycle
# instead of crashing. Covers ops HTTP/network errors (requests), socket-level
# errors (OSError), and vCenter/pyVmomi faults (vmodl), e.g. either side being
# restarted or briefly unreachable.
COMM_ERRORS = (
    requests.exceptions.RequestException,
    OSError,
    vmodl.MethodFault,
    vmodl.RuntimeFault,
)


def build_mappings(content, config):
    """Resolve counters and index existing ops resources for every mapped type.

    Requires both vCenter and ops to be reachable. Returns the list of mappings
    the collection loop iterates over.
    """
    mappings = []
    for vc_type, spec in config["vc_object"].items():
        adapter_kind, resource_kind = spec["target"].split(":", 1)
        identifiers = spec.get("identifiers", DEFAULT_IDENTIFIERS)
        ident_names = list(identifiers.keys())
        ident_sources = [identifiers[n] for n in ident_names]
        metrics = []
        for m in spec["metrics"]:
            counter_id, scale = resolve_counter(content, m["vcenter_key"])
            metrics.append((counter_id, scale, m["ops_key"]))
        mappings.append({
            "vc_type": vc_type,
            "adapter_kind": adapter_kind,
            "resource_kind": resource_kind,
            "ident_names": ident_names,
            "ident_sources": ident_sources,
            "metrics": metrics,
            "index": build_resource_index(adapter_kind, resource_kind, ident_names),
        })
        print(
            f"{vc_type} -> {spec['target']}: {len(metrics)} metric(s), "
            f"{len(mappings[-1]['index'])} existing resource(s) indexed."
        )
    return mappings


def collect_and_push(content, mapping, vc_uuid):
    """Query the latest sample for each object of one mapping and push it."""
    counter_ids = [c[0] for c in mapping["metrics"]]
    for obj in get_objects(content, mapping["vc_type"]):
        key = tuple(
            source_value(src, obj, vc_uuid) for src in mapping["ident_sources"]
        )
        resource_id = mapping["index"].get(key)
        if resource_id is None:
            print(f"  {obj.name}: no matching ops resource for {key}; skipping")
            continue

        raw = query_latest(content, obj, counter_ids)
        samples = {}
        for counter_id, scale, ops_key in mapping["metrics"]:
            if counter_id in raw:
                ts_ms, value = raw[counter_id]
                samples[ops_key] = (ts_ms, value * scale)
        if not samples:
            print(f"  {obj.name}: no data")
            continue

        push_stats(resource_id, samples)
        pushed = ", ".join(f"{k}={v:.2f}" for k, (_, v) in samples.items())
        print(f"  {obj.name}: {pushed}")


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Fetch secrets just-in-time from the configured provider; never store them
    # on disk or in persisted env. Swap the provider (env -> CyberArk/Conjur/...)
    # via the CRED_PROVIDER env var without changing the rest of the script.
    try:
        provider = get_credential_provider()
        ops_password = provider.get_secret("ops_password")
        vc_password = provider.get_secret("vcenter_password")
    except Exception as e:
        print(f"Failed to obtain credentials: {e}", file=sys.stderr)
        sys.exit(1)

    # Connections and the resource index are (re)established lazily inside the
    # loop. A communication failure with either vCenter or ops is logged and
    # retried on the next cycle, so the script rides out restarts of either side.
    si = None
    mappings = None
    print(f"Collecting every {INTERVAL}s. Ctrl+C to stop.")
    while True:
        start = time.time()
        try:
            if "Authorization" not in session.headers:
                acquire_token(ops_password)
            if si is None:
                si = connect_vcenter(vc_password)
            # RetrieveContent is a live call; it also detects a dropped session.
            content = si.RetrieveContent()
            vc_uuid = content.about.instanceUuid
            if mappings is None:
                mappings = build_mappings(content, config)
            for mapping in mappings:
                collect_and_push(content, mapping, vc_uuid)
        except COMM_ERRORS as e:
            print(f"Communication failure: {e}. Retrying in {INTERVAL}s.", file=sys.stderr)
            # Drop cached auth/connection/index so they are rebuilt next cycle.
            session.headers.pop("Authorization", None)
            si = None
            mappings = None
        time.sleep(max(0, INTERVAL - (time.time() - start)))


if __name__ == "__main__":
    main()
