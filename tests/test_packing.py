from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest

from app.core.config_models import PANEL_SPECS
from app.processor.packing import BinFormatError, decode_bin, encode_bin, verify_bin
from app.processor.palette import DEVICE_CODES
from tests.reference import fraimic_converter as ref

SPEC_133 = PANEL_SPECS["133"]
SPEC_315 = PANEL_SPECS["315"]
BLACK, WHITE, YELLOW, RED, BLUE, GREEN = range(6)


def random_indices(height: int, width: int, seed: int = 0) -> npt.NDArray[np.uint8]:
    return np.random.default_rng(seed).integers(0, 6, (height, width), dtype=np.uint8)


@pytest.mark.parametrize(("key", "size"), [("133", 960_000), ("315", 2_304_000)])
def test_exact_byte_length(key: str, size: int) -> None:
    spec = PANEL_SPECS[key]  # type: ignore[index]
    data = encode_bin(random_indices(spec.height, spec.width), spec)
    assert len(data) == size == spec.bin_size


@pytest.mark.parametrize("key", ["133", "315"])
def test_every_nibble_is_a_valid_device_code(key: str) -> None:
    spec = PANEL_SPECS[key]  # type: ignore[index]
    raw = np.frombuffer(encode_bin(random_indices(spec.height, spec.width, 1), spec), np.uint8)
    nibbles = set(np.unique(raw >> 4).tolist()) | set(np.unique(raw & 0x0F).tolist())
    assert nibbles == {0x0, 0x1, 0x2, 0x3, 0x5, 0x6}
    assert 0x4 not in nibbles


@pytest.mark.parametrize("key", ["133", "315"])
def test_byte_parity_with_official_packers(key: str) -> None:
    spec = PANEL_SPECS[key]  # type: ignore[index]
    indices = random_indices(spec.height, spec.width, 2)
    official = ref.pack_el133uf1 if key == "133" else ref.pack_el315
    expected = official(ref.COLOR_CODES[indices]).tobytes()
    assert encode_bin(indices, spec) == expected


@pytest.mark.parametrize("key", ["133", "315"])
def test_decode_round_trip(key: str) -> None:
    spec = PANEL_SPECS[key]  # type: ignore[index]
    indices = random_indices(spec.height, spec.width, 3)
    assert np.array_equal(decode_bin(encode_bin(indices, spec), spec), indices)


def test_el133_spec_walkthrough() -> None:
    """Row 0 starts with [Black, White, Yellow, Red] -> bytes 0x01 0x23."""
    indices = np.full((1600, 1200), WHITE, dtype=np.uint8)
    indices[0, :4] = [BLACK, WHITE, YELLOW, RED]
    indices[0, 600:602] = [RED, BLUE]  # first pixels of the right half
    data = encode_bin(indices, SPEC_133)
    assert data[0:2] == bytes([0x01, 0x23])
    assert data[480_000] == 0x35  # right half starts at byte 480,000
    assert data[2] == 0x11


def test_el133_left_half_precedes_right_half() -> None:
    indices = np.zeros((1600, 1200), dtype=np.uint8)
    indices[:, 600:] = GREEN
    data = encode_bin(indices, SPEC_133)
    assert set(data[:480_000]) == {0x00}
    assert set(data[480_000:]) == {0x66}


def test_el315_spec_walkthrough() -> None:
    """Bottom-left 2x2 [[Yellow, Red], [Black, White]] -> first bytes 0x01 0x23."""
    indices = np.full((2560, 1440), WHITE, dtype=np.uint8)
    indices[2559, 0:2] = [BLACK, WHITE]
    indices[2558, 0:2] = [YELLOW, RED]
    data = encode_bin(indices, SPEC_315)
    assert data[0:2] == bytes([0x01, 0x23])


def test_el315_padding_in_ic4_and_ic8() -> None:
    indices = np.zeros((2560, 1440), dtype=np.uint8)  # all black -> real bytes are 0x00
    raw = np.frombuffer(encode_bin(indices, SPEC_315), np.uint8).reshape(8, 720, 400)
    for ic in (3, 7):  # IC4 and IC8
        assert np.all(raw[ic, :, :80] == 0x00)
        assert np.all(raw[ic, :, 80:] == 0x11)
    for ic in (0, 1, 2, 4, 5, 6):
        assert np.all(raw[ic] == 0x00)


