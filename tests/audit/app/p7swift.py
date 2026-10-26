"""Swift helper sources for the Phase 7 holdout suite (compiled once into the temp root).
winlist: screens, pointer, frontmost app and on-screen windows (pid, layer, bounds, alpha, sharing) as JSON; never window titles.
ocr:     Vision text recognition of PNG files -> {path: [lines]}.
post:    synthetic mouse / key events (only used with --ui-input).
"""

WINLIST = r'''
import Cocoa
import CoreGraphics
_ = NSApplication.shared
var out: [String: Any] = [:]
out["primary_h"] = Double(NSScreen.screens.first?.frame.height ?? 0)
out["screens"] = NSScreen.screens.map { s -> [String: Any] in
  ["frame": [s.frame.origin.x, s.frame.origin.y, s.frame.width, s.frame.height],
   "visible": [s.visibleFrame.origin.x, s.visibleFrame.origin.y, s.visibleFrame.width, s.visibleFrame.height],
   "scale": Double(s.backingScaleFactor)]
}
let m = NSEvent.mouseLocation; out["mouse"] = [Double(m.x), Double(m.y)]
if let f = NSWorkspace.shared.frontmostApplication { out["front"] = ["pid": Int(f.processIdentifier), "bundle": f.bundleIdentifier ?? ""] }
let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
out["windows"] = info.map { w -> [String: Any] in
  let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
  return ["id": w[kCGWindowNumber as String] as? Int ?? 0, "pid": w[kCGWindowOwnerPID as String] as? Int ?? -1,
          "layer": w[kCGWindowLayer as String] as? Int ?? 0, "alpha": w[kCGWindowAlpha as String] as? Double ?? 1,
          "sharing": w[kCGWindowSharingState as String] as? Int ?? -1,
          "bounds": [b["X"] as? Double ?? 0, b["Y"] as? Double ?? 0, b["Width"] as? Double ?? 0, b["Height"] as? Double ?? 0]]
}
let d = try! JSONSerialization.data(withJSONObject: out, options: [])
print(String(data: d, encoding: .utf8)!)
'''

OCR = r'''
import Cocoa
import Vision
var res: [String: Any] = [:]
for p in CommandLine.arguments.dropFirst() {
  guard let img = NSImage(contentsOfFile: p) else { res[p] = NSNull(); continue }
  // composite onto mid grey so transparent areas do not read as black text backgrounds
  let sz = img.size
  let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(sz.width * 2), pixelsHigh: Int(sz.height * 2), bitsPerSample: 8,
                             samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
  NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
  NSColor(white: 0.5, alpha: 1).setFill(); NSRect(x: 0, y: 0, width: sz.width * 2, height: sz.height * 2).fill()
  img.draw(in: NSRect(x: 0, y: 0, width: sz.width * 2, height: sz.height * 2))
  NSGraphicsContext.restoreGraphicsState()
  guard let cg = rep.cgImage else { res[p] = NSNull(); continue }
  let req = VNRecognizeTextRequest(); req.recognitionLevel = .accurate; req.usesLanguageCorrection = false
  try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
  res[p] = (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }
}
let d = try! JSONSerialization.data(withJSONObject: res, options: [])
print(String(data: d, encoding: .utf8)!)
'''

PANEL = r'''
// panel <w> <h> <where: centre|left|right> <bottom offset pt>: an own non-activating NSPanel at statusBar level on the
// pointer's display, bottom-aligned to its visible frame, mouse-transparent, until killed. Prints "ready <windowNumber>".
import Cocoa
let a = CommandLine.arguments
// .prohibited: an unbundled .accessory helper still became the frontmost app in a live run, which is not what the panel stands in for
let app = NSApplication.shared; app.setActivationPolicy(.prohibited)
let w = Double(a[1])!, h = Double(a[2])!, off = Double(a[4])!
let m = NSEvent.mouseLocation
let s = NSScreen.screens.first(where: { NSMouseInRect(m, $0.frame, false) }) ?? NSScreen.screens[0]
let vf = s.visibleFrame
var x = vf.midX - w / 2
if a[3] == "left" { x = vf.minX } else if a[3] == "right" { x = vf.maxX - w }
let p = NSPanel(contentRect: NSRect(x: x, y: vf.minY + off, width: w, height: h), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
p.level = .statusBar; p.isOpaque = false; p.hasShadow = false; p.ignoresMouseEvents = true
p.backgroundColor = NSColor(calibratedRed: 0.55, green: 0.55, blue: 0.55, alpha: 0.35)
p.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
p.orderFrontRegardless()
print("ready \(p.windowNumber)"); fflush(stdout)
app.run()
'''

POST = r'''
import Cocoa
import ApplicationServices
let a = CommandLine.arguments
func pt(_ i: Int) -> CGPoint { CGPoint(x: Double(a[i])!, y: Double(a[i + 1])!) }
if a.count < 2 { exit(2) }
if a[1] == "trusted" { print(AXIsProcessTrusted() ? "1" : "0"); exit(0) }
let src = CGEventSource(stateID: .hidSystemState)
switch a[1] {
case "move":
  CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: pt(2), mouseButton: .left)?.post(tap: .cghidEventTap)
case "click":
  let p = pt(2)
  CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
  usleep(80000)
  CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
  usleep(60000)
  CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
case "drag":
  let p = pt(2), q = pt(4)
  CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
  for i in 1...20 {
    let r = CGPoint(x: p.x + (q.x - p.x) * Double(i) / 20, y: p.y + (q.y - p.y) * Double(i) / 20)
    CGEvent(mouseEventSource: src, mouseType: .leftMouseDragged, mouseCursorPosition: r, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(15000)
  }
  CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: q, mouseButton: .left)?.post(tap: .cghidEventTap)
case "key":
  // key <keycode> [ctrl,alt,cmd,shift]
  let code = CGKeyCode(Int(a[2])!)
  var fl: CGEventFlags = []
  if a.count > 3 { for m in a[3].split(separator: ",") { switch m { case "ctrl": fl.insert(.maskControl); case "alt": fl.insert(.maskAlternate); case "cmd": fl.insert(.maskCommand); case "shift": fl.insert(.maskShift); default: break } } }
  let d = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: true); d?.flags = fl; d?.post(tap: .cghidEventTap)
  usleep(40000)
  let u = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: false); u?.flags = fl; u?.post(tap: .cghidEventTap)
case "menu":
  // menu <pid>: open the app's status-item menu through Accessibility, print the item titles, cancel it
  let app = AXUIElementCreateApplication(pid_t(Int32(a[2])!))
  func attr(_ e: AXUIElement, _ n: String) -> CFTypeRef? { var v: CFTypeRef?; return AXUIElementCopyAttributeValue(e, n as CFString, &v) == .success ? v : nil }
  guard let bar = attr(app, "AXExtrasMenuBar"), let items = attr(bar as! AXUIElement, kAXChildrenAttribute) as? [AXUIElement], let item = items.first else { print("[]"); exit(0) }
  AXUIElementPerformAction(item, kAXPressAction as CFString); usleep(700000)
  var titles: [String] = []
  if let kids = attr(item, kAXChildrenAttribute) as? [AXUIElement], let menu = kids.first {
    for mi in (attr(menu, kAXChildrenAttribute) as? [AXUIElement]) ?? [] { titles.append((attr(mi, kAXTitleAttribute) as? String) ?? "") }
    AXUIElementPerformAction(menu, kAXCancelAction as CFString)
  }
  let d = try! JSONSerialization.data(withJSONObject: titles, options: []); print(String(data: d, encoding: .utf8)!)
default: exit(2)
}
usleep(50000)
'''
