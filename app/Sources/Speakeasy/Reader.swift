// The reader pipeline, replacing the Hammerspoon hotkey: Accessibility selection + bounds, a screenshot of the selection
// (screencapture -R, never a prompt: skipped without Screen Recording permission), the document hint, then the Python
// backend (`speakeasy speak`) and the embedded tracker, which draws the highlight and follows scrolling.
import AppKit
import ApplicationServices

final class Reader {
    let home = NSHomeDirectory() + "/.speakeasy"
    var backend: Process?
    var tracker: Process?
    var runId = ""

    var backendPath: String {
        let cli = NSHomeDirectory() + "/.local/bin/speakeasy"
        if FileManager.default.isExecutableFile(atPath: cli) { return cli }
        return Bundle.main.resourcePath.map { $0 + "/backend/speakeasy" } ?? cli     // bundled launcher (for now: the same Python backend)
    }
    var trackerPath: String {
        let b = Bundle.main.executableURL!.deletingLastPathComponent().appendingPathComponent("speakeasy-track").path
        return FileManager.default.isExecutableFile(atPath: b) ? b : home + "/bin/speakeasy-track"
    }
    var speaking: Bool { FileManager.default.fileExists(atPath: home + "/speak.pid") }

    func toggle() { speaking ? stop() : start() }
    func stop() {
        tracker?.terminate(); tracker = nil
        run(backendPath, ["stop"], stdin: nil)
    }

    func start() {
        let t0 = Date()
        runId = String(format: "%llx%04x", UInt64(t0.timeIntervalSince1970 * 1000), UInt32.random(in: 0...0xffff))
        guard AXIsProcessTrusted() else { (NSApp.delegate as? AppDelegate)?.showOnboarding(); return }
        guard let front = NSWorkspace.shared.frontmostApplication else { return }
        let pid = front.processIdentifier
        let appEl = AXUIElementCreateApplication(pid)
        guard let el: AXUIElement = attr(appEl, kAXFocusedUIElementAttribute) else { return }
        let text: String = attr(el, kAXSelectedTextAttribute) ?? ""
        var rect: CGRect? = nil
        var range = CFRange(location: 0, length: 0)
        if let rv: AXValue = attr(el, kAXSelectedTextRangeAttribute), AXValueGetValue(rv, .cfRange, &range), range.length > 0 {
            var r = range
            if let v = AXValueCreate(.cfRange, &r) {
                var out: CFTypeRef?
                if AXUIElementCopyParameterizedAttributeValue(el, kAXBoundsForRangeParameterizedAttribute as CFString, v, &out) == .success,
                   let o = out, CFGetTypeID(o) == AXValueGetTypeID() {
                    var b = CGRect.zero
                    if AXValueGetValue(o as! AXValue, .cgRect, &b), b.width > 4, b.height > 4 { rect = b }
                }
            }
        }
        // document hint (never logs the path; speakeasy hashes it)
        var hint: [String: Any] = ["app": front.localizedName ?? "", "bundle": front.bundleIdentifier ?? ""]
        if let win: AXUIElement = attr(appEl, kAXFocusedWindowAttribute) {
            if let t: String = attr(win, kAXTitleAttribute) { hint["title"] = t }
            if let d: String = attr(win, kAXDocumentAttribute) { hint["ax_document"] = d }
        }
        var cur: AXUIElement? = el
        for _ in 0..<20 {                      // the web area that holds the selection, for its URL
            guard let c = cur else { break }
            if let role: String = attr(c, kAXRoleAttribute), role == "AXWebArea" {
                if let u: URL = attr(c, kAXURLAttribute) { hint["url"] = u.absoluteString } else if let u: String = attr(c, kAXURLAttribute) { hint["url"] = u }
                break
            }
            cur = attr(c, kAXParentAttribute)
        }
        let hintPath = "/tmp/speakeasy_cap_doc.json"
        if let d = try? JSONSerialization.data(withJSONObject: hint) { FileManager.default.createFile(atPath: hintPath, contents: d) }

        var args = ["speak", "--doc-hint", hintPath]
        if let r = rect, CGPreflightScreenCaptureAccess() {
            let p = r.insetBy(dx: -16, dy: -16)
            let shot = "/tmp/speakeasy_cap_\(Int(Date().timeIntervalSince1970 * 1000)).png"
            run("/usr/sbin/screencapture", ["-x", "-R", "\(Int(p.minX)),\(Int(p.minY)),\(Int(p.width)),\(Int(p.height))", shot], stdin: nil, wait: true)
            if FileManager.default.fileExists(atPath: shot) {
                args += ["--image", shot, "--capture-method", "bounds", "--capture-rect", "\(Int(p.minX)),\(Int(p.minY)),\(Int(p.width)),\(Int(p.height))"]
            }
        }
        args += ["--capture-ms", String(Int(Date().timeIntervalSince(t0) * 1000)), "--run-id", runId, "--app", "menubar"]
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && args.firstIndex(of: "--image") == nil { NSSound.beep(); return }
        backend = run(backendPath, args, stdin: text)
        startTracker(pid: pid)
    }

    func startTracker(pid: pid_t) {
        tracker?.terminate(); tracker = nil
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]],
              let w = list.first(where: { ($0[kCGWindowOwnerPID as String] as? Int32) == pid && ($0[kCGWindowLayer as String] as? Int) == 0 }),
              let wid = w[kCGWindowNumber as String] as? Int else { return }
        tracker = run(trackerPath, ["--window-id", String(wid), "--pid", String(pid), "--home", home, "--fps", "5", "--overlay", "native", "--run-id", runId], stdin: nil)
    }

    @discardableResult
    func run(_ path: String, _ args: [String], stdin: String?, wait: Bool = false) -> Process? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path); p.arguments = args
        p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
        var pipe: Pipe? = nil
        if stdin != nil { pipe = Pipe(); p.standardInput = pipe }
        do { try p.run() } catch { return nil }
        if let s = stdin, let pp = pipe { pp.fileHandleForWriting.write(s.data(using: .utf8)!); try? pp.fileHandleForWriting.close() }
        if wait { p.waitUntilExit() }
        return p
    }
}

func attr<T>(_ el: AXUIElement, _ name: String) -> T? {
    var v: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success, let x = v else { return nil }
    return x as? T
}
