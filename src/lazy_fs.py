"""
lazy_fs.py
----------
WHAT THIS FILE DOES:
This is the core engine of Lazy Loading!
It exposes standard file operations (`getattr`, `readdir`, `open`, `read`).

KEY CONCEPTS IN SIMPLE WORDS:
1. Metadata Operations vs Data Operations:
   - `getattr()` (check file size/type): Metadata ONLY. Does NOT download any file data!
   - `readdir()` (list files in folder): Metadata ONLY. Does NOT download any file data!
   - `open()`    (open a file): Metadata ONLY. Validation check, NO downloads yet!
   - `read()`    (read file content): THIS is what triggers downloading!

2. How Lazy Read Works:
   When `read()` is called:
   a. Figure out which 256 KB chunk index contains the requested bytes.
   b. Check if that chunk exists in `LocalChunkCache`.
   c. If in cache: Return data instantly! (0 network calls).
   d. If NOT in cache: Download THAT specific chunk from `RemoteBlobStore`, save to cache, and return data!

3. Compare Eager vs Lazy:
   - Eager (`eager_load_all`): Downloads ALL 100% of files in image before container starts.
   - Lazy (`LazyImageFS`): Container starts instantly in ~10ms. Chunks are fetched ONLY when `read()` is called.
"""

from manifest import ImageManifest, CHUNK_SIZE
from blob_store import RemoteBlobStore
from local_cache import LocalChunkCache


class LazyImageFS:
    """
    Virtual Filesystem controller for Lazy Loading.
    Mirrors standard file system calls (like Linux VFS/FUSE callbacks).
    """

    def __init__(self, manifest: ImageManifest, blob_store: RemoteBlobStore, cache: LocalChunkCache):
        self.manifest = manifest     # Metadata index (Table of Contents)
        self.blob_store = blob_store # Remote Cloud Storage (S3 / Registry)
        self.cache = cache           # Local Machine Cache (RAM / SSD)

    # --- METADATA OPERATIONS (Instant! NEVER touch network or file data) ---

    def getattr(self, path: str) -> dict:
        """
        Returns metadata (size, permissions) for a file.
        Pure metadata operation — 0 network downloads!
        """
        entry = self.manifest.find(path)
        if entry is None:
            raise FileNotFoundError(f"File not found: {path}")
        return {"path": entry.path, "size": entry.size, "mode": entry.mode}

    def readdir(self) -> list[str]:
        """
        Lists all files in the container image.
        Pure metadata operation — 0 network downloads!
        """
        return [f.path for f in self.manifest.files]

    def open(self, path: str) -> str:
        """
        Opens a file for reading.
        Validates that the file exists in manifest.
        Pure metadata operation — NO file data is downloaded yet!
        """
        if self.manifest.find(path) is None:
            raise FileNotFoundError(f"File not found: {path}")
        return path

    # --- DATA READ OPERATION (The ONLY place where network fetching happens!) ---

    def read(self, path: str, offset: int = 0, length: int | None = None) -> bytes:
        """
        Reads byte range from a file on-demand:
        1. Finds which 256 KB chunk index ranges cover `[offset, offset + length]`.
        2. For each chunk needed:
           - Checks local cache first.
           - If missing, downloads ONLY that chunk from cloud store.
        3. Returns the exact requested bytes.
        """
        entry = self.manifest.find(path)
        if entry is None:
            raise FileNotFoundError(f"File not found: {path}")

        # If length is not specified, read to the end of the file
        if length is None:
            length = entry.size - offset

        # Determine which 256 KB chunk indices contain the requested byte range
        start_chunk = offset // CHUNK_SIZE
        end_chunk = (offset + length - 1) // CHUNK_SIZE if length > 0 else start_chunk

        out = bytearray()

        # Iterate over only the chunks required for this read request
        for chunk_idx in range(start_chunk, end_chunk + 1):
            digest = entry.chunk_hashes[chunk_idx]  # Get SHA-256 hash for this chunk

            # Step A: Check local cache first
            data = self.cache.get(digest)

            # Step B: Cache miss! Fetch chunk from cloud blob store over network
            if data is None:
                data = self.blob_store.get(digest)  # Simulated Network Call!
                self.cache.put(digest, data)        # Save to local cache for future reads

            out.extend(data)

        # Slice the bytearray to return the exact requested slice [offset : offset + length]
        chunk_start_offset = start_chunk * CHUNK_SIZE
        local_start = offset - chunk_start_offset
        return bytes(out[local_start : local_start + length])

    def read_whole_file(self, path: str) -> bytes:
        """Helper method: Reads an entire file from start to finish."""
        entry = self.manifest.find(path)
        return self.read(path, 0, entry.size)


def eager_load_all(manifest: ImageManifest, blob_store: RemoteBlobStore, cache: LocalChunkCache) -> None:
    """
    Simulates TRADITIONAL Docker loading (Eager Load):
    Downloads every single chunk of every file in the image BEFORE container starts,
    even if 95% of the files are never read by the application.
    """
    fs = LazyImageFS(manifest, blob_store, cache)
    for entry in manifest.files:
        fs.read_whole_file(entry.path)
