"""POD / receipt media access control.

Media (signatures, delivery photos, expense receipts) is personal data, so the
files under data/media must NOT be world-readable. An <img> tag can't send an
Authorization header, so we hand out short-lived **signed** URLs: the API
returns `/media/<name>?exp=<ts>&sig=<hmac>` and the media route verifies the
HMAC before serving. The signing key is config.APP_SECRET.

This file is a local stand-in for cloud object storage with signed URLs — at
deploy time `sign_media_ref` can be repointed at S3/GCS pre-signed URLs without
touching callers.

Changes made:
- sanitize and normalize media names to prevent path traversal
- URL-escape the media name and encode query params safely
- allow config.APP_SECRET to be bytes or str and validate presence
- include driver id (did) in signature only when provided
- add more precise typing and error handling
"""
from __future__ import annotations

import hashlib
import hmac
import time
import posixpath
import urllib.parse
from typing import Optional, Union

from . import config


def _name_of(ref: str) -> str:
    """Extract and normalise the stored media name.

    Accepts both values stored as "media/<name>" and bare names. Normalises
    the path using POSIX rules and rejects absolute paths or parent-directory
    references to avoid directory traversal.
    """
    if ref.startswith("media/"):
        name = ref[len("media/"):]
    else:
        name = ref
    # Normalize as a POSIX path (storage keys are URL-like)
    name = posixpath.normpath(name)
    # Reject absolute paths and any path-traversal attempts
    if name == "" or name.startswith("/") or name.startswith("..") or "/.." in name:
        raise ValueError("invalid media ref")
    return name


def _sign(name: str, exp: int, did: str) -> str:
    """Produce an HMAC-SHA256 hex digest over name, expiration and driver id.

    The config.APP_SECRET may be either bytes or str. Raise a ValueError if it
    is not set to avoid producing predictable signatures.
    """
    secret = config.APP_SECRET
    if not secret:
        raise ValueError("APP_SECRET must be configured for media signing")
    key = secret if isinstance(secret, (bytes, bytearray)) else str(secret).encode("utf-8")
    msg = f"{name}:{exp}:{did or ''}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def sign_media_ref(ref: Optional[str], driver_id: str = "", ttl: Optional[int] = None) -> Optional[str]:
    """Turn a stored ref ('media/sig_x.png' or 'sig_x.png') into a signed URL.

    The returned URL is safe for embedding in HTML: the media name is URL-
    escaped and query parameters are encoded via urllib.parse.urlencode.
    """
    if not ref:
        return None
    name = _name_of(ref)
    exp = int(time.time()) + (int(ttl) if ttl is not None else int(config.MEDIA_URL_TTL_S))
    did = driver_id or ""
    sig = _sign(name, exp, did)
    params = {"exp": exp, "sig": sig}
    if did:
        params["did"] = did
    # URL-encode the path component and the query string
    path = "/media/" + urllib.parse.quote(name, safe="/")
    return path + "?" + urllib.parse.urlencode(params)


def verify_media_token(name: str, exp: Union[str, int], did: Optional[str], sig: Optional[str]) -> bool:
    """True iff the signature matches the (name, exp, driver) and hasn't expired.

    - `exp` may be a string (from the query) or an int. Non-integer values are
      rejected. The token is rejected if it is expired or the signature is
      missing/doesn't match (use constant-time comparison).
    """
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < int(time.time()):
        return False
    # Ensure sig is provided
    if not sig:
        return False
    # `did` is optional; treat None as empty string for signing
    expected = _sign(name, exp_i, did or "")
    return hmac.compare_digest(expected, sig)
