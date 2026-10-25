// swift-tools-version: 5.9
// Speakeasy menu-bar app. Package layout follows open-wispr (MIT, github.com/human37/open-wispr); see NOTICE.
import PackageDescription

let package = Package(
    name: "Speakeasy",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/sindresorhus/KeyboardShortcuts", from: "2.0.0"),   // MIT
    ],
    targets: [
        .executableTarget(
            name: "Speakeasy",
            dependencies: ["KeyboardShortcuts"],
            path: "Sources/Speakeasy",
            linkerSettings: [.linkedFramework("AppKit"), .linkedFramework("ApplicationServices"), .linkedFramework("ScreenCaptureKit")]
        ),
    ]
)
