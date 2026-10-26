// Adapted from open-wispr (MIT, Copyright (c) 2026 human37, github.com/human37/open-wispr): Sources/OpenWisprLib/Permissions.swift.
// Speakeasy only checks silently at launch; the system prompts appear only when the user clicks a button in onboarding.
import AppKit
import SwiftUI
import ApplicationServices
import CoreGraphics

struct Permissions {
    static var accessibility: Bool { AXIsProcessTrusted() }
    static var screenRecording: Bool { CGPreflightScreenCaptureAccess() }     // polled every second by the onboarding window
    static let screenRecordingAtLaunch = CGPreflightScreenCaptureAccess()
    static func promptAccessibility() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary
        AXIsProcessTrustedWithOptions(options)
    }
    static func promptScreenRecording() { _ = CGRequestScreenCaptureAccess() }
    static func openAccessibilitySettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") { NSWorkspace.shared.open(url) }
    }
    static func openScreenRecordingSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") { NSWorkspace.shared.open(url) }
    }
}

/// Permissions (design 6.4): the first missing permission dominates; granted rows collapse to one line.
struct OnboardingView: View {
    @State var ax = Permissions.accessibility
    @State var sr = Permissions.screenRecording
    var srAtLaunch = Permissions.screenRecordingAtLaunch
    var cloudOn = Cloud.anyOn
    var live = true                                                    // snapshots render a fixed state
    let timer = Timer.publish(every: 1, on: .main, in: .common).autoconnect()
    @Environment(\.colorScheme) var scheme
    struct Row { let name: String, why: String, button: String, granted: Bool, open: () -> Void }
    var rows: [Row] {
        [Row(name: "Screen Recording", why: "Lets Speakeasy see figures and keep the highlight on the line while you scroll.", button: "Open Screen Recording settings",
             granted: sr, open: { Permissions.promptScreenRecording(); Permissions.openScreenRecordingSettings() }),
         Row(name: "Accessibility", why: "Lets Speakeasy read the selected text and find where it is on screen.", button: "Open Accessibility settings",
             granted: ax, open: { Permissions.promptAccessibility(); Permissions.openAccessibilitySettings() })]
    }
    var body: some View {
        let p = Palette(scheme), missing = rows.filter { !$0.granted }
        Surface(width: 480, height: 320) {
            Text(missing.isEmpty ? "Ready: press ⌃⌥S on any text" : missing.count == 2 ? "Two permissions, then press ⌃⌥S on any text" : "One permission, then press ⌃⌥S on any text")
                .type(.title).padding(.bottom, Space.s16)
            VStack(alignment: .leading, spacing: Space.s12) {
                ForEach(Array(rows.enumerated()), id: \.offset) { _, r in
                    if !r.granted && r.name == missing.first?.name {
                        Ticked(color: p.mark) {
                            VStack(alignment: .leading, spacing: Space.s8) {
                                Text(r.name).type(.head)
                                Text(r.why).type(.body).fixedSize(horizontal: false, vertical: true)
                                Button(r.button, action: r.open).buttonStyle(TokButtonStyle(prominent: true))
                                if r.name == "Screen Recording" && !srAtLaunch {
                                    Text("Turned it on already? macOS needs a restart of the app.").type(.label).foregroundStyle(p.slate)
                                    Button("Relaunch Speakeasy") { relaunch() }.buttonStyle(TokButtonStyle())
                                }
                            }
                        }.snapFrame("dominant")
                        Hairline()
                    } else if !r.granted {
                        HStack(alignment: .firstTextBaseline, spacing: Space.s8) {
                            Text(r.name).type(.bodyMedium); Text("Off").type(.label).foregroundStyle(p.slate)
                            Spacer(); Button(r.button, action: r.open).buttonStyle(TokButtonStyle())
                        }.padding(.leading, Space.s12)
                    } else {
                        HStack(alignment: .firstTextBaseline, spacing: Space.s8) {
                            Image(systemName: "checkmark").font(.system(size: 11, weight: .semibold)).foregroundStyle(p.mark).accessibilityLabel("Granted"); Text(r.name).type(.body); Text("On").type(.label).foregroundStyle(p.slate)
                        }.padding(.leading, Space.s12).snapFrame(missing.isEmpty && r.name == "Screen Recording" ? "dominant" : nil)
                    }
                }
            }
            Spacer()
            Text(cloudOn ? "Cloud is on: text you read goes to OpenRouter." : "Everything stays on this Mac.").type(.label).foregroundStyle(p.slate)
        }
        .onReceive(timer) { _ in if live { ax = Permissions.accessibility; sr = Permissions.screenRecording } }
    }
    func relaunch() {
        let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", "sleep 0.5; /usr/bin/open \"$0\"", Bundle.main.bundlePath]
        do { try p.run() } catch { FileHandle.standardError.write("speakeasy: relaunch failed: \(error.localizedDescription)\n".data(using: .utf8)!); return }
        NSApp.terminate(nil)
    }
}
