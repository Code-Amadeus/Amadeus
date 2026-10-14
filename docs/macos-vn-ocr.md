# macOS VN OCR

The experimental **macOS Vision OCR (Window Capture)** source reads dialogue
from a manually started game. It requires macOS 14 or later, Screen Recording
permission, and a Swift compiler available as `swiftc`. Agent, hook scripts and
a game executable are unnecessary for this source.

1. Start the game and open **VN Player → Add game → Text connection**.
2. Select **macOS Vision OCR (Window Capture)**, refresh the window list and
   choose the game's dialogue window.
3. Capture a preview and drag the selection around dialogue. The selection uses
   the displayed image's coordinates; cropping failures stop capture rather
   than reading the full window. Choose languages and a scan interval.
4. Use **Save and test text** to compare received text with dialogue before
   starting the companion. The text test does not call a model.

The text source and manual game-view attachment share the session's window ID
and process ID. A missing selected window, changed owner, or ambiguous name
cannot switch capture to another window. After the game restarts or recreates
its window, end the session and select the current window again. Updating saved
selectors does not redirect an already running session.

OCR snapshots become dialogue events after two consecutive identical scans.
Two consecutive blank scans end an occurrence, allowing the same dialogue to
be recorded again. A single blank or changing recognition result does not
duplicate an already committed line. This adds approximately one scan interval
after the first matching snapshot. It cannot establish story semantics: text
shown too briefly may be missed, and a typing animation that pauses long enough
for two identical scans may still produce a partial line. Adjust the game text
speed and interval, then verify the game's text test. Animated UI inside the
crop can prevent the snapshot from stabilizing.

Errors from permissions, capture, recognition, downstream delivery and helper
exit are reported through source status. **End session** releases the OCR
helper even after delivery fails; it does not close the manually started game.

All native capture entry points use the same build check. The build script
recompiles when either Swift source or the script is newer than the helper,
targets the machine's architecture, and replaces the executable only after a
successful compilation. Failed builds are reported instead of using a stale
helper. The generated executable lives under `build/bin/`.

The optional portrait overlay resolves bundled Tcl/Tk libraries from the base
interpreter when launched from a virtual environment. Frames use Tk's native PNG
decoder, so older Python distributions do not need Pillow's Tcl ABI bridge.
Overlay startup failures report the exit code and final error message.
