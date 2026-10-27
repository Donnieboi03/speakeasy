// speakeasy-track: keeps the focus box glued to the source text while speakeasy reads.
// Locks onto one window (by CGWindowID) at hotkey time, then a few times a second:
//   1. AX: if the event carries an absolute character range and the app answers AXBoundsForRange, use that (cheap).
//   2. Otherwise capture just that window (ScreenCaptureKit) and run Apple Vision text recognition, then re-find the
//      current sentence's source lines among the recognised lines (token overlap) and box them tightly.
// Hides the box when the window is not frontmost, the content changed (tab switch), or the text scrolled off.
// Reads  ~/.speakeasy/focus.json (written by speakeasy as each sentence starts playing)
// Draws the box itself (native click-through panel): scroll / key / app-switch / AX events move or hide it in-process;
// OCR scans correct drift. Appends stats and per-event lag to ~/.speakeasy/logs/tracker.jsonl
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
// ---------------- cue tokens (docs/design.md 1 and 6.6): the only colours the tracker draws with ----------------
struct RGB { let r: Int, g: Int, b: Int
    func cg(_ a: Double) -> CGColor { CGColor(red: CGFloat(r) / 255, green: CGFloat(g) / 255, blue: CGFloat(b) / 255, alpha: CGFloat(a)) } }
