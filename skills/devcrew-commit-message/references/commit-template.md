# Commit message

`type(task-id-or-area): concise description of the completed change`

Follow the project language and format.

Examples (each is a separate candidate):

```text
fix(DEMO-101): Removed duplicate search results
feat(DEMO-102): Added file type filtering to search
refactor(search): Simplified result sorting
docs(api): Corrected search parameter descriptions
feat(api)!: Removed the legacy search response format
```

The first example assumes duplicate results were a bug; a new deduplication capability may instead warrant `feat` when the diff establishes that intent.
