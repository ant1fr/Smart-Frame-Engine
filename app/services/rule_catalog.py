"""Describes the rules each source kind accepts, so a UI can offer a form instead of raw JSON.

A Smart Mix entry (or a pinned rule) is ``{kind, params}``, where ``params`` is the body of the
kind's display endpoint. The request models (``ImmichDisplayRequest``, ``TmdbDisplayRequest``)
stay the single source of truth for *validation*; the descriptors here only carry what a form
needs to *present* those parameters: a label, a help text, a widget, the allowed values, the
conditions under which a field matters, ready-made presets and a one-line summary recipe.

Each provider package owns its descriptor (``app/providers/<name>/rule_spec.py``) and
``default_catalog()`` registers them. A kind that has a runner but no descriptor still works: the
UI falls back to a raw JSON editor for it. ``tests/test_rule_catalog.py`` checks every descriptor
against its request model so the two cannot drift apart.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

Widget = Literal[
    "segmented",  # a few mutually exclusive choices (``options``)
    "text",  # one line of text
    "text_list",  # several phrases, entered as chips
    "int",  # a whole number (``min`` / ``max``)
    "bool",  # a switch
    "date",  # a calendar day, stored as an ISO 8601 UTC timestamp (see ``FieldSpec.time``)
    "person_list",  # people picked from the Immich libraries
    "album",  # one album picked from the Immich libraries (stored by name)
    "account_list",  # Immich account names
    "on_this_day",  # the ``OnThisDay`` object (window / years back), switchable off
]

Group = Literal["basic", "advanced"]

#: Placeholders expanded in a stored rule right before it runs (``app/core/context.py``).
PLACEHOLDERS: Final[tuple[tuple[str, str], ...]] = (
    ("season", "spring, summer, autumn or winter (follows the configured hemisphere)"),
    ("month", "the month number, 01 to 12"),
    ("month_name", "the month name in lowercase, e.g. march"),
    ("year", "the current year"),
)


class Option(BaseModel):
    """One choice of a ``segmented`` field."""

    value: str
    label: str
    help: str | None = None


class Condition(BaseModel):
    """A test on one field of the rule being edited.

    ``any_of`` holds when the field equals one of the values (an unset field equals ``null``);
    ``filled`` holds when the field is set (``true``) or not (``false``): ``null``, ``""``,
    ``false`` and an empty list count as not set, while an empty object counts as set. Both
    given means both must hold.
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    any_of: list[JsonValue] | None = None
    filled: bool | None = None


class FieldSpec(BaseModel):
    """One parameter of a rule, as a form presents it."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="Key in the rule's params (a field of the request model)")
    label: str
    widget: Widget
    help: str | None = None
    group: Group = Field(default="basic", description="advanced fields start collapsed")
    options: list[Option] = Field(default_factory=list, description="Choices of a segmented field")
    nullable: bool = Field(
        default=False, description="segmented only: offer a 'Default' choice that unsets the key"
    )
    placeholder: str | None = None
    default: JsonValue = Field(default=None, description="Value used when the key is unset")
    required: bool = False
    show_when: list[Condition] = Field(
        default_factory=list, description="The field is shown only when all conditions hold"
    )
    required_when: list[Condition] = Field(
        default_factory=list, description="The field is required when all conditions hold"
    )
    min: int | None = None
    max: int | None = None
    time: str | None = Field(
        default=None,
        description="date only: UTC time of day appended to the chosen day (HH:MM:SS)",
    )


class SummaryPart(BaseModel):
    """One piece of the one-line description of a rule, e.g. ``Album: Holidays``.

    The piece is included when ``field`` is filled (if given) and every ``when`` condition holds.
    ``template`` may use ``{value}``: lists are joined with ``, ``.
    """

    model_config = ConfigDict(extra="forbid")

    template: str
    field: str | None = None
    when: list[Condition] = Field(default_factory=list)


class Preset(BaseModel):
    """A ready-made rule offered when adding an entry."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    description: str
    icon: str = Field(description="One emoji shown on the preset tile")
    params: dict[str, JsonValue]
    weight: float = Field(default=1.0, gt=0, le=1000, description="Suggested weight")
    edit_first: bool = Field(
        default=False, description="The rule needs a choice from the user: open the editor"
    )


class RuleKindSpec(BaseModel):
    """Everything a UI needs to edit the rules of one source kind."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    label: str
    description: str
    icon: str
    service: Literal["immich", "tmdb"] | None = Field(
        default=None, description="Service that must be configured for the rule to run"
    )
    fields: list[FieldSpec]
    presets: list[Preset]
    summary: list[SummaryPart] = Field(
        description="Recipe of the one-line description of a rule, parts joined with ' · '"
    )
    summary_fallback: str | None = Field(
        default=None,
        description="Description of a rule none of the summary parts applies to (default: label)",
    )

    def field_keys(self) -> set[str]:
        """Every params key this descriptor knows about."""
        return {spec.key for spec in self.fields}


class Placeholder(BaseModel):
    """A ``{name}`` usable in the text of a rule."""

    name: str
    description: str


class RuleCatalog:
    """Registry of the descriptors, keyed by source kind."""

    def __init__(self, specs: list[RuleKindSpec] | None = None) -> None:
        self._specs: dict[str, RuleKindSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: RuleKindSpec) -> None:
        """Add (or replace) the descriptor of one kind."""
        self._specs[spec.kind] = spec

    def get(self, kind: str) -> RuleKindSpec | None:
        """The descriptor of ``kind``, or ``None`` when it has none."""
        return self._specs.get(kind)

    def all(self) -> list[RuleKindSpec]:
        """Every descriptor, ordered by kind."""
        return [self._specs[kind] for kind in sorted(self._specs)]


def placeholders() -> list[Placeholder]:
    """The placeholders a rule's text may contain."""
    return [Placeholder(name=name, description=text) for name, text in PLACEHOLDERS]


def default_catalog() -> RuleCatalog:
    """The descriptors of the built-in providers."""
    # Imported here: provider packages import the models of this module.
    from app.providers.immich.rule_spec import immich_rule_spec
    from app.providers.tmdb.rule_spec import tmdb_rule_spec

    return RuleCatalog([immich_rule_spec(), tmdb_rule_spec()])
