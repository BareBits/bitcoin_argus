#!/usr/bin/env python3
"""Generate the Bitcoin folk-hero trading cards (SVG source + PNG render).

The card data below is the single source of truth. Running this script writes,
for every card, a square ``<slug>.svg`` (the committed source artwork) and a
``<slug>.png`` rendered from it with ``rsvg-convert`` at the size Bitcart wants
for a product image. Both files live next to this script and are committed.

The art is original vector work themed to match the project's "hacker" theme
(deep navy-black with glowing blue panels); the layout borrows the familiar
collectible-card structure (name header, art window, a single strength stat, a
rules/ability box, and an italic flavor quote).

Usage:
    python3 -m argus.bitcart_cards.build        # regenerate svg + png
    python3 -m argus.bitcart_cards.build --check # fail if outputs are stale
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Square canvas (decision: portrait card centered on a square background so it
# renders correctly in Bitcart's square product grid). Rendered 1:1.
CANVAS = 1000
PNG_SIZE = 1000  # Bitcart resizes product images; a 1000px square is ample.

# --- Hacker-theme palette (mirrors argus/web/static/themes/hacker.css) --------
BG = "#02030c"
PANEL_TOP = "#0a1538"
PANEL_BOTTOM = "#03050f"
LINE = "#2746b8"
LINE_BRIGHT = "#5a86ff"
FG = "#cdd9ff"
FG_BRIGHT = "#ffffff"
ACCENT = "#6ea0ff"
INK = "#010410"


@dataclass(frozen=True)
class Card:
    slug: str
    name: str
    role: str
    type_line: str  # title shown on the strength bar (e.g. "PROGRAMMING LEGEND")
    strength: str
    price_sats: int
    number: str  # collector number, e.g. "1/3"
    ability: list[str]  # pre-wrapped lines
    flavor: list[str]  # pre-wrapped italic quote lines
    attribution: str
    art: str  # one of the art-motif keys below
    fields: dict = field(default_factory=dict)


CARDS: list[Card] = [
    Card(
        slug="hal-finney",
        name="Hal Finney",
        role="Cypherpunk · Cryptographer",
        type_line="Cryptography Genius",
        strength="8",
        price_sats=500,
        number="1/3",
        ability=[
            "Invented Reusable Proof-of-Work.",
            "Ran the first node beside Satoshi",
            "and received the first Bitcoin",
            "transaction.",
        ],
        flavor=["“Running bitcoin.”"],
        attribution="— January 10, 2009",
        art="finney",
    ),
    Card(
        slug="gavin-andresen",
        name="Gavin Andresen",
        role="Core Maintainer · Faucet-Keeper",
        type_line="Bitcoin Ops Specialist",
        strength="7",
        price_sats=1000,
        number="2/3",
        ability=[
            "Built the first Bitcoin faucet and",
            "stewarded the reference client as",
            "lead maintainer after Satoshi",
            "stepped away.",
        ],
        flavor=[
            "“Bitcoin is designed to bring us",
            "back to a decentralized currency",
            "of the people.”",
        ],
        attribution="— Forbes, 2011",
        art="andresen",
    ),
    Card(
        slug="satoshi-nakamoto",
        name="Satoshi Nakamoto",
        role="Founder · The Vanished",
        type_line="Programming Legend",
        strength="∞",
        price_sats=10000,
        number="3/3",
        ability=[
            "Authored the whitepaper, mined",
            "the genesis block, then vanished",
            "— leaving the protocol to the world.",
        ],
        flavor=[
            "“The Times 03/Jan/2009",
            "Chancellor on brink of second",
            "bailout for banks.”",
        ],
        attribution="— Genesis block, Jan 3, 2009",
        art="satoshi",
    ),
]


def _fmt_price(sats: int) -> str:
    return f"{sats:,} sats"


# Card frame geometry (centered portrait card on the square canvas).
CX, CY, CW, CH = 180, 50, 640, 900


def _coin(cx: float, cy: float, r: float, *, glow: bool = True) -> str:
    """A glowing coin stamped with the bitcoin glyph."""
    g = ' filter="url(#glow)"' if glow else ""
    return (
        f'<g{g}>'
        f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" fill="url(#coin)" '
        f'stroke="{LINE_BRIGHT}" stroke-width="2"/>'
        f'<text x="{cx:.0f}" y="{cy + r*0.55:.0f}" text-anchor="middle" '
        f'font-family="DejaVu Sans, sans-serif" font-weight="bold" '
        f'font-size="{r*1.3:.0f}" fill="{INK}">₿</text>'
        f'</g>'
    )


def _scene_finney(cx: int, cy: int) -> str:
    """A retro terminal/monitor literally running bitcoin."""
    sx, sy, sw, sh = cx - 168, cy - 104, 336, 168  # screen
    lx = sx + 22
    lines = [
        ("$ ./bitcoind -daemon", ACCENT),
        ("[init] Bitcoin v0.1.0", "#7fa6e6"),
        ("[net]  0 connections", "#7fa6e6"),
        ("running bitcoin", "#49e0a0"),
    ]
    text = ""
    ly = sy + 44
    for s, col in lines:
        weight = "bold" if s == "running bitcoin" else "normal"
        text += (
            f'<text x="{lx}" y="{ly}" font-family="DejaVu Sans Mono, monospace" '
            f'font-size="20" font-weight="{weight}" fill="{col}">{s}</text>'
        )
        ly += 32
    cursor = (
        f'<rect x="{lx+186}" y="{ly-52}" width="13" height="22" '
        f'fill="#49e0a0"/>'
    )
    return f"""
      <!-- monitor bezel + stand -->
      <rect x="{sx-16}" y="{sy-16}" width="{sw+32}" height="{sh+32}" rx="16"
            fill="url(#metal)" stroke="{LINE_BRIGHT}" stroke-width="2"
            filter="url(#glow)"/>
      <rect x="{cx-26}" y="{sy+sh+16}" width="52" height="34" fill="url(#metal)"/>
      <rect x="{cx-90}" y="{sy+sh+48}" width="180" height="16" rx="6"
            fill="url(#metal)" stroke="{LINE}" stroke-width="1"/>
      <!-- screen -->
      <rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" rx="8"
            fill="url(#screen)" stroke="{LINE}" stroke-width="2"/>
      {text}
      {cursor}
    """


def _scene_andresen(cx: int, cy: int) -> str:
    """A wall faucet dispensing a stream of bitcoin into a pile (first faucet)."""
    # Pipe in from the upper-left, down to a tap nozzle.
    px = cx - 150
    pipe = f"""
      <rect x="{px}" y="{cy-118}" width="150" height="24" rx="6"
            fill="url(#metal)" stroke="{LINE}" stroke-width="1.5"/>
      <rect x="{cx-12}" y="{cy-118}" width="26" height="66" rx="6"
            fill="url(#metal)" stroke="{LINE}" stroke-width="1.5"/>
      <path d="M {cx-22} {cy-54} h 46 l -8 26 h -30 z"
            fill="url(#metal)" stroke="{LINE_BRIGHT}" stroke-width="2"
            filter="url(#glow)"/>
    """
    # A falling stream of coins from the nozzle.
    stream = ""
    for i, (dy, r) in enumerate([(8, 13), (44, 15), (84, 17)]):
        stream += _coin(cx + (i - 1) * 6, cy - 16 + dy, r)
    # A heaped pile of coins at the bottom.
    pile = ""
    heap = [
        (cx-78, cy+96, 20), (cx-40, cy+104, 22), (cx, cy+108, 24),
        (cx+42, cy+104, 22), (cx+80, cy+96, 20),
        (cx-58, cy+78, 19), (cx-18, cy+84, 21), (cx+24, cy+84, 21),
        (cx+62, cy+78, 19), (cx, cy+62, 20),
    ]
    for hx, hyp, r in heap:
        pile += _coin(hx, hyp, r, glow=False)
    return pipe + stream + pile


def _scene_satoshi(cx: int, cy: int, x: int, y: int, w: int, h: int) -> str:
    """A Matrix-style wall of falling ones and zeroes.

    Deterministic (seeded) so the committed SVG/PNG are stable across rebuilds.
    """
    rnd = random.Random(20090103)  # genesis date, for a stable layout
    col_w, row_h = 30, 26
    glyph = ""
    x0 = x + 16
    y0 = y + 26
    cols = (w - 32) // col_w
    rows = (h - 24) // row_h
    for c in range(cols):
        gx = x0 + c * col_w + col_w // 2
        head = rnd.randint(1, rows - 1)  # brightest glyph in this column
        for r in range(rows):
            gy = y0 + r * row_h
            ch = "1" if rnd.random() < 0.5 else "0"
            if r == head:
                fill, op = "#d7ffe9", 0.95  # bright leading glyph
            elif r < head:
                fill, op = "#49e0a0", max(0.12, 0.5 - (head - r) * 0.06)
            else:
                fill, op = "#1f8f63", 0.22
            glyph += (
                f'<text x="{gx}" y="{gy}" text-anchor="middle" '
                f'font-family="DejaVu Sans Mono, monospace" font-size="22" '
                f'fill="{fill}" opacity="{op:.2f}">{ch}</text>'
            )
    return glyph


def _art(motif: str, x: int, y: int, w: int, h: int) -> str:
    """The art window: an iconic scene for each figure's legend."""
    cx = x + w // 2
    cy = y + h // 2
    if motif == "finney":
        scene = _scene_finney(cx, cy)
    elif motif == "andresen":
        scene = _scene_andresen(cx, cy)
    else:
        scene = _scene_satoshi(cx, cy, x, y, w, h)
    return f"""
    <g clip-path="url(#artclip)">
      <rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{INK}"/>
      <rect x="{x}" y="{y}" width="{w}" height="{h}" fill="url(#artbg)"/>
      {scene}
    </g>
    <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="none"
          stroke="{LINE}" stroke-width="2"/>
    """




