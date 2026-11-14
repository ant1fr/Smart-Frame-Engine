"""Fraimic / Spectra 6 ``.bin`` frame-buffer encoding and decoding.

Both panels store 4-bit device codes, two pixels per byte (high nibble first), with no
header, footer or compression. Only the memory layout differs:

* **EL133UF1 (13.3", 1200x1600, 960,000 bytes):** each row is split into a left half
  (columns 0-599) and a right half (columns 600-1199). All left-half bytes for the
  whole image come first, then all right-half bytes.
* **EL315 (31.5", 1440x2560, 2,304,000 bytes):** 8 IC blocks of 720 block rows x 400
  bytes. IC1-IC4 cover the bottom half of the image and IC5-IC8 the top half. A block
  row is a 2-pixel-wide vertical strip read bottom->top; IC4 and IC8 only carry 160
  real pixels per row, the remaining 320 bytes are ``0x11`` padding.

The packers are ports of the official Fraimic converter (MIT License,
Copyright (c) 2026 Fraimic) and are checked byte-for-byte against it in the tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import numpy as np
import numpy.typing as npt

from app.core.config_models import PANEL_SPECS, PanelModel, PanelSpec
from app.processor.palette import DEVICE_CODES, WHITE_CODE

UInt8Array = npt.NDArray[np.uint8]

EL133_HALF_COLUMNS: Final = 600

EL315_BLOCK_ROWS: Final = 720
EL315_BLOCK_PIXELS: Final = 800
EL315_PADDED_PIXELS: Final = 160
EL315_ROW_PIXELS: Final = 2560
EL315_HALF_ROWS: Final = 1280
EL315_BLOCK_BYTES: Final = EL315_BLOCK_ROWS * EL315_BLOCK_PIXELS // 2

#: Device code -> palette index (``255`` marks codes the panel does not accept).
_CODE_TO_INDEX: Final = np.full(16, 255, dtype=np.uint8)
_CODE_TO_INDEX[DEVICE_CODES] = np.arange(len(DEVICE_CODES), dtype=np.uint8)


class BinFormatError(ValueError):
    """Input does not satisfy the panel's ``.bin`` specification."""


def _pack_nibbles(codes: UInt8Array) -> UInt8Array:
    """Pack pairs of 4-bit codes along the last axis (high nibble first)."""
    return np.asarray((codes[..., 0::2] << 4) | codes[..., 1::2], dtype=np.uint8)


def _unpack_nibbles(packed: UInt8Array) -> UInt8Array:
    """Inverse of :func:`_pack_nibbles`."""
    out = np.empty((*packed.shape[:-1], packed.shape[-1] * 2), dtype=np.uint8)
    out[..., 0::2] = packed >> 4
    out[..., 1::2] = packed & 0x0F
    return out


def pack_el133uf1(codes: UInt8Array) -> UInt8Array:
    """Pack a ``(1600, 1200)`` device-code array into the EL133UF1 layout."""
    left = _pack_nibbles(codes[:, :EL133_HALF_COLUMNS])
    right = _pack_nibbles(codes[:, EL133_HALF_COLUMNS:])
    return np.concatenate([left.ravel(), right.ravel()])


