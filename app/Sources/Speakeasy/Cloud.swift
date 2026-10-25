// Optional OpenRouter (cloud) settings, off by default. Bring-your-own-key like Raycast/open-wispr: the key lives in the
// login Keychain (service speakeasy-openrouter), written through /usr/bin/security so the Python backend reads it with
// `security find-generic-password` without a prompt. The config file only holds on/off and the model name.
import AppKit
import SwiftUI

enum Cloud {
    static let service = "speakeasy-openrouter", account = "openrouter"
    static let defaultModel = "google/gemini-2.5-flash"
    static let suggestions = ["google/gemini-2.5-flash", "google/gemini-2.5-flash-lite", "openai/gpt-4.1-mini", "anthropic/claude-haiku-4.5", "qwen/qwen2.5-vl-72b-instruct"]
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

struct CloudView: View {
    @State var llm = Cloud.llmOn
    @State var ground = Cloud.groundOn
    @State var model = Cloud.model
    @State var key = ""
    @State var hasKey = Cloud.hasKey
    @State var note = ""
    var onChange: () -> Void = {}
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Optional: send work to OpenRouter instead of the local model. Off by default; if the cloud fails or is slow, Speakeasy falls back to the local model.").fixedSize(horizontal: false, vertical: true)
            Toggle("Rewrite with OpenRouter (sends the selected text)", isOn: $llm)
                .onChange(of: llm) { v in Config.set("SPEAKEASY_LLM", v ? "openrouter" : "local"); onChange() }
            Toggle("Figure grounding with OpenRouter (sends figure screenshots)", isOn: $ground)
                .onChange(of: ground) { v in Config.set("SPEAKEASY_GROUND", v ? "openrouter" : "auto"); onChange() }
            HStack {
                TextField("Model", text: $model).onSubmit { saveModel() }
                Menu("Suggestions") { ForEach(Cloud.suggestions, id: \.self) { m in Button(m) { model = m; saveModel() } } }.fixedSize()
                Button("Save") { saveModel() }
            }
            HStack {
                SecureField(hasKey ? "Key saved in Keychain (enter a new one to replace)" : "OpenRouter key (sk-or-…)", text: $key)
                Button("Save key") { note = Cloud.saveKey(key) ? "Key saved to Keychain." : "Could not save the key."; key = ""; hasKey = Cloud.hasKey }
                Button("Remove") { Cloud.removeKey(); hasKey = Cloud.hasKey; note = "Key removed." }.disabled(!hasKey)
            }
            if !note.isEmpty { Text(note).font(.footnote) }
            if (llm || ground) && !hasKey { Text("No key yet: cloud is on but Speakeasy will keep using the local model.").font(.footnote).foregroundStyle(.orange) }
            Text(llm || ground
                 ? "Privacy: while on, \(llm ? "the text you read" : "")\(llm && ground ? " and " : "")\(ground ? "figure screenshots" : "") leave this Mac and go to OpenRouter and the model provider. A cloud icon shows on the pill."
                 : "Privacy: everything stays on this Mac.")
                .font(.footnote).foregroundStyle(llm || ground ? .orange : .secondary).fixedSize(horizontal: false, vertical: true)
        }.padding(20).frame(width: 480)
    }
    func saveModel() {
        let m = model.trimmingCharacters(in: .whitespaces)
        guard m.range(of: "^[A-Za-z0-9._:/-]+$", options: .regularExpression) != nil else { note = "Model names look like vendor/model."; return }
        Config.set("SPEAKEASY_OR_MODEL", m); note = "Model set to \(m)."
    }
}
