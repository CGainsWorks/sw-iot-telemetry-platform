"""Every benchmark figure the README quotes is in the committed summary.

The full result files under bench/results/ stay out of the repository, so
bench/summary.json is the evidence for the numbers in the README and in
docs/benchmarks.md. This test ties them together: a figure edited in the prose
without a run behind it, or a summary regenerated without updating the prose,
fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUMMARY = json.loads((ROOT / "bench" / "summary.json").read_text(encoding="utf-8"))
README = (ROOT / "README.md").read_text(encoding="utf-8")
BENCHMARKS = (ROOT / "docs" / "benchmarks.md").read_text(encoding="utf-8")

RUNS = {run["label"]: run for run in SUMMARY["runs"]}


def test_the_summary_carries_no_local_paths():
    text = json.dumps(SUMMARY)
    assert ":\\\\" not in text and "/Users/" not in text and "/home/" not in text


@pytest.mark.parametrize("label", sorted(RUNS))
def test_each_run_is_quoted_with_its_measured_storage_rate(label):
    stored = RUNS[label]["stored_rate_hz"]
    figure = f"{round(stored):,}"
    assert (
        f"{figure} readings/s" in README
    ), f"README does not quote {figure} readings/s for {label}"
    assert f"{figure} readings/s" in BENCHMARKS


@pytest.mark.parametrize("label", sorted(RUNS))
def test_each_quoted_loss_figure_matches_the_run(label):
    loss = f"{RUNS[label]['loss_fraction'] * 100:.3f}%"
    assert loss in README, f"README does not quote the {loss} loss of {label}"


def test_the_readme_headline_is_the_lightest_run():
    run = RUNS["laptop-50dev-10hz"]
    p50 = round(run["end_to_end_ms"]["p50"])
    assert (
        f"{round(run['stored_rate_hz']):,} readings/s with zero loss and {p50} ms median" in README
    )
