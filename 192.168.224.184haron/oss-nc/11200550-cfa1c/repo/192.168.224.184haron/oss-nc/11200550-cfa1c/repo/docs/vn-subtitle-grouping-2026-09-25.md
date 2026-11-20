# VN captions independent of audio chunks

## Behavior

VN speech keeps its low-latency audio boundaries. Captions collect accepted audio
fragments until sentence-ending punctuation or normal speech completion. A comma,
an early speech cut or a TTS chunk change does not create a new translated caption.
Adjacent short sentences already combined in an audio item may share one caption.

The same complete source caption and translation serve every playback item in
that caption group. Translation runs once per completed group through the existing
sidecar/cache. It is never awaited by audio submission. If the full source sentence
has not arrived yet, captions wait; the audio can already be playing. Translation
may arrive later and updates only a still-current playback item.

The raw TTS display hook cannot replace a grouped caption with an audio fragment.
The existing playback identity checks still reject old subtitles and stop events.
The wallpaper source/translated/bilingual preferences and companion window's
appearance are unchanged. Normal Chat and other narration retain their existing
subtitle path.

## Ownership and lifecycle

- The runtime assigns each VN utterance its own Host identity, independent of the
  game line ID. Multiple answers to the same game line cannot share caption input.
- `server/vn_tts_bridge.py` owns caption grouping. It binds accepted audio items
  to a shared caption before they can play, and adds text only after queue
  acceptance. The private caption object stays inside the bridge.
- The runtime supplies one end notification after text delivery. Normal completion
  closes a final sentence without punctuation; pause, interruption, cancellation
  or rejection discards an unfinished caption instead of inventing its ending.
  The input accumulator is removed at this boundary; queued audio retains its
  caption reference for playback.
- Streamed and complete-response VN utterances use the same lifecycle. No new
  public event, frontend setting, dependency or model pass was introduced.
- Prompt rules and model behavior are unchanged. Grouping uses the existing
  authored speech; control tags are removed before captions or translation.

## Verification

Deterministic model-stream tests deliberately hold the speech body and subtitle
translation open. They verify that the first real bridge queue item arrives
before either is ready, then that both audio chunks show the same full caption.
The tests use the real companion overlay projection with window drawing replaced.

Coverage includes:

- one translation per complete sentence group, and no translation of its clauses;
- delayed translation across consecutive audio IDs, prefetched next sentences,
  and rejection of obsolete subtitle events;
- normal end-of-body with no additional audio, versus broken/incomplete bodies;
- pause, Host TTS interruption and rejected tail chunks;
- separate utterances on the same game line, and preservation of English spaces;
- complete-response delivery and removal of emotion/control tags;
- suppression of raw fragment redraws only for grouped VN audio.

The focused regression run passes 268 tests. Ruff, generated architecture checks
and model-less Electron startup/authentication/navigation/shutdown smoke pass.
These checks verify delivery ordering and display contents, not a new live-game
playthrough or a new physical first-sound benchmark. The preceding audio
measurements remain in [first-segment streaming](vn-first-segment-streaming-2026-09-25.md).

```powershell
$vnTests = @(rg --files tests -g 'test_vn_*.py')
.venv/Scripts/python.exe -X utf8 -m pytest @vnTests tests/test_cross_scene_narration_baseline.py tests/test_ws_control_preemption.py tests/test_visual_runtime_settings.py tests/test_local_instance_auth.py tests/test_backend_instance_auth_contract.py tests/test_sentence_splitter.py tests/test_chat_protocol_roles.py tests/test_narration_delivery.py -q
```
