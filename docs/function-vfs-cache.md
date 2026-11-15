# Asynchronous VFS cache for standard Functions

The standard PHP, Python, JavaScript and compiled TypeScript profiles share
`sdks/php/runtime/filesystem.mjs`. Its historical directory name does not select
a PHP-only implementation. No application or phpBB changes are required. The
trusted processor worker implements protocol 10.

## Pool and refresh

The first invocation initializes an in-memory snapshot through the normal
authorized mount checkout. Warm invocations use that snapshot without waiting
for VFS acquisition, version checks or downloads. Requests that leave the
filesystem unchanged perform no VFS commit either. Application secrets and
database calls retain their invocation-scoped authorization.

Every ten seconds the trusted worker asynchronously checks an immutable
read-only VFS snapshot through an independent channel. Polling continues during
application I/O; another invocation does not reset the timer. Up to four 64 KiB
reads run concurrently, and all readers settle before releasing the snapshot.
Known SHA-256 content is reused and new content is verified. Each snapshot is
bounded to 8,192 entries and 128 MiB, with a 32 MiB per-file limit. Unchanged
bytes are shared between current and staged snapshots.

Refresh prepares a pending snapshot without touching the application filesystem.
Checkout applies it before the next invocation. Runtime identity and base-version
checks discard refreshes overtaken by another commit. Read-only traffic can overlap
refresh without starving it. Full snapshots
remove absent files even when VFS has lost its delta history. Cold startup and
explicit management/CLI operations retain a fresh checkout.

Reads use bounded eventual consistency. A successful background check keeps the
pool eligible for 30 seconds. Failed or slow refreshes never make a warm hit
wait; after that bound, new invocations fail promptly instead of serving an
indefinitely stale or revoked mount. An idle refresh can restore eligibility.
Restart or eviction discards the pool. Local filesystem scans, memory use and
background resource consumption still have a cost; network latency is removed
from warm non-writing requests, not from cold startup or acknowledged writes.

## Safe commits and authorization

When application execution changes files, directories, modes or mtimes, commit
acquires an exclusive write lease and compares its authoritative version with
the invocation's starting version. Only an exact match permits staging writes
and the normal atomic VFS commit. The response waits for acknowledgment. A
mismatch publishes no writes and fails with `FILESYSTEM_CONFLICT`; application
code is never automatically replayed. Failed invocations reconstruct their local
tree from the last known committed state before reuse.

SQL and VFS commits are separate systems. A rejected VFS commit cannot undo an
SQL transaction, outgoing message or other effect that already completed. This
is why conflicts must be surfaced without automatic application retries.

Manager issues a separate five-minute `filesystem-cache` purpose grant during
mount acquisition. The node consumes it before returning the response to the
guest. The normal invocation grant still closes at invocation end. Cache grants
permit only read-only acquire, list, read and abort; no writes, commits, arbitrary
secrets, sockets or Function calls. Each poll rechecks deployment revision, VFS
attachment, paths and the Vault field grant, then renews the restricted grant.
Manager obtains the mount credential itself. Each snapshot session closes after
its poll; release, eviction or disconnect cancels outstanding cache work.

## Profile parity

| Characteristic | PHP | Python | JavaScript | TypeScript |
| --- | --- | --- | --- | --- |
| Local warm file pool | Yes | Yes | Yes | JavaScript runtime |
| Independent asynchronous refresh | Yes | Yes | Yes | Same protocol |
| Updates at invocation boundaries | Yes | Yes | Yes | Same protocol |
| Version-checked atomic writes and local rollback | Yes | Yes | Yes | Same protocol |
| Source invalidation | OPcache files | Application modules and handlers | Module graph, blob URLs and handlers | Compiled module graph |
| HTTP, static assets, direct events and Kafka | Preserved | Preserved | Preserved | Preserved |
| Management and CLI success/failure semantics | Preserved | Preserved | Preserved | Preserved |
| Invocation-scoped SDK, secrets and sockets | Preserved | Preserved | Preserved | Preserved |
| Phase/operation timing | Yes | Yes | Yes | Same metrics |

Plain SDK Functions retain bundle assets and globals in their existing warm
runtime; they do not declare standard-profile VFS persistence. Native `mini-x64`
Functions use OS disks rather than this MEMFS adapter. Their filesystem contracts
are not silently changed by this feature.

## Verification

`function-runtime/test-vfs-cache.mjs` tests blocked background I/O, zero-network
warm reads, staged updates, safe writes, conflicts, rollback, expiration, lost
delta history, corrupt/incomplete downloads and memory bounds.

`backend/test_standard_profiles_parity.py` compiles an annotated TypeScript app
and runs the same HTTP, SDK, events, Kafka, management, CLI and source replacement
scenario in all four flavours. A second test uses the exact trusted processor
`WORKER_SOURCE` and real ten-second idle polling in Chrome. Manager and broker
tests check grants, renewal, revocation and isolation from invocation authority.

## Live measurements, 15 November 2026

These are measurements of the existing deployment before this cache candidate.

| Request | Public wall time | Runtime total | PostgreSQL channel read wait | VFS acquire + commit |
| --- | ---: | ---: | ---: | ---: |
| Cold | 38.766 s | Not in bounded log tail | Not measured | Not measured |
| Warm | 5.469 s | 4.709 s | 2.708 s | 1.340 s |
| Warm | 4.843 s | 4.142 s | 2.624 s | 1.073 s |

