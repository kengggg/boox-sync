import BooxCore
import Darwin
import Foundation

/// The UI owns the process lifetime. Pipes carry versioned JSON, never shell commands.
final class EngineClient: @unchecked Sendable {
  private let lock = NSLock()
  private var processes: [Int32: Process] = [:]
  let executable: URL

  init() {
    executable = Bundle.main.bundleURL.appendingPathComponent(
      "Contents/Helpers/BOOXEngine.app/Contents/MacOS/boox-engine")
  }

  func call(_ request: [String: Any], progress: @escaping ([String: Any]) -> Void = { _ in })
    async throws -> [String: Any]
  {
    try await withCheckedThrowingContinuation { continuation in
      DispatchQueue.global(qos: .utility).async {
        do { continuation.resume(returning: try self.perform(request, progress: progress)) } catch {
          continuation.resume(throwing: error)
        }
      }
    }
  }

  private func perform(_ request: [String: Any], progress: @escaping ([String: Any]) -> Void) throws
    -> [String: Any]
  {
    guard FileManager.default.isExecutableFile(atPath: executable.path) else {
      throw SettingsError("The bundled sync engine is missing. Rebuild or reinstall BOOX Sync.")
    }
    let process = Process()
    process.executableURL = executable
    let input = Pipe()
    let output = Pipe()
    process.standardInput = input
    process.standardOutput = output
    // Errors are returned in the JSON result. Avoid a second blocking pipe
    // reader competing with process workers on the shared dispatch pool.
    process.standardError = FileHandle.nullDevice
    var environment = ProcessInfo.processInfo.environment
    // The packaged engine must not import the developer's Python environment.
    for key in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"] { environment.removeValue(forKey: key) }
    process.environment = environment
    try process.run()
    lock.lock()
    processes[process.processIdentifier] = process
    lock.unlock()
    defer {
      if process.isRunning {
        if Darwin.kill(-process.processIdentifier, SIGTERM) != 0 { process.terminate() }
      }
      lock.lock()
      processes.removeValue(forKey: process.processIdentifier)
      lock.unlock()
    }
    var request = request
    request["protocol"] = 1
    var encoded = try JSONSerialization.data(withJSONObject: request)
    encoded.append(0x0a)
    try input.fileHandleForWriting.write(contentsOf: encoded)
    try input.fileHandleForWriting.close()
    var pending = Data()
    var final: [String: Any]?
    while true {
      let chunk = output.fileHandleForReading.availableData
      if chunk.isEmpty { break }
      pending.append(chunk)
      guard pending.count < 16 * 1024 * 1024 else {
        process.terminate()
        throw SettingsError("The sync engine returned an oversized response.")
      }
      while let end = pending.firstIndex(of: 0x0a) {
        let line = pending[..<end]
        pending.removeSubrange(...end)
        guard let event = try JSONSerialization.jsonObject(with: line) as? [String: Any],
          event["protocol"] as? Int == 1
        else { throw SettingsError("Invalid response from the sync engine.") }
        if event["event"] as? String == "progress" { progress(event) }
        if event["event"] as? String == "result" { final = event }
      }
    }
    process.waitUntilExit()
    guard let final else {
      throw SettingsError(
        "The sync engine stopped unexpectedly. Your previous archive is retained; try again.")
    }
    guard final["ok"] as? Bool == true, process.terminationStatus == 0 else {
      throw SettingsError(final["error"] as? String ?? "The sync operation failed.")
    }
    return final["data"] as? [String: Any] ?? [:]
  }

  func stop() {
    lock.lock()
    let active = Array(processes.values)
    lock.unlock()
    for process in active where process.isRunning {
      // engine_entry creates a session; this also stops its ADB clients.
      if Darwin.kill(-process.processIdentifier, SIGTERM) != 0 { process.terminate() }
    }
  }
}
