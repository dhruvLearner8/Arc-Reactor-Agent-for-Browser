"""
Circuit breaker implementation for external tool calls.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Dict


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    half_open_max_calls: int = 2
    state: CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int = field(default=0)
    success_count: int = field(default=0)
    last_failure_time: float = field(default=0.0)
    half_open_calls: int = field(default=0)
    _lock: Lock = field(default_factory=Lock)

    def can_execute(self) -> bool:
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
                    return True
                return False
            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls < self.half_open_max_calls:
                    self.half_open_calls += 1
                    return True
                return False
            return False

    def record_success(self):
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.half_open_max_calls:
                    self._transition_to(CircuitState.CLOSED)
            elif self.state == CircuitState.CLOSED:
                self.failure_count = max(0, self.failure_count - 1)

    def record_failure(self):
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.OPEN)
            elif self.state == CircuitState.CLOSED and self.failure_count >= self.failure_threshold:
                self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState):
        old_state = self.state
        self.state = new_state
        if new_state == CircuitState.CLOSED:
            self.failure_count = 0
            self.success_count = 0
            self.half_open_calls = 0
            print(f"Circuit [{self.name}]: {old_state.value} -> CLOSED")
        elif new_state == CircuitState.OPEN:
            self.half_open_calls = 0
            self.success_count = 0
            print(f"Circuit [{self.name}]: {old_state.value} -> OPEN")
        elif new_state == CircuitState.HALF_OPEN:
            self.half_open_calls = 0
            self.success_count = 0
            print(f"Circuit [{self.name}]: {old_state.value} -> HALF_OPEN")

    def get_status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "last_failure": self.last_failure_time,
            "time_until_retry": max(
                0, self.recovery_timeout - (time.time() - self.last_failure_time)
            )
            if self.state == CircuitState.OPEN
            else 0,
        }


class CircuitOpenError(Exception):
    pass


_breakers: Dict[str, CircuitBreaker] = {}
_registry_lock = Lock()


def get_breaker(
    name: str, failure_threshold: int = 5, recovery_timeout: float = 60.0
) -> CircuitBreaker:
    with _registry_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
        return _breakers[name]
