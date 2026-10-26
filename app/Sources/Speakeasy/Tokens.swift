// docs/design.md sections 1 to 3: every colour, font, size, spacing and radius of the app. The only file where a colour
// value may appear (contract C1); views take colours from `Palette` and type from `TType`.
import AppKit
import CoreText
import SwiftUI

enum Hex {
    static let ink: UInt32 = 0x14233B, navy: UInt32 = 0x1C3150, navyLine: UInt32 = 0x2C4468, slate: UInt32 = 0x566577
    static let paper: UInt32 = 0xF3F6F8, paper2: UInt32 = 0xE8EEF2, line: UInt32 = 0xCBD5DE
    static let ice: UInt32 = 0xA8D8EC, glacier: UInt32 = 0x1F6E99, ember: UInt32 = 0xE0892B, brick: UInt32 = 0xB23A2B
    static let mist: UInt32 = 0xE3ECF2                                   // light text on navy; dark-appearance ink
    static let paperDark: UInt32 = 0x101B2C, paper2Dark: UInt32 = 0x182841, slateDark: UInt32 = 0x9AAABB, lineDark: UInt32 = 0x2A3D58
}

func rgb(_ v: UInt32, _ alpha: Double = 1) -> Color {
    Color(.sRGB, red: Double((v >> 16) & 255) / 255, green: Double((v >> 8) & 255) / 255, blue: Double(v & 255) / 255, opacity: alpha)
}
func nsRGB(_ v: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((v >> 16) & 255) / 255, green: CGFloat((v >> 8) & 255) / 255, blue: CGFloat(v & 255) / 255, alpha: alpha)
}

/// Window colours for one appearance. The bar and the on-screen cues look the same in both (design 1).
struct Palette {
    let paper, paper2, ink, slate, line, mark: Color                   // mark: glacier on light, ice on dark
    let ice = rgb(Hex.ice), glacier = rgb(Hex.glacier), ember = rgb(Hex.ember), brick = rgb(Hex.brick)
    let navy = rgb(Hex.navy), navyLine = rgb(Hex.navyLine), mist = rgb(Hex.mist)
    init(_ scheme: ColorScheme) {
        let dark = scheme == .dark
        paper = rgb(dark ? Hex.paperDark : Hex.paper); paper2 = rgb(dark ? Hex.paper2Dark : Hex.paper2)
        ink = rgb(dark ? Hex.mist : Hex.ink); slate = rgb(dark ? Hex.slateDark : Hex.slate)
        line = rgb(dark ? Hex.lineDark : Hex.line); mark = rgb(dark ? Hex.ice : Hex.glacier)
    }
}

enum Space { static let s4: CGFloat = 4, s8: CGFloat = 8, s12: CGFloat = 12, s16: CGFloat = 16, s24: CGFloat = 24, s40: CGFloat = 40 }
enum Radius { static let chip: CGFloat = 2, button: CGFloat = 3, panel: CGFloat = 4 }
enum Motion { static let expand = 0.12, slide = 0.16 }
let tickWidth: CGFloat = 3                                           // the margin tick (design 4)