Those warm requests downloaded no files and changed no persistent files. In a
second three-request run, wall times were 9.016 / 5.015 / 5.453 seconds and
channel read waits were 3.658 / 2.636 / 3.057 seconds (9.351 seconds total).
The dedicated `phpbb` database's `pg_stat_database` counters increased by:

- 3 sessions and 29 committed transactions, with no rollbacks;
- 102.035 ms of `active_time`, versus 9,318.919 ms of session time;
- 5,535 shared-buffer hits and no physical block reads.

Server identity, postmaster start and statistics epoch were unchanged. The
counter sample is database-wide and can include concurrent forum traffic.
Nevertheless, it separates the observed SQL server activity from the much larger
runtime channel waits. `network.read` is relay/network waiting, not PostgreSQL
CPU time; read/write operation totals overlap and must not be added together.
No statistics were reset and no instrumentation was enabled. This server has
no `pg_stat_statements` preload/extension, and `track_io_timing` is off.

Public-only receipts are in `backend/build/vfs-cache-baseline-20261115.json`,
`vfs-cache-baseline-log-20261115.json`,
`vfs-cache-database-observability-20261115.json` and
`vfs-cache-database-baseline-20261115.json`. They do not establish a production
speedup from an uninstalled candidate.

## Coordinated rollout status

On 15 November the Function-only Node projection was applied and independently
verified on srv13 and srv14. Their existing workers were reloaded to processor
page revision `3970b8737a204034`; no new workers were registered. Existing
synthetic Python HTTP Functions returned the expected responses in both regions:

| Node | First call after reload | Warm call |
| --- | ---: | ---: |
| srv13 | 3.859 s | 0.422 s |
| srv14 | 4.000 s | 0.344 s |

These calls verify the new processor page and ordinary Python execution. The
same nine Function source/asset hashes are now installed and verified on srv12,
srv13 and srv14. The srv12 projection preserved the HA chat's independently
updated recovery module, native epoch and VM state. Its two existing workers
were reloaded to the same page revision; all three nodes report no stale pages.

Manager image `1.5.108` was published immutably but never activated. The HA
release subsequently required an additional recovery read-prefetch repair.
Published successor `1.5.109` inherits the exact verified `1.5.108` image and
includes that hash-pinned HA overlay. Its registry digest is
`sha256:c475899fbaa2a62d165f5bf167b9ff854ed9585275ac9cb716c49a1af959014e`.
The actual installer reconstruction matches Node archive
`d25065bc25b95f453d1570c0f8af826927d0209ef300759243c1492602c84d2f`.

The original HA backup completed naturally and passed physical restore
qualification before activation. Manager `1.5.109` is deployed with two healthy
replicas, both installer caches ready. The owning follow-up Terraform plan is
empty. Original PostgreSQL history, schema and unrelated core containers were
preserved. The original backup worker was never interrupted.

The four retained standard VFS fixtures on srv12 passed live cache hits,
acknowledged write/read/remove operations, failed-CLI rollback and external
VFS updates delivered asynchronously between invocations. Each external probe
restored its original synthetic note content, mode and mtime.

| Profile | Warm HTTP fixture wall time | Warm runtime total |
| --- | ---: | ---: |
| PHP | 203–266 ms | 1–5 ms |
| Python | 250–391 ms | 1–115 ms |
| JavaScript | 250–375 ms | 1–13 ms |
| Compiled TypeScript | 296–500 ms | 1–5 ms |

All those warm requests reported `fs.checkout=cache`, zero fetched files and
no foreground VFS acquire or commit. A JavaScript refresh took 5,416 ms while
an invocation completed in 2 ms inside the runtime. Its separate public API
wall time still includes routing/queueing variability; the guarantee concerns
the invocation's VFS path, not every other source of platform latency.

Live four-profile VFS acceptance ran on srv12, where the retained configured
VFS workloads exist. On srv13 and srv14, acceptance verifies identical deployed
framework hashes and actual regional worker execution; it does not claim twelve
independent regional VFS workload deployments.

The final warm-only phpBB sample returned HTTP 200 in 5.563 / 5.046 / 4.297 s.
All three runtime traces used the pool with zero foreground VFS acquire, read
or commit. Their database channel reads totaled 8.795 s. PostgreSQL's `phpbb`
counters increased by 131.961 ms active time, 3 sessions and 28 committed
transactions, with zero rollbacks and zero physical block reads. Database
identity, postmaster start and statistics epoch stayed unchanged. This remains
a small database-wide sample, not a controlled throughput benchmark. The much
larger channel waits remain the next performance bottleneck; SQL processing
does not explain them. The first request after worker reload still paid cold
startup: 41.594 s.

No phpBB source, configuration or bundle has been changed. Public SDK packages
remain unpublished; this release updates their bundled runtime integration
rather than their public APIs. Durable write responses still await commit
acknowledgment.

Release receipts and exact saved plans are retained under
`tf-coderforge-stack/build/vfs-cache-rollout-20261115`.

## Rollout ordering

Release restricted Manager sessions first, then the matched node guest assets
and protocol-10 processor page. Use the administrator processor reload API;
current invocations finish first. Do not start new processors or change phpBB,
its bundle, database or VFS data. Coordinate with any active Manager/HA release.

After rollout, verify `fs.checkout=cache` with no foreground filesystem operations
for warm non-writing requests, check an external update across idle refresh, and
repeat the public/database measurement using `live-vfs-timing.py --database-stats`.
