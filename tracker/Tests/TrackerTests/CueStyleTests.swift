import XCTest
import CoreGraphics
@testable import speakeasy_track

// phase7-usable-app.md C5 check 5 / design.md 6.6: cue colours come from the tokens only.
final class CueStyleTests: XCTestCase {
    func rgba(_ c: CGColor?) -> [Int] { (c?.components ?? []).enumerated().map { i, v in i == 3 ? Int((v * 100).rounded()) : Int((v * 255).rounded()) } }

    func testBandIsIceFillWithGlacierTick() {
        let l = cueLook("text_band", darkPage: false)
        XCTAssertEqual(rgba(l.fill), [168, 216, 236, 38]); XCTAssertNil(l.stroke); XCTAssertEqual(l.radius, 2)
        XCTAssertEqual(rgba(l.tick), [31, 110, 153, 100])
    }
    func testDarkPageBandIsGlacierAndBoxStrokeIce() {
        XCTAssertEqual(rgba(cueLook("text_band", darkPage: true).fill), [31, 110, 153, 45])
        XCTAssertEqual(rgba(cueLook("figure_box", darkPage: true).stroke), [168, 216, 236, 100])
    }
    func testBoxOvalRowAndBackReference() {
        let box = cueLook("figure_box", darkPage: false)
        XCTAssertEqual(rgba(box.stroke), [31, 110, 153, 100]); XCTAssertEqual(rgba(box.fill), [168, 216, 236, 10])
        XCTAssertEqual(box.lineWidth, 2); XCTAssertEqual(box.radius, 3)
        let oval = cueLook("part_oval", darkPage: false)
        XCTAssertEqual(rgba(oval.stroke), [224, 137, 43, 100]); XCTAssertNil(oval.fill)
        let row = cueLook("table_row", darkPage: false)
        XCTAssertEqual(rgba(row.fill), [168, 216, 236, 38]); XCTAssertNil(row.tick)
        let ref = cueLook("back_ref", darkPage: false)
        XCTAssertEqual(rgba(ref.stroke), [31, 110, 153, 60]); XCTAssertEqual(ref.dash, [4, 3]); XCTAssertEqual(ref.lineWidth, 1)
    }
    func testRenderedBandKeepsBlackTextContrast() {
        // 38 % ice over a white page is about #DFF0F7: black text on it stays far above 4.5:1
        let a = 0.38, mix = [168.0, 216, 236].map { (a * $0 + (1 - a) * 255) / 255 }
        func lin(_ c: Double) -> Double { c <= 0.03928 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
        let L = 0.2126 * lin(mix[0]) + 0.7152 * lin(mix[1]) + 0.0722 * lin(mix[2])
        XCTAssertGreaterThan((L + 0.05) / 0.05, 4.5)
        XCTAssertNotNil(renderCue("text_band", darkPage: false))
    }
}
