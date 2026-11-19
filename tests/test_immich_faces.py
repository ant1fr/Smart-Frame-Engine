from __future__ import annotations

import pytest

from app.processor.fit import FocusRegion
from app.providers.immich.faces import FACE_PADDING, face_regions
from app.providers.immich.models import FaceDto, PersonDto


def face(
    person: str | None,
    box: tuple[float, float, float, float],
    frame: tuple[int, int] = (1000, 500),
) -> FaceDto:
    return FaceDto(
        imageWidth=frame[0],
        imageHeight=frame[1],
        boundingBoxX1=box[0],
        boundingBoxY1=box[1],
        boundingBoxX2=box[2],
        boundingBoxY2=box[3],
        person=PersonDto(id=person) if person else None,
    )


def test_boxes_are_normalized_by_their_own_frame_and_padded() -> None:
    [region] = face_regions([face("a", (400, 100, 600, 300))], image_size=(1000, 500), padding=0.0)
    assert (region.x, region.y, region.width, region.height) == pytest.approx((0.4, 0.2, 0.2, 0.4))

    [padded] = face_regions([face("a", (400, 100, 600, 300))], image_size=(1000, 500))
    assert padded.width == pytest.approx(0.2 * (1 + 2 * FACE_PADDING))
    assert padded.height == pytest.approx(0.4 * (1 + 2 * FACE_PADDING))


def test_box_frame_may_differ_from_the_image_size() -> None:
    # Immich scales boxes to a ~1440px preview frame; the original has other pixels.
    [region] = face_regions(
        [face("a", (720, 480, 1080, 960), frame=(1440, 1920))],
        image_size=(3024, 4032),
        padding=0.0,
    )
    assert (region.x, region.y) == pytest.approx((0.5, 0.25))
    assert (region.width, region.height) == pytest.approx((0.25, 0.25))


def test_target_people_win_and_strangers_are_ignored() -> None:
    faces = [
        face("stranger", (0, 0, 300, 300)),  # biggest face, but not requested
        face("alice", (500, 100, 600, 200)),
        face(None, (700, 100, 800, 200)),
    ]
    regions = face_regions(faces, image_size=(1000, 500), target_person_ids={"alice"}, padding=0)
    assert len(regions) == 1 and regions[0].x == pytest.approx(0.5)


def test_all_faces_are_the_fallback_when_no_target_matches_and_sorted_by_size() -> None:
    faces = [
        face("a", (0, 0, 50, 50)),
        face(None, (200, 0, 400, 200)),
        face("b", (600, 0, 650, 50)),
    ]
    regions = face_regions(faces, image_size=(1000, 500), target_person_ids={"zzz"}, padding=0)
    areas = [r.area for r in regions]
    assert areas == sorted(areas, reverse=True) and len(regions) == 3
    assert face_regions(faces, image_size=(1000, 500), padding=0)[0].x == pytest.approx(0.2)


def test_degenerate_and_out_of_range_boxes() -> None:
    faces = [
        face("a", (10, 10, 10, 50)),  # zero width
        FaceDto(  # unusable frame
            imageWidth=0,
            imageHeight=0,
            boundingBoxX1=0,
            boundingBoxY1=0,
            boundingBoxX2=1,
            boundingBoxY2=1,
        ),
        face("b", (-50, -50, 100, 100)),  # sticks out of the frame: clamped
        face("c", (300, 300, 100, 100)),  # reversed corners
    ]
    regions = face_regions(faces, image_size=(1000, 500), padding=0)
    assert len(regions) == 2
    assert all(isinstance(r, FocusRegion) for r in regions)
    assert min(r.x for r in regions) == 0.0


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [
        # Raw sensor frame 4000x3000 (landscape), displayed 3000x4000 (portrait). The raw box
        # x 3000-3400, y 600-1000 is u 0.75-0.85, v 0.2-0.333 in normalized coordinates.
        ("6", (1 - 1 / 3, 0.75, 1 / 3 - 0.2, 0.1)),  # (u, v) -> (1 - v, u): clockwise
        ("8", (0.2, 0.15, 1 / 3 - 0.2, 0.1)),  # (u, v) -> (v, 1 - u): counter-clockwise
    ],
)
def test_orientation_guard_rotates_raw_space_boxes(
    orientation: str, expected: tuple[float, float, float, float]
) -> None:
    raw = face("a", (3000, 600, 3400, 1000), frame=(4000, 3000))
    [region] = face_regions([raw], image_size=(3000, 4000), exif_orientation=orientation, padding=0)
    assert (region.x, region.y, region.width, region.height) == pytest.approx(expected, abs=1e-6)


def test_orientation_guard_stays_off_for_already_corrected_boxes() -> None:
    # Verified live (Immich 3.3.5): orientation 6 asset, boxes already in the portrait frame.
    box = face("a", (731, 460, 853, 596), frame=(1440, 1920))
    plain = face_regions([box], image_size=(3024, 4032), exif_orientation=None, padding=0)
    guarded = face_regions([box], image_size=(3024, 4032), exif_orientation="6", padding=0)
    assert plain == guarded
    # Orientations 1-4 never change dimensions, so the guard cannot (and does not) act.
    for orientation in ("1", "3", "0", "bogus", None, 9):
        assert (
            face_regions([box], image_size=(3024, 4032), exif_orientation=orientation, padding=0)
            == plain
        )


def test_orientation_five_and_seven_transform_corners_exactly() -> None:
    raw = face("a", (0, 0, 1000, 500), frame=(4000, 3000))  # top-left quarter-ish box
    five = face_regions([raw], image_size=(3000, 4000), exif_orientation=5, padding=0)[0]
    seven = face_regions([raw], image_size=(3000, 4000), exif_orientation="7", padding=0)[0]
    assert (five.x, five.y) == pytest.approx((0.0, 0.0))
    assert (five.width, five.height) == pytest.approx((500 / 3000, 1000 / 4000))
    assert (seven.x + seven.width, seven.y + seven.height) == pytest.approx((1.0, 1.0))
