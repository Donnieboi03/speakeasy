// The Reading log (design 6.3) from `speakeasy stats --json` schema 2, the problems with their live checks (contract A2.1),
// and the per-read problem the bar shows when a read ends. Logs never hold text; this file reads enums, hashes and counts.
import AppKit
import SwiftUI

struct StatsV2: Codable {
    struct Pair: Codable { var today: Int; var window: Int }
    struct PairD: Codable { var today: Double; var window: Double }
    struct Median: Codable { var median: Double; var n: Int }
    struct Day: Codable { var date: String; var median: Double }
    struct FirstAudio: Codable { var median: Double?; var p90: Double?; var n: Int; var by_provider: [String: Median]; var trend: [Day] }
    struct Items: Codable { var figures: Int; var tables: Int }
    struct Completion: Codable { var finished: Int; var stopped: Int; var error: Int }
    struct Highlight: Codable { var shows: Int; var landed: Int; var hidden: Int; var flagged: Int; var no_cue_sentences: Int; var sentences: Int }
    struct Resume: Codable { var page: Int?; var fig_id: String?; var sentence: Int?; var of: Int? }
    struct Doc: Codable { var doc: String; var name: String?; var pages: [Int]; var items: [String]; var reads: Int; var last_ts: String?; var resume: Resume? }
    struct Problem: Codable { var id: String; var count: Int; var last_ts: String?; var open: Bool }
    var schema: Int
    var window_days: Int
    var reads: Pair
    var listened_min: PairD
    var first_audio_s: FirstAudio
    var items: Items
    var completion: Completion
    var highlight: Highlight?
    var speech_wpm: Int?
    var documents: [Doc]
    var problems: [Problem]
    var since: [String: String]?

    static func load(_ backend: String, window: Int) -> Result<StatsV2, StatsError> {
        let p = Process(), out = Pipe()
        p.executableURL = URL(fileURLWithPath: backend); p.arguments = ["stats", "--json", "--window", String(window)]
        p.standardOutput = out; p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return .failure(StatsError(status: -1)) }
        let d = out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
        guard p.terminationStatus == 0 else { return .failure(StatsError(status: p.terminationStatus)) }
        do { return .success(try JSONDecoder().decode(StatsV2.self, from: d)) }
        catch { return .failure(StatsError(status: p.terminationStatus, decode: true)) }
    }
}
struct StatsError: Error { var status: Int32; var decode = false }

// ---------------- problems (A2.1) ----------------
struct ProblemItem: Identifiable { var id: String; var count: Int; var lastTs: String?; var live: Bool }

enum ProblemCopy {
    static func sentence(_ p: ProblemItem) -> String {
        let n = p.count, reads = n == 1 ? "1 read" : "\(n) reads"
        switch p.id {
        case "kokoro-down": return n > 0 ? "Kokoro is not running. \(reads) used the macOS voice." : "Kokoro is not running. Reads use the macOS voice."
        case "model-down": return n > 0 ? "The local model did not answer. \(reads) \(n == 1 ? "was" : "were") read without a rewrite." : "The local model did not answer. Reads go without a rewrite."
        case "cloud-fallback": return "OpenRouter failed \(n == 1 ? "once" : "\(n) times"). Those reads used the local model."
        case "layout-missing": return "The PDF layout model is missing. Figures are found by rules only."
        case "highlight-failed": return "The highlight could not be drawn on \(reads)."
        case "permission": return Permissions.screenRecording ? "Accessibility is off. Reading a selection needs it." : "Screen Recording is off. Figures and the highlight need it."
        case "slow-start": return "\(reads) took over 10 s to start."
        default: return p.id
        }
    }
    static func short(_ id: String) -> String {
        ["kokoro-down": "Kokoro is not running", "model-down": "The local model did not answer", "cloud-fallback": "OpenRouter failed",
         "layout-missing": "The PDF layout model is missing", "highlight-failed": "The highlight could not be drawn",
         "permission": Permissions.screenRecording ? "Accessibility is off" : "Screen Recording is off", "slow-start": "Reads took over 10 s to start"][id] ?? id
    }
    static func action(_ id: String) -> String? {
        switch id {
        case "kokoro-down": return "Restart Kokoro"
        case "model-down": return "Open Ollama"
        case "cloud-fallback": return "Open Cloud settings"
        case "layout-missing": return "Copy fetch command"
        case "highlight-failed": return "Open Screen Recording settings"
        case "permission": return Permissions.screenRecording ? "Open Accessibility settings" : "Open Screen Recording settings"
        case "slow-start": return "Open Reading log"
        default: return nil
        }
    }
    static func perform(_ id: String) {
        let app = NSApp.delegate as? AppDelegate
        switch id {
        case "kokoro-down":
            let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/launchctl"); p.arguments = ["kickstart", "-k", "gui/\(getuid())/com.speakeasy.tts"]
            do { try p.run() } catch { FileHandle.standardError.write("speakeasy: launchctl failed: \(error.localizedDescription)\n".data(using: .utf8)!) }
        case "model-down": NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: "/Applications/Ollama.app"), configuration: .init())
        case "cloud-fallback": app?.showCloud()
        case "layout-missing":
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString("python3 ~/.speakeasy/bin/layoutdet.py fetch ~/.speakeasy/models", forType: .string)
        case "highlight-failed": Permissions.openScreenRecordingSettings()
        case "permission": if Permissions.screenRecording { Permissions.openAccessibilitySettings() } else { Permissions.openScreenRecordingSettings() }
        case "slow-start": app?.showReadingLog()
        default: break
        }
    }
}

