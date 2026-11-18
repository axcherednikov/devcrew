# Inline review comment

Use a separate paragraph under each heading.

```text
Problem:
...

Suggested fix:
...

Evidence:
...
```

Example from static inspection:

```text
Problem:
When the API fails, this function returns an empty result without an error. Callers cannot distinguish a failure from a successful empty response.

Suggested fix:
Return the original error or wrap it with %w.

Evidence:
Static inspection of the error branch at src/service.go:37 showed that it returns nil, nil. No test was run.
```
