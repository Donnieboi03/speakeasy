// swift-tools-version: 5.9
// XCTest harness for the tracker. The source is src/tracker.swift (symlinked), the same file install.sh and app/build.sh compile.
import PackageDescription

let package = Package(
    name: "SpeakeasyTracker",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "speakeasy-track", path: "Sources/speakeasy-track",
                          linkerSettings: [.linkedFramework("AppKit"), .linkedFramework("Vision"), .linkedFramework("ApplicationServices")]),
        .testTarget(name: "TrackerTests", dependencies: ["speakeasy-track"], path: "Tests/TrackerTests"),
    ]
)
