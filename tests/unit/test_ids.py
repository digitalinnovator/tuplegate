import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rag_authz import ids

SPICEDB_ID = re.compile(r"^(([a-zA-Z0-9/_|\-=+]{1,})|\*)$")
# OpenFGA forbids ':' '#' and whitespace inside ids; the whole "type:id" is capped at 256.
OPENFGA_FORBIDDEN = re.compile(r"[:#\s]")

# NUL is reserved for the raw digest form and refused by the model (model._check_id).
ids_text = st.text(min_size=1, max_size=400).filter(lambda s: s != "*" and "\x00" not in s)


@given(ids_text)
def test_encoded_ids_are_valid_for_both_engines(s: str) -> None:
    e = ids.encode(s)
    assert SPICEDB_ID.match(e)
    assert not OPENFGA_FORBIDDEN.search(e)
    assert len(f"container:{e}") <= 256
    assert ids.is_valid_encoded(e)


@given(ids_text)
def test_roundtrip(s: str) -> None:
    e = ids.encode(s)
    # Hashed ids come back in raw form, which encodes to the same digest.
    assert ids.decode(e) == ids.normalise(s)
    assert ids.encode(ids.decode(e)) == e
    if not ids.is_hashed(e):
        assert ids.decode(e) == s


def test_raw_form_cannot_be_forged() -> None:
    from rag_authz.model import doc

    with pytest.raises(ValueError, match="raw digest"):
        ids.encode(ids.RAW_PREFIX + "not-a-digest")
    with pytest.raises(ValueError, match="NUL"):
        doc("a\x00b")
    long_id = "x/" + "é" * 150
    assert doc(ids.normalise(long_id)).id.startswith(ids.RAW_PREFIX)


@given(ids_text, ids_text)
def test_injective(a: str, b: str) -> None:
    if a != b:
        assert ids.encode(a) != ids.encode(b)


def test_readable_examples() -> None:
    assert ids.encode("alice@example.com") == "alice=40example=2Ecom"
    assert ids.encode("github/acme/api/src/app.py") == "github/acme/api/src/app=2Epy"
    assert ids.encode("*") == "*"


def test_long_ids_are_hashed_and_distinct() -> None:
    a = ids.encode("x" * 300)
    b = ids.encode("x" * 301)
    assert ids.is_hashed(a)
    assert ids.is_hashed(b)
    assert a != b


def test_escape_cannot_collide_with_hash_form() -> None:
    # A literal "=h..." in the input is escaped, so it can never look like a digest.
    assert not ids.is_hashed(ids.encode("=h" + "0" * 64))


@pytest.mark.parametrize("bad", ["", "a b", "=4", "=zz", "a.b"])
def test_decode_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError, match=r"encoded id|bad escape|UTF-8|utf-8|not an"):
        ids.decode(bad)
