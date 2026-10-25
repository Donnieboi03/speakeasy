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
}

final class AreaOverlayView: NSView {
    var start: NSPoint?, current: NSPoint?
    var onDone: ((NSRect?) -> Void)?
    let shape = CAShapeLayer()
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true; layer?.backgroundColor = NSColor.black.withAlphaComponent(0.12).cgColor
        shape.fillColor = NSColor.systemBlue.withAlphaComponent(0.12).cgColor
        shape.strokeColor = NSColor.systemBlue.cgColor; shape.lineWidth = 1.5; shape.lineDashPattern = [5, 3]
        layer?.addSublayer(shape)
    }
    required init?(coder: NSCoder) { nil }
    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .crosshair) }
    override func mouseDown(with e: NSEvent) { start = convert(e.locationInWindow, from: nil); current = start; redraw() }
    override func mouseDragged(with e: NSEvent) { current = convert(e.locationInWindow, from: nil); redraw() }
    override func mouseUp(with e: NSEvent) {
        current = convert(e.locationInWindow, from: nil)
        guard let r = sel(), r.width >= 8, r.height >= 8, let w = window else { onDone?(nil); return }
        onDone?(w.convertToScreen(convert(r, to: nil)))
    }
    override func keyDown(with e: NSEvent) { if e.keyCode == 53 { onDone?(nil) } else { super.keyDown(with: e) } }   // Esc
    override func cancelOperation(_ sender: Any?) { onDone?(nil) }
    func sel() -> NSRect? {
        guard let a = start, let b = current else { return nil }
        return NSRect(x: min(a.x, b.x), y: min(a.y, b.y), width: abs(a.x - b.x), height: abs(a.y - b.y))
    }
    func redraw() { shape.path = sel().map { CGPath(rect: $0, transform: nil) } }
}

final class KeyPanel: NSPanel { override var canBecomeKey: Bool { true } }

final class AreaSelector {
    var windows: [NSWindow] = []
    var prevApp: NSRunningApplication?
    var monitor: Any?

    /// Shows the overlay; calls back with the chosen rect in global top-left (CG) coordinates, or nil when cancelled.
    func pick(_ done: @escaping (CGRect?) -> Void) {
        guard windows.isEmpty else { return }
        prevApp = NSWorkspace.shared.frontmostApplication
        var finished = false
        let finish: (NSRect?) -> Void = { [weak self] r in
            guard let self = self, !finished else { return }
            finished = true
            self.windows.forEach { $0.orderOut(nil) }; self.windows = []
            if let m = self.monitor { NSEvent.removeMonitor(m); self.monitor = nil }
            self.prevApp?.activate()                                  // give focus back to the document before reading
            done(r.map(ScreenGeo.toCG))
        }
        for s in NSScreen.screens {
            let w = KeyPanel(contentRect: s.frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            w.level = .screenSaver; w.isOpaque = false; w.backgroundColor = .clear; w.hasShadow = false
            w.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]; w.ignoresMouseEvents = false
            let v = AreaOverlayView(frame: NSRect(origin: .zero, size: s.frame.size)); v.onDone = finish
            w.contentView = v; w.setFrame(s.frame, display: true)
            w.orderFrontRegardless(); windows.append(w)
        }
        NSApp.activate(ignoringOtherApps: true)
        let main = windows.first(where: { $0.frame.contains(NSEvent.mouseLocation) }) ?? windows.first
        main?.makeKey(); main?.makeFirstResponder(main?.contentView)
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { e in if e.keyCode == 53 { finish(nil); return nil }; return e }
        NSCursor.crosshair.set()
    }

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
