"""Dependencies and persistent iteration state, explicit at the CLI boundary."""
from dataclasses import dataclass, field
from typing import Callable
import time


@dataclass
class RuntimeContext:
    values: dict = field(default_factory=dict)
    wall_clock: Callable[[], float] = time.time
