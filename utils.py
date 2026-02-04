"""
Utility functions used by the booking web application.

This module currently provides a function to convert a QR Code (from
``qrcodegen.QrCode``) into an SVG string.  The implementation is
adapted from the example code in the qrcodegen project:

https://www.nayuki.io/page/qr-code-generator-library

The returned SVG has a white background and black modules.  The caller
can embed the resulting string directly into an HTML document via
``<div>{svg}</div>`` or set it as the ``src`` of an image element using
a ``data:image/svg+xml;base64,...`` URI.
"""

from __future__ import annotations

from typing import List

from .qrcodegen import QrCode  # type: ignore


def to_svg_str(qr: QrCode, border: int = 4) -> str:
    """Return a string of SVG code for the given QR Code.

    Args:
        qr: A ``qrcodegen.QrCode`` instance.
        border: The number of white modules to pad around the code.

    Returns:
        A string containing SVG markup.  The string uses Unix line
        endings (``\n``) irrespective of platform.

    Raises:
        ValueError: If ``border`` is negative.
    """
    if border < 0:
        raise ValueError("Border must be non‑negative")
    parts: List[str] = []
    size = qr.get_size()
    # Build up a series of small path commands for each dark module
    for y in range(size):
        for x in range(size):
            if qr.get_module(x, y):
                parts.append(f"M{x + border},{y + border}h1v1h-1z")
    path_data = " ".join(parts)
    total = size + border * 2
    return (
        f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        f"<!DOCTYPE svg PUBLIC '-//W3C//DTD SVG 1.1//EN' 'http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd'>\n"
        f"<svg xmlns=\"http://www.w3.org/2000/svg\" version=\"1.1\" viewBox=\"0 0 {total} {total}\" stroke=\"none\">\n"
        f"    <rect width=\"100%\" height=\"100%\" fill=\"#FFFFFF\"/>\n"
        f"    <path d=\"{path_data}\" fill=\"#000000\"/>\n"
        f"</svg>\n"
    )