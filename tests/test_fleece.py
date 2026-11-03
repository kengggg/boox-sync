"""Wire-format examples and rejection of incomplete/unsupported metadata."""

import pytest
from fleece_fixture import fleece, notebook

from boox_sync.fleece import FleeceError, decode_fleece


def test_shared_keys_unicode_and_current_root():
    keys = ["source", "title", "type", "status"]
    doc = notebook("สมุด " + "long title " * 20, status=0)
    # Older revisions may remain in the body; the final root is authoritative.
    encoded = fleece(doc, keys, prefix=fleece(notebook("Old title", status=1)))
    assert decode_fleece(encoded, keys) == doc


@pytest.mark.parametrize(
    "wire, expected",
    [
        ("0001", 1),
        ("0fff", -1),
        ("3000", None),
        ("3400", False),
        ("3800", True),
        ("6002000100028003", [1, 2]),  # narrow array
        ("680200010000000200008005", [1, 2]),  # wide array
        ("20000000803f8003", 1.0),
        ("2800000000000000f03f8005", 1.0),
        ("110001008002", 256),
        ("426869008002", "hi"),
    ],
)
def test_documented_scalars_and_collections(wire, expected):
    assert decode_fleece(bytes.fromhex(wire)) == expected


@pytest.mark.parametrize(
    "wire", ["", "00", "8000", "8002", "c001", "7001", "4fff", "4400", "3f00"]
)
def test_malformed_or_external_references_rejected(wire):
    with pytest.raises(FleeceError):
        decode_fleece(bytes.fromhex(wire))


def test_unknown_shared_key_rejected():
    with pytest.raises(FleeceError, match="shared keys"):
        decode_fleece(fleece({"title": "Notebook"}, ["title"]))


def test_excessive_nesting_rejected():
    doc = None
    for _ in range(70):
        doc = [doc]
    with pytest.raises(FleeceError, match="nested"):
        decode_fleece(fleece(doc))
