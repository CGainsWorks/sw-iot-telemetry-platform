"""Schema version policy for device messages.

A fleet cannot be upgraded atomically. Devices sit behind flaky links, some are
asleep for days and a few never take the update at all, so at any moment the
broker carries several schema versions at once. The policy below is what lets
the platform accept that instead of fighting it.

Policy
------
Versions are semantic, ``MAJOR.MINOR.PATCH``, carried in every payload as
``schema_version``.

* **PATCH** documentation, or a relaxed constraint. Always accepted.
* **MINOR** additive only: new optional fields, new metric names. A consumer
  written for ``1.0`` must keep working against ``1.4`` by ignoring what it does
  not recognise.
* **MAJOR** anything else: a removed field, a renamed field, a changed type or a
  changed unit. A consumer must refuse a major it was not built for.

A consumer therefore accepts a message when the major matches one it supports
and the minor is anything at all, older or newer. Newer minors are safe because
they are additive by definition; older minors are safe because every field a
newer consumer relies on either existed at ``x.0`` or is optional.

Producers are the mirror image: a device stamps the version it was built
against and never guesses. Rejected messages go to the dead-letter topic with
the reason attached, so a botched firmware rollout is visible in a dashboard
rather than as silent data loss.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

#: The version this build of the platform produces.
CURRENT_SCHEMA_VERSION = "1.1.0"

#: Majors this build of the platform is able to consume.
SUPPORTED_MAJORS: frozenset[int] = frozenset({1})


class SchemaVersionError(ValueError):
    """Raised when a version string is malformed or unsupported."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, raw: str) -> Version:
        match = _SEMVER_RE.match(raw.strip()) if isinstance(raw, str) else None
        if match is None:
            raise SchemaVersionError(f"not a MAJOR.MINOR.PATCH version: {raw!r}")
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.major}.{self.minor}.{self.patch}"


CURRENT_VERSION = Version.parse(CURRENT_SCHEMA_VERSION)


def is_supported(raw: str) -> bool:
    """True if this build can consume a payload stamped ``raw``."""
    try:
        return Version.parse(raw).major in SUPPORTED_MAJORS
    except SchemaVersionError:
        return False


def require_supported(raw: str) -> Version:
    """Parse ``raw`` and raise unless this build can consume it."""
    version = Version.parse(raw)
    if version.major not in SUPPORTED_MAJORS:
        supported = ", ".join(str(m) for m in sorted(SUPPORTED_MAJORS))
        raise SchemaVersionError(
            f"schema major {version.major} is not supported by this build "
            f"(supported majors: {supported})"
        )
    return version


def is_newer_minor(raw: str) -> bool:
    """True if ``raw`` is the same major as this build but a later minor.

    Worth counting as a metric: it means firmware in the field is ahead of the
    platform, and some fields are being dropped on the floor.
    """
    try:
        version = Version.parse(raw)
    except SchemaVersionError:
        return False
    return version.major == CURRENT_VERSION.major and version.minor > CURRENT_VERSION.minor
