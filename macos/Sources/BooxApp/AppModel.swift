import AppKit
import BooxCore
import ServiceManagement
import SwiftUI
import UserNotifications

@MainActor final class AppModel: ObservableObject {
  @Published var settings: AppSettings
  @Published var devices: [Device] = []
  @Published var notebooks: [Notebook] = []
  @Published var inventoryCheckedAt: Date?
  var onTabletCount: Int { notebooks.filter { $0.presence == .active }.count }
  var archivedCount: Int { notebooks.filter { $0.isArchived }.count }
  var librarySummary: String {
    inventoryCheckedAt == nil
      ? "\(notebooks.count) saved notebooks · tablet list not checked"
      : "\(onTabletCount) on tablet · \(archivedCount) archived"
  }
  @Published var outcomes: [[String: Any]] = []
  @Published var busy = false
  @Published var phase = "Ready"
  @Published var progress: Double = 0
  @Published var error: String?
  @Published var connectionError: String?
  @Published var lastFinished: Date?
  @Published var lastReportStatus = ""
  @Published var nextRun: Date?
  @Published var freeBytes: Int64 = 0
  @Published var historyNote: Notebook?
  @Published var loginEnabled = false
  @Published var quitWhenFinished = false
  let dataDirectory: URL
  let engine = EngineClient()
  private var timer: Timer?
  private var probing = false
  private var notifiedErrors = Set<String>()
  private var lastConnection: String?
  private var syncOnAttach = false
  private var started = false
  private var observations: [NSObjectProtocol] = []

  init() {
    let override = ProcessInfo.processInfo.environment["BOOX_SYNC_HOME"]
    dataDirectory =
      override.map { URL(fileURLWithPath: $0) }
      ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
      .appendingPathComponent("BOOX Sync")
    let path = dataDirectory.appendingPathComponent("native-settings.json")
    var loadError: String?
    if let data = try? Data(contentsOf: path) {
      do {
        settings = try JSONDecoder().decode(AppSettings.self, from: data)
        try settings.validate()
      } catch {
        settings = AppSettings()
        loadError =
          "Saved settings could not be read. Your archive is unchanged. Choose your device and folder again."
      }
    } else {
      settings = AppSettings()
    }
    error = loadError
    if let data = try? Data(contentsOf: dataDirectory.appendingPathComponent("native-status.json")),
      let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    {
      if let stamp = value["last_finished"] as? Double {
        lastFinished = Date(timeIntervalSince1970: stamp)
      }
      lastReportStatus = value["status"] as? String ?? ""
    }
    loginEnabled = SMAppService.mainApp.status == .enabled
  }

  var selectedDevice: Device? { devices.first { $0.serial == settings.deviceSerial } }
  var connectionTitle: String {
    if settings.deviceSerial.isEmpty { return "Choose a tablet" }
    if connectionError != nil { return "Connection unavailable" }
    guard let device = selectedDevice else { return "Not connected" }
    switch device.state {
    case "device": return "Connected"
    case "unauthorized": return "Approval needed on tablet"
    case "offline": return "Tablet is offline"
    default: return "Tablet in \(device.state) mode"
    }
  }
  var canSync: Bool {
    settings.configured && selectedDevice?.state == "device" && !busy && !quitWhenFinished
  }
  var scheduleTitle: String {
    if busy { return phase }
    if !settings.schedule.automatic { return "Automatic sync paused" }
    if !settings.configured { return "Finish setup to start syncing" }
    if selectedDevice?.state != "device" { return "Waiting for the tablet" }
    if !settings.schedule.allows(Date()) { return "Outside allowed hours" }
    if let nextRun { return "Next sync \(nextRun.formatted(date: .omitted, time: .shortened))" }
    return "Ready to sync"
  }
  var adbURL: URL { Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/adb") }

  func request(_ command: String, extra: [String: Any] = [:], using config: AppSettings? = nil)
    -> [String: Any]
  {
    var request: [String: Any] = [
      "command": command, "adb_path": adbURL.path,
      "data_dir": dataDirectory.path, "config": (config ?? settings).engineConfig,
    ]
    extra.forEach { request[$0] = $1 }
    return request
  }

  func start() {
    guard !started else { return }
    started = true
    resolveBookmark()
    Task {
      await refreshLibrary()
      await poll()
    }
    timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
      Task { @MainActor in await self?.poll() }
    }
    for name in [
      NSWorkspace.didWakeNotification, NSWorkspace.didMountNotification,
      NSWorkspace.didUnmountNotification,
    ] {
      observations.append(
        NSWorkspace.shared.notificationCenter.addObserver(forName: name, object: nil, queue: .main)
        { [weak self] _ in
          Task { @MainActor in
            self?.resolveBookmark()
            await self?.poll()
          }
        })
    }
    observations.append(
      NotificationCenter.default.addObserver(
        forName: NSNotification.Name.NSSystemTimeZoneDidChange, object: nil, queue: .main
      ) { [weak self] _ in
        Task { @MainActor in await self?.poll() }
      })
  }

