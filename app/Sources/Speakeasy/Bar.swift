// The bar (contract B, design 6.1): one non-activating panel at the bottom centre of the display, created once and kept
// (VoiceInk #572: a panel rebuilt per trigger shows 250 ms late). Behaviour modelled on Wispr Flow's Flow Bar; the panel
// pattern is the earlier pill's, adapted from open-wispr (MIT). It never takes focus from the document (B4).
import AppKit
import SwiftUI
import KeyboardShortcuts

enum BarState: String { case idle, hover, armed, preparing, reading, paused, error, hidden }
enum BarPos: String, CaseIterable { case centre, left, right }

final class BarModel: ObservableObject {
    @Published var state: BarState = .idle
    @Published var label = ""                      // "Figure 4" while preparing / reading (live only)
    @Published var sentI = 0
    @Published var sentN: Int? = nil
    @Published var startedAt = Date()
    @Published var tick = Date()                   // drives the elapsed counter
    @Published var cloud = false
    @Published var errorText = ""
    @Published var errorAction: String? = nil
    @Published var errorTick = false               // idle after an error: a brick tick until hovered
    @Published var flagged = false
    @Published var lastPlace = ""                  // "p. 6, first figure" from the Reading log
    var onArea: () -> Void = {}, onSelection: () -> Void = {}, onStop: () -> Void = {}
    var onPause: () -> Void = {}, onNext: () -> Void = {}, onFlag: () -> Void = {}, onErrorAction: () -> Void = {}
    var onBarClick: () -> Void = {}

    static func size(_ s: BarState) -> CGSize {
        switch s {
        case .idle, .hidden: return CGSize(width: 56, height: 8)
        case .hover: return CGSize(width: 400, height: 36)
        case .armed, .preparing: return CGSize(width: 300, height: 36)
        case .reading, .paused: return CGSize(width: 360, height: 36)
        case .error: return CGSize(width: 440, height: 36)
        }
    }
    static let idleHit = CGSize(width: 72, height: 24)
}

final class BarPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class BarHostingView: NSHostingView<BarView> {
    var onPointer: (Bool) -> Void = { _ in }
    var contextMenu: () -> NSMenu? = { nil }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.filter { $0.userInfo?["bar"] != nil }.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self, userInfo: ["bar": true]))
    }
    override func mouseEntered(with e: NSEvent) { super.mouseEntered(with: e); onPointer(true) }
    override func mouseExited(with e: NSEvent) { super.mouseExited(with: e); onPointer(false) }
    override func rightMouseDown(with e: NSEvent) {
        if let m = contextMenu() { NSMenu.popUpContextMenu(m, with: e, for: self) } else { super.rightMouseDown(with: e) }
    }
}

/// Owns the panel, its placement (B1), states (B2), hiding (B5) and the live-state poll.
final class Bar: NSObject {
    let model = BarModel()
    let panel: BarPanel
    let host: BarHostingView
    var pos: BarPos = .centre
    var dodge: BarPos? = nil                       // collision: the corner the bar slid to
    var clearSince: Date? = nil
    var hiddenUntil: Date? = nil, hiddenByKey = false, readHidden = false, fullscreenHidden = false
    var activeScreen: NSScreen? = nil              // the source window's display while a read runs
    var poll: Timer? = nil, idleTimer: Timer? = nil, errorTimer: Timer? = nil
    var hoverWork: DispatchWorkItem? = nil
    var dragStart: (mouse: NSPoint, origin: NSPoint)? = nil
    var spoke = false
    var externalRead = false                       // a read the app did not start (CLI), followed through state.json
    var stateMtime: Date? = nil, watch: Timer? = nil
    enum Occupied: Equatable { case stacked(BarPos, top: CGFloat), alt(BarPos) }     // stacked: over a bar-shaped occupant
    var occupied: Occupied? = nil                  // another app's floating window sits on the bar's spot
    var idleTicks = 0
    var settingOn: Bool { Config.on("SPEAKEASY_BAR") }
    var hideIdleInFullscreen: Bool { Config.on("SPEAKEASY_BAR_HIDE_FULLSCREEN") }
    var testPos: String? { ProcessInfo.processInfo.environment["SPEAKEASY_BAR_TEST_POS"] }

