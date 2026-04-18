"""
main.py — OCI free-tier instance claimer.

Polls OCI on a configurable interval and launches a VM the moment capacity
becomes available.  On success, sends a Telegram notification with the
instance OCID and public IP.  On hard errors (auth, bad config, etc.) it
alerts and exits immediately rather than retrying blindly.

Usage:
    python main.py

All config comes from .env — see .env.example.
"""

import asyncio
import logging
import math
import random
import sys
import time
from datetime import datetime, timezone

import oci
from telegram import Bot

from config import Config, load_config

# ── Constants ─────────────────────────────────────────────────────────────────

# OCI error codes / messages that mean "no capacity — try again later"
CAPACITY_CODES = {"InternalError", "LimitExceeded", "QuotaExceeded"}
CAPACITY_MSG_FRAGMENT = "out of host capacity"

# HTTP status codes that are always soft (transient) failures
TRANSIENT_HTTP = {429, 500, 503}

# Maximum tool-loop iterations when fetching the instance public IP
VNIC_POLL_ATTEMPTS = 12
VNIC_POLL_SLEEP    = 5   # seconds between VNIC attachment checks


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("oci_claimer")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


# ── Telegram ──────────────────────────────────────────────────────────────────

async def _send_async(token: str, chat_id: str, text: str) -> None:
    bot = Bot(token=token)
    async with bot:
        await bot.send_message(chat_id=chat_id, text=text)


def send_telegram(token: str, chat_id: str, text: str, logger: logging.Logger) -> None:
    """Best-effort Telegram notification — never raises."""
    try:
        asyncio.run(_send_async(token, chat_id, text))
    except Exception as exc:
        logger.warning(f"Telegram notification failed: {exc}")


# ── OCI helpers ───────────────────────────────────────────────────────────────

def build_oci_config(cfg: Config) -> dict:
    return {
        "user":        cfg.user_ocid,
        "key_file":    cfg.private_key_path,
        "fingerprint": cfg.fingerprint,
        "tenancy":     cfg.tenancy_ocid,
        "region":      cfg.region,
    }


def is_capacity_error(exc: oci.exceptions.ServiceError) -> bool:
    """
    Return True if the ServiceError means "no capacity right now, retry later".

    OCI returns HTTP 500 / code "InternalError" with a message containing
    "Out of host capacity" for standard capacity failures.  Rate-limit
    (HTTP 429) and quota errors are also treated as soft/retriable.
    """
    if exc.status in TRANSIENT_HTTP:
        return True
    if exc.code in CAPACITY_CODES:
        return True
    if CAPACITY_MSG_FRAGMENT in str(exc.message).lower():
        return True
    return False


def build_launch_details(cfg: Config) -> oci.core.models.LaunchInstanceDetails:
    """Construct the LaunchInstanceDetails payload from config."""
    metadata = {}
    if cfg.ssh_public_key:
        metadata["ssh_authorized_keys"] = cfg.ssh_public_key

    shape_config = None
    if cfg.shape_ocpus is not None and cfg.shape_memory_gb is not None:
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=cfg.shape_ocpus,
            memory_in_gbs=cfg.shape_memory_gb,
        )

    return oci.core.models.LaunchInstanceDetails(
        availability_domain = cfg.availability_domain,
        compartment_id      = cfg.compartment_ocid,
        shape               = cfg.shape,
        image_id            = cfg.image_ocid,
        display_name        = cfg.display_name,
        metadata            = metadata or None,
        shape_config        = shape_config,
        create_vnic_details = oci.core.models.CreateVnicDetails(
            subnet_id        = cfg.subnet_ocid,
            assign_public_ip = True,
        ),
    )


def attempt_launch(
    compute: oci.core.ComputeClient,
    details: oci.core.models.LaunchInstanceDetails,
    logger: logging.Logger,
) -> oci.core.models.Instance | None:
    """
    Try to launch the instance once.

    Returns:
        Instance object on success.
        None if capacity is unavailable (soft failure — caller should retry).

    Raises:
        oci.exceptions.ServiceError for hard OCI errors.
        Any other exception propagates as-is.
    """
    try:
        response = compute.launch_instance(details)
        return response.data
    except oci.exceptions.ServiceError as exc:
        if is_capacity_error(exc):
            logger.info(
                f"Capacity unavailable — HTTP {exc.status} / {exc.code}: "
                f"{str(exc.message)[:120]}"
            )
            return None
        # Any other ServiceError (auth, bad OCID, wrong region…) is a hard failure
        raise


