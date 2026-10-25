// speakeasy-track: keeps the focus box glued to the source text while speakeasy reads.
// Locks onto one window (by CGWindowID) at hotkey time, then a few times a second:
//   1. AX: if the event carries an absolute character range and the app answers AXBoundsForRange, use that (cheap).
//   2. Otherwise capture just that window (ScreenCaptureKit) and run Apple Vision text recognition, then re-find the
//      current sentence's source lines among the recognised lines (token overlap) and box them tightly.
// Hides the box when the window is not frontmost, the content changed (tab switch), or the text scrolled off.
// Reads  ~/.speakeasy/focus.json (written by speakeasy as each sentence starts playing)
// Draws the box itself (native click-through panel, --overlay native, default): scroll / key / app-switch / AX events move or
// hide it in-process; OCR scans correct drift. Still writes ~/.speakeasy/track.json (Hammerspoon fallback overlay) and
// appends stats and per-event lag to ~/.speakeasy/logs/tracker.jsonl
// Everything is local (Vision + ScreenCaptureKit are part of macOS). Build: swiftc -O -parse-as-library tracker.swift
import Foundation
import AppKit
import ScreenCaptureKit
import Vision
import PDFKit
import ApplicationServices
import Darwin

// ---------------- small utils ----------------
func now() -> Double { Date().timeIntervalSince1970 }
let BUILD = "dev"                     // replaced with the git sha / source hash by install.sh
func readJSON(_ path: String) -> [String: Any]? {
    guard let d = FileManager.default.contents(atPath: path) else { return nil }
    return (try? JSONSerialization.jsonObject(with: d)) as? [String: Any]
}
func writeJSONAtomic(_ obj: [String: Any], _ path: String) {
    guard let d = try? JSONSerialization.data(withJSONObject: obj) else { return }
    let tmp = path + ".tmp"
    FileManager.default.createFile(atPath: tmp, contents: d)
    _ = rename(tmp, path)
}
let logQ = DispatchQueue(label: "speakeasy.log")          // log writes never run on the main thread
func appendLine(_ obj: [String: Any], _ path: String) {
    guard var d = try? JSONSerialization.data(withJSONObject: obj) else { return }
    d.append(0x0a)
    if Thread.isMainThread { logQ.async { writeLine(d, path) } } else { logQ.sync { writeLine(d, path) } }
}
func writeLine(_ d: Data, _ path: String) {
    if let h = FileHandle(forWritingAtPath: path) { h.seekToEndOfFile(); h.write(d); h.closeFile() }
    else { FileManager.default.createFile(atPath: path, contents: d) }
}
let STOP: Set<String> = ["the", "and", "for", "are", "was", "with", "this", "that", "from", "has", "its", "of", "to", "in", "on", "at", "is", "a", "an", "or", "by"]
func tokens(_ s: String) -> [String] {
    let low = s.precomposedStringWithCompatibilityMapping.lowercased()   // NFKC: PDF ligatures (fi, fl) match OCR text
    var out: [String] = []
    var cur = ""
    for ch in low.unicodeScalars {
        if CharacterSet.alphanumerics.contains(ch) { cur.unicodeScalars.append(ch) }
        else { if !cur.isEmpty { out.append(cur); cur = "" } }
    }
    if !cur.isEmpty { out.append(cur) }
    return out.filter { ($0.count >= 2 || $0.first!.isNumber) && !STOP.contains($0) }
}
func rss_mb() -> Double {
    var info = task_vm_info_data_t()
    var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
    let kr = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count) }
    }
    return kr == KERN_SUCCESS ? Double(info.phys_footprint) / 1_048_576.0 : 0
}
func cpu_s() -> Double {
    var ru = rusage(); getrusage(RUSAGE_SELF, &ru)
    return Double(ru.ru_utime.tv_sec + ru.ru_stime.tv_sec) + Double(ru.ru_utime.tv_usec + ru.ru_stime.tv_usec) / 1e6
}

// ---------------- window state ----------------
struct WinState { var frame: CGRect; var onScreen: Bool; var front: Bool; var frontOwner: String = ""; var frontId: Int = 0 }
func windowState(_ wid: CGWindowID) -> WinState? {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return nil }
    var frontWid: CGWindowID? = nil
    var mine: CGRect? = nil
    var frontOwner = ""
    let me = Int32(getpid())
    var srcPid: Int32? = nil, srcArea = 0.0
    for w in list where CGWindowID(w[kCGWindowNumber as String] as? Int ?? 0) == wid {
        srcPid = w[kCGWindowOwnerPID as String] as? Int32
        if let b = w[kCGWindowBounds as String] as? NSDictionary, let rr = CGRect(dictionaryRepresentation: b as CFDictionary) { srcArea = Double(rr.width * rr.height) }
    }
    for w in list {
        let owner = w[kCGWindowOwnerPID as String] as? Int32 ?? 0
        if owner == me { continue }                                       // our own overlay panel
        if let sp = srcPid, owner == sp, CGWindowID(w[kCGWindowNumber as String] as? Int ?? 0) != wid,
           let b = w[kCGWindowBounds as String] as? NSDictionary, let rr = CGRect(dictionaryRepresentation: b as CFDictionary),
           Double(rr.width * rr.height) < 0.6 * srcArea { continue }     // the app's own popups / bubbles / helper layers
        let layer = w[kCGWindowLayer as String] as? Int ?? 0
        let id = CGWindowID(w[kCGWindowNumber as String] as? Int ?? 0)
        let alpha = w[kCGWindowAlpha as String] as? Double ?? 1
        var r = CGRect.zero
        if let b = w[kCGWindowBounds as String] as? NSDictionary, let rr = CGRect(dictionaryRepresentation: b as CFDictionary) { r = rr }
        // the frontmost real window: skip invisible and tiny strips (toolbars, status items)
        if layer == 0 && alpha > 0.01 && r.width >= 80 && r.height >= 80 && frontWid == nil { frontWid = id; frontOwner = w[kCGWindowOwnerName as String] as? String ?? "" }
        if id == wid { mine = r }
    }
    if let f = mine { return WinState(frame: f, onScreen: true, front: frontWid == wid, frontOwner: frontOwner, frontId: Int(frontWid ?? 0)) }
    // not on screen (minimised / other Space): still report the frame if we can
    if let all = CGWindowListCopyWindowInfo([.optionIncludingWindow], wid) as? [[String: Any]], let w = all.first,
       let b = w[kCGWindowBounds as String] as? NSDictionary, let f = CGRect(dictionaryRepresentation: b as CFDictionary) {
        return WinState(frame: f, onScreen: false, front: false)
    }
    return nil
}

// ---------------- AX ----------------
func axBounds(_ el: AXUIElement, _ loc: Int, _ len: Int) -> CGRect? {
    var r = CFRange(location: loc, length: max(1, len))
    guard let v = AXValueCreate(.cfRange, &r) else { return nil }
    var out: CFTypeRef?
    let err = AXUIElementCopyParameterizedAttributeValue(el, kAXBoundsForRangeParameterizedAttribute as CFString, v, &out)
    guard err == .success, let o = out, CFGetTypeID(o) == AXValueGetTypeID() else { return nil }
    var rect = CGRect.zero
    guard AXValueGetValue(o as! AXValue, .cgRect, &rect), rect.width > 1, rect.height > 1 else { return nil }
    return rect
}
func focusedElement(_ pid: pid_t) -> AXUIElement? {
    let app = AXUIElementCreateApplication(pid)
    var v: CFTypeRef?
    guard AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute as CFString, &v) == .success, let e = v else { return nil }
    return (e as! AXUIElement)
}


