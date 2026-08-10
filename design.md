# Lazy-Loading Content-Addressed Container Filesystem
### Design note + proof of concept — Nishil Patel

## 1. The problem, restated

Starting a container today usually means: pull N gzipped tarball layers, decompress
them, and unpack every file into a rootfs directory, *before* the container's
entrypoint runs — even though most workloads only ever touch a small fraction of
those files (Modal's example: a container almost always ships `/usr/share/doc/*`,
which practically nothing reads). For an 8 GiB "fat" ML image, that unpack step
can take on the order of a minute, dominated by network transfer and
single-threaded decompression, and it's repeated on every cold start on every
node — that's the bottleneck Modal, Nydus/Dragonfly, and AWS's SOCI snapshotter
are all attacking from slightly different angles.

The fix all of them converge on has three orthogonal ideas stacked together:

1. **Separate metadata from data.** An image is represented as a small index
   (paths, sizes, modes, and a *pointer* to where each file's bytes live) that
   can be fetched and parsed in milliseconds. The bulk of the image — the file
   data — is not touched at mount time.
2. **Make the filesystem itself lazy.** Mount that index as a real (or
   virtual) filesystem, and only pull a file's bytes over the network the
   instant a process actually calls `read()` on it. Everything else stays a
   promise, never redeemed.
3. **Content-address the data.** Name each chunk of file data by the hash of
   its bytes rather than by path. This gives you (a) free deduplication —
   identical bytes anywhere in any image collapse to one stored copy and one
   cached copy — and (b) free integrity checking, since the hash you asked
   for is the hash you must get back.

Everything else (OverlayFS for the writable layer, tiered caching, P2P
distribution) is in service of making idea #2 fast and idea #3 pay off.

## 2. Target architecture (what a production system looks like)

```
                          ┌─────────────────────────┐
   docker build / push -> │  Registry / Blob Store   │  (S3-like, content-addressed)
                          └───────────┬─────────────┘
                                      │ chunk fetch, on demand
                                      ▼
                     ┌────────────────────────────────┐
                     │   Tiered content-addressed      │
                     │   cache: mem → local NVMe/SSD    │
                     │   → zonal cache → regional CDN   │
                     └───────────────┬────────────────┘
                                     │
                     ┌───────────────▼────────────────┐
                     │  FUSE server (Rust/Go)           │  <- serves getattr/
                     │  or in-kernel EROFS+fscache       │     readdir/read from
                     │  reads the ~KB/MB metadata index  │     the index + cache
                     └───────────────┬────────────────┘
                                     │  read-only mount (lowerdir)
                     ┌───────────────▼────────────────┐
                     │        OverlayFS union mount      │  <- upperdir = per-
                     │  lower = lazy FS, upper = tmpfs   │     container writable
                     └───────────────┬────────────────┘     scratch, discarded
                                     │                        on container exit
                              container rootfs
```

Two implementation lineages exist in the wild and are worth distinguishing:

| Approach | How it works | Examples |
|---|---|---|
| **Userspace (FUSE)** | A userspace daemon intercepts VFS calls via `/dev/fuse`, does the network fetch + cache lookup, returns bytes to the kernel, which returns them to the caller. Simple, portable, but every read pays a kernel↔userspace context-switch (Modal notes `import torch` alone is ~26k syscalls). | Modal's Rust FUSE server, early Nydus, gVisor-fronted setups |
| **In-kernel** | The chunk index format is made compatible with a real in-kernel filesystem (Nydus's RAFS v6 rides on **EROFS**), and on-demand fetch is delegated to the kernel's **fscache** subsystem instead of a userspace round trip. Faster, but requires a modern kernel (5.19+) and is more invasive to operate. | Nydus "EROFS over fscache" |

For a from-scratch build I'd start with the FUSE approach — it's portable
across kernels/cloud providers and is exactly what Modal shipped first — and
treat the in-kernel path as a later optimization once the format and
semantics are proven.

## 3. Key design decisions (and why)

**Chunking strategy.** Fixed-size chunking is simplest but a single byte
inserted near the start of a file shifts every subsequent chunk boundary and
kills dedup between "almost identical" files (e.g. two versions of the same
`.so`). Production systems (Nydus, restic, casync) use **content-defined
chunking** (rolling hash, e.g. FastCDC) so chunk boundaries are determined by
local content, not by a global offset — insertions only perturb chunks near
the edit. I used fixed-size chunking in the POC for clarity; the chunking
function is isolated behind one interface (`chunk_bytes`) specifically so it
can be swapped for CDC without touching the rest of the system.

**Cache eviction.** Node-local cache is finite and needs an eviction policy.
Plain LRU (used in the POC, and in a distributed-cache project I built
previously with the same pattern) is a reasonable baseline; a real system
would likely weight by chunk size and reuse-across-containers rather than
pure recency, since a small, hot, shared base-layer chunk is more valuable to
keep than a large, cold, single-use chunk of the same age.

**Integrity & supply chain.** Because chunk identity *is* a hash of its
content, verification is structural: after fetching a chunk, recompute its
hash and reject on mismatch. Extend this to the whole index with a Merkle
tree (this is exactly what Nydus's RAFS metadata does) so a single root hash
can attest to the entire image, catching a compromised registry or
man-in-the-middle without extra infrastructure.

**Consistency of the writable layer.** Never mutate the lazy, shared,
read-only layer. All writes go to a per-container OverlayFS `upperdir`
(tmpfs or ephemeral local disk) that's thrown away when the container exits.
This is what lets thousands of containers share one cached copy of a base
image's chunks safely.

**Availability / network failure.** A purely lazy system is fragile if the
network blips mid-run (Nydus's docs call this out explicitly). Mitigations:
background prefetch of the whole image immediately after mount (so a slow
network degrades to "eventually-eager" rather than blocking), retries with
backoff on individual chunk fetches, and falling back to peer nodes (this is
what Dragonfly's P2P layer is for — pulling a missing chunk from a nearby
node that already has it instead of round-tripping to the origin registry).

**Prefetching heuristics.** Pure demand-fetch is correct but leaves latency
on the table for files you can predict will be read (e.g. an interpreter's
own stdlib). A production system would log/replay access traces per image to
build a prefetch list, similar to eStargz's "priority" file list burned into
the image at build time.

## 4. What the POC demonstrates (and how to run it)

`src/` contains a small, dependency-free Python implementation of the read
path end to end:

| File | Real-system equivalent |
|---|---|
| `blob_store.py` | Registry / S3 blob backend, with simulated network latency+bandwidth |
| `manifest.py` | Image build/push — chunking + content-addressing + the metadata index |
| `local_cache.py` | Node-local cache tier (LRU, capacity-bounded) |
| `lazy_fs.py` | The FUSE server's `getattr`/`readdir`/`open`/`read` callbacks |
| `demo.py` | End-to-end scenario: push two images, eager vs. lazy load, warm start, cross-image cache reuse |

```
cd src && python3 demo.py
```

On a representative run (two ~35 MB synthetic images sharing a base layer,
where the simulated workload only touches its "hot" app files, like a real
process that never opens `/usr/share/doc/*`):

- **Push:** deduplication across the two images' shared base layer alone saved
  ~15% of total stored bytes (this only grows with more images sharing a base).
- **Eager load:** ~0.53s and 35.3 MB transferred to become "ready."
- **Lazy load:** ~0.02s and 832 KB transferred to become ready — **~27x
  faster, ~98% less network traffic**, because only the files the workload
  actually opened were fetched.
- **Warm start** (same node, same chunks): sub-millisecond, served entirely
  from the local cache.
- **Cross-image reuse:** a *second, different* image reading a shared
  base-layer file it never fetched itself gets a 100% local cache hit,
  because content-addressing made that chunk fungible across images.

This intentionally does **not** implement a real FUSE mount or OverlayFS —
the sandbox this was built in has `/dev/fuse` but no `libfuse` installable
offline, and standing up a real kernel mount adds engineering surface
without changing the argument being demonstrated: metadata/data separation
plus content-addressed caching is what buys the speedup, and that claim is
fully testable in userspace. If I were to take this further as a real
project, the next increment would be wiring `lazy_fs.py`'s callbacks into
`pyfuse3`/`fusepy` (or rewriting the hot path in Rust/Go for the syscall
overhead reasons above) and mounting it as the lower layer of a real
OverlayFS, so it could actually host a running container.

## 5. References

- Modal — *Fast, lazy container loading in Modal.com* (talk + writeup) and
  *Memory snapshots: Checkpoint/restore for sub-second startup*
- Modal — *Inside Modal Notebooks: How we built a cloud GPU notebook that
  boots in seconds* (Rust FUSE server, tiered content-addressed cache)
- Dragonfly / Nydus — RAFS format design docs, *The evolution of the Nydus
  Image Acceleration* (CNCF blog), `containerd/nydus-snapshotter`
- AWS — *Under the hood: Lazy Loading Container Images with Seekable OCI and
  AWS Fargate* (SOCI snapshotter)
