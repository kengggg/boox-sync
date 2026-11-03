import Darwin
import XCTest

@testable import BooxCore

final class EngineClientTests: XCTestCase {
  private let result = #"{"protocol":1,"event":"result","ok":true,"data":{"answer":42}}"#

  private func client(
    _ script: String, idle: Double = 2, total: Double = 5, maxLine: Int = 1024 * 1024
  )
    -> EngineClient
  {
    EngineClient(
      executable: URL(fileURLWithPath: "/usr/bin/env"),
      arguments: [
        "python3", "-u", "-c",
        "import os, sys, time, signal, json\ntry: os.setsid()\nexcept OSError: pass\n" + script,
      ],
      limits: .init(idle: idle, total: total, terminationGrace: 0.15, maxLineBytes: maxLine))
  }

  private func failure(_ client: EngineClient, contains: String, request: [String: Any] = [:]) async
  {
    let start = Date()
    do {
      _ = try await client.call(request)
      XCTFail("Expected failure")
    } catch {
      XCTAssertTrue(error.localizedDescription.contains(contains), error.localizedDescription)
    }
    XCTAssertLessThan(Date().timeIntervalSince(start), 8)
  }

  func testRequestAndSplitProgressResult() async throws {
    let worker = client(
      """
      request = json.loads(sys.stdin.readline())
      assert request['protocol'] == 1 and request['command'] == 'health'
      data = '{"protocol":1,"event":"progress","completed":1,"total":2}\\n'
      sys.stdout.write(data[:10]); sys.stdout.flush(); time.sleep(0.02)
      sys.stdout.write(data[10:]); sys.stdout.flush()
      print('\(result)')
      """)
    let progress = expectation(description: "progress")
    let data = try await worker.call(["command": "health"]) { event in
      XCTAssertEqual(event["completed"] as? Int, 1)
      progress.fulfill()
    }
    XCTAssertEqual(data["answer"] as? Int, 42)
    await fulfillment(of: [progress], timeout: 1)
  }

  func testRejectsMalformedProtocolAndFinalResults() async {
    for response in [
      "not json", #"{"protocol":true,"event":"result","ok":true,"data":{}}"#,
      #"{"protocol":2,"event":"result","ok":true,"data":{}}"#,
      #"{"protocol":1,"event":"unknown"}"#,
    ] {
      await failure(client("print('\(response)')"), contains: "Invalid response")
    }
    for response in [
      #"{"protocol":1,"event":"result","ok":true}"#,
      #"{"protocol":1,"event":"result","ok":true,"data":[]}"#,
      #"{"protocol":1,"event":"result","data":{}}"#,
    ] {
      await failure(client("print('\(response)')"), contains: "incomplete result")
    }
    await failure(client("print('\(result)'); print('\(result)')"), contains: "Invalid response")
    await failure(client("sys.stdout.write('\(result)')"), contains: "without a complete result")
    await failure(client("sys.exit(7)"), contains: "without a complete result")
    await failure(client("print('\(result)'); sys.exit(7)"), contains: "sync operation failed")
    await failure(client("print('x' * 2048)", maxLine: 1024), contains: "oversized response")
  }

  func testReturnsEngineError() async {
    await failure(
      client(
        #"print('{"protocol":1,"event":"result","ok":false,"error":"Archive disconnected"}'); sys.exit(1)"#
      ),
      contains: "Archive disconnected")
  }

  func testTimeoutCoversSilentHelperClosedOutputAndFinalWithoutExit() async {
    for script in [
      "time.sleep(30)", "os.close(1); time.sleep(30)",
      "print('\(result)'); time.sleep(30)",
      "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
    ] {
      await failure(client(script, idle: 0.4), contains: "stopped responding")
    }
  }

  func testTimeoutCoversBlockedRequestWrite() async {
    await failure(
      client("time.sleep(30)", idle: 0.4), contains: "stopped responding",
      request: ["padding": String(repeating: "x", count: 900_000)])
  }

  func testProgressDoesNotEvadeOverallDeadline() async {
    await failure(
      client(
        """
        while True:
            print('{"protocol":1,"event":"progress"}')
            time.sleep(0.05)
        """, idle: 0.5, total: 0.7), contains: "stopped responding")
  }

