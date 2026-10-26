// Area selector: drag a rectangle over any screen (crosshair, like Cmd-Shift-4), Esc cancels.
// Common practice reused: one borderless, non-opaque overlay window per screen above everything (the approach of
// Flameshot / open-source Swift capture tools), the selection captured in memory with ScreenCaptureKit (SCScreenshotManager,
// excluding our own windows), and Vision text recognition when the app exposes no Accessibility text.
import AppKit
import ScreenCaptureKit
import Vision

/// Global screen rect helpers: AppKit is bottom-left origin, CoreGraphics / AX / the tracker are top-left.
enum ScreenGeo {
    static var mainH: CGFloat { NSScreen.screens.first?.frame.height ?? 0 }
    static func toCG(_ r: NSRect) -> CGRect { CGRect(x: r.minX, y: mainH - r.maxY, width: r.width, height: r.height) }
    static func toNS(_ r: CGRect) -> NSRect { NSRect(x: r.minX, y: mainH - r.maxY, width: r.width, height: r.height) }
}

/// One screen of the armed overlay: ink dim at 12 %, the selection as a 1 pt glacier line over 12 % ice, and the size
/// readout in Plex Mono next to the cursor (design 6.6). It never becomes key: Esc arrives through a temporary hotkey (B3).
final class AreaOverlayView: NSView {
    var start: NSPoint?, current: NSPoint?
    var onDone: ((NSRect?, String) -> Void)?
    var onDrag: ((NSRect) -> Void)?
    let shape = CAShapeLayer(), readout = CATextLayer(), chip = CALayer()
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true; layer?.backgroundColor = nsRGB(Hex.ink, 0.12).cgColor
        shape.fillColor = nsRGB(Hex.ice, 0.12).cgColor
        shape.strokeColor = nsRGB(Hex.glacier).cgColor; shape.lineWidth = 1
        chip.backgroundColor = nsRGB(Hex.navy).cgColor; chip.cornerRadius = Radius.chip; chip.isHidden = true
        readout.font = TType.labelMono.nsFont; readout.fontSize = TType.labelMono.size; readout.foregroundColor = nsRGB(Hex.ice).cgColor
        readout.contentsScale = NSScreen.main?.backingScaleFactor ?? 2; readout.alignmentMode = .center
        chip.addSublayer(readout)
        layer?.addSublayer(shape); layer?.addSublayer(chip)
    }
    required init?(coder: NSCoder) { nil }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.activeAlways, .cursorUpdate, .mouseMoved, .inVisibleRect], owner: self, userInfo: nil))
    }
    override func cursorUpdate(with event: NSEvent) { NSCursor.crosshair.set() }
    override func mouseMoved(with event: NSEvent) { NSCursor.crosshair.set() }
    override func mouseDown(with e: NSEvent) { start = convert(e.locationInWindow, from: nil); current = start; redraw() }
    override func mouseDragged(with e: NSEvent) { current = convert(e.locationInWindow, from: nil); redraw() }
    override func mouseUp(with e: NSEvent) {
        current = convert(e.locationInWindow, from: nil)
        guard let r = sel(), r.width >= 8, r.height >= 8, let w = window else { onDone?(nil, "tiny"); return }
        onDone?(w.convertToScreen(convert(r, to: nil)), "read")
    }
    func sel() -> NSRect? {
        guard let a = start, let b = current else { return nil }
        return NSRect(x: min(a.x, b.x), y: min(a.y, b.y), width: abs(a.x - b.x), height: abs(a.y - b.y))
    }
    func redraw() {
        CATransaction.begin(); CATransaction.setDisableActions(true)
        shape.path = sel().map { CGPath(rect: $0.insetBy(dx: 0.5, dy: 0.5), transform: nil) }
        if let r = sel(), let c = current {
            readout.string = "\(Int(r.width)) × \(Int(r.height))"
            let w = CGFloat((readout.string as? String ?? "").count) * 7 + 12
            chip.frame = CGRect(x: c.x + 14, y: c.y - 26, width: w, height: 18); readout.frame = chip.bounds.insetBy(dx: 0, dy: 1)
            chip.isHidden = false
            if let win = window { onDrag?(win.convertToScreen(convert(r, to: nil))) }
        }
        CATransaction.commit()
    }
}

