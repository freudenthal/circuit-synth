"""Honest availability facade (Stage 10.3).

Fully mocked -- no network, no credentials required. Verifies the facade:
- skips sources with no credentials (never fabricates stock),
- normalizes real DigiKey/JLC rows into PartAvailability,
- never returns the JLC web-scraper demo data,
- handles a mix of one-up / one-down source.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from circuit_synth.manufacturing.availability import (
    AvailabilityReport,
    PartAvailability,
    check_availability,
)


def _digikey_component(mpn, stock, price, ds="http://d.example/ds.pdf"):
    return SimpleNamespace(
        manufacturer_part_number=mpn,
        quantity_available=stock,
        unit_price=price,
        datasheet_url=ds,
    )


def test_no_credentials_skips_both_sources(monkeypatch):
    """No creds anywhere -> both sources skipped, zero fabricated results."""
    monkeypatch.delenv("JLCPCB_KEY", raising=False)
    monkeypatch.delenv("JLCPCB_SECRET", raising=False)

    # DigiKey client construction raises ValueError when unconfigured.
    with patch(
        "circuit_synth.manufacturing.digikey.DigiKeyComponentSearch",
        side_effect=ValueError("DigiKey API credentials not configured"),
    ):
        report = check_availability("2N7000")

    assert isinstance(report, AvailabilityReport)
    assert report.results == []
    assert report.skipped["digikey"] == "no credentials"
    assert report.skipped["jlcpcb"] == "no credentials"
    assert not report  # __bool__ is False when empty


def test_digikey_hit_normalized(monkeypatch):
    """A mocked DigiKey result becomes a normalized PartAvailability."""
    monkeypatch.delenv("JLCPCB_KEY", raising=False)
    monkeypatch.delenv("JLCPCB_SECRET", raising=False)

    fake_search = MagicMock()
    fake_search.search_components.return_value = [
        _digikey_component("2N7000", 50000, 0.12),
        _digikey_component("2N7002", 3, 0.05),  # below min_stock filter below
    ]
    with patch(
        "circuit_synth.manufacturing.digikey.DigiKeyComponentSearch",
        return_value=fake_search,
    ):
        report = check_availability("2N7000", min_stock=100)

    dk = [r for r in report.results if r.source == "digikey"]
    assert len(dk) == 1  # the stock=3 row filtered out
    row = dk[0]
    assert isinstance(row, PartAvailability)
    assert row.mpn == "2N7000"
    assert row.stock == 50000
    assert row.unit_price == pytest.approx(0.12)
    assert row.datasheet_url == "http://d.example/ds.pdf"
    assert report.skipped["jlcpcb"] == "no credentials"


def test_jlc_uses_api_not_demo_scraper(monkeypatch):
    """With JLC creds set, the facade calls the credentialed API, not the scraper."""
    monkeypatch.setenv("JLCPCB_KEY", "k")
    monkeypatch.setenv("JLCPCB_SECRET", "s")

    fake_iface = MagicMock()
    fake_iface.search_components.return_value = [
        {
            "manufacturer_part": "2N7000",
            "lcsc_part": "C8492",
            "stock": 12000,
            "price": "$0.03@100pcs",
            "datasheet": "http://jlc.example/2n7000.pdf",
        }
    ]
    with patch(
        "circuit_synth.manufacturing.jlcpcb.jlc_parts_lookup.JlcPartsInterface",
        return_value=fake_iface,
    ):
        report = check_availability("2N7000", sources=("jlcpcb",))

    jlc = [r for r in report.results if r.source == "jlcpcb"]
    assert len(jlc) == 1
    assert jlc[0].mpn == "2N7000"
    assert jlc[0].stock == 12000
    assert jlc[0].unit_price == pytest.approx(0.03)  # parsed from "$0.03@100pcs"
    # Demo-data marker must never appear in real results.
    assert all(getattr(r, "demo_data", None) is None for r in report.results)


def test_mixed_one_up_one_down(monkeypatch):
    """DigiKey up, JLC down (no creds): one result, JLC skipped."""
    monkeypatch.delenv("JLCPCB_KEY", raising=False)
    monkeypatch.delenv("JLCPCB_SECRET", raising=False)

    fake_search = MagicMock()
    fake_search.search_components.return_value = [
        _digikey_component("LM358", 8000, 0.15)
    ]
    with patch(
        "circuit_synth.manufacturing.digikey.DigiKeyComponentSearch",
        return_value=fake_search,
    ):
        report = check_availability("LM358")

    assert len(report.results) == 1
    assert report.results[0].source == "digikey"
    assert report.skipped == {"jlcpcb": "no credentials"}


def test_network_error_is_a_skip_not_a_raise(monkeypatch):
    """A supplier query exception becomes a skip reason, never propagates."""
    monkeypatch.delenv("JLCPCB_KEY", raising=False)
    monkeypatch.delenv("JLCPCB_SECRET", raising=False)

    fake_search = MagicMock()
    fake_search.search_components.side_effect = RuntimeError("connection reset")
    with patch(
        "circuit_synth.manufacturing.digikey.DigiKeyComponentSearch",
        return_value=fake_search,
    ):
        report = check_availability("2N7000", sources=("digikey",))

    assert report.results == []
    assert "query error" in report.skipped["digikey"]
    assert "connection reset" in report.skipped["digikey"]


def test_unknown_source_skipped(monkeypatch):
    monkeypatch.delenv("JLCPCB_KEY", raising=False)
    monkeypatch.delenv("JLCPCB_SECRET", raising=False)
    report = check_availability("2N7000", sources=("mouser",))
    assert report.skipped["mouser"] == "unknown source"
