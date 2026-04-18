# OCI Claimer

Polls the Oracle Cloud Infrastructure API and claims a free-tier VM the moment
capacity becomes available.  On success it sends a Telegram message with the
instance OCID and public IP.

---

## How it works

1. Every `POLL_INTERVAL_SECONDS` (default 60 s) it calls
   `ComputeClient.launch_instance()` with your configured shape.
2. If OCI returns *"Out of host capacity"* (HTTP 500 / `InternalError`) it logs
   the failure and sleeps with **exponential backoff + full jitter**, capped at
   `MAX_BACKOFF_SECONDS`.
3. On **success** it fetches the public IP via VNIC and sends a Telegram alert,
   then exits `0`.
4. On any **hard error** (bad credentials, wrong OCID, unexpected exception) it
   alerts via Telegram and exits `1` — it never silently retries misconfigured
   calls.

---

## Prerequisites

- Python 3.11+
- An OCI account with Always Free tier available
- A Telegram bot token (use the one Gary already has, or create a new one with
  [@BotFather](https://t.me/BotFather))

---

## Step 1 — Generate an OCI API key pair

OCI uses RSA key pairs for API authentication (separate from your SSH keys).

```bash
# Create the OCI config directory
mkdir -p ~/.oci

# Generate a 2048-bit private key
openssl genrsa -out ~/.oci/oci_api_key.pem 2048

# Derive the matching public key
openssl rsa -pubout -in ~/.oci/oci_api_key.pem -out ~/.oci/oci_api_key_public.pem

# Lock down permissions so OCI SDK doesn't complain
chmod 600 ~/.oci/oci_api_key.pem
```

---

## Step 2 — Upload the public key to OCI Console

1. Sign in at [cloud.oracle.com](https://cloud.oracle.com).
2. Click the **profile icon** (top-right) → **User Settings**.
3. Scroll to **API Keys** → **Add API Key**.
4. Choose **Paste Public Key**, paste the contents of `~/.oci/oci_api_key_public.pem`.
5. Click **Add**.
6. OCI shows a **fingerprint** (`aa:bb:cc:...`) — copy it for `.env`.

---

## Step 3 — Collect the required OCIDs

| Variable | Where to find it |
|---|---|
| `OCI_TENANCY_OCID` | Profile icon → **Tenancy: \<name\>** → OCID field |
| `OCI_USER_OCID` | Profile icon → **User Settings** → OCID field |
| `OCI_FINGERPRINT` | User Settings → API Keys → the fingerprint from Step 2 |
| `OCI_COMPARTMENT_OCID` | **Identity & Security → Compartments** — the root compartment OCID equals the tenancy OCID |
| `OCI_AVAILABILITY_DOMAIN` | **Compute → Instances → Create Instance → Placement** — note the AD name, e.g. `IfZg:US-SANJOSE-1-AD-1` |
| `OCI_IMAGE_OCID` | **Compute → Images** — filter by your region and pick an Oracle Linux 8 or Ubuntu 22.04 image; copy its OCID |
| `OCI_SUBNET_OCID` | **Networking → Virtual Cloud Networks → \<your VCN\> → Subnets** — pick a public subnet |

> **Tip:** If you don't have a VCN yet, go to **Networking → Virtual Cloud
> Networks → Start VCN Wizard** and choose "Create VCN with Internet
> Connectivity".  This creates a public and a private subnet automatically.

---

## Step 4 — Configure .env

```bash
cd oci_claimer
cp .env.example .env
```

Open `.env` and fill in every value.  The fields `OCI_SHAPE_OCPUS` and
`OCI_SHAPE_MEMORY_GB` are only needed for the Ampere A1.Flex shape — leave them
commented out for `VM.Standard.E2.1.Micro`.

```
# Example for the AMD micro (x86, Always Free)
OCI_SHAPE=VM.Standard.E2.1.Micro

# Example for the Ampere ARM flex (also Always Free — higher demand)
# OCI_SHAPE=VM.Standard.A1.Flex
# OCI_SHAPE_OCPUS=4
# OCI_SHAPE_MEMORY_GB=24
```

---

## Step 5 — Install dependencies and run

```bash
# From the repo root or inside oci_claimer/
pip install -r oci_claimer/requirements.txt

# Run from the oci_claimer directory so the .env is found automatically
cd oci_claimer
python main.py
```

Logs go to both stdout and `oci_claimer.log` (configurable via `LOG_FILE`).

---

## Running persistently (VPS)

If you want this to keep polling even when your laptop is closed, copy it to the
same VPS that runs `bot_persistent.py` and create a systemd unit:

```ini
# /etc/systemd/system/oci-claimer.service
[Unit]
Description=OCI Free-Tier Instance Claimer
After=network-online.target

[Service]
WorkingDirectory=/home/ubuntu/ScheduleGary/oci_claimer
ExecStart=/home/ubuntu/ScheduleGary/venv/bin/python main.py
Restart=on-failure
RestartSec=30
EnvironmentFile=/home/ubuntu/ScheduleGary/oci_claimer/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable oci-claimer
sudo systemctl start oci-claimer
journalctl -u oci-claimer -f
```

The service has `Restart=on-failure` but the script itself exits `1` on hard
errors and `0` on success — so systemd won't restart it after a successful
claim or a config error, only on transient crashes.

---

## Backoff behaviour

| Consecutive soft failures | Wait before next attempt (approximate) |
|---|---|
| 1 | 0 – 60 s |
| 2 | 0 – 120 s |
| 3 | 0 – 240 s |
| 4+ | 0 – 300 s (capped) |

Full jitter means the actual wait is a uniform random draw in `[0, ceiling]`.
This spreads requests from multiple competing clients and avoids thundering-herd
against the OCI endpoint.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `InvalidConfig` on startup | Private key path wrong or permissions too loose (`chmod 600`) |
| HTTP 401 / `NotAuthenticated` | Fingerprint or user OCID mismatch — re-check Step 2 |
| HTTP 404 on `image_id` | Image OCID is region-specific; make sure it matches `OCI_REGION` |
| Script claims capacity but instance never appears | Check the OCI Console for a failed/terminated instance; the subnet may lack a route to the internet gateway |
| Telegram notification not arriving | Verify `TELEGRAM_CHAT_ID` — send `/start` to the bot first to open the chat |
