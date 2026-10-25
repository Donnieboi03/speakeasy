// Stats window + menu-bar line, from `speakeasy stats --json` (counts only; runs.jsonl never holds text).
import AppKit
import SwiftUI

struct StatsData: Decodable {
    var reads = 0, words_in = 0, words_listened = 0, figures_explained = 0, tables_read = 0, lists_read = 0
    var audio_minutes = 0.0, time_saved_min_estimate = 0.0
    var median_first_audio_ms: Int?
    var streak_days = 0, longest_streak_days = 0, today_words = 0
    var days: [String: Int] = [:]

    static func load(_ backend: String) -> StatsData {
        let p = Process(), out = Pipe()
        p.executableURL = URL(fileURLWithPath: backend); p.arguments = ["stats", "--json"]
        p.standardOutput = out; p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return StatsData() }
        let d = out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
        return (try? JSONDecoder().decode(StatsData.self, from: d)) ?? StatsData()
    }
    var menuLine: String { "Today \(today_words) words · \(streak_days)-day streak" }
}

struct StatsView: View {
    let s: StatsData
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 22) {
                fig("\(s.words_listened)", "words listened")
                fig("\(s.streak_days)d", "streak (best \(s.longest_streak_days)d)")
                fig("\(s.figures_explained)", "figures explained")
                fig("\(s.tables_read)", "tables read")
            }
            Heatmap(days: s.days)
            HStack(spacing: 22) {
                fig(s.median_first_audio_ms.map { String(format: "%.1fs", Double($0) / 1000) } ?? "–", "median first audio")
                fig(String(format: "%.0f min", s.time_saved_min_estimate), "est. reading time saved")
                fig(String(format: "%.0f min", s.audio_minutes), "listened")
            }
            Text("Time saved is an estimate: words read to you ÷ 238 words per minute (average adult silent reading speed).")
                .font(.footnote).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }.padding(20).frame(width: 520)
    }
    func fig(_ v: String, _ l: String) -> some View {
        VStack(alignment: .leading) { Text(v).font(.title2.bold()); Text(l).font(.caption).foregroundStyle(.secondary) }
    }
}

/// GitHub-style heatmap of words listened per day, last 18 weeks.
struct Heatmap: View {
    let days: [String: Int]
    var body: some View {
        let cal = Calendar.current, f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"
        let today = cal.startOfDay(for: Date())
        let start = cal.date(byAdding: .day, value: -(17 * 7 + cal.component(.weekday, from: today) - 1), to: today)!
        let mx = max(1, days.values.max() ?? 1)
        return HStack(alignment: .top, spacing: 3) {
            ForEach(0..<18, id: \.self) { w in
                VStack(spacing: 3) {
                    ForEach(0..<7, id: \.self) { d in
                        let day = cal.date(byAdding: .day, value: w * 7 + d, to: start)!
                        let n = days[f.string(from: day)] ?? 0
                        RoundedRectangle(cornerRadius: 2)
                            .fill(day > today ? Color.clear : n == 0 ? Color.gray.opacity(0.18) : Color.green.opacity(0.25 + 0.75 * Double(n) / Double(mx)))
                            .frame(width: 11, height: 11).help("\(f.string(from: day)): \(n) words")
                    }
                }
            }
        }
    }
}