// ---------------- native overlay + event-driven following (main thread) ----------------
// The box is drawn here (a borderless, click-through, non-activating panel) so scroll / AX / app-switch events move or
// hide it in-process, without a file relay. Scroll: AX re-query of the sentence range when the app answers it, else the
// scroll delta times a calibrated scale; text-recognition scans correct drift. Every draw / hide / scroll / ax event is
// appended to tracker.jsonl at once (schema in the README).
func uptimeNs() -> UInt64 { clock_gettime_nsec_np(CLOCK_UPTIME_RAW) }
final class Live: NSObject {
    static let shared = Live()
    var home = ""
    var log = ""
    var panel: NSPanel? = nil
    var shape = CAShapeLayer()
    var rect: CGRect? = nil              // target text box, global top-left coordinates
    var isEllipse = false
    var front = true, winFront = true, hiddenByKey = false, scanVisible = false
    var keyHideAt = 0.0          // when a tab-switch key hid the box; a later scan of the same page brings it back
    var winFrame: CGRect? = nil
    var srcPid: pid_t = 0
    var axEl: AXUIElement? = nil
    var range: [Int]? = nil
    var selBase: Int? = nil              // AX offset of the selection at hotkey time
    var selText = ""                     // the selected text (to place each sentence's source line inside it)
    var axGood = false
    var lastMove = 0.0
    var kCont = 1.0, kLine = 1.0, pred = 0.0, predCont = true
    var off = CGPoint.zero               // scrolling since the hotkey: speakeasy's own boxes predate it
    var link: AnyObject? = nil
    var dirty = false
    var observer: AXObserver? = nil
    var tap: CFMachPort? = nil
    var src = "event"                    // where the current rect came from: event / ax / ocr / doc / scroll-delta
    var seq = -1
    // pending event timing
    var pendingScroll: [UInt64] = []
    var hideTrigger: (String, Double)? = nil
    var shown = false, lastDrawn = NSRect.zero
    // counters for the exit record
    var draws = 0, hides = 0, scrollEvents = 0, axEvents = 0, axHits = 0, ocrHits = 0, deltaHits = 0
    var lagN = 0, lagSum = 0.0, lagMax = 0.0
    var hideN = 0, hideSum = 0.0, hideMax = 0.0
    var logN = 0
    var drawnSeq = -2
    var axFail = ""
    var axMisses = 0                     // consecutive AX misses; at 2 "no-bounds" the app has no text geometry (Electron): stop asking
    var axDead = false
    var deltaOK = false                  // scroll-delta predictions match OCR: OCR can slow down
    var stallN = 0, stallMax = 0.0, lastWork = "idle"
    var hb: Timer? = nil
    var lastFlush = 0.0
    var drawFails = 0
    var refPanel: NSPanel? = nil
    // shape scheme: text_band | part_oval | figure_box | table_row; table_header is a faint tint kept while a table is read
    var cueKind = "text_band"
    var drawMeta: [String: Any] = [:]       // run_id, label, doc_transform of the current event, for draw records
    var headerPanel: NSPanel? = nil
    let headerShape = CAShapeLayer()
    var headerDelta: CGRect? = nil          // header rect relative to the row band's origin (moves with it on scroll)
    let refShape = CAShapeLayer()
    var refShown = 0, transitions = 0, heldBig = 0
    var cue = "text", suppressed = false
    var colRect: CGRect? = nil, lineH: CGFloat = 0
    var stuckVotes = 0, lastMomentum = 0.0
    var pendingJump: CGRect? = nil
    var hScrollOK = false                // horizontal deltas only once OCR confirmed the content really moves sideways
    var stuckY = 0                       // +1/-1: content did not move under deltas of that sign (document edge)
    var predX = 0.0, predAt = 0.0, rejX = 0.0
    var wantScan = false                 // a scroll just happened / was rejected: confirm position at once
    var rejN: [String: Int] = [:], rejTotal = 0, lastRejLog = 0.0, corrections = 0
    var runId = ""
    var winTimer: DispatchSourceTimer? = nil
    // Window-front watch on its own queue every 25 ms, independent of the scan loop (which can sit in a 0.5 s capture + OCR).
    func startWinWatch(_ wid: CGWindowID) {
        let t = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .userInteractive))
        t.schedule(deadline: .now(), repeating: .milliseconds(25))
        let pid = srcPid
        t.setEventHandler {
            guard let w = windowState(wid) else { return }
            let fr = w.front && w.onScreen
            let appFront = NSWorkspace.shared.frontmostApplication?.processIdentifier == pid
            let t0 = now()
            DispatchQueue.main.async {
                let L = Live.shared
                if L.winFront != fr {
                    if !fr && L.shown { L.hideTrigger = (appFront ? (w.onScreen ? "window-switch" : "not-frontmost") : "app-switch", t0) }
                    L.winFront = fr; L.winFrame = w.frame; L.apply()
                }
            }
        }
        t.resume(); winTimer = t
    }

    func rec(_ o: [String: Any]) {
        if logN > 6000 { return }
        logN += 1
        var d = o; d["ts"] = (now() * 1000).rounded() / 1000; d["tracker_pid"] = Int(getpid())
        appendLine(d, log)
    }
    func setup(home: String, pid: pid_t, el: AXUIElement?) {
        self.home = home; log = home + "/logs/tracker.jsonl"; srcPid = pid; axEl = el
        if let e = el {                                          // the selection at hotkey time: anchors the sentence ranges
            var v: CFTypeRef?
            if AXUIElementCopyAttributeValue(e, kAXSelectedTextRangeAttribute as CFString, &v) == .success, let x = v, CFGetTypeID(x) == AXValueGetTypeID() {
                var r = CFRange(location: 0, length: 0)
                if AXValueGetValue(x as! AXValue, .cfRange, &r), r.length > 0 { selBase = r.location }
            }
            var t: CFTypeRef?
            if AXUIElementCopyAttributeValue(e, kAXSelectedTextAttribute as CFString, &t) == .success, let s = t as? String { selText = s }
        }
        rec(["event": "ax-selection", "has_element": el != nil, "has_range": selBase != nil, "sel_chars": selText.count])
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 10, height: 10), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = false; p.ignoresMouseEvents = true
        p.level = .statusBar; p.hidesOnDeactivate = false; p.isReleasedWhenClosed = false
        p.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        let v = NSView(frame: p.contentView!.bounds); v.wantsLayer = true; v.autoresizingMask = [.width, .height]
        shape.lineWidth = 1.5
        v.layer!.addSublayer(shape)
        p.contentView = v
        panel = p
        p.alphaValue = 0; p.orderFrontRegardless(); p.orderOut(nil); p.alphaValue = 1   // pay the first-show cost now, not mid-sentence
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main) { [weak self] n in
            guard let s = self, let app = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else { return }
            let f = app.processIdentifier == s.srcPid
            if !f && s.front { s.hideTrigger = ("app-switch", now()) }
            s.front = f; s.apply()
        }
        startTap()
        startAXObserver()
        var last = now()
        hb = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in   // main-thread stall detector
            guard let s = self else { return }
            let t = now(), late = (t - last - 0.05) * 1000; last = t
            if late > 100 { s.stallN += 1; s.stallMax = max(s.stallMax, late)
                s.rec(["event": "main-stall", "late_ms": late.rounded(), "after": s.lastWork]) }
        }
        RunLoop.main.add(hb!, forMode: .common)
        // No view display link: it only ticks while the panel is on screen, so a hidden panel was never shown (draws: 0).
        // Coalesce instead: apply at most once per main-loop turn.
    }
    func style(color: String?, opacity: Double, ellipse: Bool) {
        var r = 0.23, g = 0.51, b = 0.96
        if let c = color, c.count == 7, let v = Int(c.dropFirst(), radix: 16) { r = Double((v >> 16) & 255) / 255; g = Double((v >> 8) & 255) / 255; b = Double(v & 255) / 255 }
        CATransaction.begin(); CATransaction.setDisableActions(true)
        shape.fillColor = CGColor(red: r, green: g, blue: b, alpha: opacity)
        shape.strokeColor = CGColor(red: r, green: g, blue: b, alpha: min(1, opacity * 3))
        CATransaction.commit()
        isEllipse = ellipse
    }
    // Text: a soft tinted band (low-alpha fill, rounded, no border). Figure/table parts: a distinct outline or circle.
    func cueStyle(figure: Bool, color: String?) {
        var r = 0.23, g = 0.51, b = 0.96
        if let c = color, c.count == 7, let v = Int(c.dropFirst(), radix: 16) { r = Double((v >> 16) & 255) / 255; g = Double((v >> 8) & 255) / 255; b = Double(v & 255) / 255 }
        CATransaction.begin(); CATransaction.setDisableActions(true)
        if cueKind == "table_row" {                                   // a row reads like text: the same soft band
            shape.fillColor = CGColor(red: r, green: g, blue: b, alpha: 0.16)
            shape.strokeColor = CGColor(red: r, green: g, blue: b, alpha: 0); shape.lineWidth = 0
        } else if figure {
            shape.fillColor = CGColor(red: r, green: g, blue: b, alpha: 0.06)
            shape.strokeColor = CGColor(red: r, green: g, blue: b, alpha: 0.9); shape.lineWidth = 2
        } else {
            shape.fillColor = CGColor(red: r, green: g, blue: b, alpha: 0.16)
            shape.strokeColor = CGColor(red: r, green: g, blue: b, alpha: 0); shape.lineWidth = 0
        }
        CATransaction.commit()
    }
    func frameFor(_ r: CGRect) -> NSRect {
        let H = NSScreen.screens.first?.frame.height ?? 0
        let (dx, dy): (CGFloat, CGFloat) = isEllipse ? (r.width * 0.12 + 6, r.height * 0.12 + 6) : (4, 2)
        let g = r.insetBy(dx: -dx, dy: -dy)
        return NSRect(x: g.minX, y: H - g.maxY, width: g.width, height: g.height)
    }
    func apply(scrollTs: UInt64? = nil) {
        if let t = scrollTs { pendingScroll.append(t) }
        if dirty { return }
        dirty = true
        // coalesce: a trackpad sends 60-120 scroll events/s (plus momentum); move the panel at most once per ~8 ms
        let wait = max(0, 0.008 - (now() - lastFlush))
        if wait == 0 && Thread.isMainThread { flush() }
        else { DispatchQueue.main.asyncAfter(deadline: .now() + wait) { self.flush() } }
    }
    @objc func tickLink(_ l: AnyObject) { if dirty { flush() } }
    func hideReason() -> String {
        if let t = hideTrigger { return t.0 }
        if rect == nil { return "speech-finished" }
        if !front { return "app-switch" }
        if !winFront { return "not-frontmost" }
        if hiddenByKey { return "tab-switch" }
        return "scrolled-off"
    }
    func flush() {
        dirty = false; lastWork = "flush"; lastFlush = now()
        guard let p = panel else { return }
        var inWin = true
        if let r = rect, let w = winFrame {
            let vis = r.intersection(w)
            inWin = !vis.isNull && vis.width * vis.height >= 0.3 * r.width * r.height
            if !inWin && shown { hideTrigger = ("out-of-window", now()); rec(["event": "box-out-of-window", "rect": [Int(r.minX), Int(r.minY), Int(r.width), Int(r.height)]]); wantScan = true }
        }
        let show = rect != nil && front && winFront && !hiddenByKey && scanVisible && !suppressed && inWin
        if show, var r = rect {
            if let w = winFrame { r = r.intersection(w) }                 // clamp to the window
            let f = frameFor(r)
            CATransaction.begin(); CATransaction.setDisableActions(true)
            p.setFrame(f, display: false)
            let b = CGRect(x: 1, y: 1, width: f.width - 2, height: f.height - 2)
            shape.frame = p.contentView!.bounds
            let rad: CGFloat = cueKind == "figure_box" ? min(12, min(b.width, b.height) / 4) : 6
            shape.path = isEllipse ? CGPath(ellipseIn: b, transform: nil) : CGPath(roundedRect: b, cornerWidth: rad, cornerHeight: rad, transform: nil)
            CATransaction.commit()
            if !p.isVisible { p.orderFrontRegardless() }
            drawHeader(r)
            if !shown || abs(f.minY - lastDrawn.minY) > 2 || abs(f.minX - lastDrawn.minX) > 2 || abs(f.width - lastDrawn.width) > 2 || abs(f.height - lastDrawn.height) > 2 {
                draws += 1
                let H = NSScreen.screens.first?.frame.height ?? 0
                rec(drawMeta.merging(["event": "draw", "seq": seq, "cue": cue, "cue_kind": cueKind, "header": headerDelta != nil, "rect": [Int(f.minX), Int(H - f.maxY), Int(f.width), Int(f.height)],
                     "text_rect": [Int(r.minX), Int(r.minY), Int(r.width), Int(r.height)], "src": src, "overlay": "swift-panel",
                     "on_screen": p.isVisible]) { $1 })
                shown = true; lastDrawn = f
            }
            if drawnSeq != seq { drawnSeq = seq; let o: [String: Any] = ["seq": seq, "tracker_pid": Int(getpid()), "drawn": true, "ts": now()], h = home
                logQ.async { writeJSONAtomic(o, h + "/overlay.json") } }
            hideTrigger = nil
        } else {
            if p.isVisible { p.orderOut(nil) }
            refPanel?.orderOut(nil); headerPanel?.orderOut(nil)
            if shown {
                shown = false; hides += 1
                let trig = hideReason()
                var ms: Double? = nil
                if let t = hideTrigger { ms = (now() - t.1) * 1000; hideN += 1; hideSum += ms!; hideMax = max(hideMax, ms!) }
                rec(["event": "hide", "seq": seq, "trigger": trig, "hide_ms": ms.map { ($0 * 10).rounded() / 10 } ?? NSNull()])
            }
            hideTrigger = nil
        }
        let n = uptimeNs()
        var bmax = 0.0, bn = 0
        for t in pendingScroll where t > 0 && n > t {
            let ms = Double(n - t) / 1e6
            if ms < 5000 { lagN += 1; lagSum += ms; lagMax = max(lagMax, ms); bmax = max(bmax, ms); bn += 1 }
        }
        if bn > 0 { rec(["event": "scroll", "scroll_lag_ms": (bmax * 10).rounded() / 10, "events": bn, "src": src]) }   // one record per panel move
        pendingScroll.removeAll()
    }
    // ---- scans from the tracker loop ----
    func reject(_ why: String) {
        rejN[why, default: 0] += 1; rejTotal += 1
        let t = now()
        if t - lastRejLog > 0.5 { lastRejLog = t; rec(["event": "scroll-rejected", "reason": why, "counts": rejN]); rejN = [:] }
    }
    func scan(rect r: CGRect?, visible: Bool, capTs: Double, method: String, reason: String?) {
        lastWork = "scan"
        if capTs > 0 && capTs < lastMove { return }               // screenshot taken before the last scroll: stale
        wantScan = false
        if visible, let r = r, let old = rect {
            let ex = r.minX - old.minX, ey = r.minY - old.minY
            // verify the prediction: content that did not move under accepted deltas -> that direction is stuck (edge)
            let settled = capTs > 0 && capTs - lastMove >= 0.15 && now() - lastMomentum > 0.25
            if abs(pred) >= 80 && settled {
                let actual = pred + ey
                if abs(actual) < 0.3 * abs(pred) {
                    stuckVotes += 1
                    if stuckVotes >= 2 { stuckY = pred > 0 ? 1 : -1; rec(["event": "scroll-stuck", "dir": stuckY, "pred": Int(pred), "actual": Int(actual), "votes": stuckVotes]) }
                    else { rec(["event": "scroll-stuck-vote", "pred": Int(pred), "actual": Int(actual)]); return }   // keep the prediction for now
                } else { stuckVotes = 0; stuckY = 0 }
            } else if abs(pred) >= 80 { wantScan = true; return }                        // unsettled capture: do not judge (or snap) yet
            // a big snap needs two agreeing scans
            if abs(ey) > 400 && src == "scroll-delta" {
                if let pj = pendingJump, abs(pj.minY - r.minY) < 20 { pendingJump = nil }
                else { pendingJump = r; rec(["event": "jump-pending", "dy": Int(ey)]); wantScan = true; return }
            }
            if abs(predX) >= 30 {
                let ax = predX + ex
                hScrollOK = abs(ax) >= 0.5 * abs(predX)
            } else if abs(rejX) >= 30 && abs(ex) >= 0.5 * abs(rejX) && (ex > 0) == (rejX > 0) {
                hScrollOK = true; rec(["event": "hscroll-confirmed", "rejected_dx": Int(rejX), "moved": Int(ex)])   // it really scrolls sideways
            }
            if stuckY != 0 && abs(pred) < 30 && abs(ey) >= 30 { stuckY = 0 }          // content moved after all (e.g. more loaded)
            rejX = 0
            if abs(ex) > 8 || abs(ey) > 8 {
                corrections += 1
                rec(["event": "correction", "dx": Int(ex), "dy": Int(ey), "from": src, "to": method, "after_ms": Int((now() - predAt) * 1000)])
            }
            predX = 0
        }
        if visible, let r = r {
            if let old = rect, abs(pred) >= 20, method != "ax" {
                let actual = pred + (r.minY - old.minY)
                let k = (predCont ? kCont : kLine) * actual / pred
                if k > 0.2 && k < 5 { if predCont { kCont = 0.5 * kCont + 0.5 * k } else { kLine = 0.5 * kLine + 0.5 * k } }
            }
            if method == "ocr" || method == "doc" { ocrHits += 1 } else if method == "ax" { axHits += 1 }
            if src == "scroll-delta", let old = rect { deltaOK = abs(old.minY - r.minY) <= 12 && abs(old.minX - r.minX) <= 12 }
            if method == "ocr" || method == "doc" {               // learn the text column and line height
                colRect = colRect.map { CGRect(x: min($0.minX, r.minX), y: 0, width: max($0.maxX, r.maxX) - min($0.minX, r.minX), height: 0) } ?? CGRect(x: r.minX, y: 0, width: r.width, height: 0)
                if r.height < 30 { lineH = lineH > 0 ? 0.7 * lineH + 0.3 * r.height : r.height }
            }
            pred = 0; rect = r; scanVisible = true; hiddenByKey = false; src = method
        } else {
            if scanVisible { hideTrigger = (reason ?? "scrolled-off", now()) }
            scanVisible = false
        }
        apply()
    }
    func shift(dx: CGFloat, dy: CGFloat, capTs: Double) {
        if capTs > 0 && capTs < lastMove { return }               // a scroll since the capture already moved it
        if let r = rect { rect = r.offsetBy(dx: dx, dy: dy); src = "fig-anchor"; apply() }
    }
    func drawHeader(_ row: CGRect) {
        guard cueKind == "table_row", let d = headerDelta, !suppressed else { headerPanel?.orderOut(nil); return }
        if headerPanel == nil {
            let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 10, height: 10), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = false; p.ignoresMouseEvents = true
            p.level = .statusBar; p.hidesOnDeactivate = false; p.isReleasedWhenClosed = false
            p.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
            let v = NSView(frame: p.contentView!.bounds); v.wantsLayer = true; v.autoresizingMask = [.width, .height]
            v.layer!.addSublayer(headerShape); p.contentView = v; headerPanel = p
        }
        guard let p = headerPanel else { return }
        var h = CGRect(x: row.minX + d.minX, y: row.minY + d.minY, width: d.width, height: d.height)
        if let w = winFrame { h = h.intersection(w) }                    // clipped to the window like the band
        if h.isNull || h.height < 3 { p.orderOut(nil); return }
        let H = NSScreen.screens.first?.frame.height ?? 0
        let g = h.insetBy(dx: -4, dy: -2), f = NSRect(x: g.minX, y: H - g.maxY, width: g.width, height: g.height)
        CATransaction.begin(); CATransaction.setDisableActions(true)
        p.setFrame(f, display: false); headerShape.frame = p.contentView!.bounds
        headerShape.path = CGPath(roundedRect: CGRect(x: 1, y: 1, width: f.width - 2, height: f.height - 2), cornerWidth: 5, cornerHeight: 5, transform: nil)
        headerShape.fillColor = shape.fillColor?.copy(alpha: 0.07); headerShape.strokeColor = nil
        CATransaction.commit()
        p.orderFrontRegardless()
    }
    func showRef(_ r: CGRect) {
        if refPanel == nil {
            let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 10, height: 10), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = false; p.ignoresMouseEvents = true
            p.level = .statusBar; p.hidesOnDeactivate = false; p.isReleasedWhenClosed = false
            p.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
            let v = NSView(frame: p.contentView!.bounds); v.wantsLayer = true; v.autoresizingMask = [.width, .height]
            refShape.lineWidth = 1; refShape.lineDashPattern = [4, 3]
            v.layer!.addSublayer(refShape); p.contentView = v; refPanel = p
        }
        guard let p = refPanel, front, winFront else { return }
        let f = frameFor(r.offsetBy(dx: off.x, dy: off.y))
        CATransaction.begin(); CATransaction.setDisableActions(true)
        p.setFrame(f, display: false)
        refShape.frame = p.contentView!.bounds
        let b = CGRect(x: 1, y: 1, width: f.width - 2, height: f.height - 2)
        refShape.path = CGPath(roundedRect: b, cornerWidth: 5, cornerHeight: 5, transform: nil)
        refShape.fillColor = CGColor(red: 0.23, green: 0.51, blue: 0.96, alpha: 0.05)
        refShape.strokeColor = CGColor(red: 0.23, green: 0.51, blue: 0.96, alpha: 0.45)
        CATransaction.commit()
        p.orderFrontRegardless()
        refShown += 1
        rec(["event": "ref-outline", "seq": seq, "rect": [Int(r.minX), Int(r.minY), Int(r.width), Int(r.height)], "ms": 1500])
        let my = seq
        _ = my; DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { p.orderOut(nil) }
    }
    func event(seq s: Int, rect r0: CGRect?, range rg: [Int]?, srcLines: [String], color: String?, opacity: Double, ellipse: Bool,
               isText: Bool = true, nlines: Int? = nil, ref: CGRect? = nil, figure: Bool = false, drawOn: Bool = true,
               cueKind ck: String? = nil, header: CGRect? = nil, meta: [String: Any] = [:]) {
        var r = r0
        drawMeta = meta.merging(["run_id": runId]) { a, _ in a }
        cue = figure ? "figure" : "text"; suppressed = !drawOn
        cueKind = ck ?? (ellipse ? "part_oval" : figure ? "figure_box" : "text_band")
        if cueKind == "table_row", let h = header, let n = r0 { headerDelta = CGRect(x: h.minX - n.minX, y: h.minY - n.minY, width: h.width, height: h.height) }
        else { headerDelta = nil }                              // row 0 is the header itself; other cues have none
        cueStyle(figure: figure, color: color)
        if figure { refPanel?.orderOut(nil) }                    // one cue at a time: no text outline while a figure part is boxed
        if !drawOn { rec(["event": "cue-off", "seq": s, "cue": cue]) }
        // never flash a big provisional box between sentences: a text sentence's box must fit its own lines
        if let n = r0, isText {
            var nr = n.offsetBy(dx: off.x, dy: off.y)
            // the backend only knows the selection box (no per-line geometry in web views): take x/width from the text
            // column OCR has confirmed, and cap the height, so the band can move at once instead of waiting for OCR
            if let c = colRect, nr.width > c.width * 1.2 { nr = CGRect(x: c.minX, y: nr.minY, width: c.width, height: nr.height) }
            let lh = lineH > 0 ? lineH : 20
            // cap only when the backend says how many lines the sentence has (an unknown count used to cap every box at
            // 2 lines = 128 pt, leaving long sentences' boxes 40-110 pt short)
            let maxH = nlines.map { CGFloat($0) * lh * 3 + 8 } ?? .greatestFiniteMagnitude
            if nr.height > maxH { nr = CGRect(x: nr.minX, y: nr.minY, width: nr.width, height: maxH) }
            let limit = nlines.map { CGFloat($0) * 40 + 12 } ?? .greatestFiniteMagnitude
            let held = (nr.height > max(limit, maxH + 1) || (colRect == nil && nr.width > 700)) && rect != nil
            if !held { r = CGRect(x: nr.minX - off.x, y: nr.minY - off.y, width: nr.width, height: nr.height) }
            wantScan = true                                         // targeted OCR right away for the exact lines
            transitions += 1
            if held { heldBig += 1 }
            rec(["event": "transition", "seq": s, "old": rect.map { [Int($0.width), Int($0.height)] } ?? NSNull(),
                 "new": [Int(nr.width), Int(nr.height)], "nlines": nlines ?? NSNull(), "held": held])
            if held { r = nil }                                     // keep the previous box until OCR / AX finds the tight one
        }
        if let rf = ref { showRef(rf) }
        seq = s
        isEllipse = ellipse
        range = rg
        if range == nil, let base = selBase, let first = srcLines.first, !first.isEmpty {
            // place the sentence's source lines inside the selected text -> an absolute AX range
            let all = srcLines.joined(separator: "\n")
            if let lo = selText.range(of: first) {
                let start = selText.distance(from: selText.startIndex, to: lo.lowerBound)
                let lastLine = srcLines.last ?? first
                var len = first.count
                if let hi = selText.range(of: lastLine, range: lo.lowerBound..<selText.endIndex) {
                    len = selText.distance(from: lo.lowerBound, to: hi.upperBound)
                }
                range = [base + start, max(1, min(len, all.count + 40))]
            }
        }
        axGood = false
        if let r = r { rect = r.offsetBy(dx: off.x, dy: off.y); scanVisible = true; src = "event" }
        apply()                                                   // draw the event box now; AX refines it later
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {   // no fallback box any more: fail loudly instead
            guard self.seq == s, self.drawnSeq != s, self.rect != nil, !self.suppressed, self.front, self.winFront, self.scanVisible, !self.hiddenByKey else { return }
            let why = self.panel == nil ? "no panel" : (self.panel!.isVisible ? "visible but no draw record" : "panel not on screen")
            self.drawFails += 1
            self.rec(["event": "draw-failed", "seq": s, "why": why, "after_ms": 300])
            let line = "\(stamp()) NATIVE PANEL FAILED TO DRAW: seq \(s) (\(why)), run \(self.runId)\n"
            if let h = FileHandle(forWritingAtPath: self.home + "/logs/hs.log") { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile() }
            FileHandle.standardError.write(line.data(using: .utf8)!)
        }
        if axDead { rec(["event": "ax-range", "seq": s, "hit": false, "why": "ax-disabled"]); return }
        guard let el = axEl, let rg = range, rg.count == 2 else {
            axFail = axEl == nil ? "no-element" : "no-range"
            rec(["event": "ax-range", "seq": s, "range": range ?? NSNull(), "hit": false, "why": axFail]); return
        }
        let t0 = now()
        DispatchQueue.global(qos: .userInteractive).async {        // AX IPC to a busy app can take 100s of ms: never on main
            let r = axBounds(el, rg[0], rg[1])
            DispatchQueue.main.async {
                guard self.seq == s else { return }
                let ok = self.takeAX(r)
                if ok { self.axHits += 1; self.apply() }
                self.rec(["event": "ax-range", "seq": s, "range": rg, "hit": ok, "why": ok ? NSNull() : self.axFail, "ax_ms": ((now() - t0) * 10000).rounded() / 10])
            }
        }
    }
    func takeAX(_ b: CGRect?) -> Bool {
        guard let r = b else {
            axFail = "no-bounds"; axGood = false; axMisses += 1
            if axMisses >= 2 && !axDead { axDead = true; gAxDead = true; rec(["event": "ax-disabled", "why": "no-bounds x2 (app exposes no text geometry)"]) }
            return false
        }
        axMisses = 0
        if let w = winFrame, !(r.width < w.width * 0.6 || r.width < 320) { axFail = "too-wide \(Int(r.width))"; axGood = false; return false }
        axFail = ""
        if let w = winFrame, !w.intersects(r) { if scanVisible { hideTrigger = ("scrolled-off", now()) }; scanVisible = false; axGood = true; return true }
        rect = r; axGood = true; scanVisible = true; src = "ax"; return true
    }
    @discardableResult
    func requeryAX() -> Bool {
        guard let el = axEl else { axFail = "no-element"; axGood = false; return false }
        guard let rg = range, rg.count == 2 else { axFail = "no-range"; axGood = false; return false }
        if axDead { return false }
        lastWork = "ax-requery"
        return takeAX(axBounds(el, rg[0], rg[1]))
    }
    // ---- events ----
    func startTap() {
        let mask = (1 << CGEventType.scrollWheel.rawValue) | (1 << CGEventType.keyDown.rawValue)
        let cb: CGEventTapCallBack = { _, type, ev, _ in
            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let t = Live.shared.tap { CGEvent.tapEnable(tap: t, enable: true) }
                return Unmanaged.passUnretained(ev)
            }
            Live.shared.handle(type, ev)
            return Unmanaged.passUnretained(ev)
        }
        guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .tailAppendEventTap, options: .listenOnly,
                                        eventsOfInterest: CGEventMask(mask), callback: cb, userInfo: nil) else {
            rec(["event": "tap", "ok": false]); return
        }
        tap = t
        CFRunLoopAddSource(CFRunLoopGetMain(), CFMachPortCreateRunLoopSource(nil, t, 0), .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        rec(["event": "tap", "ok": true])
    }
    func handle(_ type: CGEventType, _ ev: CGEvent) {
        guard front, rect != nil else { return }
        if type == .keyDown {
            let f = ev.flags, kc = ev.getIntegerValueField(.keyboardEventKeycode)
            let cmd = f.contains(.maskCommand), ctrl = f.contains(.maskControl), sh = f.contains(.maskShift), alt = f.contains(.maskAlternate)
            let tabSwitch = isTabSwitchKey(cmd: cmd, ctrl: ctrl, shift: sh, alt: alt, keyCode: kc)
                || (cmd && alt && (kc == 123 || kc == 124)) || (cmd && (kc == 17 || kc == 13)) || [116, 121, 115, 119].contains(kc)
            if tabSwitch { hiddenByKey = true; keyHideAt = now(); lastMove = now(); hideTrigger = ("tab-switch", now()); apply() }
            return
        }
        guard let w = winFrame, w.contains(ev.location) else { return }
        let dy0 = Double(ev.getIntegerValueField(.scrollWheelEventPointDeltaAxis1))
        let dx0 = Double(ev.getIntegerValueField(.scrollWheelEventPointDeltaAxis2))
        if dx0 == 0 && dy0 == 0 { return }
        scrollEvents += 1
        let cont = ev.getIntegerValueField(.scrollWheelEventIsContinuous) != 0
        let k = cont ? kCont : kLine
        // Never trust deltas blindly: a trackpad keeps sending them when the content cannot move (sideways in a page
        // that does not scroll horizontally, past the end, rubber-band and momentum after an edge).
        var ddx = dx0, ddy = dy0
        if ddx != 0 && !hScrollOK { reject("horizontal"); rejX += ddx * k; ddx = 0 }
        if ddy != 0 && stuckY != 0 && (ddy > 0) == (stuckY > 0) {
            let mom = ev.getIntegerValueField(.scrollWheelEventMomentumPhase) != 0
            if mom { lastMomentum = now() }
            reject(mom ? "overscroll" : "no-content-move"); ddy = 0
        }
        if ddy != 0 && (ddy > 0) != (stuckY > 0) && stuckY != 0 { stuckY = 0 }      // reversing direction: content can move again
        if ddx == 0 && ddy == 0 { wantScan = true; return }
        if ev.getIntegerValueField(.scrollWheelEventMomentumPhase) != 0 { lastMomentum = now() }
        lastMove = now(); predAt = now(); wantScan = true
        predX += ddx * k
        off.x += ddx * k; off.y += ddy * k
        let dx = ddx, dy = ddy
        if range != nil && axGood && requeryAX() { axHits += 1; apply(scrollTs: ev.timestamp); return }   // exact: ask the app
        if let r = rect { rect = r.offsetBy(dx: dx * k, dy: dy * k); src = "scroll-delta"; deltaHits += 1 }
        pred += dy * k; predCont = cont
        apply(scrollTs: ev.timestamp)
    }
    func startAXObserver() {
        guard srcPid > 0, AXIsProcessTrusted() else { rec(["event": "ax-observer", "ok": false, "why": "not trusted"]); return }
        var obs: AXObserver?
        let cb: AXObserverCallback = { _, _, note, _ in
            let s = Live.shared
            if s.axDead || s.range == nil || !s.axGood { s.axEvents += 1; return }   // only re-query while AX is working
            s.axEvents += 1
            let t0 = now()
            let ok = s.range != nil && s.requeryAX()
            if ok { s.axHits += 1; s.apply() }
            s.rec(["event": "ax", "notif": note as String, "ax_ms": ((now() - t0) * 10000).rounded() / 10, "hit": ok])
        }
        guard AXObserverCreate(srcPid, cb, &obs) == .success, let o = obs else { rec(["event": "ax-observer", "ok": false, "why": "create failed"]); return }
        let app = AXUIElementCreateApplication(srcPid)
        var added = 0
        for n in [kAXValueChangedNotification, kAXSelectedTextChangedNotification, kAXLayoutChangedNotification] {
            if let el = axEl, AXObserverAddNotification(o, el, n as CFString, nil) == .success { added += 1 }
        }
        _ = app
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(o), .commonModes)
        observer = o
        rec(["event": "ax-observer", "ok": true, "notifications": added])
    }
    func stats() -> [String: Any] {
        ["draws": draws, "hides": hides, "scroll_events": scrollEvents, "ax_events": axEvents, "ax_hits": axHits, "ocr_hits": ocrHits,
         "scroll_delta_hits": deltaHits,
         "scroll_lag_avg_ms": lagN > 0 ? (lagSum / Double(lagN) * 10).rounded() / 10 : NSNull(), "scroll_lag_max_ms": (lagMax * 10).rounded() / 10,
         "hide_ms_avg": hideN > 0 ? (hideSum / Double(hideN) * 10).rounded() / 10 : NSNull(), "hide_ms_max": (hideMax * 10).rounded() / 10,
         "draw_failed": drawFails, "scroll_rejected": rejTotal, "corrections": corrections, "transitions": transitions, "transitions_held_big": heldBig, "ref_outlines": refShown, "main_stalls": stallN, "main_stall_max_ms": stallMax.rounded(), "ax_disabled": axDead,
         "k_trackpad": (kCont * 100).rounded() / 100, "k_wheel": (kLine * 100).rounded() / 100]
    }
}

