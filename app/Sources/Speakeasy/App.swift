// Speakeasy menu-bar app: global hotkey (KeyboardShortcuts), permissions onboarding, and the reader pipeline:
// selection (Accessibility) -> screenshot of its bounds -> Python backend (rewrite + speech) -> embedded tracker (native overlay).
// Status-bar pattern after open-wispr's StatusBarController (MIT, github.com/human37/open-wispr).
import AppKit
import SwiftUI
import KeyboardShortcuts

extension KeyboardShortcuts.Name {
    static let readSelection = Self("readSelection", default: .init(.s, modifiers: [.control, .option]))
}

@main
struct SpeakeasyMain {
    static func main() {
        let app = NSApplication.shared
        let d = AppDelegate()
        app.delegate = d
        app.setActivationPolicy(.accessory)          // menu-bar only
        app.run()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    var onboarding: NSWindow?
    var settings: NSWindow?
    var statsWin: NSWindow?
    let reader = Reader()
    let pill = Pill()
    let statsLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = NSImage(systemSymbolName: "text.bubble", accessibilityDescription: "Speakeasy")
        let m = NSMenu()
        m.delegate = self
        statsLine.isEnabled = false; m.addItem(statsLine)
        m.addItem(withTitle: "Stats…", action: #selector(showStats), keyEquivalent: "").target = self
        m.addItem(.separator())
        m.addItem(withTitle: "Read selection", action: #selector(read), keyEquivalent: "").target = self
        m.addItem(withTitle: "Stop", action: #selector(stop), keyEquivalent: "").target = self
        m.addItem(.separator())
        for (title, key) in [("Highlight text", "SPEAKEASY_TEXT_HIGHLIGHT"), ("Figure cues", "SPEAKEASY_FIGURE_CUES")] {
            let it = NSMenuItem(title: title, action: #selector(toggleCue(_:)), keyEquivalent: "")
            it.target = self; it.representedObject = key; it.state = Config.on(key) ? .on : .off; m.addItem(it)
        }
        m.addItem(.separator())
        m.addItem(withTitle: "Permissions…", action: #selector(showOnboarding), keyEquivalent: "").target = self
        m.addItem(withTitle: "Hotkey…", action: #selector(showSettings), keyEquivalent: "").target = self
        m.addItem(.separator())
        m.addItem(withTitle: "Check for Updates…", action: #selector(checkUpdates), keyEquivalent: "").target = self
        m.addItem(withTitle: "Quit Speakeasy", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = m
        handOverFromHammerspoon()
        reader.onState = { [weak self] s in DispatchQueue.main.async { self?.pill.set(s) } }
        pill.model.onStop = { [weak self] in self?.reader.stop() }
        KeyboardShortcuts.onKeyUp(for: .readSelection) { [weak self] in self?.reader.toggle() }
        if !Permissions.accessibility || !Permissions.screenRecording { showOnboarding() }   // shows a window; prompts only on click
    }
    @objc func toggleCue(_ it: NSMenuItem) {
        guard let key = it.representedObject as? String else { return }
        let on = !Config.on(key); Config.set(key, on ? "on" : "off"); it.state = on ? .on : .off
        pill.model.textHighlight = Config.on("SPEAKEASY_TEXT_HIGHLIGHT"); pill.model.figureCues = Config.on("SPEAKEASY_FIGURE_CUES")
    }
    @objc func showStats() {
        let w = statsWin ?? NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 320), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.title = "Speakeasy stats"; w.isReleasedWhenClosed = false
        w.contentView = NSHostingView(rootView: StatsView(s: StatsData.load(reader.backendPath)))
        if statsWin == nil { w.center() }; statsWin = w
        NSApp.activate(ignoringOtherApps: true); w.makeKeyAndOrderFront(nil)
    }
    /// The app owns the hotkey: tell an old Hammerspoon speakeasy block to let go (it deletes its hotkey when
    /// SPEAKEASY_HOTKEY=app) and reload it headlessly if the hs CLI is installed.
    func handOverFromHammerspoon() {
        guard Config.value("SPEAKEASY_HOTKEY") != "app" else { return }
        Config.set("SPEAKEASY_HOTKEY", "app")
        for hs in ["/opt/homebrew/bin/hs", "/usr/local/bin/hs"] where FileManager.default.isExecutableFile(atPath: hs) {
            _ = reader.run(hs, ["-t", "5", "-c", "hs.reload()"], stdin: nil); break
        }
    }
    func menuWillOpen(_ menu: NSMenu) {
        let b = reader.backendPath
        DispatchQueue.global().async { let s = StatsData.load(b); DispatchQueue.main.async { self.statsLine.title = s.menuLine } }
        for it in menu.items { if let k = it.representedObject as? String { it.state = Config.on(k) ? .on : .off } }
    }
    @objc func checkUpdates() { Updater.check() }
    @objc func read() { reader.toggle() }
    @objc func stop() { reader.stop() }
    @objc func showOnboarding() {
        if onboarding == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 300), styleMask: [.titled, .closable], backing: .buffered, defer: false)
            w.title = "Speakeasy permissions"; w.isReleasedWhenClosed = false
            w.contentView = NSHostingView(rootView: OnboardingView())
            w.center(); onboarding = w
        }
        NSApp.activate(ignoringOtherApps: true); onboarding?.makeKeyAndOrderFront(nil)
    }
    @objc func showSettings() {
        if settings == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 360, height: 120), styleMask: [.titled, .closable], backing: .buffered, defer: false)
            w.title = "Speakeasy hotkey"; w.isReleasedWhenClosed = false
            w.contentView = NSHostingView(rootView: Form { KeyboardShortcuts.Recorder("Read selection:", name: .readSelection) }.padding())
            w.center(); settings = w
        }
        NSApp.activate(ignoringOtherApps: true); settings?.makeKeyAndOrderFront(nil)
    }
}

struct OnboardingView: View {
    @State var ax = Permissions.accessibility
    @State var sr = Permissions.screenRecording
    let srAtLaunch = Permissions.screenRecordingAtLaunch
    let timer = Timer.publish(every: 1, on: .main, in: .common).autoconnect()
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Speakeasy reads the text you select aloud and highlights it as it reads. Everything runs on this Mac.").fixedSize(horizontal: false, vertical: true)
            row("Accessibility", "To read the selected text and where it is on screen.", ax) { Permissions.promptAccessibility(); Permissions.openAccessibilitySettings() }
            row("Screen Recording", "To see charts and figures, and to keep the highlight on the text.", sr) { Permissions.promptScreenRecording(); Permissions.openScreenRecordingSettings() }
            Text("Hotkey: Control-Option-S (change it from the menu bar icon).").font(.footnote).foregroundStyle(.secondary)
            if !srAtLaunch {
                // macOS applies a new Screen Recording grant only to a fresh process (the check can stay false until then)
                HStack { Text(sr ? "Screen Recording granted. Relaunch Speakeasy to use it." : "Already turned Screen Recording on? Relaunch to apply it.").font(.footnote); Spacer(); Button("Relaunch") { relaunch() } }
            }
        }
        .padding(20).frame(width: 440)
        .onReceive(timer) { _ in ax = Permissions.accessibility; sr = Permissions.screenRecording }
    }
    func relaunch() {
        let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", "sleep 0.5; /usr/bin/open \"$0\"", Bundle.main.bundlePath]
        try? p.run(); NSApp.terminate(nil)
    }
    func row(_ t: String, _ why: String, _ ok: Bool, _ grant: @escaping () -> Void) -> some View {
        HStack {
            Image(systemName: ok ? "checkmark.circle.fill" : "circle").foregroundStyle(ok ? .green : .secondary)
            VStack(alignment: .leading) { Text(t).bold(); Text(why).font(.footnote).foregroundStyle(.secondary) }
            Spacer()
            if !ok { Button("Grant…", action: grant) }
        }
    }
}

