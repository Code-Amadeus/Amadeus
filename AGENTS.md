# Engineering Decision Principles

These rules apply to implementation, repair, refactoring, and test work in this repository.

- Diagnose before changing code. Classify a failure as a product-semantic, authority-boundary, abstraction, integration, implementation, or test-instrument problem, and fix it at the owning layer.
- Prefer the smallest coherent structural change that removes the root cause across providers, models, and journeys. Reuse and simplify existing contracts before adding a new field, state, branch, abstraction, or fallback.
- Do not over-defend a local failure. Identify the one missing fact or broken invariant at the failing boundary and repair it in the component that already owns the necessary context. Do not add another model pass, Host classifier, schema, typed envelope, fallback, duplicated context, or speculative guard unless evidence shows that the smaller root-cause fix is insufficient. Prefer one explainable invariant over several overlapping protections.
- Do not solve general problems with provider-specific shortcuts, prompt keyword patches, duplicated sources of truth, silent retries, or growing exception lists. If a special case is genuinely required, state the invariant that makes it exceptional and keep it bounded and observable.
- Treat defenses as proportional to risk. Fail closed for permissions, destructive actions, identity ambiguity, and execution authority; do not add speculative guards that mask defects, block valid interaction, or make ordinary flows brittle.
- Keep responsibilities explicit: models interpret semantics; the host owns identity, durable state, permissions, execution authority, and ledger facts. Model-generated translations and narration are evidence or presentation, not new authority sources.
- Test semantic contracts and user-visible outcomes rather than incidental wording, filenames, timing, or tool order unless those details are themselves part of the contract. Cover both sides of a boundary so a fix cannot merely trade one regression for another.
- Before retaining a compatibility path or workaround, prove that a live caller still needs it. Remove superseded defenses and update the relevant tests and documentation when a structural fix replaces them.
- Optimize for code that a new maintainer can explain from its invariants. Elegance here means fewer independent rules, clear ownership, observable failure, and no loss of necessary safety—not merely fewer lines.

## Downstream feedback to upstream

When downstream development uncovers a bug, continue the local fix and prepare a
short upstream report when the evidence below supports one. Reporting must not
block local work.

- Verify the canonical upstream repository and current revision; record the checked commit. Compare the affected upstream implementation with local changes. Report a concrete upstream defect supported by a minimal reproduction on unmodified upstream, or directly verifiable upstream code evidence. Distinguish observed results from inference and state anything untested. Local customization conflicts or unsupported environment differences alone are not upstream bugs; keep uncertain findings as local notes until verified.
- Include only relevant environment facts: OS/version, CPU architecture, runtime/dependency versions, and GPU/driver or provider/model versions when needed. Use synthetic inputs and short sanitized evidence. Remove credentials, usernames, personal paths, device identifiers, private endpoints, conversations, and proprietary material; do not upload raw logs or full configuration dumps. Follow [SECURITY.md](SECURITY.md) for security-sensitive findings.
- Check existing issues and fixes before proposing a new issue. Keep one defect per report; prefer an existing issue for new evidence, fix validation, or changed upstream status. Avoid duplicate or repetitive updates.
- Draft locally first. Show the sanitized report and destination to the user before publishing, and obtain permission unless an existing explicit authorization already covers that submission or follow-up. This guidance alone does not authorize posting, monitoring, telemetry, or automatic uploads.
- If a local fix offers useful insight, ask its author for permission to share a brief Markdown implementation note. Summarize the owning layer, key change, validation, and limitations; omit private code and unrelated customizations. Permission to report the bug does not grant permission to share the implementation. Follow [CONTRIBUTING.md](CONTRIBUTING.md) for any later code contribution.

Use this compact issue body; omit irrelevant environment fields and the optional
implementation note when it is not approved:

```markdown
## Problem
<User-visible failure and impact; expected vs. actual behavior.>

## Upstream check
<Repository + checked commit/date; relevant local differences and how they were
excluded; clean reproduction or upstream file/line evidence; remaining uncertainty.>

## Environment and evidence (sanitized)
<Relevant OS/architecture/runtime/dependency versions; GPU/driver/provider if relevant.>
<Minimal steps or test command + observed result; short log excerpt or code permalink.>
<Related issue/fix, if any.>

## Optional implementation note (author-approved)
<Owning layer; key idea; before/after validation; tradeoffs and limits.>
```
