// Optional OpenRouter (cloud) settings, off by default. Bring-your-own-key like Raycast/open-wispr: the key lives in the
// login Keychain (service speakeasy-openrouter), written through /usr/bin/security so the Python backend reads it with
// `security find-generic-password` without a prompt. The config file only holds on/off and the model name.
import AppKit
import SwiftUI

enum Cloud {
    static let service = "speakeasy-openrouter", account = "openrouter"
    static let defaultModel = "google/gemini-3.5-flash-lite"
    static let suggestions = ["google/gemini-3.5-flash-lite", "google/gemini-2.5-flash", "openai/gpt-4.1-mini", "anthropic/claude-haiku-4.5", "qwen/qwen2.5-vl-72b-instruct"]
    static var llmOn: Bool { Config.value("SPEAKEASY_LLM") == "openrouter" }
    static var groundOn: Bool { Config.value("SPEAKEASY_GROUND") == "openrouter" }
    static var anyOn: Bool { llmOn || groundOn }
    static var model: String { Config.value("SPEAKEASY_OR_MODEL").flatMap { $0.isEmpty ? nil : $0 } ?? defaultModel }

    @discardableResult static func security(_ input: String?, _ args: [String]) -> (Int32, String) {
        let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/security"); p.arguments = args
        let out = Pipe(), inp = Pipe(); p.standardOutput = out; p.standardError = Pipe(); p.standardInput = inp
        do { try p.run() } catch { return (-1, "") }
        if let s = input { inp.fileHandleForWriting.write(s.data(using: .utf8)!) }
        try? inp.fileHandleForWriting.close()
        let d = out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
        return (p.terminationStatus, String(data: d, encoding: .utf8) ?? "")
    }
    static var hasKey: Bool { security(nil, ["find-generic-password", "-s", service, "-a", account]).0 == 0 }
    /// `security -i` reads the command on stdin, so the key never appears in a process list.
    static func saveKey(_ key: String) -> Bool {
        let k = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !k.isEmpty, !k.contains("'"), !k.contains("\n") else { return false }
        _ = security("add-generic-password -U -s \(service) -a \(account) -T /usr/bin/security -w '\(k)'\n", ["-i"])
        return hasKey
    }
    static func removeKey() { security(nil, ["delete-generic-password", "-s", service, "-a", account]) }
}

/// The speed trade-off line under the Cloud settings, from the Reading log's numbers (last 7 days).
struct CloudTradeoff {
    var cloudReads = 0, cloudMedian: Double? = nil, localMedian: Double? = nil
    init(stats: StatsV2?) {
        let bp = stats?.first_audio_s.by_provider ?? [:]
        cloudReads = bp["openrouter"]?.n ?? 0; cloudMedian = bp["openrouter"]?.median; localMedian = bp["local"]?.median
    }
    init(cloudReads: Int, cloudMedian: Double?, localMedian: Double?) { self.cloudReads = cloudReads; self.cloudMedian = cloudMedian; self.localMedian = localMedian }
    var line: String {
        if cloudReads == 0 { return localMedian.map { "Last 7 days: no reads used OpenRouter; local first audio \(Fmt.num($0)) s." } ?? "Last 7 days: no reads yet." }
        return "Last 7 days: \(cloudReads) \(cloudReads == 1 ? "read" : "reads") used OpenRouter, \(Fmt.num(cloudMedian)) s to first audio" + (localMedian.map { " (local: \(Fmt.num($0)) s)." } ?? ".")
    }
}

