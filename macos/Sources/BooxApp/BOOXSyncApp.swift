import AppKit
import BooxCore
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
  weak var model: AppModel?
  func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
  func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
    guard let model else { return .terminateNow }
    if model.busy {
      let alert = NSAlert()
      alert.messageText = "A sync operation is still running"
      alert.informativeText =
        "Finish the current operation before quitting to keep the latest notebook update complete."
      alert.addButton(withTitle: "Finish and Quit")
      alert.addButton(withTitle: "Keep Running")
      if alert.runModal() == .alertFirstButtonReturn {
        // The background sync can finish while the confirmation is open.
        if !model.busy {
          model.stop()
          return .terminateNow
        }
        model.quitWhenFinished = true
        return .terminateLater
      }
      return .terminateCancel
    }
    model.stop()
    return .terminateNow
  }
}

private enum MenuBarIcon {
  static let image: NSImage = {
    guard
      let url = Bundle.main.url(forResource: "MenuBarIcon", withExtension: "png"),
      let image = NSImage(contentsOf: url)
    else {
      let fallback =
        NSImage(
          systemSymbolName: "book.closed", accessibilityDescription: "BOOX Sync")
        ?? NSImage()
      fallback.isTemplate = true
      return fallback
    }
    image.isTemplate = true
    let height: CGFloat = 18
    let ratio = image.size.width / max(image.size.height, 1)
    image.size = NSSize(width: (height * ratio).rounded(), height: height)
    return image
  }()
}

@main struct BOOXSyncApp: App {
  @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
  @StateObject private var model = AppModel()
  var body: some Scene {
    Window("BOOX Sync", id: "main") {
      MainWindow(model: model)
        .onAppear {
          delegate.model = model
          model.start()
        }
    }
    .defaultSize(width: 1000, height: 690)
    .commands {
      CommandGroup(after: .newItem) {
        Button("Sync Now") { Task { await model.sync() } }
          .keyboardShortcut("r", modifiers: .command).disabled(!model.canSync)
      }
    }
    Settings {
      SettingsWindow(model: model)
    }
    // Avoid the accessibility graph cycle observed with a sheet attached to
    // the searchable split-view library on macOS 27.
    Window("Recovery History", id: "recovery") {
      if let note = model.historyNote {
        RecoveryView(model: model, note: note).id(note.id)
      } else {
        Text("Choose a notebook in the library to see its recovery history.").padding(24)
      }
    }
    .windowResizability(.contentSize)
    MenuBarExtra(
      isInserted: Binding(
        get: { model.settings.showMenuBar },
        set: { value in
          guard value != model.settings.showMenuBar else { return }
          var next = model.settings
          next.showMenuBar = value
          Task { try? await model.save(next) }
        })
    ) {
      StatusMenu(model: model)
    } label: {
      Image(nsImage: MenuBarIcon.image)
        .renderingMode(.template)
        .accessibilityLabel("BOOX Sync")
    }
  }
}

struct StatusMenu: View {
  @ObservedObject var model: AppModel
  @Environment(\.openWindow) private var openWindow
  var body: some View {
    Text(model.connectionTitle)
    Text(model.scheduleTitle)
    if model.lastReportStatus == "failed" { Text("Last sync finished with issues") }
    Divider()
    Button("Sync Now") { Task { await model.sync() } }.disabled(!model.canSync)
    Button(model.settings.schedule.automatic ? "Pause Automatic Sync" : "Resume Automatic Sync") {
      var next = model.settings
      next.schedule.automatic.toggle()
      Task { do { try await model.save(next) } catch { model.error = error.localizedDescription } }
    }.disabled(model.busy)
    Button("Open BOOX Sync") {
      openWindow(id: "main")
      NSApp.activate(ignoringOtherApps: true)
    }
    Button("Open Archive") { model.revealArchive() }.disabled(model.settings.archivePath.isEmpty)
    SettingsLink { Text("Settings…") }
    Divider()
    Button("Quit BOOX Sync") { NSApp.terminate(nil) }.keyboardShortcut("q")
  }
}
