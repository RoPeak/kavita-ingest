from __future__ import annotations

import pytest

from kavita_ingest.domain import SequenceKind, SequenceNumber


@pytest.mark.parametrize(
    ("raw", "normalized", "kind", "rendered"),
    [
        ("1", "1", SequenceKind.INTEGER, "001"),
        ("001", "1", SequenceKind.INTEGER, "001"),
        ("0.5", "0.5", SequenceKind.DECIMAL, "0.5"),
        ("70.5", "70.5", SequenceKind.DECIMAL, "70.5"),
        ("1A", "1A", SequenceKind.ALPHANUMERIC, "1A"),
        ("1-5", "1-5", SequenceKind.RANGE, "1-5"),
        ("TPB1", "TPB1", SequenceKind.SYMBOLIC, "TPB1"),
    ],
)
def test_sequence_number_preserves_identity(
    raw: str, normalized: str, kind: SequenceKind, rendered: str
) -> None:
    value = SequenceNumber.parse(raw)
    assert value.raw == raw
    assert value.normalized == normalized
    assert value.kind is kind
    assert value.render() == rendered


def test_sequence_number_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        SequenceNumber.parse(" ")


def test_decimal_sort_key_distinguishes_fractional_magnitude() -> None:
    assert SequenceNumber.parse("0.05").sort_key < SequenceNumber.parse("0.5").sort_key
