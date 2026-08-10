"""
lazy_fs.py
----------
The heart of the POC. Exposes a POSIX-ish read API over an ImageManifest,
fetching file *data* lazily and only for the byte ranges actually
requested -- never eagerly pulling the whole image.

This class is deliberately shaped to mirror the callbacks a real FUSE
low-level driver implements, so the mapping to a production system is
obvious:

    LazyImageFS.getattr(path)   <->  FUSE getattr()   (stat a file)
    LazyImageFS.readdir(path)   <->  FUSE readdir()    (list a directory)
    LazyImageFS.open(path)      <->  FUSE open()       (no data touched yet)
    LazyImageFS.read(path,...)  <->  FUSE read()       (THIS is what
                                       triggers on-demand chunk fetches)

In a real deployment this class would be swapped for a Rust/Go/C FUSE
server process (or, on newer kernels, an in-kernel EROFS-over-fscache
implementation like Nydus RAFS v6), mounted read-only as the lower layer
of an OverlayFS, with a writable upper layer for container-local
mutations. See design.md for that mapping in detail.
"""
from manifest import ImageManifest, CHUNK_SIZE
from blob_store import RemoteBlobStore
from local_cache import LocalChunkCache


class LazyImageFS:
    def __init__(self, manifest: ImageManifest, blob_store: RemoteBlobStore, cache: LocalChunkCache):
        self.manifest = manifest
        self.blob_store = blob_store
        self.cache = cache

    # --- metadata-only operations: NEVER touch file data or the network ---
    def getattr(self, path: str) -> dict:
        entry = self.manifest.find(path)
        if entry is None:
            raise FileNotFoundError(path)
        return {"path": entry.path, "size": entry.size, "mode": entry.mode}

    def readdir(self) -> list[str]:
        return [f.path for f in self.manifest.files]

    def open(self, path: str) -> str:
        # A real "open" call is also metadata-only -- it just validates the
        # path exists and returns a handle. No bytes move yet.
        if self.manifest.find(path) is None:
            raise FileNotFoundError(path)
        return path

    # --- the only operation that pulls data over the network ---
    def read(self, path: str, offset: int = 0, length: int | None = None) -> bytes:
        entry = self.manifest.find(path)
        if entry is None:
            raise FileNotFoundError(path)
        if length is None:
            length = entry.size - offset

        start_chunk = offset // CHUNK_SIZE
        end_chunk = (offset + length - 1) // CHUNK_SIZE if length > 0 else start_chunk

        out = bytearray()
        for chunk_idx in range(start_chunk, end_chunk + 1):
            digest = entry.chunk_hashes[chunk_idx]
            data = self.cache.get(digest)
            if data is None:
                data = self.blob_store.get(digest)   # <-- network fetch, only now
                self.cache.put(digest, data)
            out.extend(data)

        # Trim to the exact requested byte range within the fetched chunks.
        chunk_start_offset = start_chunk * CHUNK_SIZE
        local_start = offset - chunk_start_offset
        return bytes(out[local_start : local_start + length])

    def read_whole_file(self, path: str) -> bytes:
        entry = self.manifest.find(path)
        return self.read(path, 0, entry.size)


def eager_load_all(manifest: ImageManifest, blob_store: RemoteBlobStore, cache: LocalChunkCache) -> None:
    """
    The traditional `docker pull && tar -x` behavior for comparison:
    fetch and cache every chunk of every file up front, whether or not
    the workload will ever touch it.
    """
    fs = LazyImageFS(manifest, blob_store, cache)
    for entry in manifest.files:
        fs.read_whole_file(entry.path)