// ---------------- OCR ----------------
struct OLine { let text: String; let toks: Set<String>; let box: CGRect }   // box in screen points (top-left origin)

// Document-aware focus: speakeasy sends a target in PDF page space plus text anchors (page lines with their page rects).
// Match anchors to recognised lines, fit page->screen (uniform scale + offset), map the target (exact line / figure / panel).
var gDocDiag: [String: Int] = [:]
func docMap(_ lines: [OLine], _ doc: [String: Any], frame: CGRect? = nil) -> (CGRect, Int, Double)? {
    gDocDiag = [:]
    guard let tg = doc["target"] as? [Double], tg.count == 4, let anc = doc["anchors"] as? [[Any]] else { return nil }
    var P: [(Double, Double)] = [], S: [(Double, Double)] = []
    for a in anc {
        guard a.count == 5, let t = a[4] as? String else { continue }
        let c = a[0..<4].compactMap { ($0 as? Double) ?? ($0 as? Int).map(Double.init) }
        if c.count != 4 { continue }
        let at = Set(tokens(t)); if at.count < 3 { continue }
        let cands = lines.filter { l in
            let inter = Double(l.toks.intersection(at).count)
            return inter / Double(max(1, at.count)) >= 0.75 && inter / Double(max(1, l.toks.count)) >= 0.6 }
        if cands.count != 1 { gDocDiag[cands.isEmpty ? "unmatched" : "ambiguous", default: 0] += 1; continue }
        let b = cands[0].box
        // a line cut by the window/capture edge starts mid-word: its left edge is not the page line's left edge
        if let f = frame, b.minX <= f.minX + 3 || b.maxX >= f.maxX - 3 { continue }
        // OCR line boxes start at the first glyph: use the left edge and vertical centre
        P.append((c[0], (c[1] + c[3]) / 2)); S.append((Double(b.minX), Double(b.midY)))
    }
    // fit, drop the worst outliers (wrongly matched lines), refit
    func fitPS(_ P: [(Double, Double)], _ S: [(Double, Double)]) -> (Double, Double, Double)? {
        let n = Double(P.count); if P.count < 2 { return nil }
        let mpx = P.map { $0.0 }.reduce(0, +) / n, mpy = P.map { $0.1 }.reduce(0, +) / n
        let msx = S.map { $0.0 }.reduce(0, +) / n, msy = S.map { $0.1 }.reduce(0, +) / n
        var num = 0.0, den = 0.0
        for (p, q) in zip(P, S) { num += (p.0 - mpx) * (q.0 - msx) + (p.1 - mpy) * (q.1 - msy); den += pow(p.0 - mpx, 2) + pow(p.1 - mpy, 2) }
        if den < 100 { return nil }
        let sc = num / den; return (sc, msx - sc * mpx, msy - sc * mpy)
    }
    if P.count >= 4, let (s0, x0, y0) = fitPS(P, S) {
        let res = zip(P, S).map { (p, q) in hypot(s0 * p.0 + x0 - q.0, s0 * p.1 + y0 - q.1) }
        let med = res.sorted()[res.count / 2]
        let keep = res.indices.filter { res[$0] <= max(6, 2.5 * med) }
        if keep.count >= 2 && keep.count < P.count { P = keep.map { P[$0] }; S = keep.map { S[$0] } }
    }
    let n = Double(P.count); if P.count < 2 { return nil }
    let mpx = P.map { $0.0 }.reduce(0, +) / n, mpy = P.map { $0.1 }.reduce(0, +) / n
    let msx = S.map { $0.0 }.reduce(0, +) / n, msy = S.map { $0.1 }.reduce(0, +) / n
    var num = 0.0, den = 0.0
    for (p, q) in zip(P, S) { num += (p.0 - mpx) * (q.0 - msx) + (p.1 - mpy) * (q.1 - msy); den += pow(p.0 - mpx, 2) + pow(p.1 - mpy, 2) }
    if den < 100 { return nil }
    let sc = num / den; if sc < 0.3 || sc > 6 { return nil }
    let ox = msx - sc * mpx, oy = msy - sc * mpy
    var err = 0.0
    for (p, q) in zip(P, S) { err += pow(sc * p.0 + ox - q.0, 2) + pow(sc * p.1 + oy - q.1, 2) }
    let rms = (err / n).squareRoot()
    if rms > 10 { return nil }
    return (CGRect(x: sc * tg[0] + ox, y: sc * tg[1] + oy, width: sc * (tg[2] - tg[0]), height: sc * (tg[3] - tg[1])), P.count, rms)
}
var gAxDead = false   // set on main when the app has no AX text geometry; the scan loop then skips AX lookups
func stamp() -> String { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd HH:mm:ss"; return f.string(from: Date()) }
func recognise(_ img: CGImage, frame: CGRect, accurate: Bool) -> [OLine] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = accurate ? .accurate : .fast
    req.usesLanguageCorrection = false
    let h = VNImageRequestHandler(cgImage: img, options: [:])
    try? h.perform([req])
    var out: [OLine] = []
    for o in req.results ?? [] {
        guard let c = o.topCandidates(1).first else { continue }
        let b = o.boundingBox   // normalised to the captured image, bottom-left origin -> no Retina scale involved
        let r = CGRect(x: frame.minX + b.minX * frame.width, y: frame.minY + (1 - b.maxY) * frame.height,
                       width: b.width * frame.width, height: b.height * frame.height)
        out.append(OLine(text: c.string, toks: Set(tokens(c.string)), box: r))
    }
    return out
}
// Re-find the target source lines among recognised lines; returns a tight union box.
func refind(_ lines: [OLine], target: [String], near: CGRect?) -> CGRect? {
    let tset = Set(target.flatMap { tokens($0) })
    if tset.isEmpty { return nil }
    // tokens that occur on many recognised lines ("Fast", "No") say little about where the sentence is
    var df: [String: Int] = [:]
    for l in lines { for t in l.toks { df[t, default: 0] += 1 } }
    var score: [Int: Double] = [:]
    for (i, l) in lines.enumerated() where !l.toks.isEmpty {
        let shared = l.toks.intersection(tset)
        if shared.isEmpty || Double(shared.count) / Double(l.toks.count) < 0.5 { continue }
        score[i] = shared.reduce(0.0) { $0 + 1.0 / Double(df[$1] ?? 1) }
    }
    if score.isEmpty { return nil }
    func sameRow(_ r: CGRect, _ b: CGRect) -> Bool { min(r.maxY, b.maxY) - max(r.minY, b.minY) >= 0.5 * min(r.height, b.height) }
    // group recognised lines into visual rows (table cells on one row belong together)
    var rows: [(box: CGRect, score: Double)] = []
    for i in lines.indices.sorted(by: { lines[$0].box.midY < lines[$1].box.midY }) {
        let r = lines[i].box, sc = score[i] ?? 0
        if let k = rows.lastIndex(where: { sameRow(r, $0.box) }) {
            if sc > 0 { rows[k].box = rows[k].score > 0 ? rows[k].box.union(r) : r }
            rows[k].score += sc
        } else { rows.append((box: sc > 0 ? r : r, score: sc)) }
    }
    // a row's box covers only its matching cells
    for k in rows.indices where rows[k].score > 0 {
        var bx: CGRect? = nil
        for (i, sc) in score where sc > 0 && sameRow(lines[i].box, rows[k].box) { bx = bx?.union(lines[i].box) ?? lines[i].box }
        if let b = bx { rows[k].box = b }
    }
    func dist(_ r: CGRect) -> Double { guard let n = near else { return 0 }; return hypot(r.midX - n.midX, r.midY - n.midY) }
    guard let ai = rows.indices.filter({ rows[$0].score >= 0.99 }).max(by: { a, b in
        abs(rows[a].score - rows[b].score) > 0.01 ? rows[a].score < rows[b].score : dist(rows[a].box) > dist(rows[b].box) }) else { return nil }
    // multi-line sentences: add adjacent strong rows, at most as many rows as the target has lines (+1 for wrapping)
    let maxRows = max(1, target.filter { !tokens($0).isEmpty }.reduce(0) { $0 + max(1, Int(ceil(Double($1.count) / 110.0))) })
    var lo = ai, hi = ai, box = rows[ai].box
    while hi - lo + 1 < maxRows {
        let up = lo - 1 >= 0 && rows[lo - 1].score >= 0.99 ? rows[lo - 1].score : -1
        let dn = hi + 1 < rows.count && rows[hi + 1].score >= 0.99 ? rows[hi + 1].score : -1
        if up < 0 && dn < 0 { break }
        if dn >= up { hi += 1; box = box.union(rows[hi].box) } else { lo -= 1; box = box.union(rows[lo].box) }
    }
    return box
}

