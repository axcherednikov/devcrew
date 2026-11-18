# template-short.md

Use the short brief in an issue tracker or for a product owner or other nontechnical reader. Describe the change in business terms. Keep column names, code, and file paths in the engineering brief.

Typical length: 30–50 lines.

---

<Title> (<back | front | fullstack | infra>)

<⚠️ Depends on …, if the task has prerequisites.>

<⚠️ Coordinate with team …, if the change affects their scope.>

Goal

<In 1–3 sentences, explain who benefits, what they receive, and why it matters. Use everyday language.>

Scope of work

- <Change 1 as the user will experience it>
- <Change 2>
- <Change 3>

Rationale

<In 2–4 sentences, describe the problem, alternatives considered, reasons for rejecting them, and the main trade-off.>

Outcome

- <Observable outcome 1 and what it makes possible>
- <Outcome 2>
- <Outcome 3>

Out of scope

- <Item 1 and its destination: another task, deferred work, or a decision to revisit when needed>
- <Item 2>
- <Item 3>

Estimate: <X–Y hours>

---

## Guidance for the author

- **Title.** Use a short, descriptive title with a scope tag in parentheses: `back`, `front`, `fullstack`, `infra`, or `qa`.
- **⚠️ notices.** Include dependencies or coordination when they affect the work.
- **Goal.** Explain in one paragraph why the task exists. If this takes more space, consider splitting the task.
- **Scope of work.** Use action verbs such as “add”, “allow”, or “introduce”. “A new table for X” may be appropriate for the audience; individual `<column_name>: <type>` details belong elsewhere.
- **Rationale.** Explain why this approach was chosen. Mention alternatives considered and any secondary technical benefit.
- **Outcome.** Describe the resulting state in the present tense, e.g. “Customers can open page X in their account” or “The catalog displays Y correctly.”
- **Out of scope.** State where each item belongs, e.g. “Bulk operations will get a separate task once requirements are available”; “The audit log is outside the MVP”; “The frontend team will build the admin UI using the completed endpoints.”
- **Estimate.** Give a range in hours. If working with an AI assistant changes the estimate, use “X–Y hours solo / N–M hours with AI.”

## Common mistakes

- Including database column names, internal types, file paths, or class names.
- Using internal jargon such as “entity”, “repository”, or “migration” when “table”, “storage”, or “schema change” conveys the meaning.
- Writing long bullets. Keep each to 1–3 lines.
- Leaving “TODO: clarify” notes. Resolve material questions before handing over the brief.
- Using Markdown tables where a short list is easier to read.
