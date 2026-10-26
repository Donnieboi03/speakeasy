import XCTest
import CoreGraphics
@testable import speakeasy_track

// phase5-parts.md A2.7: the first draw after a scroll uses the current page->screen fit, not the capture-time rect.
final class FirstDrawTests: XCTestCase {
    let capture = (2.0, 10.0, 20.0)                     // page->screen at the hotkey: screen = 2 * page + (10, 20)
    let target: [Double] = [100, 200, 150, 240]

    func testScrollThenRefitMapsTheTargetWithTheFit() {
        // 300 pt scroll since the hotkey; OCR then found the page 10 pt lower than the deltas said (refit), stored
        // without the scroll offset: screen = 2 * page + (10, 30) + off
        let off = CGPoint(x: 0, y: -300), refit = (2.0, 10.0, 30.0)
        let captureRect = CGRect(x: 2 * 100 + 10, y: 2 * 200 + 20, width: 100, height: 80)
        let d = firstDraw(docTarget: target, docLines: [], rects: [], rect: captureRect, fit: refit, off: off)
        XCTAssertEqual(d.src, "page-fit")
        let want = CGRect(x: 210, y: 2 * 200 + 30 - 300, width: 100, height: 80)
        XCTAssertEqual(d.rect!.minX, want.minX, accuracy: 2); XCTAssertEqual(d.rect!.minY, want.minY, accuracy: 2)
        XCTAssertEqual(d.rect!.width, want.width, accuracy: 2); XCTAssertEqual(d.rect!.height, want.height, accuracy: 2)
    }

    func testBandLinesUseTheFitToo() {
        let lines: [[Double]] = [[50, 100, 300, 110], [50, 112, 280, 122]]
        let d = firstDraw(docTarget: [50, 100, 300, 122], docLines: lines, rects: [CGRect(x: 0, y: 0, width: 5, height: 5)], rect: nil,
                          fit: capture, off: CGPoint(x: 0, y: -300))
        XCTAssertEqual(d.src, "page-fit")
        XCTAssertEqual(d.band.count, 2)
        XCTAssertEqual(d.band[0], CGRect(x: 110, y: 220 - 300, width: 500, height: 20))
    }

    func testNoFitIsTheCaptureRectPlusTheScroll() {
        let r = CGRect(x: 210, y: 420, width: 100, height: 80)
        let d = firstDraw(docTarget: target, docLines: [], rects: [], rect: r, fit: nil, off: CGPoint(x: 0, y: -300))
        XCTAssertEqual(d.src, "capture+offset")
        XCTAssertEqual(d.rect, CGRect(x: 210, y: 120, width: 100, height: 80))
    }
}
