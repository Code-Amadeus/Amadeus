# Browser Interaction Branch Lifecycle

This note defines the long-term boundary for browser continuation inside
Amadeus. The goal is to keep browser operation precise without turning the main
chat into a raw DOM log or letting an old browser task leak into a new one.

## Core Objects

### Browser Session

The browser session is the technical Playwright/page handle.

- It can survive page navigation.
- It owns the current URL, title, DOM snapshot, screenshot, refs, and click/fill
  execution state.
- It is not a conversation topic by itself.

### Interaction Branch

The interaction branch is the semantic user task around a browser session.

- It starts from a main-chat checkpoint.
- It may read high-detail page context and DOM.
- It stores branch-local transcript, hidden summary, current page state, actions,
  and artifacts.
- It should stay short-lived and task-scoped.

### Provider Run

The provider run is one execution attempt.

- It may update the active interaction branch.
- It should not become durable main-chat history by default.
- Raw provider reports belong in hidden/tool memory, not visible branch
  transcript.

### Main Chat Merge

The main chat receives only the useful result of the branch.

- Visible user/assistant exchange can be shown in chat.
- Hidden summary can be stored as work context.
- Raw DOM, raw refs, and full provider trace remain branch-local.

## Routing Rule

The active browser branch uses four route decisions:

- `continue`: the user is still operating the current page/site/task.
- `retarget`: the user is asking for a new site, topic, or open-ended search.
- `close`: the user explicitly ends the branch.
- `ignore`: the message is normal chat/noise and should go to main chat.

The latest user instruction is authoritative inside a continued branch.
Older goal/checkpoint/transcript is background only.

When a continued instruction arrives while a Browser provider run is active,
the coordinator steers that same run instead of starting a second run against
the same Playwright session. Steering is latest-wins and revisioned:

1. `run.created` registers the active semantic branch before terminal result.
2. A new instruction is queued on the active run with a monotonic revision.
3. The adapter lets the current atomic browser action finish.
4. It drops the remaining old plan, observes the live page again, and replans
   from the newest instruction.
5. The terminal merge is sealed against later steers so its narration and
   canvas state share one host-observed page snapshot.

This is cooperative immediate steering, not token-stream injection. An action
already sent to a website cannot be undone by changing the next instruction.

## Continue Cases

Continue the current branch when the user anchors the request to the current
page or asks for a page action:

- "search this site for Amadeus videos"
- "click the first result"
- "open that video"
- "go back"
- "type this keyword"
- short values only when the branch is explicitly waiting for a value

The DOM and interaction refs are then used by the browser branch planner.

## Retarget Cases

Retarget closes the old interaction branch and returns control to main chat.
Main chat may then route the new request to Browser, OpenClaw, or another
provider.

Examples:

- Bilibili branch -> "open Wikipedia and search Amadeus"
- Bilibili branch -> "look up Paxos papers"
- any branch -> "new task"
- any branch -> "another website"

Retarget does not necessarily close the technical browser session. It closes the
semantic branch so old goal/history cannot dominate the new request. If its
provider run is still active, the coordinator sends a cooperative stop-plan
steer: the current atomic action may finish, but remaining actions are dropped.
The closed run is tombstoned so its later terminal result cannot recreate the
old semantic branch.

## Why Bilibili -> Wiki Must Supersede

The browser page can navigate from Bilibili to Wikipedia, but the semantic task
changed. If the old branch is reused, old Bilibili goal and provider reports can
pollute the next prompt. The clean behavior is:

1. Close/supersede the Bilibili interaction branch.
2. Let main chat interpret the new user request.
3. If browser is chosen again, create a new interaction branch.
4. Keep only compact hidden summary of the previous branch if useful.

## Prompt Hygiene

Branch prompts must be composed in this priority order:

1. Latest user instruction.
2. Current page title/URL.
3. Branch goal as background.
4. Clean main-chat checkpoint.
5. Clean user branch transcript.
6. Hidden branch summary.

Do not include:

