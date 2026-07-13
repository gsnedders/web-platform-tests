"""A small, round-tripping parser for pip-style requirements files.

This covers the subset of the format used across this repository: blank
lines, whole-line comments (including the indented ``# via ...``
annotations that ``uv pip compile`` emits), requirement specifiers (PEP
508, via :mod:`packaging`) with an optional trailing inline comment, and
everything else (``-r``/``-c`` includes, ``--only-binary`` and similar
options) as opaque directive lines.

pip itself treats "requirements files" and "constraints files" as two
formats with slightly different supported syntax, but nothing here cares
about that distinction: a constraints file is parsed the same way as any
other requirements file.

Parsing and then converting back to a string reproduces the input
byte-for-byte, since every line variant keeps its original text verbatim
and only :class:`RequirementLine` exposes a way to change anything.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Sequence, Union

from packaging.requirements import InvalidRequirement, Requirement


@dataclass(frozen=True)
class BlankLine:
    text: str

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class CommentLine:
    text: str

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class DirectiveLine:
    """Anything that isn't blank, a comment, or a plain requirement spec."""

    text: str

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class RequirementLine:
    requirement: Requirement
    spec_text: str  # the requirement part of the line, verbatim
    comment: str  # "" or the trailing inline comment, including its leading whitespace

    @property
    def marker_text(self) -> Optional[str]:
        """The marker clause exactly as written, starting at ";", or None if there isn't one."""
        index = self.spec_text.find(";")
        return None if index == -1 else self.spec_text[index:]

    def with_version(self, version: str) -> RequirementLine:
        """Return a copy of this line with its pinned version replaced.

        Only the version substring changes; everything else about the
        line (name casing, extras, marker, comment) is kept exactly as
        written.
        """
        return self.with_pin(version, self.marker_text)

    def relaxed(self) -> RequirementLine:
        """Return a copy of this line with a single '==' pin loosened to '>='.

        This gives a resolver like `uv pip compile` room to pick something
        newer than the current pin. Lines that aren't pinned with a single
        '==' are returned unchanged.
        """
        specs = list(self.requirement.specifier)
        if len(specs) != 1 or specs[0].operator != "==":
            return self

        old_pin = f"=={specs[0].version}"
        new_pin = f">={specs[0].version}"
        index = self.spec_text.find(old_pin)
        if index == -1:
            return self

        new_spec_text = self.spec_text[:index] + new_pin + self.spec_text[index + len(old_pin):]
        return replace(self, requirement=Requirement(new_spec_text), spec_text=new_spec_text)

    def with_pin(self, version: str, marker_text: Optional[str]) -> RequirementLine:
        """Return a copy of this line pinned to `version` under `marker_text`.

        `marker_text` replaces the marker clause (starting at ";"), or
        removes it if None. If it's unchanged from this line's own
        `marker_text`, the original marker text - including its exact
        surrounding whitespace - is kept rather than being reformatted.
        """
        specs = list(self.requirement.specifier)
        if len(specs) != 1 or specs[0].operator != "==":
            raise ValueError(f"{self.requirement.name} isn't pinned with a single '==' specifier")

        semi_index = self.spec_text.find(";")
        before, suffix = (
            (self.spec_text, "") if semi_index == -1 else (self.spec_text[:semi_index], self.spec_text[semi_index:])
        )

        old_pin = f"=={specs[0].version}"
        new_pin = f"=={version}"
        pin_index = before.find(old_pin)
        if pin_index == -1:
            raise ValueError(f"couldn't find {old_pin!r} in {before!r}")
        new_before = before[:pin_index] + new_pin + before[pin_index + len(old_pin):]

        if marker_text == self.marker_text:
            new_spec_text = new_before + suffix
        elif marker_text is None:
            new_spec_text = new_before.rstrip()
        else:
            new_spec_text = f"{new_before.rstrip()} {marker_text}"

        return replace(self, requirement=Requirement(new_spec_text), spec_text=new_spec_text)

    def __str__(self) -> str:
        return self.spec_text + self.comment


Entry = Union[BlankLine, CommentLine, DirectiveLine, RequirementLine]


def parse_line(line: str) -> Entry:
    if not line.strip():
        return BlankLine(line)

    hash_index = line.find("#")
    spec_text, comment = (line, "") if hash_index == -1 else (line[:hash_index], line[hash_index:])

    if not spec_text.strip():
        return CommentLine(line)

    try:
        requirement = Requirement(spec_text)
    except InvalidRequirement:
        return DirectiveLine(line)

    return RequirementLine(requirement, spec_text, comment)


@dataclass(frozen=True)
class RequirementsFile:
    entries: Sequence[Entry]
    trailing_newline: bool

    @classmethod
    def parse(cls, text: str) -> RequirementsFile:
        return cls([parse_line(line) for line in text.splitlines()], text.endswith("\n"))

    def __str__(self) -> str:
        if not self.entries:
            return ""
        body = "\n".join(str(entry) for entry in self.entries)
        return body + "\n" if self.trailing_newline else body
