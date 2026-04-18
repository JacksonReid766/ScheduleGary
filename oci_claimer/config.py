"""
config.py — Load and validate all settings from the .env file.
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from secrets_oci import _get, get_oci_key_path


def _require(key: str) -> str:
    return _get(key, required=True)


def _optional(key: str, default: str = "") -> str:
    val = _get(key, required=False)
    return val if val else default


@dataclass
class Config:
    # ── OCI identity ──────────────────────────────────────────────────────────
    tenancy_ocid: str
    user_ocid: str
    fingerprint: str
    private_key_path: str

    # ── OCI placement ─────────────────────────────────────────────────────────
    region: str
    compartment_ocid: str
    availability_domain: str
    image_ocid: str
    subnet_ocid: str

    # ── Instance shape ────────────────────────────────────────────────────────
    shape: str                        # e.g. VM.Standard.E2.1.Micro
    shape_ocpus: Optional[float]      # required for flex shapes (A1.Flex)
    shape_memory_gb: Optional[float]  # required for flex shapes (A1.Flex)

    # ── Optional metadata ─────────────────────────────────────────────────────
    ssh_public_key: Optional[str]     # injected into instance metadata
    display_name: str

    # ── Polling ───────────────────────────────────────────────────────────────
    poll_interval: int     # base interval in seconds (default: 60)
    max_backoff: int       # hard cap on backoff in seconds (default: 300)

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_token: str
    telegram_chat_id: str

    # ── Logging ───────────────────────────────────────────────────────────────
    log_file: str


def load_config() -> Config:
    """
    Read all required and optional variables, validate, and return a Config.
    Raises ValueError immediately if any required key is absent so the script
    fails fast instead of discovering missing config mid-run.
    """
    # Shape-config fields are optional — only needed for flex shapes like A1.Flex
    raw_ocpus = _optional("OCI_SHAPE_OCPUS")
    raw_mem   = _optional("OCI_SHAPE_MEMORY_GB")

    return Config(
        # Identity
        tenancy_ocid     = _require("OCI_TENANCY_OCID"),
        user_ocid        = _require("OCI_USER_OCID"),
        fingerprint      = _require("OCI_FINGERPRINT"),
        private_key_path = get_oci_key_path(),

        # Placement
        region              = _optional("OCI_REGION",              "us-sanjose-1"),
        compartment_ocid    = _require("OCI_COMPARTMENT_OCID"),
        availability_domain = _require("OCI_AVAILABILITY_DOMAIN"),
        image_ocid          = _require("OCI_IMAGE_OCID"),
        subnet_ocid         = _require("OCI_SUBNET_OCID"),

        # Shape
        shape           = _optional("OCI_SHAPE", "VM.Standard.E2.1.Micro"),
        shape_ocpus     = float(raw_ocpus) if raw_ocpus else None,
        shape_memory_gb = float(raw_mem)   if raw_mem   else None,

        # Metadata
        ssh_public_key = _optional("OCI_SSH_PUBLIC_KEY") or None,
        display_name   = _optional("OCI_DISPLAY_NAME", "oci-claimer-instance"),

        # Polling
        poll_interval = int(_optional("POLL_INTERVAL_SECONDS", "60")),
        max_backoff   = int(_optional("MAX_BACKOFF_SECONDS",    "300")),

        # Telegram
        telegram_token   = _require("TELEGRAM_TOKEN"),
        telegram_chat_id = _require("TELEGRAM_CHAT_ID"),

        # Logging
        log_file = _optional("LOG_FILE", "oci_claimer.log"),
    )
