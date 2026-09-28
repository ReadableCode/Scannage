"""QR codes for the printed labels. A code holds only the box URL."""

from __future__ import annotations

import io

import segno

QUIET_ZONE_MODULES = 4
PIXELS_PER_MODULE = 8


def svg(text: str) -> bytes:
    """SVG with a viewBox and a default size, so it scales in a page and draws on a canvas."""
    # boost_error off: the level stays M instead of rising when the symbol has room
    code = segno.make(text, error="m", micro=False, boost_error=False)
    out = io.BytesIO()
    code.save(
        out,
        kind="svg",
        border=QUIET_ZONE_MODULES,
        omitsize=True,
        xmldecl=False,
        nl=False,
        dark="#000",
        light="#fff",
    )
    # omitsize gives the viewBox; the size is added back because a canvas needs one
    width, height = code.symbol_size(scale=PIXELS_PER_MODULE, border=QUIET_ZONE_MODULES)
    return out.getvalue().replace(b"<svg ", f'<svg width="{width}" height="{height}" '.encode(), 1)
