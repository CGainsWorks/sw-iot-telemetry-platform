"""MQTT topic grammar.

The topic is not decoration, it is the authorisation boundary. Both brokers in
play bind a connection to an identity and then restrict which topics that
identity may write:

* **AWS IoT Core** substitutes ``${iot:Connection.Thing.ThingName}`` into the
  device policy, so a thing can only publish beneath its own name.
* **Mosquitto** with ``use_identity_as_username true`` sets the username from
  the client certificate common name, and the ACL pattern ``%u`` does the same
  job.

Either way the ``device_id`` segment of the topic is the authenticated identity,
whereas the ``device_id`` inside the payload is just a claim. Ingest compares
the two and rejects any mismatch, which is what stops a compromised device from
writing telemetry attributed to its neighbours.

Grammar::

    dt/{tenant}/{site_id}/{device_id}/telemetry     device to platform, readings
    dt/{tenant}/{site_id}/{device_id}/status        device to platform, lifecycle
    cmd/{tenant}/{site_id}/{device_id}/req          platform to device, request
    cmd/{tenant}/{site_id}/{device_id}/res          device to platform, response
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

DEFAULT_TENANT = "acme"

_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


class TopicKind(str, Enum):
    TELEMETRY = "telemetry"
    STATUS = "status"
    COMMAND_REQUEST = "req"
    COMMAND_RESPONSE = "res"


class TopicError(ValueError):
    """Raised when a topic does not match the grammar."""


@dataclass(frozen=True)
class TopicParts:
    tenant: str
    site_id: str
    device_id: str
    kind: TopicKind


def _check(segment: str, label: str) -> str:
    if not _SEGMENT_RE.match(segment):
        raise TopicError(f"{label} segment {segment!r} is not a valid topic segment")
    return segment


def telemetry_topic(device_id: str, site_id: str, tenant: str = DEFAULT_TENANT) -> str:
    return f"dt/{_check(tenant, 'tenant')}/{_check(site_id, 'site')}/{_check(device_id, 'device')}/telemetry"


def status_topic(device_id: str, site_id: str, tenant: str = DEFAULT_TENANT) -> str:
    return f"dt/{_check(tenant, 'tenant')}/{_check(site_id, 'site')}/{_check(device_id, 'device')}/status"


def command_request_topic(device_id: str, site_id: str, tenant: str = DEFAULT_TENANT) -> str:
    return f"cmd/{_check(tenant, 'tenant')}/{_check(site_id, 'site')}/{_check(device_id, 'device')}/req"


def command_response_topic(device_id: str, site_id: str, tenant: str = DEFAULT_TENANT) -> str:
    return f"cmd/{_check(tenant, 'tenant')}/{_check(site_id, 'site')}/{_check(device_id, 'device')}/res"


def subscription_all_telemetry(tenant: str = DEFAULT_TENANT) -> str:
    return f"dt/{_check(tenant, 'tenant')}/+/+/telemetry"


def subscription_all_status(tenant: str = DEFAULT_TENANT) -> str:
    return f"dt/{_check(tenant, 'tenant')}/+/+/status"


def parse(topic: str) -> TopicParts:
    """Split a concrete topic into its parts, or raise :class:`TopicError`."""
    segments = topic.split("/")
    if len(segments) != 5:
        raise TopicError(f"expected 5 topic segments, got {len(segments)}: {topic!r}")

    prefix, tenant, site_id, device_id, leaf = segments
    if (
        prefix == "dt"
        and leaf in ("telemetry", "status")
        or prefix == "cmd"
        and leaf in ("req", "res")
    ):
        kind = TopicKind(leaf)
    else:
        raise TopicError(f"unrecognised topic shape: {topic!r}")

    return TopicParts(
        tenant=_check(tenant, "tenant"),
        site_id=_check(site_id, "site"),
        device_id=_check(device_id, "device"),
        kind=kind,
    )


def mosquitto_acl_patterns(tenant: str = DEFAULT_TENANT) -> list[str]:
    """ACL lines that confine a device to its own topic space.

    ``%u`` is the certificate common name. Generated here rather than hand
    written into the broker config so the grammar has exactly one definition.
    """
    return [
        f"pattern write dt/{tenant}/+/%u/telemetry",
        f"pattern write dt/{tenant}/+/%u/status",
        f"pattern read cmd/{tenant}/+/%u/req",
        f"pattern write cmd/{tenant}/+/%u/res",
    ]
