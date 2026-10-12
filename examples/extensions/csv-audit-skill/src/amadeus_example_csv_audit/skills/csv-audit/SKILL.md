---
name: csv-audit
description: Audit a small CSV table for missing required values, duplicate record identifiers and negative amounts, then produce a JSON quality report.
---

Read `references/rules.json` before checking the user's CSV. Preserve the source.
Use only the supplied data; missing values are not permission to invent facts.
Count data rows from 1, excluding the header. Report every violation, including
multiple violations on the same row. A duplicate means a later occurrence of
an identifier already seen in an earlier row. A missing amount is not negative.

Write the requested JSON report with `row_count`, `issues`, and `rules_revision`.
Each issue has `row` (integer), `code` (the reference's exact code), and `column`.
Take `rules_revision` from the reference. Do not put data-quality issues into
a work handoff template. Do not edit other files or contact external services.
