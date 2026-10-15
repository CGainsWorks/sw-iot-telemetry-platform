"""Shared setup for the integration suite.

The event loop policy has to be chosen before pytest-asyncio creates a loop,
which is why it happens at import time here rather than inside a fixture.
"""

from __future__ import annotations

from telemetry_common.runtime import use_compatible_event_loop

# aiomqtt drives its socket through add_reader/remove_writer, which the default
# Windows event loop does not implement. No effect on Linux or macOS.
use_compatible_event_loop()
