"""
local_cache.py
---------------
Simulates the node-local cache tier(s) that sit in front of the remote
blob store: page cache / local SSD in a real deployment. Reuses the same
LRU-with-capacity idea as a classic distributed cache node -- just keyed
by content hash instead of by request key, and storing chunk bytes.

Once a chunk is fetched from the network once, subsequent reads (by this
container, or by a *different* container that happens to reference the
same chunk hash -- e.g. shared base-image layers) are served locally,
which is what makes warm starts on the same node dramatically faster.
"""
from collections import OrderedDict


class LocalChunkCache:
    def __init__(self, capacity_bytes: int):
        self.capacity_bytes = capacity_bytes
        self._store: "OrderedDict[str, bytes]" = OrderedDict()
        self._size = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, digest: str) -> bytes | None:
        if digest in self._store:
            self._store.move_to_end(digest)  # mark as most-recently-used
            self.hits += 1
            return self._store[digest]
        self.misses += 1
        return None

    def put(self, digest: str, data: bytes) -> None:
        if digest in self._store:
            self._store.move_to_end(digest)
            return
        self._store[digest] = data
        self._size += len(data)
        while self._size > self.capacity_bytes and self._store:
            evicted_digest, evicted_data = self._store.popitem(last=False)
            self._size -= len(evicted_data)
            self.evictions += 1

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
