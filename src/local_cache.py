"""
local_cache.py
---------------
WHAT THIS FILE DOES:
This file implements a fast, Local File Cache on the worker machine (like RAM or a fast SSD).

KEY CONCEPTS IN SIMPLE WORDS:
1. Why do we need a Local Cache?
   Downloading files over the internet from cloud storage is slow.
   Once a data chunk is downloaded, we save a copy in this local cache.
   - Next time your app reads that file -> Served instantly in 0 milliseconds!
   - If another container needs the exact same base library -> Served instantly from cache!

2. LRU Eviction Policy (Least Recently Used):
   Since hard drive/RAM space is limited (`capacity_bytes`), if the cache gets full,
   we automatically throw away the oldest, least-recently-used file chunks to free up space.
"""

from collections import OrderedDict


class LocalChunkCache:
    """
    A memory-bounded LRU (Least Recently Used) cache for file data chunks.
    Key: Chunk SHA-256 Digest (Hash)
    Value: Raw Chunk Data Bytes
    """

    def __init__(self, capacity_bytes: int):
        self.capacity_bytes = capacity_bytes  # Maximum cache size allowed in bytes
        self._store: "OrderedDict[str, bytes]" = OrderedDict()  # Keeps insertion order for LRU
        self._size = 0                          # Current total bytes stored in cache

        # Performance tracking metrics
        self.hits = 0        # Count of successful reads from local cache
        self.misses = 0      # Count of cache misses (had to download from cloud)
        self.evictions = 0  # Count of chunks removed because cache was full

    def get(self, digest: str) -> bytes | None:
        """
        Retrieves a chunk from local cache using its SHA-256 hash.
        - If found: Marks chunk as recently used, increments `hits`, and returns data.
        - If NOT found: Increments `misses` and returns None.
        """
        if digest in self._store:
            # Cache Hit! Move chunk key to end of OrderedDict (marking it most recently used)
            self._store.move_to_end(digest)
            self.hits += 1
            return self._store[digest]

        # Cache Miss! The file chunk is not stored locally yet
        self.misses += 1
        return None

    def put(self, digest: str, data: bytes) -> None:
        """
        Saves a downloaded chunk into the local cache.
        If adding this chunk exceeds `capacity_bytes`, it automatically deletes
        the oldest (least recently used) chunks until space is under the limit.
        """
        if digest in self._store:
            # Chunk already exists in cache, just mark it as recently used
            self._store.move_to_end(digest)
            return

        # Add new chunk to cache store
        self._store[digest] = data
        self._size += len(data)

        # Enforce capacity limit (LRU Eviction)
        while self._size > self.capacity_bytes and self._store:
            # Remove oldest item from front of OrderedDict
            evicted_digest, evicted_data = self._store.popitem(last=False)
            self._size -= len(evicted_data)
            self.evictions += 1

    @property
    def hit_rate(self) -> float:
        """Calculates cache hit percentage (0.0 = 0%, 1.0 = 100%)."""
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
