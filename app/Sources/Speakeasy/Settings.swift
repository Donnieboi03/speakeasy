// Settings (design 6.5): hotkeys, the bar, the highlight and the Reading log's document names. The group being edited
// carries the margin tick.
import AppKit
import SwiftUI
import KeyboardShortcuts

struct SettingsView: View {
    @State var barPos: BarPos
    var onBarPos: (BarPos) -> Void = { _ in }
    var onFullscreen: () -> Void = {}
    @State var hideFullscreen = Config.on("SPEAKEASY_BAR_HIDE_FULLSCREEN")
    @State var textBand = Config.on("SPEAKEASY_TEXT_HIGHLIGHT")
    @State var figureCues = Config.on("SPEAKEASY_FIGURE_CUES")
    @State var docNames = Config.on("SPEAKEASY_DOC_NAMES")
    @State var active = "Hotkeys"
    var recorders = true                                               // snapshots draw the current keys as text
    @Environment(\.colorScheme) var scheme

    var body: some View {
        let p = Palette(scheme)
        Surface(width: 420, height: 620) {
            Text("Settings").type(.title).padding(.bottom, Space.s16)
            VStack(alignment: .leading, spacing: Space.s12) {
                group("Hotkeys", p) {
                    keyRow("Read selection", .readSelection); keyRow("Read area", .readArea); keyRow("Show or hide bar", .toggleBar)
                    keyRow("Pause or resume", .pause); keyRow("Next sentence", .nextSentence); keyRow("Wrong spot", .wrongSpot)
                    Text("Pause, next and wrong spot work only while a read runs.").type(.label).foregroundStyle(p.slate)
                }
                Hairline()
                group("Bar", p) {
                    HStack(spacing: Space.s16) {
                        ForEach([(BarPos.centre, "Bottom centre"), (.left, "Bottom left"), (.right, "Bottom right")], id: \.0) { pos, title in
                            TokChoice(title: title, on: barPos == pos) { barPos = pos; onBarPos(pos) }
                        }
                    }
                    check("Hide the idle bar in full-screen apps", $hideFullscreen, "SPEAKEASY_BAR_HIDE_FULLSCREEN") { onFullscreen() }
                    Text("Screen sharing apps can see the bar. Hide it with ⌃⌥B.").type(.label).foregroundStyle(p.slate)
                }
                Hairline()
                group("Highlight", p) {
                    check("Band on the sentence being read", $textBand, "SPEAKEASY_TEXT_HIGHLIGHT")
                    check("Boxes and circles on figures", $figureCues, "SPEAKEASY_FIGURE_CUES")
                }
                Hairline()
                group("Reading log", p) {
                    check("Show document names", $docNames, "SPEAKEASY_DOC_NAMES") { Library.setNames(docNames) }
                    Text("Names and paths stay in library.json on this Mac, never in the logs. Turning this off deletes the file.")
                        .type(.label).foregroundStyle(p.slate).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
    func group<C: View>(_ title: String, _ p: Palette, @ViewBuilder _ c: () -> C) -> some View {
        let inner = VStack(alignment: .leading, spacing: Space.s4) { Text(title).type(.head).padding(.bottom, Space.s4); c() }
        return Ticked(color: p.mark, active: active == title) { inner }
        .snapFrame(active == title ? "dominant" : nil)
        .contentShape(Rectangle()).onHover { if $0 { active = title } }
    }
    func keyRow(_ title: String, _ name: KeyboardShortcuts.Name) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).type(.body).frame(width: 160, alignment: .leading)
            if recorders { KeyboardShortcuts.Recorder(for: name).controlSize(.small) }
            else { Text(KeyboardShortcuts.getShortcut(for: name)?.description ?? "").type(.data).padding(.horizontal, Space.s8).padding(.vertical, 2)
                .overlay(RoundedRectangle(cornerRadius: Radius.button).stroke(Palette(scheme).line, lineWidth: 1)) }
        }
    }
    func check(_ title: String, _ v: Binding<Bool>, _ key: String, _ after: @escaping () -> Void = {}) -> some View {
        Toggle(isOn: v) { Text(title).type(.body) }.toggleStyle(TokCheckboxStyle())
            .onChange(of: v.wrappedValue) { x in Config.set(key, x ? "on" : "off"); after() }
    }
}

/// library.json (contract A5.3) as the setting sees it: off deletes the file, on starts an empty one (mode 0600).
enum Library {
    static var path: String { Home.dir + "/library.json" }
    static func setNames(_ on: Bool) {
        let fm = FileManager.default
        if !on {
            if fm.fileExists(atPath: path) {
                do { try fm.removeItem(atPath: path) } catch { FileHandle.standardError.write("speakeasy: cannot delete library.json: \(error.localizedDescription)\n".data(using: .utf8)!) }
            }
            return
        }
        if !fm.fileExists(atPath: path) { _ = fm.createFile(atPath: path, contents: Data("{}".utf8), attributes: [.posixPermissions: 0o600]) }
    }
}