def unpack_el133uf1(data: UInt8Array) -> UInt8Array:
    """Inverse of :func:`pack_el133uf1`, returning ``(1600, 1200)`` device codes."""
    spec = PANEL_SPECS["133"]
    half = data.size // 2
    rows = spec.height
    left = _unpack_nibbles(data[:half].reshape(rows, EL133_HALF_COLUMNS // 2))
    right = _unpack_nibbles(data[half:].reshape(rows, EL133_HALF_COLUMNS // 2))
    return np.concatenate([left, right], axis=1)


def pack_el315(codes: UInt8Array) -> UInt8Array:
    """Pack a ``(2560, 1440)`` device-code array into the EL315 IC-block layout."""
    flipped = np.flipud(codes)  # bottom half first, each half read bottom -> top
    blocks: list[UInt8Array] = []
    for half in range(2):  # IC1-IC4 (bottom half), then IC5-IC8 (top half)
        strip = flipped[half * EL315_HALF_ROWS : (half + 1) * EL315_HALF_ROWS]
        band = (
            strip.reshape(EL315_HALF_ROWS, EL315_BLOCK_ROWS, 2)
            .transpose(1, 0, 2)
            .reshape(EL315_BLOCK_ROWS, EL315_ROW_PIXELS)
        )
        for ic in range(4):
            real_pixels = EL315_PADDED_PIXELS if ic == 3 else EL315_BLOCK_PIXELS
            start = ic * EL315_BLOCK_PIXELS
            nibbles = np.full((EL315_BLOCK_ROWS, EL315_BLOCK_PIXELS), WHITE_CODE, dtype=np.uint8)
            nibbles[:, :real_pixels] = band[:, start : start + real_pixels]
            blocks.append(_pack_nibbles(nibbles))
    return np.concatenate([block.ravel() for block in blocks])


def unpack_el315(data: UInt8Array) -> UInt8Array:
    """Inverse of :func:`pack_el315`, returning ``(2560, 1440)`` device codes."""
    blocks = _unpack_nibbles(data.reshape(8, EL315_BLOCK_ROWS, EL315_BLOCK_PIXELS // 2))
    halves: list[UInt8Array] = []
    for half in range(2):
        group = blocks[half * 4 : half * 4 + 4]
        band = np.concatenate(
            [group[0], group[1], group[2], group[3][:, :EL315_PADDED_PIXELS]], axis=1
        )
        strip = (
            band.reshape(EL315_BLOCK_ROWS, EL315_HALF_ROWS, 2)
            .transpose(1, 0, 2)
            .reshape(EL315_HALF_ROWS, EL315_BLOCK_ROWS * 2)
        )
        halves.append(strip)
    return np.flipud(np.concatenate(halves, axis=0))


PACKERS: Final[dict[PanelModel, Callable[[UInt8Array], UInt8Array]]] = {
    "133": pack_el133uf1,
    "315": pack_el315,
}
UNPACKERS: Final[dict[PanelModel, Callable[[UInt8Array], UInt8Array]]] = {
    "133": unpack_el133uf1,
    "315": unpack_el315,
}


def encode_bin(indices: UInt8Array, spec: PanelSpec) -> bytes:
    """Encode portrait palette indices into the panel's ``.bin`` frame buffer.

    Args:
        indices: ``(height, width)`` palette indices (``0..5``) in the panel's native
            portrait orientation.
        spec: Target panel.

    Raises:
        BinFormatError: On a size mismatch or out-of-range palette index.
    """
    if indices.shape != (spec.height, spec.width):
        raise BinFormatError(
            f"{spec.label} needs {spec.width}x{spec.height} portrait indices, "
            f"got {indices.shape[1] if indices.ndim > 1 else '?'}x{indices.shape[0]}"
        )
    if bool(np.any(indices >= len(DEVICE_CODES))):
        raise BinFormatError("Palette indices must be in 0..5")
    codes = DEVICE_CODES[indices.astype(np.intp, copy=False)]
    packed = PACKERS[spec.key](codes)
    if packed.size != spec.bin_size:
        raise BinFormatError(f"Encoded {packed.size} bytes, expected exactly {spec.bin_size}")
    return packed.tobytes()


def decode_bin(data: bytes, spec: PanelSpec) -> UInt8Array:
    """Decode a ``.bin`` back to ``(height, width)`` portrait palette indices.

    Raises:
        BinFormatError: On a wrong length or a nibble that is not a valid device code.
    """
    if len(data) != spec.bin_size:
        raise BinFormatError(f"{spec.label} .bin must be {spec.bin_size} bytes, got {len(data)}")
    raw = np.frombuffer(data, dtype=np.uint8)
    nibbles = _unpack_nibbles(raw)
    if not np.all(np.isin(nibbles, DEVICE_CODES)):
        raise BinFormatError("The .bin contains nibbles outside {0x0,0x1,0x2,0x3,0x5,0x6}")
    codes = UNPACKERS[spec.key](raw)
    return _CODE_TO_INDEX[codes]
