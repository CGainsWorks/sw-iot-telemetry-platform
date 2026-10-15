"""One set of analytical queries, two SQL dialects.

The analytical layer targets BigQuery. Testing it would then need a GCP project,
a billing account and network access, which means in practice it would not be
tested at all and the queries in the repository would be the ones nobody has
ever run.

So the queries are written once against a small macro vocabulary and rendered
into either BigQuery Standard SQL or DuckDB. DuckDB reads the Parquet the
exporter produces, runs in-process with no server, and is close enough to
BigQuery on the constructs used here that a query which works against one works
against the other. CI runs every query against DuckDB on exported fixtures, so a
syntax error or a broken join is caught in the repository rather than in a
BigQuery bill.

The macro vocabulary is deliberately tiny. Everything in it is a construct where
the two dialects genuinely differ; anything they agree on is written plainly.
Adding a macro is a decision to accept a little indirection in exchange for a
query that can be tested, and it should be resisted for anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

QUERIES_DIR = Path(__file__).resolve().parent / "queries"

_MACRO_RE = re.compile(r"\{\{\s*([A-Z_]+)\s*\(([^}]*?)\)\s*\}\}")
_ARG_SPLIT_RE = re.compile(r",(?![^()]*\))")


class Dialect(str, Enum):
    BIGQUERY = "bigquery"
    DUCKDB = "duckdb"


class MacroError(ValueError):
    """Raised when a query uses a macro that does not exist, or misuses one."""


@dataclass(frozen=True)
class Macro:
    name: str
    arity: int
    bigquery: str
    duckdb: str
    description: str

    def render(self, dialect: Dialect, args: list[str]) -> str:
        if len(args) != self.arity:
            raise MacroError(f"{self.name} takes {self.arity} argument(s), got {len(args)}: {args}")
        template = self.bigquery if dialect is Dialect.BIGQUERY else self.duckdb
        return template.format(*args)


MACROS: dict[str, Macro] = {
    macro.name: macro
    for macro in (
        Macro(
            name="NOW",
            arity=0,
            bigquery="CURRENT_TIMESTAMP()",
            duckdb="now()",
            description="Current instant, UTC.",
        ),
        Macro(
            name="TRUNC_DAY",
            arity=1,
            bigquery="TIMESTAMP_TRUNC({0}, DAY)",
            duckdb="date_trunc('day', {0})",
            description="Round a timestamp down to the start of its day.",
        ),
        Macro(
            name="TRUNC_HOUR",
            arity=1,
            bigquery="TIMESTAMP_TRUNC({0}, HOUR)",
            duckdb="date_trunc('hour', {0})",
            description="Round a timestamp down to the start of its hour.",
        ),
        Macro(
            name="DAYS_AGO",
            arity=1,
            bigquery="TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {0} DAY)",
            duckdb="now() - INTERVAL {0} DAY",
            description="An instant N days before now.",
        ),
        Macro(
            name="SECONDS_BETWEEN",
            arity=2,
            bigquery="TIMESTAMP_DIFF({1}, {0}, SECOND)",
            duckdb="date_diff('second', {0}, {1})",
            # Argument order is the source of every bug in date arithmetic:
            # BigQuery is (end, start) and DuckDB is (start, end). The macro
            # fixes it as (start, end) and does the swapping here, once.
            description="Seconds from the first argument to the second.",
        ),
        Macro(
            name="SAFE_DIVIDE",
            arity=2,
            bigquery="SAFE_DIVIDE({0}, {1})",
            duckdb="({0} / NULLIF({1}, 0))",
            description="Division that yields NULL rather than an error on a zero divisor.",
        ),
        Macro(
            name="TO_DATE",
            arity=1,
            bigquery="DATE({0})",
            duckdb="CAST({0} AS DATE)",
            description="Calendar date of a timestamp.",
        ),
        Macro(
            name="MEDIAN",
            arity=1,
            # BigQuery has no MEDIAN. APPROX_QUANTILES(x, 2) returns three
            # boundaries, min, median and max, so offset 1 is the median.
            bigquery="APPROX_QUANTILES({0}, 2)[OFFSET(1)]",
            duckdb="median({0})",
            description="Median of a column, as an aggregate.",
        ),
        Macro(
            name="AS_INT",
            arity=1,
            bigquery="CAST({0} AS INT64)",
            duckdb="CAST({0} AS BIGINT)",
            description="Cast to a 64-bit integer. The two dialects spell the type differently.",
        ),
        # The next two exist because macros do not nest. Writing
        # {{TO_DATE(DAYS_AGO(90))}} looks like it should work and does not: the
        # renderer treats the inner call as a literal argument and emits a
        # function neither warehouse has. Rather than make the renderer
        # recursive, which makes every macro's expansion harder to reason about,
        # the compositions that are actually needed get their own entry, and
        # _reject_nested below turns the mistake into an error.
        Macro(
            name="DATE_DAYS_AGO",
            arity=1,
            bigquery="DATE_SUB(CURRENT_DATE(), INTERVAL {0} DAY)",
            duckdb="(CURRENT_DATE - INTERVAL {0} DAY)",
            description="The calendar date N days before today.",
        ),
        Macro(
            name="SECONDS_UNTIL",
            arity=1,
            bigquery="TIMESTAMP_DIFF({0}, CURRENT_TIMESTAMP(), SECOND)",
            duckdb="date_diff('second', now(), {0})",
            description="Seconds from now until the given timestamp. Negative if past.",
        ),
    )
}


def _reject_nested(sql: str) -> None:
    """Fail on a macro call written outside braces, which would not be expanded.

    Without this a nested call such as ``{{TO_DATE(DAYS_AGO(90))}}`` renders to
    ``CAST(DAYS_AGO(90) AS DATE)`` and only fails when the warehouse rejects the
    unknown function, which for BigQuery means after a network round trip and a
    billing event.
    """
    stripped = _MACRO_RE.sub(" ", sql)
    for name in MACROS:
        if re.search(rf"\b{name}\s*\(", stripped):
            raise MacroError(
                f"{name}( appears outside a {{{{...}}}} macro. Macros do not nest: "
                f"write a composite macro instead of calling {name} inside another."
            )


def render(sql: str, dialect: Dialect, *, table_prefix: str = "") -> str:
    """Expand macros in ``sql`` for ``dialect``.

    ``table_prefix`` is prepended to every ``{{table}}`` reference. BigQuery
    needs ``project.dataset.table``; DuckDB reads a view name registered from a
    Parquet file, so it gets an empty prefix.
    """

    _reject_nested(sql)

    def expand(match: re.Match[str]) -> str:
        name = match.group(1)
        macro = MACROS.get(name)
        if macro is None:
            known = ", ".join(sorted(MACROS))
            raise MacroError(f"unknown macro {name!r}. Known macros: {known}")

        raw_args = match.group(2).strip()
        args = [arg.strip() for arg in _ARG_SPLIT_RE.split(raw_args)] if raw_args else []
        return macro.render(dialect, args)

    rendered = _MACRO_RE.sub(expand, sql)

    # Catch a malformed macro that the pattern did not match, rather than
    # shipping it to the warehouse and getting a syntax error there.
    leftover = re.search(r"\{\{(?!table:)", rendered)
    if leftover:
        context = rendered[max(0, leftover.start() - 40) : leftover.start() + 60]
        raise MacroError(f"unexpanded macro near: ...{context}...")

    rendered = re.sub(
        r"\{\{table:([a-z_]+)\}\}",
        lambda m: f"{table_prefix}{m.group(1)}",
        rendered,
    )

    if "{{" in rendered:
        raise MacroError("unexpanded placeholder remains after rendering")

    return rendered


def load(name: str) -> str:
    path = QUERIES_DIR / f"{name}.sql"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in QUERIES_DIR.glob("*.sql")))
        raise FileNotFoundError(f"no query named {name!r}. Available: {available}")
    return path.read_text(encoding="utf-8")


def query_names() -> list[str]:
    return sorted(path.stem for path in QUERIES_DIR.glob("*.sql"))


def describe_macros() -> str:
    lines = ["| Macro | Arguments | BigQuery | DuckDB |", "| --- | --- | --- | --- |"]
    for macro in MACROS.values():
        lines.append(
            f"| `{{{{{macro.name}(...)}}}}` | {macro.arity} | "
            f"`{macro.bigquery}` | `{macro.duckdb}` |"
        )
    return "\n".join(lines)