/// Live checks: off the main thread, at most once per 60 s, at launch and when the menu or the Reading log opens.
final class Problems {
    var liveFailing: Set<String> = []
    var lastCheck: Date? = nil
    func checkLive(force: Bool, done: @escaping () -> Void) {
        if !force, let l = lastCheck, Date().timeIntervalSince(l) < 60 { done(); return }
        lastCheck = Date()
        DispatchQueue.global().async {
            var failing: Set<String> = []
            if (Config.value("SPEAKEASY_TTS") ?? "say").lowercased() == "kokoro", !Problems.answers(Problems.ttsHost(), timeout: 0.3) { failing.insert("kokoro-down") }
            if !Problems.modelListed() { failing.insert("model-down") }
            if !Permissions.accessibility || !Permissions.screenRecording { failing.insert("permission") }
            DispatchQueue.main.async {
                for id in failing.subtracting(self.liveFailing).sorted() { AppLog.event("problem", ["id": id, "live": true]) }
                self.liveFailing = failing; done()
            }
        }
    }
    func open(_ s: StatsV2?) -> [ProblemItem] {
        var out = liveFailing.sorted().map { id in ProblemItem(id: id, count: s?.problems.first { $0.id == id }?.count ?? 0, lastTs: nil, live: true) }
        for p in (s?.problems ?? []).filter({ $0.open && !liveFailing.contains($0.id) }).sorted(by: { ($0.last_ts ?? "") > ($1.last_ts ?? "") }) {
            out.append(ProblemItem(id: p.id, count: p.count, lastTs: p.last_ts, live: false))
        }
        return out
    }
    static func ttsHost() -> URL {
        let u = URL(string: Config.value("SPEAKEASY_TTS_URL") ?? "http://127.0.0.1:8000") ?? URL(string: "http://127.0.0.1:8000")!
        var c = URLComponents(); c.scheme = u.scheme ?? "http"; c.host = u.host ?? "127.0.0.1"; c.port = u.port; c.path = "/"
        return c.url!
    }
    /// Any HTTP answer within the timeout counts: the server is up.
    static func answers(_ url: URL, timeout: Double) -> Bool { fetch(url, timeout: timeout) != nil }
    static func fetch(_ url: URL, timeout: Double) -> Data? {
        var req = URLRequest(url: url); req.timeoutInterval = timeout
        let done = DispatchSemaphore(value: 0); var out: Data? = nil
        URLSession.shared.dataTask(with: req) { d, r, _ in if r != nil { out = d ?? Data() }; done.signal() }.resume()
        _ = done.wait(timeout: .now() + timeout + 0.2)
        return out
    }
    static func modelListed() -> Bool {
        let base = URL(string: Config.value("SPEAKEASY_LLM_URL") ?? "http://localhost:11434/v1") ?? URL(string: "http://localhost:11434")!
        var c = URLComponents(); c.scheme = base.scheme; c.host = base.host; c.port = base.port; c.path = "/api/tags"
        guard let u = c.url, let d = fetch(u, timeout: 1), let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let models = j["models"] as? [[String: Any]] else { return false }
        let want = Config.value("SPEAKEASY_MODEL") ?? "qwen3-vl:4b-instruct"
        return models.contains { ($0["name"] as? String) == want || ($0["model"] as? String) == want }
    }
}

