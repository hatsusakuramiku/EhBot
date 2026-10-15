"""Cross-transport document identity follows TDLib's public unique-file encoding."""
import pytest

from app.connections.telegram_files import document_id


@pytest.mark.parametrize("value, expected", [
    ("AgADKgAH", 42), ("mtproto:42", 42), ("AgADKQAH", 41),
    ("mtproto:-42", -42), ("mtproto:9223372036854775807", 2**63 - 1),
    ("mtproto:-9223372036854775808", -(2**63)),
    ("mtproto:0", None), ("mtproto:9223372036854775808", None),
    ("mtproto: 42", None), ("mtproto:+42", None), ("unknown", None),
    ("AgA", None), ("AgAA", None), ("AgADAAc", None), ("AQADKgAH", None),
    ("AwADKgAH", None), ("AgADKgAHAAE", None), (None, None), (42, None),
])
def test_document_identity(value, expected):
    assert document_id({"file_unique_id": value}) == expected
