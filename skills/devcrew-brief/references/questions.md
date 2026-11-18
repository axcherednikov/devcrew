# Interview questions

Use this question bank to elicit requirements, especially from nontechnical users. Read the relevant code, tests and documentation first. Ask only about material gaps that remain. A single specific question is enough for a local gap. There is no question quota. Skip anything already confirmed.

Interview style:

- Ask one or two questions per message so the user can answer each one.
- Use everyday language. For example, explain an entity as a record or table, a repository as a data store, and an endpoint as the API call someone makes.
- Restate the answer in your own words to check your understanding.
- Follow up when an answer raises a new question.

## Question dependencies and the frontier

First check code, tests, manifests, configuration, ADRs, domain documentation, CI and existing contracts. Treat facts established there as evidence. Build a question graph only for material decisions that these sources cannot resolve reliably.

For each unknown decision, record which unresolved decisions affect how to phrase the question. Before every round, form the `question frontier` from material questions whose prerequisites are known and whose answers are unavailable in the repository. Ask independent frontier questions in one compact round. Wait for prerequisites before asking dependent questions. After each answer, record the confirmed decision, recompute the graph, remove questions that no longer apply and open those now ready. Skip resolved questions. Continue independent planning work while waiting for an answer.

The optional grill has a stricter cadence. When the decision owner is available, ask exactly one question per turn. Gather targeted evidence first, then choose the highest-risk unresolved question on the current frontier. After the answer, update the decision and recompute the frontier before asking about anything that depends on it. You may recommend an answer and briefly explain why. Only the owner's answer confirms the choice. Use the grill to critique a developed, nontrivial plan after discovery. See [grill.md](grill.md).

When the requirements and available facts support an internal technical choice, investigate the options, decide within your role, and explain your reasoning under the [engineering policy](../../devcrew-task/references/engineering-policy.md#understand-first-choose-the-smallest-complete-solution). If facts are missing, continue the available targeted investigation. Conventions can settle many local implementation details safely. Ask the user when a material requirement remains unclear, confirmation is outside the reach of the investigation, or a significant unresolved risk affects data, security or an irreversible outcome. Pause dependent work, describe the facts and the risk, and ask a specific question with a recommended option and the reason for it. A recommendation remains a recommendation until the user confirms it. A justified internal choice is an engineering decision.

When helpful, structure a material question like this:

```text
TITLE
Why this decision matters: <one short reason>
Options:
A. ...
B. ...
Recommendation: ...
Trade-off: ...
```

Use a natural open-ended question when options would be artificial. A recommendation requires confirmation before it becomes a requirement.

## 1. Goal and user

- Who will use this? A mobile customer, an employee using the admin interface, another system, or an automated process?
- What should the user see or receive? What exactly appears on the screen, in an email, or in the user's account?
- What happens if we leave things as they are? What workaround do people use today?
- Who requested the change: product, support, or another team? What outcome do they want?

## 2. Scenario and flow

- Walk me through a concrete example. User X arrives, does Y, and then what happens?
- How does a comparable process work today? What would you change?
- What happens immediately after this step? Who sees the result?
- Are there distinct scenarios? Which is most common, most complex, and least common?

## 3. Edge cases and errors

- What is the most unusual case we need to handle? Give an example.
- What happens if the input is empty, contradictory, or in an unexpected format?
- What happens if an external system such as the POS, payment gateway or SMS provider times out or returns an error?
- What if someone refreshes the page, presses the button twice, or retries a submission?
- What if two users attempt conflicting actions at the same time?

## 4. Dependencies and context

- What does this depend on? Which tasks, systems or data must already be ready?
- What other tasks or systems will this affect? Who else works with these data?
- Can work begin if a dependency is missing, or does it block the start?
- Is another team or colleague working on something related? Do you need to agree on a contract with them?

## 5. Volume, load and performance

- How many records are involved? Tens, thousands, or millions?
- How often does this happen: once a day, every minute, on an event, or on every click?
- How fast must it be? Does the user need an immediate result? Is a second acceptable? Would anything under a minute be fine?
- Could it run at peak load, for example during a sale or at rush hour?

## 6. Scope boundaries

- What should this task explicitly leave out? What would you like to defer?
- What belongs to another task or team?
- Which related features are already expected in the next two or three months?

## 7. Contract, when known

- What does the input look like? Where does it come from?
- What does the output look like? Who reads it?
- Which errors can the user receive? What should the UI show for each one?
- Who authenticates? Which permissions do they need?

## 8. Testing and acceptance

- How will you know the task is complete? What do you need to see working to accept it?
- Which scenarios must work?
- What will a tester check before signing off?

## 9. Technical context for a technical user

- Which stack and architecture does the project use? Ask if `AGENTS.md`, the README and repository evidence leave this unclear.
- Which existing interfaces or abstractions should this work use?
- Which databases, queues or external services does it affect?
- Are there migration requirements such as a working rollback or preserving all data?

## When to stop asking

Stop when you can answer the following:

- [ ] What must be built, including inputs, outputs and flow?
- [ ] Where does the task end? What is excluded?
- [ ] What does it depend on, and what does it block?
- [ ] Which edge cases need attention?
- [ ] Which risks or open questions remain?

If the user cannot answer everything, the brief can still document what is known. Record the gap under `Out of scope / Open questions` or flag a product decision needed before work starts.

## Questions to skip

- Avoid asking a nontechnical user to make technical choices such as whether a table needs an index. Determine that from expected volume.
- Read the stack from the existing code.
- Take responsibility for the architecture within the agreed scope.
- When a broad question would demand a long essay, offer meaningful alternatives such as “Is A, B or C closer to what you have in mind?”