/// Keys that switch the document under the box (tab / page): Cmd-1..9, Ctrl-Tab, Cmd-Shift-[ / ]. Cmd-Shift-3/4/5/6
/// are macOS screenshot shortcuts, not tab switches: they used to hide the box for the rest of the read.
func isTabSwitchKey(cmd: Bool, ctrl: Bool, shift: Bool, alt: Bool, keyCode kc: Int64) -> Bool {
    let digit: Set<Int64> = [18, 19, 20, 21, 23, 22, 26, 28, 25]
    return (cmd && !alt && !shift && digit.contains(kc)) || (ctrl && kc == 48) || (cmd && shift && (kc == 33 || kc == 30))
}

/// After a tab-switch key, the box comes back once a scan shows the same page (token overlap with the run's first scan).
func sameContent(_ a: Set<String>, _ b: Set<String>) -> Bool {
    let uni = Double(max(1, a.union(b).count))
    return Double(a.intersection(b).count) / uni >= 0.6
}

// ---------------- lifetime ----------------
/// Why the tracker must stop now, or nil. Pure so it is unit-tested. An orphan (parent gone, ppid 1) or a tracker
/// past its max lifetime must never linger: a stray one kept LaunchServices thinking Speakeasy was running.
func lifeVerdict(elapsed: Double, maxLife: Double, ppid: pid_t, parentPid: pid_t, sourcePid: pid_t, alive: (pid_t) -> Bool) -> String? {
    if elapsed > maxLife { return "max-life" }
    if parentPid > 0 && !alive(parentPid) { return "parent-exited" }
    if parentPid > 0 && ppid == 1 { return "orphaned" }
    if sourcePid > 0 && !alive(sourcePid) { return "source-exited" }
    return nil
}
final class LifeFlag: @unchecked Sendable { var v = false }
enum Lifetime {
    static let stopping = LifeFlag()
    nonisolated(unsafe) static var reason: String? = nil
    nonisolated(unsafe) static var keep: [AnyObject] = []
    static let graceS = 2.0
    /// Ask the run loop to stop (it logs its exit), then hard-exit after a grace period whatever it is blocked on.
    static func stop(_ why: String) {
        if stopping.v { return }
        reason = why; stopping.v = true
        DispatchQueue.global().asyncAfter(deadline: .now() + graceS) { _exit(0) }
    }
    static func install(_ a: [String: String]) {
        signal(SIGTERM, SIG_IGN); signal(SIGINT, SIG_IGN); signal(SIGHUP, SIG_IGN)
        for sg in [SIGTERM, SIGINT, SIGHUP] {
            let src = DispatchSource.makeSignalSource(signal: sg, queue: .global())
            src.setEventHandler { stop(sg == SIGTERM ? "sigterm" : sg == SIGINT ? "sigint" : "sighup") }
            src.resume(); keep.append(src)
        }
        let env = ProcessInfo.processInfo.environment
        let maxLife = Double(a["max-life"] ?? env["SPEAKEASY_TRACK_MAX_S"] ?? "") ?? 960     // the read loop itself stops at 900 s
        let parent = pid_t(a["parent-pid"] ?? "") ?? 0
        let source = a["selftest-hang"] != nil || a["window-id"] != nil ? (pid_t(a["pid"] ?? "") ?? 0) : 0
        let t0 = Date()
        let timer = DispatchSource.makeTimerSource(queue: .global())
        timer.schedule(deadline: .now() + 0.5, repeating: 0.5)
        timer.setEventHandler {
            if let why = lifeVerdict(elapsed: Date().timeIntervalSince(t0), maxLife: maxLife, ppid: getppid(), parentPid: parent,
                                     sourcePid: source, alive: { kill($0, 0) == 0 || errno == EPERM }) { stop(why) }
        }
        timer.resume(); keep.append(timer)
    }
}

