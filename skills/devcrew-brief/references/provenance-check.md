# Check the sources of requirements

Apply this check to every brief format before delivery and before a final executor dry run, if one is needed. A package can be internally consistent and still invent a feature, a constraint or a supposed customer decision.

## Mark sources while writing

Give every substantive requirement and decision an ID and a source:

- `[customer]` — link the supplied document or ticket, or quote a short, exact excerpt from the request in context. Attribute your own conclusions to yourself.
- `[from code]` — give the path, symbol and line inspected. Record the revision and any material uncommitted changes. A function's existence does not establish its behavior.
- `[assumption]` — describe an implementation choice that preserves established requirements and behavior. Explain its basis and distinguish an engineering conclusion drawn from confirmed facts from an untested hypothesis. A justified technical choice does not become a user requirement. Architectural significance alone does not require approval. For restoration, follow the verified behavior of the original under the [engineering policy](../../devcrew-task/references/engineering-policy.md#understand-first-choose-the-smallest-complete-solution). If evidence is incomplete, continue the available investigation. Bring a material unresolved requirement or risk to compatibility, data, security or irreversible outcomes to the user. An assumption label cannot resolve that question or supply missing confirmation.

The [Russian brief template](ru/template-main.md) uses localized names for the same three sources in the same order. Keep the source meaning and requirement IDs when translating or reviewing an existing brief.

For a short task, a source table in the main file suffices. A larger package may use `decisions.md` and reference its IDs from individual requirements. Keep technical citations in the main file and make sure the business summary accurately reflects those requirements. One attribution per claim is sufficient.

## Inputs and checks

Give the reviewer the full package, the decision table and relevant source excerpts. Supply specific evidence in place of a conversation transcript. If an independent context is unavailable, identify the check as a self-review.

1. Trace each requirement and added mechanism to a source. A cache, cron job, new entity or endpoint without one may expand scope.
2. Verify each `[customer]` label against the actual source. An author's statement in `decisions.md` alone is insufficient.
3. Follow each `[from code]` label to verified behavior at the specified path. Distinguish a documented external API contract from an observation of a live call.
4. Check that assumptions remain distinguishable from mandatory user requirements throughout the package.
5. When a decision changes, remove its earlier version from the contract, design, plan, test scenarios and acceptance criteria.

For each finding, give the requirement or file, claimed source, supporting evidence or evidence gap, and the correction needed. A missing source warrants “unconfirmed.” A finding of “incorrect” requires contrary evidence.

Correct misattribution or remove an unsupported addition. Look first in relevant code, tests and documentation. Ordinary implementation decisions that preserve confirmed behavior may remain as explicit assumptions. If material ambiguity persists, ask a short, specific question. Use a brief when several related requirements need formalization. Wait for the answer before implementing dependent behavior. Neither a reversible change nor an assumption label supplies a missing requirement. Carry forward decisions that have already been accepted.
