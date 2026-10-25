// speakeasy-track: keeps the focus box glued to the source text while speakeasy reads.
// Locks onto one window (by CGWindowID) at hotkey time, then a few times a second:
//   1. AX: if the event carries an absolute character range and the app answers AXBoundsForRange, use that (cheap).
//   2. Otherwise capture just that window (ScreenCaptureKit) and run Apple Vision text recognition, then re-find the
//      current sentence's source lines among the recognised lines (token overlap) and box them tightly.
// Hides the box when the window is not frontmost, the content changed (tab switch), or the text scrolled off.
// Reads  ~/.speakeasy/focus.json (written by speakeasy as each sentence starts playing)
// Writes ~/.speakeasy/track.json (read by the Hammerspoon overlay) and appends stats to ~/.speakeasy/logs/tracker.jsonl
// Everything is local (Vision + ScreenCaptureKit are part of macOS). Build: swiftc -O -parse-as-library tracker.swift
import Foundation
import AppKit
import ScreenCaptureKit
import Vision
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

// ---------------- OCR ----------------
struct OLine { let text: String; let toks: Set<String>; let box: CGRect }   // box in screen points (top-left origin)
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
        app.setActivationPolicy(.prohibited)          // no Dock icon, never steals focus
        _ = CGMainDisplayID()
        let args = a
        Task.detached { await Tracker.run(args); exit(0) }
        app.run()
    }
    static func run(_ a: [String: String]) async {
        // Offline check: --ocr-image file.png --find "text" [--frame x,y,w,h]
        if let p = a["ocr-image"] {
            guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: p) as CFURL, nil),
                  let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { print("{\"error\":\"bad image\"}"); return }
            let f = (a["frame"] ?? "").split(separator: ",").compactMap { Double($0) }
            let frame = f.count == 4 ? CGRect(x: f[0], y: f[1], width: f[2], height: f[3]) : CGRect(x: 0, y: 0, width: img.width, height: img.height)
            for acc in [false, true] {
                let t0 = now(); let lines = recognise(img, frame: frame, accurate: acc); let ms = (now() - t0) * 1000
                let hit = refind(lines, target: (a["find"] ?? "").components(separatedBy: "\n"), near: nil)
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

        // ScreenCaptureKit: find our window once
        var filter: SCContentFilter? = nil
        if let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false),
           let w = content.windows.first(where: { $0.windowID == wid }) {
            filter = SCContentFilter(desktopIndependentWindow: w)
        }
        let t0 = now(), cpu0 = cpu_s()
        var frames = 0, axHits = 0, ocrHits = 0, misses = 0, hidden = 0
        var ocrMs = 0.0, capMs = 0.0, ocrN = 0
        var lastSeq = -1, lastPid = -1, lastShowTs = now(), lastClear: Double? = nil
        var lastBox: CGRect? = nil, lastOut: [String: Any] = [:]
        var baseline: Set<String>? = nil
        var missRun = 0
        var frameLogN = 0
        var lastAccurate = 0.0
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
            if state == "clear" { if lastClear == nil { lastClear = tick }; if tick - lastClear! > 1.5 { exitReason = "speech-finished"; break } } else { lastClear = nil }
            if state == "show" { lastShowTs = tick }
            if tick - lastShowTs > (lastSeq < 0 ? 45 : 25) { exitReason = "no-focus-events"; break }                  // nothing to follow (first wait covers LLM/vision)
            if epid > 0 && lastPid > 0 && epid != lastPid { exitReason = "newer-run"; break }                   // a newer speakeasy run took over
            if epid > 0 && kill(pid_t(epid), 0) != 0 && state == "show" && tick - t0 > 2 { exitReason = "speaker-died"; break }   // speaker died
            if state == "show" && (seq != lastSeq || epid != lastPid) { lastSeq = seq; lastPid = epid; missRun = 0 }
            var out: [String: Any] = ["seq": seq, "focus_pid": epid, "window_id": Int(wid)]
            var scanned = false
            var winFrame: CGRect? = nil
            if state == "show" {
                activeS += tick - prevTick
                let ws = windowState(wid)
                let seqChanged = (lastOut["seq"] as? Int) != seq
                let wasHiddenForFront = (lastOut["reason"] as? String).map { ["not-frontmost", "not-on-screen", "window-gone"].contains($0) } ?? false
                if ws == nil || !ws!.onScreen || !ws!.front {
                    // hide at once (checked every tick, no debounce)
                    out["visible"] = false; out["reason"] = ws == nil ? "window-gone" : (ws!.onScreen ? "not-frontmost" : "not-on-screen"); hidden += 1
                } else if !(seqChanged || wasHiddenForFront || tick - lastScan >= 1.0 / fps) {
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
                        if let img = try? await SCScreenshotManager.captureImage(contentFilter: flt, configuration: cfg) {
                            capMs += (now() - c0) * 1000
                            let o0 = now()
                            var lines = recognise(img, frame: frame, accurate: accurate)
                            var hit = refind(lines, target: target, near: lastBox)
                            if hit == nil && !accurate && tick - lastAccurate > 1.5 {   // accurate retry, at most every 1.5 s
                                lastAccurate = tick
                                lines = recognise(img, frame: frame, accurate: true); hit = refind(lines, target: target, near: lastBox)
                            }
                            ocrMs += (now() - o0) * 1000; ocrN += 1
                            let all = Set(lines.flatMap { $0.toks })
                            if baseline == nil { baseline = all }
                            if let r = hit { found = r; method = "ocr" }
                            else {
                                let inter = Double(all.intersection(baseline!).count), uni = Double(max(1, all.union(baseline!).count))
                                out["reason"] = inter / uni < 0.35 ? "content-changed" : "scrolled-off"
                            }
                        } else { errNote = "capture failed (Screen Recording permission?)" }
                    }
                    if let r = found {
                        missRun = 0; lastBox = r
                        if method == "ax" { axHits += 1 } else { ocrHits += 1 }
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
        writeJSONAtomic(["visible": false, "reason": "tracker-exit", "seq": lastSeq, "focus_pid": lastPid, "ts": now()], trackPath)
        let wall = max(0.001, now() - t0), active = frames
        let hits = axHits + ocrHits
        var st: [String: Any] = ["ts": (now() * 1000).rounded() / 1000, "event": "exit", "reason": exitReason, "focus_pid": lastPid, "window_id": Int(wid), "wall_s": (wall * 10).rounded() / 10,
                                 "frames": active, "fps": (Double(active) / max(0.001, activeS) * 100).rounded() / 100, "active_s": (activeS * 10).rounded() / 10,
                                 "ax_hits": axHits, "ocr_hits": ocrHits, "misses": misses, "hidden_frames": hidden,
                                 "refind_hit_rate": hits + misses > 0 ? (Double(hits) / Double(hits + misses) * 100).rounded() / 100 : NSNull(),
                                 "ocr_ms_avg": ocrN > 0 ? (ocrMs / Double(ocrN)).rounded() : NSNull(),
                                 "capture_ms_avg": ocrN > 0 ? (capMs / Double(ocrN)).rounded() : NSNull(),
                                 "cpu_s": ((cpu_s() - cpu0) * 100).rounded() / 100, "cpu_pct": ((cpu_s() - cpu0) / wall * 1000).rounded() / 10,
                                 "rss_mb_peak": maxRss.rounded(), "ocr_level": accurate ? "accurate" : "fast"]
        if let e = errNote { st["error"] = e }
        appendLine(st, home + "/logs/tracker.jsonl")
    }
}
