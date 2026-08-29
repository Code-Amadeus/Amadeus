## What and why

Describe the problem, the intended outcome, and why this is the smallest coherent
change.

Linked Issue for product-semantic or public-contract changes: <!-- #123 -->

## Change class

- [ ] Routine fix, documentation, test, maintenance, or presentation-only UI
- [ ] Product-semantic or public-contract change discussed in the linked Issue
- [ ] Isolated, default-off experiment

Owning layer:

User-visible effect, or `none`:

Compatibility or migration impact, or `none`:

## Evidence

Commands and manual journeys run:

- [ ] Relevant Python tests pass
- [ ] CPU/model-less baseline remains supported
- [ ] Electron `npm run build` passes when Electron code changed
- [ ] Dependency audit passes when dependencies changed
- [ ] Before/after screenshots are attached for visible UI changes
- [ ] Documentation/examples are updated for changed settings or contracts

## Final check

- [ ] This PR addresses one coherent problem without unrelated cleanup
- [ ] It does not add a speculative API, fallback, or compatibility path
- [ ] No secrets, local state, model weights, voice material, or restricted assets are included
- [ ] Third-party notices and provenance are preserved