def fetch_public_ip(
    compute: oci.core.ComputeClient,
    vnet:    oci.core.VirtualNetworkClient,
    instance: oci.core.models.Instance,
    cfg: Config,
    logger: logging.Logger,
) -> str:
    """
    Wait for the VNIC to attach and return the instance's public IP.
    Falls back to "check OCI Console" if the VNIC isn't ready in time.
    """
    logger.info("Waiting for VNIC attachment to retrieve public IP…")
    for attempt in range(VNIC_POLL_ATTEMPTS):
        try:
            attachments = compute.list_vnic_attachments(
                compartment_id=cfg.compartment_ocid,
                instance_id=instance.id,
            ).data

            ready = [
                a for a in attachments
                if a.lifecycle_state == "ATTACHED"
            ]
            if ready:
                vnic = vnet.get_vnic(ready[0].vnic_id).data
                ip = vnic.public_ip
                if ip:
                    logger.info(f"Public IP: {ip}")
                    return ip
        except Exception as exc:
            logger.warning(f"VNIC poll attempt {attempt + 1} failed: {exc}")

        time.sleep(VNIC_POLL_SLEEP)

    logger.warning("VNIC not ready after polling — IP will appear in OCI Console.")
    return "check OCI Console"


# ── Backoff ───────────────────────────────────────────────────────────────────

def backoff_seconds(consecutive: int, base: int, cap: int) -> float:
    """
    Exponential backoff with full jitter.

    delay = random(0, min(cap, base * 2^(consecutive-1)))

    On the first failure (consecutive=1) this returns a value in [0, base],
    effectively just adding jitter to the normal poll interval.
    """
    ceiling = min(cap, base * (2 ** (consecutive - 1)))
    return random.uniform(0, ceiling)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    # Load and validate config first — fail fast if anything is missing
    try:
        cfg = load_config()
    except ValueError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    logger = setup_logging(cfg.log_file)
    logger.info("=" * 60)
    logger.info("OCI Claimer starting up")
    logger.info(f"  Shape:               {cfg.shape}")
    logger.info(f"  Region:              {cfg.region}")
    logger.info(f"  Availability domain: {cfg.availability_domain}")
    logger.info(f"  Base poll interval:  {cfg.poll_interval}s")
    logger.info(f"  Max backoff:         {cfg.max_backoff}s")
    logger.info(f"  Log file:            {cfg.log_file}")
    logger.info("=" * 60)

    oci_config = build_oci_config(cfg)

    # Validate the OCI config structure before making any API calls
    try:
        oci.config.validate_config(oci_config)
    except oci.exceptions.InvalidConfig as exc:
        msg = f"Invalid OCI config: {exc}"
        logger.error(msg)
        send_telegram(cfg.telegram_token, cfg.telegram_chat_id,
                      f"OCI Claimer failed to start: {msg}", logger)
        sys.exit(1)

    compute = oci.core.ComputeClient(oci_config)
    vnet    = oci.core.VirtualNetworkClient(oci_config)
    details = build_launch_details(cfg)

    consecutive_failures = 0
    attempt_number       = 0

    while True:
        attempt_number += 1
        logger.info(f"--- Attempt #{attempt_number} ---")

        try:
            instance = attempt_launch(compute, details, logger)

        except oci.exceptions.ServiceError as exc:
            # Hard OCI error — alert and exit
            msg = (
                f"OCI Claimer stopped: hard API error on attempt #{attempt_number}\n"
                f"HTTP {exc.status} / {exc.code}: {exc.message}"
            )
            logger.error(msg)
            send_telegram(cfg.telegram_token, cfg.telegram_chat_id, msg, logger)
            sys.exit(1)

        except KeyboardInterrupt:
            logger.info("Interrupted by user — exiting.")
            sys.exit(0)

        except Exception as exc:
            # Unexpected error — alert and exit
            msg = (
                f"OCI Claimer stopped: unexpected error on attempt #{attempt_number}\n"
                f"{type(exc).__name__}: {exc}"
            )
            logger.error(msg, exc_info=True)
            send_telegram(cfg.telegram_token, cfg.telegram_chat_id, msg, logger)
            sys.exit(1)

        if instance is not None:
            # ── Success ───────────────────────────────────────────────────────
            public_ip = fetch_public_ip(compute, vnet, instance, cfg, logger)

            success_msg = (
                f"Instance claimed after {attempt_number} attempt(s)!\n\n"
                f"OCID:   {instance.id}\n"
                f"Shape:  {instance.shape}\n"
                f"State:  {instance.lifecycle_state}\n"
                f"Region: {cfg.region}\n"
                f"AD:     {cfg.availability_domain}\n"
                f"IP:     {public_ip}"
            )
            logger.info(success_msg)
            send_telegram(cfg.telegram_token, cfg.telegram_chat_id,
                          success_msg, logger)
            sys.exit(0)

        # ── Capacity unavailable — back off and retry ─────────────────────────
        consecutive_failures += 1
        wait = backoff_seconds(consecutive_failures, cfg.poll_interval, cfg.max_backoff)
        logger.info(
            f"No capacity (failure #{consecutive_failures}) — "
            f"retrying in {wait:.1f}s"
        )
        time.sleep(wait)


if __name__ == "__main__":
    main()
