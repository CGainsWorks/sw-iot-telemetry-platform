"""Event loop setup that has to happen before any asyncio MQTT client runs.

Python on Windows defaults to the Proactor event loop, which has no
``add_reader``/``remove_writer``. paho-mqtt, and therefore aiomqtt, drives its
socket through exactly those calls, so an MQTT client on a default Windows loop
raises ``NotImplementedError`` from inside a destructor and takes the process
down in a way that is hard to read.

Every service runs in a Linux container where this is a no-op. It matters for
the things that run on a developer's own machine: the simulator, the benchmark
and the integration tests.
"""

from __future__ import annotations

import asyncio
import sys


def use_compatible_event_loop() -> bool:
    """Select an event loop that supports socket readers. Call before ``asyncio.run``.

    Returns True if the policy was changed, so a caller can log it.
    """
    if sys.platform != "win32":
        return False

    selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if selector_policy is None:  # pragma: no cover - not reachable on win32
        return False

    if isinstance(asyncio.get_event_loop_policy(), selector_policy):
        return False

    asyncio.set_event_loop_policy(selector_policy())
    return True