def build_svg(card: Card) -> str:
    pad = 22
    ix = CX + pad
    iw = CW - 2 * pad
    # vertical layout cursors
    header_y = CY + 22
    header_h = 92
    art_y = header_y + header_h + 14
    art_h = 296
    typebar_y = art_y + art_h + 12
    typebar_h = 34
    box_y = typebar_y + typebar_h + 12
    box_h = 300
    footer_y = CY + CH - 78

    # Ability text lines.
    ability_spans = ""
    ay = box_y + 44
    for line in card.ability:
        ability_spans += (
            f'<text x="{CX + CW//2}" y="{ay}" text-anchor="middle" '
            f'font-family="DejaVu Sans, sans-serif" font-size="23" '
            f'fill="{FG_BRIGHT}">{line}</text>\n'
        )
        ay += 33

    # Flavor (italic) lines + attribution.
    fy = ay + 26
    flavor_spans = (
        f'<line x1="{ix+40}" y1="{ay+2}" x2="{ix+iw-40}" y2="{ay+2}" '
        f'stroke="{LINE}" stroke-width="1" opacity="0.7"/>\n'
    )
    for line in card.flavor:
        flavor_spans += (
            f'<text x="{CX + CW//2}" y="{fy}" text-anchor="middle" '
            f'font-family="DejaVu Sans, sans-serif" font-style="italic" '
            f'font-size="21" fill="{FG}">{line}</text>\n'
        )
        fy += 29
    flavor_spans += (
        f'<text x="{CX + CW//2}" y="{fy+6}" text-anchor="middle" '
        f'font-family="DejaVu Sans, sans-serif" font-style="italic" '
        f'font-size="16" fill="{ACCENT}">{card.attribution}</text>\n'
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS}" height="{CANVAS}"
     viewBox="0 0 {CANVAS} {CANVAS}">
  <defs>
    <radialGradient id="page" cx="70%" cy="-10%" r="90%">
      <stop offset="0%" stop-color="#1a3aa0" stop-opacity="0.35"/>
      <stop offset="60%" stop-color="{BG}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="panel" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{PANEL_TOP}"/>
      <stop offset="100%" stop-color="{PANEL_BOTTOM}"/>
    </linearGradient>
    <linearGradient id="bar" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#061235"/>
      <stop offset="50%" stop-color="#3f6cff"/>
      <stop offset="100%" stop-color="#061235"/>
    </linearGradient>
    <linearGradient id="btn" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#2a4fd0"/>
      <stop offset="100%" stop-color="#1b3082"/>
    </linearGradient>
    <radialGradient id="artbg" cx="50%" cy="30%" r="80%">
      <stop offset="0%" stop-color="#0b1b48"/>
      <stop offset="100%" stop-color="{INK}"/>
    </radialGradient>
    <radialGradient id="coin" cx="42%" cy="36%" r="70%">
      <stop offset="0%" stop-color="#bcd3ff"/>
      <stop offset="55%" stop-color="#6e9bff"/>
      <stop offset="100%" stop-color="#1b3082"/>
    </radialGradient>
    <linearGradient id="metal" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#6e8fd8"/>
      <stop offset="100%" stop-color="#16265e"/>
    </linearGradient>
    <radialGradient id="screen" cx="50%" cy="30%" r="90%">
      <stop offset="0%" stop-color="#04183a"/>
      <stop offset="100%" stop-color="{INK}"/>
    </radialGradient>
    <radialGradient id="badge" cx="50%" cy="40%" r="60%">
      <stop offset="0%" stop-color="#3f6cff"/>
      <stop offset="100%" stop-color="#0a1538"/>
    </radialGradient>
    <filter id="glow" x="-40%" y="-40%" width="180%" height="180%">
      <feGaussianBlur stdDeviation="6" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <clipPath id="artclip">
      <rect x="{ix}" y="{art_y}" width="{iw}" height="{art_h}" rx="8"/>
    </clipPath>
  </defs>

  <!-- page background -->
  <rect width="{CANVAS}" height="{CANVAS}" fill="{BG}"/>
  <rect width="{CANVAS}" height="{CANVAS}" fill="url(#page)"/>

  <!-- card frame -->
  <rect x="{CX}" y="{CY}" width="{CW}" height="{CH}" rx="26" fill="url(#panel)"
        stroke="{LINE_BRIGHT}" stroke-width="3" filter="url(#glow)"/>
  <rect x="{CX+6}" y="{CY+6}" width="{CW-12}" height="{CH-12}" rx="20"
        fill="none" stroke="{LINE}" stroke-width="1.5" opacity="0.8"/>

  <!-- header: signature centered gradient bar -->
  <rect x="{ix}" y="{header_y}" width="{iw}" height="{header_h}" rx="10"
        fill="url(#bar)" stroke="{LINE_BRIGHT}" stroke-width="1"/>
  <text x="{CX + CW//2}" y="{header_y+44}" text-anchor="middle"
        font-family="DejaVu Sans, sans-serif" font-weight="bold"
        font-size="38" fill="{FG_BRIGHT}">{card.name}</text>
  <text x="{CX + CW//2}" y="{header_y+74}" text-anchor="middle"
        font-family="DejaVu Sans, sans-serif" font-size="18"
        letter-spacing="1" fill="#dbe6ff">{card.role}</text>

  <!-- art window -->
  {_art(card.art, ix, art_y, iw, art_h)}

  <!-- type / strength bar -->
  <rect x="{ix}" y="{typebar_y}" width="{iw}" height="{typebar_h}" rx="6"
        fill="url(#btn)" stroke="{LINE_BRIGHT}" stroke-width="1"/>
  <text x="{ix+14}" y="{typebar_y+24}" font-family="DejaVu Sans Mono, monospace"
        font-size="18" letter-spacing="2" fill="#dbe6ff">{card.type_line.upper()}</text>
  <text x="{ix+iw-14}" y="{typebar_y+24}" text-anchor="end"
        font-family="DejaVu Sans Mono, monospace" font-size="18"
        letter-spacing="1" fill="#dbe6ff">STRENGTH {card.strength}</text>

  <!-- ability / flavor text box -->
  <rect x="{ix}" y="{box_y}" width="{iw}" height="{box_h}" rx="8"
        fill="{INK}" stroke="{LINE}" stroke-width="1.5"/>
  {ability_spans}
  {flavor_spans}

  <!-- footer: price + collector line -->
  <rect x="{ix}" y="{footer_y}" width="{iw}" height="56" rx="8"
        fill="url(#btn)" stroke="{LINE_BRIGHT}" stroke-width="1"/>
  <text x="{ix+16}" y="{footer_y+37}" font-family="DejaVu Sans, sans-serif"
        font-weight="bold" font-size="28" fill="{FG_BRIGHT}"
        filter="url(#glow)">{_fmt_price(card.price_sats)}</text>
  <text x="{ix+iw-16}" y="{footer_y+36}" text-anchor="end"
        font-family="DejaVu Sans Mono, monospace" font-size="16"
        fill="#cfdcff">ARGUS · {card.number}</text>

  <!-- strength badge (overlaps the top-left card corner) -->
  <circle cx="{CX+58}" cy="{CY+58}" r="40" fill="url(#badge)"
          stroke="{LINE_BRIGHT}" stroke-width="2.5" filter="url(#glow)"/>
  <text x="{CX+58}" y="{CY+72}" text-anchor="middle"
        font-family="DejaVu Sans, sans-serif" font-weight="bold"
        font-size="40" fill="{FG_BRIGHT}">{card.strength}</text>
</svg>
"""


def render(check: bool = False) -> int:
    rsvg = "rsvg-convert"
    stale: list[str] = []
    for card in CARDS:
        svg_path = HERE / f"{card.slug}.svg"
        png_path = HERE / f"{card.slug}.png"
        svg = build_svg(card)
        if check:
            if not svg_path.exists() or svg_path.read_text() != svg:
                stale.append(svg_path.name)
        else:
            svg_path.write_text(svg)
            subprocess.run(
                [rsvg, "-w", str(PNG_SIZE), "-h", str(PNG_SIZE),
                 str(svg_path), "-o", str(png_path)],
                check=True,
            )
            print(f"wrote {svg_path.name} + {png_path.name}")
    if check and stale:
        print(f"STALE (run build): {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify committed SVGs match the data; do not write")
    args = ap.parse_args()
    return render(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
