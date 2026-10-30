# <slug>: <one-line hypothesis>

- **Date:** YYYY-MM-DD
- **Author:** <name or agent, and the machine id from the result file>
- **Outcome:** success | failure | inconclusive
- **Branch / PR:** research/<slug> — <PR URL, filled in after `gh pr create`>
- **Result files:** research/results/<file>.json (every standard run you made)

## Hypothesis

What you expected to speed up, which scenarios, and why (profiling evidence, prior
ideas it builds on).

## What was done

The change, in a few sentences. Files touched. Anything surprising.

## Measurements

Paste the harness's `speedup of candidate vs main` and `vs lake` lines, the
`correctness` line, and the `verdict` lines verbatim. Mention caps (cpus, mem) and
repeat count. If you reran for noise, say so and list every result file.

## Conclusion

Why it worked or did not. What the next person should try, or avoid.