def test_el315_matches_spec_frame_mapping() -> None:
    """Check sampled nibbles against the spec formula on an asymmetric pattern."""
    rng = np.random.default_rng(7)
    indices = random_indices(2560, 1440, 4)
    raw = np.frombuffer(encode_bin(indices, SPEC_315), np.uint8)
    codes = DEVICE_CODES[indices]
    for _ in range(5_000):
        k = int(rng.integers(1, 9))  # IC1..IC8
        g, j = (k - 1) // 4, (k - 1) % 4
        real = 160 if j == 3 else 800
        b = int(rng.integers(0, 720))
        p = int(rng.integers(0, real))
        q = j * 800 + p
        x = 2 * b + (q % 2)
        y = 2559 - (g * 1280 + q // 2)
        byte = raw[(k - 1) * 288_000 + b * 400 + p // 2]
        nibble = byte >> 4 if p % 2 == 0 else byte & 0x0F
        assert nibble == codes[y, x], (k, b, p)


def test_el315_is_not_a_plain_transpose() -> None:
    """The spec warns that a transposed layout 'looks plausible'; use an asymmetric marker."""
    indices = np.full((2560, 1440), WHITE, dtype=np.uint8)
    indices[0:80, 1438:1440] = RED  # top-right 80x2 strip -> IC8, last block row
    raw = np.frombuffer(encode_bin(indices, SPEC_315), np.uint8).reshape(8, 720, 400)
    assert np.all(raw[7, 719, :80] == 0x33)
    assert np.count_nonzero(raw == 0x33) == 80


@pytest.mark.parametrize("shape", [(1200, 1600), (1600, 1201), (10, 10)])
def test_encode_rejects_wrong_shape(shape: tuple[int, int]) -> None:
    with pytest.raises(BinFormatError):
        encode_bin(np.zeros(shape, dtype=np.uint8), SPEC_133)


def test_encode_rejects_out_of_range_index() -> None:
    indices = np.zeros((1600, 1200), dtype=np.uint8)
    indices[5, 5] = 6
    with pytest.raises(BinFormatError, match=r"0..5"):
        encode_bin(indices, SPEC_133)


def test_decode_rejects_bad_data() -> None:
    with pytest.raises(BinFormatError, match="960000"):
        decode_bin(b"\x00" * 10, SPEC_133)
    with pytest.raises(BinFormatError, match="nibbles"):
        decode_bin(bytes([0x44]) * 960_000, SPEC_133)


# --- compliance check ----------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["133", "315"])
def test_verify_bin_accepts_an_encoded_frame(key: str) -> None:
    spec = PANEL_SPECS[key]  # type: ignore[index]
    rng = np.random.default_rng(5)
    indices = rng.integers(0, 6, (spec.height, spec.width), dtype=np.uint8)
    verify_bin(encode_bin(indices, spec), spec)


@pytest.mark.parametrize("key", ["133", "315"])
def test_verify_bin_rejects_a_wrong_size_or_header(key: str) -> None:
    spec = PANEL_SPECS[key]  # type: ignore[index]
    good = encode_bin(np.zeros((spec.height, spec.width), dtype=np.uint8), spec)
    for bad in (good[:-1], good + b"\x00", b"\x00\x00\x00\x00" + good):
        with pytest.raises(BinFormatError, match="bytes"):
            verify_bin(bad, spec)


@pytest.mark.parametrize("key", ["133", "315"])
@pytest.mark.parametrize("bad_byte", [0x04, 0x40, 0x70, 0xF0, 0x0F])
def test_verify_bin_rejects_codes_outside_the_palette(key: str, bad_byte: int) -> None:
    spec = PANEL_SPECS[key]  # type: ignore[index]
    data = bytearray(encode_bin(np.zeros((spec.height, spec.width), dtype=np.uint8), spec))
    data[1234] = bad_byte
    with pytest.raises(BinFormatError, match="nibbles"):
        verify_bin(bytes(data), spec)


@pytest.mark.parametrize("block", [3, 7])
def test_verify_bin_rejects_broken_el315_padding(block: int) -> None:
    spec = PANEL_SPECS["315"]
    data = bytearray(encode_bin(np.zeros((spec.height, spec.width), dtype=np.uint8), spec))
    # Row 5 of IC4/IC8: a valid nibble pair, but not the 0x11 padding the spec mandates.
    data[block * 288_000 + 5 * 400 + 200] = 0x00
    with pytest.raises(BinFormatError, match=f"IC{block + 1}"):
        verify_bin(bytes(data), spec)
