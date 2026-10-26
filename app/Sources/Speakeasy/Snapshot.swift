// `Speakeasy --snapshot <surface>:<state> --out <png> [--appearance light|dark] [--fixture <stats.json>]` renders one
// surface offscreen at 2x and writes <png> plus <png>.json (the frames of the dominant element and its margin tick, points,
// top-left origin); `Speakeasy --self-check fonts` prints the registered PostScript names (contract C4). Neither touches
// ~/.speakeasy, the menu bar or the hotkeys.
import AppKit
import SwiftUI

struct SnapFrames: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) { value.merge(nextValue()) { a, _ in a } }
}
extension View {
    func snapFrame(_ name: String?) -> some View {
        background(GeometryReader { g in Color.clear.preference(key: SnapFrames.self, value: name.map { [$0: g.frame(in: .named("snap"))] } ?? [:]) })
    }
}

final class SnapFrameBox: ObservableObject { var frames: [String: CGRect] = [:] }

enum Snapshot {
    static let surfaces: [String: [String]] = [
        "bar": ["idle", "hover", "armed", "preparing", "reading", "paused", "error", "hidden"],
        "reading-log": ["data", "empty", "error"], "permissions": ["none", "one", "all"], "settings": ["default"], "cloud": ["off", "key", "nokey"]]

