// The reader pipeline (parity with the old Hammerspoon hotkey script): Accessibility selection, its bounds and per-line
// bounds, a copy fallback for apps without AX text (web views), a screenshot of the selection (or of the window when nothing
// is selected), the document hint, then the Python backend (`speakeasy speak`) and the embedded tracker, which draws the
// highlight and follows scrolling. Never prompts for permissions: missing ones are skipped (onboarding asks on a click).
import AppKit
import ApplicationServices

final class Reader {
    let home = NSHomeDirectory() + "/.speakeasy"
    var backend: Process?
    var tracker: Process?
    var runId = ""
    var onState: ((String) -> Void)?          // "listening" while capturing; the pill watches files for the rest

    var backendPath: String {
        let cli = NSHomeDirectory() + "/.local/bin/speakeasy"
        if FileManager.default.isExecutableFile(atPath: cli) { return cli }
        return Bundle.main.resourcePath.map { $0 + "/backend/speakeasy" } ?? cli
    }
    var trackerPath: String {
        let b = Bundle.main.executableURL!.deletingLastPathComponent().appendingPathComponent("speakeasy-track").path
        return FileManager.default.isExecutableFile(atPath: b) ? b : home + "/bin/speakeasy-track"
    }
    var speaking: Bool {
        guard let s = try? String(contentsOfFile: home + "/speak.pid", encoding: .utf8), let p = pid_t(s.trimmingCharacters(in: .whitespacesAndNewlines)) else { return false }
        return kill(p, 0) == 0
    }

    func toggle() { speaking ? stop() : start() }
    func stop() {
        killTracker()
        run(backendPath, ["stop"], stdin: nil)
        onState?("idle")
    }
    func killTracker() {
        tracker?.terminate(); tracker = nil
        if let s = try? String(contentsOfFile: home + "/tracker.pid", encoding: .utf8), let p = pid_t(s.trimmingCharacters(in: .whitespacesAndNewlines)), p > 0 { kill(p, SIGTERM) }
        try? FileManager.default.removeItem(atPath: home + "/tracker.pid")
    }

