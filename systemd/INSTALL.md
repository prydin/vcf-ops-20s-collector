# Running the collector as a systemd service

These steps install `push_metrics.py` as a long-running Linux service managed by
systemd, so it starts on boot and restarts automatically.

> This is test tooling, not a supported product. The unit runs the script as an
> unprivileged user with a hardened sandbox — review it before using anywhere real.

Files in this folder:

- `vcf-ops-20s-collector.service` — the systemd unit.
- `collector.env.example` — template for the environment/secrets file.

## 1. Install the code

Put the project where the unit expects it (`WorkingDirectory`):

```bash
sudo mkdir -p /opt/vcf-ops-20s-collector
sudo cp push_metrics.py config.yaml /opt/vcf-ops-20s-collector/
```

Install the Python dependencies so `/usr/bin/python3` can import them:

```bash
sudo python3 -m pip install requests pyvmomi pyyaml
```

Prefer isolation? Use a virtualenv instead and point `ExecStart` at it:

```bash
sudo python3 -m venv /opt/vcf-ops-20s-collector/.venv
sudo /opt/vcf-ops-20s-collector/.venv/bin/pip install requests pyvmomi pyyaml
# then in the unit:
#   ExecStart=/opt/vcf-ops-20s-collector/.venv/bin/python push_metrics.py
```

## 2. Create the service account

A dedicated, no-login system user keeps the collector unprivileged:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin vcfcollector
sudo chown -R vcfcollector:vcfcollector /opt/vcf-ops-20s-collector
```

## 3. Configure credentials and settings

The service reads non-secret settings and secrets (`OPS_PASS`, `VC_PASS`) from an
environment file:

```bash
sudo mkdir -p /etc/vcf-ops-20s-collector
sudo cp systemd/collector.env.example /etc/vcf-ops-20s-collector/collector.env
sudo nano /etc/vcf-ops-20s-collector/collector.env   # fill in real values

# It holds secrets — restrict access:
sudo chown root:root /etc/vcf-ops-20s-collector/collector.env
sudo chmod 600       /etc/vcf-ops-20s-collector/collector.env
```

In production, prefer a real secrets manager over storing passwords on disk:
implement a `CredentialProvider` in the script, set `CRED_PROVIDER` in the env
file, and leave the `*_PASS` values out.

## 4. Install and start the unit

```bash
sudo cp systemd/vcf-ops-20s-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vcf-ops-20s-collector.service
```

## 5. Verify

```bash
# Status and recent output:
sudo systemctl status vcf-ops-20s-collector.service

# Live logs (the script prints each host and pushed value):
sudo journalctl -u vcf-ops-20s-collector.service -f
```

A 401 in the logs usually means `OPS_PASS` is wrong. Pushed stats take a couple
of minutes to appear in the Ops UI even after a successful push.

## Managing the service

```bash
sudo systemctl restart vcf-ops-20s-collector.service   # after editing config.yaml
sudo systemctl stop    vcf-ops-20s-collector.service
sudo systemctl disable vcf-ops-20s-collector.service   # stop starting on boot
```

Restart after any `config.yaml` change: the resource index is built once at
startup, so new or removed resources are only picked up on restart.

## Updating the env file

Changes to `/etc/vcf-ops-20s-collector/collector.env` are not picked up live:

```bash
sudo systemctl restart vcf-ops-20s-collector.service
```
