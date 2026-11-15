from __future__ import annotations

import pytest

from app.core.text import fold
from app.providers.tmdb.models import SearchResult, TmdbImage, TmdbRequest
from app.providers.tmdb.ranking import parse_language, pick_result, poster_tier, rank_posters


def poster(
    name: str,
    lang: str | None,
    region: str | None = None,
    votes: float = 5.0,
    count: int = 10,
) -> TmdbImage:
    return TmdbImage(
        file_path=f"/{name}.jpg",
        iso_639_1=lang,
        iso_3166_1=region,
        vote_average=votes,
        vote_count=count,
    )


def names(images: list[TmdbImage]) -> list[str]:
    return [image.file_path.strip("/").removesuffix(".jpg") for image in images]


@pytest.mark.parametrize(
    ("language", "expected"),
    [("fr-FR", ("fr", "FR")), ("en-US", ("en", "US")), ("fr", ("fr", None))],
)
def test_parse_language(language: str, expected: tuple[str, str | None]) -> None:
    assert parse_language(language) == expected


def test_posters_follow_the_exact_regional_priority() -> None:
    images = [
        poster("german", "de", "DE", votes=9.9),
        poster("english", "en", "US", votes=9.0),
        poster("textless", None, votes=8.0),
        poster("quebec", "fr", "CA", votes=7.0),
        poster("france", "fr", "FR", votes=1.0),
        poster("french_any", "fr", None, votes=7.5),
    ]
    ranked = rank_posters(images, "fr", "FR")
    assert names(ranked) == ["france", "french_any", "quebec", "textless", "english", "german"]
    assert [poster_tier(image, "fr", "FR") for image in ranked] == [0, 1, 1, 2, 3, 4]


def test_ties_within_a_tier_use_rating_then_vote_count() -> None:
    images = [
        poster("low", "fr", "FR", votes=5.1, count=100),
        poster("high", "fr", "FR", votes=5.6, count=2),
        poster("high_more_votes", "fr", "FR", votes=5.6, count=40),
    ]
    assert names(rank_posters(images, "fr", "FR")) == ["high_more_votes", "high", "low"]


def test_missing_region_data_degrades_to_language_ranking() -> None:
    images = [poster("en", "en"), poster("fr", "fr"), poster("none", None)]
    assert names(rank_posters(images, "fr", "FR")) == ["fr", "none", "en"]
    # Without a requested region, every French poster shares the first tier.
    assert poster_tier(poster("fr_ca", "fr", "CA"), "fr", None) == 1


def test_english_request_still_prefers_its_region() -> None:
    images = [poster("gb", "en", "GB", votes=9.0), poster("us", "en", "US", votes=1.0)]
    assert names(rank_posters(images, "en", "US")) == ["us", "gb"]


def test_fold_ignores_accents_case_and_punctuation() -> None:
    assert fold("Amélie") == fold("AMELIE") == "amelie"
    assert fold("Le Fabuleux Destin d'Amélie Poulain") == "le fabuleux destin d amelie poulain"


def result(
    tmdb_id: int, title: str, date: str | None = None, original: str | None = None
) -> SearchResult:
    return SearchResult(
        id=tmdb_id, media_type="movie", title=title, original_title=original, release_date=date
    )


def test_pick_result_prefers_an_exact_title_and_the_requested_year() -> None:
    results = [
        result(1, "Dune: Part Two", "2024-02-27"),
        result(2, "Dune", "1984-12-14"),
        result(3, "Dune", "2021-09-15"),
    ]
    assert pick_result(results, "dune", 2021) == results[2]
    assert pick_result(results, "Dune", None) == results[1]
    assert pick_result(results, "Something else", None) == results[0]
    assert pick_result([], "Dune", None) is None


def test_pick_result_matches_the_original_title() -> None:
    results = [
        result(1, "Spirited Away (dub)"),
        result(2, "Le Voyage de Chihiro", original="千と千尋の神隠し"),
    ]
    assert pick_result(results, "千と千尋の神隠し", None) == results[1]


def test_search_result_year() -> None:
    assert result(1, "x", "2001-04-25").year == 2001
    assert result(1, "x", "").year is None
    assert result(1, "x", "garbage").year is None


def test_request_validation() -> None:
    assert TmdbRequest(query="  Amélie  ").query == "Amélie"
    assert TmdbRequest(query="x", media_type="auto").media_type == "multi"  # type: ignore[arg-type]
    assert TmdbRequest(query="x", language="fr-CA").language == "fr-CA"
    with pytest.raises(ValueError):
        TmdbRequest(query="x", media_type="movie", season=2)
    with pytest.raises(ValueError):
        TmdbRequest(query="x", language="french")
    with pytest.raises(ValueError):
        TmdbRequest(query="   ")


def test_fold_transliterates_ligatures() -> None:
    assert fold("Ágætis byrjun") == "agaetis byrjun"
    assert fold("Œuvre") == "oeuvre"
    assert fold("Straße") == "strasse"
    assert fold("Søren") == "soren"
