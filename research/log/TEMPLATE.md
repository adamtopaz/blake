# <slug>: <one-line hypothesis>

- **Date:** YYYY-MM-DD
- **Author:** <name or agent, and the machine id from the result file>
- **Outcome:** success | failure | inconclusive
- **Branch / PR:** research/<slug> — <PR URL>
- **Result file:** research/results/<file>.json

## Hypothesis

What you expected to speed up, which scenarios, and why (profiling evidence, prior
ideas it builds on).

## What was done

The change, in a few sentences. Files touched. Anything surprising.

## Measurements

Paste the harness's `speedup of candidate vs main` and `vs lake` lines, and the
`correctness` line. Mention caps (cpus, mem) and repeat count. If you reran for
noise, say so.

## Conclusion

Why it worked or did not. What the next person should try, or avoid.
