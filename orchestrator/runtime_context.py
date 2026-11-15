"""Dependencies and persistent iteration state, explicit at the CLI boundary."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class RuntimeContext:
    values: dict = field(default_factory=dict)
    wall_clock: Callable[[], float] = time.time
