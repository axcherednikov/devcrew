# ADR: supervised visible-chat handoff after a full brief

**Status:** accepted by the task owner in the source brief

**Context:** A complete, explicitly approved brief must continue in one visible
Codex chat, with its parent able to observe and assess work. Existing task and
session coordination already provides a Markdown inventory/action ledger and
native thread tools.

## Decision

Keep the transition in the brief and coordinate instructions. Reuse the
configured sessions inventory for a stable prepared action, actual native
receipt/IDs, cursor, evidence references, acceptance state and next action.
Require explicit approval for both chat creation and bounded follow-up
messages. Discover and use the host's real project/thread tools; reconcile
ambiguous outcomes before retrying. The parent checks delivered artifacts and
evidence while the owning workflow retains its normal verification and
independent review.

## Alternatives considered

- **Python bridge, daemon, or new persistent schema:** rejected because the
  native host tools already support the needed user-visible interaction and
  the existing Markdown ledger can record recovery state. A new runtime would
  duplicate host behavior and create a second source of truth.
- **Internal subagent instead of a visible chat:** rejected because it does not
  satisfy the user-visible-chat requirement and has different lifecycle and
  identity semantics.
- **Blind retry after timeout:** rejected because thread/message transport has
  no guaranteed idempotency. It can create duplicate execution or instructions.

## Consequences and limits

The ledger is reconciliation guidance, not a transaction engine; it cannot
promise exactly-once delivery. The parent only polls while active and does not
provide a daemon or background monitoring. Native thread completion is not
acceptance. If tool schemas, project identity, exact source access, approval,
or checkout cannot be verified, the safe result is a persisted blocker and
next action. Model/effort policy remains owned by the existing execution
workflow and is not inherited from the chat model.
