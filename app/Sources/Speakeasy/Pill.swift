// Floating status pill (after Wispr Flow / open-wispr's recording indicator): listening -> thinking -> speaking,
// a stop button, and a gear with the two highlight toggles. Never takes focus from the app being read.
import AppKit
import SwiftUI

final class PillModel: ObservableObject {
    @Published var state = "idle"                       // idle | listening | thinking | speaking
    @Published var textHighlight = Config.on("SPEAKEASY_TEXT_HIGHLIGHT")
    @Published var figureCues = Config.on("SPEAKEASY_FIGURE_CUES")
    var onStop: (() -> Void)?
    var onArea: (() -> Void)?
}

final class Pill {
    let model = PillModel()
    var panel: NSPanel?
    var poll: Timer?
    let statePath = NSHomeDirectory() + "/.speakeasy/state.json"

    func set(_ s: String) {
        if s == "idle" { model.state = "idle"; hide(); return }
        model.state = s; show()
        if s == "thinking" && poll == nil {             // the backend reports "speaking" in ~/.speakeasy/state.json
            poll = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in self?.readState() }
        }
    }
    func readState() {
        guard let d = FileManager.default.contents(atPath: statePath),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let s = j["state"] as? String else { return }
        if s == "speaking" && model.state == "thinking" { model.state = "speaking" }
    }
    func show() {
        if panel == nil {
            let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 250, height: 44),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.isFloatingPanel = true; p.level = .statusBar; p.backgroundColor = .clear; p.isOpaque = false; p.hasShadow = true
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            p.isMovableByWindowBackground = true; p.hidesOnDeactivate = false
            p.contentView = NSHostingView(rootView: PillView(model: model))
            if let s = NSScreen.main?.visibleFrame { p.setFrameOrigin(NSPoint(x: s.midX - 125, y: s.minY + 24)) }
            panel = p
        }
        panel?.orderFrontRegardless()
    }
    func hide() { poll?.invalidate(); poll = nil; panel?.orderOut(nil) }
}

struct PillView: View {
    @ObservedObject var model: PillModel
    @State var pulse = false
    var label: String { ["listening": "Listening", "thinking": "Thinking", "speaking": "Speaking"][model.state] ?? "" }
    var color: Color { model.state == "speaking" ? .green : model.state == "thinking" ? .orange : .blue }
    var body: some View {
        HStack(spacing: 10) {
            Circle().fill(color).frame(width: 9, height: 9)
                .scaleEffect(pulse ? 1.25 : 0.8).animation(.easeInOut(duration: 0.6).repeatForever(), value: pulse)
                .onAppear { pulse = true }
            if model.state == "speaking" { Bars() } else { Text(label).font(.system(size: 13, weight: .medium)) }
            Spacer(minLength: 4)
            Menu {
                Toggle("Highlight text", isOn: Binding(get: { model.textHighlight }, set: { model.textHighlight = $0; Config.set("SPEAKEASY_TEXT_HIGHLIGHT", $0 ? "on" : "off") }))
                Toggle("Figure cues", isOn: Binding(get: { model.figureCues }, set: { model.figureCues = $0; Config.set("SPEAKEASY_FIGURE_CUES", $0 ? "on" : "off") }))
            } label: { Image(systemName: "gearshape") }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().help("Highlight settings")
            Button { model.onArea?() } label: { Image(systemName: "viewfinder") }
                .buttonStyle(.borderless).help("Read an area (Control-Option-A)")
            Button { model.onStop?() } label: { Image(systemName: "stop.fill") }
                .buttonStyle(.borderless).help("Stop reading")
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 14).frame(width: 250, height: 44)
        .background(Capsule().fill(Color.black.opacity(0.82)))
    }
}

/// Simple animated level bars while speaking (no audio tap; purely a "speaking" cue).
struct Bars: View {
    @State var t = 0.0
    let timer = Timer.publish(every: 0.12, on: .main, in: .common).autoconnect()
    var body: some View {
        HStack(spacing: 3) {
            ForEach(0..<7) { i in
                Capsule().frame(width: 3, height: 6 + 12 * abs(sin(t + Double(i) * 0.9)))
            }
        }
        .frame(height: 20).onReceive(timer) { _ in withAnimation(.linear(duration: 0.12)) { t += 0.5 } }
    }
}
