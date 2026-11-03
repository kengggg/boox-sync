import XCTest

@testable import BooxCore

final class ScheduleTests: XCTestCase {
  func date(_ value: String) -> Date { ISO8601DateFormatter().date(from: value)! }
  func schedule() -> SyncSchedule {
    var value = SyncSchedule()
    value.limitedHours = true
    value.timeZoneID = "UTC"
    value.weekdays = [0, 1, 2, 3, 4]
    return value
  }

  func testDaytimeBoundariesAndWeekend() {
    let s = schedule()
    XCTAssertFalse(s.allows(date("2026-11-02T07:59:59Z")))
    XCTAssertTrue(s.allows(date("2026-11-02T08:00:00Z")))
    XCTAssertFalse(s.allows(date("2026-11-02T18:00:00Z")))
    XCTAssertFalse(s.allows(date("2026-11-07T12:00:00Z")))
    XCTAssertEqual(
      s.nextAllowed(onOrAfter: date("2026-11-06T19:00:00Z")), date("2026-11-09T08:00:00Z"))
  }

  func testOvernightBelongsToStartingDay() {
    var s = schedule()
    s.weekdays = [4]
    s.startMinute = 22 * 60
    s.endMinute = 6 * 60
    XCTAssertTrue(s.allows(date("2026-11-06T23:00:00Z")))
    XCTAssertTrue(s.allows(date("2026-11-07T05:59:00Z")))
    XCTAssertFalse(s.allows(date("2026-11-07T06:00:00Z")))
    XCTAssertFalse(s.allows(date("2026-11-07T23:00:00Z")))
  }

  func testSundayOvernightWrapsIntoMonday() {
    var s = schedule()
    s.weekdays = [6]
    s.startMinute = 22 * 60
    s.endMinute = 6 * 60
    XCTAssertTrue(s.allows(date("2026-11-09T02:00:00Z")))
    XCTAssertFalse(s.allows(date("2026-11-10T02:00:00Z")))
  }

  func testIntervalAdvancesFromCompletionAndDoesNotReplayMissedRuns() {
    let now = date("2026-11-02T09:00:00Z")
    let s = schedule()
    XCTAssertEqual(s.nextRun(after: now, now: now), now.addingTimeInterval(30))
    XCTAssertEqual(s.nextRun(after: now.addingTimeInterval(-500), now: now), now)
  }

  func testIntervalCannotRunPastWindow() {
    let s = schedule()
    let now = date("2026-11-02T17:59:50Z")
    XCTAssertEqual(s.nextRun(after: now, now: now), date("2026-11-03T08:00:00Z"))
  }

  func testPausedAutomaticSyncDoesNotChangeManualWindowPolicy() {
    var s = schedule()
    s.automatic = false
    XCTAssertNil(s.nextRun(after: nil))
    // Manual requests intentionally bypass nextRun/allows at the app boundary.
    XCTAssertFalse(s.allows(date("2026-11-02T22:00:00Z")))
  }

  func testDSTSpringForwardFindsFirstValidTime() {
    var s = schedule()
    s.timeZoneID = "America/New_York"
    s.weekdays = [6]
    s.startMinute = 150
    s.endMinute = 240
    XCTAssertEqual(
      s.nextAllowed(onOrAfter: date("2026-03-08T06:00:00Z")), date("2026-03-08T07:00:00Z"))
  }

  func testDSTFallBackBothOccurrencesAreAllowed() {
    var s = schedule()
    s.timeZoneID = "America/New_York"
    s.weekdays = [6]
    s.startMinute = 60
    s.endMinute = 120
    XCTAssertTrue(s.allows(date("2026-11-01T05:30:00Z")))
    XCTAssertTrue(s.allows(date("2026-11-01T06:30:00Z")))
  }

  func testInvalidWindowAndSettingsAreRejected() {
    var s = schedule()
    s.weekdays = []
    XCTAssertThrowsError(try s.validate())
    s.weekdays = [0]
    s.endMinute = s.startMinute
    XCTAssertThrowsError(try s.validate())
    var settings = AppSettings()
    settings.deviceSerial = "x; invalid"
    XCTAssertThrowsError(try settings.validate())
  }

  func testDSTFallBackCanReenterTheSecondWindow() {
    var s = schedule()
    s.timeZoneID = "America/New_York"
    s.weekdays = [6]
    s.startMinute = 90
    s.endMinute = 105
    XCTAssertEqual(
      s.nextAllowed(onOrAfter: date("2026-11-01T05:50:00Z")), date("2026-11-01T06:30:00Z"))
  }

  func testSettingsRoundTripAndDefaults() throws {
    var value = AppSettings()
    value.archivePath = "/tmp/Thai ภาษาไทย"
    value.deviceSerial = "TEST123"
    let decoded = try JSONDecoder().decode(AppSettings.self, from: JSONEncoder().encode(value))
    XCTAssertEqual(decoded, value)
    XCTAssertEqual(decoded.keepRevisions, 5)
    XCTAssertTrue(decoded.automaticPDF)
    XCTAssertTrue(decoded.showMenuBar)
    XCTAssertFalse(decoded.configured)
  }
}
