# monitor-wake: end the build when its last job finishes, not at the monitor's next tick

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-30-monitor-wake.md
- **Scenarios improved:** noop, leaf, swarm (on `wide`)

## Problem

Lake's build monitor (`Monitor.loop` in `Lake/Build/Run.lean`) runs on the main
thread while the build's jobs run on the task pool. Each iteration scans the
jobs, reports the finished ones, renders progress, and then sleeps the rest of
`updateFrequency`, which is 100 ms. Only a scan that finds no unfinished jobs
(and an empty job queue) ends the build. So every `lake build` ends 0-100 ms
after its last job actually finished, waiting for the next tick.

Seen in `strace -f -ttt` of `lake build Corpus.M00500` on `medium-wide` (a module
with a handful of dependencies): all files read and checked 45 ms after exec,
then 89 ms blocked in a futex, exit at 134 ms. Stock Lake behaves the same.

## Mechanism

`Monitor.sleep` now takes the unfinished jobs of the last scan and calls
`waitUntil`, which returns when all of them have finished or when the tick's
deadline (`lastUpdate + updateFrequency`) passes, whichever comes first. It polls
`IO.hasFinished` in 1 ms slices, advancing a cursor past finished jobs, so a
tick costs one task-state read per job that finished plus one per 1 ms slice.
Progress is still rendered once per loop iteration, and jobs are reported by
the same scan in the same order, so the output is unchanged except that the
final scan happens sooner. Nothing about what is built or how changes, so
artifacts are trivially identical.

## Where it lives

`Lake/Build/Run.lean`: `Monitor.waitUntil`, `Monitor.sleep`, and its call in
`Monitor.loop`.

## Measured gains

Standard sweep on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), medium
corpora, result `research/results/20261030T151332Z-2e698309-4bc612e.json`:

```
speedup of candidate vs main: cold=1.003x, noop=1.542x, leaf=1.227x, swarm=1.027x, hub=1.006x, primary_geomean=1.182x
speedup of candidate vs lake: cold=1.005x, noop=1.583x, leaf=1.257x, swarm=1.023x, hub=1.004x, primary_geomean=1.196x
```

Improved: `noop` on all corpora (paired 1.579x deep, 1.518x layered, 1.594x wide),
`leaf` on all corpora (1.138x, 1.158x, 1.324x), and `swarm` on `wide` (1.070x).

By hand on `medium-wide` (20 alternating pairs against stock Lake, CPUs 16-31):
`lake build Corpus.M00500` 0.035 s vs Lake's 0.147 s (4.2x); a no-op of the whole
library 0.131 s vs 0.157 s (1.24x).

## Caveats and follow-ups

- The 1 ms polling slice means the monitor thread wakes ~1000 times a second
  while a build runs. That costs microseconds per wake (well under 0.1% of the
  CPUs) and could be replaced by waiting on a combined task if it ever matters.
- What remains of a 1000-module no-op (~0.1 s after startup) is real work, and
  it is mostly serial: ~0.28 s of CPU in ~0.13 s of wall time on 16 CPUs, and
  ~0.22 s on a single CPU. The trace checks seem to proceed along the import
  DAG, one job continuation after another. Finding that serial chain is the
  next lever for `noop` and `leaf`, and it grows with the depth of the DAG.