/// The problem one finished read shows in the bar (A2.1), from its runs.jsonl record: whitelisted enum fields only.
struct RunProblem {
    let id: String, text: String
    static let unreachable: Set<String> = ["URLError", "ConnectionError", "ConnectionRefusedError", "ConnectionResetError", "TimeoutError", "timeout", "RemoteDisconnected", "OSError", "HTTPError"]
    static func record(_ runId: String) -> [String: Any]? {
        guard !runId.isEmpty, let h = FileHandle(forReadingAtPath: Home.dir + "/logs/runs.jsonl") else { return nil }
        defer { h.closeFile() }
        let size = h.seekToEndOfFile(); h.seek(toFileOffset: size > 262_144 ? size - 262_144 : 0)
        let lines = String(decoding: h.readDataToEndOfFile(), as: UTF8.self).split(separator: "\n").reversed()
        for l in lines where l.contains("\"\(runId)\"") {
            if let j = try? JSONSerialization.jsonObject(with: Data(l.utf8)) as? [String: Any], j["run_id"] as? String == runId {
                let keep = ["tts", "tts_requested", "backend", "llm_error_class", "llm_provider", "llm_provider_configured", "doc_parse", "sent_spoken", "end"]
                return j.filter { keep.contains($0.key) }
            }
        }
        return nil
    }
    static func of(runId: String) -> RunProblem? {
        guard let r = record(runId) else { return nil }
        if r["tts_requested"] as? String == "kokoro", r["tts"] as? String == "say" { return RunProblem(id: "kokoro-down", text: "Kokoro is not running. This read used the macOS voice.") }
        if r["backend"] as? String == "fallback", unreachable.contains(r["llm_error_class"] as? String ?? "") { return RunProblem(id: "model-down", text: "The local model did not answer. This read had no rewrite.") }
        if r["llm_provider_configured"] as? String == "openrouter", r["llm_provider"] as? String == "local" { return RunProblem(id: "cloud-fallback", text: "OpenRouter failed. This read used the local model.") }
        if let lay = (r["doc_parse"] as? [String: Any])?["layout"] as? [String: Any], lay["src"] as? String == "heuristic",
           ["model-missing", "model-mismatch", "no-onnxruntime", "load-failed"].contains(lay["reason"] as? String ?? "") {
            return RunProblem(id: "layout-missing", text: "The PDF layout model is missing. Figures are found by rules only.")
        }
        if (r["sent_spoken"] as? Int ?? 0) == 0, r["end"] as? String != "stopped" { return RunProblem(id: "empty", text: "Nothing was read. The selection was empty.") }
        return nil
    }
}

/// The user's "wrong spot" flags (app.jsonl user_flag: ts, seq, cue_kind) for the Reading log's list.
enum FlagLog {
    struct Flag: Hashable { var ts: Double; var cueKind: String? }
    static func recent(days: Int) -> [Flag] {
        guard let d = FileManager.default.contents(atPath: Home.dir + "/logs/app.jsonl") else { return [] }
        let from = Calendar.current.startOfDay(for: Date()).addingTimeInterval(-Double(days - 1) * 86400).timeIntervalSince1970
        return String(decoding: d, as: UTF8.self).split(separator: "\n").filter { $0.contains("\"user_flag\"") }.compactMap { l in
            guard let j = try? JSONSerialization.jsonObject(with: Data(l.utf8)) as? [String: Any], let ts = j["ts"] as? Double, ts >= from else { return nil }
            return Flag(ts: ts, cueKind: j["cue_kind"] as? String)
        }.suffix(3).reversed()
    }
}

