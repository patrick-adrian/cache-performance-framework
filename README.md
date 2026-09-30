# Cache Performance Framework

A parameterised cache in SystemVerilog, driven from Python, for measuring what
cache size, block size, associativity and replacement policy actually cost.

The RTL is a timed, blocking, single-outstanding-miss cache: it counts cycles,
not just hits. Python owns everything around it — trace generation, the
configuration matrix, builds, parallel runs, aggregation and plots — and carries
a reference model that the RTL is checked against access by access.

```
rtl/      the cache and its replacement policies
tb/       the testbench: streams a trace in, writes one CSV row out
py/       the cacheperf package and its tests
scripts/  toolchain provisioning
```

## Setup

Everything runs inside WSL. The script installs the toolchain, creates a
virtualenv outside the repo and installs the package into it.

```bash
bash scripts/setup_wsl.sh
source ~/.cache/cache-perf/venv/bin/activate
python -m cacheperf.preflight --smoke
```

The smoke test builds and runs a trivial Verilator model; if it passes, the
framework will build.

## Quick start

Four presets sweep one axis each and pair it with workloads that make the axis
visible:

```bash
cacheperf --preset size -o results/size.csv
cacheperf-report results/size.csv -o figures/size
```

`cacheperf` writes one CSV row per configuration. `cacheperf-report` reads that
CSV back, works out which axes actually vary, and draws a figure per axis per
metric. `--no-plots` prints the same numbers as tables.

## The four axes

Every table below is real output from the corresponding preset. Miss rate is
the fraction of accesses that missed; AMAT is cycles per access.

### Cache size

```bash
cacheperf --preset size -o results/size.csv
```

Nine capacities from 1 kB to 256 kB, 64-byte blocks, 4-way, LRU.

```
cache size                       1k      2k      4k      8k     16k     32k     64k    128k    256k
looping, 32 kB working set   0.0625  0.0625  0.0625  0.0625  0.0625  0.0026  0.0026  0.0026  0.0026
uniform random, 64 kB        0.9845  0.9690  0.9374  0.8743  0.7498  0.5008  0.0051  0.0051  0.0051
```

Two shapes of the same axis. The loop is a cliff: LRU is pessimal for a cyclic
reference pattern, so a working set one byte too large to fit misses at exactly
the rate a cache of zero bytes would, and the whole benefit arrives at once when
it fits. The random workload declines smoothly instead, because each doubling of
capacity captures a proportional share of a flat reuse distribution.

### Block size

```bash
cacheperf --preset block -o results/block.csv
```

A 16 kB 4-way cache with blocks from 8 to 256 bytes.

```
block size                      8      16      32      64     128     256
sequential walk            0.5000  0.2500  0.1250  0.0625  0.0312  0.0156
pointer chase              1.0000  1.0000  1.0000  1.0000  0.9699  0.9539
```

The sequential walk halves its miss rate on every doubling — its misses are
exactly footprint/block, and nothing else about the cache matters. The pointer
chase is the control: a random permutation of 64-byte nodes has no spatial
locality to find, so a bigger block fetches bytes that are never read. Block
size is not a free win; it is a bet on spatial locality.

### Associativity

```bash
cacheperf --preset ways -o results/ways.csv
```

An 8 kB cache of 64-byte blocks, 1 through 16 ways.

```
associativity                   1       2       4       8      16
strided, 8 kB stride       1.0000  1.0000  1.0000  1.0000  0.0020
tiled matmul, 64x64        0.0863  0.0301  0.0149  0.0149  0.0149
```

The strided trace touches 16 addresses, all of which land in one set for any of
these geometries. Below 16 ways the set cannot hold them and every access
misses; at 16 ways they all fit and the miss rate collapses to the compulsory
misses alone. This is the pure conflict-miss cliff. The tiled multiply is the
realistic case: three streams contending for the same sets, most of the benefit
banked by 4 ways and nothing left after that.

### Replacement policy

```bash
cacheperf --preset policy -o results/policy.csv
```

```
policy                       fifo     lru    plru  random
looping, 4-way             0.0625  0.0625  0.0625  0.0617
looping, 8-way             0.0625  0.0625  0.0625  0.0616
thrash, 4-way              0.5558  0.5558  0.5557  0.2247
thrash, 8-way              1.0000  1.0000  0.9996  0.2257
```

On the loop, policy is worth nothing: the reference pattern never revisits a
block before it is evicted, so there is no decision to get right. On the thrash
trace — nine blocks cycling through an eight-way set — LRU evicts precisely the
block needed next and never hits once, and FIFO and tree PLRU do the same.
Random replacement guesses wrong most of the time and is four times better for
it. Policy matters exactly where the working set overhangs a set by a little.

## Building a sweep

Any axis can be given as a comma-separated list; the sweep is their cross
product. `--trace` is repeatable rather than comma-separated, because a trace
spec contains commas.

```bash
cacheperf \
  --size 4k,8k,16k,32k \
  --block 32,64 \
  --ways 1,2,4,8 \
  --policy lru,plru,fifo,random \
  --trace 'looping:working_set=16k,accesses=200000,write_ratio=0.3' \
  --trace 'tiled_matmul:n=128,tile=16' \
  -o results/matrix.csv
```

