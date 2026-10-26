// Update path. Sparkle-ready: Info.plist carries SUFeedURL / SUPublicEDKey (see docs/release.md); until Sparkle
// (MIT, sparkle-project.org) is linked, "Check for updates…" reads the same GitHub release and opens it if newer.
import AppKit

enum Updater {
    static let latest = URL(string: "https://api.github.com/repos/Donnieboi03/speakeasy/releases/latest")!
    static var current: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0" }

    static func newer(_ a: String, than b: String) -> Bool {
        let x = a.trimmingCharacters(in: CharacterSet(charactersIn: "v")).split(separator: ".").map { Int($0) ?? 0 }
        let y = b.split(separator: ".").map { Int($0) ?? 0 }
        for i in 0..<max(x.count, y.count) {
            let p = i < x.count ? x[i] : 0, q = i < y.count ? y[i] : 0
            if p != q { return p > q }
        }
        return false
    }

    static func check() {
        URLSession.shared.dataTask(with: latest) { d, _, _ in
            let j = d.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let tag = j?["tag_name"] as? String, page = (j?["html_url"] as? String).flatMap(URL.init(string:))
            DispatchQueue.main.async {
                let a = NSAlert()
                if let t = tag, let u = page, newer(t, than: current) {
                    a.messageText = "Speakeasy \(t.trimmingCharacters(in: CharacterSet(charactersIn: "v"))) is available. You have \(current)."
                    a.addButton(withTitle: "Open download page"); a.addButton(withTitle: "Not now")
                    NSApp.activate(ignoringOtherApps: true)
                    if a.runModal() == .alertFirstButtonReturn { NSWorkspace.shared.open(u) }
                } else {
                    a.messageText = tag == nil ? "Could not reach GitHub to check for updates." : "Up to date: \(current)."
                    a.addButton(withTitle: "Close")
                    NSApp.activate(ignoringOtherApps: true); a.runModal()
                }
            }
        }.resume()
    }
}
