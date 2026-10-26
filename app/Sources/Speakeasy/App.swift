// Speakeasy menu-bar app: global hotkeys (KeyboardShortcuts), the bar, permissions onboarding, and the reader pipeline:
// selection (Accessibility) -> screenshot of its bounds -> Python backend (rewrite + speech) -> embedded tracker (native overlay).
// Status-bar pattern after open-wispr's StatusBarController (MIT, github.com/human37/open-wispr).
import AppKit
import SwiftUI
import KeyboardShortcuts

extension KeyboardShortcuts.Name {
    static let readSelection = Self("readSelection", default: .init(.s, modifiers: [.control, .option]))
    static let readArea = Self("readArea", default: .init(.a, modifiers: [.control, .option]))
    static let toggleBar = Self("toggleBar", default: .init(.b, modifiers: [.control, .option]))
    // temporary: registered only while armed (Esc) or while a read runs (the rest), so the keys reach the document otherwise
    static let cancelArea = Self("cancelArea", default: .init(.escape))
    static let pause = Self("pause", default: .init(.p, modifiers: [.control, .option]))
    static let nextSentence = Self("nextSentence", default: .init(.n, modifiers: [.control, .option]))
    static let wrongSpot = Self("wrongSpot", default: .init(.w, modifiers: [.control, .option]))
}

@main
struct SpeakeasyMain {
    static func main() {
        let args = Array(CommandLine.arguments.dropFirst())
        if args.contains("--snapshot") || args.contains("--self-check") { exit(Snapshot.run(args)) }
        let app = NSApplication.shared
        let d = AppDelegate()
        app.delegate = d
        app.setActivationPolicy(.accessory)          // menu-bar only
        app.run()
    }
}

/// $SPEAKEASY_HOME (tests, snapshots) or ~/.speakeasy.
enum Home {
    static let dir: String = {
        let e = ProcessInfo.processInfo.environment["SPEAKEASY_HOME"] ?? ""
        return e.isEmpty ? NSHomeDirectory() + "/.speakeasy" : (e as NSString).expandingTildeInPath
    }()
}