/// "p. 5, first figure" from a resume point; fig_ids are page-local indexes (p<page>:fig<n>), never figure labels.
enum ResumeText {
    static let ordinals = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
    static func item(_ fid: String) -> String? {
        guard let colon = fid.firstIndex(of: ":") else { return nil }
        let rest = fid[fid.index(after: colon)...], table = rest.hasPrefix("tab")
        guard let n = Int(rest.dropFirst(3)), n >= 1 else { return nil }
        return (n <= ordinals.count ? ordinals[n - 1] + " " : "") + (table ? "table" : "figure") + (n > ordinals.count ? " \(n)" : "")
    }
    static func short(_ d: StatsV2.Doc) -> String {
        guard let r = d.resume, let p = r.page else { return "" }
        return "p. \(p)" + (r.fig_id.flatMap(item).map { ", \($0)" } ?? "")
    }
    static func long(_ d: StatsV2.Doc) -> String? {
        guard let r = d.resume, let p = r.page else { return nil }
        var s = "Resume at p. \(p)"
        if let i = r.fig_id.flatMap(item) { s += ", \(i)" }
        if let n = r.sentence, let of = r.of, of > 0 { s += ", sentence \(min(n, of)) of \(of)" }
        return s
    }
}

// ---------------- the window ----------------
struct ReadingLogModel {
    var result: Result<StatsV2, StatsError>
    var window: Int
    var live: Set<String> = []
    var flags: [FlagLog.Flag] = []
    var problems: [ProblemItem] {
        guard case .success(let s) = result else { return [] }
        var out = live.sorted().map { id in ProblemItem(id: id, count: s.problems.first { $0.id == id }?.count ?? 0, lastTs: nil, live: true) }
        out += s.problems.filter { !live.contains($0.id) }.map { ProblemItem(id: $0.id, count: $0.count, lastTs: $0.last_ts, live: false) }
        return out
    }
}

enum Fmt {
    static func date(_ iso: String?) -> String {
        guard let s = iso else { return "" }
        let inF = DateFormatter(); inF.locale = Locale(identifier: "en_US_POSIX"); inF.dateFormat = "yyyy-MM-dd'T'HH:mm:ssZ"
        guard let d = inF.date(from: s) else { return "" }
        let out = DateFormatter(); out.dateFormat = "MMM d, HH:mm"
        return out.string(from: d)
    }
    static func day(_ isoDay: String) -> String {
        let inF = DateFormatter(); inF.locale = Locale(identifier: "en_US_POSIX"); inF.dateFormat = "yyyy-MM-dd"
        guard let d = inF.date(from: isoDay) else { return isoDay }
        let out = DateFormatter(); out.dateFormat = "MMM d"; return out.string(from: d)
    }
    static func num(_ v: Double?) -> String { v.map { String(format: "%.1f", $0) } ?? "–" }
}

