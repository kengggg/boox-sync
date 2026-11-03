import Darwin
import Foundation

/// Owns one helper per request. Only versioned JSON crosses the process boundary.
public final class EngineClient: @unchecked Sendable {
  struct Limits {
    var idle: TimeInterval
    var total: TimeInterval
    var terminationGrace: TimeInterval = 2
    var maxLineBytes = 16 * 1024 * 1024

    static func forRequest(_ request: [String: Any]) -> Limits {
      switch request["command"] as? String {
      case "devices", "health": return Limits(idle: 20, total: 30)
      case "sync", "view", "render_file", "copy_archive", "restore":
        let config = request["config"] as? [String: Any]
        let adbTimeout = config?["adb_timeout_seconds"] as? Double ?? 120
        return Limits(idle: max(900, adbTimeout * 3), total: 4 * 60 * 60)
      default: return Limits(idle: 60, total: 120)
      }
    }
  }

  private final class Operation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    func cancel() {
      lock.lock()
      cancelled = true
      lock.unlock()
    }
    func checkCancellation() throws {
      lock.lock()
      let value = cancelled
      lock.unlock()
      if value { throw CancellationError() }
    }
  }

  private struct Envelope: Decodable {
    enum Event: String, Decodable { case progress, result }
    let version: Int
    let event: Event
    let ok: Bool?
    let error: String?
    enum CodingKeys: String, CodingKey {
      case version = "protocol"
      case event, ok, error
    }
  }

  private let lock = NSLock()
  private var operations: [UUID: Operation] = [:]
  private let executable: URL
  private let arguments: [String]
  private let limitsOverride: Limits?

  public convenience init() {
    self.init(
      executable: Bundle.main.bundleURL.appendingPathComponent(
        "Contents/Helpers/BOOXEngine.app/Contents/MacOS/boox-engine"))
  }

  init(executable: URL, arguments: [String] = [], limits: Limits? = nil) {
    self.executable = executable
    self.arguments = arguments
    limitsOverride = limits
  }

  public func call(_ request: [String: Any], progress: @escaping ([String: Any]) -> Void = { _ in })
    async throws -> [String: Any]
  {
    let id = UUID()
    let operation = Operation()
    return try await withTaskCancellationHandler {
      try await withCheckedThrowingContinuation { continuation in
        lock.lock()
        operations[id] = operation
        lock.unlock()
        DispatchQueue.global(qos: .utility).async {
          defer {
            self.lock.lock()
            self.operations.removeValue(forKey: id)
            self.lock.unlock()
          }
          do {
            continuation.resume(returning: try self.perform(request, operation, progress: progress))
          } catch {
            continuation.resume(throwing: error)
          }
        }
      }
    } onCancel: {
      operation.cancel()
    }
  }

  private func perform(
    _ request: [String: Any], _ operation: Operation, progress: @escaping ([String: Any]) -> Void
  ) throws -> [String: Any] {
    try operation.checkCancellation()
    guard FileManager.default.isExecutableFile(atPath: executable.path) else {
      throw SettingsError("The bundled sync engine is missing. Reinstall BOOX Sync and try again.")
    }
    var request = request
    request["protocol"] = 1
    var encoded = try JSONSerialization.data(withJSONObject: request)
    encoded.append(0x0a)
    guard encoded.count <= 1024 * 1024 else {
      throw SettingsError("The sync engine request is too large.")
    }
    let limits = limitsOverride ?? Limits.forRequest(request)
    let process = Process()
    process.executableURL = executable
    process.arguments = arguments
    let input = Pipe()
    let output = Pipe()
    process.standardInput = input
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    var environment = ProcessInfo.processInfo.environment
    for key in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"] { environment.removeValue(forKey: key) }
    process.environment = environment
    let reader = output.fileHandleForReading
    let writer = input.fileHandleForWriting
    defer {
      try? reader.close()
      try? writer.close()
    }
    // Neither a helper that ignores stdin nor an inherited stdout pipe may block this worker.
    for fd in [reader.fileDescriptor, writer.fileDescriptor] {
      guard fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK) != -1 else {
        throw SettingsError("Could not prepare the sync engine connection.")
      }
    }
    guard fcntl(writer.fileDescriptor, F_SETNOSIGPIPE, 1) != -1 else {
      throw SettingsError("Could not prepare the sync engine connection.")
    }
    try operation.checkCancellation()
    try process.run()
    let pid = process.processIdentifier
    var ownsGroup = false
    defer { terminate(process, ownsGroup: ownsGroup, grace: limits.terminationGrace) }
    let started = ProcessInfo.processInfo.systemUptime
    var lastEvent = started
    var pending = Data()
    var final: [String: Any]?
    var succeeded = false
    var failure: String?
    var written = 0
    var eof = false
    var buffer = [UInt8](repeating: 0, count: 64 * 1024)

    while true {
      try operation.checkCancellation()
      let now = ProcessInfo.processInfo.systemUptime
      if now - started > limits.total || now - lastEvent > limits.idle {
        throw SettingsError(
          "The sync engine stopped responding and was stopped. Try again. If this repeats, export diagnostics from Settings."
        )
      }
      if process.isRunning, getpgid(pid) == pid { ownsGroup = true }
      if written < encoded.count {
        let count = encoded.withUnsafeBytes { bytes in
          Darwin.write(
            writer.fileDescriptor, bytes.baseAddress!.advanced(by: written), encoded.count - written
          )
        }
        if count > 0 {
          written += count
          if written == encoded.count { try writer.close() }
        } else if count < 0 && errno != EAGAIN && errno != EINTR {
          throw SettingsError(
            "The sync engine closed its connection before accepting the request. Try again.")
        }
      }
      if !eof {
        let count = Darwin.read(reader.fileDescriptor, &buffer, buffer.count)
        if count > 0 {
          pending.append(contentsOf: buffer.prefix(count))
          while let end = pending.firstIndex(of: 0x0a) {
            guard pending.distance(from: pending.startIndex, to: end) <= limits.maxLineBytes else {
              throw SettingsError("The sync engine returned an oversized response.")
            }
            let line = Data(pending[..<end])
            pending.removeSubrange(...end)
            guard final == nil,
              let envelope = try? JSONDecoder().decode(Envelope.self, from: line),
              envelope.version == 1,
              let value = try? JSONSerialization.jsonObject(with: line) as? [String: Any]
            else {
              throw SettingsError(
                "Invalid response from the sync engine. Reinstall BOOX Sync if this repeats.")
            }
            switch envelope.event {
            case .progress: progress(value)
            case .result:
              guard let ok = envelope.ok,
                !ok || value["data"] is [String: Any]
              else { throw SettingsError("The sync engine returned an incomplete result.") }
              final = value
              succeeded = ok
              failure = envelope.error
            }
            lastEvent = ProcessInfo.processInfo.systemUptime
          }
          guard pending.count <= limits.maxLineBytes else {
            throw SettingsError("The sync engine returned an oversized response.")
          }
          continue
        } else if count == 0 {
          eof = true
        } else if errno != EAGAIN && errno != EINTR {
          throw SettingsError("The sync engine connection could not be read. Try again.")
        }
      }
      if eof && !process.isRunning { break }
      // Polling is bounded even after EOF or when a descendant keeps the pipe open.
      var descriptor = pollfd(
        fd: eof ? -1 : reader.fileDescriptor, events: Int16(POLLIN), revents: 0)
      _ = Darwin.poll(&descriptor, 1, 50)
    }
    guard pending.isEmpty, let final else {
      throw SettingsError(
        "The sync engine stopped without a complete result. Try again; recovery history is available in Notebooks."
      )
    }
    guard succeeded, process.terminationStatus == 0 else {
      throw SettingsError(
        failure ?? "The sync operation failed. Try again; see Activity for details.")
    }
    return final["data"] as! [String: Any]  // Validated before accepting the final envelope.
  }

  private func terminate(_ process: Process, ownsGroup: Bool, grace: TimeInterval) {
    let pid = process.processIdentifier
    let group = ownsGroup || (process.isRunning && getpgid(pid) == pid)
    guard group || process.isRunning else { return }
    let target = group ? -pid : pid
    _ = Darwin.kill(target, SIGTERM)
    let deadline = ProcessInfo.processInfo.systemUptime + grace
    while ProcessInfo.processInfo.systemUptime < deadline {
      if !process.isRunning && (!group || Darwin.kill(target, 0) != 0) { return }
      Thread.sleep(forTimeInterval: 0.02)
    }
    _ = Darwin.kill(target, SIGKILL)
    // Give the killed writer time to release its archive lock before a retry.
    // Foundation reaps asynchronously, so even this final wait must be bounded.
    let reapDeadline = ProcessInfo.processInfo.systemUptime + 1
    while process.isRunning && ProcessInfo.processInfo.systemUptime < reapDeadline {
      Thread.sleep(forTimeInterval: 0.02)
    }
  }

  public func stop() {
    lock.lock()
    let active = Array(operations.values)
    lock.unlock()
    for operation in active { operation.cancel() }
  }
}
