"""Thread-safe, byte-bounded LRU cache for in-memory artifacts.

Generic over "an artifact is a dict of numpy arrays" (matrices, annotation
tables, ...). Used by lambdas to keep memory flat while serving many different
datasets: only the most recently used artifacts stay resident.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import Optional

import numpy as np

Artifact = dict[str, np.ndarray]


def artifact_nbytes(artifact: Artifact) -> int:
    """Total in-memory size of an artifact, in bytes."""
    return sum(int(array.nbytes) for array in artifact.values())


class LruCache:
    """Thread-safe LRU cache bounded by total bytes of cached artifacts.

    Requests may be served on multiple threads, so every access is locked.
    Eviction is least-recently-used and capped by summed `nbytes`, not a fixed
    count, because artifacts vary in size. A single artifact larger than the cap
    is still kept (we never evict the only entry), so requests can always be served.
    """

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._store: "OrderedDict[int, Artifact]" = OrderedDict()
        self._sizes: dict[int, int] = {}
        self._total = 0
        self._lock = Lock()

    def get(self, key: int) -> Optional[Artifact]:
        """Return a cached artifact and mark it most-recently-used."""
        with self._lock:
            artifact = self._store.get(key)
            if artifact is not None:
                self._store.move_to_end(key)
            return artifact

    def put(self, key: int, artifact: Artifact) -> None:
        """Insert/replace an artifact and evict LRU entries past the byte cap."""
        with self._lock:
            if key in self._store:
                self._total -= self._sizes.pop(key)
                del self._store[key]

            size = artifact_nbytes(artifact)
            self._store[key] = artifact
            self._sizes[key] = size
            self._total += size
            self._store.move_to_end(key)

            # Keep at least the just-inserted entry even if it alone exceeds the cap.
            while self._total > self.max_bytes and len(self._store) > 1:
                evicted_key, _ = self._store.popitem(last=False)
                self._total -= self._sizes.pop(evicted_key)

    def stats(self) -> dict[str, int]:
        """Return current cache occupancy (for logging / debugging)."""
        with self._lock:
            return {
                "entries": len(self._store),
                "bytes": self._total,
                "max_bytes": self.max_bytes,
            }