// ---------------- type (design 2) ----------------
enum Fonts {
    static let files = ["SourceSerif4-Semibold", "SourceSerif4-Regular", "IBMPlexSans-Regular", "IBMPlexSans-Medium", "IBMPlexMono-Regular", "IBMPlexMono-Medium"]
    static let serif = "SourceSerif4-Semibold", sans = "IBMPlexSans", sansMedium = "IBMPlexSans-Medm", mono = "IBMPlexMono", monoMedium = "IBMPlexMono-Medm"
    static let faces: [(face: String, names: [String])] = [("serif", [serif]), ("sans", [sans, sansMedium]), ("mono", [mono, monoMedium])]
    private(set) static var missing: Set<String> = []               // faces ("serif" | "sans" | "mono") that fell back
    /// Registers the bundled fonts for this process only; returns the faces that could not be registered.
    @discardableResult static func register(from dir: URL? = Bundle.main.resourceURL?.appendingPathComponent("Fonts")) -> Set<String> {
        for f in files {
            guard let u = dir?.appendingPathComponent(f + ".otf") else { continue }
            var err: Unmanaged<CFError>?
            if !CTFontManagerRegisterFontsForURL(u as CFURL, .process, &err), let e = err?.takeRetainedValue(),
               CFErrorGetCode(e) != CTFontManagerError.alreadyRegistered.rawValue {
                FileHandle.standardError.write("speakeasy: font \(f) not registered: \(CFErrorCopyDescription(e) as String)\n".data(using: .utf8)!)
            }
        }
        missing = Set(faces.filter { $0.names.contains { NSFont(name: $0, size: 12) == nil } }.map(\.face))
        return missing
    }
    static func font(_ face: String, _ name: String, _ size: CGFloat, weight: Font.Weight) -> Font {
        if !missing.contains(face) { return .custom(name, fixedSize: size) }
        switch face {                                                     // fallback chain: Charter, SF Pro Text, SF Mono
        case "serif": return NSFont(name: "Charter-Bold", size: size) != nil ? .custom("Charter-Bold", fixedSize: size) : .system(size: size, weight: .semibold, design: .serif)
        case "mono": return .system(size: size, weight: weight, design: .monospaced)
        default: return .system(size: size, weight: weight)
        }
    }
}

/// The type scale (px, 4 px baseline grid): label 11, body 13, data 13, head 15, title 20, hero 28.
enum TType {
    case label, labelMono, body, bodyMedium, data, dataMedium, head, title, hero
    var size: CGFloat { switch self { case .label, .labelMono: 11; case .body, .bodyMedium, .data, .dataMedium: 13; case .head: 15; case .title: 20; case .hero: 28 } }
    var lineHeight: CGFloat { switch self { case .label, .labelMono: 16; case .body, .bodyMedium, .data, .dataMedium, .head: 20; case .title: 28; case .hero: 32 } }
    var font: Font {
        switch self {
        case .label: Fonts.font("sans", Fonts.sansMedium, size, weight: .medium)
        case .labelMono: Fonts.font("mono", Fonts.monoMedium, size, weight: .medium)
        case .body: Fonts.font("sans", Fonts.sans, size, weight: .regular)
        case .bodyMedium: Fonts.font("sans", Fonts.sansMedium, size, weight: .medium)
        case .data: Fonts.font("mono", Fonts.mono, size, weight: .regular)
        case .dataMedium: Fonts.font("mono", Fonts.monoMedium, size, weight: .medium)
        case .head, .title, .hero: Fonts.font("serif", Fonts.serif, size, weight: .semibold)
        }
    }
    var nsFont: NSFont {
        let name: String
        switch self {
        case .label: name = Fonts.sansMedium
        case .labelMono: name = Fonts.monoMedium
        case .body: name = Fonts.sans
        case .bodyMedium: name = Fonts.sansMedium
        case .data: name = Fonts.mono
        case .dataMedium: name = Fonts.monoMedium
        case .head, .title, .hero: name = Fonts.serif
        }
        return NSFont(name: name, size: size) ?? .systemFont(ofSize: size)
    }
    var tracking: CGFloat { self == .label ? size * 0.02 : 0 }
}

extension View {
    func type(_ t: TType) -> some View { font(t.font).tracking(t.tracking).lineSpacing(max(0, t.lineHeight - t.size * 1.25)) }
}