    func start() {
        let t0 = Date()
        runId = String(format: "%llx%04x", UInt64(t0.timeIntervalSince1970 * 1000), UInt32.random(in: 0...0xffff))
        guard AXIsProcessTrusted() else { (NSApp.delegate as? AppDelegate)?.showOnboarding(); return }
        guard let front = NSWorkspace.shared.frontmostApplication else { return }
        onState?("listening")
        let pid = front.processIdentifier
        let appEl = AXUIElementCreateApplication(pid)
        let win = frontWindow(pid)
        startTracker(pid: pid, wid: win?.id)                       // lock onto the source window now
        writeDocHint(front: front, appEl: appEl)
        for p in ["/tmp/speakeasy_cap_lines.json", "/tmp/speakeasy_cap_sel.png"] { try? FileManager.default.removeItem(atPath: p) }

        var text = ""
        var rect: CGRect? = nil
        if let focused: AXUIElement = attr(appEl, kAXFocusedUIElementAttribute) {
            let (el, r) = findSelection(focused)
            rect = r
            if let e = el { text = attr(e, kAXSelectedTextAttribute) ?? "" }
            if text.isEmpty, let t: String = attr(focused, kAXSelectedTextAttribute) { text = t }
        }
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { text = copySelection() }   // web views / Electron
        let sr = CGPreflightScreenCaptureAccess()
        var args = ["speak", "--doc-hint", "/tmp/speakeasy_cap_doc.json"]
        let shot = "/tmp/speakeasy_cap_sel.png"
        if let r = rect, sr {
            let p = clamp(r.insetBy(dx: -16, dy: -16))
            capture(["-x", "-R", rs(p), shot])
            if FileManager.default.fileExists(atPath: shot) {
                args += ["--image", shot, "--capture-method", "bounds", "--capture-rect", rs(p)]
                if FileManager.default.fileExists(atPath: "/tmp/speakeasy_cap_lines.json") { args += ["--line-bounds", "/tmp/speakeasy_cap_lines.json"] }
            }
        } else if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, sr, let w = win,
                  (Config.value("SPEAKEASY_NO_SELECTION_CAPTURE") ?? "window") == "window" {
            capture(["-x", "-o", "-l", String(w.id), shot])          // nothing selected: explain the window
            if FileManager.default.fileExists(atPath: shot) {
                args += ["--image", shot, "--capture-method", "no-selection-window", "--capture-rect", rs(w.frame)]
            }
        }
        if args.firstIndex(of: "--capture-method") == nil { args += ["--capture-method", "none"] }
        args += ["--capture-ms", String(Int(Date().timeIntervalSince(t0) * 1000)), "--run-id", runId, "--app", "menubar"]
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && args.firstIndex(of: "--image") == nil {
            NSSound.beep(); killTracker(); onState?("idle"); return
        }
        onState?("thinking")
        backend = run(backendPath, args, stdin: text)
        let me = backend
        backend?.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { if self?.backend === me { self?.backend = nil; self?.onState?("idle") } }
        }
    }

    // ---- selection ----
    /// The element holding the selection (the focused one, else a descendant: depth 4, 300 nodes) and its bounds.
    func findSelection(_ el: AXUIElement) -> (AXUIElement?, CGRect?) {
        if let b = bounds(el) { return (el, b) }
        var queue: [(AXUIElement, Int)] = [(el, 0)], seen = 0
        while !queue.isEmpty && seen < 300 {
            let (n, d) = queue.removeFirst(); seen += 1
            if seen > 1, let b = bounds(n) { return (n, b) }
            if d < 4, let kids: [AXUIElement] = attr(n, kAXChildrenAttribute) { queue += kids.map { ($0, d + 1) } }
        }
        return (nil, nil)
    }
    func bounds(_ el: AXUIElement) -> CGRect? {
        var range = CFRange(location: 0, length: 0)
        if let rv: AXValue = attr(el, kAXSelectedTextRangeAttribute), AXValueGetValue(rv, .cfRange, &range), range.length > 0 {
            if let b = rangeBounds(el, range.location, range.length) {
                if Config.on("SPEAKEASY_FOCUS") { writeLineBounds(el, range) }
                return b
            }
        }
        if let tm: CFTypeRef = attr(el, "AXSelectedTextMarkerRange") {      // WebKit / Chromium
            var out: CFTypeRef?
            if AXUIElementCopyParameterizedAttributeValue(el, "AXBoundsForTextMarkerRange" as CFString, tm, &out) == .success, let b = rectOf(out) { return b }
        }
        return nil
    }
    func rangeBounds(_ el: AXUIElement, _ loc: Int, _ len: Int) -> CGRect? {
        var r = CFRange(location: loc, length: len)
        guard let v = AXValueCreate(.cfRange, &r) else { return nil }
        var out: CFTypeRef?
        guard AXUIElementCopyParameterizedAttributeValue(el, kAXBoundsForRangeParameterizedAttribute as CFString, v, &out) == .success else { return nil }
        return rectOf(out)
    }
    func rectOf(_ o: CFTypeRef?) -> CGRect? {
        guard let o = o, CFGetTypeID(o) == AXValueGetTypeID() else { return nil }
        var b = CGRect.zero
        return AXValueGetValue(o as! AXValue, .cgRect, &b) && b.width > 4 && b.height > 4 ? b : nil
    }
    /// Per visual line bounds of the selection (for the focus band), as the backend expects: [{loc, abs, len, rect}].
    func writeLineBounds(_ el: AXUIElement, _ range: CFRange) {
        func param(_ name: String, _ v: CFTypeRef) -> CFTypeRef? {
            var out: CFTypeRef?; return AXUIElementCopyParameterizedAttributeValue(el, name as CFString, v, &out) == .success ? out : nil
        }
        guard let f = param(kAXLineForIndexParameterizedAttribute, range.location as CFNumber) as? Int,
              let l = param(kAXLineForIndexParameterizedAttribute, (range.location + range.length - 1) as CFNumber) as? Int,
              l >= f, l - f <= 300 else { return }
        var out: [[String: Any]] = []
        for ln in f...l {
            guard let lv = param(kAXRangeForLineParameterizedAttribute, ln as CFNumber), CFGetTypeID(lv) == AXValueGetTypeID() else { continue }
            var lr = CFRange(location: 0, length: 0); AXValueGetValue(lv as! AXValue, .cfRange, &lr)
            let s0 = max(lr.location, range.location), s1 = min(lr.location + lr.length, range.location + range.length)
            if s1 > s0, let b = rangeBounds(el, s0, s1 - s0) {
                out.append(["loc": s0 - range.location, "abs": s0, "len": s1 - s0, "rect": [b.minX, b.minY, b.width, b.height]])
            }
        }
        if !out.isEmpty, let d = try? JSONSerialization.data(withJSONObject: out) { FileManager.default.createFile(atPath: "/tmp/speakeasy_cap_lines.json", contents: d) }
    }
    /// Cmd-C into the frontmost app, read the copied text, restore the pasteboard.
    func copySelection() -> String {
        let pb = NSPasteboard.general
        let saved = pb.pasteboardItems?.map { it -> [(NSPasteboard.PasteboardType, Data)] in it.types.compactMap { t in it.data(forType: t).map { (t, $0) } } } ?? []
        let before = pb.changeCount
        let src = CGEventSource(stateID: .combinedSessionState)
        for down in [true, false] {
            let e = CGEvent(keyboardEventSource: src, virtualKey: 8, keyDown: down)      // 'c'
            e?.flags = .maskCommand; e?.post(tap: .cghidEventTap)
        }
        var changed = false
        for _ in 0..<10 { usleep(25_000); if pb.changeCount != before { changed = true; break } }
        let txt = changed ? (pb.string(forType: .string) ?? "") : ""
        if changed && !saved.isEmpty {
            pb.clearContents()
            pb.writeObjects(saved.map { pairs in let it = NSPasteboardItem(); for (t, d) in pairs { it.setData(d, forType: t) }; return it })
        }
        return txt
    }

    // ---- document hint (never logged as a path; the backend hashes it) ----
    func writeDocHint(front: NSRunningApplication, appEl: AXUIElement) {
        let path = "/tmp/speakeasy_cap_doc.json"
        try? FileManager.default.removeItem(atPath: path)
        if (Config.value("SPEAKEASY_DOC_READ") ?? "local") == "off" { return }
        var hint: [String: Any] = ["app": front.localizedName ?? "", "bundle": front.bundleIdentifier ?? ""]
        if let win: AXUIElement = attr(appEl, kAXFocusedWindowAttribute) {
            if let t: String = attr(win, kAXTitleAttribute) { hint["title"] = t }
            if let d: String = attr(win, kAXDocumentAttribute) { hint["ax_document"] = d }
        }
        var cur: AXUIElement? = attr(appEl, kAXFocusedUIElementAttribute)
        for _ in 0..<40 {
            guard let c = cur else { break }
            if let role: String = attr(c, kAXRoleAttribute), role == "AXWebArea" {
                if let u: URL = attr(c, kAXURLAttribute) { hint["url"] = u.absoluteString } else if let u: String = attr(c, kAXURLAttribute) { hint["url"] = u }
                break
            }
            cur = attr(c, kAXParentAttribute)
        }
        if let d = try? JSONSerialization.data(withJSONObject: hint) { FileManager.default.createFile(atPath: path, contents: d) }
    }

    // ---- tracker ----
    func frontWindow(_ pid: pid_t) -> (id: Int, frame: CGRect)? {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]],
              let w = list.first(where: { ($0[kCGWindowOwnerPID as String] as? Int32) == pid && ($0[kCGWindowLayer as String] as? Int) == 0 }),
              let wid = w[kCGWindowNumber as String] as? Int else { return nil }
        var f = CGRect.zero
        if let b = w[kCGWindowBounds as String] as? NSDictionary { CGRectMakeWithDictionaryRepresentation(b, &f) }
        return (wid, f)
    }
    func startTracker(pid: pid_t, wid: Int?) {
        killTracker()
        guard Config.on("SPEAKEASY_FOCUS"), Config.on("SPEAKEASY_TRACK"), let wid = wid else { return }
        tracker = run(trackerPath, ["--window-id", String(wid), "--pid", String(pid), "--home", home,
                                    "--fps", Config.value("SPEAKEASY_TRACK_FPS") ?? "5", "--overlay", "native", "--run-id", runId], stdin: nil)
        if let t = tracker { try? String(t.processIdentifier).write(toFile: home + "/tracker.pid", atomically: true, encoding: .utf8) }
    }

    // ---- helpers ----
    func rs(_ r: CGRect) -> String { "\(Int(r.minX)),\(Int(r.minY)),\(Int(r.width)),\(Int(r.height))" }
    func clamp(_ r: CGRect) -> CGRect {
        guard let s = NSScreen.screens.first?.frame else { return r }
        let w = min(r.width, s.width), h = min(r.height, s.height)
        return CGRect(x: max(0, min(r.minX, s.width - w)), y: max(0, min(r.minY, s.height - h)), width: w, height: h)
    }
    func capture(_ args: [String]) { run("/usr/sbin/screencapture", args, stdin: nil, wait: true) }
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
