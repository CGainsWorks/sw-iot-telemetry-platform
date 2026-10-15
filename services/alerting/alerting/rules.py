"""The alert rule model.

Rules are YAML so they are reviewable in a pull request, and validated into
these models at load time so a typo is a startup failure rather than a rule that
silently never fires. Silently-never-fires is the worst outcome available to an
alerting system: it looks exactly like everything being fine.

Six kinds cover the catalogue. Each maps onto a question the platform can answer
from data it already stores, which is why there is no general expression
language here. An expression language would be more flexible and would also mean
every rule needs its own query planner and its own set of failure modes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class RuleKind(str, Enum):
    THRESHOLD = "threshold"
    FLATLINE = "flatline"
    RATE_OF_CHANGE = "rate_of_change"
    OFFLINE = "offline"
    DEAD_LETTER_RATE = "dead_letter_rate"
    CERTIFICATE_EXPIRY = "certificate_expiry"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Aggregation(str, Enum):
    """Which column of the rollup a rule reads.

    A closed set, because the value is interpolated into SQL. Nothing a rule
    author writes reaches the query except through one of these names.
    """

    AVG = "avg"
    MIN = "min"
    MAX = "max"
    LAST = "last"

    @property
    def column(self) -> str:
        return {
            Aggregation.AVG: "avg_value",
            Aggregation.MIN: "min_value",
            Aggregation.MAX: "max_value",
            Aggregation.LAST: "last_value",
        }[self]


#: Allowed comparison operators. Also a closed set for the same reason.
OPERATORS: dict[str, str] = {
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
}

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h|d)$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


class RuleError(ValueError):
    """Raised when a rule definition is malformed."""


def parse_duration(raw: str | int | float) -> float:
    """Parse ``30s``, ``5m``, ``2h`` or ``1d`` into seconds."""
    if isinstance(raw, int | float):
        return float(raw)
    match = _DURATION_RE.match(str(raw).strip())
    if match is None:
        raise RuleError(f"not a duration: {raw!r}. Use forms like 30s, 5m, 2h, 1d")
    value, unit = float(match.group(1)), match.group(2)
    return value * {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[unit]


@dataclass(frozen=True)
class Scope:
    """Which devices a rule applies to. ``None`` means all of them."""

    site_id: str | None = None
    device_id: str | None = None
    profile: str | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> Scope:
        if not raw:
            return cls()

        def clean(key: str) -> str | None:
            value = raw.get(key)
            if value in (None, "", "*", "all"):
                return None
            if not isinstance(value, str):
                raise RuleError(f"scope.{key} must be a string, got {type(value).__name__}")
            return value

        return cls(site_id=clean("site_id"), device_id=clean("device_id"), profile=clean("profile"))

    def as_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("site_id", self.site_id),
                ("device_id", self.device_id),
                ("profile", self.profile),
            )
            if value is not None
        }


@dataclass(frozen=True)
class Rule:
    rule_id: str
    name: str
    kind: RuleKind
    severity: Severity
    description: str = ""
    enabled: bool = True

    metric: str | None = None
    aggregation: Aggregation = Aggregation.AVG
    operator: str = ">"
    threshold: float = 0.0

    #: How far back the rule looks.
    window_s: float = 300.0
    #: How long the condition must hold before the alert fires. Zero fires on the
    #: first breach. Anything above zero is what separates an alert from a
    #: notification about a single noisy sample.
    for_s: float = 0.0
    #: How long the condition must stop holding before the alert resolves.
    #: Non-zero by default so a value oscillating around the threshold does not
    #: produce a stream of fire and resolve pairs.
    recovery_s: float = 120.0

    #: For rate_of_change only: the minimum relative spread, (max - min) / max,
    #: that each half of the window must show before its floor is believed.
    #:
    #: Exists because a duty-cycled machine swings between two levels, and a
    #: half-window that happened to sit entirely inside a running stretch has a
    #: minimum at the running level rather than at the idle floor. Requiring both
    #: halves to have seen the signal vary proves each one observed both states.
    #: Zero disables the check, which is right for a signal that is not duty
    #: cycled.
    spread_ratio: float = 0.0

    scope: Scope = field(default_factory=Scope)
    channels: tuple[str, ...] = ("log",)
    #: Minimum seconds between repeat notifications while an alert stays firing.
    renotify_s: float = 3600.0

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.rule_id):
            raise RuleError(
                f"rule id {self.rule_id!r} must be lower-case letters, digits and hyphens"
            )
        if self.operator not in OPERATORS:
            raise RuleError(
                f"rule {self.rule_id}: operator {self.operator!r} is not one of "
                f"{', '.join(OPERATORS)}"
            )
        metric_kinds = (RuleKind.THRESHOLD, RuleKind.FLATLINE, RuleKind.RATE_OF_CHANGE)
        if self.kind in metric_kinds and not self.metric:
            raise RuleError(f"rule {self.rule_id}: kind {self.kind.value} requires a metric")
        if self.for_s > self.window_s:
            raise RuleError(
                f"rule {self.rule_id}: 'for' ({self.for_s}s) cannot exceed "
                f"'window' ({self.window_s}s), the rule could never fire"
            )
        if not self.channels:
            raise RuleError(f"rule {self.rule_id}: at least one channel is required")
        if not 0.0 <= self.spread_ratio < 1.0:
            raise RuleError(
                f"rule {self.rule_id}: spread_ratio must be at least 0 and below 1, "
                f"got {self.spread_ratio}. A ratio of 1 or more can never be met, "
                "so the rule would silently never fire."
            )

    @property
    def min_buckets(self) -> int:
        """One-minute buckets that must all breach before the rule fires."""
        if self.for_s <= 0:
            return 1
        return max(1, int(self.for_s // 60) + (1 if self.for_s % 60 else 0))

    def definition(self) -> dict[str, Any]:
        """JSON-serialisable form, stored in ``alert_rules.definition``."""
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "metric": self.metric,
            "aggregation": self.aggregation.value,
            "operator": self.operator,
            "threshold": self.threshold,
            "window_s": self.window_s,
            "for_s": self.for_s,
            "spread_ratio": self.spread_ratio,
            "recovery_s": self.recovery_s,
            "renotify_s": self.renotify_s,
            "scope": self.scope.as_dict(),
            "channels": list(self.channels),
        }

    def describe_condition(self) -> str:
        if self.kind is RuleKind.THRESHOLD:
            return (
                f"{self.aggregation.value}({self.metric}) {self.operator} {self.threshold} "
                f"for {int(self.for_s)}s"
            )
        if self.kind is RuleKind.FLATLINE:
            return f"{self.metric} varies by less than {self.threshold} over {int(self.window_s)}s"
        if self.kind is RuleKind.RATE_OF_CHANGE:
            return (
                f"{self.metric} changes by {self.operator} {self.threshold} per minute "
                f"over {int(self.window_s)}s"
            )
        if self.kind is RuleKind.OFFLINE:
            return f"no telemetry for {int(self.threshold)}s"
        if self.kind is RuleKind.DEAD_LETTER_RATE:
            return f"more than {self.threshold} rejected messages in {int(self.window_s)}s"
        if self.kind is RuleKind.CERTIFICATE_EXPIRY:
            return f"certificate expires within {int(self.threshold)} days"
        return "unknown condition"


def _rule_from_dict(raw: dict[str, Any]) -> Rule:
    try:
        kind = RuleKind(raw["kind"])
    except KeyError as exc:
        raise RuleError(f"rule {raw.get('id', '<no id>')!r} is missing 'kind'") from exc
    except ValueError as exc:
        valid = ", ".join(k.value for k in RuleKind)
        raise RuleError(
            f"rule {raw.get('id', '<no id>')!r}: unknown kind {raw['kind']!r}. Valid: {valid}"
        ) from exc

    try:
        severity = Severity(raw.get("severity", "warning"))
    except ValueError as exc:
        valid = ", ".join(s.value for s in Severity)
        raise RuleError(
            f"rule {raw.get('id')!r}: unknown severity {raw.get('severity')!r}. Valid: {valid}"
        ) from exc

    try:
        aggregation = Aggregation(raw.get("aggregation", "avg"))
    except ValueError as exc:
        valid = ", ".join(a.value for a in Aggregation)
        raise RuleError(
            f"rule {raw.get('id')!r}: unknown aggregation {raw.get('aggregation')!r}. "
            f"Valid: {valid}"
        ) from exc

    channels = raw.get("channels") or ["log"]
    if isinstance(channels, str):
        channels = [channels]

    for required in ("id", "name"):
        if required not in raw:
            raise RuleError(f"rule is missing {required!r}: {raw}")

    return Rule(
        rule_id=str(raw["id"]),
        name=str(raw["name"]),
        kind=kind,
        severity=severity,
        description=str(raw.get("description", "")),
        enabled=bool(raw.get("enabled", True)),
        metric=raw.get("metric"),
        aggregation=aggregation,
        operator=str(raw.get("operator", ">")),
        threshold=float(raw.get("threshold", 0.0)),
        window_s=parse_duration(raw.get("window", "5m")),
        for_s=parse_duration(raw.get("for", 0)),
        recovery_s=parse_duration(raw.get("recovery", "2m")),
        spread_ratio=float(raw.get("spread_ratio", 0.0)),
        scope=Scope.parse(raw.get("scope")),
        channels=tuple(str(c) for c in channels),
        renotify_s=parse_duration(raw.get("renotify", "1h")),
    )


def load_rules(path: str | Path) -> list[Rule]:
    """Load and validate the rule catalogue.

    Raises on the first bad rule rather than skipping it. A rule that fails to
    load is an alert that will never fire, and starting up anyway would hide that.
    """
    location = Path(path)
    if not location.exists():
        raise RuleError(f"alert rules file not found: {location}")

    document = yaml.safe_load(location.read_text(encoding="utf-8")) or {}
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list):
        raise RuleError(f"{location}: expected a top-level 'rules' list")

    rules = [_rule_from_dict(raw) for raw in raw_rules]

    seen: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen:
            raise RuleError(f"duplicate rule id {rule.rule_id!r}")
        seen.add(rule.rule_id)

    return rules
