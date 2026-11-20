---
name: handoff-brief
description: Prepare a concise work handoff or completion report from supplied changes, verification results and remaining work.
---

Read `references/layout.json` before drafting. Use its heading and section names
in order, and put its revision marker at the bottom. Do not infer that a test was
run or an item completed unless the supplied evidence says so.

Read the user's specified input file. Its JSON has `changes`, `verification` and
`remaining` lists. Write the requested Markdown output in the authorized work
directory, using one bullet per supplied item. If no output path was given,
return the report in the response. Do not edit source code or contact services.