  func resolveBookmark() {
    guard let bookmark = settings.archiveBookmark else { return }
    var stale = false
    if let url = try? URL(
      resolvingBookmarkData: bookmark, options: [.withoutUI, .withoutMounting], relativeTo: nil,
      bookmarkDataIsStale: &stale), FileManager.default.fileExists(atPath: url.path)
    {
      settings.archivePath = url.path
      if stale {
        settings.archiveBookmark = try? url.bookmarkData(
          options: .minimalBookmark, includingResourceValuesForKeys: nil, relativeTo: nil)
      }
    }
  }

  func poll() async {
    guard !probing else { return }
    probing = true
    defer { probing = false }
    do {
      let data = try await engine.call(request("devices"))
      devices = (data["devices"] as? [[String: Any]] ?? []).compactMap(Device.init)
      connectionError = nil
      let current = selectedDevice?.state
      if lastConnection != current {
        if current == "device" { syncOnAttach = true }
        if current == nil { notifiedErrors.removeAll() }
        lastConnection = current
      }
      nextRun = settings.schedule.nextRun(after: syncOnAttach ? nil : lastFinished)
      if canSync, let nextRun, nextRun <= Date() {
        // Keep connection probes independent while a long sync is running.
        Task { await self.sync() }
      }
    } catch { connectionError = error.localizedDescription }
  }

  func refreshLibrary() async {
    guard !settings.archiveID.isEmpty, !busy else { return }
    do { applyLibrary(try await engine.call(request("library"))) } catch {
      self.error = error.localizedDescription
    }
  }

  func applyLibrary(_ data: [String: Any]) {
    notebooks = (data["notebooks"] as? [[String: Any]] ?? []).compactMap(Notebook.init)
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let checked = data["inventory_checked_at"] as? String ?? ""
    inventoryCheckedAt = formatter.date(from: checked) ?? ISO8601DateFormatter().date(from: checked)
    freeBytes = (data["free_bytes"] as? NSNumber)?.int64Value ?? 0
    if let report = data["last_report"] as? [String: Any] {
      outcomes = report["outcomes"] as? [[String: Any]] ?? []
    }
  }

  func run(_ command: String, extra: [String: Any] = [:], using config: AppSettings? = nil)
    async throws -> [String: Any]
  {
    guard !busy else { throw SettingsError("Another operation is still running.") }
    busy = true
    progress = 0
    phase = command == "sync" ? "Reading notebooks…" : "Working…"
    defer {
      busy = false
      phase = "Ready"
      if quitWhenFinished {
        // Allow the caller to persist its final status/settings before exit.
        DispatchQueue.main.async {
          self.stop()
          NSApp.reply(toApplicationShouldTerminate: true)
        }
      }
    }
    return try await engine.call(request(command, extra: extra, using: config)) {
      [weak self] event in
      Task { @MainActor in
        guard let self else { return }
        let count = event["completed"] as? Int ?? 0
        let total = event["total"] as? Int ?? 0
        self.progress = total > 0 ? Double(count) / Double(total) : 0
        if let title = event["title"] as? String {
          self.phase = "Syncing \(title)"
        } else if event["phase"] as? String == "copy" {
          self.phase = "Verifying archive copy (\(count)/\(total))"
        }
      }
    }
  }

  func sync() async {
    guard canSync else { return }
    syncOnAttach = false
    do {
      error = nil
      let data = try await run("sync")
      applyLibrary(data)
      let report = data["report"] as? [String: Any] ?? [:]
      lastReportStatus = report["status"] as? String ?? "failed"
      if !["ok", "no_notes"].contains(lastReportStatus) {
        let failures = (report["outcomes"] as? [[String: Any]] ?? []).filter {
          $0["status"] as? String == "failed"
        }.count
        error =
          report["error"] as? String
          ?? "\(failures) notebook(s) could not be synced. Open Activity for details."
        notify(error!, key: "sync-issues", isError: true)
      } else {
        notify("Your notebooks are up to date.", key: UUID().uuidString, isError: false)
      }
    } catch {
      self.error = error.localizedDescription
      lastReportStatus = "failed"
      notify(error.localizedDescription, key: "sync-error", isError: true)
    }
    lastFinished = Date()
    nextRun = settings.schedule.nextRun(after: lastFinished)
    saveStatus()
  }