// ---------------- main ----------------
@main
struct Tracker {
    // CoreGraphics / WindowServer must be initialised before any CGWindowList, ScreenCaptureKit or AX call, otherwise a
    // bare CLI dies with "Assertion failed: (did_initialize), function CGS_REQUIRE_INIT". So: become a (hidden,
    // never-activating) NSApplication, touch the display, run the tracker on a background task and keep the main run loop alive.
    static func main() {
        var a: [String: String] = [:]
        var it = CommandLine.arguments.dropFirst().makeIterator()
        while let k = it.next() { if k.hasPrefix("--") { a[String(k.dropFirst(2))] = it.next() ?? "" } }
        Lifetime.install(a)                           // SIGTERM, parent/source death and a hard max lifetime, independent of run()
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)           // no Dock icon; the overlay panel never activates
        _ = CGMainDisplayID()
        let args = a
        Task.detached {
            if args["selftest-hang"] != nil { while true { try? await Task.sleep(nanoseconds: 1_000_000_000) } }   // tests only
            await Tracker.run(args); exit(0)
        }
        app.run()
    }
    static func run(_ a: [String: String]) async {
        // Offline check: --ocr-image file.png --find "text" [--frame x,y,w,h]
        // --ocr-lines file.png --frame x,y,w,h : recognised lines as JSON, boxes in frame coordinates (used for raster figures)
        // --pdf-lines file.pdf : text lines per page with page-space boxes (top-left origin), via PDFKit (no PyMuPDF needed)
        if let p = a["pdf-lines"] {
            guard let doc = PDFDocument(url: URL(fileURLWithPath: p)) else { print("{\"error\":\"bad pdf\"}"); return }
            var pages: [[String: Any]] = []
            for i in 0..<doc.pageCount {
                guard let pg = doc.page(at: i) else { continue }
                let mb = pg.bounds(for: .mediaBox)
                var lines: [[Any]] = []
                if let sel = pg.selection(for: mb) {
                    for l in sel.selectionsByLine() {
                        let t = (l.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                        if t.isEmpty { continue }
                        let b = l.bounds(for: pg)
                        let x0 = b.minX - mb.minX, y0 = mb.maxY - b.maxY
                        lines.append([(x0 * 10).rounded() / 10, (y0 * 10).rounded() / 10, ((x0 + b.width) * 10).rounded() / 10, ((y0 + b.height) * 10).rounded() / 10, t])
                    }
                }
                pages.append(["n": i + 1, "w": Double(mb.width), "h": Double(mb.height), "lines": lines, "figures": [Any](), "figs_done": true])
            }
            if let d = try? JSONSerialization.data(withJSONObject: ["kind": "pdf", "pages": pages, "engine": "pdfkit"]), let s = String(data: d, encoding: .utf8) { print(s) }
            return
        }
        if let p = a["ocr-lines"] {
            guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: p) as CFURL, nil),
                  let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { print("[]"); return }
            let f = (a["frame"] ?? "").split(separator: ",").compactMap { Double($0) }
            let frame = f.count == 4 ? CGRect(x: f[0], y: f[1], width: f[2], height: f[3]) : CGRect(x: 0, y: 0, width: img.width, height: img.height)
            let lines = recognise(img, frame: frame, accurate: true)
            let arr: [[String: Any]] = lines.map { ["t": $0.text, "r": [Double($0.box.minX), Double($0.box.minY), Double($0.box.maxX), Double($0.box.maxY)]] }
            if let d = try? JSONSerialization.data(withJSONObject: arr), let s = String(data: d, encoding: .utf8) { print(s) } else { print("[]") }
            return
        }
        if let p = a["ocr-image"] {
            guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: p) as CFURL, nil),
                  let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { print("{\"error\":\"bad image\"}"); return }
            let f = (a["frame"] ?? "").split(separator: ",").compactMap { Double($0) }
            let frame = f.count == 4 ? CGRect(x: f[0], y: f[1], width: f[2], height: f[3]) : CGRect(x: 0, y: 0, width: img.width, height: img.height)
            for acc in [false, true] {
                let t0 = now(); let lines = recognise(img, frame: frame, accurate: acc); let ms = (now() - t0) * 1000
                let hit = refind(lines, target: (a["find"] ?? "").components(separatedBy: "\n"), near: nil)
                if let dp = a["doc"], let d = readJSON(dp) {
                    if let (r, k, e) = docMap(lines, d, frame: frame) { print("{\"level\":\"\(acc ? "accurate" : "fast")\",\"doc_rect\":[\(Int(r.minX)),\(Int(r.minY)),\(Int(r.width)),\(Int(r.height))],\"anchors\":\(k),\"rms\":\(String(format: "%.1f", e))}") }
                    else { print("{\"doc_rect\":null}") }
                }
                if a["debug"] != nil { for l in lines { print("  \(Int(l.box.minY)) \(Int(l.box.height)) \(l.text)") } }
                print("{\"level\":\"\(acc ? "accurate" : "fast")\",\"ocr_ms\":\(Int(ms)),\"lines\":\(lines.count),\"hit\":\(hit.map { "[\(Int($0.minX)),\(Int($0.minY)),\(Int($0.width)),\(Int($0.height))]" } ?? "null")}")
            }
            print("{\"rss_mb\":\(Int(rss_mb())),\"cpu_s\":\(String(format: "%.2f", cpu_s()))}")
            return
        }
        let h0 = a["home"] ?? (NSHomeDirectory() + "/.speakeasy")
        try? FileManager.default.createDirectory(atPath: h0 + "/logs", withIntermediateDirectories: true)
        let srAllowed = CGPreflightScreenCaptureAccess()          // checked first; never prompts
        let axAllowed = AXIsProcessTrusted()
        appendLine(["ts": (now() * 1000).rounded() / 1000, "event": "start", "window_id": Int(a["window-id"] ?? "") ?? NSNull(),
                    "pid": Int(a["pid"] ?? "") ?? NSNull(), "fps": a["fps"] ?? "3", "screen_recording": srAllowed, "accessibility": axAllowed,
                    "tracker_pid": Int(getpid()), "run_id": a["run-id"] ?? NSNull(), "build": BUILD,
                    "overlay": (a["overlay"] ?? "native") == "native" ? "swift-panel" : "hammerspoon"], h0 + "/logs/tracker.jsonl")
        // Never prompt: without Screen Recording permission, log and exit before any ScreenCaptureKit/CGWindowList call.
        if !srAllowed {
            appendLine(["ts": (now() * 1000).rounded() / 1000, "event": "exit", "reason": "no_screen_recording", "window_id": Int(a["window-id"] ?? "") ?? NSNull(), "frames": 0], h0 + "/logs/tracker.jsonl")
            exit(0)
        }
        guard let widS = a["window-id"], let wid = CGWindowID(widS) else { FileHandle.standardError.write("usage: speakeasy-track --window-id N --pid P [--home DIR] [--fps 3]\n".data(using: .utf8)!); exit(2) }
        let pid = pid_t(a["pid"] ?? "0") ?? 0
        let home = a["home"] ?? (NSHomeDirectory() + "/.speakeasy")
        let fps = max(0.5, min(10, Double(a["fps"] ?? "5") ?? 5))   // text-recognition scans per second
        let tickS = 0.08                                               // loop tick; window front checks also run on their own 25 ms timer
        var lastScan = 0.0, lastTick = now()
        let accurate = (a["ocr"] ?? "fast") == "accurate"     // fast first; accurate is the retry when fast misses
        let focusPath = home + "/focus.json", trackPath = home + "/track.json"
        try? FileManager.default.createDirectory(atPath: home + "/logs", withIntermediateDirectories: true)
        // element focused at hotkey time; only when already trusted (launched by Hammerspoon), never prompt
        let axEl = pid > 0 && AXIsProcessTrusted() ? focusedElement(pid) : nil
        let native = true            // the native panel is the only focus box (the Hammerspoon canvas was removed)
        if native { await MainActor.run { Live.shared.runId = a["run-id"] ?? ""; Live.shared.setup(home: home, pid: pid, el: axEl); Live.shared.startWinWatch(wid) } }

        // ScreenCaptureKit: find our window once
        var filter: SCContentFilter? = nil
        if let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false),
           let w = content.windows.first(where: { $0.windowID == wid }) {
            filter = SCContentFilter(desktopIndependentWindow: w)
        }
        let t0 = now(), cpu0 = cpu_s()
        var frames = 0, axHits = 0, ocrHits = 0, docHits = 0, misses = 0, hidden = 0
        var ocrMs = 0.0, capMs = 0.0, ocrN = 0
        var lastSeq = -1, lastPid = -1, lastShowTs = now(), lastClear: Double? = nil
        var lastBox: CGRect? = nil, lastOut: [String: Any] = [:]
        var baseline: Set<String>? = nil
        var missRun = 0
        var figCapRef: CGRect? = nil, figCapSeq = -1
        var frameLogN = 0
        var lastAccurate = 0.0
        var speakerSeen: Int32? = nil
        var maxRss = 0.0
        var activeS = 0.0
        var errNote: String? = filter == nil ? "window not shareable (Screen Recording permission?)" : nil

        var exitReason = "max-time"
        final class Flag: @unchecked Sendable { var v = false }
        let term = Flag()
        var sigSrcs: [DispatchSourceSignal] = []
        _ = sigSrcs
        while true {
            if term.v || Lifetime.stopping.v { exitReason = term.v ? "sigterm" : (Lifetime.reason ?? "lifetime"); break }
            let tick = now()
            if tick - t0 > 900 { break }
            let prevTick = lastTick; lastTick = tick
            let ev = readJSON(focusPath) ?? [:]
            let state = ev["state"] as? String ?? ""
            let epid = ev["pid"] as? Int ?? -1
            let seq = ev["seq"] as? Int ?? -1
            let evTs = ev["ts"] as? Double ?? 0
            let fresh = evTs >= t0 - 0.5                                   // written by this run, not left over from the last one
            if lastSeq < 0 {
                // Before the first sentence: vision / the LLM can take many seconds. Wait for the first focus event or for the
                // speakeasy process to exit; a stale "clear" from an earlier run is not the end of this one.
                let sp = Int32((try? String(contentsOfFile: home + "/speak.pid", encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "") ?? 0
                if sp > 0 && kill(sp, 0) == 0 { speakerSeen = sp }
                if let p = speakerSeen, kill(p, 0) != 0 { exitReason = "speaker-exited"; break }
                if speakerSeen == nil && tick - t0 > 10 { exitReason = "speaker-not-started"; break }
                if state == "clear" && fresh && epid > 0 && Int32(epid) == speakerSeen { exitReason = "speech-finished"; break }
                if tick - t0 > 240 { exitReason = "no-focus-events"; break }
            } else {
                if state == "clear" { if lastClear == nil { lastClear = tick }; if tick - lastClear! > 1.5 { exitReason = "speech-finished"; break } } else { lastClear = nil }
                if tick - lastShowTs > 25 { exitReason = "no-focus-events"; break }
            }
            if state == "show" && (fresh || lastSeq >= 0) { lastShowTs = tick }
            if epid > 0 && lastPid > 0 && epid != lastPid { exitReason = "newer-run"; break }                   // a newer speakeasy run took over
            if epid > 0 && kill(pid_t(epid), 0) != 0 && state == "show" && (fresh || lastSeq >= 0) && tick - t0 > 2 { exitReason = "speaker-died"; break }   // speaker died
            let showing = state == "show" && (fresh || lastSeq >= 0)
            if showing && (seq != lastSeq || epid != lastPid) {
                lastSeq = seq; lastPid = epid; missRun = 0
                if native {
                    let rr = (ev["rect"] as? [Double]).flatMap { $0.count == 4 ? CGRect(x: $0[0], y: $0[1], width: $0[2], height: $0[3]) : nil }
                    let rg = ev["range"] as? [Int], col = ev["color"] as? String, op = ev["opacity"] as? Double ?? 0.22
                    let ell = (ev["shape"] as? String) == "ellipse"
                    let sl = ev["src"] as? [String] ?? [], sq = seq
                    let dkind = (ev["doc"] as? [String: Any])?["kind"] as? String
                    let isText = !ell && (dkind == nil || dkind == "line")
                    let nl = ev["nlines"] as? Int
                    let cueFig = (ev["cue"] as? String) == "figure", drawOn = ev["draw"] as? Bool ?? true
                    let ref = (ev["ref"] as? [Double]).flatMap { $0.count == 4 ? CGRect(x: $0[0], y: $0[1], width: $0[2], height: $0[3]) : nil }
                    let hdr = (ev["header"] as? [Double]).flatMap { $0.count == 4 ? CGRect(x: $0[0], y: $0[1], width: $0[2], height: $0[3]) : nil }
                    let ck = ev["cue_kind"] as? String
                    var meta: [String: Any] = [:]
                    if let dm = ev["doc"] as? [String: Any] { meta["label"] = dm["label"] ?? NSNull(); meta["doc_target"] = dm["target"] ?? NSNull() }
                    if let t = ev["doc_transform"] { meta["doc_transform"] = t }
                    if let c = ev["clamped"] { meta["clamped"] = c }
                    await MainActor.run { Live.shared.event(seq: sq, rect: rr, range: rg, srcLines: sl, color: col, opacity: op, ellipse: ell, isText: isText && !cueFig, nlines: nl, ref: drawOn ? ref : nil, figure: cueFig, drawOn: drawOn, cueKind: ck, header: hdr, meta: meta) }
                }
            }
            if native && !showing && lastSeq >= 0 { await MainActor.run { if Live.shared.rect != nil { Live.shared.hideTrigger = ("speech-finished", now()) }; Live.shared.rect = nil; Live.shared.apply() } }
            var out: [String: Any] = ["seq": seq, "focus_pid": epid, "window_id": Int(wid)]
            var scanned = false
            var winFrame: CGRect? = nil
            if showing {
                activeS += tick - prevTick
                let ws = windowState(wid)
                var liveAx = false, deltaOK = false, wantNow = false
                if native, let w = ws {
                    let fr = w.front && w.onScreen
                    let appFront = NSWorkspace.shared.frontmostApplication?.processIdentifier == pid
                    (liveAx, deltaOK, wantNow) = await MainActor.run { () -> (Bool, Bool, Bool) in
                        let L = Live.shared; L.winFrame = w.frame
                        if L.winFront != fr {
                            if !fr && L.shown { L.hideTrigger = (appFront ? (w.onScreen ? "window-switch" : "not-frontmost") : "app-switch", now()) }
                            L.winFront = fr; L.apply()
                        }
                        let wn = L.wantScan && now() - L.lastMove > 0.06; return (L.axGood, L.deltaOK, wn) }
                }
                let seqChanged = (lastOut["seq"] as? Int) != seq
                let wasHiddenForFront = (lastOut["reason"] as? String).map { ["not-frontmost", "not-on-screen", "window-gone"].contains($0) } ?? false
                if ws == nil || !ws!.onScreen || !ws!.front {
                    // hide at once (checked every tick, no debounce)
                    out["visible"] = false; out["reason"] = ws == nil ? "window-gone" : (ws!.onScreen ? "not-frontmost" : "not-on-screen"); hidden += 1
                    if let w = ws, w.onScreen { out["front_owner"] = w.frontOwner; out["front_id"] = w.frontId }
                } else if !(seqChanged || wasHiddenForFront || (wantNow && tick - lastScan > 0.1) || tick - lastScan >= (liveAx ? 2.0 : deltaOK ? 1.0 : 1.0 / fps)) {   // scroll-delta confirmed by OCR: 1 scan/s   // AX follows events; OCR is a slow drift check
                    out = lastOut                                        // between scans: keep the last answer
                } else {
                    scanned = true; frames += 1; lastScan = tick
                    let frame = ws!.frame; winFrame = frame
                    var found: CGRect? = nil, method = ""
                    var continueFig = false
                    if !gAxDead, let el = axEl, let rg = ev["range"] as? [Int], rg.count == 2, let r = axBounds(el, rg[0], rg[1]) {
                        let content = CGRect(x: frame.minX, y: frame.minY + 24, width: frame.width, height: frame.height - 24)
                        // web views often report the whole column/container: only trust tight AX boxes, else use word boxes
                        if content.intersects(r) { if r.width < frame.width * 0.6 || r.width < 320 { found = r.intersection(frame); method = "ax" } }
                        else { out["visible"] = false; out["reason"] = "scrolled-off" }
                    }
                    if found == nil && out["visible"] == nil, let flt = filter, let target = ev["src"] as? [String], !target.isEmpty {
                        let cfg = SCStreamConfiguration()
                        let scale = min(2.0, max(1.0, 1600.0 / max(1, frame.width)))
                        cfg.width = Int(frame.width * scale); cfg.height = Int(frame.height * scale)
                        cfg.showsCursor = false
                        if #available(macOS 14.0, *) { cfg.ignoreShadowsSingleWindow = true }
                        let c0 = now()
                        out["cap_ts"] = c0                                      // the overlay drops scans older than the last scroll / tab switch
                        if let img = try? await SCScreenshotManager.captureImage(contentFilter: flt, configuration: cfg) {
                            capMs += (now() - c0) * 1000
                            let o0 = now()
                            var lines = recognise(img, frame: frame, accurate: accurate)
                            var hit: CGRect? = nil
                            if let dm = ev["doc"] as? [String: Any], let (r, k, e) = docMap(lines, dm, frame: frame) {
                                let vis = r.intersection(frame)
                                // sanity: the mapped box must lie mostly in the window, and a text line must agree with where its words are
                                var ok = !vis.isNull && vis.width * vis.height >= 0.5 * r.width * r.height
                                if ok && (dm["kind"] as? String) == "line", let rf = refind(lines, target: target, near: lastBox),
                                   rf.intersection(r).isNull { ok = false }
                                if ok { hit = vis; method = "doc"; out["anchors"] = k; out["rms"] = (e * 10).rounded() / 10 }
                                else { out["doc_rejected"] = true }
                            } else if ev["doc"] != nil { out["doc_diag"] = gDocDiag }
                            let dk = (ev["doc"] as? [String: Any])?["kind"] as? String
                            let isFig = dk != nil && dk != "line"
                            if hit == nil && isFig {
                                // figure / panel / object box: the caption is only a scroll anchor, never the box itself
                                // (it was moving the figure box onto the caption line)
                                if let c = refind(lines, target: target, near: nil) {
                                    if let ref = figCapRef, figCapSeq == seq {
                                        let dx = c.minX - ref.minX, dy = c.minY - ref.minY
                                        if abs(dx) > 2 || abs(dy) > 2 { let cts = c0; await MainActor.run { Live.shared.shift(dx: dx, dy: dy, capTs: cts) } }
                                    }
                                    figCapRef = c; figCapSeq = seq; out["method"] = "fig-anchor"
                                }
                                out["fig_doc_miss"] = true
                                ocrMs += (now() - o0) * 1000; ocrN += 1
                                lastOut = out; continueFig = true
                            }
                            if continueFig {} else {
                            if hit == nil { hit = refind(lines, target: target, near: lastBox) }
                            if let h = hit, h.width < 24 || h.height < 7 { hit = nil; out["tiny_hit"] = true }   // a stray word, not the sentence
                            if hit == nil && !accurate && tick - lastAccurate > 1.5 {   // accurate retry, at most every 1.5 s
                                lastAccurate = tick
                                lines = recognise(img, frame: frame, accurate: true)
                                if let dm = ev["doc"] as? [String: Any], let (r, k, e) = docMap(lines, dm, frame: frame) {
                                    let vis = r.intersection(frame)
                                    if !vis.isNull && vis.width * vis.height >= 0.5 * r.width * r.height {
                                        hit = vis; method = "doc"; out["anchors"] = k; out["rms"] = (e * 10).rounded() / 10 }
                                }
                                if hit == nil { hit = refind(lines, target: target, near: lastBox) }
                            }
                            ocrMs += (now() - o0) * 1000; ocrN += 1
                            let all = Set(lines.flatMap { $0.toks })
                            if baseline == nil { baseline = all }
                            if native, let bl = baseline, sameContent(all, bl) {
                                await MainActor.run {
                                    let L = Live.shared
                                    if L.hiddenByKey && now() - L.keyHideAt > 0.4 {
                                        L.hiddenByKey = false; L.hideTrigger = nil; L.rec(["event": "unhide", "reason": "same-page"]); L.apply()
                                    }
                                }
                            }
                            if let r = hit { found = r; if method.isEmpty { method = "ocr" } }
                            else {
                                let inter = Double(all.intersection(baseline!).count), uni = Double(max(1, all.union(baseline!).count))
                                out["reason"] = inter / uni < 0.35 ? "content-changed" : "scrolled-off"
                            }
                            }
                        } else { errNote = "capture failed (Screen Recording permission?)" }
                    }
                    if native {
                        let fnd = found, vis = found != nil, capTs = out["cap_ts"] as? Double ?? 0, m = method, wf = frame
                        let hide = fnd == nil && (out["reason"] as? String) == "content-changed" || (fnd == nil && missRun >= 1 && out["reason"] != nil)
                        let rsn = out["reason"] as? String
                        _ = wf
                        if vis || hide { await MainActor.run { Live.shared.scan(rect: fnd, visible: vis, capTs: capTs, method: m, reason: rsn) } }
                    }
                    if let r = found {
                        missRun = 0; lastBox = r
                        if method == "ax" { axHits += 1 } else if method == "doc" { docHits += 1 } else { ocrHits += 1 }
                        out["visible"] = true; out["method"] = method
                        out["rect"] = [Double(r.minX).rounded(), Double(r.minY).rounded(), Double(r.width).rounded(), Double(r.height).rounded()]
                    } else if out["reason"] != nil || out["visible"] != nil {
                        missRun += 1; misses += 1
                        // tab switch / new page: hide on the first differing frame; scrolled-off: allow one-frame misses
                        if missRun >= 2 || (out["reason"] as? String) == "content-changed" { out["visible"] = false } else { out = lastOut }
                    }
                }
            } else {
                out["visible"] = false; out["reason"] = "idle"
            }
            // write only on change (>2pt move or visibility flip)
            func same(_ x: [String: Any], _ y: [String: Any]) -> Bool {
                if (x["visible"] as? Bool) != (y["visible"] as? Bool) || (x["seq"] as? Int) != (y["seq"] as? Int) { return false }
                guard let a = x["rect"] as? [Double], let b = y["rect"] as? [Double] else { return (x["rect"] == nil) == (y["rect"] == nil) }
                return zip(a, b).allSatisfy { abs($0 - $1) <= 2 }
            }
            let changed = !out.isEmpty && !same(out, lastOut)
            if changed {
                var w = out; w["ts"] = (now() * 1000).rounded() / 1000
                writeJSONAtomic(w, trackPath); lastOut = out
            }
            // per-frame log (every scan and every visibility/position change), capped per run
            if (scanned || changed) && frameLogN < 3000 && state == "show" {
                frameLogN += 1
                var fr: [String: Any] = ["ts": (now() * 1000).rounded() / 1000, "event": "frame", "tracker_pid": Int(getpid()), "seq": seq,
                                         "scan": scanned, "visible": out["visible"] ?? NSNull(), "ms": Int((now() - tick) * 1000)]
                if let m = out["method"] { fr["method"] = m }
                if let r = out["rect"] { fr["rect"] = r }
                if let r = out["reason"] { fr["reason"] = r }
                for k in ["doc_diag", "doc_rejected", "anchors", "rms"] { if let v = out[k] { fr[k] = v } }
                if let r = out["front_owner"] { fr["front_owner"] = r; fr["front_id"] = out["front_id"] ?? 0 }
                if scanned, let w = winFrame { fr["win"] = [Int(w.minX), Int(w.minY), Int(w.width), Int(w.height)] }
                appendLine(fr, home + "/logs/tracker.jsonl")
            }
            maxRss = max(maxRss, rss_mb())
            let dt = now() - tick
            _ = scanned
            let wait = max(0.01, tickS - dt)
            try? await Task.sleep(nanoseconds: UInt64(wait * 1e9))
        }
        if native { await MainActor.run { if Live.shared.rect != nil { Live.shared.hideTrigger = ("speech-finished", now()) }; Live.shared.rect = nil; Live.shared.flush() } }
        let lagSt: [String: Any] = native ? await MainActor.run { Live.shared.stats() } : [:]
        writeJSONAtomic(["visible": false, "reason": "tracker-exit", "seq": lastSeq, "focus_pid": lastPid, "ts": now()], trackPath)
        let wall = max(0.001, now() - t0), active = frames
        let hits = axHits + ocrHits + docHits
        var st: [String: Any] = ["ts": (now() * 1000).rounded() / 1000, "event": "exit", "reason": exitReason, "focus_pid": lastPid, "window_id": Int(wid), "wall_s": (wall * 10).rounded() / 10,
                                 "frames": active, "fps": (Double(active) / max(0.001, activeS) * 100).rounded() / 100, "active_s": (activeS * 10).rounded() / 10,
                                 "run_id": a["run-id"] ?? NSNull(), "build": BUILD, "tracker_pid": Int(getpid()),
                                 "ax_hits": axHits, "ocr_hits": ocrHits, "doc_hits": docHits, "misses": misses, "hidden_frames": hidden,
                                 "refind_hit_rate": hits + misses > 0 ? (Double(hits) / Double(hits + misses) * 100).rounded() / 100 : NSNull(),
                                 "ocr_ms_avg": ocrN > 0 ? (ocrMs / Double(ocrN)).rounded() : NSNull(),
                                 "capture_ms_avg": ocrN > 0 ? (capMs / Double(ocrN)).rounded() : NSNull(),
                                 "cpu_s": ((cpu_s() - cpu0) * 100).rounded() / 100, "cpu_pct": ((cpu_s() - cpu0) / wall * 1000).rounded() / 10,
                                 "rss_mb_peak": maxRss.rounded(), "ocr_level": accurate ? "accurate" : "fast"]
        if let e = errNote { st["error"] = e }
        for (k, v) in lagSt { st[k] = v }
        st["overlay"] = native ? "swift-panel" : "hammerspoon"
        appendLine(st, home + "/logs/tracker.jsonl")
    }
}
