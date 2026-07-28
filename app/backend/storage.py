"""POD / receipt media access control.

Media (signatures, delivery photos, expense receipts) is personal data, so the
files under data/media must NOT be world-readable. An <img> tag can't send an
Authorization header, so we hand out short-lived **signed** URLs: the API
returns `/media/<name>?exp=<ts>&sig=<hmac>` and the media route verifies the
HMAC before serving. The signing key is config.APP_SECRET.

This is the local stand-in for cloud object storage with signed URLs — at
deploy time `sign_media_ref` can be repointed at S3/GCS pre-signed URLs without
touching callers.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

from . import config


def _name_of(ref: str) -> str:
    return ref.split("/", 1)[1] if ref.startswith("media/") else ref


def _sign(name: str, exp: int, did: str) -> str:
    # Bind the signature to the driver so a URL minted for one driver cannot be
    # re-used to construct a URL for another's media, and access is attributable.
    msg = f"{name}:{exp}:{did or ''}".encode("utf-8")
    return hmac.new(config.APP_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def sign_media_ref(ref: Optional[str], driver_id: str = "", ttl: Optional[int] = None) -> Optional[str]:
    """Turn a stored ref ('media/sig_x.png') into a signed, expiring, driver-scoped URL."""
    if not ref:
        return None
    name = _name_of(ref)
    exp = int(time.time()) + (ttl if ttl is not None else config.MEDIA_URL_TTL_S)
    did = driver_id or ""
    return f"/media/{name}?exp={exp}&did={did}&sig={_sign(name, exp, did)}"


def verify_media_token(name: str, exp, did, sig: Optional[str]) -> bool:
    """True iff the signature matches the (name, exp, driver) and hasn't expired."""
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < int(time.time()):
        return False
    return hmac.compare_digest(_sign(name, exp_i, did or ""), sig or "")
