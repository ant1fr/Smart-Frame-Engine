"""Verbatim excerpts of the official Fraimic Spectra 6 converter, used as a test oracle.

Source: https://github.com/Fraimic/fraimic_bin_converter
File:   convert_to_bin_spectra6.py (commit 1b794a3)

MIT License

Copyright (c) 2026 Fraimic

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The code below is copied unchanged (only CLI, file I/O, panel plumbing and progress-bar
code was left out; ``enhance`` reproduces the enhancement lines of ``process_image``) so that the application's vectorized implementation can be checked against
the original behavior. Lint rules are relaxed for this file on purpose.
"""

# ruff: noqa
# mypy: ignore-errors

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

# Define the 6-color palette (black, white, yellow, red, blue, green).
# Order matters: index i maps to COLOR_CODES[i].
PALETTE_COLORS = [
    (0, 0, 0),       # Black
    (255, 255, 255), # White
    (255, 255, 0),   # Yellow
    (255, 0, 0),     # Red
    (0, 0, 255),     # Blue
    (0, 255, 0)      # Green
]

# 4-bit device codes for the EL133UF1 panel. Note 0x4 is intentionally skipped.
#                       Black White Yellow Red  Blue Green
COLOR_CODES = np.array([0x0,  0x1,  0x2,   0x3, 0x5, 0x6], dtype=np.uint8)

# White. Used for the off-panel dummy pixels in the EL315 padded ICs, which are
# always 0x11 regardless of the image's own border color.
WHITE_CODE = 0x1

# Letterbox border colors, selectable with --letterbox.
LETTERBOX_COLORS = {'black': (0, 0, 0), 'white': (255, 255, 255)}

# Precompute palette as NumPy arrays for faster access
PALETTE_ARRAY = np.array(PALETTE_COLORS, dtype=np.float32)

# blue and green prints darker, lower its luma value so the distance metric favors it over white, also at luma1
PALETTE_LUMA_ARRAY = np.array([r*250 + g*350 + b*400 for (r, g, b) in PALETTE_COLORS], dtype=np.float32) / (255.0 * 1000)


# Find the closest palette color using floating-point arithmetic (exact RGBL method)
def closest_palette_color(rgb):
    r1, g1, b1 = rgb
    # Calculate luma for the input pixel
    luma1 = (r1 * 250 + g1 * 350 + b1 * 400) / (255.0 * 1000)

    # Calculate differences using precomputed arrays
    diffR = r1 - PALETTE_ARRAY[:, 0]
    diffG = g1 - PALETTE_ARRAY[:, 1]
    diffB = b1 - PALETTE_ARRAY[:, 2]

    # Calculate RGB component of distance
    # boost blue, reduce green a bit and red a little more to compensate for human eye sensitivity and e-ink display characteristics (trial and error)
    rgb_dist = (diffR*diffR*0.250 + diffG*diffG*0.350 + diffB*diffB*0.400) * 0.75 / (255.0*255.0)

    # Calculate luma differences
    luma_diff = luma1 - PALETTE_LUMA_ARRAY
    luma_dist = luma_diff * luma_diff

    # Total distance
    total_dist = 1.5*rgb_dist + 0.60*luma_dist  # hue errors are more important, increased the rgb_dist factor.

    # Find minimum distance index
    return np.argmin(total_dist)


# Atkinson dithering returning a per-pixel palette-index array (not RGB).
# Uses the tuned closest_palette_color metric for each decision.
def quantize_atkinson_indexed(image):
    img_array = np.array(image.convert('RGB'))
    height, width, _ = img_array.shape
    # Use float array for error diffusion to avoid integer truncation issues
    working_img = img_array.astype(np.float32)
    indices = np.zeros((height, width), dtype=np.uint8)

    for y in range(height):
        for x in range(width):
            old_pixel = working_img[y, x].copy()
            # Use exact color comparison instead of lookup table for better accuracy
            idx = closest_palette_color(tuple(np.clip(old_pixel, 0, 255).astype(int)))
            new_pixel = np.array(PALETTE_COLORS[idx], dtype=np.float32)
            working_img[y, x] = new_pixel
            indices[y, x] = idx

            # Calculate error
            error = old_pixel - new_pixel

            # Atkinson error distribution - only to not-yet-processed pixels (right and down)
            # Weights: Right: 1/8, Bottom-left: 1/8, Bottom: 1/4, Bottom-right: 1/8
            # Total distributed: 5/8, which is standard for Atkinson
            if x + 1 < width:
                working_img[y, x + 1] += error * (1/8)
            if y + 1 < height:
                if x - 1 >= 0:
                    working_img[y + 1, x - 1] += error * (1/8)
                working_img[y + 1, x] += error * (1/4)
                if x + 1 < width:
                    working_img[y + 1, x + 1] += error * (1/8)

    return indices


# Scale an (already EXIF-corrected, RGB) image to its final on-panel pixel size.
# Under 'crop' this returns exactly the panel frame; under letterbox/rotate it
# returns the picture alone, which pad_to_frame later centers on the frame.
# Kept separate from padding so enhancement runs on the picture only — see
# process_image.
def scale_to_frame(image, fit, panel):
    img = image
    width, height = img.size
    target_width, target_height = panel.width, panel.height

    # 'rotate': turn landscape images upright so they better fill the portrait
    # frame, then letterbox as usual.
    if fit == 'rotate' and width > height:
        img = img.rotate(90, expand=True)
        width, height = img.size

    if fit == 'crop':
        # Scale to fill, then center-crop the overflow (no borders, edges lost).
        scale_ratio = max(target_width / width, target_height / height)
        resized_width = max(1, int(round(width * scale_ratio)))
        resized_height = max(1, int(round(height * scale_ratio)))
        resized = img.resize((resized_width, resized_height), Image.LANCZOS)
        left = (resized_width - target_width) // 2
        top = (resized_height - target_height) // 2
        return resized.crop((left, top, left + target_width, top + target_height))

    # 'letterbox' (and 'rotate' after reorientation): scale to fit entirely inside
    # the frame, no cropping. Any leftover space becomes bars in pad_to_frame.
    scale_ratio = min(target_width / width, target_height / height)
    resized_width = max(1, int(round(width * scale_ratio)))
    resized_height = max(1, int(round(height * scale_ratio)))
    return img.resize((resized_width, resized_height), Image.LANCZOS)