  func save(_ draft: AppSettings) async throws {
    guard !busy else {
      throw SettingsError("Wait for the current operation before changing settings.")
    }
    try draft.validate()
    guard draft != settings else { return }
    var next = draft
    if !next.archivePath.isEmpty
      && (next.archivePath != settings.archivePath || next.archiveID.isEmpty)
    {
      let result = try await run("prepare_archive", extra: ["path": next.archivePath])
      next.archiveID = result["archive_id"] as? String ?? ""
      next.archivePath = result["archive_dir"] as? String ?? next.archivePath
      next.archiveBookmark = try URL(fileURLWithPath: next.archivePath).bookmarkData(
        options: .minimalBookmark, includingResourceValuesForKeys: nil, relativeTo: nil)
    }
    try persist(next)
    if next.deviceSerial != settings.deviceSerial || next.archiveID != settings.archiveID {
      lastFinished = nil
      notebooks = []
      inventoryCheckedAt = nil
      outcomes = []
    }
    settings = next
    error = nil
    await refreshLibrary()
    nextRun = settings.schedule.nextRun(after: lastFinished)
    if settings.notifyErrors || settings.notifySuccess { requestNotifications() }
  }

  func persist(_ value: AppSettings) throws {
    try FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    try encoder.encode(value).write(
      to: dataDirectory.appendingPathComponent("native-settings.json"), options: .atomic)
  }

  func saveStatus() {
    try? FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
    let value: [String: Any] = [
      "last_finished": lastFinished?.timeIntervalSince1970 ?? 0, "status": lastReportStatus,
    ]
    if let data = try? JSONSerialization.data(withJSONObject: value) {
      try? data.write(
        to: dataDirectory.appendingPathComponent("native-status.json"), options: .atomic)
    }
  }

  func chooseFolder() async -> URL? {
    NSApp.activate()
    let panel = NSOpenPanel()
    panel.canChooseDirectories = true
    panel.canChooseFiles = false
    panel.canCreateDirectories = true
    panel.allowsMultipleSelection = false
    panel.prompt = "Use folder"
    panel.message = "Choose where BOOX Sync will keep notebooks, PDFs, and recovery history."
    return await panel.begin() == .OK ? panel.url : nil
  }

  func copyArchive(to target: URL) async {
    do {
      let data = try await run("copy_archive", extra: ["path": target.path])
      var next = settings
      next.archivePath = data["archive_dir"] as? String ?? target.path
      next.archiveID = data["archive_id"] as? String ?? next.archiveID
      next.archiveBookmark = try target.bookmarkData(
        options: .minimalBookmark, includingResourceValuesForKeys: nil, relativeTo: nil)
      try persist(next)
      settings = next
      await refreshLibrary()
    } catch { self.error = error.localizedDescription }
  }

  func open(_ note: Notebook) async {
    do {
      let data = try await run("view", extra: ["notebook": note.id])
      if let path = data["path"] as? String { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
      await refreshLibrary()
    } catch { self.error = error.localizedDescription }
  }

  func revealArchive() {
    if !settings.archivePath.isEmpty {
      NSWorkspace.shared.open(URL(fileURLWithPath: settings.archivePath))
    }
  }
  func reveal(_ note: Notebook) {
    NSWorkspace.shared.activateFileViewerSelecting([
      URL(fileURLWithPath: settings.archivePath).appendingPathComponent(note.folder)
    ])
  }

  func exportDiagnostics() async {
    NSApp.activate()
    let panel = NSSavePanel()
    panel.nameFieldStringValue = "BOOX-Sync-Diagnostics.json"
    guard await panel.begin() == .OK, let url = panel.url else { return }
    do {
      let data = try await run("diagnostics")
      try JSONSerialization.data(withJSONObject: data, options: [.prettyPrinted, .sortedKeys])
        .write(to: url, options: .atomic)
    } catch { self.error = error.localizedDescription }
  }

  func setLogin(_ value: Bool) {
    do {
      if value {
        try SMAppService.mainApp.register()
      } else {
        try SMAppService.mainApp.unregister()
      }
      loginEnabled = SMAppService.mainApp.status == .enabled
      if value && !loginEnabled { SMAppService.openSystemSettingsLoginItems() }
    } catch { self.error = error.localizedDescription }
  }

  func requestNotifications() {
    UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
  }

  func notify(_ message: String, key: String, isError: Bool) {
    guard isError ? settings.notifyErrors : settings.notifySuccess, !notifiedErrors.contains(key)
    else { return }
    notifiedErrors.insert(key)
    let content = UNMutableNotificationContent()
    content.title = "BOOX Sync"
    content.body = message
    UNUserNotificationCenter.current().add(
      UNNotificationRequest(identifier: key, content: content, trigger: nil))
  }

  func stop() {
    timer?.invalidate()
    engine.stop()
  }
}
