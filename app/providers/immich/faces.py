"""Turn Immich face boxes into :class:`~app.processor.fit.FocusRegion` objects.

Verified against Immich 3.3.5: ``GET /api/faces?id=<asset>`` boxes are pixels of the
face's own ``imageWidth`` x ``imageHeight`` frame, which is the EXIF-*corrected*
orientation (same as the asset ``width``/``height``) - exactly the frame our decoder
produces after ``exif_transpose``. An orientation guard still covers servers that report
boxes in raw sensor space: for EXIF orientations 5-8 the frame's portrait/landscape
shape then disagrees with the corrected image and the boxes are rotated into place.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Final

from app.processor.fit import FocusRegion
from app.providers.immich.models import FaceDto

#: Room added on every side of a face so hair, chin and shoulders survive the crop.
FACE_PADDING: Final = 0.25

Box = tuple[float, float, float, float]


def _orient(box: Box, orientation: int) -> Box:
    """Map a normalized raw-sensor box into the displayed frame (EXIF orientation 5-8)."""
    x1, y1, x2, y2 = box
    corners = [(x1, y1), (x2, y1), (x1, y2), (x2, y2)]
    if orientation == 6:  # rotate 90 clockwise to display
        mapped = [(1 - v, u) for u, v in corners]
    elif orientation == 8:  # rotate 90 counter-clockwise
        mapped = [(v, 1 - u) for u, v in corners]
    elif orientation == 5:  # transpose
        mapped = [(v, u) for u, v in corners]
    else:  # 7: transverse
        mapped = [(1 - v, 1 - u) for u, v in corners]
    xs, ys = [p[0] for p in mapped], [p[1] for p in mapped]
    return min(xs), min(ys), max(xs), max(ys)


def _parse_orientation(value: str | int | None) -> int | None:
    try:
        number = int(value) if value is not None else None
    except ValueError:
        return None
    return number if number in range(1, 9) else None


def face_regions(
    faces: Sequence[FaceDto],
    *,
    image_size: tuple[int, int],
    target_person_ids: Collection[str] = (),
    exif_orientation: str | int | None = None,
    padding: float = FACE_PADDING,
) -> list[FocusRegion]:
    """Focus regions for smart cropping, most relevant first.

    Args:
        faces: Detected faces of the asset.
        image_size: ``(width, height)`` of the EXIF-corrected image.
        target_person_ids: People the user asked for. Their faces are used exclusively
            (strangers and unassigned faces are ignored); all faces are the fallback when
            none of them was detected.
        exif_orientation: EXIF orientation of the original (used by the orientation guard).
        padding: Fraction of each box added on every side.
    """
    targets = set(target_person_ids)
    chosen = [face for face in faces if face.person is not None and face.person.id in targets]
    pool = chosen or list(faces)
    orientation = _parse_orientation(exif_orientation)
    image_is_landscape = image_size[0] > image_size[1]

    regions: list[FocusRegion] = []
    for face in pool:
        if face.imageWidth <= 0 or face.imageHeight <= 0:
            continue
        box: Box = (
            min(face.boundingBoxX1, face.boundingBoxX2) / face.imageWidth,
            min(face.boundingBoxY1, face.boundingBoxY2) / face.imageHeight,
            max(face.boundingBoxX1, face.boundingBoxX2) / face.imageWidth,
            max(face.boundingBoxY1, face.boundingBoxY2) / face.imageHeight,
        )
        box_frame_is_landscape = face.imageWidth > face.imageHeight
        non_square = image_size[0] != image_size[1] and face.imageWidth != face.imageHeight
        if (
            orientation in {5, 6, 7, 8}
            and non_square
            and box_frame_is_landscape != image_is_landscape
        ):
            box = _orient(box, orientation or 0)
        left, top = min(max(box[0], 0.0), 1.0), min(max(box[1], 0.0), 1.0)
        right, bottom = min(max(box[2], 0.0), 1.0), min(max(box[3], 0.0), 1.0)
        if right - left <= 0 or bottom - top <= 0:
            continue
        regions.append(FocusRegion(left, top, right - left, bottom - top).padded(padding))
    regions.sort(key=lambda region: region.area, reverse=True)
    return regions
