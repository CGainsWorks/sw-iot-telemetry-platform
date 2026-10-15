"""Alert rule loading and validation.

A rule that fails to load is an alert that will never fire, and that failure
looks exactly like everything being fine. So loading is strict, and these tests
pin down what it refuses.

The last class checks the shipped catalogue itself, which is what stops a typo
in ``config/alert_rules.yaml`` reaching a deployment.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from alerting.rules import (
    OPERATORS,
    Aggregation,
    Rule,
    RuleError,
    RuleKind,
    Scope,
    Severity,
    load_rules,
    parse_duration,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGUE_PATH = REPO_ROOT / "config" / "alert_rules.yaml"


def write_rules(tmp_path: Path, rules: list[dict]) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({"rules": rules}), encoding="utf-8")
    return path


def minimal(**overrides) -> dict:
    base = {
        "id": "test-rule",
        "name": "Test rule",
        "kind": "threshold",
        "severity": "warning",
        "metric": "temperature_c",
        "operator": ">",
        "threshold": 30.0,
        "window": "5m",
        "for": "1m",
    }
    base.update(overrides)
    return base


class TestParseDuration:
    @pytest.mark.parametrize(
        ("raw", "seconds"),
        [("30s", 30.0), ("5m", 300.0), ("2h", 7200.0), ("1d", 86400.0), ("1.5h", 5400.0)],
    )
    def test_units(self, raw, seconds):
        assert parse_duration(raw) == seconds

    def test_bare_numbers_are_seconds(self):
        assert parse_duration(45) == 45.0

    @pytest.mark.parametrize("raw", ["", "5", "5 minutes", "m5", "-5m", "5w", "abc"])
    def test_malformed_durations_raise(self, raw):
        with pytest.raises(RuleError):
            parse_duration(raw)


class TestScope:
    def test_missing_scope_matches_everything(self):
        scope = Scope.parse(None)
        assert scope.site_id is None and scope.device_id is None

    @pytest.mark.parametrize("wildcard", ["*", "all", "", None])
    def test_wildcards_mean_no_restriction(self, wildcard):
        assert Scope.parse({"site_id": wildcard}).site_id is None

    def test_a_concrete_value_restricts(self):
        assert Scope.parse({"site_id": "site-b"}).site_id == "site-b"

    def test_a_non_string_scope_is_an_error(self):
        with pytest.raises(RuleError):
            Scope.parse({"site_id": 42})


class TestRuleValidation:
    def test_a_minimal_rule_loads(self, tmp_path):
        rules = load_rules(write_rules(tmp_path, [minimal()]))
        assert len(rules) == 1
        assert rules[0].kind is RuleKind.THRESHOLD

    def test_unknown_kind_names_the_valid_ones(self, tmp_path):
        with pytest.raises(RuleError, match="Valid:"):
            load_rules(write_rules(tmp_path, [minimal(kind="magic")]))

    def test_unknown_severity_is_refused(self, tmp_path):
        with pytest.raises(RuleError):
            load_rules(write_rules(tmp_path, [minimal(severity="apocalyptic")]))

    def test_unknown_operator_is_refused(self, tmp_path):
        # Operators are interpolated into SQL, so the whitelist is a security
        # boundary and not only a usability one.
        with pytest.raises(RuleError, match="operator"):
            load_rules(write_rules(tmp_path, [minimal(operator="; DROP TABLE alerts; --")]))

    def test_unknown_aggregation_is_refused(self, tmp_path):
        with pytest.raises(RuleError):
            load_rules(write_rules(tmp_path, [minimal(aggregation="median")]))

    def test_a_metric_kind_without_a_metric_is_refused(self, tmp_path):
        rule = minimal()
        del rule["metric"]
        with pytest.raises(RuleError, match="requires a metric"):
            load_rules(write_rules(tmp_path, [rule]))

    def test_for_longer_than_window_is_refused(self, tmp_path):
        # Such a rule can never fire, because the window never contains enough
        # buckets to satisfy the hold.
        with pytest.raises(RuleError, match="could never fire"):
            load_rules(write_rules(tmp_path, [minimal(window="1m", **{"for": "5m"})]))

    def test_duplicate_ids_are_refused(self, tmp_path):
        with pytest.raises(RuleError, match="duplicate"):
            load_rules(write_rules(tmp_path, [minimal(), minimal()]))

    @pytest.mark.parametrize("rule_id", ["Bad_ID", "x", "with space", "UPPER"])
    def test_bad_rule_ids_are_refused(self, tmp_path, rule_id):
        with pytest.raises(RuleError):
            load_rules(write_rules(tmp_path, [minimal(id=rule_id)]))

    def test_missing_name_is_refused(self, tmp_path):
        rule = minimal()
        del rule["name"]
        with pytest.raises(RuleError, match="name"):
            load_rules(write_rules(tmp_path, [rule]))

    def test_a_missing_file_is_an_error_not_an_empty_catalogue(self, tmp_path):
        with pytest.raises(RuleError, match="not found"):
            load_rules(tmp_path / "nope.yaml")

    def test_a_file_without_a_rules_list_is_an_error(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text("something_else: 1", encoding="utf-8")
        with pytest.raises(RuleError, match="rules"):
            load_rules(path)

    def test_a_single_channel_string_is_accepted(self, tmp_path):
        rules = load_rules(write_rules(tmp_path, [minimal(channels="log")]))
        assert rules[0].channels == ("log",)


class TestRuleBehaviour:
    def test_min_buckets_rounds_up(self):
        assert (
            Rule(
                rule_id="test-rule",
                name="R",
                kind=RuleKind.OFFLINE,
                severity=Severity.WARNING,
                for_s=0,
            ).min_buckets
            == 1
        )
        assert (
            Rule(
                rule_id="test-rule",
                name="R",
                kind=RuleKind.OFFLINE,
                severity=Severity.WARNING,
                window_s=600,
                for_s=90,
            ).min_buckets
            == 2
        )
        assert (
            Rule(
                rule_id="test-rule",
                name="R",
                kind=RuleKind.OFFLINE,
                severity=Severity.WARNING,
                window_s=600,
                for_s=120,
            ).min_buckets
            == 2
        )

    def test_aggregation_maps_to_a_real_column(self):
        columns = {agg.column for agg in Aggregation}
        assert columns == {"avg_value", "min_value", "max_value", "last_value"}

    def test_definition_is_json_safe(self, tmp_path):
        import json

        rule = load_rules(write_rules(tmp_path, [minimal()]))[0]
        assert json.loads(json.dumps(rule.definition()))["kind"] == "threshold"

    def test_every_kind_describes_itself(self, tmp_path):
        for kind in RuleKind:
            rule = Rule(
                rule_id="test-rule",
                name="R",
                kind=kind,
                severity=Severity.WARNING,
                metric="temperature_c",
            )
            assert rule.describe_condition()
            assert "unknown" not in rule.describe_condition()


class TestShippedCatalogue:
    """The catalogue that actually deploys."""

    @pytest.fixture(scope="class")
    def rules(self):
        return load_rules(CATALOGUE_PATH)

    def test_it_loads(self, rules):
        assert len(rules) >= 10

    def test_every_rule_has_a_description(self, rules):
        missing = [rule.rule_id for rule in rules if not rule.description.strip()]
        assert missing == [], f"rules without a description: {missing}"

    def test_every_metric_named_is_in_the_catalogue(self, rules):
        from telemetry_common.metrics import CATALOGUE as METRICS

        unknown = [
            (rule.rule_id, rule.metric)
            for rule in rules
            if rule.metric and rule.metric not in METRICS
        ]
        assert unknown == [], f"rules naming metrics that do not exist: {unknown}"

    def test_every_channel_named_is_implemented(self, rules):
        implemented = {"log", "webhook", "stream"}
        unknown = {
            (rule.rule_id, channel)
            for rule in rules
            for channel in rule.channels
            if channel not in implemented
        }
        assert unknown == set(), f"rules naming channels that do not exist: {unknown}"

    def test_critical_rules_reach_a_human(self, rules):
        # A critical alert that only writes to a log file is not an alert.
        for rule in rules:
            if rule.severity is Severity.CRITICAL:
                assert "webhook" in rule.channels, rule.rule_id

    def test_device_facing_rules_hold_before_firing(self, rules):
        # Without a hold, one noisy sample pages someone. The kinds excluded
        # here are inherently sustained already: offline is a duration, rate of
        # change is a slope across a window.
        instant = {
            RuleKind.OFFLINE,
            RuleKind.RATE_OF_CHANGE,
            RuleKind.DEAD_LETTER_RATE,
            RuleKind.CERTIFICATE_EXPIRY,
        }
        for rule in rules:
            if rule.kind not in instant:
                assert rule.for_s > 0, f"{rule.rule_id} fires on a single sample"

    def test_every_rule_has_recovery_hysteresis(self, rules):
        # Without it, a value sitting on the threshold produces an endless
        # fire-and-resolve cycle.
        for rule in rules:
            assert rule.recovery_s > 0, rule.rule_id

    def test_rule_ids_are_unique(self, rules):
        ids = [rule.rule_id for rule in rules]
        assert len(ids) == len(set(ids))

    def test_operators_are_all_whitelisted(self, rules):
        for rule in rules:
            assert rule.operator in OPERATORS