    static func run(_ args: [String]) -> Int32 {
        func arg(_ k: String) -> String? { args.firstIndex(of: k).flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil } }
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let missing = Fonts.register()
        if args.contains("--self-check") {
            guard arg("--self-check") == "fonts" else { fail("usage: Speakeasy --self-check fonts"); return 2 }
            if !missing.isEmpty { fail("missing font faces: " + missing.sorted().joined(separator: ", ")); return 1 }
            for n in [Fonts.serif, Fonts.sans, Fonts.mono] { print(NSFont(name: n, size: 12)?.fontName ?? n) }
            return 0
        }
        guard let spec = arg("--snapshot"), let out = arg("--out") else { fail("usage: Speakeasy --snapshot <surface>:<state> --out <png> [--appearance light|dark] [--fixture stats.json]"); return 2 }
        let parts = spec.split(separator: ":").map(String.init)
        guard parts.count == 2, surfaces[parts[0]]?.contains(parts[1]) == true else {
            fail("unknown surface:state \(spec); one of " + surfaces.sorted { $0.key < $1.key }.map { k, v in v.map { "\(k):\($0)" }.joined(separator: " ") }.joined(separator: " ")); return 2
        }
        let dark = arg("--appearance") == "dark"
        var fixture: StatsV2? = nil
        if let f = arg("--fixture") {
            do { fixture = try JSONDecoder().decode(StatsV2.self, from: Data(contentsOf: URL(fileURLWithPath: f))) }
            catch { fail("cannot read the fixture \(f): \(error.localizedDescription)"); return 2 }
        }
        let (view, size) = surface(parts[0], parts[1], fixture: fixture)
        return render(view, size: size, dark: dark, out: out, meta: ["surface": parts[0], "state": parts[1], "appearance": dark ? "dark" : "light"])
    }
    static func fail(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

    static func surface(_ name: String, _ state: String, fixture: StatsV2?) -> (AnyView, CGSize) {
        switch name {
        case "bar":
            let m = BarModel(); m.state = BarState(rawValue: state) ?? .idle
            m.label = "Figure 4"; m.sentI = 4; m.sentN = 12; m.startedAt = Date().addingTimeInterval(-2.1); m.tick = Date(); m.lastPlace = "p. 6, first figure"
            m.errorText = "Kokoro is not running. This read used the macOS voice."; m.errorAction = "Restart Kokoro"
            let s = m.state == .hidden ? BarModel.idleHit : BarModel.size(m.state)        // hidden: the empty rest panel
            return (AnyView(BarView(model: m, drag: false).frame(width: s.width, height: s.height)), s)
        case "reading-log":
            let r: Result<StatsV2, StatsError> = state == "error" ? .failure(StatsError(status: 1)) : .success(state == "empty" ? sampleStats(empty: true) : fixture ?? sampleStats(empty: false))
            let flags = state == "data" ? [FlagLog.Flag(ts: Date().timeIntervalSince1970 - 3600, cueKind: "figure_box")] : []
            return (AnyView(ReadingLogView(model: ReadingLogModel(result: r, window: 7, flags: flags))), CGSize(width: 640, height: 480))
        case "permissions":
            return (AnyView(OnboardingView(ax: state != "none", sr: state == "all", srAtLaunch: true, cloudOn: false, live: false)), CGSize(width: 480, height: 320))
        case "settings":
            return (AnyView(SettingsView(barPos: .centre, hideFullscreen: true, textBand: true, figureCues: true, docNames: true, recorders: false)), CGSize(width: 420, height: 620))
        default:
            let on = state != "off"
            return (AnyView(CloudView(llm: on, ground: on, model: Cloud.defaultModel, key: "", hasKey: state == "key",
                                      tradeoff: CloudTradeoff(cloudReads: 14, cloudMedian: 2.3, localMedian: 6.1))), CGSize(width: 480, height: 340))
        }
    }

    static func render(_ view: AnyView, size: CGSize, dark: Bool, out: String, meta: [String: Any]) -> Int32 {
        let box = SnapFrameBox()
        let root = view.environment(\.colorScheme, dark ? .dark : .light).coordinateSpace(name: "snap")
            .onPreferenceChange(SnapFrames.self) { box.frames = $0 }
        let host = NSHostingView(rootView: root)
        host.frame = NSRect(origin: .zero, size: size)
        let w = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        w.isOpaque = false; w.backgroundColor = .clear; w.hasShadow = false
        w.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        w.contentView = host
        host.layoutSubtreeIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(0.4))      // layout, preferences and AppKit-backed controls settle
        host.layoutSubtreeIfNeeded()
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * 2), pixelsHigh: Int(size.height * 2), bitsPerSample: 8,
                                         samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { fail("no bitmap"); return 1 }
        rep.size = size
        NSAppearance(named: dark ? .darkAqua : .aqua)?.performAsCurrentDrawingAppearance { host.cacheDisplay(in: host.bounds, to: rep) }
        guard let png = rep.representation(using: .png, properties: [:]) else { fail("no png"); return 1 }
        do {
            try png.write(to: URL(fileURLWithPath: out))
            var j = meta; j["size"] = [size.width, size.height]; j["scale"] = 2
            j["frames"] = box.frames.mapValues { [$0.minX, $0.minY, $0.width, $0.height].map { ($0 * 10).rounded() / 10 } }
            j["fonts_missing"] = Fonts.missing.sorted()
            try JSONSerialization.data(withJSONObject: j, options: [.prettyPrinted, .sortedKeys]).write(to: URL(fileURLWithPath: out + ".json"))
        } catch { fail("cannot write \(out): \(error.localizedDescription)"); return 1 }
        return 0
    }

    static func sampleStats(empty: Bool) -> StatsV2 {
        let today = { () -> String in let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssZ"; return f.string(from: Date()) }()
        let docs: [StatsV2.Doc] = empty ? [] : [
            .init(doc: "4eb5eccca1cd", name: "Layout analysis for scientific PDFs", pages: [3, 4, 5, 6, 7], items: ["p4:fig1", "p5:fig1", "p5:tab1", "p6:fig2"], reads: 27,
                  last_ts: today, resume: .init(page: 5, fig_id: "p5:fig1", sentence: 8, of: 12)),
            .init(doc: "9c01d2b7aa40", name: nil, pages: [1, 2], items: [], reads: 2, last_ts: today, resume: .init(page: 2, fig_id: nil, sentence: 5, of: 5))]
        let trend = (0..<14).map { StatsV2.Day(date: String(format: "2026-10-%02d", 13 + $0), median: [4.8, 3.9, 5.2, 3.1, 2.9, 4.4, 6.0, 3.3, 3.8, 2.7, 4.1, 3.6, 3.0, 3.9][$0]) }
        return StatsV2(schema: 2, window_days: 7, reads: .init(today: empty ? 0 : 9, window: empty ? 0 : 27), listened_min: .init(today: empty ? 0 : 14, window: empty ? 0 : 19.9),
                       first_audio_s: .init(median: empty ? nil : 3.9, p90: empty ? nil : 16.5, n: empty ? 0 : 27,
                                            by_provider: empty ? [:] : ["local": .init(median: 6.1, n: 13), "openrouter": .init(median: 2.3, n: 14)], trend: empty ? [] : trend),
                       items: .init(figures: empty ? 0 : 4, tables: empty ? 0 : 1), completion: .init(finished: empty ? 0 : 20, stopped: empty ? 0 : 6, error: empty ? 0 : 1),
                       highlight: empty ? nil : .init(shows: 120, landed: 97, hidden: 9, flagged: 1, no_cue_sentences: 11, sentences: 160),
                       speech_wpm: empty ? nil : 175, documents: docs,
                       problems: empty ? [] : [.init(id: "kokoro-down", count: 1, last_ts: today, open: true)], since: empty ? nil : ["completion": "2026-10-27"])
    }
}
