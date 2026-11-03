import AppKit
import BooxCore
import SwiftUI

struct RecoveryView: View {
  @ObservedObject var model: AppModel
  let note: Notebook
  @Environment(\.dismissWindow) private var dismissWindow
  @State private var revisions: [[String: Any]] = []
  @State private var loading = true
  @State private var message: String?

  var body: some View {
    VStack(alignment: .leading, spacing: 16) {
      Text("Recovery History").font(.title2.bold())
      Text(note.title).foregroundStyle(.secondary)
      Text(
        "Recover a previous source revision to a new .note file. The current archive and tablet are unchanged."
      ).font(.callout)
      if loading {
        ProgressView()
      } else if revisions.isEmpty {
        ContentUnavailableView("No previous revisions yet", systemImage: "clock.arrow.circlepath")
      } else {
        List(Array(revisions.enumerated()), id: \.offset) { _, revision in
          HStack {
            VStack(alignment: .leading) {
              Text(revision["date"] as? String ?? "Revision").font(.caption)
              Text(
                (revision["status"] as? String ?? "") + " · "
                  + ByteCountFormatter.string(
                    fromByteCount: (revision["bytes"] as? NSNumber)?.int64Value ?? 0,
                    countStyle: .file)
              ).font(.caption2).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Recover…") { Task { await recover(revision) } }.disabled(model.busy)
          }
        }
      }
      if let message { Text(message).font(.caption).textSelection(.enabled) }
      HStack {
        Spacer()
        Button("Done") { dismissWindow(id: "recovery") }.keyboardShortcut(.cancelAction)
      }
    }.padding(24).frame(width: 620, height: 430)
      .task {
        do {
          let data = try await model.run("history", extra: ["notebook": note.id])
          revisions = data["revisions"] as? [[String: Any]] ?? []
        } catch { message = model.operationMessage(error) }
        loading = false
      }
  }

  private func recover(_ revision: [String: Any]) async {
    let panel = NSSavePanel()
    panel.nameFieldStringValue = note.folder + " recovered.note"
    panel.message = "Choose a new filename for the recovered notebook."
    guard await model.present(panel) == .OK, let url = panel.url, let id = revision["id"] as? String
    else {
      return
    }
    do {
      _ = try await model.run(
        "restore", extra: ["notebook": note.id, "revision": id, "output": url.path])
      message = "Recovered to \(url.path)"
    } catch { message = model.operationMessage(error) }
  }
}
