"""
blob_store.py
--------------
WHAT THIS FILE DOES:
This file simulates a remote Cloud Storage server (like AWS S3 or a Docker Registry).

KEY CONCEPTS IN SIMPLE WORDS:
1. Content-Addressing (SHA-256):
   Instead of identifying a piece of data by its file path (like /app/file.txt),
   we calculate a unique digital fingerprint (a SHA-256 hash) for the data bytes.
   - If two files are 100% identical, they get the exact same hash.
   - We only store one copy in the cloud! This saves a lot of storage (Deduplication).

2. Simulated Network Delay:
   Real cloud servers take time to send data over the internet.
   We simulate internet delay (ping/latency + download speed) using time.sleep().
   This lets us measure how fast lazy loading is compared to traditional loading.
"""

import hashlib
import time
from dataclasses import dataclass, field


def sha256_hex(data: bytes) -> str:
    """
    Creates a unique 64-character hash (fingerprint) for any given piece of data.
    Even if 1 byte changes, the resulting hash will be completely different.
    """
    return hashlib.sha256(data).hexdigest()


@dataclass
class NetworkProfile:
    """
    Simulates real-world internet connection speeds.
    
    Attributes:
        latency_s: The ping/delay before downloading starts (default: 2 milliseconds).
        bandwidth_bytes_per_s: Download speed in bytes per second (default: 200 MB/s).
    """
    latency_s: float = 0.002                          # 2 ms ping delay
    bandwidth_bytes_per_s: float = 200 * 1024 * 1024  # 200 MB per second speed

    def transfer_time(self, num_bytes: int) -> float:
        """Calculates how many seconds it takes to download `num_bytes` over this network."""
        # Total Time = Initial Connection Delay + (Data Size / Download Speed)
        return self.latency_s + (num_bytes / self.bandwidth_bytes_per_s)


@dataclass
class BlobStoreStats:
    """
    Keeps track of metrics for analysis and comparison.
    Shows how much data was uploaded, downloaded, saved, and time spent on network calls.
    """
    unique_chunks: int = 0             # Number of distinct data blocks saved
    unique_bytes: int = 0              # Total unique size stored in bytes
    chunks_written: int = 0            # Total upload attempts (including duplicates)
    bytes_deduped: int = 0             # Bytes saved because identical data was already stored
    network_gets: int = 0              # Total number of download requests made
    network_bytes_transferred: int = 0 # Total amount of data downloaded (in bytes)
    network_time_s: float = 0.0        # Total time spent waiting for network transfers


class RemoteBlobStore:
    """
    Simulates the Cloud Registry (like S3 or Docker Hub).
    Stores pieces of file data indexed by their SHA-256 hash.
    """

    def __init__(self, network: NetworkProfile = None):
        # Internal dictionary mapping: hash -> raw data bytes
        self._blobs: dict[str, bytes] = {}
        # Network settings (delay and download speed)
        self.network = network or NetworkProfile()
        # Object to record performance statistics
        self.stats = BlobStoreStats()

    # ---- UPLOAD PATH (When building/pushing an image) ----
    def put(self, data: bytes) -> str:
        """
        Uploads a block of data to the cloud store.
        Returns the SHA-256 fingerprint hash of the uploaded data.
        If the exact same block was already uploaded, it reuses it (Deduplication).
        """
        digest = sha256_hex(data)
        self.stats.chunks_written += 1

        if digest in self._blobs:
            # Identical block already exists in cloud storage!
            self.stats.bytes_deduped += len(data)
        else:
            # New block! Save it in storage.
            self._blobs[digest] = data
            self.stats.unique_chunks += 1
            self.stats.unique_bytes += len(data)

        return digest

    # ---- DOWNLOAD PATH (When lazy-fetching data on demand) ----
    def get(self, digest: str) -> bytes:
        """
        Downloads a block of data from the cloud using its hash.
        Simulates internet network delay (ping + transfer time).
        """
        if digest not in self._blobs:
            raise KeyError(f"Error: Block with hash {digest} not found in remote storage!")

        data = self._blobs[digest]

        # Calculate network delay for downloading this piece of data
        wait_time = self.network.transfer_time(len(data))
        time.sleep(wait_time)  # Pause execution to simulate network transfer time

        # Update performance statistics
        self.stats.network_gets += 1
        self.stats.network_bytes_transferred += len(data)
        self.stats.network_time_s += wait_time

        return data
