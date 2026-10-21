// OCR supplies snapshots, not dialogue events. Commit only two consecutive
// identical snapshots; a stable blank ends the current dialogue occurrence.
struct OCRDialogueTracker {
    private var candidate: String?
    private var committed = ""

    mutating func observe(_ text: String) -> String? {
        guard candidate == text else {
            candidate = text
            return nil
        }
        if text.isEmpty {
            committed = ""
            return nil
        }
        guard text != committed else { return nil }
        committed = text
        return text
    }
}
