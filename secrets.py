"""
secrets.py — Single source of truth for all ScheduleGary secrets.

On macOS: loads from macOS Keychain via the `keyring` library.
On Linux / VPS / CI: falls back to .env file via python-dotenv.

Usage in any script:
    from secrets import S
    token = S.telegram_token
"""

import os
import sys

# ── Backend detection ──────────────────────────────────────────────────────────
_USE_KEYCHAIN = False

if sys.platform == "darwin":
    try:
        import keyring
        _USE_KEYCHAIN = True
    except ImportError:
        pass

if not _USE_KEYCHAIN:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

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
            f"{'Run store_secrets.py to populate Keychain.' if _USE_KEYCHAIN else 'Check your .env file.'}"
        )
    return val


class _Secrets:
    """Lazy-loading secrets container. Access like: S.telegram_token"""

    @property
    def telegram_token(self) -> str:
        return _get("TELEGRAM_TOKEN")

    @property
    def telegram_chat_id(self) -> str:
        return _get("TELEGRAM_CHAT_ID")

    @property
    def anthropic_api_key(self) -> str:
        return _get("ANTHROPIC_API_KEY")

    @property
    def google_sheet_id(self) -> str:
        return _get("GOOGLE_SHEET_ID")

    @property
    def spreadsheet_id(self) -> str:
        return _get("SPREADSHEET_ID")

    @property
    def google_creds_json(self) -> str:
        """Returns the service account JSON as a string. Use json.loads() on it."""
        return _get("GOOGLE_CREDS_JSON")

    @property
    def google_sheets_credentials(self) -> str:
        """Base64-encoded variant used by daily_nudge.py."""
        return _get("GOOGLE_SHEETS_CREDENTIALS")

    @property
    def tavily_api_key(self) -> str:
        return _get("TAVILY_API_KEY", required=False)

    @property
    def linkedin_email(self) -> str:
        return _get("LINKEDIN_EMAIL", required=False)

    @property
    def linkedin_password(self) -> str:
        return _get("LINKEDIN_PASSWORD", required=False)


S = _Secrets()
