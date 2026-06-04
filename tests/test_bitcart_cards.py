"""Tests for the Bitcoin folk-hero trading cards and their product manifest."""

from __future__ import annotations

from argus.bitcart_cards import CARDS, ASSETS_DIR, png_paths, product_manifest
from argus.bitcart_cards import build


def test_three_cards_with_expected_sat_prices():
    by_name = {c.name: c.price_sats for c in CARDS}
    assert by_name == {
        "Hal Finney": 500,
        "Gavin Andresen": 1000,
        "Satoshi Nakamoto": 10000,
    }


def test_manifest_shape():
    manifest = product_manifest()
    assert len(manifest) == 3
    for item in manifest:
        assert set(item) == {"slug", "name", "price_sats", "image", "description"}
        assert item["image"] == f"{item['slug']}.png"
        assert isinstance(item["price_sats"], int) and item["price_sats"] > 0
        # The flavor quote (italic) carries through to the product description.
        assert "*" in item["description"]


def test_png_assets_committed():
    for png in png_paths():
        assert png.is_file(), f"missing rendered card: {png}"
        assert png.suffix == ".png"
    assert (ASSETS_DIR / "seed_products.py").is_file()


def test_committed_svgs_match_card_data():
    # `build --check` re-renders the SVG from the data and compares it to the
    # committed file; a mismatch means someone edited data without rebuilding.
    assert build.render(check=True) == 0
