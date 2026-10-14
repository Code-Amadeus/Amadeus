import AppKit
import Foundation
import ScreenCaptureKit
import Vision

_ = NSApplication.shared

struct WindowInfo: Codable {
    let id: CGWindowID
    let title: String
    let app: String
    let pid: pid_t
    let width: Int
    let height: Int
}

struct OCRResult: Codable {
    let type: String
    let text: String?
    let status: String?
    let error: String?
    let timestamp: Int64?
}

struct PreviewResult: Codable {
    let width: Int
    let height: Int
    let imageBase64: String
}

func listWindowsCG() -> [WindowInfo] {
    guard let infoList = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
        return []
    }
    var list: [WindowInfo] = []
    let ignoredApps: Set<String> = [
        "Window Server", "Dock", "ControlCenter", "NotificationCenter",
        "程序坞", "通知中心", "控制中心", "墙纸", "Wallpaper", "Item-0"
    ]
    for dict in infoList {
        guard let layer = dict[kCGWindowLayer as String] as? Int, layer == 0 else { continue }
        let wid = (dict[kCGWindowNumber as String] as? CGWindowID) ?? 0
        let pid = (dict[kCGWindowOwnerPID as String] as? pid_t) ?? 0
        let app = (dict[kCGWindowOwnerName as String] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let title = (dict[kCGWindowName as String] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard let boundsDict = dict[kCGWindowBounds as String] as? [String: Any],
              let rect = CGRect(dictionaryRepresentation: boundsDict as CFDictionary) else { continue }
        let w = Int(rect.width)
        let h = Int(rect.height)
        if w < 100 || h < 100 { continue }
        if ignoredApps.contains(app) || title == "Display 1 Backstop" { continue }
        list.append(WindowInfo(id: wid, title: title, app: app, pid: pid, width: w, height: h))
    }
    return list
}

@MainActor
func listWindows() async throws {
    if CGPreflightScreenCaptureAccess() {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            var list: [WindowInfo] = []
            for win in content.windows {
                let title = (win.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                let app = (win.owningApplication?.applicationName ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                let w = Int(win.frame.width)
                let h = Int(win.frame.height)
                if w < 100 || h < 100 { continue }
                if app == "Window Server" || app == "Dock" || app == "ControlCenter" || app == "NotificationCenter"
                    || app == "程序坞" || app == "通知中心" || title == "Display 1 Backstop" || app == "墙纸" { continue }
                list.append(WindowInfo(id: win.windowID, title: title, app: app, pid: win.owningApplication?.processID ?? 0, width: w, height: h))
            }
            if !list.isEmpty {
                let data = try JSONEncoder().encode(list)
                if let json = String(data: data, encoding: .utf8) {
                    print(json)
                    return
                }
            }
        } catch {
            // Fall through to CGWindowListCopyWindowInfo
        }
    }

    let list = listWindowsCG()
    let data = try JSONEncoder().encode(list)
    if let json = String(data: data, encoding: .utf8) {
        print(json)
    }
}

@MainActor
func findTargetWindow(content: SCShareableContent, windowId: CGWindowID?, windowName: String?, appName: String?, pid: pid_t? = nil) -> SCWindow? {
    if let wid = windowId, wid > 0 {
        if let found = content.windows.first(where: { $0.windowID == wid }) {
            return found
        }
    }
    if let targetPid = pid, targetPid > 0 {
        if let found = content.windows.first(where: { $0.owningApplication?.processID == targetPid }) {
            return found
        }
    }
    if let wname = windowName, !wname.isEmpty {
        let lower = wname.lowercased()
        if let found = content.windows.first(where: { ($0.title ?? "").lowercased().contains(lower) }) {
            return found
        }
    }
    if let aname = appName, !aname.isEmpty {
        let lower = aname.lowercased()
        if let found = content.windows.first(where: { ($0.owningApplication?.applicationName ?? "").lowercased().contains(lower) }) {
            return found
        }
    }
    return nil
}

@MainActor
func captureSingleFrame(windowId: CGWindowID?, windowName: String?, appName: String?, pid: pid_t? = nil) async throws -> CGImage? {
    if !CGPreflightScreenCaptureAccess() {
        CGRequestScreenCaptureAccess()
        fputs("PermissionDenied: Screen Recording permission is required. Please enable it in System Settings -> Privacy & Security -> Screen Recording, and restart Amadeus (Cmd+Q).\n", stderr)
        return nil
    }
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
    guard let win = findTargetWindow(content: content, windowId: windowId, windowName: windowName, appName: appName, pid: pid) else {
        return nil
    }
    let filter = SCContentFilter(desktopIndependentWindow: win)
    let config = SCStreamConfiguration()
    config.width = max(1, Int(win.frame.width))
    config.height = max(1, Int(win.frame.height))
    config.showsCursor = false
    return try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
}

func cropImage(image: CGImage, crop: CGRect) -> CGImage? {
    let imgW = CGFloat(image.width)
    let imgH = CGFloat(image.height)
    let targetRect = CGRect(
        x: max(0, min(imgW, crop.origin.x * imgW)),
        y: max(0, min(imgH, crop.origin.y * imgH)),
        width: max(1, min(imgW, crop.size.width * imgW)),
        height: max(1, min(imgH, crop.size.height * imgH))
    )
    return image.cropping(to: targetRect)
}

func recognizeText(image: CGImage, languages: [String]) throws -> String {
    var recognizedLines: [String] = []
    let request = VNRecognizeTextRequest { req, _ in
        guard let observations = req.results as? [VNRecognizedTextObservation] else { return }
        for obs in observations {
            if let top = obs.topCandidates(1).first {
                let s = top.string.trimmingCharacters(in: .whitespacesAndNewlines)
                if !s.isEmpty {
                    recognizedLines.append(s)
                }
            }
        }
    }
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = languages

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])
    return recognizedLines.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
}