final class OverlayPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class AreaSelector {
    var windows: [NSWindow] = []
    var finish: ((NSRect?, String) -> Void)? = nil
    var active: Bool { !windows.isEmpty }
    var onDrag: ((CGRect) -> Void)? = nil           // the live rectangle (global top-left): the bar's collision rule 2

    /// Shows the overlay without activating Speakeasy; calls back with the rect in global top-left (CG) coordinates and the
    /// result (`read`, `tiny`, `cancel-esc`, `cancel-click`, `cancel-hotkey`).
    func pick(_ done: @escaping (CGRect?, String) -> Void) {
        guard windows.isEmpty else { return }
        var finished = false
        finish = { [weak self] r, result in
            guard let self = self, !finished else { return }
            finished = true
            self.windows.forEach { $0.orderOut(nil) }; self.windows = []; self.finish = nil
            NSCursor.arrow.set()
            done(r.map(ScreenGeo.toCG), result)
        }
        for s in NSScreen.screens {
            let w = OverlayPanel(contentRect: s.frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            w.level = .screenSaver; w.isOpaque = false; w.backgroundColor = .clear; w.hasShadow = false
            w.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]; w.ignoresMouseEvents = false; w.hidesOnDeactivate = false
            w.acceptsMouseMovedEvents = true; w.sharingType = .none
            let v = AreaOverlayView(frame: NSRect(origin: .zero, size: s.frame.size))
            v.onDone = { [weak self] r, result in self?.finish?(r, result) }
            v.onDrag = { [weak self] r in self?.onDrag?(ScreenGeo.toCG(r)) }
            w.contentView = v; w.setFrame(s.frame, display: true)
            w.orderFrontRegardless(); windows.append(w)
        }
        NSCursor.crosshair.set()
    }
    func cancel(_ result: String) { finish?(nil, result) }

    /// The region as a CGImage, captured in memory (our own windows excluded). rect: global top-left points.
    static func capture(_ rect: CGRect, _ done: @escaping (CGImage?) -> Void) {
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { content, _ in
            guard let content = content,
                  let d = content.displays.first(where: { $0.frame.intersects(rect) && $0.frame.contains(CGPoint(x: rect.midX, y: rect.midY)) }) ?? content.displays.first
            else { return done(nil) }
            let mine = content.applications.filter { $0.processID == getpid() }
            let filter = SCContentFilter(display: d, excludingApplications: mine, exceptingWindows: [])
            let cfg = SCStreamConfiguration()
            let local = rect.intersection(d.frame).offsetBy(dx: -d.frame.minX, dy: -d.frame.minY)
            let scale = CGFloat(filter.pointPixelScale)
            cfg.sourceRect = local; cfg.width = Int(local.width * scale); cfg.height = Int(local.height * scale)
            cfg.showsCursor = false
            SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg) { img, _ in done(img) }
        }
    }

    /// Vision text lines of the crop, top to bottom, with screen rects (top-left points).
    static func ocr(_ img: CGImage, rect: CGRect) -> [(String, CGRect)] {
        let req = VNRecognizeTextRequest(); req.recognitionLevel = .accurate; req.usesLanguageCorrection = true
        try? VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
        let lines = (req.results ?? []).compactMap { o -> (String, CGRect)? in
            guard let t = o.topCandidates(1).first?.string, !t.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
            let b = o.boundingBox
            return (t, CGRect(x: rect.minX + b.minX * rect.width, y: rect.minY + (1 - b.maxY) * rect.height,
                              width: b.width * rect.width, height: b.height * rect.height))
        }
        // reading order: rows top to bottom, then left to right (two-column PDFs: left column first when columns don't overlap)
        return lines.sorted { a, b in abs(a.1.minY - b.1.minY) < min(a.1.height, b.1.height) * 0.5 ? a.1.minX < b.1.minX : a.1.minY < b.1.minY }
    }
}
