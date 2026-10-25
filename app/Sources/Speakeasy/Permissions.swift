// Adapted from open-wispr (MIT, Copyright (c) 2026 human37, github.com/human37/open-wispr): Sources/OpenWisprLib/Permissions.swift.
// Speakeasy only checks silently at launch; the system prompts appear only when the user clicks a button in onboarding.
import AppKit
import ApplicationServices
import CoreGraphics

struct Permissions {
    static var accessibility: Bool { AXIsProcessTrusted() }
    static var screenRecording: Bool { CGPreflightScreenCaptureAccess() }
    static func promptAccessibility() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary
        AXIsProcessTrustedWithOptions(options)
    }
    static func promptScreenRecording() { _ = CGRequestScreenCaptureAccess() }
    static func openAccessibilitySettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") { NSWorkspace.shared.open(url) }
    }
    static func openScreenRecordingSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") { NSWorkspace.shared.open(url) }
    }
}
