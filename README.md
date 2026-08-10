# Lazy-Loading Content-Addressed Container Filesystem

**Proof-of-Concept — Nishil Patel**

> **Live interactive demo →** [https://shortowl.github.io/Lazy-File-system-with-Content-addressable-storage/](https://shortowl.github.io/Lazy-File-system-with-Content-addressable-storage/)  
> **Deep-dive design doc →** [`design.md`](./design.md)

---

## The Problem in One Paragraph

Starting a container today means downloading and unpacking the **entire** image
before the process can run — even for files that will never be read. Modal's
team found that `import torch` only touches a few dozen files, yet ships inside
an 8 GiB image full of documentation, headers, and locale data that nothing
ever opens. On every cold start, on every node, you pay the full transfer and
decompression cost. For large ML images that can be **60+ seconds of pure
waiting**.

---

## The Fix: Three Ideas Stacked Together

```
┌─────────────────────────────────────────────────────────────────┐
│  1. Separate metadata from data                                  │
│     ↳ A tiny index (~KB) describes the image. Actual file data  │
│       is not touched at mount time.                             │
│                                                                  │
│  2. Make the filesystem lazy                                     │
│     ↳ Mount the index instantly. Only fetch a file's bytes      │
│       the moment a process actually calls read() on it.          │
│                                                                  │
│  3. Content-address the data                                     │
│     ↳ Name each file chunk by the SHA-256 of its bytes.         │
│       Identical bytes across any image → one stored copy,        │
│       one cached copy. Free dedup + free integrity checking.     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick Start — Run the PoC

**No external dependencies.** Pure Python 3 standard library only.

```bash
git clone <this-repo>
cd lazyfs_poc/src
python3 demo.py
```

### What it does

The demo simulates two container images that share a common base layer (like two
images both built `FROM python:3.11`), then runs four scenarios back-to-back:

| Step | Scenario | What it measures |
|------|----------|-----------------|
| 1 | **Push** both images to a content-addressed blob store | Deduplication at build/push time |
| 2 | **Eager load** Image A (today's docker/k8s default) | Download everything before starting |
| 3 | **Lazy load** Image A cold — workload reads only its "hot" files | Download only what the workload actually touches |
| 4 | **Warm start** — same workload, same node, second run | 100% served from local cache |
| 5 | **Cross-image dedup** — Image B reads a base-layer file cached by Image A | Zero extra network bytes |

### Real output from this machine

```
======================================================================
STEP 1: push (build manifests + content-address into blob store)
======================================================================
Image A logical size:  35.3MB  (index only: 34.8KB)
Image B logical size:  32.8MB  (index only: 31.8KB)
Sum of logical bytes across both images: 68.1MB
Unique bytes actually stored (dedup):    58.1MB
Storage saved by content-addressing:      10.0MB (14.7% smaller)

======================================================================
STEP 2: EAGER load of image A (pull + unpack everything, then start)
======================================================================
Time to become ready:     0.627s
Network bytes transferred: 35.3MB

======================================================================
STEP 3: LAZY load of image A (mount index instantly, fetch on demand)
======================================================================
Time to mount + list directory (metadata only): 0.01ms
Time to become ready (cold, workload-driven fetch): 0.016s
Network bytes transferred: 832.0KB

--> Lazy load was 38.4x faster and transferred 97.7% fewer bytes than eager load.

======================================================================
STEP 4: WARM start (same node, chunks already cached locally)
======================================================================
Time to become ready (warm): 0.10ms
Local cache hit rate: 41.7%  (hits=5, misses=7)

======================================================================
STEP 5: A DIFFERENT image (B) reads a base-layer file already cached
======================================================================
Image B reads 'shared/lib_5.so' (same bytes/hash as an image-A file) in 0.05ms
Extra network bytes needed: 0.0B (served entirely from local cache)
```

**Key results:**
- ⚡ **38× faster** startup (lazy vs eager cold start)
- 📉 **97.7% less network** transferred on cold start
- 🔥 **0.10ms** warm start — entirely from local cache
- ♻️ **0 extra bytes** when a second image reads a shared file already cached

---

## Code Structure

```
src/
├── blob_store.py   # Simulated S3-like content-addressed remote storage
│                   # with configurable network latency + bandwidth
├── manifest.py     # Image build/push — chunk files, SHA-256-address them,
│                   # build the metadata index (~KB, not MB)
├── local_cache.py  # Node-local LRU chunk cache (capacity-bounded)
├── lazy_fs.py      # The core: getattr / readdir / read callbacks —
│                   # the FUSE server's logic, without the FUSE mount
└── demo.py         # End-to-end scenario tying all five modules together
```

Each file maps 1-to-1 to a real production component:

| `src/` file | What it replaces in production |
|-------------|-------------------------------|
| `blob_store.py` | AWS S3, GCS, or a registry's blob backend |
| `manifest.py` | `docker push` + image indexer (like Nydus's `nydus-image`) |
| `local_cache.py` | Node NVMe/SSD tiered cache (Modal uses mem → SSD → zonal CDN) |
| `lazy_fs.py` | FUSE server in Rust/Go (Modal) or in-kernel EROFS+fscache (Nydus) |
| `demo.py` | The container runtime integration test |

---

## Architecture Overview

```
  docker build / push
        │
        ▼
  ┌─────────────────────────┐
  │   Registry / Blob Store  │  ← content-addressed (SHA-256 chunk keys)
  │   (blob_store.py)        │    simulated latency: 1ms, BW: 150 MB/s
  └────────────┬────────────┘
               │ on-demand chunk fetch
               ▼
  ┌─────────────────────────┐
  │   Node-local LRU Cache   │  ← bounded capacity, survives container
  │   (local_cache.py)       │    restarts, shared across images
  └────────────┬────────────┘
               │
  ┌────────────▼────────────┐
  │   Lazy FS (lazy_fs.py)  │  ← reads metadata index → answers getattr/
  │   "Smart File Watcher"  │    readdir instantly; fetches chunks only
  │                         │    on read(), never before
  └────────────┬────────────┘
               │  (in production: FUSE lower-dir of an OverlayFS mount)
        container rootfs
```

**Why FUSE and not a real kernel mount in this PoC?**  
The sandbox this was built in has `/dev/fuse` but `libfuse` isn't installable
offline, and a real kernel mount adds engineering surface without changing the
core argument. The logic in `lazy_fs.py` is the exact set of callbacks a real
FUSE server would implement — wiring it to `pyfuse3`/`fusepy` (or a Rust
rewrite for production syscall overhead) is the next natural step.

---

## Key Design Decisions

### Chunking
Fixed-size chunks (used here for clarity). Production systems like Nydus use
**content-defined chunking** (FastCDC rolling hash) so chunk boundaries are
stable across minor file edits — one inserted byte doesn't destroy dedup for
the entire rest of the file. The `chunk_bytes` function in `manifest.py` is
isolated behind a single interface specifically so this can be swapped without
touching anything else.

### Cache Eviction
Plain **LRU** by recency (also what I used in a previous distributed-cache
project). A production system would weight by chunk size × reuse-across-images
— a tiny, hot, shared stdlib chunk is worth keeping over a large, cold,
single-image chunk of the same age.

### Integrity
Because a chunk's identity *is* a hash of its bytes, verification is
structural: fetch → recompute hash → reject on mismatch. Extend to a Merkle
tree over the whole index (what Nydus's RAFS metadata does) and a single root
hash attests the entire image, catching a compromised registry transparently.

### Network Failure
Pure demand-fetch is fragile on a bad connection. Mitigations: background
prefetch of the full image after mount (degrades to "eventually eager" rather
than crashing), per-chunk retry with backoff, and P2P fallback to a nearby
node that already has the chunk (what Dragonfly adds on top of Nydus).

---

## How This Compares to Production Systems

| System | Mechanism | Key tradeoff |
|--------|-----------|-------------|
| **This PoC** | FUSE callbacks in Python | Portable, no kernel deps, demonstrates the concept cleanly |
| **Modal** | Rust FUSE server + tiered content-addressed cache | Production-grade; ~26k syscalls for `import torch` — Rust removes the context-switch overhead |
| **Nydus (RAFS v6)** | In-kernel EROFS + fscache | Zero userspace round-trips per read; requires Linux 5.19+ |
| **AWS SOCI** | Seekable OCI index + lazy layer fetch | Stays within the OCI spec; works with existing registries |
| **eStargz** | Priority file list burned in at build time | Prefetch heuristics baked into the image, no runtime FS needed |

---

## What's Next (if taken to production)

1. **Wire into real FUSE** — call `pyfuse3` with the callbacks in `lazy_fs.py`
   and mount as the `lowerdir` of a real OverlayFS so it can host a running
   container process.
2. **Rewrite hot path in Rust/Go** — `import torch` triggers ~26,000 `read()`
   syscalls; each FUSE round-trip adds ~10µs, so Python overhead matters here.
3. **Content-defined chunking** — swap in FastCDC in `manifest.py` for better
   dedup across image versions.
4. **Prefetch traces** — log file access patterns per image, replay on next
   cold start to eliminate even the on-demand latency for predictable files.
5. **P2P layer** — pull missing chunks from peer nodes on the same cluster
   instead of always going back to the origin registry.

---

## References

- [Modal — Fast, lazy container loading](https://modal.com/blog/fast-container-loading) — the talk this PoC is based on
- [Nydus / RAFS — The evolution of image acceleration](https://www.cncf.io/blog/2022/10/26/nydus-a-dragonfly-sub-project-image-acceleration-service/) (CNCF blog)
- [AWS SOCI snapshotter — Lazy loading with Seekable OCI](https://aws.amazon.com/blogs/aws/aws-fargate-now-supports-lazy-loading-container-images/)
- [`containerd/nydus-snapshotter`](https://github.com/containerd/nydus-snapshotter) — production containerd integration
- [FastCDC paper](https://www.usenix.org/conference/atc16/technical-sessions/presentation/xia) — content-defined chunking algorithm used in production systems
