"""Bitcoin folk-hero trading cards.

The card data and artwork live in :mod:`argus.bitcart_cards.build` (which is
also the SVG/PNG generator). This package exposes that data to the rest of
Argus so the Bitcart deploy can seed the rendered PNGs as store products.
"""

from __future__ import annotations

from pathlib import Path

from .build import CARDS, Card

ASSETS_DIR = Path(__file__).resolve().parent


def _description(card: Card) -> str:
    """A store-product description (markdown) assembled from the card text."""
    ability = " ".join(card.ability)
    quote = " ".join(card.flavor)
    return (
        f"**{card.type_line}** — {card.role}\n\n"
        f"{ability}\n\n"
        f"*{quote}*\n"
        f"{card.attribution}"
    )


def product_manifest() -> list[dict[str, object]]:
    """The seed-product list consumed by the deploy-time seeding script.

    Each entry maps one card to a Bitcart product: the PNG filename (relative
    to the copied ``products/`` dir), the price in sats, and a description.
    """
    return [
        {
            "slug": c.slug,
            "name": c.name,
            "price_sats": c.price_sats,
            "image": f"{c.slug}.png",
            "description": _description(c),
        }
        for c in CARDS
    ]


def png_paths() -> list[Path]:
    """Absolute paths of the rendered card PNGs (the product images)."""
    return [ASSETS_DIR / f"{c.slug}.png" for c in CARDS]