enum CueTok {
    static let ice = RGB(r: 168, g: 216, b: 236), glacier = RGB(r: 31, g: 110, b: 153), ember = RGB(r: 224, g: 137, b: 43)
    static let bandAlpha = 0.38, bandDarkAlpha = 0.45, boxFillAlpha = 0.10, headerAlpha = 0.18, refAlpha = 0.60
    static let tickW: CGFloat = 3, tickGap: CGFloat = 4            // the margin tick: 3 pt wide, 4 pt left of the first line
    static let darkPageLuminance = 0.25                             // below it the band uses glacier and the box stroke ice
}
/// How one cue kind is drawn: fill / stroke colours, stroke width, corner radius, dash, and the margin tick (text bands).
struct CueLook { let fill: CGColor?; let stroke: CGColor?; let lineWidth: CGFloat; let radius: CGFloat; let dash: [NSNumber]?; let tick: CGColor? }
func cueLook(_ kind: String, darkPage: Bool) -> CueLook {
    switch kind {
    case "text_band": return CueLook(fill: darkPage ? CueTok.glacier.cg(CueTok.bandDarkAlpha) : CueTok.ice.cg(CueTok.bandAlpha), stroke: nil, lineWidth: 0,
                                     radius: 2, dash: nil, tick: darkPage ? CueTok.ice.cg(1) : CueTok.glacier.cg(1))
    case "table_row": return CueLook(fill: darkPage ? CueTok.glacier.cg(CueTok.bandDarkAlpha) : CueTok.ice.cg(CueTok.bandAlpha), stroke: nil, lineWidth: 0,
                                     radius: 2, dash: nil, tick: nil)
    case "table_header": return CueLook(fill: CueTok.ice.cg(CueTok.headerAlpha), stroke: nil, lineWidth: 0, radius: 2, dash: nil, tick: nil)
    case "part_oval": return CueLook(fill: nil, stroke: CueTok.ember.cg(1), lineWidth: 2, radius: 0, dash: nil, tick: nil)
    case "back_ref": return CueLook(fill: nil, stroke: CueTok.glacier.cg(CueTok.refAlpha), lineWidth: 1, radius: 2, dash: [4, 3], tick: nil)
    default: return CueLook(fill: CueTok.ice.cg(CueTok.boxFillAlpha), stroke: darkPage ? CueTok.ice.cg(1) : CueTok.glacier.cg(1), lineWidth: 2,
                            radius: 3, dash: nil, tick: nil)          // figure_box (figure, panel, table)
    }
}
/// Mean relative luminance (0 dark .. 1 light) of the window capture under a screen rect (top-left points).
func meanLuminance(_ img: CGImage, frame: CGRect, under r: CGRect) -> Double? {
    let sx = Double(img.width) / Double(max(1, frame.width)), sy = Double(img.height) / Double(max(1, frame.height))
    let px = CGRect(x: (r.minX - frame.minX) * sx, y: (r.minY - frame.minY) * sy, width: r.width * sx, height: r.height * sy)
        .intersection(CGRect(x: 0, y: 0, width: img.width, height: img.height))
    guard !px.isNull, px.width >= 1, px.height >= 1, let crop = img.cropping(to: px) else { return nil }
    var buf = [UInt8](repeating: 0, count: 8 * 8 * 4)
    guard let ctx = CGContext(data: &buf, width: 8, height: 8, bitsPerComponent: 8, bytesPerRow: 32, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    ctx.interpolationQuality = .medium; ctx.draw(crop, in: CGRect(x: 0, y: 0, width: 8, height: 8))
    var sum = 0.0
    for i in stride(from: 0, to: buf.count, by: 4) { sum += (0.2126 * Double(buf[i]) + 0.7152 * Double(buf[i + 1]) + 0.0722 * Double(buf[i + 2])) / 255 }
    return sum / 64
}
/// Offscreen render of one cue over a plain page (white or dark), with the same looks the panel uses: `--render-cue`.
func renderCue(_ kind: String, darkPage: Bool, scale: CGFloat = 2) -> CGImage? {
    let W: CGFloat = 360, H: CGFloat = 160, s = scale
    guard let ctx = CGContext(data: nil, width: Int(W * s), height: Int(H * s), bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    ctx.scaleBy(x: s, y: s)
    ctx.setFillColor(darkPage ? CGColor(red: 0.12, green: 0.12, blue: 0.13, alpha: 1) : CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
    let look = cueLook(kind, darkPage: darkPage), body = CGRect(x: 60, y: 50, width: 240, height: 60)
    let path = kind == "part_oval" ? CGPath(ellipseIn: body, transform: nil)
        : CGPath(roundedRect: body, cornerWidth: look.radius, cornerHeight: look.radius, transform: nil)
    if let f = look.fill { ctx.setFillColor(f); ctx.addPath(path); ctx.fillPath() }
    if let st = look.stroke {
        ctx.setStrokeColor(st); ctx.setLineWidth(look.lineWidth)
        if let d = look.dash { ctx.setLineDash(phase: 0, lengths: d.map { CGFloat($0.doubleValue) }) }
        ctx.addPath(path); ctx.strokePath()
    }
    if let t = look.tick { ctx.setFillColor(t); ctx.fill(CGRect(x: body.minX - CueTok.tickW, y: body.minY, width: CueTok.tickW, height: body.height)) }
    return ctx.makeImage()
}
// ---------------- shared text fold (docs/contracts/phase3-sentence-ids.md A: the same steps as src/textfold.py) ----------------
let foldUnitWords = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
                     "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
let foldTensWords = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
let foldDigitWords = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
let foldUnitsLongestFirst = foldUnitWords.sorted { $0.count > $1.count }   // "seventeen" before "seven"
let foldInvisible: Set<UInt32> = [0xAD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF]
let foldHyphens: Set<UInt32> = [0x2D, 0x2010, 0x2011]
func isKeyScalar(_ u: Unicode.Scalar) -> Bool {
    switch u.properties.generalCategory {
    case .uppercaseLetter, .lowercaseLetter, .titlecaseLetter, .modifierLetter, .otherLetter, .decimalNumber: return true
    default: return false
    }
}
func isLetterScalar(_ u: Unicode.Scalar) -> Bool { isKeyScalar(u) && u.properties.generalCategory != .decimalNumber }
func isMarkScalar(_ u: Unicode.Scalar) -> Bool {
    switch u.properties.generalCategory { case .nonspacingMark, .spacingMark, .enclosingMark: return true; default: return false }
}
/// Steps 1-5 of the fold: the folded scalars and, for each, the index (unicode scalars) of the original character it came from.
func foldedScalars(_ text: String) -> ([Unicode.Scalar], [Int]) {
    let src = Array(text.unicodeScalars)
    var s: [Unicode.Scalar] = [], o: [Int] = []
    var i = 0
    while i < src.count {                                   // 1. NFKC per cluster (a base plus its marks); 2. invisible characters
        var j = i + 1
        while j < src.count && isMarkScalar(src[j]) { j += 1 }
        if j == i + 1 && src[i].value < 0x80 { s.append(src[i]); o.append(i) }
        else {
            var v = String.UnicodeScalarView(); v.append(contentsOf: src[i..<j])
            for u in String(v).precomposedStringWithCompatibilityMapping.unicodeScalars where !foldInvisible.contains(u.value) { s.append(u); o.append(i) }
        }
        i = j
    }
    func blank(_ u: Unicode.Scalar) -> Bool { u == " " || u == "\t" }
    var t: [Unicode.Scalar] = [], to: [Int] = []
    var k = 0
    while k < s.count {                                     // 3. line-end hyphen rejoin; any other line break is a space
        if foldHyphens.contains(s[k].value) {
            var e = k + 1
            while e < s.count && blank(s[e]) { e += 1 }
            var brk = false
            if e < s.count && s[e] == "\r" { e += 1; if e < s.count && s[e] == "\n" { e += 1 }; brk = true }
            else if e < s.count && s[e] == "\n" { e += 1; brk = true }
            if brk {
                while e < s.count && blank(s[e]) { e += 1 }
                if k > 0 && e < s.count && isLetterScalar(s[k - 1]) && s[e].properties.generalCategory == .lowercaseLetter { k = e; continue }
            }
        }
        if s[k] == "\r" || s[k] == "\n" {
            t.append(" "); to.append(o[k]); k += (s[k] == "\r" && k + 1 < s.count && s[k + 1] == "\n") ? 2 : 1; continue
        }
        t.append(s[k]); to.append(o[k]); k += 1
    }
    s = []; o = []
    for (u, off) in zip(t, to) {                            // 4. default lowercase mapping, one character at a time
        if u.value < 0x80 { let l = (u.value >= 65 && u.value <= 90) ? Unicode.Scalar(u.value + 32)! : u; s.append(l); o.append(off) }
        else { for l in u.properties.lowercaseMapping.unicodeScalars { s.append(l); o.append(off) } }
    }
    func word(_ i: Int) -> Bool { i >= 0 && i < s.count && isKeyScalar(s[i]) }
    func has(_ p: Int, _ w: String) -> Bool {
        let ws = Array(w.unicodeScalars)
        return p + ws.count <= s.count && zip(s[p..<(p + ws.count)], ws).allSatisfy { $0 == $1 }
    }
    var r: [Unicode.Scalar] = [], ro: [Int] = []
    func emit(_ text: String, _ offs: [Int]) { r.append(contentsOf: text.unicodeScalars); ro.append(contentsOf: offs) }
    var p = 0
    while p < s.count {                                     // 5. number words -> digits, per cent / % -> percent (whole words)
        var end = -1, tens: Int? = nil, unit: Int? = nil, unitAt = 0, small: Int? = nil, centAt = -1
        if let ti = foldTensWords.firstIndex(where: { has(p, $0) }) {
            tens = ti + 2; end = p + foldTensWords[ti].unicodeScalars.count
            var q = end
            if q < s.count && foldHyphens.contains(s[q].value) { q += 1 } else { while q < s.count && blank(s[q]) { q += 1 } }
            if q > end, let di = foldDigitWords.firstIndex(where: { has(q, $0) }) { unit = di + 1; unitAt = q; end = q + foldDigitWords[di].unicodeScalars.count }
        } else if let w = foldUnitsLongestFirst.first(where: { has(p, $0) }) {
            small = foldUnitWords.firstIndex(of: w)!; end = p + w.unicodeScalars.count
        } else if has(p, "per") {
            var q = p + 3
            while q < s.count && blank(s[q]) { q += 1 }
            if q > p + 3 && has(q, "cent") { centAt = q; end = q + 4 }
        } else if s[p] == "%" { end = p + 1 }
        if end < 0 { r.append(s[p]); ro.append(o[p]); p += 1; continue }
        let keep = { r.append(contentsOf: s[p..<end]); ro.append(contentsOf: o[p..<end]) }
        if s[p] == "%" {
            let text = (word(p - 1) ? " " : "") + "percent" + (word(end) ? " " : "")
            emit(text, Array(repeating: o[p], count: text.unicodeScalars.count))
        } else if word(p - 1) { keep() }
        else if let tv = tens {
            let end1 = p + foldTensWords[tv - 2].unicodeScalars.count
            if let u = unit, !word(end) { emit("\(tv)\(u)", [o[p], o[unitAt]]) }
            else if word(end1) { keep() }
            else { emit("\(tv)0", [o[p], o[p + 1]]); r.append(contentsOf: s[end1..<end]); ro.append(contentsOf: o[end1..<end]) }
        } else if word(end) { keep() }
        else if let n = small { let d = String(n); emit(d, (0..<d.count).map { o[p + $0] }) }
        else { emit("percent", (0..<3).map { o[p + $0] } + (0..<4).map { o[centAt + $0] }) }
        p = end
    }
    return (r, ro)
}
/// The fold key (only L* / Nd characters of steps 1-5) and, per key character, the index of its original character.
func fold(_ text: String) -> (key: String, offsets: [Int]) {
    let (s, o) = foldedScalars(text)
    var key = String.UnicodeScalarView(), offs: [Int] = []
    for (u, off) in zip(s, o) where isKeyScalar(u) { key.append(u); offs.append(off) }
    return (String(key), offs)
}
/// Word tokens: maximal L* / Nd runs of steps 1-5.
func foldTokens(_ text: String) -> [String] {
    var out: [String] = [], cur = String.UnicodeScalarView()
    for u in foldedScalars(text).0 {
        if isKeyScalar(u) { cur.append(u) } else if !cur.isEmpty { out.append(String(cur)); cur = String.UnicodeScalarView() }
    }
    if !cur.isEmpty { out.append(String(cur)) }
    return out
}
/// Match tokens: fold tokens without single-character non-digit runs. No stopword list: anchors and refind weigh common
/// words by how many recognised lines carry them.
func matchTokens(_ s: String) -> [String] {
    foldTokens(s).filter { $0.unicodeScalars.count >= 2 || $0.unicodeScalars.first!.properties.generalCategory == .decimalNumber }
}
func tokens(_ s: String) -> [String] { matchTokens(s) }
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
    // page->screen fit behind draw records' page_rect, kept without the scroll offset `off` (screen = s * page + o + off)
    var pageFit: (s: Double, ox: Double, oy: Double, src: String)? = nil
    var firstDrawSrc = "capture+offset"     // how the current show's first draw was placed: page-fit | capture+offset | held
    var headerPanel: NSPanel? = nil
    let headerShape = CAShapeLayer()
    var headerDelta: CGRect? = nil          // header rect relative to the row band's origin (moves with it on scroll)
    let refShape = CAShapeLayer()
    var refShown = 0, transitions = 0, heldBig = 0
    var cue = "text", suppressed = false
    var band: [CGRect] = []                 // text band line rects relative to `rect`'s origin (empty: a single-rect show)
    var bandPage: [[Double]] = []           // the show's doc.lines (page space), mapped with one page fit
    var sentenceId: Any = NSNull()
    var colRect: CGRect? = nil, lineH: CGFloat = 0
    var stuckVotes = 0, lastMomentum = 0.0
    var pendingJump: CGRect? = nil
    var hScrollOK = false                // horizontal deltas only once OCR confirmed the content really moves sideways
    var stuckY = 0                       // +1/-1: content did not move under deltas of that sign (document edge)
    var predX = 0.0, predAt = 0.0, rejX = 0.0
    var wantScan = false                 // a scroll just happened / was rejected: confirm position at once
    var rejN: [String: Int] = [:], rejTotal = 0, lastRejLog = 0.0, corrections = 0
    var runId = ""
    // per-show result (contract A3 cue_result): when the show arrived, its first draw, corrections after it
    var showOpen = false, showAt = 0.0, showFirstDrawAt: Double? = nil, showCorrections = 0, showMaxShift: CGFloat = 0, showHid = false
    var cueShows = 0, cueLanded = 0, cueHidden = 0
    var darkPage = false
    let tickShape = CAShapeLayer()
    var lastCueWrite = 0.0, cueRect: [Int]? = nil
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
        v.layer!.addSublayer(shape); v.layer!.addSublayer(tickShape)
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
            if s.shown, t - s.lastCueWrite >= 0.25 { s.writeCue() }          // cue.json stays fresh while the cue is drawn
        }
        RunLoop.main.add(hb!, forMode: .common)
        // No view display link: it only ticks while the panel is on screen, so a hidden panel was never shown (draws: 0).
        // Coalesce instead: apply at most once per main-loop turn.
    }
    // Text: a soft ice band (fill, no border) with the glacier margin tick. Figure / table: a glacier box; parts: the ember oval.
    func cueStyle() {
        let look = cueLook(cueKind, darkPage: darkPage)
        CATransaction.begin(); CATransaction.setDisableActions(true)
        shape.fillColor = look.fill; shape.strokeColor = look.stroke; shape.lineWidth = look.lineWidth; shape.lineDashPattern = look.dash
        tickShape.fillColor = look.tick; tickShape.isHidden = look.tick == nil
        CATransaction.commit()
    }
    /// The panel's area around a cue's text rect (top-left screen points): room for the oval's margin or the band's tick.
    func cueArea(_ r: CGRect) -> CGRect {
        if isEllipse { return r.insetBy(dx: -(r.width * 0.12 + 6), dy: -(r.height * 0.12 + 6)) }
        let left: CGFloat = cueKind == "text_band" ? CueTok.tickGap + CueTok.tickW : 4     // the band fill pads 4, the tick sits left of it
        return CGRect(x: r.minX - left, y: r.minY - 2, width: r.width + left + 4, height: r.height + 4)
    }
    func frameFor(_ r: CGRect) -> NSRect {
        let H = NSScreen.screens.first?.frame.height ?? 0
        let g = cueArea(r)
        return NSRect(x: g.minX, y: H - g.maxY, width: g.width, height: g.height)
    }
    /// cue.json for the bar's collision rule: {run_id, seq, rect} of the drawn cue, global top-left screen points.
    func writeCue() {
        guard let r = cueRect else { return }
        lastCueWrite = now()
        if let d = try? JSONSerialization.data(withJSONObject: ["run_id": runId, "seq": seq, "rect": r, "ts": (now() * 1000).rounded() / 1000]) {
            try? d.write(to: URL(fileURLWithPath: home + "/cue.json"), options: .atomic)
        }
    }
    func removeCue() { cueRect = nil; try? FileManager.default.removeItem(atPath: home + "/cue.json") }
    /// cue_result for the show that just ended (A3): landed = drawn within 300 ms and no later correction beyond
    /// max(12 pt, half a line); hidden = it ended hidden (app switch, scrolled off) and is left out of on_target_rate.
    func endShow() {
        guard showOpen else { return }
        showOpen = false
        let firstMs: Double? = showFirstDrawAt.map { ($0 - showAt) * 1000 }
        let limit = max(12, (lineH > 0 ? lineH : 20) / 2)
        let landed = (firstMs ?? .infinity) <= 300 && showMaxShift <= limit
        let hidden = !shown && (showHid || (showFirstDrawAt == nil && !(front && winFront && !hiddenByKey && scanVisible)))
        cueShows += 1; if landed { cueLanded += 1 }; if hidden { cueHidden += 1 }
        rec(["event": "cue_result", "run_id": runId, "seq": seq, "cue_kind": cueKind, "first_draw_ms": firstMs.map { $0.rounded() } ?? NSNull(),
             "corrections": showCorrections, "max_shift_px": Int(showMaxShift.rounded()), "landed": landed, "hidden": hidden])
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
    func setBand(_ abs: [CGRect]) {
        guard let u = unionOf(abs) else { band = []; return }
        rect = u; band = abs.map { $0.offsetBy(dx: -u.minX, dy: -u.minY) }
    }
    func bandAbsolute() -> [CGRect] {
        guard let r = rect else { return [] }
        return band.map { $0.offsetBy(dx: r.minX, dy: r.minY) }
    }
    func flush() {
        dirty = false; lastWork = "flush"; lastFlush = now()
        guard let p = panel else { return }
        var inWin = true
        var lines = bandAbsolute()
        if let r = rect, let w = winFrame {
            if !lines.isEmpty {                                           // a band: each line clamped, 30 % of the summed area
                if let c = clampBand(lines, to: w) { lines = c } else { inWin = false }
            } else {
                let vis = r.intersection(w)
                inWin = !vis.isNull && vis.width * vis.height >= 0.3 * r.width * r.height
            }
            if !inWin && shown { hideTrigger = ("out-of-window", now()); rec(["event": "box-out-of-window", "rect": [Int(r.minX), Int(r.minY), Int(r.width), Int(r.height)]]); wantScan = true }
        }
        let show = rect != nil && front && winFront && !hiddenByKey && scanVisible && !suppressed && inWin
        if show, var r = rect {
            if let u = unionOf(lines) { r = u } else if let w = winFrame { r = r.intersection(w) }   // clamp to the window
            let f = frameFor(r)
            CATransaction.begin(); CATransaction.setDisableActions(true)
            p.setFrame(f, display: false)
            let area = cueArea(r), look = cueLook(cueKind, darkPage: darkPage)
            func local(_ q: CGRect) -> CGRect { CGRect(x: q.minX - area.minX, y: area.maxY - q.maxY, width: q.width, height: q.height) }
            shape.frame = p.contentView!.bounds; tickShape.frame = p.contentView!.bounds
            if !lines.isEmpty { shape.path = bandPath(lines, in: area, radius: look.radius) }
            else if isEllipse { shape.path = CGPath(ellipseIn: CGRect(x: 1, y: 1, width: f.width - 2, height: f.height - 2), transform: nil) }
            else {
                let body = local(r.insetBy(dx: -4, dy: -2)).insetBy(dx: 1, dy: 1), rad = min(look.radius, body.height / 2)
                shape.path = CGPath(roundedRect: body, cornerWidth: rad, cornerHeight: rad, transform: nil)
            }
            let tick = CGRect(x: r.minX - CueTok.tickGap - CueTok.tickW, y: r.minY - 2, width: CueTok.tickW, height: r.height + 4)
            tickShape.path = look.tick == nil ? nil : CGPath(rect: local(tick), transform: nil)
            CATransaction.commit()
            cueRect = [Int(f.minX), Int((NSScreen.screens.first?.frame.height ?? 0) - f.maxY), Int(f.width), Int(f.height)]
            if now() - lastCueWrite >= 0.25 { writeCue() }
            if showOpen && showFirstDrawAt == nil { showFirstDrawAt = now() }
            showHid = false
            if !p.isVisible { p.orderFrontRegardless() }
            drawHeader(r)
            if !shown || abs(f.minY - lastDrawn.minY) > 2 || abs(f.minX - lastDrawn.minX) > 2 || abs(f.width - lastDrawn.width) > 2 || abs(f.height - lastDrawn.height) > 2 {
                draws += 1
                let H = NSScreen.screens.first?.frame.height ?? 0
                let pageRect: Any = pageFit.map { pf -> [Double] in
                    let ox = pf.ox + Double(off.x), oy = pf.oy + Double(off.y)
                    return [(Double(r.minX) - ox) / pf.s, (Double(r.minY) - oy) / pf.s, (Double(r.maxX) - ox) / pf.s, (Double(r.maxY) - oy) / pf.s].map { ($0 * 10).rounded() / 10 }
                } ?? NSNull()
                var draw: [String: Any] = ["event": "draw", "seq": seq, "cue": cue, "cue_kind": cueKind, "header": headerDelta != nil, "rect": [Int(f.minX), Int(H - f.maxY), Int(f.width), Int(f.height)],
                     "text_rect": [Int(r.minX), Int(r.minY), Int(r.width), Int(r.height)], "src": src, "overlay": "swift-panel",
                     "on_screen": p.isVisible, "page_rect": pageRect, "page_fit": pageFit?.src ?? NSNull()]
                draw["sentence_id"] = sentenceId; draw["line_count"] = lines.isEmpty ? 1 : lines.count
                if drawnSeq != seq { draw["first_draw_src"] = firstDrawSrc }
                rec(drawMeta.merging(draw) { $1 })
                shown = true; lastDrawn = f
            }
            drawnSeq = seq
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
                if trig != "speech-finished" { showHid = true }
                removeCue()
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
    func scan(rect r: CGRect?, visible: Bool, capTs: Double, method: String, reason: String?, fit: (Double, Double, Double)? = nil, band newBand: [CGRect]? = nil) {
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
                if showOpen && showFirstDrawAt != nil { showCorrections += 1; showMaxShift = max(showMaxShift, max(abs(ex), abs(ey))) }
                rec(["event": "correction", "seq": seq, "dx": Int(ex), "dy": Int(ey), "from": src, "to": method, "after_ms": Int((now() - predAt) * 1000)])
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
            if let b = newBand { setBand(b) }
            if method == "doc", let f = fit { pageFit = (f.0, f.1 - Double(off.x), f.2 - Double(off.y), "anchors") }
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
        let look = cueLook("table_header", darkPage: darkPage)
        headerShape.path = CGPath(roundedRect: CGRect(x: 1, y: 1, width: f.width - 2, height: f.height - 2), cornerWidth: look.radius, cornerHeight: look.radius, transform: nil)
        headerShape.fillColor = look.fill; headerShape.strokeColor = nil
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
            v.layer!.addSublayer(refShape); p.contentView = v; refPanel = p
        }
        guard let p = refPanel, front, winFront else { return }
        let rr = r.offsetBy(dx: off.x, dy: off.y).insetBy(dx: -4, dy: -2)
        let H = NSScreen.screens.first?.frame.height ?? 0, f = NSRect(x: rr.minX, y: H - rr.maxY, width: rr.width, height: rr.height)
        let look = cueLook("back_ref", darkPage: darkPage)
        CATransaction.begin(); CATransaction.setDisableActions(true)
        p.setFrame(f, display: false)
        refShape.frame = p.contentView!.bounds
        let b = CGRect(x: 1, y: 1, width: f.width - 2, height: f.height - 2)
        refShape.path = CGPath(roundedRect: b, cornerWidth: look.radius, cornerHeight: look.radius, transform: nil)
        refShape.fillColor = look.fill; refShape.strokeColor = look.stroke
        refShape.lineWidth = look.lineWidth; refShape.lineDashPattern = look.dash
        CATransaction.commit()
        p.orderFrontRegardless()
        refShown += 1
        rec(["event": "ref-outline", "seq": seq, "rect": [Int(r.minX), Int(r.minY), Int(r.width), Int(r.height)], "ms": 1500])
        let my = seq
        _ = my; DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { p.orderOut(nil) }
    }
    func event(seq s: Int, rect r0: CGRect?, range rg: [Int]?, srcLines: [String], color: String?, opacity: Double, ellipse: Bool,
               isText: Bool = true, nlines: Int? = nil, ref: CGRect? = nil, figure: Bool = false, drawOn: Bool = true,
               cueKind ck: String? = nil, header: CGRect? = nil, meta: [String: Any] = [:],
               rects: [CGRect] = [], docLines: [[Double]] = [], sentenceId sid: Any = NSNull()) {
        var r = r0
        endShow()
        showOpen = drawOn; showAt = now(); showFirstDrawAt = nil; showCorrections = 0; showMaxShift = 0; showHid = false
        drawMeta = meta.merging(["run_id": runId]) { a, _ in a }
        if (meta["doc_target"] as? [Double]) == nil { pageFit = nil }   // no page-space box: nothing to map back
        else if pageFit?.src != "anchors", let t = meta["doc_transform"] as? [Double], t.count == 3 { pageFit = (t[0], t[1], t[2], "event-transform") }
        sentenceId = sid
        let isBand = isText && !figure && (!rects.isEmpty || !docLines.isEmpty)
        bandPage = isBand ? docLines : []
        // first draw: the page geometry through the current page fit when there is one (A2.7), else the show's screen
        // geometry plus the scroll since the hotkey
        let fd = firstDraw(docTarget: meta["doc_target"] as? [Double], docLines: isBand ? docLines : [], rects: isBand ? rects : [],
                           rect: r0, fit: pageFit.map { ($0.s, $0.ox, $0.oy) }, off: off)
        firstDrawSrc = fd.src
        if fd.src == "capture+offset" && (off.x != 0 || off.y != 0) { wantScan = true }
        var firstBand: [CGRect]? = isBand && !fd.band.isEmpty ? fd.band : nil
        if !isBand && fd.src == "page-fit", let m = fd.rect { r = m.offsetBy(dx: -off.x, dy: -off.y) }
        if isBand && firstBand == nil, let pf = pageFit { firstBand = mapBand(docLines, fit: (pf.s, pf.ox + Double(off.x), pf.oy + Double(off.y))) }
        cue = figure ? "figure" : "text"; suppressed = !drawOn
        cueKind = ck ?? (ellipse ? "part_oval" : figure ? "figure_box" : "text_band")
        if cueKind == "table_row", let h = header, let n = r0 { headerDelta = CGRect(x: h.minX - n.minX, y: h.minY - n.minY, width: h.width, height: h.height) }
        else { headerDelta = nil }                              // row 0 is the header itself; other cues have none
        cueStyle()
        if figure { refPanel?.orderOut(nil) }                    // one cue at a time: no text outline while a figure part is boxed
        if !drawOn { rec(["event": "cue-off", "seq": s, "cue": cue]) }
        // never flash a big provisional box between sentences: a text sentence's box must fit its own lines
        if let n = r0, isText, firstBand == nil, fd.src != "page-fit" {
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
            if held { r = nil; firstDrawSrc = "held" }              // keep the previous box until OCR / AX finds the tight one
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
        if let b = firstBand { setBand(b); scanVisible = true; src = "event" }
        else { band = []; if let r = r { rect = r.offsetBy(dx: off.x, dy: off.y); scanVisible = true; src = "event" } }
        apply()                                                   // draw the event box now; AX refines it later
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {   // no fallback box any more: fail loudly instead
            guard self.seq == s, self.drawnSeq != s, self.rect != nil, !self.suppressed, self.front, self.winFront, self.scanVisible, !self.hiddenByKey else { return }
            let why = self.panel == nil ? "no panel" : (self.panel!.isVisible ? "visible but no draw record" : "panel not on screen")
            self.drawFails += 1
            self.rec(["event": "draw-failed", "seq": s, "why": why, "after_ms": 300])
            let line = "\(stamp()) NATIVE PANEL FAILED TO DRAW: seq \(s) (\(why)), run \(self.runId)\n"
            FileHandle.standardError.write(line.data(using: .utf8)!)
        }
        if axDead { rec(["event": "ax-range", "seq": s, "hit": false, "why": "ax-disabled"]); return }
        if !bandPage.isEmpty { rec(["event": "ax-range", "seq": s, "hit": false, "why": "page-band"]); return }   // the page fit places it
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
         "k_trackpad": (kCont * 100).rounded() / 100, "k_wheel": (kLine * 100).rounded() / 100,
         "cues": ["shows": cueShows, "landed": cueLanded, "hidden": cueHidden]]
    }
}

// ---------------- OCR ----------------
struct OLine { let text: String; let toks: Set<String>; let box: CGRect }   // box in screen points (top-left origin)

// Document-aware focus: speakeasy sends a target in PDF page space plus text anchors (page lines with their page rects).
// Match anchors to recognised lines, fit page->screen (uniform scale + offset), map the target (exact line / figure / panel).
var gDocDiag: [String: Int] = [:]
func docMap(_ lines: [OLine], _ doc: [String: Any], frame: CGRect? = nil) -> (CGRect, Int, Double)? {
    docMapFit(lines, doc, frame: frame).map { ($0.0, $0.1, $0.2) }
}
// docMap plus the fit itself (scale, x offset, y offset): screen = s * page + offset
func docMapFit(_ lines: [OLine], _ doc: [String: Any], frame: CGRect? = nil) -> (CGRect, Int, Double, (Double, Double, Double))? {
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
    return (CGRect(x: sc * tg[0] + ox, y: sc * tg[1] + oy, width: sc * (tg[2] - tg[0]), height: sc * (tg[3] - tg[1])), P.count, rms, (sc, ox, oy))
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
// ---------------- `speakeasy-track ocr IN.png OUT.json` (phase6-block-classifier.md A1.1) ----------------
// Vision settings are constants: no flag, environment or config changes the result. Recognised text never reaches stdout / stderr.
let OCR_ACCURATE = true, OCR_LANGUAGE_CORRECTION = true, OCR_AUTO_LANGUAGE = true
func ocrJSONString(_ s: String) -> String {
    guard let d = try? JSONSerialization.data(withJSONObject: [s], options: [.withoutEscapingSlashes]), let a = String(data: d, encoding: .utf8) else { return "\"\"" }
    return String(a.dropFirst().dropLast())
}
func ocrNum(_ v: Double) -> String { v == v.rounded() ? String(format: "%.1f", v) : String(v) }
/// Exit 0: OUT.json written atomically (zero lines too); 2 usage; 3 IN.png unreadable; 4 Vision error. A stale OUT.json is removed first.
/// Engine "vision-text" (VNRecognizeTextRequest); tables are [] (the caller groups lines into rows). RecognizeDocumentsRequest
/// (macOS 26 "vision-documents") needs the macOS 26 SDK, which this toolchain does not have.
func ocrCommand(_ args: [String]) -> Int32 {
    guard args.count == 2 else { FileHandle.standardError.write("usage: speakeasy-track ocr IN.png OUT.json\n".data(using: .utf8)!); return 2 }
    let inPath = args[0], outPath = args[1]
    unlink(outPath)
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: inPath) as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return 3 }
    let t0 = now()
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = OCR_ACCURATE ? .accurate : .fast
    req.usesLanguageCorrection = OCR_LANGUAGE_CORRECTION
    if #available(macOS 13.0, *) { req.automaticallyDetectsLanguage = OCR_AUTO_LANGUAGE }
    do { try VNImageRequestHandler(cgImage: img, options: [:]).perform([req]) } catch { return 4 }
    let W = Double(img.width), H = Double(img.height)
    func clip(_ v: Double, _ m: Double) -> Double { (min(max(v, 0), m) * 10).rounded() / 10 }
    var found: [(r: [Double], t: String, c: Double)] = []
    for o in req.results ?? [] {
        guard let c = o.topCandidates(1).first else { continue }
        let t = c.string.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        if t.isEmpty { continue }
        let b = o.boundingBox                        // normalised, bottom-left origin
        let r = [clip(Double(b.minX) * W, W), clip((1 - Double(b.maxY)) * H, H), clip(Double(b.maxX) * W, W), clip((1 - Double(b.minY)) * H, H)]
        if r[0] >= r[2] || r[1] >= r[3] { continue }
        found.append((r, t, (Double(c.confidence) * 100).rounded() / 100))
    }
    // rows by y0; a line whose vertical centre is within half the smaller height of the row's first line joins it; rows by x0
    found.sort { ($0.r[1], $0.r[0]) < ($1.r[1], $1.r[0]) }
    var rows: [[(r: [Double], t: String, c: Double)]] = []
    for l in found {
        if let f = rows.last?.first, abs((l.r[1] + l.r[3]) / 2 - (f.r[1] + f.r[3]) / 2) < 0.5 * min(l.r[3] - l.r[1], f.r[3] - f.r[1]) { rows[rows.count - 1].append(l) }
        else { rows.append([l]) }
    }
    let ordered = rows.flatMap { $0.sorted { $0.r[0] < $1.r[0] } }
    let lines = ordered.map { "{\"rect\": [\($0.r.map(ocrNum).joined(separator: ", "))], \"text\": \(ocrJSONString($0.t)), \"conf\": \(ocrNum($0.c))}" }
    let ms = Int(((now() - t0) * 1000).rounded())
    let body = "{\"version\": 1, \"engine\": \"vision-text\", \"image_size\": [\(img.width), \(img.height)], \"unit\": \"px\", \"origin\": \"top-left\", "
        + "\"lines\": [\(lines.joined(separator: ", "))], \"tables\": [], \"ms\": \(ms)}"
    let tmp = outPath + ".tmp\(getpid())"
    guard FileManager.default.createFile(atPath: tmp, contents: body.data(using: .utf8)), rename(tmp, outPath) == 0 else { unlink(tmp); return 4 }
    return 0
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

// ---------------- text bands: one rect per spoken line (docs/contracts/phase3-sentence-ids.md D) ----------------
/// Page-space line rects [x0, y0, x1, y1] mapped to the screen with one fit (screen = s * page + offset); never a per-line fit.
func mapBand(_ lines: [[Double]], fit: (Double, Double, Double)) -> [CGRect] {
    lines.map { CGRect(x: fit.0 * $0[0] + fit.1, y: fit.0 * $0[1] + fit.2, width: fit.0 * ($0[2] - $0[0]), height: fit.0 * ($0[3] - $0[1])) }
}
/// A2.7: where a show is first drawn. With a page->screen fit (OCR anchors, or the show's own transform; screen = s * page +
/// o, kept without the scroll offset) the show's page geometry (doc.lines, else doc.target) is mapped with that fit plus
/// the scroll since the hotkey; without one, its screen geometry plus that scroll. Returns (band rects, rect, source).
func firstDraw(docTarget: [Double]?, docLines: [[Double]], rects: [CGRect], rect: CGRect?,
               fit: (Double, Double, Double)?, off: CGPoint) -> (band: [CGRect], rect: CGRect?, src: String) {
    if let f = fit {
        let g = (f.0, f.1 + Double(off.x), f.2 + Double(off.y))
        if !docLines.isEmpty { let b = mapBand(docLines, fit: g); return (b, unionOf(b), "page-fit") }
        if let t = docTarget, t.count == 4 { return ([], mapBand([t], fit: g).first, "page-fit") }
    }
    let b = rects.map { $0.offsetBy(dx: off.x, dy: off.y) }
    return (b, b.isEmpty ? rect?.offsetBy(dx: off.x, dy: off.y) : unionOf(b), "capture+offset")
}
func unionOf(_ rects: [CGRect]) -> CGRect? { rects.dropFirst().reduce(rects.first) { $0?.union($1) } }
/// Each rect clamped to the window, the ones fully outside dropped; nil (hide) when under 30 % of the band's area is visible.
func clampBand(_ rects: [CGRect], to win: CGRect) -> [CGRect]? {
    let total = rects.reduce(0.0) { $0 + Double($1.width * $1.height) }
    let vis = rects.map { $0.intersection(win) }.filter { !$0.isNull && $0.width > 0 && $0.height > 0 }
    let shown = vis.reduce(0.0) { $0 + Double($1.width * $1.height) }
    return total > 0 && shown >= 0.3 * total ? vis : nil
}
/// One rounded rect per band line inside a panel covering `area` (screen, top-left origin); the panel's layer has a
/// bottom-left origin. Overlapping line rects fill once (one path, non-zero winding): nothing spans a gutter or a gap.
func bandPath(_ rects: [CGRect], in area: CGRect, radius: CGFloat = 2) -> CGPath {
    let p = CGMutablePath()
    for b in rects {
        let q = b.insetBy(dx: -4, dy: -2)
        let local = CGRect(x: q.minX - area.minX, y: area.maxY - q.maxY, width: q.width, height: q.height).insetBy(dx: 1, dy: 1)
        let rad = min(radius, local.height / 2)
        p.addRoundedRect(in: local, cornerWidth: rad, cornerHeight: rad)
    }
    return p
}
/// Refind as a check, not a source: passes when OCR recognises at least one band line (text from the page anchors) whose box
/// intersects that line's mapped rect. Without line texts, the source lines' refind box must touch the band.
func bandCheck(_ lines: [OLine], band: [CGRect], texts: [String?], src: [String]) -> Bool {
    var known = false
    for (r, t) in zip(band, texts) {
        guard let t = t else { continue }
        let at = Set(tokens(t)); if at.count < 2 { continue }
        known = true
        if lines.contains(where: { Double($0.toks.intersection(at).count) / Double(at.count) >= 0.75 && $0.box.intersects(r) }) { return true }
    }
    if known { return false }
    guard let rf = refind(lines, target: src, near: unionOf(band)) else { return false }
    return band.contains { $0.intersects(rf) }
}
/// The text of each doc.lines rect, from the show's anchors (page lines with their page rects), or nil when not an anchor.
func bandTexts(_ docLines: [[Double]], anchors: [[Any]]) -> [String?] {
    docLines.map { dl in
        anchors.first { a in
            a.count == 5 && zip(a[0..<4], dl).allSatisfy { abs((($0 as? Double) ?? ($0 as? Int).map(Double.init) ?? .nan) - $1) < 0.05 }
        }?[4] as? String
    }
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
        if CommandLine.arguments.count >= 2 && CommandLine.arguments[1] == "ocr" { exit(ocrCommand(Array(CommandLine.arguments.dropFirst(2)))) }
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
        // --render-cue text_band|back_ref|figure_box|part_oval|table_row --out file.png [--page white|dark]: one cue drawn offscreen
        if let kind = a["render-cue"] {
            guard let out = a["out"], let img = renderCue(kind, darkPage: a["page"] == "dark"),
                  let dst = CGImageDestinationCreateWithURL(URL(fileURLWithPath: out) as CFURL, "public.png" as CFString, 1, nil) else {
                FileHandle.standardError.write("usage: speakeasy-track --render-cue KIND --out FILE.png [--page white|dark]\n".data(using: .utf8)!); exit(2)
            }
            CGImageDestinationAddImage(dst, img, nil)
            if !CGImageDestinationFinalize(dst) { FileHandle.standardError.write("could not write \(out)\n".data(using: .utf8)!); exit(1) }
            return
        }
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
                    "overlay": "swift-panel"], h0 + "/logs/tracker.jsonl")
        // Never prompt: without Screen Recording permission, log and exit before any ScreenCaptureKit/CGWindowList call.
        if !srAllowed {
            appendLine(["ts": (now() * 1000).rounded() / 1000, "event": "exit", "reason": "no_screen_recording", "window_id": Int(a["window-id"] ?? "") ?? NSNull(), "frames": 0,
                        "run_id": a["run-id"] ?? NSNull(), "tracker_pid": Int(getpid()), "cues": ["shows": 0, "landed": 0, "hidden": 0]], h0 + "/logs/tracker.jsonl")
            exit(0)
        }
        guard let widS = a["window-id"], let wid = CGWindowID(widS) else { FileHandle.standardError.write("usage: speakeasy-track --window-id N --pid P [--home DIR] [--fps 3]\n".data(using: .utf8)!); exit(2) }
        let pid = pid_t(a["pid"] ?? "0") ?? 0
        let home = a["home"] ?? (NSHomeDirectory() + "/.speakeasy")
        let fps = max(0.5, min(10, Double(a["fps"] ?? "5") ?? 5))   // text-recognition scans per second
        let tickS = 0.08                                               // loop tick; window front checks also run on their own 25 ms timer
        var lastScan = 0.0, lastTick = now()
        let accurate = (a["ocr"] ?? "fast") == "accurate"     // fast first; accurate is the retry when fast misses
        let focusPath = home + "/focus.json"
        try? FileManager.default.createDirectory(atPath: home + "/logs", withIntermediateDirectories: true)
        // element focused at hotkey time; only when already trusted, never prompt
        let axEl = pid > 0 && AXIsProcessTrusted() ? focusedElement(pid) : nil
        await MainActor.run { Live.shared.runId = a["run-id"] ?? ""; Live.shared.setup(home: home, pid: pid, el: axEl); Live.shared.startWinWatch(wid) }

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
        var bandFails = 0                                              // band checks failed in a row (D.4)
        var figCapRef: CGRect? = nil, figCapSeq = -1
        var frameLogN = 0
        var lastAccurate = 0.0
        var toneSeq = -1
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
                let bandRects = (ev["rects"] as? [[Double]] ?? []).compactMap { $0.count == 4 ? CGRect(x: $0[0], y: $0[1], width: $0[2], height: $0[3]) : nil }
                let docLines = ((ev["doc"] as? [String: Any])?["lines"] as? [[Double]] ?? []).filter { $0.count == 4 }
                let sid: Any = ev["sentence_id"] ?? NSNull()
                let eventMeta = meta
                await MainActor.run { Live.shared.event(seq: sq, rect: rr, range: rg, srcLines: sl, color: col, opacity: op, ellipse: ell, isText: isText && !cueFig, nlines: nl, ref: drawOn ? ref : nil, figure: cueFig, drawOn: drawOn, cueKind: ck, header: hdr, meta: eventMeta, rects: bandRects, docLines: docLines, sentenceId: sid) }
            }
            if !showing && lastSeq >= 0 { await MainActor.run { Live.shared.endShow(); if Live.shared.rect != nil { Live.shared.hideTrigger = ("speech-finished", now()) }; Live.shared.rect = nil; Live.shared.apply() } }
            var out: [String: Any] = ["seq": seq, "focus_pid": epid, "window_id": Int(wid)]
            var scanned = false
            var winFrame: CGRect? = nil
            if showing {
                activeS += tick - prevTick
                let ws = windowState(wid)
                var liveAx = false, deltaOK = false, wantNow = false
                if let w = ws {
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
                    var hitFit: (Double, Double, Double)? = nil
                    var continueFig = false
                    let evDoc = ev["doc"] as? [String: Any]
                    let evDocLines = (evDoc?["lines"] as? [[Double]] ?? []).filter { $0.count == 4 }
                    let evKind = evDoc?["kind"] as? String
                    let isBandShow = (ev["cue"] as? String) != "figure" && (evKind == nil || evKind == "line")
                        && (!(ev["rects"] as? [[Double]] ?? []).isEmpty || !evDocLines.isEmpty)
                    var bandMapped: [CGRect]? = nil
                    if !(isBandShow && !evDocLines.isEmpty), !gAxDead, let el = axEl, let rg = ev["range"] as? [Int], rg.count == 2, let r = axBounds(el, rg[0], rg[1]) {
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
                            // dark documents (design 6.6): sample the page under the cue once per show
                            if toneSeq != seq, let cur = await MainActor.run(body: { Live.shared.rect }), let lum = meanLuminance(img, frame: frame, under: cur) {
                                toneSeq = seq; let dark = lum < CueTok.darkPageLuminance, sq = seq
                                await MainActor.run {
                                    let L = Live.shared
                                    if L.darkPage != dark { L.darkPage = dark; L.cueStyle(); L.rec(["event": "page_tone", "seq": sq, "dark": dark]); L.apply() }
                                }
                            }
                            let o0 = now()
                            var lines = recognise(img, frame: frame, accurate: accurate)
                            var hit: CGRect? = nil
                            if isBandShow {
                                // refind is a check, never the band: pass -> keep; two failures in a row -> refit from anchors
                                let cur = await MainActor.run { Live.shared.bandAbsolute() }
                                let texts = bandTexts(evDocLines, anchors: evDoc?["anchors"] as? [[Any]] ?? [])
                                if !cur.isEmpty && bandCheck(lines, band: cur, texts: cur.count == texts.count ? texts : cur.map { _ in nil }, src: target) {
                                    bandFails = 0; hit = unionOf(cur); method = "check"
                                } else {
                                    bandFails += 1
                                    if bandFails >= 2 || cur.isEmpty, let dm = evDoc, !evDocLines.isEmpty, let (_, k, e, fit) = docMapFit(lines, dm, frame: frame) {
                                        let mapped = mapBand(evDocLines, fit: fit)
                                        bandFails = 0; bandMapped = mapped; hit = unionOf(mapped); method = "doc"; hitFit = fit
                                        out["anchors"] = k; out["rms"] = (e * 10).rounded() / 10
                                    } else if !cur.isEmpty {
                                        let all = Set(lines.flatMap { $0.toks })
                                        let samePage = baseline.map { Double(all.intersection($0).count) / Double(max(1, all.union($0).count)) >= 0.35 } ?? true
                                        if samePage { hit = unionOf(cur); method = "kept" }               // the last good mapping, moved by scroll deltas
                                        if bandFails >= 2 { let n = bandFails; await MainActor.run { Live.shared.rec(["event": "check_failed", "seq": seq, "fails": n]) } }
                                    }
                                }
                            } else if let dm = ev["doc"] as? [String: Any], let (r, k, e, fit) = docMapFit(lines, dm, frame: frame) {
                                let vis = r.intersection(frame)
                                // sanity: the mapped box must lie mostly in the window, and a text line must agree with where its words are
                                var ok = !vis.isNull && vis.width * vis.height >= 0.5 * r.width * r.height
                                if ok && (dm["kind"] as? String) == "line", let rf = refind(lines, target: target, near: lastBox),
                                   rf.intersection(r).isNull { ok = false }
                                if ok { hit = vis; method = "doc"; hitFit = fit; out["anchors"] = k; out["rms"] = (e * 10).rounded() / 10 }
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
                            if hit == nil && !isBandShow { hit = refind(lines, target: target, near: lastBox) }
                            if !isBandShow, let h = hit, h.width < 24 || h.height < 7 { hit = nil; out["tiny_hit"] = true }   // a stray word, not the sentence
                            if hit == nil && !isBandShow && !accurate && tick - lastAccurate > 1.5 {   // accurate retry, at most every 1.5 s
                                lastAccurate = tick
                                lines = recognise(img, frame: frame, accurate: true)
                                if let dm = ev["doc"] as? [String: Any], let (r, k, e, fit) = docMapFit(lines, dm, frame: frame) {
                                    let vis = r.intersection(frame)
                                    if !vis.isNull && vis.width * vis.height >= 0.5 * r.width * r.height {
                                        hit = vis; method = "doc"; hitFit = fit; out["anchors"] = k; out["rms"] = (e * 10).rounded() / 10 }
                                }
                                if hit == nil { hit = refind(lines, target: target, near: lastBox) }
                            }
                            ocrMs += (now() - o0) * 1000; ocrN += 1
                            let all = Set(lines.flatMap { $0.toks })
                            if baseline == nil { baseline = all }
                            if let bl = baseline, sameContent(all, bl) {
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
                    let fnd = found, vis = found != nil, capTs = out["cap_ts"] as? Double ?? 0, m = method, fitFound = hitFit, newBand = bandMapped
                    let hide = fnd == nil && (out["reason"] as? String) == "content-changed" || (fnd == nil && missRun >= 1 && out["reason"] != nil)
                    let rsn = out["reason"] as? String
                    if vis || hide { await MainActor.run { Live.shared.scan(rect: fnd, visible: vis, capTs: capTs, method: m, reason: rsn, fit: fitFound, band: newBand) } }
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
            // changed = >2pt move or visibility flip
            func same(_ x: [String: Any], _ y: [String: Any]) -> Bool {
                if (x["visible"] as? Bool) != (y["visible"] as? Bool) || (x["seq"] as? Int) != (y["seq"] as? Int) { return false }
                guard let a = x["rect"] as? [Double], let b = y["rect"] as? [Double] else { return (x["rect"] == nil) == (y["rect"] == nil) }
                return zip(a, b).allSatisfy { abs($0 - $1) <= 2 }
            }
            let changed = !out.isEmpty && !same(out, lastOut)
            if changed { lastOut = out }
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
        await MainActor.run { Live.shared.endShow(); if Live.shared.rect != nil { Live.shared.hideTrigger = ("speech-finished", now()) }; Live.shared.rect = nil; Live.shared.flush(); Live.shared.removeCue() }
        let lagSt: [String: Any] = await MainActor.run { Live.shared.stats() }
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
        st["overlay"] = "swift-panel"
        appendLine(st, home + "/logs/tracker.jsonl")
    }
}
