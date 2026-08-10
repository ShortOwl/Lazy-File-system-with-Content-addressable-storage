"""
manifest.py
-----------
Builds the "index" that Modal's video describes: a small metadata
structure that fully describes an image's file tree (paths, modes, sizes)
but contains only *pointers* (content hashes) to the actual data, which
lives in the RemoteBlobStore.

This is the equivalent of Nydus's RAFS metadata blob / Modal's ~5MB index.
It is cheap to load in full up front -- what's expensive is the file
*data*, which we deliberately do NOT touch here.
"""
import json
import os
from dataclasses import dataclass, field, asdict

from blob_store import RemoteBlobStore, sha256_hex

CHUNK_SIZE = 256 * 1024  # 256 KiB fixed-size chunking for this POC.
# Real systems (Nydus/eStargz/Modal) often use content-defined chunking
# (e.g. FastCDC) instead of fixed-size, so that inserting/removing a few
# bytes in a file doesn't shift every chunk boundary after it and destroy
# dedup. Fixed-size chunking is used here for simplicity; the interface
# below doesn't care which strategy produced the chunk list.


@dataclass
class FileEntry:
    path: str
    size: int
    mode: int
    chunk_hashes: list[str] = field(default_factory=list)


@dataclass
class ImageManifest:
    image_id: str
    files: list[FileEntry] = field(default_factory=list)

    def index_size_bytes(self) -> int:
        """Approximate serialized size of the metadata-only index."""
        return len(json.dumps([asdict(f) for f in self.files]).encode())

    def total_logical_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def find(self, path: str) -> FileEntry | None:
        for f in self.files:
            if f.path == path:
                return f
        return None


def chunk_bytes(data: bytes, chunk_size: int = CHUNK_SIZE):
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


def build_manifest(source_dir: str, blob_store: RemoteBlobStore, image_id: str) -> ImageManifest:
    """
    Simulates `docker build` + `docker push`: walk a local directory tree,
    content-address every file's chunks into the remote blob store, and
    emit a manifest containing only metadata + chunk hashes.
    """
    manifest = ImageManifest(image_id=image_id)
    for root, _dirs, files in os.walk(source_dir):
        for fname in files:
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, source_dir)
            with open(full_path, "rb") as fh:
                data = fh.read()
            chunk_hashes = [blob_store.put(c) for c in chunk_bytes(data)]
            manifest.files.append(
                FileEntry(
                    path=rel_path,
                    size=len(data),
                    mode=0o644,
                    chunk_hashes=chunk_hashes,
                )
            )
    return manifest
