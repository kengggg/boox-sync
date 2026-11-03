import Foundation

public struct AppSettings: Codable, Equatable {
  public var version = 1
  public var deviceSerial = ""
  public var archivePath = ""
  public var archiveID = ""
  public var archiveBookmark: Data?
  public var schedule = SyncSchedule()
  public var automaticPDF = true
  public var keepRevisions = 5
  public var adbTimeoutSeconds = 120
  public var showMenuBar = true
  public var notifyErrors = true
  public var notifySuccess = false

  public init() {}
  public var configured: Bool {
    !deviceSerial.isEmpty && !archivePath.isEmpty && !archiveID.isEmpty
  }

  public func validate() throws {
    try schedule.validate()
    guard (1...100).contains(keepRevisions) else {
      throw SettingsError("Keep between 1 and 100 previous revisions.")
    }
    guard (10...600).contains(adbTimeoutSeconds) else {
      throw SettingsError("Choose a connection timeout between 10 and 600 seconds.")
    }
    if !deviceSerial.isEmpty
      && deviceSerial.range(of: "^[A-Za-z0-9]+$", options: .regularExpression) == nil
    {
      throw SettingsError("Select a USB device with a valid serial number.")
    }
    if !archivePath.isEmpty && !archivePath.hasPrefix("/") {
      throw SettingsError("Choose an absolute archive folder path.")
    }
  }

  public var engineConfig: [String: Any] {
    [
      "device_serial": deviceSerial, "archive_dir": archivePath, "archive_id": archiveID,
      "poll_seconds": schedule.intervalSeconds, "adb_timeout_seconds": adbTimeoutSeconds,
      "keep_revisions": keepRevisions, "render_after_sync": automaticPDF,
    ]
  }
}

public struct Device: Identifiable, Equatable {
  public let serial: String
  public let state: String
  public let model: String
  public var id: String { serial }
  public init?(json: [String: Any]) {
    guard let serial = json["serial"] as? String, let state = json["state"] as? String else {
      return nil
    }
    self.serial = serial
    self.state = state
    self.model = json["model"] as? String ?? "Android device"
  }
}

public enum NotebookPresence: String {
  case active, inactive, absent, unknown
  case otherDevice = "other_device"

  public var label: String {
    switch self {
    case .active: return "On tablet"
    case .inactive: return "Archived"
    case .absent: return "No longer listed"
    case .unknown: return "Not checked"
    case .otherDevice: return "Other tablet"
    }
  }
}

public enum LibraryScope: String, CaseIterable, Identifiable {
  case onTablet = "On tablet"
  case archived = "Archived"
  case all = "All"
  public var id: String { rawValue }
  public func includes(_ notebook: Notebook) -> Bool {
    switch self {
    case .onTablet: return notebook.presence == .active
    case .archived: return notebook.isArchived
    case .all: return true
    }
  }
}

public struct Notebook: Identifiable {
  public let id: String
  public let title: String
  public let folder: String
  public let status: String
  public let reasons: [String]
  public let error: String?
  public let pages: Int?
  public let syncedAt: String
  public let presence: NotebookPresence
  public let hasArchive: Bool
  public var isArchived: Bool {
    hasArchive && [.inactive, .absent, .otherDevice].contains(presence)
  }
  public init?(json: [String: Any]) {
    guard let id = json["id"] as? String, let folder = json["folder"] as? String else { return nil }
    self.id = id
    self.folder = folder
    presence = NotebookPresence(rawValue: json["presence"] as? String ?? "") ?? .unknown
    hasArchive = !folder.isEmpty && (json["has_archive"] as? Bool ?? true)
    title = json["title"] as? String ?? "Untitled"
    status = json["status"] as? String ?? "pending"
    reasons = json["reasons"] as? [String] ?? []
    error = json["error"] as? String
    pages = json["pages"] as? Int
    syncedAt = json["synced_at"] as? String ?? ""
  }
}