/// logs/app.jsonl (contract B7): every record has ts and event, run_id while a read is live; never titles, paths, text or app names.
enum AppLog {
    static var runId: String? = nil
    static let q = DispatchQueue(label: "speakeasy.applog")
    static func event(_ name: String, _ fields: [String: Any] = [:]) {
        var r = fields; r["event"] = name; r["ts"] = (Date().timeIntervalSince1970 * 1000).rounded() / 1000
        if let id = runId, r["run_id"] == nil { r["run_id"] = id }
        guard var d = try? JSONSerialization.data(withJSONObject: r, options: [.sortedKeys]) else { return }
        d.append(0x0a)
        q.async {
            let dir = Home.dir + "/logs", path = dir + "/app.jsonl"
            try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
            if let h = FileHandle(forWritingAtPath: path) { h.seekToEndOfFile(); h.write(d); h.closeFile() }
            else if !FileManager.default.createFile(atPath: path, contents: d) { FileHandle.standardError.write("speakeasy: cannot write \(path)\n".data(using: .utf8)!) }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    var windows: [String: NSWindow] = [:]
    let reader = Reader()
    lazy var bar = Bar()
    let problems = Problems()
    var stats: StatsV2? = nil
    var userStopped = false
    var errorPerform: () -> Void = {}
    let statsLine = NSMenuItem(title: "Today: 0 reads, 0 min", action: nil, keyEquivalent: "")
    let problemLine = NSMenuItem(title: "", action: #selector(showReadingLog), keyEquivalent: "")
    let stopItem = NSMenuItem(title: "Stop", action: #selector(stop), keyEquivalent: "")
    let permissionsItem = NSMenuItem(title: "Permissions…", action: #selector(showOnboarding), keyEquivalent: "")
    let cloudItem = NSMenuItem(title: "Cloud (OpenRouter)…", action: #selector(showCloud), keyEquivalent: "")
    let barItem = NSMenuItem(title: "Show bar", action: #selector(toggleBarSetting), keyEquivalent: "")

    func applicationDidFinishLaunching(_ n: Notification) {
        for face in Fonts.register().sorted() { AppLog.event("font_fallback", ["face": face]) }
        DispatchQueue.global().async { Reader.killStaleTrackers() }
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = StatusGlyph.image()
        item.button?.setAccessibilityLabel("Speakeasy")
        buildMenu()
        wireReader(); wireBar(); wireHotkeys()
        refreshStats()
        problems.checkLive(force: true) { [weak self] in self?.updateProblemLine() }
        if !Permissions.accessibility || !Permissions.screenRecording { showOnboarding() }   // shows a window; prompts only on click
    }

    // ---- menu (design 6.2: the one native surface) ----
    func buildMenu() {
        let m = NSMenu(); m.delegate = self
        statsLine.isEnabled = false; m.addItem(statsLine)
        problemLine.target = self; problemLine.isHidden = true; m.addItem(problemLine)
        m.addItem(.separator())
        let sel = NSMenuItem(title: "Read selection", action: #selector(read), keyEquivalent: ""); sel.target = self; m.addItem(sel)
        let area = NSMenuItem(title: "Read area", action: #selector(readArea), keyEquivalent: ""); area.target = self; m.addItem(area)
        stopItem.target = self; m.addItem(stopItem)
        m.addItem(.separator())
        for (title, key) in [("Highlight text", "SPEAKEASY_TEXT_HIGHLIGHT"), ("Figure cues", "SPEAKEASY_FIGURE_CUES")] {
            let it = NSMenuItem(title: title, action: #selector(toggleCue(_:)), keyEquivalent: "")
            it.target = self; it.representedObject = key; m.addItem(it)
        }
        barItem.target = self; m.addItem(barItem)
        MainActor.assumeIsolated { sel.setShortcut(for: .readSelection); area.setShortcut(for: .readArea); barItem.setShortcut(for: .toggleBar) }
        m.addItem(.separator())
        m.addItem(withTitle: "Reading log…", action: #selector(showReadingLog), keyEquivalent: "").target = self
        m.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: ",").target = self
        cloudItem.target = self; m.addItem(cloudItem)
        permissionsItem.target = self; m.addItem(permissionsItem)
        m.addItem(.separator())
        m.addItem(withTitle: "Check for updates…", action: #selector(checkUpdates), keyEquivalent: "").target = self
        m.addItem(withTitle: "Quit Speakeasy", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = m
    }
    func menuWillOpen(_ menu: NSMenu) {
        refreshStats()
        problems.checkLive(force: false) { [weak self] in self?.updateProblemLine() }
        for it in menu.items { if let k = it.representedObject as? String { it.state = Config.on(k) ? .on : .off } }
        barItem.state = Config.on("SPEAKEASY_BAR") && !bar.hiddenByKey ? .on : .off
        stopItem.isHidden = !reader.speaking
        permissionsItem.isHidden = Permissions.accessibility && Permissions.screenRecording
        cloudItem.title = "Cloud (OpenRouter)…  " + (Cloud.anyOn ? "On" : "Off")
        updateProblemLine()
    }
    func updateProblemLine() {
        guard let p = problems.open(stats).first else { problemLine.isHidden = true; return }
        problemLine.isHidden = false
        problemLine.attributedTitle = NSAttributedString(string: ProblemCopy.short(p.id), attributes: [.foregroundColor: nsRGB(Hex.brick)])
    }
    /// `speakeasy stats --json` off the main thread; the menu line keeps the previous value until the new one arrives (A6).
    func refreshStats(window: Int = 7, then: ((Result<StatsV2, StatsError>) -> Void)? = nil) {
        let b = reader.backendPath
        DispatchQueue.global().async {
            let r = StatsV2.load(b, window: window)
            DispatchQueue.main.async {
                if case .success(let s) = r, window == 7 {
                    self.stats = s
                    self.statsLine.title = "Today: \(s.reads.today) reads, \(Int(s.listened_min.today.rounded())) min"
                    self.bar.model.lastPlace = s.documents.first.map(ResumeText.short) ?? ""
                    self.updateProblemLine()
                }
                then?(r)
            }
        }
    }

    // ---- reader, bar, hotkeys ----
    func wireReader() {
        reader.onStarted = { [weak self] screen in self?.userStopped = false; self?.bar.readStarted(screen: screen) }
        reader.onArmed = { [weak self] up in
            guard let s = self else { return }
            if up { s.bar.activeScreen = s.bar.screen(); s.bar.set(.armed) } else if s.bar.model.state == .armed { s.bar.set(.idle) }
        }
        reader.area.onDrag = { [weak self] r in self?.bar.avoid(r) }
        reader.onFailed = { [weak self] text, action in self?.fail(text, action) }
        reader.onFinished = { [weak self] id, _ in
            guard let s = self else { return }
            AppLog.runId = nil
            let p = s.userStopped ? nil : RunProblem.of(runId: id)
            if let p = p, p.id != "empty" { AppLog.event("problem", ["id": p.id, "live": false]); s.errorPerform = { ProblemCopy.perform(p.id) } }
            s.bar.readEnded(problem: s.userStopped ? nil : p.map { ($0.text, ProblemCopy.action($0.id)) })
            if s.userStopped { s.bar.set(.idle) }
            s.refreshStats()
        }
    }
    func fail(_ text: String, _ action: String?) {
        errorPerform = action == "Open Screen Recording settings" ? { Permissions.openScreenRecordingSettings() } : {}
        bar.showError(text, action: action)
    }
    func wireBar() {
        let m = bar.model
        m.onArea = { [weak self] in self?.reader.startArea(via: "bar") }
        m.onBarClick = { [weak self] in
            guard let s = self else { return }
            #if DEBUG
            let before = NSWorkspace.shared.frontmostApplication?.processIdentifier
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { AppLog.event("focus_kept", ["ok": before == NSWorkspace.shared.frontmostApplication?.processIdentifier]) }
            #endif
            if [.idle, .hover, .armed].contains(s.bar.model.state) { s.reader.startArea(via: "bar") }
        }
        m.onSelection = { [weak self] in self?.reader.start() }
        m.onStop = { [weak self] in self?.stopRead(via: "bar") }
        m.onPause = { [weak self] in self?.pauseToggle(via: "bar") }
        m.onNext = { [weak self] in self?.reader.control("next", via: "bar") }
        m.onFlag = { [weak self] in self?.flag(via: "bar") }
        m.onErrorAction = { [weak self] in self?.errorPerform(); self?.bar.set(.idle) }
    }
    func wireHotkeys() {
        KeyboardShortcuts.onKeyUp(for: .readArea) { [weak self] in self?.reader.startArea(via: "hotkey") }
        KeyboardShortcuts.onKeyUp(for: .readSelection) { [weak self] in
            guard let s = self else { return }
            if s.reader.speaking || s.reader.backend != nil { s.stopRead(via: "hotkey") } else { s.reader.start() }
        }
        KeyboardShortcuts.onKeyUp(for: .toggleBar) { [weak self] in self?.bar.toggleHidden() }
        KeyboardShortcuts.onKeyDown(for: .cancelArea) { [weak self] in self?.reader.area.cancel("cancel-esc") }
        KeyboardShortcuts.onKeyUp(for: .pause) { [weak self] in self?.pauseToggle(via: "hotkey") }
        KeyboardShortcuts.onKeyUp(for: .nextSentence) { [weak self] in self?.reader.control("next", via: "hotkey") }
        KeyboardShortcuts.onKeyUp(for: .wrongSpot) { [weak self] in self?.flag(via: "hotkey") }
        KeyboardShortcuts.disable(.cancelArea, .pause, .nextSentence, .wrongSpot)
    }
    func stopRead(via: String) { userStopped = true; reader.stop(via: via); bar.set(.idle) }
    func pauseToggle(via: String) {
        reader.control(bar.model.state == .paused ? "resume" : "pause", via: via)
    }
    /// "Wrong spot" (B6): no backend call, only a user_flag with the live run_id, seq and cue_kind.
    func flag(via: String) {
        AppLog.event("control", ["action": "flag", "via": via])
        let j = readJSONFile(Home.dir + "/state.json") ?? [:]
        AppLog.event("user_flag", ["run_id": j["run_id"] ?? NSNull(), "seq": j["seq"] ?? NSNull(), "cue_kind": j["cue_kind"] ?? NSNull()])
        bar.flag()
    }
    @objc func toggleCue(_ it: NSMenuItem) {
        guard let key = it.representedObject as? String else { return }
        let on = !Config.on(key); Config.set(key, on ? "on" : "off"); it.state = on ? .on : .off
    }
    @objc func toggleBarSetting() {
        let on = !(Config.on("SPEAKEASY_BAR") && !bar.hiddenByKey)
        Config.set("SPEAKEASY_BAR", on ? "on" : "off"); bar.hiddenByKey = false; bar.hiddenUntil = nil
        if !on { AppLog.event("bar_hide", ["reason": "setting"]) }
        bar.refreshRest()
    }
    @objc func checkUpdates() { Updater.check() }
    @objc func read() { reader.toggle(via: "menu") }
    @objc func readArea() { reader.startArea(via: "menu") }
    @objc func stop() { stopRead(via: "menu") }

    // ---- windows (normal windows: they activate the app, opened only by an explicit choice, B4) ----
    func open<V: View>(_ key: String, _ title: String, _ size: NSSize, _ view: V) {
        let w = windows[key] ?? NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        if windows[key] == nil { w.center(); w.isReleasedWhenClosed = false }
        w.title = title
        w.contentView = NSHostingView(rootView: view)
        w.setContentSize(size)
        windows[key] = w
        NSApp.activate(ignoringOtherApps: true); w.makeKeyAndOrderFront(nil)
    }
    @objc func showReadingLog() {
        problems.checkLive(force: false) { [weak self] in self?.showReadingLogWindow(window: 7) }
    }
    func showReadingLogWindow(window: Int) {
        refreshStats(window: window) { [weak self] r in
            guard let s = self else { return }
            let model = ReadingLogModel(result: r, window: window, live: s.problems.liveFailing, flags: FlagLog.recent(days: window))
            s.open("log", "Reading log", NSSize(width: 640, height: 480),
                   ReadingLogView(model: model, onWindow: { [weak s] w in s?.showReadingLogWindow(window: w) },
                                  onAction: { id in ProblemCopy.perform(id) }, onOpenLogs: { NSWorkspace.shared.open(URL(fileURLWithPath: Home.dir + "/logs")) }))
        }
    }
    @objc func showCloud() {
        let s = stats
        open("cloud", "Cloud (OpenRouter)", NSSize(width: 480, height: 340),
             CloudView(tradeoff: CloudTradeoff(stats: s), onChange: { [weak self] in self?.bar.model.cloud = Cloud.anyOn }))
    }
    @objc func showOnboarding() {
        open("permissions", "Permissions", NSSize(width: 480, height: 320), OnboardingView())
    }
    @objc func showSettings() {
        open("settings", "Settings", NSSize(width: 420, height: 620), SettingsView(barPos: bar.pos, onBarPos: { [weak self] p in
            guard let b = self?.bar else { return }
            b.pos = p; UserDefaults.standard.set(p.rawValue, forKey: b.arrangementKey()); b.place(reason: "drag")
        }, onFullscreen: { [weak self] in self?.bar.refreshRest() }))
    }
}

/// The status item glyph: a 3 pt margin tick beside two short lines, a template image macOS tints (design 6.2).
enum StatusGlyph {
    static func image() -> NSImage {
        let img = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            nsRGB(Hex.ink).setFill()
            NSBezierPath(rect: NSRect(x: 3, y: 3, width: 3, height: 12)).fill()
            NSBezierPath(roundedRect: NSRect(x: 8, y: 10, width: 8, height: 2), xRadius: 1, yRadius: 1).fill()
            NSBezierPath(roundedRect: NSRect(x: 8, y: 6, width: 6, height: 2), xRadius: 1, yRadius: 1).fill()
            return true
        }
        img.isTemplate = true
        return img
    }
}

/// ~/.speakeasy/config KEY=VALUE lines (read by the backend on every run).
enum Config {
    static var path: String { Home.dir + "/config" }
    static func lines() -> [String] { ((try? String(contentsOfFile: path, encoding: .utf8)) ?? "").components(separatedBy: "\n") }
    static func value(_ key: String) -> String? {
        if let e = ProcessInfo.processInfo.environment[key], !e.isEmpty { return e }
        for l in lines() where l.trimmingCharacters(in: .whitespaces).hasPrefix(key + "=") {
            return l.split(separator: "=", maxSplits: 1).last.map { $0.trimmingCharacters(in: CharacterSet(charactersIn: " \"'")) }
        }
        return nil
    }
    static func on(_ key: String) -> Bool {
        guard let v = value(key)?.lowercased() else { return true }
        return !["off", "0", "false", "no"].contains(v)
    }
    static func set(_ key: String, _ value: String) {
        var ls = lines().filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix(key + "=") }
        while ls.last == "" { ls.removeLast() }
        ls.append("\(key)=\(value)")
        do { try (ls.joined(separator: "\n") + "\n").write(toFile: path, atomically: true, encoding: .utf8) }
        catch { FileHandle.standardError.write("speakeasy: cannot write \(path): \(error.localizedDescription)\n".data(using: .utf8)!) }
    }
}
