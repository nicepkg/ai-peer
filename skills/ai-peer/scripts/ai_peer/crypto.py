"""E2E encryption for ai-peer — password-based, relay sees only ciphertext."""
import base64
import hashlib
import sys

# Fernet is optional — graceful fallback to plaintext
try:
    from cryptography.fernet import Fernet, InvalidToken
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    InvalidToken = Exception


def derive_key(password: str, room_id: str) -> bytes:
    """Derive a Fernet key from password + room_id using PBKDF2 (stdlib)."""
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), room_id.encode(), 100_000)
    return base64.urlsafe_b64encode(dk)  # Fernet needs 32 url-safe base64 bytes


def encrypt(plaintext: str, password: str, room_id: str) -> str:
    """Encrypt a message. Returns base64 ciphertext."""
    if not HAS_CRYPTO:
        raise RuntimeError(
            "Cannot encrypt: 'cryptography' package not installed. "
            "Install with: pip install cryptography"
        )
    key = derive_key(password, room_id)
    f = Fernet(key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, password: str, room_id: str, strict: bool = False) -> str:
    """Decrypt a message. Returns plaintext. Fail-closed if cryptography missing.

    strict=True rejects unencrypted messages in password-protected rooms.
    """
    if not isinstance(ciphertext, str):
        return "[invalid — non-string content]"
    if not ciphertext.startswith("gAAA"):
        if strict:
            return "[rejected — unencrypted message in encrypted room]"
        return ciphertext  # Not encrypted, return as-is
    if not HAS_CRYPTO:
        raise RuntimeError(
            "Cannot decrypt: 'cryptography' package not installed. "
            "Install with: pip install cryptography"
        )
    key = derive_key(password, room_id)
    f = Fernet(key)
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except Exception:
        return "[encrypted — wrong password or corrupted]"


def is_encrypted(text: str) -> bool:
    """Check if text looks like a Fernet token."""
    return text.startswith("gAAA") and len(text) > 50
