"""
manifest.py
-----------
WHAT THIS FILE DOES:
This file builds the "File Directory" (Image Manifest Index).

KEY CONCEPTS IN SIMPLE WORDS:
1. What is an Image Manifest?
   It is a tiny index (like a book's table of contents).
   It contains a list of all files in the container image:
   - File Name (e.g., app/main.py)
   - File Size (e.g., 1024 bytes)
   - Permissions (e.g., 0o644)
   - Chunk Hashes: A list of SHA-256 fingerprints pointing to where the actual file data lives.

2. Why is this fast?
   Downloading this index takes only milliseconds because it is very small (~30 KB).
   The actual file contents stay in cloud storage until requested!

3. Chunking (Fixed 256 KB blocks):
   Large files are split into small 256 KB blocks (chunks).
   Each chunk is uploaded separately and assigned its own unique SHA-256 hash.
"""

import json
import os
from dataclasses import dataclass, field, asdict

from blob_store import RemoteBlobStore, sha256_hex

# Default chunk size: 256 KiB (262,144 bytes) per block
CHUNK_SIZE = 256 * 1024


@dataclass
class FileEntry:
    """
    Metadata information for a single file inside the container image.
    Notice it does NOT contain the actual file content — only pointers (hashes)!
    """
    path: str                   # Relative path of the file (e.g., "app/main.py")
    size: int                   # Size of the file in bytes
    mode: int                   # POSIX file permissions (e.g., 0o644 for read/write)
    chunk_hashes: list[str] = field(default_factory=list) # List of SHA-256 hashes for data blocks


@dataclass
class ImageManifest:
    """
    The full index (Table of Contents) for an entire container image.
    Contains the list of all FileEntry objects.
    """
    image_id: str
    files: list[FileEntry] = field(default_factory=list)

    def index_size_bytes(self) -> int:
        """Calculates the tiny size of this metadata index (in bytes)."""
        return len(json.dumps([asdict(f) for f in self.files]).encode())

    def total_logical_bytes(self) -> int:
        """Returns the total combined size of all files listed in the image."""
        return sum(f.size for f in self.files)

    def find(self, path: str) -> FileEntry | None:
        """Look up metadata for a specific file path."""
        for f in self.files:
            if f.path == path:
                return f
        return None


def chunk_bytes(data: bytes, chunk_size: int = CHUNK_SIZE):
    """
    Helper function: Takes raw file data bytes and splits them into 256 KB chunks.
    Yields one chunk at a time.
    """
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


def build_manifest(source_dir: str, blob_store: RemoteBlobStore, image_id: str) -> ImageManifest:
    """
    Simulates `docker build` + `docker push`:
    1. Scans all files in a folder (`source_dir`).
    2. Splits each file into 256 KB data chunks.
    3. Uploads each chunk to the remote blob store (obtaining SHA-256 hashes).
    4. Builds and returns the lightweight `ImageManifest` metadata object.
    """
    manifest = ImageManifest(image_id=image_id)

    # Walk through all subdirectories and files in source_dir
    for root, _dirs, files in os.walk(source_dir):
        for fname in files:
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, source_dir)

            # Read raw file bytes
            with open(full_path, "rb") as fh:
                data = fh.read()

            # Split into chunks and upload each to the cloud blob store
            chunk_hashes = [blob_store.put(c) for c in chunk_bytes(data)]

            # Save file metadata entry into the manifest
            manifest.files.append(
                FileEntry(
                    path=rel_path,
                    size=len(data),
                    mode=0o644,
                    chunk_hashes=chunk_hashes,
                )
            )

    return manifest