func emit(result: OCRResult) {
    if let data = try? JSONEncoder().encode(result), let str = String(data: data, encoding: .utf8) {
        print(str)
        fflush(stdout)
    }
}

@MainActor
func runStream(windowId: CGWindowID?, windowName: String?, appName: String?, pid: pid_t? = nil, crop: CGRect, languages: [String], intervalMs: Int) async {
    var lastText = ""
    var wasWaiting = false
    emit(result: OCRResult(type: "status", text: nil, status: "starting", error: nil, timestamp: Int64(Date().timeIntervalSince1970 * 1000)))

    if !CGPreflightScreenCaptureAccess() {
        CGRequestScreenCaptureAccess()
        emit(result: OCRResult(type: "status", text: nil, status: "error", error: "PermissionDenied: Screen Recording permission is required. Please enable it in System Settings and restart Amadeus (Cmd+Q).", timestamp: Int64(Date().timeIntervalSince1970 * 1000)))
        return
    }

    while true {
        do {
            guard let frame = try await captureSingleFrame(windowId: windowId, windowName: windowName, appName: appName, pid: pid) else {
                if !wasWaiting {
                    emit(result: OCRResult(type: "status", text: nil, status: "waiting", error: "Target window not found", timestamp: Int64(Date().timeIntervalSince1970 * 1000)))
                    wasWaiting = true
                }
                try await Task.sleep(nanoseconds: UInt64(intervalMs) * 1_000_000)
                continue
            }
            if wasWaiting {
                emit(result: OCRResult(type: "status", text: nil, status: "running", error: nil, timestamp: Int64(Date().timeIntervalSince1970 * 1000)))
                wasWaiting = false
            }

            let cropped = cropImage(image: frame, crop: crop) ?? frame
            let text = try recognizeText(image: cropped, languages: languages)

            if !text.isEmpty && text != lastText {
                lastText = text
                emit(result: OCRResult(type: "line", text: text, status: nil, error: nil, timestamp: Int64(Date().timeIntervalSince1970 * 1000)))
            }
        } catch {
            emit(result: OCRResult(type: "error", text: nil, status: nil, error: error.localizedDescription, timestamp: Int64(Date().timeIntervalSince1970 * 1000)))
        }

        try? await Task.sleep(nanoseconds: UInt64(intervalMs) * 1_000_000)
    }
}

@MainActor
func main() async {
    let args = CommandLine.arguments
    var mode = "stream"
    var windowId: CGWindowID?
    var windowName: String?
    var appName: String?
    var pid: pid_t?
    var crop = CGRect(x: 0.0, y: 0.65, width: 1.0, height: 0.35)
    var languages = ["ja", "zh-Hans", "zh-Hant", "en-US"]
    var intervalMs = 700

    var idx = 1
    while idx < args.count {
        let arg = args[idx]
        switch arg {
        case "--list-windows":
            mode = "list"
        case "--preview-frame":
            mode = "preview"
        case "--capture-only":
            mode = "capture_only"
        case "--stream":
            mode = "stream"
        case "--window-id":
            if idx + 1 < args.count {
                idx += 1
                windowId = CGWindowID(args[idx])
            }
        case "--pid":
            if idx + 1 < args.count {
                idx += 1
                pid = pid_t(args[idx])
            }
        case "--window-name":
            if idx + 1 < args.count {
                idx += 1
                windowName = args[idx]
            }
        case "--app-name":
            if idx + 1 < args.count {
                idx += 1
                appName = args[idx]
            }
        case "--crop":
            if idx + 1 < args.count {
                idx += 1
                let parts = args[idx].split(separator: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
                if parts.count == 4 {
                    crop = CGRect(x: parts[0], y: parts[1], width: parts[2], height: parts[3])
                }
            }
        case "--languages":
            if idx + 1 < args.count {
                idx += 1
                languages = args[idx].split(separator: ",").map { String($0.trimmingCharacters(in: .whitespaces)) }
            }
        case "--interval-ms":
            if idx + 1 < args.count {
                idx += 1
                intervalMs = max(200, Int(args[idx]) ?? 700)
            }
        default:
            break
        }
        idx += 1
    }

    do {
        switch mode {
        case "list":
            try await listWindows()
            exit(0)
        case "preview":
            guard let image = try await captureSingleFrame(windowId: windowId, windowName: windowName, appName: appName, pid: pid) else {
                fputs("Window not found\n", stderr)
                exit(1)
            }
            let rep = NSBitmapImageRep(cgImage: image)
            guard let jpeg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.7]) else {
                fputs("JPEG conversion failed\n", stderr)
                exit(1)
            }
            let base64 = jpeg.base64EncodedString()
            let res = PreviewResult(width: image.width, height: image.height, imageBase64: "data:image/jpeg;base64," + base64)
            let out = try JSONEncoder().encode(res)
            if let json = String(data: out, encoding: .utf8) {
                print(json)
            }
            exit(0)
        case "capture_only":
            guard let image = try await captureSingleFrame(windowId: windowId, windowName: windowName, appName: appName, pid: pid) else {
                fputs("Window not found\n", stderr)
                exit(1)
            }
            let rep = NSBitmapImageRep(cgImage: image)
            guard let png = rep.representation(using: .png, properties: [:]) else {
                fputs("PNG conversion failed\n", stderr)
                exit(1)
            }
            FileHandle.standardOutput.write(png)
            exit(0)
        default:
            await runStream(windowId: windowId, windowName: windowName, appName: appName, pid: pid, crop: crop, languages: languages, intervalMs: intervalMs)
        }
    } catch {
        fputs("Error: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
}

Task {
    await main()
}
RunLoop.main.run()