    override init() {
        panel = BarPanel(contentRect: NSRect(origin: .zero, size: BarModel.idleHit), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        host = BarHostingView(rootView: BarView(model: model))
        super.init()
        panel.isFloatingPanel = true; panel.level = .statusBar; panel.backgroundColor = .clear; panel.isOpaque = false; panel.hasShadow = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.hidesOnDeactivate = false; panel.becomesKeyOnlyIfNeeded = true; panel.isMovable = false
        panel.ignoresMouseEvents = false                // set explicitly: the whole hit area takes clicks, clear pixels too
        panel.sharingType = .none                       // honoured by older capture paths only (B5.1)
        panel.animationBehavior = .none
        panel.contentView = host
        host.onPointer = { [weak self] inside in self?.pointer(inside) }
        host.contextMenu = { [weak self] in self?.restMenu() }
        model.onBarClick = { [weak self] in self?.model.onArea() }
        pos = savedPos()
        idleTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.idleCheck() }
        watch = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.watchState() }
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            guard let s = self else { return }
            if let a = s.activeScreen, !NSScreen.screens.contains(a) { s.activeScreen = nil }    // display removed while reading
            s.pos = s.savedPos(); s.place(reason: "display"); s.checkOccupied()
        }
        set(.idle)
        checkOccupied()
    }

    // ---- states ----
    /// At rest the bar is `hidden` while the setting is off, ⌃⌥B or "Hide for 1 hour" holds, or a full-screen app is in front.
    var restHidden: Bool { !settingOn || hiddenByKey || hiddenUntil != nil || (fullscreenHidden && hideIdleInFullscreen) }
    func set(_ wanted: BarState) {
        let s: BarState = (wanted == .idle || wanted == .hover) && restHidden ? .hidden : wanted
        let from = model.state
        if from != s { AppLog.event("bar_state", ["from": from.rawValue, "to": s.rawValue]) }
        model.state = s
        if (s == .reading && from != .reading && from != .paused) || (from == .hidden && s != .hidden) { checkOccupied() }
        if s != .error { errorTimer?.invalidate(); errorTimer = nil }
        let active = [.armed, .preparing, .reading, .paused, .error].contains(s)
        if [.preparing, .reading, .paused].contains(s) { startPoll() } else { stopPoll() }
        KeyboardShortcuts.disable(.cancelArea, .pause, .nextSentence, .wrongSpot)
        if s == .armed { KeyboardShortcuts.enable(.cancelArea) }
        if [.preparing, .reading, .paused].contains(s) { KeyboardShortcuts.enable(.pause, .nextSentence, .wrongSpot) }
        if !active { activeScreen = nil; dodge = nil }
        panel.level = s == .armed ? NSWindow.Level(rawValue: NSWindow.Level.screenSaver.rawValue + 1) : .statusBar
        place(reason: nil)
    }
    var visibleNow: Bool {
        switch model.state {
        case .hidden: return false
        case .armed, .preparing, .reading, .paused, .error: return settingOn && !readHidden     // a read shows a hidden bar for that read
        case .idle, .hover: return true
        }
    }
    /// Re-evaluates the rest state after a hide or show: idle <-> hidden.
    func refreshRest() {
        if [.idle, .hover].contains(model.state) && restHidden { set(.hidden) }
        else if model.state == .hidden && !restHidden { set(.idle) }
        else { place(reason: nil) }
    }
    func readStarted(screen: NSScreen?, external: Bool = false) {
        activeScreen = screen; spoke = false; readHidden = false; externalRead = external
        model.startedAt = Date(); model.label = ""; model.sentI = 0; model.sentN = nil; model.errorTick = false; model.flagged = false
        model.cloud = Cloud.anyOn
        set(.preparing)
    }
    func readEnded(problem: (text: String, action: String?)?) {
        if let p = problem { showError(p.text, action: p.action) }
        else if !spoke && model.state == .preparing { showError("Nothing was read. The selection was empty.", action: nil) }
        else { set(.idle) }
    }
    func showError(_ text: String, action: String?) {
        model.errorText = text; model.errorAction = action; set(.error)
        errorTimer = Timer.scheduledTimer(withTimeInterval: 8, repeats: false) { [weak self] _ in
            guard let s = self, s.model.state == .error else { return }
            s.model.errorTick = true; s.set(.idle)
        }
    }
    func flag() {
        model.flagged = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.model.flagged = false }
    }

    // ---- pointer ----
    func pointer(_ inside: Bool) {
        hoverWork?.cancel()
        let w = DispatchWorkItem { [weak self] in
            guard let s = self else { return }
            if inside && s.model.state == .idle { s.model.errorTick = false; s.set(.hover) }
            if !inside && s.model.state == .hover { s.set(.idle) }
        }
        hoverWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + (inside ? 0.12 : 0.3), execute: w)
    }
    func dragChanged() {
        let m = NSEvent.mouseLocation
        if dragStart == nil { dragStart = (m, panel.frame.origin) }
        guard let d = dragStart, hypot(m.x - d.mouse.x, m.y - d.mouse.y) > 4 else { return }
        panel.setFrameOrigin(NSPoint(x: d.origin.x + m.x - d.mouse.x, y: d.origin.y + m.y - d.mouse.y))
    }
    func dragEnded() {
        let m = NSEvent.mouseLocation
        defer { dragStart = nil }
        guard let d = dragStart, hypot(m.x - d.mouse.x, m.y - d.mouse.y) > 4 else { model.onBarClick(); return }   // a click, not a move
        let vf = screen().visibleFrame, mid = panel.frame.midX
        pos = BarPos.allCases.min { abs(homeX($0, vf, panel.frame.width) + panel.frame.width / 2 - mid) < abs(homeX($1, vf, panel.frame.width) + panel.frame.width / 2 - mid) } ?? .centre
        UserDefaults.standard.set(pos.rawValue, forKey: arrangementKey())
        place(reason: "drag")
    }
    func restMenu() -> NSMenu? {
        guard [.idle, .hover].contains(model.state) else { return nil }
        let m = NSMenu()
        for (t, a) in [("Hide for 1 hour", #selector(hideHour)), ("Hide until ⌃⌥B", #selector(hideUntilKey)), ("Settings…", #selector(openSettings))] {
            m.addItem(withTitle: t, action: a, keyEquivalent: "").target = self
        }
        return m
    }
    @objc func hideHour() { hiddenUntil = Date().addingTimeInterval(3600); AppLog.event("bar_hide", ["reason": "hour"]); refreshRest() }
    @objc func hideUntilKey() { hiddenByKey = true; AppLog.event("bar_hide", ["reason": "hotkey"]); refreshRest() }
    @objc func openSettings() { (NSApp.delegate as? AppDelegate)?.showSettings() }
    func toggleHidden() {
        if [.idle, .hover, .hidden].contains(model.state) { hiddenByKey = !(hiddenByKey || hiddenUntil != nil) } else { readHidden.toggle(); hiddenByKey = readHidden }
        hiddenUntil = nil
        if hiddenByKey { AppLog.event("bar_hide", ["reason": "hotkey"]) }
        refreshRest()
    }

    // ---- placement (B1) ----
    func arrangementKey() -> String {
        "barPos." + NSScreen.screens.map { "\(Int($0.frame.minX)),\(Int($0.frame.minY)),\(Int($0.frame.width)),\(Int($0.frame.height))" }.joined(separator: ";")
    }
    func savedPos() -> BarPos { UserDefaults.standard.string(forKey: arrangementKey()).flatMap(BarPos.init(rawValue:)) ?? .centre }
    func screen() -> NSScreen {
        if let a = activeScreen, NSScreen.screens.contains(a) { return a }
        let m = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(m, $0.frame, false) } ?? NSScreen.main ?? NSScreen.screens[0]
    }
    func homeX(_ p: BarPos, _ vf: NSRect, _ w: CGFloat) -> CGFloat {
        switch p { case .centre: return vf.midX - w / 2; case .left: return vf.minX + Space.s8; case .right: return vf.maxX - w - Space.s8 }
    }
    func frame(for p: BarPos) -> NSRect {
        if p == pos, let o = occupied {
            switch o {
            case .stacked(let at, let top): return frame(left: homeX(at, screen().visibleFrame, BarModel.size(model.state).width), bottom: top + Space.s8)
            case .alt(let alt): return homeFrame(alt)
            }
        }
        return homeFrame(p)
    }
    /// The spot frame(for: p) really places the bar at (the occupied home spot's stack or alternative).
    func placedPos(_ p: BarPos) -> BarPos {
        guard p == pos, let o = occupied else { return p }
        switch o { case .stacked(let at, _): return at; case .alt(let alt): return alt }
    }
    func homeFrame(_ p: BarPos) -> NSRect {
        let sc = screen(), vf = sc.visibleFrame
        let full = fullscreenHidden && model.state != .idle && model.state != .hover
        return frame(left: homeX(p, vf, BarModel.size(model.state).width), bottom: (full ? sc.frame.minY : vf.minY) + Space.s8)
    }
    /// The panel frame for a visible bar whose left and bottom edges are given (idle: the larger hit area around the stub).
    func frame(left x: CGFloat, bottom: CGFloat) -> NSRect {
        let size = BarModel.size(model.state)
        if model.state == .idle || model.state == .hidden {
            let hit = BarModel.idleHit
            return NSRect(x: x - (hit.width - size.width) / 2, y: bottom - (hit.height - size.height) / 2, width: hit.width, height: hit.height)
        }
        return NSRect(x: x, y: bottom, width: size.width, height: size.height)
    }
    /// The visible bar inside a panel frame (idle: the stub, not the hit area).
    func visibleRect(_ f: NSRect) -> NSRect {
        let size = BarModel.size(model.state)
        return NSRect(x: f.midX - size.width / 2, y: f.midY - size.height / 2, width: size.width, height: size.height)
    }

    // ---- another app's floating window on the bar's spot (e.g. a dictation bar at a high window level) ----
    /// Other apps' on-screen windows at or above the bar's level that overlap rect (AppKit coordinates).
    func occupants(_ rect: NSRect) -> [NSRect] {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return [] }
        let level = Int(NSWindow.Level.statusBar.rawValue), screenArea = screen().frame.width * screen().frame.height
        let ownNames: Set<String> = ["speakeasy-track", "SpeakeasyTracker"]   // the cue panel is ours (a helper process)
        return list.compactMap { w -> NSRect? in
            guard (w[kCGWindowOwnerPID as String] as? Int32) != getpid(), !ownNames.contains(w[kCGWindowOwnerName as String] as? String ?? ""),
                  (w[kCGWindowLayer as String] as? Int ?? 0) >= level, (w[kCGWindowAlpha as String] as? Double ?? 1) > 0.01,
                  let b = w[kCGWindowBounds as String] as? NSDictionary, let r = CGRect(dictionaryRepresentation: b as CFDictionary) else { return nil }
            let f = ScreenGeo.toNS(r)
            return f.width * f.height < 0.5 * screenArea && f.intersects(rect) ? f : nil   // screen-wide overlays are not "a spot"
        }
    }
    /// B1 rule 8. The home spot (bottom centre; a remembered manual position is used as-is) is occupied when another
    /// process's on-screen window at level >= statusBar overlaps the bar there. Over a bar-shaped occupant (at most 80 pt
    /// tall, bottom within 40 pt of the visible frame's bottom) the bar stacks 8 pt above it on the spot's horizontal
    /// centre; any other occupant blocks the spot. Spots are tried centre, bottom left, bottom right; all blocked: bottom
    /// right anyway. Logs bar_move `occupied` with a count when the result changes, never titles, apps or pids.
    func checkOccupied() {
        let before = occupied
        occupied = nil
        var hitsAtHome = 0
        if UserDefaults.standard.string(forKey: arrangementKey()) == nil {
            let vf = screen().visibleFrame, big = BarModel.size(.error)            // the widest state must fit when stacked
            var chosen: Occupied? = nil, found = false
            for spot in [BarPos.centre, .left, .right] {
                let hits = occupants(visibleRect(homeFrame(spot)))
                if spot == .centre { hitsAtHome = hits.count }
                if hits.isEmpty { chosen = spot == pos ? nil : .alt(spot); found = true; break }
                guard hits.allSatisfy({ $0.height <= 80 && abs($0.minY - vf.minY) <= 40 }), let top = hits.map(\.maxY).max() else { continue }
                let up = NSRect(x: homeX(spot, vf, big.width), y: top + Space.s8, width: big.width, height: big.height)
                if vf.contains(up) && occupants(up).isEmpty { chosen = .stacked(spot, top: top); found = true; break }
            }
            occupied = found ? chosen : .alt(.right)       // all three blocked: bottom right anyway
        }
        if occupied != before {
            var f: [String: Any] = ["reason": "occupied", "pos": pos.rawValue, "occupants": hitsAtHome, "stacked": false]
            switch occupied { case .stacked(let at, _)?: f["pos"] = at.rawValue; f["stacked"] = true; case .alt(let a)?: f["pos"] = a.rawValue; case nil: break }
            AppLog.event("bar_move", f)
            place(reason: nil)
        }
    }
    func place(reason: String?) {
        guard visibleNow else { if panel.isVisible { panel.orderOut(nil) }; return }
        var f = frame(for: dodge ?? pos)
        if testPos == "selection", let r = testRect { f = NSRect(x: r.midX - f.width / 2, y: r.midY - f.height / 2, width: f.width, height: f.height) }
        let animate = reason == "collision" && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        if animate {
            NSAnimationContext.runAnimationGroup { c in c.duration = Motion.slide; c.timingFunction = CAMediaTimingFunction(name: .easeOut); panel.animator().setFrame(f, display: true) }
        } else { panel.setFrame(f, display: true) }
        if !panel.isVisible { panel.orderFrontRegardless() }
        if let r = reason { AppLog.event("bar_move", ["reason": r, "pos": placedPos(dodge ?? pos).rawValue]) }
    }
    var testRect: NSRect? = nil                    // SPEAKEASY_BAR_TEST_POS=selection (test builds): the selection's frame

    func idleCheck() {
        if let u = hiddenUntil, u < Date() { hiddenUntil = nil; refreshRest() }
        idleTicks += 1
        if model.state == .idle && idleTicks % 5 == 0 { checkOccupied() }        // rule 8: every 5 s while idle
        let fs = frontmostIsFullscreen()
        if fs != fullscreenHidden {
            fullscreenHidden = fs
            if fs && hideIdleInFullscreen && [.idle, .hover].contains(model.state) { AppLog.event("bar_hide", ["reason": "fullscreen"]) }
            if [.idle, .hover, .hidden].contains(model.state) { refreshRest() } else { place(reason: "fullscreen") }
        }
        if [.idle, .hover].contains(model.state), panel.isVisible, !NSMouseInRect(NSPoint(x: panel.frame.midX, y: panel.frame.midY), screen().frame, false) {
            place(reason: "display")                   // idle: the bar follows the pointer's display
        }
    }
    /// The frontmost app's window covers its whole display (a full-screen Space).
    func frontmostIsFullscreen() -> Bool {
        guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier, pid != getpid(),
              let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return false }
        let screens = NSScreen.screens.map { ScreenGeo.toCG($0.frame) }
        return list.contains { w in
            guard (w[kCGWindowOwnerPID as String] as? Int32) == pid, (w[kCGWindowLayer as String] as? Int) == 0,
                  let b = w[kCGWindowBounds as String] as? NSDictionary, let r = CGRect(dictionaryRepresentation: b as CFDictionary) else { return false }
            return screens.contains { abs($0.minX - r.minX) < 1 && abs($0.minY - r.minY) < 1 && abs($0.width - r.width) < 1 && abs($0.height - r.height) < 1 }
        }
    }

    // ---- collision with the cue or the area being dragged (B1 rules 1 and 2) ----
    func avoid(_ cueCG: CGRect?) {
        guard panel.isVisible, let cue = cueCG else { return }          // stale or absent: rules off, the bar stays where it is
        let c = ScreenGeo.toNS(cue), home = frame(for: pos)
        if home.insetBy(dx: -24, dy: -24).intersects(c) {
            clearSince = nil
            let vf = screen().visibleFrame
            let far: BarPos = c.midX - vf.minX > vf.maxX - c.midX ? .left : .right
            if dodge != far && far != pos { dodge = far; place(reason: "collision") }
        } else if dodge != nil {
            clearSince = clearSince ?? Date()
            if Date().timeIntervalSince(clearSince!) >= 1.5 { dodge = nil; clearSince = nil; place(reason: "collision") }
        }
    }

    // ---- reads the app did not start (B2: the bar follows state.json, whoever wrote it) ----
    func watchState() {
        guard [.idle, .hover, .hidden, .error].contains(model.state) else { return }
        let path = Home.dir + "/state.json"
        guard let m = (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date, m != stateMtime else { return }
        stateMtime = m
        guard let j = readJSONFile(path), let st = j["state"] as? String, st == "thinking" || st == "speaking", readAlive(j) else { return }
        readStarted(screen: nil, external: true)
        readLive()
    }
    /// The backend behind state.json is still running: speak.pid alive, or the file was written in the last 3 s.
    func readAlive(_ j: [String: Any]) -> Bool {
        if let s = try? String(contentsOfFile: Home.dir + "/speak.pid", encoding: .utf8), let p = pid_t(s.trimmingCharacters(in: .whitespacesAndNewlines)),
           p > 0, kill(p, 0) == 0 { return true }
        return Date().timeIntervalSince1970 - (j["t"] as? Double ?? 0) < 3
    }

    // ---- live state (state.json, cue.json) ----
    func startPoll() {
        guard poll == nil else { return }
        poll = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in self?.readLive() }
    }
    func stopPoll() { poll?.invalidate(); poll = nil }
    func readLive() {
        model.tick = Date()
        if let j = readJSONFile(Home.dir + "/state.json") {
            if externalRead, (j["state"] as? String == "idle" || !readAlive(j)) {
                externalRead = false
                let id = j["run_id"] as? String ?? ""
                (NSApp.delegate as? AppDelegate)?.reader.onFinished(id, true); return
            }
            if let s = j["state"] as? String, s == "speaking" {
                spoke = true
                let paused = j["paused"] as? Bool ?? false
                if model.state == .preparing || model.state == .reading || model.state == .paused { if model.state != (paused ? .paused : .reading) { set(paused ? .paused : .reading) } }
            }
            model.label = j["label"] as? String ?? model.label
            model.sentI = j["sent_i"] as? Int ?? model.sentI
            model.sentN = j["sent_n"] as? Int ?? model.sentN
            if let c = j["cloud"] as? Bool { model.cloud = c }
            if let ms = j["started_ms"] as? Double { model.startedAt = Date(timeIntervalSince1970: ms / 1000) }
            AppLog.runId = j["run_id"] as? String
        }
        if let c = readJSONFile(Home.dir + "/cue.json"), let ts = (c["ts"] as? Double) ?? Bar.mtime(Home.dir + "/cue.json"),
           Date().timeIntervalSince1970 - ts <= 1,
           let r = c["rect"] as? [Double], r.count == 4 {
            avoid(CGRect(x: r[0], y: r[1], width: r[2], height: r[3]))
        } else { avoid(nil) }
    }
}

