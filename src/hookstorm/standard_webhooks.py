"""Standard Webhooks signing helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


def decode_standard_webhooks_secret(secret: str) -> bytes:
    """Decode a Standard Webhooks whsec_ secret and validate its key size."""
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise ValueError("Standard Webhooks secrets must use the whsec_ prefix.")
    encoded = secret.removeprefix("whsec_")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Standard Webhooks secret must contain valid Base64 after whsec_.") from exc
    if not 24 <= len(key) <= 64:
        raise ValueError("Standard Webhooks HMAC keys must decode to 24–64 bytes.")
    return key


def standard_webhooks_headers(
    secret: str,
    event_id: str,
    body: bytes,
    timestamp: int,
    *,
    invalid: bool = False,
) -> dict[str, str]:
    """Create Standard Webhooks metadata and a v1 HMAC signature for one attempt."""
    key = decode_standard_webhooks_secret(secret)
    signed = f"{event_id}.{timestamp}.".encode() + body
    digest = bytearray(hmac.new(key, signed, hashlib.sha256).digest())
    if invalid:
        digest[0] ^= 0x01
    signature = base64.b64encode(bytes(digest)).decode("ascii")
    return {
        "webhook-id": event_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": f"v1,{signature}",
    }