/// ~/.speakeasy/config KEY=VALUE lines (read by the backend on every run).
enum Config {
    static var path: String { NSHomeDirectory() + "/.speakeasy/config" }
    static func lines() -> [String] { ((try? String(contentsOfFile: path, encoding: .utf8)) ?? "").components(separatedBy: "\n") }
    static func value(_ key: String) -> String? {
        if let e = ProcessInfo.processInfo.environment[key], !e.isEmpty { return e }
        for l in lines() where l.trimmingCharacters(in: .whitespaces).hasPrefix(key + "=") {
            return l.split(separator: "=", maxSplits: 1).last.map { $0.trimmingCharacters(in: CharacterSet(charactersIn: " \"'")) }
        }
        return nil
    }
    static func on(_ key: String) -> Bool {
        for l in lines() where l.trimmingCharacters(in: .whitespaces).hasPrefix(key + "=") {
            let v = l.split(separator: "=", maxSplits: 1).last.map { $0.trimmingCharacters(in: CharacterSet(charactersIn: " \"'")).lowercased() } ?? "on"
            return !["off", "0", "false", "no"].contains(v)
        }
        return true
    }
    static func set(_ key: String, _ value: String) {
        var ls = lines().filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix(key + "=") }
        while ls.last == "" { ls.removeLast() }
        ls.append("\(key)=\(value)")
        try? (ls.joined(separator: "\n") + "\n").write(toFile: path, atomically: true, encoding: .utf8)
    }
}