| Flag | Axis | Notes |
| --- | --- | --- |
| `--size` | capacity | `4096`, `4k`, `1M` |
| `--block` | block size | bytes, power of two, at least 4 |
| `--ways` | associativity | power of two; 1 is direct mapped |
| `--policy` | replacement | `lru`, `plru`, `fifo`, `random` |
| `--write-policy` | write hits | `write_back`, `write_through` (`wb`, `wt`) |
| `--write-allocate` | write misses | `1` allocates, `0` writes around |
| `--miss-penalty` | timing | cycles to fetch a block |
| `--hit-latency` | timing | cycles to resolve an access |
| `--seed` | random policy | non-zero; it is the LFSR state |
| `--addr-width` | address bits | 8 to 32 |

Useful extras: `--dry-run` prints the matrix and the number of builds without
running anything, `--classify` adds compulsory/capacity/conflict columns,
`-j` caps concurrent simulations and `--force-build` discards cached binaries.

Combinations that describe no real cache — a capacity that leaves a fractional
number of sets, say — are dropped with a warning rather than aborting the sweep,
because any rectangular matrix over these axes contains some.

## Traces

A trace is a spec string, `name` or `name:key=value,key=value`. Byte counts
accept `k` and `M` suffixes.

| Generator | What it exercises |
| --- | --- |
| `sequential:footprint,stride,repeats` | block size, and nothing else |
| `strided:footprint,stride,repeats` | conflict misses, set mapping |
| `looping:working_set,accesses,stride` | capacity, with an LRU cliff |
| `uniform_random:footprint,accesses` | the no-locality lower bound |
| `pointer_chase:footprint,accesses,node_size` | spatial locality's absence |
| `thrash:ways,sets,block_bytes,repeats` | replacement policy |
| `tiled_matmul:n,tile,elem_size` | block size and associativity together |

All of them take `write_ratio` and `seed` where it makes sense. Traces are
generated once into `~/.cache/cache-perf/traces/` and reused; a generator is
deterministic given its spec, so the file for a spec is that spec's trace.

Real traces can be imported instead, from Dinero IV format or from
`valgrind --tool=lackey --trace-mem=yes`:

```python
from cacheperf import traces

traces.write_trace("app.hex", traces.import_lackey("lackey.out"))
```

The on-disk format is one hex word per line: the address in the low 32 bits and
bit 32 set for a write. That reserved bit is why `--addr-width` stops at 32.

## What gets built, and when

Cache geometry fixes array dimensions and address field widths, so it has to be
an elaboration-time parameter: every distinct size/block/ways/address-width
combination is its own Verilator build. Everything else — policy, write policy,
write allocation, miss penalty, hit latency, seed — is a plusarg on an
already-built binary. Sweeping all four runtime axes over a hundred values costs
zero builds.

Builds are cached under `~/.cache/cache-perf/build/` and keyed by the geometry
plus a content digest of every RTL and testbench source, so editing the RTL
invalidates every binary and editing a trace generator invalidates none. They
live outside the repo because a C++ build is thousands of small files and
`/mnt/c` is a 9p mount. Set `CACHEPERF_CACHE` to move them.

## Timing

The cache is blocking with one outstanding miss. An access costs:

| Outcome | Cycles |
| --- | --- |
| Hit | `hit_latency` |
| Miss, victim clean or invalid | `hit_latency + miss_penalty` |
| Miss, victim dirty | `hit_latency + 2 * miss_penalty` |
| Write miss, no write allocate | `hit_latency + miss_penalty` |

A dirty victim costs twice because the write-back drains before the fill starts.
AMAT is total cycles over total accesses, which is what the reports plot
alongside miss rate — a policy that trades a few hits for fewer write-backs can
win on AMAT while losing on miss rate.

## Correctness

`py/cacheperf/model.py` is the oracle: a functional cache model with its own
implementation of each policy. The RTL is correct insofar as it reproduces that
model's hit-or-miss decision on every single access, and `test_equivalence.py`
checks exactly that across geometries, policies, write policies, write
allocation, timing parameters and traces — the full sequence, not the totals,
since two caches can reach the same totals by different routes.

Two choices in the model are load-bearing, and the RTL follows them rather than
the other way round:

- Victim selection prefers invalid ways. A set that is not yet full takes its
  lowest invalid way and the policy is never consulted.
- The random policy's LFSR advances once per allocation, not once per cycle. A
  cycle-driven LFSR could not be predicted by an untimed model, which would make
  random replacement permanently unverifiable.

Cycle counts are checked twice over: against the model's own arithmetic, and in
directed tests against the table above computed by hand, so that a shared
misconception could not satisfy both.

## Tests

```bash
pytest                  # everything
pytest -m 'not rtl'     # skip anything that needs verilator
```

The RTL-backed tests build every geometry they need on first run and hit the
build cache afterwards.

## Adding a replacement policy

1. Add it to `Policy` in `config.py`. The enum value is the encoding the RTL
   sees, so it is the single definition shared by both languages.
2. Implement it as a `Replacement` subclass in `model.py` and register it in
   `_POLICIES`. This is the specification.
3. Add `rtl/repl_<name>.sv` with the same ports as the existing units, and a
   case arm in `replacement_unit.sv`.
4. Run `pytest py/tests/test_equivalence.py`. It will not pass until the two
   agree on every access.