- `[WORK_OBSERVER]` reports.
- `### Browser result` raw reports.
- provider final reports as visible transcript.
- generated branch task prompts nested inside the next branch task.

Provider reports are stored as hidden branch memory and summarized into work
context.

## Branch Exit Strategy

A branch should exit when:

- the user asks to close/cancel/end it;
- the user asks for a different site or new topic;
- an open-ended research/search request is not anchored to the current page;
- the browser closes;
- TTL expires.

The branch can remain active across:

- click/fill/observe actions on the current page;
- navigation inside the same site/task;
- user corrections that are explicitly anchored to the current page.

## Non-Goals

- Do not inject full DOM into durable main chat.
- Do not make provider observer a browser branch controller.
- Do not treat every browser page as a new branch.
- Do not use raw canvas text as the source of truth for browser actions.

## Current Implementation Notes

Primary code:

- `server/interaction_branch.py`
- `agent_host/adapters/browser_branch.py`
- `server/browser_branch_planner.py`
- `server/handlers/chat_handler.py`

Important implementation rules (updated 2026-08-06, single-brain routing):

- Routing authority belongs to the main conversation LLM. When a branch is
  active, `work_context.render_branch_routing_context()` injects a compact
  branch state block plus mandatory routing rules into the system prompt;
  the model expresses intent via `[DELEGATE provider="browser"
  branch="continue|new|close" task="..."]`.
- `try_route_user_message()` keeps only three STRUCTURAL fast paths
  (state/shape-triggered, no vocabulary): waiting-for-value short reply ->
  continue; explicit URL same site -> continue; explicit URL new site ->
  supersede. Everything else defers to the main LLM.
- `_handle_delegate()` consumes the branch attribute: continue routes into
  `continue_from_delegate()` (background run, observer narrates — chat is
  never blocked); close calls `close_active_branch()`.
- `_should_start_new_branch()` honors explicit `branch_intent` metadata
  before falling back to source-based inference.
- `_branch_task()` keeps latest instruction first and filters noisy context.
- `_merge_run_into_branch()` stores provider reports as hidden branch memory.
- `run.created` makes the branch steerable before the first provider result;
  per-session serialization prevents overlapping runs from mutating one
  browser session.
- ProviderRuntime enforces the `immediate` capability and records
  `steer_queued`; BrowserBranchAdapter records `steer_applied` and checks for a
  newer revision before planning, before each action, after each action, and
  before terminal merge.
- `ChatHandler` applies its turn/epoch guard after a branch wait just as it does
  after a main-LLM wait. An older overlapping turn may finish internally, but it
  cannot emit a subtitle/token, write chat history, publish `chat.complete`, or
  trigger TTS after a newer turn has taken ownership.
- Retarget/close is also cooperative: it stops the remaining plan at the next
  safe action boundary, preserves the Playwright session, and tombstones the old
  run result so the closed branch cannot resurrect.
- WorkActivity shows `steer_queued` and `steer_applied` as compact, silent
  Checkpoint cards with no buttons. A semantic stop uses stop-specific wording
  and suppresses the old run's generic Review/Result card.
- The former 11 keyword heuristics (`_browser_branch_route_decision` cascade)
  were removed: they mis-captured ordinary chat into branches and missed
  non-whitelisted sites/phrasings in all three languages.

## Test Scenarios

Expected behavior:

- Active Bilibili branch + "search this site for Amadeus videos" -> continue.
- Active Bilibili branch + "open Wikipedia and search Amadeus" -> retarget.
- Active Bilibili branch + "look up Paxos papers" -> retarget.
- Active waiting branch + "Amadeus" -> continue as value.
- Active idle branch + "Amadeus" -> ignore/main chat unless anchored.
- Provider result from same `interaction_branch_id` -> update same branch.
- Provider result from new browser delegation -> supersede old branch.
- Two overlapping same-site ChatHandler turns -> one provider run; only the
  newest turn may emit chat completion, persistence, and TTS.
- Retarget during a multi-action run -> current atomic action may finish;
  remaining actions stop, browser session survives, old result stays tombstoned.