/// The one control style: 12 x 8 padding, radius 3, hairline border; `prominent` fills with the mark colour.
struct TokButtonStyle: ButtonStyle {
    var prominent = false
    @Environment(\.colorScheme) var scheme
    func makeBody(configuration c: Configuration) -> some View {
        let p = Palette(scheme)
        return c.label.type(.body)
            .foregroundStyle(prominent ? p.paper : p.ink)
            .padding(.horizontal, Space.s12).padding(.vertical, Space.s8)
            .background(RoundedRectangle(cornerRadius: Radius.button).fill(prominent ? p.mark.opacity(c.isPressed ? 0.85 : 1) : (c.isPressed ? p.paper2 : p.paper)))
            .overlay(RoundedRectangle(cornerRadius: Radius.button).stroke(prominent ? p.mark : p.line, lineWidth: 1))
            .contentShape(Rectangle())
    }
}

/// A window surface: paper background, 24 inset, the given size.
struct Surface<Content: View>: View {
    let width: CGFloat, height: CGFloat?
    @ViewBuilder let content: () -> Content
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette(scheme)
        VStack(alignment: .leading, spacing: 0) { content() }
            .padding(Space.s24)
            .frame(width: width, height: height, alignment: .topLeading)
            .background(p.paper)
            .foregroundStyle(p.ink)
            .tint(p.mark)
    }
}

/// The margin tick left of a dominant element (design 4): reports its frame for `--snapshot` sidecars.
struct Ticked<Content: View>: View {
    var color: Color
    var active = true
    @ViewBuilder let content: () -> Content
    var body: some View {
        content().padding(.leading, Space.s12)
            .overlay(alignment: .leading) { Rectangle().fill(active ? color : Color.clear).frame(width: tickWidth).snapFrame(active ? "tick" : nil) }
    }
}

struct Hairline: View {
    @Environment(\.colorScheme) var scheme
    var body: some View { Rectangle().fill(Palette(scheme).line).frame(height: 1) }
}

/// Checkbox drawn from the tokens (the native one paints pure white): a 14 pt box, radius 3, the mark colour when on.
struct TokCheckboxStyle: ToggleStyle {
    @Environment(\.colorScheme) var scheme
    func makeBody(configuration c: Configuration) -> some View {
        let p = Palette(scheme)
        return HStack(alignment: .firstTextBaseline, spacing: Space.s8) {
            TokBox(on: c.isOn, p: p).alignmentGuide(.firstTextBaseline) { $0[.bottom] - 2 }
            c.label
        }.contentShape(Rectangle()).onTapGesture { c.isOn.toggle() }
    }
}
struct TokBox: View {
    let on: Bool, p: Palette
    var body: some View {
        RoundedRectangle(cornerRadius: Radius.button).fill(on ? p.mark : p.paper)
            .overlay(RoundedRectangle(cornerRadius: Radius.button).strokeBorder(on ? p.mark : p.slate, lineWidth: 1))
            .overlay(Path { q in q.move(to: CGPoint(x: 3.5, y: 7.5)); q.addLine(to: CGPoint(x: 6, y: 10)); q.addLine(to: CGPoint(x: 10.5, y: 4)) }
                        .stroke(on ? p.paper : Color.clear, style: StrokeStyle(lineWidth: 1.6, lineCap: .round, lineJoin: .round)))
            .frame(width: 14, height: 14)
    }
}
/// One choice of a set (the bar position): the same box, filled with a smaller square when chosen.
struct TokChoice: View {
    let title: String, on: Bool, action: () -> Void
    @Environment(\.colorScheme) var scheme
    var body: some View {
        let p = Palette(scheme)
        Button(action: action) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s4) {
                RoundedRectangle(cornerRadius: Radius.button).strokeBorder(on ? p.mark : p.slate, lineWidth: 1)
                    .overlay(RoundedRectangle(cornerRadius: Radius.chip).fill(on ? p.mark : Color.clear).padding(3.5))
                    .frame(width: 14, height: 14).alignmentGuide(.firstTextBaseline) { $0[.bottom] - 2 }
                Text(title).type(.body).foregroundStyle(p.ink)
            }.contentShape(Rectangle())
        }.buttonStyle(.plain)
    }
}
