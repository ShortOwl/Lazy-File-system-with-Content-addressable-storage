"""
demo.py
-------
End-to-end scenario that mirrors the video's narrative:

  1. Build two "container images" that share a common base layer (like two
     images both built FROM python:3.11) plus some image-specific files.
     Push them to a shared content-addressed RemoteBlobStore --> shows
     deduplication at push time.

  2. Compare EAGER load (today's docker/k8s behavior: pull + unpack
     everything before the container can start) vs LAZY load (mount the
     ~KB-sized index instantly, only fetch chunks the workload actually
     touches).

  3. Run the *same* workload again on the same node ("warm start") to show
     the local cache eliminating almost all network traffic.
"""
import os
import random
import shutil
import time

from blob_store import RemoteBlobStore, NetworkProfile
from manifest import build_manifest
from local_cache import LocalChunkCache
from lazy_fs import LazyImageFS, eager_load_all


WORKDIR = "/tmp/lazyfs_demo"


def _rand_bytes(n: int, seed: int) -> bytes:
    rnd = random.Random(seed)
    return bytes(rnd.getrandbits(8) for _ in range(n))


def make_synthetic_image(root: str, shared_dir: str, hot_files: list[str],
                          n_doc_files: int, doc_file_size: int, n_hot: int, hot_file_size: int):
    """
    Builds a directory tree shaped like a real container image:
      - shared/...     <- identical across images (base OS / interpreter),
                           copied in from `shared_dir` so bytes match exactly
      - usr/share/doc/* <- lots of files a workload almost never reads
      - app/*           <- a handful of "hot" files the workload DOES read
    """
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)

    # shared/base-layer files: copy so content (and thus hashes) match
    dst_shared = os.path.join(root, "shared")
    shutil.copytree(shared_dir, dst_shared)

    # rarely-touched doc/library files, unique per image
    doc_dir = os.path.join(root, "usr", "share", "doc")
    os.makedirs(doc_dir)
    for i in range(n_doc_files):
        with open(os.path.join(doc_dir, f"doc_{i}.dat"), "wb") as fh:
            fh.write(_rand_bytes(doc_file_size, seed=hash((root, "doc", i)) & 0xFFFFFFFF))

    # hot files: what the "workload" actually reads on startup
    app_dir = os.path.join(root, "app")
    os.makedirs(app_dir)
    for i in range(n_hot):
        with open(os.path.join(app_dir, f"hot_{i}.py"), "wb") as fh:
            fh.write(_rand_bytes(hot_file_size, seed=hash((root, "hot", i)) & 0xFFFFFFFF))
    return root


def build_shared_base(shared_root: str, n_files: int, file_size: int):
    if os.path.exists(shared_root):
        shutil.rmtree(shared_root)
    os.makedirs(shared_root)
    for i in range(n_files):
        with open(os.path.join(shared_root, f"lib_{i}.so"), "wb") as fh:
            fh.write(_rand_bytes(file_size, seed=1000 + i))  # fixed seed -> identical across images
    return shared_root


