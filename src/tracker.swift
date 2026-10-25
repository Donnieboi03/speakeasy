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
func appendLine(_ obj: [String: Any], _ path: String) {
    guard var d = try? JSONSerialization.data(withJSONObject: obj) else { return }
    d.append(0x0a)
    if let h = FileHandle(forWritingAtPath: path) { h.seekToEndOfFile(); h.write(d); h.closeFile() }
    else { FileManager.default.createFile(atPath: path, contents: d) }
}
let STOP: Set<String> = ["the", "and", "for", "are", "was", "with", "this", "that", "from", "has", "its", "of", "to", "in", "on", "at", "is", "a", "an", "or", "by"]
func tokens(_ s: String) -> [String] {
    let low = s.lowercased()
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
struct WinState { var frame: CGRect; var onScreen: Bool; var front: Bool }
func windowState(_ wid: CGWindowID) -> WinState? {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return nil }
    var frontWid: CGWindowID? = nil
    var mine: CGRect? = nil
    for w in list {
        let layer = w[kCGWindowLayer as String] as? Int ?? 0
        let id = CGWindowID(w[kCGWindowNumber as String] as? Int ?? 0)
        let alpha = w[kCGWindowAlpha as String] as? Double ?? 1
        var r = CGRect.zero
        if let b = w[kCGWindowBounds as String] as? NSDictionary, let rr = CGRect(dictionaryRepresentation: b as CFDictionary) { r = rr }
        // the frontmost real window: skip invisible and tiny strips (toolbars, status items)
        if layer == 0 && alpha > 0.01 && r.width >= 80 && r.height >= 80 && frontWid == nil { frontWid = id }
        if id == wid { mine = r }
    }
    if let f = mine { return WinState(frame: f, onScreen: true, front: frontWid == wid) }
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
// The box is drawn here (a borderless, click-through, non-activating panel) so scroll / AX / app-switch events move or hide
// it in the same process, without a file relay. Scroll: AX re-query of the sentence range when the app answers it, else the
// scroll delta times a calibrated scale; text recognition scans correct drift. Lag (event -> frame applied) is logged.
func uptimeNs() -> UInt64 { clock_gettime_nsec_np(CLOCK_UPTIME_RAW) }
final class Live: NSObject {
    static let shared = Live()
    var home = ""
    var panel: NSPanel? = nil
    var shape = CAShapeLayer()
    var rect: CGRect? = nil              // target box, global top-left coordinates
    var isEllipse = false
    var front = true, hiddenByKey = false, scanVisible = false
    var winFrame: CGRect? = nil
    var srcPid: pid_t = 0
    var axEl: AXUIElement? = nil
    var range: [Int]? = nil
    var axGood = false                   // the app answers tight AXBoundsForRange for this sentence
    var lastMove = 0.0                   // epoch of the last scroll / tab key: older scans are stale
    var kCont = 1.0, kLine = 1.0, pred = 0.0, predCont = true
    var pendingEv: [UInt64] = []         // event timestamps waiting for the next display refresh
    var lagN = 0, lagSum = 0.0, lagMax = 0.0, lagLast = 0.0
    var link: AnyObject? = nil
    var dirty = false
    var lagLogN = 0
    var off = CGPoint.zero               // scrolling since the hotkey: speakeasy's own boxes predate it
    var observer: AXObserver? = nil

    func setup(home: String, pid: pid_t, el: AXUIElement?) {
        self.home = home; srcPid = pid; axEl = el
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 10, height: 10), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = false; p.ignoresMouseEvents = true
        p.level = .statusBar; p.hidesOnDeactivate = false; p.isReleasedWhenClosed = false
        p.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        let v = NSView(frame: p.contentView!.bounds); v.wantsLayer = true; v.autoresizingMask = [.width, .height]
        shape.lineWidth = 1.5
        v.layer!.addSublayer(shape)
        p.contentView = v
        panel = p
        // instant hide / show on app switches
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main) { [weak self] n in
            guard let s = self, let app = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else { return }
            s.front = app.processIdentifier == s.srcPid; s.apply(event: nil)
        }
        startTap()
        startAXObserver()
        if #available(macOS 14.0, *) {
            let l = p.contentView!.displayLink(target: self, selector: #selector(tickLink(_:)))
            l.add(to: .main, forMode: .common); link = l
        }
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
    // ---- drawing ----
    func frameFor(_ r: CGRect) -> NSRect {
        let H = NSScreen.screens.first?.frame.height ?? 0
        let (dx, dy): (CGFloat, CGFloat) = isEllipse ? (r.width * 0.12 + 6, r.height * 0.12 + 6) : (4, 2)
        let g = r.insetBy(dx: -dx, dy: -dy)
        return NSRect(x: g.minX, y: H - g.maxY, width: g.width, height: g.height)
    }
    func apply(event ts: UInt64?) {
        if let t = ts { pendingEv.append(t) }
        dirty = true
        if link == nil { flush() }               // no display link (older macOS): apply now
    }
    @objc func tickLink(_ l: AnyObject) { if dirty { flush() } }
    func flush() {
        dirty = false
        guard let p = panel else { return }
        let show = rect != nil && front && !hiddenByKey && scanVisible
        if show, let r = rect {
            let f = frameFor(r)
            CATransaction.begin(); CATransaction.setDisableActions(true)
            p.setFrame(f, display: false)
            let b = CGRect(x: 1, y: 1, width: f.width - 2, height: f.height - 2)
            shape.frame = p.contentView!.bounds
            shape.path = isEllipse ? CGPath(ellipseIn: b, transform: nil) : CGPath(roundedRect: b, cornerWidth: 6, cornerHeight: 6, transform: nil)
            CATransaction.commit()
            if !p.isVisible { p.orderFrontRegardless() }
        } else if p.isVisible { p.orderOut(nil) }
        let n = uptimeNs()
        for t in pendingEv where t > 0 && n > t {
            let ms = Double(n - t) / 1e6
            if ms < 5000 { lagN += 1; lagSum += ms; lagMax = max(lagMax, ms); lagLast = ms }
        }
        if !pendingEv.isEmpty && lagLogN < 3000 && lagLast > 0 {
            lagLogN += 1
            appendLine(["ts": (now() * 1000).rounded() / 1000, "event": "lag", "ms": (lagLast * 10).rounded() / 10, "n": pendingEv.count,
                        "via": axGood ? "ax" : "delta", "tracker_pid": Int(getpid())], home + "/logs/tracker.jsonl")
        }
        pendingEv.removeAll()
    }
    // ---- scans from the tracker loop ----
    func scan(rect r: CGRect?, visible: Bool, capTs: Double, method: String, win: CGRect?) {
        if let w = win { winFrame = w }
        if capTs > 0 && capTs < lastMove { return }               // screenshot taken before the last scroll: stale
        if visible, let r = r {
            if let old = rect, abs(pred) >= 20, method != "ax" {    // calibrate the scroll scale from where the text really went
                let actual = pred + (r.minY - old.minY)
                let k = (predCont ? kCont : kLine) * actual / pred
                if k > 0.2 && k < 5 { if predCont { kCont = 0.5 * kCont + 0.5 * k } else { kLine = 0.5 * kLine + 0.5 * k } }
            }
            pred = 0; rect = r; scanVisible = true; hiddenByKey = false
        } else { scanVisible = false }
        apply(event: nil)
    }
    func event(rect r: CGRect?, range rg: [Int]?, color: String?, opacity: Double, ellipse: Bool) {
        style(color: color, opacity: opacity, ellipse: ellipse)
        range = rg; axGood = false
        if let r = r { rect = r.offsetBy(dx: off.x, dy: off.y); scanVisible = true }
        _ = requeryAX()
        apply(event: nil)
    }
    func requeryAX() -> Bool {
        guard let el = axEl, let rg = range, rg.count == 2, let r = axBounds(el, rg[0], rg[1]) else { axGood = false; return false }
        if let w = winFrame, !(r.width < w.width * 0.6 || r.width < 320) { axGood = false; return false }
        if let w = winFrame, !w.intersects(r) { scanVisible = false; axGood = true; return true }
        rect = r; axGood = true; scanVisible = true; return true
    }
    // ---- events ----
    func startTap() {
        let mask = (1 << CGEventType.scrollWheel.rawValue) | (1 << CGEventType.keyDown.rawValue)
        let cb: CGEventTapCallBack = { _, type, ev, _ in
            Live.shared.handle(type, ev)
            return Unmanaged.passUnretained(ev)
        }
        guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .tailAppendEventTap, options: .listenOnly,
                                          eventsOfInterest: CGEventMask(mask), callback: cb, userInfo: nil) else {
            appendLine(["ts": now(), "event": "tap-failed"], home + "/logs/tracker.jsonl"); return
        }
        let src = CFMachPortCreateRunLoopSource(nil, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
    }
    func handle(_ type: CGEventType, _ ev: CGEvent) {
        guard front, rect != nil else { return }
        if type == .keyDown {
            let f = ev.flags, kc = ev.getIntegerValueField(.keyboardEventKeycode)
            let digit: Set<Int64> = [18, 19, 20, 21, 23, 22, 26, 28, 25]
            let cmd = f.contains(.maskCommand), ctrl = f.contains(.maskControl), sh = f.contains(.maskShift), alt = f.contains(.maskAlternate)
            let tabSwitch = (cmd && !alt && digit.contains(kc)) || (ctrl && kc == 48) || (cmd && sh && (kc == 33 || kc == 30))
                || (cmd && alt && (kc == 123 || kc == 124)) || (cmd && (kc == 17 || kc == 13)) || [116, 121, 115, 119].contains(kc)
            if tabSwitch { hiddenByKey = true; lastMove = now(); apply(event: ev.timestamp) }
            return
        }
        // scroll: only when the pointer is over the source window
        guard let w = winFrame else { return }
        let m = ev.location
        guard w.contains(m) else { return }
        let dy = Double(ev.getIntegerValueField(.scrollWheelEventPointDeltaAxis1))
        let dx = Double(ev.getIntegerValueField(.scrollWheelEventPointDeltaAxis2))
        if dx == 0 && dy == 0 { return }
        lastMove = now()
        if range != nil && axGood && requeryAX() { apply(event: ev.timestamp); return }   // exact: the app moved the text, ask where
        let cont = ev.getIntegerValueField(.scrollWheelEventIsContinuous) != 0
        let k = cont ? kCont : kLine
        if let r = rect { rect = r.offsetBy(dx: dx * k, dy: dy * k) }
        off.x += dx * k; off.y += dy * k
        pred += dy * k; predCont = cont
        apply(event: ev.timestamp)
    }
    func startAXObserver() {
        guard srcPid > 0, AXIsProcessTrusted() else { return }
        var obs: AXObserver?
        let cb: AXObserverCallback = { _, _, _, _ in
            let s = Live.shared
            if s.range != nil && s.requeryAX() { s.apply(event: uptimeNs()) }
        }
        guard AXObserverCreate(srcPid, cb, &obs) == .success, let o = obs else { return }
        let app = AXUIElementCreateApplication(srcPid)
        for n in [kAXValueChangedNotification, kAXSelectedTextChangedNotification, kAXWindowMovedNotification, kAXWindowResizedNotification,
                  kAXFocusedUIElementChangedNotification, kAXLayoutChangedNotification] {
            AXObserverAddNotification(o, app, n as CFString, nil)
            if let el = axEl { AXObserverAddNotification(o, el, n as CFString, nil) }
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(o), .commonModes)
        observer = o
    }
    func lagStats() -> [String: Any] {
        ["scroll_events": lagN, "lag_ms_avg": lagN > 0 ? (lagSum / Double(lagN) * 10).rounded() / 10 : NSNull(),
         "lag_ms_max": (lagMax * 10).rounded() / 10, "k_trackpad": (kCont * 100).rounded() / 100, "k_wheel": (kLine * 100).rounded() / 100]
    }
}

