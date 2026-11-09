"""
Credential encryption/decryption for GTFS-RT source auth.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the `cryptography` library.

If CREDENTIALS_KEY is not set in the environment, one is generated automatically
and written to the .env file. This means encryption is always active.
"""

import json
import os
from pathlib import Path

_fernet = None
_key_warning = False


def _ensure_key() -> str:
    """Return the Fernet key, auto-generating and persisting it if missing."""
    global _key_warning
    key = os.getenv("CREDENTIALS_KEY", "").strip()
    if key:
        return key

    # Auto-generate and write to .env
    from cryptography.fernet import Fernet
    new_key = Fernet.generate_key().decode()

    # Try to find and update .env file
    env_paths = [
        Path("/app/.env"),
        Path(".env"),
        Path(__file__).parent.parent / ".env",
    ]
    written = False
    for env_path in env_paths:
        if env_path.exists():
            existing = env_path.read_text()
            if "CREDENTIALS_KEY" not in existing:
                with env_path.open("a") as f:
                    f.write(f"\nCREDENTIALS_KEY={new_key}\n")
                written = True
                break

    if not written:
        # Write to the first candidate
        for env_path in env_paths:
            try:
                with env_path.open("a") as f:
                    f.write(f"\nCREDENTIALS_KEY={new_key}\n")
                written = True
                break
            except Exception:
                continue

    # Set in current process
    os.environ["CREDENTIALS_KEY"] = new_key
    _key_warning = True
    return new_key


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    key = _ensure_key()
    from cryptography.fernet import Fernet
    _fernet = Fernet(key.encode())
    return _fernet


def is_key_auto_generated() -> bool:
    """Returns True if the key was auto-generated this session (not pre-configured)."""
    return _key_warning


def encrypt_json(data: dict | None) -> str | None:
    """Encrypt a dict to a ciphertext string. Returns None if data is empty/None."""
    if not data:
        return None
    f = _get_fernet()
    plaintext = json.dumps(data, ensure_ascii=False).encode()
    return f.encrypt(plaintext).decode()


def decrypt_json(value: str | dict | None) -> dict:
    """Decrypt a ciphertext string back to a dict. Handles plaintext JSON fallback."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    f = _get_fernet()
    text = value if isinstance(value, str) else value.decode()
    try:
        plaintext = f.decrypt(text.encode())
        return json.loads(plaintext)
    except Exception:
        # Fallback: might be an unencrypted legacy value
        try:
            return json.loads(text)
        except Exception:
            return {}


def encrypt_string(value: str) -> str:
    """Encrypt a plain string. Returns ciphertext string."""
    f = _get_fernet()
    return f.encrypt(value.encode()).decode()


def decrypt_string(value: str) -> str:
    """Decrypt a ciphertext string back to plaintext. Falls through if not encrypted."""
    if not value:
        return value
    try:
        f = _get_fernet()
        return f.decrypt(value.encode()).decode()
    except Exception:
        return value  # Already plaintext or wrong key — return as-is
