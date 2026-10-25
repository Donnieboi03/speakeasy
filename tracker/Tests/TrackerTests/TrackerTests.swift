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
