"""Small thread-safe TTL cache that keeps the last good value when a refresh fails."""

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class CacheResult:
    value: Any
    fetched_at: float  # time.time() of the last successful fetch
    error: str | None = None  # set when the latest refresh failed and value is stale

    @property
    def stale(self) -> bool:
        return self.error is not None


class TTLCache:
    def __init__(self, ttl: float = 15.0, clock: Callable[[], float] = time.time):
        self.ttl = ttl
        self.clock = clock
        self._entries: dict[str, CacheResult] = {}
        self._checked: dict[str, float] = {}
        self._lock = threading.Lock()

    def get(self, key: str, fetch: Callable[[], Any]) -> CacheResult:
        """Return a cached value, calling fetch() when it is older than the TTL.

        If fetch() raises and an older value exists, that value is returned with
        `error` set. If there is no older value, the exception propagates.
        """
        with self._lock:
            now = self.clock()
            entry = self._entries.get(key)
            if entry is not None and now - self._checked.get(key, 0) < self.ttl:
                return entry
            try:
                value = fetch()
            except Exception as exc:
                if entry is None:
                    raise
                self._checked[key] = now
                entry = CacheResult(entry.value, entry.fetched_at, str(exc))
                self._entries[key] = entry
                return entry
            entry = CacheResult(value, now)
            self._entries[key] = entry
            self._checked[key] = now
            return entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._checked.clear()