// ---------------- OCR ----------------
struct OLine { let text: String; let toks: Set<String>; let box: CGRect }   // box in screen points (top-left origin)

// Document-aware focus: speakeasy sends a target in PDF page space plus text anchors (page lines with their page rects).
// Match anchors to recognised lines, fit page->screen (uniform scale + offset), map the target (exact line / figure / panel).
func docMap(_ lines: [OLine], _ doc: [String: Any], frame: CGRect? = nil) -> (CGRect, Int, Double)? {
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
        if cands.count != 1 { continue }
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
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)           // no Dock icon; the overlay panel never activates
        _ = CGMainDisplayID()
        let args = a
        Task.detached { await Tracker.run(args); exit(0) }
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
                    "tracker_pid": Int(getpid())], h0 + "/logs/tracker.jsonl")
        // Never prompt: without Screen Recording permission, log and exit before any ScreenCaptureKit/CGWindowList call.
        if !srAllowed {
            appendLine(["ts": (now() * 1000).rounded() / 1000, "event": "exit", "reason": "no_screen_recording", "window_id": Int(a["window-id"] ?? "") ?? NSNull(), "frames": 0], h0 + "/logs/tracker.jsonl")
            exit(0)
        }
        guard let widS = a["window-id"], let wid = CGWindowID(widS) else { FileHandle.standardError.write("usage: speakeasy-track --window-id N --pid P [--home DIR] [--fps 3]\n".data(using: .utf8)!); exit(2) }
        let pid = pid_t(a["pid"] ?? "0") ?? 0
        let home = a["home"] ?? (NSHomeDirectory() + "/.speakeasy")
        let fps = max(0.5, min(10, Double(a["fps"] ?? "5") ?? 5))   // text-recognition scans per second
        let tickS = 0.08                                               // frontmost / window checks: every 80 ms
        var lastScan = 0.0, lastTick = now()
        let accurate = (a["ocr"] ?? "fast") == "accurate"     // fast first; accurate is the retry when fast misses
        let focusPath = home + "/focus.json", trackPath = home + "/track.json"
        try? FileManager.default.createDirectory(atPath: home + "/logs", withIntermediateDirectories: true)
        // element focused at hotkey time; only when already trusted (launched by Hammerspoon), never prompt
        let axEl = pid > 0 && AXIsProcessTrusted() ? focusedElement(pid) : nil
        let native = (a["overlay"] ?? "native") == "native"
        if native { await MainActor.run { Live.shared.setup(home: home, pid: pid, el: axEl) } }

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
        var frameLogN = 0
        var lastAccurate = 0.0
        var speakerSeen: Int32? = nil
        var maxRss = 0.0
        var activeS = 0.0
        var errNote: String? = filter == nil ? "window not shareable (Screen Recording permission?)" : nil

        var exitReason = "max-time"
        while true {
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
                    await MainActor.run { Live.shared.event(rect: rr, range: rg, color: col, opacity: op, ellipse: ell) }
                }
            }
            if native && !showing && lastSeq >= 0 { await MainActor.run { Live.shared.rect = nil; Live.shared.apply(event: nil) } }
            var out: [String: Any] = ["seq": seq, "focus_pid": epid, "window_id": Int(wid)]
            var scanned = false
            var winFrame: CGRect? = nil
            if showing {
                activeS += tick - prevTick
                let ws = windowState(wid)
                var liveAx = false
                if native, let w = ws {
                    let fr = w.front && w.onScreen
                    liveAx = await MainActor.run { () -> Bool in
                        let L = Live.shared; L.winFrame = w.frame
                        if L.front != fr { L.front = fr; L.apply(event: nil) }
                        return L.axGood }
                }
                let seqChanged = (lastOut["seq"] as? Int) != seq
                let wasHiddenForFront = (lastOut["reason"] as? String).map { ["not-frontmost", "not-on-screen", "window-gone"].contains($0) } ?? false
                if ws == nil || !ws!.onScreen || !ws!.front {
                    // hide at once (checked every tick, no debounce)
                    out["visible"] = false; out["reason"] = ws == nil ? "window-gone" : (ws!.onScreen ? "not-frontmost" : "not-on-screen"); hidden += 1
                } else if !(seqChanged || wasHiddenForFront || tick - lastScan >= (liveAx ? 2.0 : 1.0 / fps)) {   // AX follows events; OCR is a slow drift check
                    out = lastOut                                        // between scans: keep the last answer
                } else {
                    scanned = true; frames += 1; lastScan = tick
                    let frame = ws!.frame; winFrame = frame
                    var found: CGRect? = nil, method = ""
                    if let el = axEl, let rg = ev["range"] as? [Int], rg.count == 2, let r = axBounds(el, rg[0], rg[1]) {
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
                            }
                            if hit == nil { hit = refind(lines, target: target, near: lastBox) }
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
                            if let r = hit { found = r; if method.isEmpty { method = "ocr" } }
                            else {
                                let inter = Double(all.intersection(baseline!).count), uni = Double(max(1, all.union(baseline!).count))
                                out["reason"] = inter / uni < 0.35 ? "content-changed" : "scrolled-off"
                            }
                        } else { errNote = "capture failed (Screen Recording permission?)" }
                    }
                    if native {
                        let fnd = found, vis = found != nil, capTs = out["cap_ts"] as? Double ?? 0, m = method, wf = frame
                        let hide = fnd == nil && (out["reason"] as? String) == "content-changed" || (fnd == nil && missRun >= 1 && out["reason"] != nil)
                        if vis || hide { await MainActor.run { Live.shared.scan(rect: fnd, visible: vis, capTs: capTs, method: m, win: wf) } }
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
                if scanned, let w = winFrame { fr["win"] = [Int(w.minX), Int(w.minY), Int(w.width), Int(w.height)] }
                appendLine(fr, home + "/logs/tracker.jsonl")
            }
            maxRss = max(maxRss, rss_mb())
            let dt = now() - tick
            _ = scanned
            let wait = max(0.01, tickS - dt)
            try? await Task.sleep(nanoseconds: UInt64(wait * 1e9))
        }
        if native { await MainActor.run { Live.shared.rect = nil; Live.shared.flush() } }
        let lagSt: [String: Any] = native ? await MainActor.run { Live.shared.lagStats() } : [:]
        writeJSONAtomic(["visible": false, "reason": "tracker-exit", "seq": lastSeq, "focus_pid": lastPid, "ts": now()], trackPath)
        let wall = max(0.001, now() - t0), active = frames
        let hits = axHits + ocrHits + docHits
        var st: [String: Any] = ["ts": (now() * 1000).rounded() / 1000, "event": "exit", "reason": exitReason, "focus_pid": lastPid, "window_id": Int(wid), "wall_s": (wall * 10).rounded() / 10,
                                 "frames": active, "fps": (Double(active) / max(0.001, activeS) * 100).rounded() / 100, "active_s": (activeS * 10).rounded() / 10,
                                 "ax_hits": axHits, "ocr_hits": ocrHits, "doc_hits": docHits, "misses": misses, "hidden_frames": hidden,
                                 "refind_hit_rate": hits + misses > 0 ? (Double(hits) / Double(hits + misses) * 100).rounded() / 100 : NSNull(),
                                 "ocr_ms_avg": ocrN > 0 ? (ocrMs / Double(ocrN)).rounded() : NSNull(),
                                 "capture_ms_avg": ocrN > 0 ? (capMs / Double(ocrN)).rounded() : NSNull(),
                                 "cpu_s": ((cpu_s() - cpu0) * 100).rounded() / 100, "cpu_pct": ((cpu_s() - cpu0) / wall * 1000).rounded() / 10,
                                 "rss_mb_peak": maxRss.rounded(), "ocr_level": accurate ? "accurate" : "fast"]
        if let e = errNote { st["error"] = e }
        for (k, v) in lagSt { st[k] = v }
        st["overlay"] = native ? "native" : "hammerspoon"
        appendLine(st, home + "/logs/tracker.jsonl")
    }
}
