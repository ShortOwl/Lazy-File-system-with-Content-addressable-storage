"""
blob_store.py
--------------
Simulates the *remote* content-addressed blob backend (think: a container
registry / S3 bucket in a real system, or Modal's blob storage tier).

Key idea: content-addressing. Every chunk of file data is named by the
SHA-256 hash of its bytes, not by a file path. Two files (even in two
completely different container images) that happen to share a chunk of
identical bytes are stored ONCE. This is what gives content-addressed
systems their deduplication for free.

We also simulate network cost (latency + bandwidth) so the demo can show
*why* eager pulling is slow and lazy pulling + caching is fast.
"""
import hashlib
import time
from dataclasses import dataclass, field


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class NetworkProfile:
    """Crude model of network cost for a remote fetch."""
    latency_s: float = 0.002        # per-request round trip (2ms)
    bandwidth_bytes_per_s: float = 200 * 1024 * 1024  # 200 MB/s link

    def transfer_time(self, num_bytes: int) -> float:
        return self.latency_s + (num_bytes / self.bandwidth_bytes_per_s)


@dataclass
class BlobStoreStats:
    unique_chunks: int = 0
    unique_bytes: int = 0
    chunks_written: int = 0          # total put() calls (incl. duplicates)
    bytes_deduped: int = 0           # bytes saved because chunk already existed
    network_gets: int = 0
    network_bytes_transferred: int = 0
    network_time_s: float = 0.0


class RemoteBlobStore:
    """
    A content-addressed key-value store: hash -> bytes.
    Simulates being "far away" (a registry / blob storage service) --
    every get() pays simulated network latency + bandwidth cost.
    """

    def __init__(self, network: NetworkProfile = None):
        self._blobs: dict[str, bytes] = {}
        self.network = network or NetworkProfile()
        self.stats = BlobStoreStats()

    # ---- write path (image build / push) ----
    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        self.stats.chunks_written += 1
        if digest in self._blobs:
            self.stats.bytes_deduped += len(data)
        else:
            self._blobs[digest] = data
            self.stats.unique_chunks += 1
            self.stats.unique_bytes += len(data)
        return digest

    # ---- read path (lazy fetch on-demand) ----
    def get(self, digest: str) -> bytes:
        if digest not in self._blobs:
            raise KeyError(f"blob {digest} not found in remote store")
        data = self._blobs[digest]
        wait = self.network.transfer_time(len(data))
        time.sleep(wait)  # simulate the network round trip
        self.stats.network_gets += 1
        self.stats.network_bytes_transferred += len(data)
        self.stats.network_time_s += wait
        return data