struct ReadingLogView: View {
    let model: ReadingLogModel
    var onWindow: (Int) -> Void = { _ in }
    var onAction: (String) -> Void = { _ in }
    var onOpenLogs: () -> Void = {}
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette(scheme)
        Surface(width: 640, height: 480) {
            HStack(alignment: .firstTextBaseline) {
                Text("Reading log").type(.title)
                Spacer()
                Menu(["Today", "Last 7 days", "Last 30 days"][[1, 7, 30].firstIndex(of: model.window) ?? 1]) {
                    Button("Today") { onWindow(1) }; Button("Last 7 days") { onWindow(7) }; Button("Last 30 days") { onWindow(30) }
                }.menuStyle(.borderlessButton).fixedSize().type(.body)
            }.padding(.bottom, Space.s16)
            switch model.result {
            case .failure(let e):
                VStack(alignment: .leading, spacing: Space.s8) {
                    Ticked(color: p.brick) {
                        VStack(alignment: .leading, spacing: Space.s4) {
                            Text("Could not read the log at ~/.speakeasy/logs/runs.jsonl").type(.body).snapFrame("dominant")
                            Text(e.decode ? "The stats output was not readable (exit status \(e.status))." : "speakeasy stats exited with status \(e.status).").type(.label).foregroundStyle(p.slate)
                        }
                    }
                    Button("Open logs folder", action: onOpenLogs).buttonStyle(TokButtonStyle())
                }
                Spacer()
            case .success(let s) where s.reads.window == 0 && s.documents.isEmpty:
                Ticked(color: p.mark) {
                    Text("Nothing read yet. Select a paragraph and press ⌃⌥S, or drag over a figure with ⌃⌥A.").type(.body)
                        .fixedSize(horizontal: false, vertical: true).snapFrame("dominant")
                }.frame(width: 380, alignment: .leading)
                Spacer()
                footer(s, p)
            case .success(let s):
                HStack(alignment: .top, spacing: Space.s16) {
                    documents(s, p).frame(width: 352, alignment: .topLeading)
                    Rectangle().fill(p.line).frame(width: 1)
                    side(s, p)
                }.frame(height: 352, alignment: .top).clipped()
                Spacer(minLength: 0)
                footer(s, p)
            }
        }
    }
    func documents(_ s: StatsV2, _ p: Palette) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.s16) {
                ForEach(Array(s.documents.prefix(12).enumerated()), id: \.offset) { i, d in
                    Ticked(color: p.mark, active: i == 0) { DocRow(doc: d, p: p) }
                        .snapFrame(i == 0 ? "dominant" : nil)
                    if i < min(s.documents.count, 12) - 1 { Hairline() }
                }
                if s.documents.isEmpty { Text("Reads without a document: area captures of screens and windows.").type(.body).foregroundStyle(p.slate) }
            }
        }
    }
    func side(_ s: StatsV2, _ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: Space.s4) {
            Text("First audio").type(.head)
            numRow(Fmt.num(s.first_audio_s.median), "s", "median", hero: true, p)
            numRow(Fmt.num(s.first_audio_s.p90), "s", "slowest 1 in 10", hero: false, p)
            if !s.first_audio_s.by_provider.isEmpty {
                Text(s.first_audio_s.by_provider.sorted { $0.key < $1.key }.map { "\($0.key == "openrouter" ? "Cloud" : "Local") \(Fmt.num($0.value.median)) s" }.joined(separator: " · "))
                    .type(.labelMono).foregroundStyle(p.slate).lineLimit(1)
            }
            if s.first_audio_s.trend.count > 1 { Trend(days: s.first_audio_s.trend, p: p) }
            Text("Highlight").type(.head).padding(.top, Space.s12)
            if let h = s.highlight {
                let cued = h.shows - h.hidden
                numRow(cued > 0 ? "\(Int((100 * Double(min(h.landed, cued)) / Double(cued)).rounded()))" : "–", "%", "on target first time", hero: false, p)
                numRow("\(h.flagged)", "", "flagged wrong spot", hero: false, p)
                ForEach(model.flags.prefix(2), id: \.self) { f in
                    Text("\(Fmt.date(ISO8601DateFormatter.local(f.ts))) · \((f.cueKind ?? "cue").replacingOccurrences(of: "_", with: " "))").type(.labelMono).foregroundStyle(p.slate)
                }
                if h.sentences > 0 { Text("\(h.no_cue_sentences) of \(h.sentences) sentences had no cue").type(.label).foregroundStyle(p.slate) }
                if let since = s.since?["highlight"] { Text("since \(Fmt.day(since))").type(.label).foregroundStyle(p.slate) }
            } else { Text("No highlight data yet").type(.body).foregroundStyle(p.slate) }
            let probs = model.problems
            if !probs.isEmpty {
                Text("Problems").type(.head).padding(.top, Space.s12)
                ForEach(probs.prefix(2)) { pr in
                    VStack(alignment: .leading, spacing: Space.s4) {
                        Text([ProblemCopy.short(pr.id), pr.count > 0 ? (pr.count == 1 ? "1 read" : "\(pr.count) reads") : nil, pr.lastTs.map(Fmt.date)]
                                .compactMap { $0 }.joined(separator: ", ")).type(.body).foregroundStyle(pr.live || pr.lastTs == nil ? p.brick : p.ink)
                            .lineLimit(2).help(ProblemCopy.sentence(pr))
                        if let a = ProblemCopy.action(pr.id) { Button(a) { onAction(pr.id) }.buttonStyle(.plain).type(.bodyMedium).foregroundStyle(p.mark) }
                    }
                }
            }
            Spacer(minLength: 0)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
    func numRow(_ v: String, _ unit: String, _ label: String, hero: Bool, _ p: Palette) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.s4) {
            Text(v).type(hero ? .hero : .data).monospacedDigit().frame(minWidth: hero ? 64 : 44, alignment: .trailing).snapFrame(hero ? "hero" : nil)
            Text(unit).type(.label).foregroundStyle(p.slate)
            Text(label).type(.label).foregroundStyle(p.slate)
        }
    }
    func footer(_ s: StatsV2, _ p: Palette) -> some View {
        VStack(alignment: .leading, spacing: Space.s4) {
            Hairline()
            HStack(alignment: .firstTextBaseline, spacing: Space.s4) {
                Text("\(s.reads.window)").type(.data); Text("reads ·").type(.label).foregroundStyle(p.slate)
                Text(String(format: "%.1f", s.listened_min.window)).type(.data); Text("min listened ·").type(.label).foregroundStyle(p.slate)
                Text(s.speech_wpm.map(String.init) ?? "–").type(.data); Text("wpm").type(.label).foregroundStyle(p.slate)
                Spacer()
                let c = s.completion
                Text("\(c.finished) finished, \(c.stopped) stopped, \(c.error) \(c.error == 1 ? "error" : "errors")").type(.label).foregroundStyle(p.slate)
                if let since = s.since?["completion"] { Text("since \(Fmt.day(since))").type(.label).foregroundStyle(p.slate) }
            }.padding(.top, Space.s8)
        }
    }
}