# Center the scaled picture on the panel frame, padding any remainder with
# letterbox_rgb. A no-op when the picture already fills the frame (e.g. --fit crop).
def pad_to_frame(image, panel, letterbox_rgb):
    if image.size == (panel.width, panel.height):
        return image

    framed = Image.new('RGB', (panel.width, panel.height), letterbox_rgb)
    framed.paste(image, ((panel.width - image.width) // 2,
                         (panel.height - image.height) // 2))
    return framed


# Pack device codes into the EL133UF1 (13.3") .bin layout.
def pack_el133uf1(color_code_array):
    """
    - 1200x1600 pixels, portrait
    - Two pixels per byte (high nibble = even col, low nibble = odd col)
    - Split into left half (cols 0-599) then right half (cols 600-1199):
      all left-half bytes for the whole image come first, then all right-half bytes
    - Total size: 960,000 bytes
    """
    left_half = color_code_array[:, 0:600]
    left_packed = (left_half[:, 0::2] << 4) | left_half[:, 1::2]

    right_half = color_code_array[:, 600:1200]
    right_packed = (right_half[:, 0::2] << 4) | right_half[:, 1::2]

    return np.concatenate([left_packed.ravel(), right_packed.ravel()]).astype(np.uint8)


# EL315 block geometry: 8 IC blocks, each 720 block rows x 800 block pixels.
EL315_BLOCK_ROWS = 720


EL315_BLOCK_PIXELS = 800


EL315_PADDED_PIXELS = 160  # real pixels per row in the padded ICs (IC4, IC8)


EL315_ROW_PIXELS = 2560    # block pixels per block row, across an IC group


EL315_HALF_ROWS = 1280     # image rows covered by one IC group


# Pack device codes into the EL315 (31.5") .bin layout.
def pack_el315(color_code_array):
    """
    - 1440x2560 pixels, portrait
    - 8 IC blocks of 288,000 bytes written sequentially IC1 -> IC8; each block is
      720 block rows x 400 bytes, two pixels per byte (high nibble first)
    - The ICs come in two groups of four, each group covering half the image:
      IC1-IC4 the BOTTOM half (rows 1280-2559), IC5-IC8 the TOP half (rows 0-1279).
    - Within a group, one block row is a 2-pixel-wide VERTICAL STRIP of the image,
      1280 rows tall, read bottom->top; the two pixels of each image row are stored
      left then right. So block row b covers image columns 2b and 2b+1, and block
      pixel p sits at image row offset p // 2 up from the bottom of the half.
    - The four ICs in a group split that 1280-row strip as 400/400/400/80 rows,
      counting up from the bottom of the half, so IC4 and IC8 hold only 160 real
      pixels per row; the remaining 320 bytes of their rows are 0x11 padding.
    - Total size: 2,304,000 bytes
    """
    # Flipping vertically puts the bottom half of the image first, matching the
    # IC1-IC4 group, and makes each half read bottom->top.
    flipped = np.flipud(color_code_array)  # 2560 x 1440

    blocks = []
    for half in range(2):  # IC1-IC4 (bottom half), then IC5-IC8 (top half)
        strip = flipped[half * EL315_HALF_ROWS:(half + 1) * EL315_HALF_ROWS]
        # Regroup 1280 rows x 1440 cols into 720 block rows of 2560 pixels, so that
        # each block row walks one 2-px-wide column pair down the strip.
        band = (strip.reshape(EL315_HALF_ROWS, EL315_BLOCK_ROWS, 2)
                     .transpose(1, 0, 2)
                     .reshape(EL315_BLOCK_ROWS, EL315_ROW_PIXELS))
        for ic in range(4):
            real_pixels = EL315_PADDED_PIXELS if ic == 3 else EL315_BLOCK_PIXELS
            start = ic * EL315_BLOCK_PIXELS
            # Prefill with white so the padded ICs' dummy bytes come out as 0x11.
            nibbles = np.full((EL315_BLOCK_ROWS, EL315_BLOCK_PIXELS), WHITE_CODE, dtype=np.uint8)
            nibbles[:, :real_pixels] = band[:, start:start + real_pixels]
            blocks.append((nibbles[:, 0::2] << 4) | nibbles[:, 1::2])

    return np.concatenate([block.ravel() for block in blocks]).astype(np.uint8)


class Panel:
    """Minimal stand-in for the official Panel class (only width/height are used here)."""

    def __init__(self, width, height):
        self.width = width
        self.height = height


def enhance(scaled_image, brightness=1.1, contrast=1.2, saturation=1.2):
    """The enhancement steps of the official process_image(), same calls in the same order."""
    enhanced_image = ImageEnhance.Brightness(scaled_image).enhance(brightness)
    enhanced_image = ImageEnhance.Contrast(enhanced_image).enhance(contrast)
    enhanced_image = ImageEnhance.Color(enhanced_image).enhance(saturation)
    enhanced_image = enhanced_image.filter(ImageFilter.EDGE_ENHANCE)
    enhanced_image = enhanced_image.filter(ImageFilter.SMOOTH)
    enhanced_image = enhanced_image.filter(ImageFilter.SHARPEN)
    return enhanced_image
