import XCTest
import CoreGraphics
@testable import speakeasy_track

final class TrackerTests: XCTestCase {
    func line(_ t: String, _ r: CGRect) -> OLine { OLine(text: t, toks: Set(tokens(t)), box: r) }

    func testTokensFoldLigaturesAndCase() {
        // NFKC: the PDF ligature "ﬁ" must match OCR's "fi"
        XCTAssertEqual(tokens("The ﬁgure SHOWS signiﬁcant"), tokens("the figure shows significant"))
        XCTAssertTrue(tokens("a 3 of").contains("3"))          // single digits survive, single letters do not
        XCTAssertFalse(tokens("a 3 of").contains("a"))
    }

    func testDocMapFitsScaleAndOffset() {
        // page lines at x=100, y = 100, 130, ... mapped on screen with scale 1.5 and offset (40, 200)
        let texts = ["attention mechanisms allow modelling dependencies", "without regard to their distance",
                     "in the input or output sequences", "in all but a few cases however",
                     "such attention mechanisms are used together"]
        var anchors: [[Any]] = [], ocr: [OLine] = []
        for (i, t) in texts.enumerated() {
            let y0 = 100.0 + Double(i) * 30, y1 = y0 + 10
            anchors.append([100.0, y0, 400.0, y1, t])
            ocr.append(line(t, CGRect(x: 40 + 1.5 * 100, y: 200 + 1.5 * y0, width: 450, height: 15)))
        }
        let doc: [String: Any] = ["target": [100.0, 300.0, 300.0, 400.0], "anchors": anchors]
        guard let (r, n, rms) = docMap(ocr, doc) else { return XCTFail("no fit") }
        XCTAssertEqual(n, 5); XCTAssertLessThan(rms, 1)
        XCTAssertEqual(Double(r.minX), 190, accuracy: 1); XCTAssertEqual(Double(r.minY), 650, accuracy: 1)
        XCTAssertEqual(Double(r.width), 300, accuracy: 1); XCTAssertEqual(Double(r.height), 150, accuracy: 1)
    }

    func testDocMapRefusesTooFewAnchors() {
        let doc: [String: Any] = ["target": [0.0, 0.0, 10.0, 10.0], "anchors": [[0.0, 0.0, 10.0, 10.0, "only one anchor line here"]]]
        XCTAssertNil(docMap([line("only one anchor line here", CGRect(x: 0, y: 0, width: 10, height: 10))], doc))
    }

    func testRefindPicksTheSentenceLine() {
        let ls = [line("Results improved on every benchmark", CGRect(x: 0, y: 0, width: 300, height: 14)),
                  line("The encoder maps an input sequence", CGRect(x: 0, y: 20, width: 300, height: 14)),
                  line("to a sequence of continuous representations", CGRect(x: 0, y: 40, width: 300, height: 14))]
        let r = refind(ls, target: ["The encoder maps an input sequence to a sequence of continuous representations"], near: nil)
        XCTAssertNotNil(r)
        if let r = r { XCTAssertGreaterThanOrEqual(r.minY, 19); XCTAssertLessThanOrEqual(r.minY, 41) }
    }
}

final class LifetimeTests: XCTestCase {
    func testVerdict() {
        let alive: (pid_t) -> Bool = { $0 != 99 }
        XCTAssertNil(lifeVerdict(elapsed: 5, maxLife: 960, ppid: 50, parentPid: 50, sourcePid: 60, alive: alive))
        XCTAssertEqual(lifeVerdict(elapsed: 961, maxLife: 960, ppid: 50, parentPid: 50, sourcePid: 60, alive: alive), "max-life")
        XCTAssertEqual(lifeVerdict(elapsed: 5, maxLife: 960, ppid: 1, parentPid: 99, sourcePid: 60, alive: alive), "parent-exited")
        XCTAssertEqual(lifeVerdict(elapsed: 5, maxLife: 960, ppid: 1, parentPid: 50, sourcePid: 60, alive: alive), "orphaned")
        XCTAssertEqual(lifeVerdict(elapsed: 5, maxLife: 960, ppid: 50, parentPid: 50, sourcePid: 99, alive: alive), "source-exited")
    }

    // The real binary must die on SIGTERM, when its parent dies, and at its max lifetime, even while stuck.
    var bin: URL { Bundle(for: LifetimeTests.self).bundleURL.deletingLastPathComponent().appendingPathComponent("speakeasy-track") }
    func spawn(_ args: [String]) throws -> Process {
        let p = Process(); p.executableURL = bin; p.arguments = ["--selftest-hang", "1"] + args
        p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
        try p.run(); return p
    }
    func waitExit(_ p: Process, _ s: Double) -> Bool {
        let end = Date().addingTimeInterval(s)
        while p.isRunning && Date() < end { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL); return false }
        return true
    }
    func testExitsOnSigterm() throws {
        let p = try spawn([]); usleep(800_000)
        XCTAssertTrue(p.isRunning)
        p.terminate()
        XCTAssertTrue(waitExit(p, 4), "tracker ignored SIGTERM")
    }
    func testExitsAtMaxLife() throws {
        XCTAssertTrue(waitExit(try spawn(["--max-life", "1"]), 6), "tracker outlived --max-life")
    }
    func testExitsWhenParentDies() throws {
        let parent = Process(); parent.executableURL = URL(fileURLWithPath: "/bin/sleep"); parent.arguments = ["30"]; try parent.run()
        let p = try spawn(["--parent-pid", String(parent.processIdentifier)]); usleep(800_000)
        XCTAssertTrue(p.isRunning)
        parent.terminate(); parent.waitUntilExit()
        XCTAssertTrue(waitExit(p, 5), "tracker outlived its parent")
    }
}

final class KeyHideTests: XCTestCase {
    func testScreenshotShortcutsAreNotTabSwitches() {
        for kc: Int64 in [20, 21, 23, 22] { XCTAssertFalse(isTabSwitchKey(cmd: true, ctrl: false, shift: true, alt: false, keyCode: kc)) }   // Cmd-Shift-3/4/5/6
        XCTAssertTrue(isTabSwitchKey(cmd: true, ctrl: false, shift: false, alt: false, keyCode: 21))     // Cmd-4
        XCTAssertTrue(isTabSwitchKey(cmd: false, ctrl: true, shift: false, alt: false, keyCode: 48))     // Ctrl-Tab
        XCTAssertTrue(isTabSwitchKey(cmd: true, ctrl: false, shift: true, alt: false, keyCode: 30))      // Cmd-Shift-]
    }
    func testSamePageBringsTheBoxBack() {
        let page: Set<String> = Set((0..<50).map { "w\($0)" })
        XCTAssertTrue(sameContent(page.union(["x1", "x2"]), page))
        XCTAssertFalse(sameContent(Set((40..<90).map { "w\($0)" }), page))
    }
}