  func testCancellationStopsOwnedChildEvenWhenItIgnoresTermination() async throws {
    let worker = client(
      """
      signal.signal(signal.SIGTERM, signal.SIG_IGN)
      pid = os.fork()
      if pid == 0:
          time.sleep(30)
      else:
          print(json.dumps({'protocol':1, 'event':'progress', 'pid':pid}))
          time.sleep(30)
      """)
    let child = expectation(description: "child started")
    let pid = LockedPID()
    let task = Task {
      try await worker.call([:]) { event in
        pid.set(event["pid"] as! Int32)
        child.fulfill()
      }
    }
    await fulfillment(of: [child], timeout: 3)
    task.cancel()
    do {
      _ = try await task.value
      XCTFail("Expected cancellation")
    } catch {
      XCTAssertTrue(error is CancellationError)
    }
    // Allow the OS to reap the orphan; do not confuse asynchronous reaping with a live worker.
    for _ in 0..<100 {
      if Darwin.kill(pid.get(), 0) != 0 { break }
      try await Task.sleep(nanoseconds: 20_000_000)
    }
    XCTAssertEqual(Darwin.kill(pid.get(), 0), -1)
    let next = try await client("print('\(result)')").call([:])
    XCTAssertEqual(next["answer"] as? Int, 42)
  }

  func testInheritedPipeAfterParentExitIsBounded() async {
    await failure(
      client(
        """
        if os.fork() == 0:
            time.sleep(30)
        else:
            time.sleep(0.1)
            os._exit(0)
        """, idle: 0.4), contains: "stopped responding")
  }

  func testAlreadyCancelledTaskNeverLaunchesHelper() async {
    let task = Task {
      withUnsafeCurrentTask { $0?.cancel() }
      return try await client("print('\(result)')").call([:])
    }
    do {
      _ = try await task.value
      XCTFail("Expected cancellation")
    } catch {
      XCTAssertTrue(error is CancellationError)
    }
  }

  func testMissingExecutableIsActionable() async {
    await failure(
      EngineClient(executable: URL(fileURLWithPath: "/missing-boox-engine")), contains: "Reinstall")
  }

  func testStopCancelsConcurrentRequestsAndClientCanBeReused() async throws {
    let worker = client(
      """
      request = json.loads(sys.stdin.readline())
      if request.get('command') != 'quick':
          print('{"protocol":1,"event":"progress"}')
          time.sleep(30)
      print('\(result)')
      """)
    let ready = expectation(description: "both requests started")
    ready.expectedFulfillmentCount = 2
    let tasks = (0..<2).map { _ in
      Task { try await worker.call([:]) { _ in ready.fulfill() } }
    }
    await fulfillment(of: [ready], timeout: 3)
    worker.stop()
    for task in tasks {
      do {
        _ = try await task.value
        XCTFail("Expected cancellation")
      } catch {
        XCTAssertTrue(error is CancellationError)
      }
    }
    let data = try await worker.call(["command": "quick"])
    XCTAssertEqual(data["answer"] as? Int, 42)
  }

  func testPackagedEngineThroughNativeClient() async throws {
    guard let path = ProcessInfo.processInfo.environment["BOOX_TEST_ENGINE"] else {
      throw XCTSkip("Run after packaging with BOOX_TEST_ENGINE pointing to the bundled helper.")
    }
    let worker = EngineClient(executable: URL(fileURLWithPath: path))
    let health = try await worker.call(["command": "health"])
    XCTAssertFalse((health["version"] as? String ?? "").isEmpty)
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let archive = try await worker.call(["command": "prepare_archive", "path": root.path])
    XCTAssertNotNil(UUID(uuidString: archive["archive_id"] as? String ?? ""))
    let source = URL(fileURLWithPath: #filePath)
      .deletingLastPathComponent().deletingLastPathComponent()
      .deletingLastPathComponent().deletingLastPathComponent()
      .appendingPathComponent("tests/fixtures/ballpoint.note")
    let target = root.appendingPathComponent("Native client ภาษาไทย.pdf")
    let data = try await worker.call([
      "command": "render_file", "path": source.path, "output": target.path,
    ])
    XCTAssertEqual(data["path"] as? String, target.path)
    XCTAssertEqual(try Data(contentsOf: target).prefix(5), Data("%PDF-".utf8))
  }
}

private final class LockedPID: @unchecked Sendable {
  private let lock = NSLock()
  private var pid: Int32 = 0
  func set(_ value: Int32) {
    lock.lock()
    pid = value
    lock.unlock()
  }
  func get() -> Int32 {
    lock.lock()
    defer { lock.unlock() }
    return pid
  }
}