extension Bar {
    /// A file's modification time (epoch seconds): cue.json freshness when the writer left out `ts`.
    static func mtime(_ path: String) -> Double? {
        ((try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date)?.timeIntervalSince1970
    }
}

func readJSONFile(_ path: String) -> [String: Any]? {
    guard let d = FileManager.default.contents(atPath: path) else { return nil }
    return (try? JSONSerialization.jsonObject(with: d)) as? [String: Any]
}

// ---------------- the view ----------------
struct BarView: View {
    @ObservedObject var model: BarModel
    var drag: Bool = true
    var body: some View {
        let size = BarModel.size(model.state)
        ZStack {
            Color.clear.contentShape(Rectangle())
            if model.state != .hidden { surface.frame(width: size.width, height: size.height).snapFrame("dominant") }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .gesture(DragGesture(minimumDistance: 0, coordinateSpace: .global)
                    .onChanged { _ in guard drag else { return }; (NSApp.delegate as? AppDelegate)?.bar.dragChanged() }
                    .onEnded { _ in guard drag else { return }; (NSApp.delegate as? AppDelegate)?.bar.dragEnded() })
    }
    var tickColor: Color { model.state == .error || model.errorTick ? rgb(Hex.brick) : rgb(Hex.ice) }
    @ViewBuilder var surface: some View {
        let size = BarModel.size(model.state)
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: Radius.panel).fill(rgb(Hex.navy)).frame(width: size.width, height: size.height)
            RoundedRectangle(cornerRadius: Radius.panel).strokeBorder(rgb(Hex.navyLine), lineWidth: 1)
            // the margin tick IS the bar's left edge (design 4), clipped by the bar's corners
            Rectangle().fill(tickColor).frame(width: tickWidth, height: size.height).snapFrame("tick")
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .clipShape(RoundedRectangle(cornerRadius: Radius.panel))
            if size.height > 8 {                          // a fixed content width: a wider label never shifts the bar or its tick
                content.frame(width: size.width - Space.s16 - Space.s8, alignment: .leading).padding(.leading, Space.s16)
            }
        }
    }
    var mist: Color { rgb(Hex.mist) }
    var ice: Color { rgb(Hex.ice) }
    func hint(_ n: KeyboardShortcuts.Name) -> String { KeyboardShortcuts.getShortcut(for: n)?.description ?? "" }
    @ViewBuilder var content: some View {
        switch model.state {
        case .hover:
            HStack(spacing: Space.s8) {
                Button { model.onArea() } label: { Text("Read area ").font(TType.label.font) + Text(hint(.readArea)).font(TType.labelMono.font) }
                Text("·").foregroundStyle(ice)
                Button { model.onSelection() } label: { Text("Read selection ").font(TType.label.font) + Text(hint(.readSelection)).font(TType.labelMono.font) }
                Spacer(minLength: Space.s8)
                if !model.lastPlace.isEmpty { Text(model.lastPlace).type(.labelMono).foregroundStyle(ice).lineLimit(1) }
            }.buttonStyle(BarButtonStyle())
        case .armed:
            HStack {
                Text("Drag over what to read").type(.body).foregroundStyle(mist)
                Spacer()
                Text("Esc cancels").type(.label).foregroundStyle(ice)
            }
        case .preparing:
            HStack(spacing: Space.s8) {
                Text(model.label.isEmpty ? "Preparing" : "Preparing \(model.label)").type(.body).foregroundStyle(mist).lineLimit(1)
                Spacer()
                Text(String(format: "%.1f", max(0, model.tick.timeIntervalSince(model.startedAt)))).font(TType.data.font).foregroundColor(ice)
                    + Text(" s").font(TType.label.font).foregroundColor(ice)
                Spacer()
                Button("■ Stop") { model.onStop() }.buttonStyle(BarButtonStyle())
            }
        case .reading, .paused:
            HStack(spacing: Space.s8) {
                SentenceTicks(i: model.sentI, n: max(model.sentN ?? model.sentI, model.sentI), paused: model.state == .paused)
                Text("\(model.sentI) of \(model.sentN.map(String.init) ?? "…")").type(.labelMono).foregroundStyle(mist).fixedSize()
                if model.cloud { Text("Cloud").type(.label).foregroundStyle(ice) }
                Spacer(minLength: 0)
                if model.flagged { Text("Flagged").type(.label).foregroundStyle(ice) }
                Group {
                    Button(model.state == .paused ? "▶ Resume" : "❙❙ Pause") { model.onPause() }
                    Button("⏭ Next") { model.onNext() }
                    Button("■ Stop") { model.onStop() }
                    Button("⚑") { model.onFlag() }.help("Wrong spot: the highlight is on the wrong place")
                }.buttonStyle(BarButtonStyle())
            }
        case .error:
            HStack(spacing: Space.s8) {
                Text(model.errorText).type(.body).foregroundStyle(mist).lineLimit(1).minimumScaleFactor(0.85)
                Spacer(minLength: Space.s4)
                if let a = model.errorAction { Button(a) { model.onErrorAction() }.buttonStyle(BarButtonStyle()) }
            }
        case .idle, .hidden: EmptyView()
        }
    }
}