def human(n_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(n_bytes) < 1024:
            return f"{n_bytes:,.1f}{unit}"
        n_bytes /= 1024
    return f"{n_bytes:,.1f}TB"


def run():
    random.seed(42)
    shutil.rmtree(WORKDIR, ignore_errors=True)
    os.makedirs(WORKDIR)

    # --- 1. Build two images sharing a base layer -------------------------
    shared_root = build_shared_base(os.path.join(WORKDIR, "shared_base"), n_files=20, file_size=512 * 1024)
    image_a_root = make_synthetic_image(
        os.path.join(WORKDIR, "image_a"), shared_root, hot_files=[],
        n_doc_files=200, doc_file_size=128 * 1024, n_hot=5, hot_file_size=64 * 1024,
    )
    image_b_root = make_synthetic_image(
        os.path.join(WORKDIR, "image_b"), shared_root, hot_files=[],
        n_doc_files=180, doc_file_size=128 * 1024, n_hot=5, hot_file_size=64 * 1024,
    )

    blob_store = RemoteBlobStore(NetworkProfile(latency_s=0.001, bandwidth_bytes_per_s=150 * 1024 * 1024))

    print("=" * 70)
    print("STEP 1: push (build manifests + content-address into blob store)")
    print("=" * 70)
    manifest_a = build_manifest(image_a_root, blob_store, image_id="app:image-a")
    manifest_b = build_manifest(image_b_root, blob_store, image_id="app:image-b")

    logical_total = manifest_a.total_logical_bytes() + manifest_b.total_logical_bytes()
    physical_total = blob_store.stats.unique_bytes
    print(f"Image A logical size:  {human(manifest_a.total_logical_bytes())}  "
          f"(index only: {human(manifest_a.index_size_bytes())})")
    print(f"Image B logical size:  {human(manifest_b.total_logical_bytes())}  "
          f"(index only: {human(manifest_b.index_size_bytes())})")
    print(f"Sum of logical bytes across both images: {human(logical_total)}")
    print(f"Unique bytes actually stored (dedup):    {human(physical_total)}")
    print(f"Storage saved by content-addressing:      {human(logical_total - physical_total)} "
          f"({100 * (1 - physical_total / logical_total):.1f}% smaller)")

    # --- 2. EAGER load of image A (today's docker/k8s behavior) -----------
    print()
    print("=" * 70)
    print("STEP 2: EAGER load of image A (pull + unpack everything, then start)")
    print("=" * 70)
    eager_cache = LocalChunkCache(capacity_bytes=1024 * 1024 * 1024)
    eager_blob_store = RemoteBlobStore(NetworkProfile(latency_s=0.001, bandwidth_bytes_per_s=150 * 1024 * 1024))
    build_manifest(image_a_root, eager_blob_store, image_id="app:image-a")  # re-push to a clean store
    t0 = time.perf_counter()
    eager_load_all(manifest_a, eager_blob_store, eager_cache)
    t1 = time.perf_counter()
    print(f"Time to become ready:     {t1 - t0:.3f}s")
    print(f"Network bytes transferred: {human(eager_blob_store.stats.network_bytes_transferred)}")

    # --- 3. LAZY load of image A: mount instantly, run the real workload --
    print()
    print("=" * 70)
    print("STEP 3: LAZY load of image A (mount index instantly, fetch on demand)")
    print("=" * 70)
    lazy_cache = LocalChunkCache(capacity_bytes=1024 * 1024 * 1024)
    lazy_blob_store = RemoteBlobStore(NetworkProfile(latency_s=0.001, bandwidth_bytes_per_s=150 * 1024 * 1024))
    build_manifest(image_a_root, lazy_blob_store, image_id="app:image-a")

    t_mount0 = time.perf_counter()
    fs = LazyImageFS(manifest_a, lazy_blob_store, lazy_cache)
    fs.readdir()  # walking the tree is metadata-only -- doesn't touch data
    t_mount1 = time.perf_counter()
    print(f"Time to mount + list directory (metadata only): {1000 * (t_mount1 - t_mount0):.2f}ms")

    # Simulate the workload: it only reads the 5 "hot" app files plus one
    # shared base-layer file (e.g. loading a shared library at startup),
    # never the 200 doc files -- this is the realistic case the video
    # describes (torch import touches a few dozen files, not
    # /usr/share/doc/*).
    first_shared_file = next(f.path for f in manifest_a.files if f.path.startswith("shared/"))
    t2 = time.perf_counter()
    for entry in manifest_a.files:
        if entry.path.startswith("app/"):
            fs.read_whole_file(entry.path)
    fs.read_whole_file(first_shared_file)
    t3 = time.perf_counter()
    print(f"Time to become ready (cold, workload-driven fetch): {t3 - t2:.3f}s")
    print(f"Network bytes transferred: {human(lazy_blob_store.stats.network_bytes_transferred)}")

    speedup = (t1 - t0) / (t3 - t2 + (t_mount1 - t_mount0))
    savings = 1 - (lazy_blob_store.stats.network_bytes_transferred / eager_blob_store.stats.network_bytes_transferred)
    print(f"\n--> Lazy load was {speedup:.1f}x faster and transferred "
          f"{100 * savings:.1f}% fewer bytes than eager load.")

    # --- 4. WARM start: run the same workload again on the same node ------
    print()
    print("=" * 70)
    print("STEP 4: WARM start (same node, chunks already cached locally)")
    print("=" * 70)
    t4 = time.perf_counter()
    for entry in manifest_a.files:
        if entry.path.startswith("app/"):
            fs.read_whole_file(entry.path)
    t5 = time.perf_counter()
    print(f"Time to become ready (warm): {1000 * (t5 - t4):.2f}ms")
    print(f"Local cache hit rate: {100 * lazy_cache.hit_rate:.1f}%  "
          f"(hits={lazy_cache.hits}, misses={lazy_cache.misses})")

    # --- 5. Second container reading a SHARED (base-layer) file -----------
    print()
    print("=" * 70)
    print("STEP 5: A DIFFERENT image (B) reads a base-layer file already cached")
    print("=" * 70)
    fs_b = LazyImageFS(manifest_b, lazy_blob_store, lazy_cache)  # note: shares lazy_cache with image A!
    shared_file = next(f.path for f in manifest_b.files if f.path.startswith("shared/"))
    net_bytes_before = lazy_blob_store.stats.network_bytes_transferred
    t6 = time.perf_counter()
    fs_b.read_whole_file(shared_file)
    t7 = time.perf_counter()
    net_bytes_after = lazy_blob_store.stats.network_bytes_transferred
    print(f"Image B reads '{shared_file}' (same bytes/hash as an image-A file) in {1000*(t7-t6):.2f}ms")
    print(f"Extra network bytes needed: {human(net_bytes_after - net_bytes_before)} "
          f"({'served entirely from local cache' if net_bytes_after == net_bytes_before else 'had to fetch'})")


if __name__ == "__main__":
    run()
