# Daily-task live acceptance

The same Chinese task was given to Pi and OpenClaw in separate new sessions and
directories: find the original arXiv page for *Attention Is All You Need*, read
its abstract, save title/first author/first submission date/three Chinese summary
points/source URL to a UTF-8 file, open the original page in the default browser,
and open the summary in Windows Notepad.

| Run | Execution model | Time | Observed outcome |
| --- | --- | --- | --- |
| Pi, initial | DeepSeek V4 Flash, DeepSeek API | 21.81 s | Search, read, file write/read-back, browser request and Notepad launch completed; final narration incorrectly English |
| Pi, locale fix | Same | 20.67 s | Same task completed with Chinese narration; summary's Notepad window observed |
| Pi, independent window check | Same | 23.28 s | Same task completed; Chrome article title and uniquely named Notepad summary window both observed |
| OpenClaw | DeepSeek V4 Flash, Volcengine | 17.39 s | Original page fetched, summary written, browser and Notepad opened; summary window observed |

The initial Pi run exposed a missing `presentation_locale` argument to the
shared progress/reporting contract. The adapter now forwards it; a regression
test and subsequent live runs cover the correction.

Pi invoked the anonymous search tool and HTTP article reader. OpenClaw directly
fetched the known arXiv URL; its trace did not include a separate search query.
Both files contained the correct title, Ashish Vaswani, 12 June 2017, original
URL and three Chinese points consistent with the abstract. Pi's verification
shell commands were denied by the harness; it recovered using native `read`.
Only Notepad with the exact output filename was approved. OpenClaw used its
already configured native execution policy, including PowerShell commands.

These are small acceptance samples, not a performance benchmark: model endpoints,
permissions and tool sets differ. Pi was not faster in this sample. No memory or
token-cost comparison was measured.

Window verification used read-only Windows process top-level window metadata.
During Pi's final run, Chrome exposed `[1706.03762] Attention Is All You Need`,
then Notepad exposed `pi-daily-20260921-c-article-summary.txt`, both with nonzero
main window handles. The raw process-start acknowledgement alone was not counted
as window evidence. Desktop screenshot/accessibility tools could not initialize
(`failed to write kernel assets`, still failing after reset), so screenshot/DOM
and foreground/focus verification remain unperformed. This is provider-level
live acceptance, not a full Electron chat UI automation run.

Local reports, UTF-8 artifacts, native session handles and window observations
are intentionally excluded from Git under `runtime/pi-acceptance/`. Reproduce
with `scripts/pi_daily_acceptance.py`; it uses real model/network calls and opens
real windows. Close test windows manually when finished.
