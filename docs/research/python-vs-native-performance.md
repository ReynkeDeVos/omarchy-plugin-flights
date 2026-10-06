# Python versus Rust, Zig, or C for the flight widget

Research date: 6 October 2026. Scope: the current implementation, not a rewrite benchmark.

**Recommendation:** keep Python for this widget unless profiling reveals a resource problem. A native implementation could reduce interpreter/import overhead and CPU work, but the current architecture suggests network waiting and refresh cadence will matter more to visible flight-status freshness. That is an inference; no live network requests or equivalent Rust/Zig/C implementations were benchmarked.

## What the code actually does

- The backend is Python: [FlightTracker.qml](../../FlightTracker.qml) launches `python3 bin/flight_status.py`. Rendering, interaction, and a 30-second leave-by countdown timer remain in QML. Rewriting the backend therefore would not directly accelerate QML rendering.
- Refreshes default to 60 seconds, with a 30-second minimum. A refresh cannot start while the previous helper is running; stdout is consumed when the helper finishes. [Source: refresh, Process and Timer definitions](../../FlightTracker.qml).
- Each leg fetches a dated FlightStats page, reads at most 2,500,000 bytes, extracts JSON, and normalizes a small record. Airborne legs can then resolve a route and try several ADS-B callsigns sequentially. [Source: fetch_page, enrich_with_adsb and fetch_leg](../../bin/flight_status.py), [route/callsign lookup](../../bin/adsb.py).
- Separate legs already run concurrently in a thread pool, with at most four workers. Route results are cached for seven days and pinned callsigns for three hours; prior journey records provide a failure fallback. [Source: main](../../bin/flight_status.py), [cache and lookup functions](../../bin/adsb.py).
- FlightStats uses an 18-second timeout and ADS-B/route lookups use 12 seconds. These are blocking-operation timeout settings, **not measured request durations or strict total-refresh deadlines**. Python documents `urlopen` timeout in terms of blocking operations such as connection attempts. [Code](../../bin/flight_status.py), [ADS-B code](../../bin/adsb.py), [urllib.request documentation](https://docs.python.org/3/library/urllib.request.html#urllib.request.urlopen).

## Offline measurements

Measured locally on an Intel Core Ultra 5 135U, x86_64, `/usr/bin/python3` 3.14.7, with warm OS caches and no network requests:

| Experiment | Median | Range |
| --- | ---: | ---: |
| 30 subprocess runs of `python3 bin/flight_status.py --help`, output discarded | 67.454 ms | 62.111–88.104 ms |
| Synthetic two-leg report; seven `timeit.repeat` batches of 1,000 reports | 0.214 ms/report | 0.210–0.229 ms/report |

The first includes process launch, imports, argparse/help, and shutdown; it is not a normal refresh. **The second is synthetic:** it decodes 12,800 bytes representing two FlightStats records with 100 track positions each, normalizes and enriches them using a fixed aircraft/route stub, computes journey/pickup/notifications with an identical previous snapshot, and encodes JSON. It excludes network, HTML extraction, disk cache/state I/O, locks, thread-pool creation, and UI rendering. These local measurements support low computation cost for the sample, not verified end-to-end I/O dominance or a measured native speedup.

## Where another language could help

Every refresh starts an interpreter and imports modules. A compiled executable avoids Python interpreter initialization and Python module imports, although it still has process and library initialization costs. Python supports `-X importtime` to measure import costs; startup should be measured separately from network waiting. [Python command-line documentation](https://docs.python.org/3/using/cmdline.html).

Native code can potentially reduce the cost of Python loops, object manipulation, and allocation. The size of that improvement for these particular functions is unmeasured. Some work is already native-backed in CPython: JSON scanning selects the `_json` accelerator when available, `math` mostly wraps the platform C math library, and TLS uses OpenSSL. A rewrite does not convert an entirely interpreted parser/cryptography/math stack into native code. [CPython JSON scanner source](https://github.com/python/cpython/blob/3.14/Lib/json/scanner.py), [math documentation](https://docs.python.org/3/library/math.html), [ssl documentation](https://docs.python.org/3/library/ssl.html).

Threads can overlap network waiting despite the ordinary CPython GIL; Python explicitly identifies threads as appropriate for concurrent I/O-bound tasks. The existing pool already applies this to separate legs. It does not overlap dependent lookups within one leg. [Threading documentation](https://docs.python.org/3/library/threading.html), [current main and lookup functions](../../bin/flight_status.py), [ADS-B lookups](../../bin/adsb.py).

## Why visible performance may barely change

Inference from the architecture: a faster backend cannot make the remote service respond earlier or update its underlying flight record sooner. It can reduce local computation and startup. Under the existing timer, a newly published change can also wait until the next poll regardless of implementation language. Live request timings and provider update cadence were not measured.

An illustrative model is `refresh time = startup + network critical path + local work`. Improving local work only reduces that component; the threaded network critical path is not the sum of every request across all legs.

Before a rewrite, profile startup, parsing/normalization, network phases, and peak memory separately. If responsiveness is the goal, evaluate showing cached status immediately and improving the request path. If repeated startup across monitors is the goal, evaluate sharing a helper/cache. These are candidate architectural changes, not changes made by this research.

## Deployment tradeoff

Rust, Zig, and C can produce executables; a native release would need a build/distribution arrangement for supported targets and any linked dependencies. Python currently uses only standard-library imports plus the local ADS-B module and requires `python3`. Rust documents optimized release executables, Zig documents target/cross-compilation support, and GCC documents compilation and linking stages. These sources establish deployment options, not a performance ranking between languages. [Current Python imports](../../bin/flight_status.py), [Rust release builds](https://doc.rust-lang.org/book/ch01-03-hello-cargo.html#building-for-release), [Zig overview](https://ziglang.org/learn/overview/), [GCC compilation options](https://gcc.gnu.org/onlinedocs/gcc/Overall-Options.html).

If a native rewrite becomes justified, benchmark an equivalent optimized implementation. This investigation provides no evidence for ranking Rust, Zig, and C on this workload.
