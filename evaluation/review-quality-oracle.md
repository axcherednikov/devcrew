# Review quality oracle (host only)

This file is for the independent semantic evaluator. Never include it in a subject prompt, fixture workspace, reviewer context, or retained subject evidence.

## Expected findings

- `tenant-cache-isolation`: A only, P1. The shared cache is keyed by invoice ID alone. Trigger: tenant-a and tenant-b request `invoice-17` in the same cache. Consequence: tenant-b can receive tenant-a's invoice. Location: `get_invoice`, the key construction and cache lookup. Fix: include tenant ID in the key.
- `zero-limit-defaulted`: A only, P2. `raw or 25` treats explicit zero as absent. Trigger: `listing_limit(0)`. Consequence: invalid input is accepted as 25 instead of rejected. Location: `listing_limit`, default expression. Fix: default only for `None`.
- B has no actionable findings. The tuple key separates tenants and the comparisons reject zero and values above 100. Style preference is not actionable.

## Probe oracle

The shared-cache probe must call `get_invoice` twice with the same invoice ID and distinct tenants. A's calls contain one DB call and both results identify tenant-a; B's calls contain two and identify each tenant. `listing_limit(0)` returns 25 without raising in A because `raw or 25` replaces zero with the default; B raises `ValueError`. `listing_limit(None)` returns 25 and `listing_limit(100)` returns 100 for both. Probe markers establish observed behavior only; they are not a substitute for the semantic evaluation.
