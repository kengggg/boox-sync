import XCTest

@testable import BooxCore

final class LibraryTests: XCTestCase {
  func note(_ presence: String? = nil, folder: String = "Notebook") -> Notebook {
    var json: [String: Any] = ["id": "TEST:abc", "folder": folder]
    if let presence { json["presence"] = presence }
    return Notebook(json: json)!
  }
  func testDefaultViewUsesVerifiedActiveState() {
    XCTAssertTrue(LibraryScope.onTablet.includes(note("active")))
    for state in ["inactive", "absent", "other_device", "unknown", "future-state"] {
      XCTAssertFalse(LibraryScope.onTablet.includes(note(state)))
    }
    XCTAssertFalse(LibraryScope.onTablet.includes(note()))
    XCTAssertTrue(LibraryScope.all.includes(note()))
  }
  func testArchivedScopePreservesInactiveAbsentAndOtherDeviceCopies() {
    for state in ["inactive", "absent", "other_device"] {
      XCTAssertTrue(LibraryScope.archived.includes(note(state)))
      XCTAssertFalse(LibraryScope.archived.includes(note(state, folder: "")))
    }
    XCTAssertFalse(LibraryScope.archived.includes(note("active")))
    XCTAssertFalse(LibraryScope.archived.includes(note()))
  }
  func testFailedFirstTransferRemainsOnTabletWithoutLocalActions() {
    let unsaved = note("active", folder: "")
    XCTAssertTrue(LibraryScope.onTablet.includes(unsaved))
    XCTAssertFalse(unsaved.hasArchive)
  }
}
