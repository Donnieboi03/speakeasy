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

final class AppDelegate: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var onboarding: NSWindow?
    var settings: NSWindow?
    let reader = Reader()

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = NSImage(systemSymbolName: "text.bubble", accessibilityDescription: "Speakeasy")
        let m = NSMenu()
        m.addItem(withTitle: "Read selection", action: #selector(read), keyEquivalent: "").target = self
        m.addItem(withTitle: "Stop", action: #selector(stop), keyEquivalent: "").target = self
        m.addItem(.separator())
        m.addItem(withTitle: "Permissions…", action: #selector(showOnboarding), keyEquivalent: "").target = self
        m.addItem(withTitle: "Hotkey…", action: #selector(showSettings), keyEquivalent: "").target = self
        m.addItem(.separator())
        m.addItem(withTitle: "Quit Speakeasy", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = m
        KeyboardShortcuts.onKeyUp(for: .readSelection) { [weak self] in self?.reader.toggle() }
        if !Permissions.accessibility || !Permissions.screenRecording { showOnboarding() }   // shows a window; prompts only on click
    }
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
    let timer = Timer.publish(every: 1, on: .main, in: .common).autoconnect()
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Speakeasy reads the text you select aloud and highlights it as it reads. Everything runs on this Mac.").fixedSize(horizontal: false, vertical: true)
            row("Accessibility", "To read the selected text and where it is on screen.", ax) { Permissions.promptAccessibility(); Permissions.openAccessibilitySettings() }
            row("Screen Recording", "To see charts and figures, and to keep the highlight on the text.", sr) { Permissions.promptScreenRecording(); Permissions.openScreenRecordingSettings() }
            Text("Hotkey: Control-Option-S (change it from the menu bar icon).").font(.footnote).foregroundStyle(.secondary)
        }
        .padding(20).frame(width: 440)
        .onReceive(timer) { _ in ax = Permissions.accessibility; sr = Permissions.screenRecording }
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
