"""Raw-input safety contracts for durable identity canonicalization."""

from __future__ import annotations

import pytest

from trading_assistant.identity import (
    canonical_analyst_version,
    canonical_request_id,
    canonical_symbol,
)


@pytest.mark.parametrize(
    "value",
    [
        "\nrequest.ID:1",
        "request.ID:1\n",
        "\trequest.ID:1",
        "request.ID:1\t",
        "\u00a0request.ID:1\u00a0",
        "request.K",
        "request.ß",
        "request.ı",
    ],
    ids=[
        "leading-newline",
        "trailing-newline",
        "leading-tab",
        "trailing-tab",
        "nbsp",
        "kelvin-sign",
        "sharp-s",
        "dotless-i",
    ],
)
def test_request_id_rejects_unsafe_raw_input_before_transformation(value):
    with pytest.raises(ValueError, match="request_id"):
        canonical_request_id(value)


def test_request_id_trims_only_normal_outer_ascii_spaces():
    assert canonical_request_id("  request.ID:1  ") == "request.ID:1"


@pytest.mark.parametrize(
    "value",
    [
        "\naapl",
        "aapl\n",
        "\taapl",
        "aapl\t",
        "\u00a0aapl\u00a0",
        "K",
        "ß",
        "ı",
    ],
    ids=[
        "leading-newline",
        "trailing-newline",
        "leading-tab",
        "trailing-tab",
        "nbsp",
        "kelvin-sign",
        "sharp-s",
        "dotless-i",
    ],
)
def test_symbol_rejects_unsafe_raw_input_before_case_conversion(value):
    with pytest.raises(ValueError, match="symbol"):
        canonical_symbol(value)


def test_symbol_trims_outer_ascii_spaces_then_applies_ascii_uppercase():
    assert canonical_symbol("  aapl  ") == "AAPL"


@pytest.mark.parametrize(
    "value",
    [
        "\nV2",
        "V2\n",
        "\tV2",
        "V2\t",
        "\u00a0V2\u00a0",
        "K",
        "ß",
        "ı",
    ],
    ids=[
        "leading-newline",
        "trailing-newline",
        "leading-tab",
        "trailing-tab",
        "nbsp",
        "kelvin-sign",
        "sharp-s",
        "dotless-i",
    ],
)
def test_version_rejects_unsafe_raw_input_before_case_conversion(value):
    with pytest.raises(ValueError, match="analyst_version"):
        canonical_analyst_version(value)


def test_version_trims_outer_ascii_spaces_then_applies_ascii_lowercase():
    assert canonical_analyst_version("  V2  ") == "v2"