struct CloudView: View {
    @State var llm = Cloud.llmOn
    @State var ground = Cloud.groundOn
    @State var model = Cloud.model
    @State var key = ""
    @State var hasKey = Cloud.hasKey
    @State var replacing = false
    @State var note = ""
    var tradeoff = CloudTradeoff(stats: nil)
    var onChange: () -> Void = {}
    @Environment(\.colorScheme) var scheme
    var privacy: String {
        if !(llm || ground) { return "Cloud is off. Everything stays on this Mac." }
        let what = [llm ? "the text you read" : nil, ground ? "figure screenshots" : nil].compactMap { $0 }.joined(separator: " and ")
        return "While on, \(what) \(llm && ground ? "go" : "goes") to OpenRouter and \(model)."
    }
    var body: some View {
        let p = Palette(scheme)
        Surface(width: 480, height: 340) {
            Ticked(color: p.mark) { Text(privacy).type(.head).fixedSize(horizontal: false, vertical: true).snapFrame("dominant") }
                .padding(.bottom, Space.s16)
            VStack(alignment: .leading, spacing: Space.s8) {
                toggleRow("Rewrite with OpenRouter", "sends selected text", $llm) { Config.set("SPEAKEASY_LLM", $0 ? "openrouter" : "local") }
                toggleRow("Find figure parts with OpenRouter", "sends crops", $ground) { Config.set("SPEAKEASY_GROUND", $0 ? "openrouter" : "auto") }
                HStack(alignment: .firstTextBaseline, spacing: Space.s8) {
                    Text("Model").type(.label).foregroundStyle(p.slate).frame(width: 40, alignment: .leading)
                    TextField("vendor/model", text: $model).textFieldStyle(.plain).type(.data).padding(Space.s4)
                        .overlay(RoundedRectangle(cornerRadius: Radius.button).stroke(p.line, lineWidth: 1)).onSubmit { saveModel() }
                    Menu("Suggestions") { ForEach(Cloud.suggestions, id: \.self) { m in Button(m) { model = m; saveModel() } } }.menuStyle(.borderlessButton).fixedSize().type(.label)
                    Button("Save") { saveModel() }.buttonStyle(TokButtonStyle())
                }
                HStack(alignment: .firstTextBaseline, spacing: Space.s8) {
                    Text("Key").type(.label).foregroundStyle(p.slate).frame(width: 40, alignment: .leading)
                    if hasKey && !replacing {
                        Text("saved in Keychain").type(.body).foregroundStyle(p.slate).frame(maxWidth: .infinity, alignment: .leading)
                        Button("Replace") { replacing = true }.buttonStyle(TokButtonStyle())
                        Button("Remove") { Cloud.removeKey(); hasKey = Cloud.hasKey; note = "Key removed." }.buttonStyle(TokButtonStyle())
                    } else {
                        SecureField("OpenRouter key (sk-or-…)", text: $key).textFieldStyle(.plain).type(.data).padding(Space.s4)
                            .overlay(RoundedRectangle(cornerRadius: Radius.button).stroke(p.line, lineWidth: 1))
                        Button("Save key") { note = Cloud.saveKey(key) ? "Key saved to Keychain." : "Could not save the key."; key = ""; hasKey = Cloud.hasKey; replacing = false }
                            .buttonStyle(TokButtonStyle(prominent: true))
                    }
                }
                if !note.isEmpty { Text(note).type(.label).foregroundStyle(p.slate) }
                if (llm || ground) && !hasKey { Text("No key yet: Speakeasy keeps using the local model.").type(.label).foregroundStyle(p.brick) }
            }.padding(.leading, Space.s12)
            Spacer(minLength: Space.s16)
            Text(tradeoff.line).type(.body).foregroundStyle(p.slate).fixedSize(horizontal: false, vertical: true).padding(.leading, Space.s12)
        }
    }
    func toggleRow(_ title: String, _ hint: String, _ v: Binding<Bool>, _ save: @escaping (Bool) -> Void) -> some View {
        let p = Palette(scheme)
        return HStack(alignment: .firstTextBaseline) {
            Toggle(isOn: v) { Text(title).type(.body) }.toggleStyle(TokCheckboxStyle()).onChange(of: v.wrappedValue) { x in save(x); onChange() }
            Spacer()
            Text(hint).type(.label).foregroundStyle(p.slate)
        }
    }
    func saveModel() {
        let m = model.trimmingCharacters(in: .whitespaces)
        guard m.range(of: "^[A-Za-z0-9._:/-]+$", options: .regularExpression) != nil else { note = "Model names look like vendor/model."; return }
        Config.set("SPEAKEASY_OR_MODEL", m); note = "Model set to \(m)."
    }
}