/// One tick per sentence: read ones ice, the current one ember (paused: an ice outline), the rest navy-line.
struct SentenceTicks: View {
    let i: Int, n: Int, paused: Bool
    var body: some View {
        let shown = min(n, 18)
        HStack(spacing: 2) {
            ForEach(0..<max(shown, 1), id: \.self) { k in
                let s = shown < n ? Int((Double(k) + 0.5) * Double(n) / Double(shown)) : k     // more sentences than ticks: proportional
                let cur = s == i - 1 || (shown < n && s < i && Int((Double(k + 1) + 0.5) * Double(n) / Double(shown)) >= i)
                RoundedRectangle(cornerRadius: 1)
                    .fill(cur ? (paused ? Color.clear : rgb(Hex.ember)) : s < i - 1 ? rgb(Hex.ice) : rgb(Hex.navyLine))
                    .overlay(RoundedRectangle(cornerRadius: 1).strokeBorder(cur && paused ? rgb(Hex.ice) : Color.clear, lineWidth: 1))
                    .frame(width: 3, height: 12)
            }
        }
    }
}

struct BarButtonStyle: ButtonStyle {
    func makeBody(configuration c: Configuration) -> some View {
        c.label.type(.label).foregroundStyle(rgb(Hex.mist)).lineLimit(1).fixedSize()
            .padding(.horizontal, Space.s4).padding(.vertical, 2)
            .background(RoundedRectangle(cornerRadius: Radius.chip).fill(c.isPressed ? rgb(Hex.navyLine) : Color.clear))
            .contentShape(Rectangle())
    }
}
