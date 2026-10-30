# <slug>: <title>

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/YYYY-MM-DD-<slug>.md
- **Scenarios improved:** noop / leaf / swarm / hub / cold

## Problem

What was slow and why (with the profiling evidence that showed it).

## Mechanism

How the change makes it faster. Be concrete: data structures, process model,
what work is avoided or reordered, why artifacts stay identical.

## Where it lives

Files and functions, so the next person can find and extend it.

## Measured gains

Harness speedup lines vs `main` and vs stock Lake, the corpora, caps, machine id.

## Caveats and follow-ups

Limits of the approach, risks, and ideas it enables.
