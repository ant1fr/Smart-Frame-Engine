"""How a TMDB rule is presented in the Smart Mix editor (see ``app/services/rule_catalog.py``).

The keys are fields of ``TmdbRequest``; ``tests/test_rule_catalog.py`` keeps them in sync. Only
the lookup is stored in a rule: the scheduler never replays the fallback picture or the
ephemeral settings of a direct push.
"""

from __future__ import annotations

from app.services.rule_catalog import (
    Condition,
    FieldSpec,
    Option,
    Preset,
    RuleKindSpec,
    SummaryPart,
)


def tmdb_rule_spec() -> RuleKindSpec:
    """Descriptor of the ``tmdb`` source kind."""
    return RuleKindSpec(
        kind="tmdb",
        label="Movie & TV posters",
        description="The HD poster of a movie or series from TMDB, shown whole (never cropped).",
        icon="🎬",
        service="tmdb",
        fields=[
            FieldSpec(
                key="query",
                label="Title",
                widget="text",
                required=True,
                placeholder="e.g. Blade Runner",
            ),
            FieldSpec(
                key="media_type",
                label="Look for",
                widget="segmented",
                default="multi",
                options=[
                    Option(value="multi", label="Movie or series"),
                    Option(value="movie", label="Movie"),
                    Option(value="tv", label="TV series"),
                ],
            ),
            FieldSpec(
                key="year",
                label="Year",
                widget="int",
                help="Release year, to tell remakes apart. Optional.",
                min=1870,
                max=2200,
            ),
            FieldSpec(
                key="season",
                label="Season",
                widget="int",
                help="Prefer the poster of this season. Optional.",
                min=0,
                max=100,
                show_when=[Condition(field="media_type", any_of=["tv", "multi", None])],
            ),
            FieldSpec(
                key="language",
                label="Poster language",
                widget="text",
                help="Overrides the language set in Settings > TMDB, e.g. fr-FR or en-US.",
                placeholder="Default",
                group="advanced",
            ),
        ],
        presets=[
            Preset(
                id="movie",
                label="A movie poster",
                description="The poster of one movie.",
                icon="🎞️",
                params={"query": "Blade Runner", "media_type": "movie"},
                edit_first=True,
            ),
            Preset(
                id="series",
                label="A series poster",
                description="The poster of a series, optionally of one season.",
                icon="📺",
                params={"query": "Severance", "media_type": "tv", "season": 1},
                edit_first=True,
            ),
        ],
        summary=[
            SummaryPart(template="{value}", field="query"),
            SummaryPart(template="{value}", field="year"),
            SummaryPart(template="Season {value}", field="season"),
        ],
        summary_fallback="Poster (no title yet)",
    )