struct DocRow: View {
    let doc: StatsV2.Doc, p: Palette
    var body: some View {
        VStack(alignment: .leading, spacing: Space.s4) {
            if let n = doc.name, !n.isEmpty { Text(n).type(.title).lineLimit(2) }
            else {
                HStack(alignment: .firstTextBaseline, spacing: Space.s8) {
                    Text("Document \(doc.doc.prefix(6))").type(.dataMedium); Text("(name hidden)").type(.label).foregroundStyle(p.slate)
                }
            }
            HStack(alignment: .firstTextBaseline) {
                PageStrip(pages: doc.pages, p: p)
                Spacer()
                VStack(alignment: .trailing, spacing: 0) {
                    let figs = doc.items.filter { $0.contains(":fig") }.count, tabs = doc.items.count - figs
                    Text([figs > 0 ? "\(figs) \(figs == 1 ? "figure" : "figures")" : nil, tabs > 0 ? "\(tabs) \(tabs == 1 ? "table" : "tables")" : nil]
                        .compactMap { $0 }.joined(separator: ", ")).type(.label).foregroundStyle(p.slate)
                    Text(Fmt.date(doc.last_ts)).type(.labelMono).foregroundStyle(p.slate)
                }
            }
            if let r = ResumeText.long(doc) { Text(r).type(.body) }
        }
    }
}

/// One cell per page between the first and last page listened to: ▮ listened, ▯ skipped.
struct PageStrip: View {
    let pages: [Int], p: Palette
    var body: some View {
        let lo = pages.min() ?? 0, hi = min((pages.max() ?? 0), lo + 15)
        HStack(alignment: .firstTextBaseline, spacing: Space.s4) {
            Text("p.").type(.label).foregroundStyle(p.slate)
            ForEach(Array(lo...max(lo, hi)), id: \.self) { n in
                VStack(spacing: 2) {
                    Text("\(n)").type(.labelMono)
                    RoundedRectangle(cornerRadius: 1).fill(pages.contains(n) ? p.mark : Color.clear)
                        .overlay(RoundedRectangle(cornerRadius: 1).strokeBorder(p.mark, lineWidth: 1)).frame(width: 6, height: 10)
                }.frame(minWidth: 14)
            }
        }
    }
}

/// The 14-day first-audio trend as Plex Mono marks (no chart library).
struct Trend: View {
    let days: [StatsV2.Day], p: Palette
    var body: some View {
        let mx = max(days.map(\.median).max() ?? 1, 0.1), marks = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        Text(days.map { marks[min(7, Int(($0.median / mx) * 7.99))] }.joined()).type(.data).foregroundStyle(p.mark)
            .help(days.map { "\($0.date): \(Fmt.num($0.median)) s" }.joined(separator: "\n"))
    }
}

extension ISO8601DateFormatter {
    static func local(_ epoch: Double) -> String {
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssZ"
        return f.string(from: Date(timeIntervalSince1970: epoch))
    }
}
