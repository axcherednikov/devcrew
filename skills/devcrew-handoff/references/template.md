# Output templates

Use only the requested audience sections. A full handoff starts with a private summary, followed by QA, the client team, and the customer. Make each audience section ready to share on its own. The private summary is for the user only.

## Private summary

**What changed**

- …

**Key details**

- …

**Verified**

- … (only checks actually run)

**Limitations and remaining work**

- None / …

**Git**

- MR / commit / branch / UNKNOWN

## QA

### Changed behavior

…

### Scenarios to test

1. **Precondition:** … **Action:** … **Expected result:** …

### Critical cases

- … / None required by the available evidence.

### Unchanged behavior

- … / Omit this section unless it helps define the regression scope.

### Limitations

- … / None

## Frontend / client

### Public contract changes

- … / `NOT_APPLICABLE`: no public API or other client-facing contract changed.

### Required client updates

- … / No frontend or mobile changes are required.

### Compatibility

- … / `UNKNOWN`

## For the customer

### What we delivered

- …

### How it works now

…

### What you need to know

- … / None

## Technical verification (only when requested)

- Tests and static analysis actually run, relevant commit or MR, and known technical limitations.
