"""
secrets_oci.py — OCI-specific secrets loader with Mac/VPS fallback.

On macOS: reads from macOS Keychain. The OCI private key content is stored
in Keychain and written to a tempfile at runtime — it never sits on disk
unencrypted on the Mac.

On Linux / VPS: reads from oci_claimer/.env, uses OCI_PRIVATE_KEY_PATH
to point to the key file on disk (unchanged from before).
"""

import atexit
import os
import sys
import tempfile

# ── Backend detection ──────────────────────────────────────────────────────────
_USE_KEYCHAIN = False

if sys.platform == "darwin":
    try:
        import keyring
        _USE_KEYCHAIN = True
    except ImportError:
        pass

if not _USE_KEYCHAIN:
    import pathlib
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).parent / ".env")

_SVC = "ScheduleGary"


def _get(key: str, required: bool = True) -> str:
    """Fetch a secret by key. On Mac: Keychain. Elsewhere: env var."""
    if _USE_KEYCHAIN:
        val = keyring.get_password(_SVC, key)
    else:
        val = os.environ.get(key, "")
    val = (val or "").strip()
    if required and not val:
        raise RuntimeError(
            f"Secret '{key}' is missing. "
            f"{'Run store_secrets.py to populate Keychain.' if _USE_KEYCHAIN else 'Check oci_claimer/.env.'}"
        )
    return val


def get_oci_key_path() -> str:
    """
    Return a path to the OCI private key.

    On Mac: writes PEM content from Keychain to a tempfile and returns
    its path. The tempfile is deleted automatically at process exit.

    On VPS: returns the path from OCI_PRIVATE_KEY_PATH env var directly.
    """
    if _USE_KEYCHAIN:
        pem_content = _get("OCI_PRIVATE_KEY_CONTENT")
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
        tmp.write(pem_content)
        tmp.flush()
        tmp.close()
        atexit.register(os.unlink, tmp.name)
        return tmp.name
    return _get("OCI_PRIVATE_KEY_PATH")
