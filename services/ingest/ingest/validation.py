"""The ingest decision: accept a message, or dead-letter it with a reason.

Pure functions, no broker and no Kafka, so every rule below is unit tested
directly. This is the only place in the platform that decides what counts as a
valid message, and it is the boundary where an untrusted payload becomes a
trusted record.

The ordering of the checks matters. Identity is settled before anything in the
payload is believed, because the topic is authenticated and the payload is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import ValidationError

from telemetry_common.models import (
    DeadLetter,
    NormalisedReading,
    StatusMessage,
    TelemetryMessage,
    explode,
)
from telemetry_common.topics import TopicError, TopicKind, TopicParts, parse
from telemetry_common.versioning import SchemaVersionError, is_newer_minor, require_supported


class RejectReason(str, Enum):
    """Why a message was refused. One value per dashboard line."""

    MALFORMED_TOPIC = "malformed_topic"
    MALFORMED_JSON = "malformed_json"
    SCHEMA_UNSUPPORTED = "schema_unsupported"
    SCHEMA_INVALID = "schema_invalid"
    IDENTITY_MISMATCH = "identity_mismatch"
    SITE_MISMATCH = "site_mismatch"
    IMPLAUSIBLE_READING = "implausible_reading"
    TIMESTAMP_OUT_OF_RANGE = "timestamp_out_of_range"


#: How far ahead of the platform clock a device timestamp may sit before the
#: message is refused. Some skew is normal on hardware with no RTC; hours of it
#: means a broken clock, and letting it through would put rows in the future
#: where no query or continuous aggregate will ever look at them again.
MAX_CLOCK_AHEAD = timedelta(minutes=5)

#: How far behind. Generous, because a device that buffered through an outage is
#: doing the right thing and its backlog should be accepted.
MAX_CLOCK_BEHIND = timedelta(days=7)


@dataclass
class Accepted:
    """A message that passed every check."""

    parts: TopicParts
    message: TelemetryMessage
    readings: list[NormalisedReading]
    #: True when the firmware is on a later minor than this build. The message is
    #: still accepted; the flag exists so the fleet running ahead of the platform
    #: is visible as a metric rather than as silently dropped fields.
    schema_ahead: bool = False


@dataclass
class AcceptedStatus:
    parts: TopicParts
    message: StatusMessage


@dataclass
class Rejected:
    reason: RejectReason
    detail: str
    dead_letter: DeadLetter


IngestResult = Accepted | AcceptedStatus | Rejected


def _reject(
    reason: RejectReason, detail: str, topic: str, raw: str, received_at: datetime
) -> Rejected:
    return Rejected(
        reason=reason,
        detail=detail,
        dead_letter=DeadLetter(
            reason=reason.value,
            detail=detail,
            topic=topic,
            received_at=received_at,
            raw=raw,
        ),
    )


def _decode(payload: bytes | str) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def handle(
    topic: str,
    payload: bytes | str,
    *,
    received_at: datetime | None = None,
    reject_implausible: bool = True,
) -> IngestResult:
    """Decide what to do with one message off the broker."""
    stamp = received_at or datetime.now(UTC)
    raw = _decode(payload)

    # 1. Topic first. The topic is what the broker authenticated, so until it
    #    parses there is no identity to attribute anything to.
    try:
        parts = parse(topic)
    except TopicError as exc:
        return _reject(RejectReason.MALFORMED_TOPIC, str(exc), topic, raw, stamp)

    if parts.kind not in (TopicKind.TELEMETRY, TopicKind.STATUS):
        return _reject(
            RejectReason.MALFORMED_TOPIC,
            f"ingest does not consume {parts.kind.value} topics",
            topic,
            raw,
            stamp,
        )

    # 2. JSON.
    try:
        document: Any = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _reject(RejectReason.MALFORMED_JSON, str(exc), topic, raw, stamp)

    if not isinstance(document, dict):
        return _reject(
            RejectReason.MALFORMED_JSON,
            f"expected a JSON object, got {type(document).__name__}",
            topic,
            raw,
            stamp,
        )

    # 3. Schema version, before validation. A payload from an unsupported major
    #    may look structurally fine and still mean something entirely different,
    #    so it must never reach the model.
    version_raw = document.get("schema_version")
    try:
        require_supported(str(version_raw))
    except SchemaVersionError as exc:
        return _reject(RejectReason.SCHEMA_UNSUPPORTED, str(exc), topic, raw, stamp)

    if parts.kind is TopicKind.STATUS:
        return _handle_status(parts, document, topic, raw, stamp)

    # 4. Structure.
    try:
        message = TelemetryMessage.model_validate(document)
    except ValidationError as exc:
        return _reject(
            RejectReason.SCHEMA_INVALID,
            "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:5]
            ),
            topic,
            raw,
            stamp,
        )

    # 5. Identity. The topic segment came from the TLS certificate common name;
    #    the payload field is only a claim. A mismatch means either a bug in the
    #    firmware or a device publishing on someone else's behalf, and neither
    #    should be stored under either identity.
    if message.device_id != parts.device_id:
        return _reject(
            RejectReason.IDENTITY_MISMATCH,
            f"payload claims device_id={message.device_id!r} "
            f"but the authenticated topic identity is {parts.device_id!r}",
            topic,
            raw,
            stamp,
        )

    if message.site_id != parts.site_id:
        return _reject(
            RejectReason.SITE_MISMATCH,
            f"payload claims site_id={message.site_id!r} but the topic says {parts.site_id!r}",
            topic,
            raw,
            stamp,
        )

    # 6. Clock.
    if message.ts > stamp + MAX_CLOCK_AHEAD:
        return _reject(
            RejectReason.TIMESTAMP_OUT_OF_RANGE,
            f"timestamp {message.ts.isoformat()} is more than "
            f"{MAX_CLOCK_AHEAD} ahead of the platform clock",
            topic,
            raw,
            stamp,
        )
    if message.ts < stamp - MAX_CLOCK_BEHIND:
        return _reject(
            RejectReason.TIMESTAMP_OUT_OF_RANGE,
            f"timestamp {message.ts.isoformat()} is more than "
            f"{MAX_CLOCK_BEHIND} behind the platform clock",
            topic,
            raw,
            stamp,
        )

    # 7. Physical plausibility.
    if reject_implausible:
        implausible = message.implausible_metrics()
        if implausible:
            return _reject(
                RejectReason.IMPLAUSIBLE_READING,
                "readings outside the catalogue range: "
                + ", ".join(f"{m}={message.readings[m]}" for m in implausible),
                topic,
                raw,
                stamp,
            )

    return Accepted(
        parts=parts,
        message=message,
        readings=explode(message, ingest_ts=stamp),
        schema_ahead=is_newer_minor(message.schema_version),
    )


def _handle_status(
    parts: TopicParts,
    document: dict[str, Any],
    topic: str,
    raw: str,
    stamp: datetime,
) -> IngestResult:
    try:
        message = StatusMessage.model_validate(document)
    except ValidationError as exc:
        return _reject(
            RejectReason.SCHEMA_INVALID,
            "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:5]
            ),
            topic,
            raw,
            stamp,
        )

    if message.device_id != parts.device_id:
        return _reject(
            RejectReason.IDENTITY_MISMATCH,
            f"payload claims device_id={message.device_id!r} "
            f"but the authenticated topic identity is {parts.device_id!r}",
            topic,
            raw,
            stamp,
        )

    return AcceptedStatus(parts=parts, message=message)
