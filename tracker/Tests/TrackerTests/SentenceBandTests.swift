import XCTest
import CoreGraphics
@testable import speakeasy_track

// Implementer tests for the Phase 3 fold and text bands. The vectors are shared with tests/test_text_fold.py.
final class TextFoldTests: XCTestCase {
    func testSharedVectors() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("../../../tests/text_fold_vectors.json")
        let vectors = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [[String: Any]]
        XCTAssertGreaterThan(vectors.count, 10)
        for v in vectors {
            let input = v["input"] as! String
            let (key, offs) = fold(input)
            XCTAssertEqual(key, v["key"] as! String, "key of \(input.debugDescription)")
            XCTAssertEqual(offs.count, key.unicodeScalars.count)
            XCTAssertEqual(foldTokens(input), v["tokens"] as! [String], "tokens of \(input.debugDescription)")
            XCTAssertEqual(tokens(input), v["match_tokens"] as! [String], "match tokens of \(input.debugDescription)")
        }
    }
    func testOffsets() {
        XCTAssertEqual(Array(fold("classi\u{FB01}cation").offsets[5...8]), [5, 6, 6, 7])
        let text = Array("classi-\nfication".unicodeScalars)
        XCTAssertTrue(fold("classi-\nfication").offsets.allSatisfy { text[$0] != "-" && text[$0] != "\n" })
        XCTAssertEqual(Array(fold("Twenty-five dogs").offsets.prefix(2)), [0, 7])
    }
    func testWholeWordsOnly() {
        XCTAssertEqual(fold("someone").key, "someone")
        XCTAssertEqual(fold("twenty fives").key, "20fives")
        XCTAssertEqual(fold("Seventeen and seventy-one").key, "17and71")
        XCTAssertEqual(foldTokens("25% of"), ["25", "percent", "of"])
    }
}

final class BandTests: XCTestCase {
    func testOneFitMapsEveryLine() {
        let lines: [[Double]] = [[50, 100, 300, 110], [50, 112, 280, 122], [320, 100, 560, 110]]
        let r = mapBand(lines, fit: (2, 10, 20))
        XCTAssertEqual(r.count, 3)
        XCTAssertEqual(r[0], CGRect(x: 110, y: 220, width: 500, height: 20))
        XCTAssertEqual(r[2].minX, 650)                       // the other column keeps its own rect: no gutter fill
    }
    func testClampDropsOutsideAndHidesUnder30Percent() {
        let win = CGRect(x: 0, y: 0, width: 400, height: 300)
        let band = [CGRect(x: 10, y: 250, width: 300, height: 20), CGRect(x: 10, y: 290, width: 300, height: 20), CGRect(x: 10, y: 330, width: 300, height: 20)]
        let c = clampBand(band, to: win)
        XCTAssertEqual(c?.count, 2)
        XCTAssertEqual(c?[1].height, 10)
        XCTAssertNil(clampBand([CGRect(x: 10, y: 295, width: 300, height: 20), CGRect(x: 10, y: 320, width: 300, height: 20)], to: win))
    }
    func testBandPathHasOneShapePerLine() {
        let lines = [CGRect(x: 10, y: 10, width: 100, height: 12), CGRect(x: 200, y: 10, width: 100, height: 12)]
        let area = unionOf(lines)!.insetBy(dx: -4, dy: -2)
        let path = bandPath(lines, in: area)
        XCTAssertFalse(path.contains(CGPoint(x: 150 - area.minX, y: area.height / 2)))   // nothing across the gutter
        XCTAssertTrue(path.contains(CGPoint(x: 50 - area.minX, y: area.height / 2)))
    }
    func testCheckNeedsAnIntersectingLine() {
        func line(_ t: String, _ r: CGRect) -> OLine { OLine(text: t, toks: Set(tokens(t)), box: r) }
        let ocr = [line("the encoder maps an input sequence", CGRect(x: 100, y: 200, width: 300, height: 14))]
        let texts: [String?] = ["The encoder maps an input sequence"]
        XCTAssertTrue(bandCheck(ocr, band: [CGRect(x: 98, y: 198, width: 310, height: 18)], texts: texts, src: []))
        XCTAssertFalse(bandCheck(ocr, band: [CGRect(x: 98, y: 400, width: 310, height: 18)], texts: texts, src: []))
    }
    func testAnchorTextsByRect() {
        let anchors: [[Any]] = [[50.0, 100.0, 300.0, 110.0, "first line text here"], [50.0, 112.0, 280.0, 122.0, "second line"]]
        let t = bandTexts([[50, 112, 280, 122], [1, 2, 3, 4]], anchors: anchors)
        XCTAssertEqual(t[0], "second line"); XCTAssertNil(t[1])
    }
}
