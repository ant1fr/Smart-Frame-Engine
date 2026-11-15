"""Generate the bundled tileable blue-noise tile (``app/assets/blue_noise_64.npy``).

Void-and-cluster (Ulichney, 1993) on a torus: every cell gets a distinct rank
``0..N*N-1`` such that thresholding at any level yields an evenly spread (blue-noise)
pattern. Deterministic for a given seed, so the committed asset is reproducible::

    python scripts/make_blue_noise.py            # writes app/assets/blue_noise_64.npy
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import numpy.typing as npt

SIZE = 64
SIGMA = 1.9
SEED = 20261115


def gaussian_kernel(size: int, sigma: float) -> npt.NDArray[np.float64]:
    """Toroidal Gaussian centered on cell (0, 0)."""
    axis = np.minimum(np.arange(size), size - np.arange(size)).astype(np.float64)
    distance = axis[:, None] ** 2 + axis[None, :] ** 2
    return np.exp(-distance / (2 * sigma**2))


def void_and_cluster(size: int = SIZE, sigma: float = SIGMA, seed: int = SEED) -> npt.NDArray:
    """Rank matrix (``uint16``) of a ``size`` x ``size`` blue-noise tile."""
    rng = np.random.default_rng(seed)
    total = size * size
    kernel = gaussian_kernel(size, sigma)

    def stamp(y: int, x: int) -> npt.NDArray[np.float64]:
        return np.roll(kernel, (y, x), axis=(0, 1))

    # --- Initial pattern: ~10% random ones, relaxed until the tightest cluster and the
    # largest void coincide.
    pattern = np.zeros((size, size), dtype=bool)
    ones = max(1, total // 10)
    pattern.flat[rng.choice(total, ones, replace=False)] = True
    energy = np.zeros((size, size))
    for y, x in zip(*np.nonzero(pattern), strict=True):
        energy += stamp(y, x)
    while True:
        masked = np.where(pattern, energy, -np.inf)
        cy, cx = np.unravel_index(np.argmax(masked), masked.shape)
        pattern[cy, cx] = False
        energy -= stamp(cy, cx)
        masked = np.where(pattern, np.inf, energy)
        vy, vx = np.unravel_index(np.argmin(masked), masked.shape)
        pattern[vy, vx] = True
        energy += stamp(vy, vx)
        if (vy, vx) == (cy, cx):
            break

    ranks = np.zeros((size, size), dtype=np.int64)

    # --- Phase 1: remove the tightest cluster repeatedly, ranks count down from `ones`-1.
    work = pattern.copy()
    work_energy = energy.copy()
    for rank in range(ones - 1, -1, -1):
        masked = np.where(work, work_energy, -np.inf)
        y, x = np.unravel_index(np.argmax(masked), masked.shape)
        ranks[y, x] = rank
        work[y, x] = False
        work_energy -= stamp(y, x)

    # --- Phase 2: from the initial pattern, fill the largest void until half are ones.
    work = pattern.copy()
    work_energy = energy.copy()
    for rank in range(ones, total // 2):
        masked = np.where(work, np.inf, work_energy)
        y, x = np.unravel_index(np.argmin(masked), masked.shape)
        ranks[y, x] = rank
        work[y, x] = True
        work_energy += stamp(y, x)

    # --- Phase 3: the zeros are now the minority: place the rest at the tightest cluster
    # of zeros (the largest void of the inverted pattern).
    zero_energy = np.zeros((size, size))
    for y, x in zip(*np.nonzero(~work), strict=True):
        zero_energy += stamp(y, x)
    for rank in range(total // 2, total):
        masked = np.where(work, -np.inf, zero_energy)
        y, x = np.unravel_index(np.argmax(masked), masked.shape)
        ranks[y, x] = rank
        work[y, x] = True
        zero_energy -= stamp(y, x)

    return ranks.astype(np.uint16)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("app/assets") / f"blue_noise_{SIZE}.npy")
    arguments = parser.parse_args()
    tile = void_and_cluster()
    if not np.array_equal(np.sort(tile.ravel()), np.arange(SIZE * SIZE)):
        raise SystemExit("ranks are not a permutation")
    np.save(arguments.out, tile)
    print(f"wrote {arguments.out} ({tile.shape}, {tile.dtype})")
