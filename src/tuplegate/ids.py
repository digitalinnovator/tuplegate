"""Encode canonical ids into identifiers both OpenFGA and SpiceDB accept.

Canonical ids are human-readable (``alice@example.com``, ``github/acme/api/src/app.py``).
SpiceDB object ids allow only ``[a-zA-Z0-9/_|\\-=+]``, and OpenFGA caps ``type:id`` at 256
bytes. The codec keeps the safe characters, writes every other UTF-8 byte as ``=XX`` (upper-case
hex), and replaces ids whose encoding would be too long with ``=h`` plus a SHA-256 digest.

Properties (tested with Hypothesis):

- The output only uses the SpiceDB-safe alphabet.
- ``decode(encode(x)) == x`` whenever the id was not hashed. A hashed id decodes to the raw
  form ``RAW_PREFIX + digest``, which ``encode`` maps back to the same digest. Tuples read back
  from an engine therefore round-trip, and ``normalise`` puts canonical ids in the same form so
  the sync engine can diff them.
- The encoding is injective: an escape is always ``=`` plus two upper-case hex digits, so the
  hashed form ``=h...`` (lower-case ``h``) can never collide with an escaped id.

The same codec is used for both engines so a tuple dump from either reads the same way.
"""

from __future__ import annotations

import hashlib
import re

_SAFE = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_|-+")
# OpenFGA limits the whole "type:id" string to 256 bytes; the longest type name is
# "container" (9) plus ":" (1). 200 leaves room for longer type names later.
MAX_ENCODED_LEN = 200
_HASH_PREFIX = "=h"
# Marks an id that is only known by its digest (read back from an engine). NUL never appears in
# ids from the sources we sync, and the codec escapes it, so no canonical id can mimic this.
RAW_PREFIX = "\x00raw:"
_ALLOWED_RE = re.compile(r"^[a-zA-Z0-9/_|\-=+]+$")
_ESCAPE_RE = re.compile(r"=([0-9A-F]{2})")


def encode(canonical: str) -> str:
    if not canonical:
        raise ValueError("cannot encode an empty id")
    if canonical == "*":
        return "*"
    if canonical.startswith(RAW_PREFIX):
        digest = canonical[len(RAW_PREFIX) :]
        if not is_hashed(digest) or not _ALLOWED_RE.match(digest):
            raise ValueError(f"not a raw digest id: {canonical!r}")
        return digest
    raw = canonical.encode("utf-8")
    out = "".join(chr(b) if b in _SAFE else f"={b:02X}" for b in raw)
    if len(out) > MAX_ENCODED_LEN:
        return _HASH_PREFIX + hashlib.sha256(raw).hexdigest()
    return out


def is_hashed(encoded: str) -> bool:
    return encoded.startswith(_HASH_PREFIX)


def normalise(canonical: str) -> str:
    """The id as it reads back from an engine: unchanged, or ``RAW_PREFIX + digest``."""
    encoded = encode(canonical)
    return RAW_PREFIX + encoded if is_hashed(encoded) else canonical


def decode(encoded: str) -> str:
    """Invert :func:`encode`. A hashed id comes back in its raw form (see ``normalise``)."""
    if encoded == "*":
        return "*"
    if is_hashed(encoded):
        if not _ALLOWED_RE.match(encoded):
            raise ValueError(f"not an encoded id: {encoded!r}")
        return RAW_PREFIX + encoded
    if not _ALLOWED_RE.match(encoded):
        raise ValueError(f"not an encoded id: {encoded!r}")
    raw = bytearray()
    i = 0
    while i < len(encoded):
        if encoded[i] == "=":
            m = _ESCAPE_RE.match(encoded, i)
            if m is None:
                raise ValueError(f"bad escape at {i} in {encoded!r}")
            raw.append(int(m.group(1), 16))
            i += 3
        else:
            raw.append(ord(encoded[i]))
            i += 1
    return raw.decode("utf-8")


def is_valid_encoded(encoded: str) -> bool:
    return encoded == "*" or (bool(_ALLOWED_RE.match(encoded)) and len(encoded) <= MAX_ENCODED_LEN)
