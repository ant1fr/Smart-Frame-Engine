"""How an Immich rule is presented in the Smart Mix editor (see ``app/services/rule_catalog.py``).

The keys are fields of ``ImmichDisplayRequest``; ``tests/test_rule_catalog.py`` keeps them in sync.
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


def _mode_is(*modes: str) -> Condition:
    return Condition(field="mode", any_of=list(modes))


def immich_rule_spec() -> RuleKindSpec:
    """Descriptor of the ``immich`` source kind."""
    return RuleKindSpec(
        kind="immich",
        label="Immich photos",
        description="Photos from your Immich libraries, filtered by people, album or theme.",
        icon="🖼️",
        service="immich",
        fields=[
            FieldSpec(
                key="mode",
                label="Pick photos from",
                widget="segmented",
                default="random",
                options=[
                    Option(value="random", label="Anywhere", help="Any photo matching the filters"),
                    Option(value="favorites", label="Favorites", help="Photos you starred"),
                    Option(value="album", label="An album"),
                    Option(value="search", label="A theme", help="Describe it, e.g. a beach"),
                ],
            ),
            FieldSpec(
                key="album_name",
                label="Album",
                widget="album",
                help="Photos are picked from this album.",
                show_when=[_mode_is("album")],
                required_when=[_mode_is("album")],
            ),
            FieldSpec(
                key="clip_queries",
                label="Themes",
                widget="text_list",
                help="Describe what you want to see; a photo matching any phrase can be picked. "
                "Placeholders such as {season} follow the calendar.",
                placeholder="e.g. {season} landscape",
                show_when=[_mode_is("search")],
                required_when=[_mode_is("search")],
                max=10,
            ),
            FieldSpec(
                key="persons",
                label="People",
                widget="person_list",
                help="Only photos with these people. Leave empty to use the people chosen in "
                "Settings > Immich.",
            ),
            FieldSpec(
                key="match_mode",
                label="Photos must show",
                widget="segmented",
                nullable=True,
                options=[
                    Option(value="any", label="Any of them"),
                    Option(value="all", label="All of them"),
                ],
                show_when=[Condition(field="persons", filled=True)],
            ),
            FieldSpec(
                key="on_this_day",
                label="On this day",
                widget="on_this_day",
                help="Memories: photos taken around today's date in previous years.",
                show_when=[
                    Condition(field="taken_after", filled=False),
                    Condition(field="taken_before", filled=False),
                ],
            ),
            FieldSpec(
                key="taken_after",
                label="Taken after",
                widget="date",
                time="00:00:00",
                group="advanced",
                show_when=[Condition(field="on_this_day", filled=False)],
            ),
            FieldSpec(
                key="taken_before",
                label="Taken before",
                widget="date",
                time="23:59:59",
                group="advanced",
                show_when=[Condition(field="on_this_day", filled=False)],
            ),
            FieldSpec(
                key="accounts",
                label="Immich accounts",
                widget="account_list",
                help="Limit the selection to these accounts. Leave empty for all of them.",
                group="advanced",
            ),
            FieldSpec(
                key="always_include",
                label="Always include",
                widget="person_list",
                help="People added to every selection. Leave empty to use Settings > Immich.",
                group="advanced",
            ),
            FieldSpec(
                key="whole_library",
                label="Ignore the default filters",
                widget="bool",
                help="Do not apply the people, album and favorites chosen in Settings > Immich.",
                group="advanced",
            ),
            FieldSpec(
                key="fit",
                label="Framing",
                widget="segmented",
                nullable=True,
                group="advanced",
                options=[
                    Option(value="smart_crop", label="Smart crop", help="Fill the frame"),
                    Option(value="contain", label="Whole photo", help="Never cropped"),
                ],
            ),
        ],
        presets=[
            Preset(
                id="favorites",
                label="Favorites",
                description="Photos you starred in Immich.",
                icon="⭐",
                params={"mode": "favorites"},
            ),
            Preset(
                id="on-this-day",
                label="On this day",
                description="Photos taken around today's date in previous years.",
                icon="📅",
                params={"on_this_day": {}},
                weight=2.0,
            ),
            Preset(
                id="person",
                label="A person",
                description="Photos of one or more people.",
                icon="🧑",
                params={"mode": "random", "persons": []},
                edit_first=True,
            ),
            Preset(
                id="album",
                label="An album",
                description="Photos from one album.",
                icon="📚",
                params={"mode": "album"},
                edit_first=True,
            ),
            Preset(
                id="theme",
                label="A theme",
                description="Photos matching a description, such as a beach or snow.",
                icon="🔎",
                params={"mode": "search", "clip_queries": ["beach vacation"]},
                edit_first=True,
            ),
            Preset(
                id="season",
                label="Photos of the season",
                description="Landscapes that follow the calendar: spring, summer, autumn, winter.",
                icon="🍂",
                params={"mode": "search", "clip_queries": ["{season} landscape"]},
            ),
            Preset(
                id="whole-library",
                label="Whole library",
                description="Any photo, ignoring the default people, album and favorites filters.",
                icon="🌍",
                params={"mode": "random", "whole_library": True},
            ),
        ],
        summary=[
            SummaryPart(template="Favorites", when=[_mode_is("favorites")]),
            SummaryPart(template="Album: {value}", field="album_name", when=[_mode_is("album")]),
            SummaryPart(template="Theme: {value}", field="clip_queries", when=[_mode_is("search")]),
            SummaryPart(template="People: {value}", field="persons"),
            SummaryPart(template="On this day", field="on_this_day"),
            SummaryPart(template="Whole library", field="whole_library"),
        ],
    )
