import BooxCore
import SwiftUI

private enum Destination: String, CaseIterable, Identifiable {
  case overview = "Overview"
  case notebooks = "Notebooks"
  case activity = "Activity"
  var id: String { rawValue }
  var symbol: String {
    switch self {
    case .overview: return "rectangle.grid.2x2"
    case .notebooks: return "books.vertical"
    case .activity: return "clock.arrow.circlepath"
    }
  }
}

struct MainWindow: View {
  @ObservedObject var model: AppModel
  @State private var destination: Destination? = .overview

  var body: some View {
    NavigationSplitView {
      List(Destination.allCases, selection: $destination) { item in
        Label {
          Text(item.rawValue)
            .font(.system(size: 13, weight: .medium))
        } icon: {
          Image(systemName: item.symbol)
            .font(.system(size: 15, weight: .regular))
            .symbolRenderingMode(.hierarchical)
            .frame(width: 20)
        }
        .listItemTint(.monochrome)
        .padding(.vertical, 5)
        .tag(item)
      }
      .listStyle(.sidebar)
      .navigationSplitViewColumnWidth(min: 190, ideal: 210, max: 260)
      .safeAreaInset(edge: .bottom) {
        SidebarDeviceStatus(model: model)
          .padding(12)
      }
    } detail: {
      VStack(spacing: 0) {
        if let error = model.error {
          HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            Text(error).font(.callout).textSelection(.enabled).frame(
              maxWidth: .infinity, alignment: .leading)
            Button {
              model.error = nil
            } label: {
              Image(systemName: "xmark")
            }.buttonStyle(.plain).help("Dismiss")
          }.padding(14).background(Color.orange.opacity(0.10))
        }
        if !model.settings.configured {
          SetupView(model: model)
        } else {
          switch destination ?? .overview {
          case .overview: OverviewView(model: model)
          case .notebooks: LibraryView(model: model)
          case .activity: ActivityView(model: model)
          }
        }
        if model.busy {
          VStack(alignment: .leading, spacing: 7) {
            Text(model.phase).font(.caption).lineLimit(1)
            ProgressView(value: model.progress)
          }.padding().background(.bar)
        }
      }
      .navigationTitle(destination?.rawValue ?? "BOOX Sync")
      // Let the full-height split view draw one continuous sidebar edge.
      // The separate toolbar background overlaps that edge on macOS 27.
      .toolbarBackground(.hidden, for: .windowToolbar)
      .toolbar {
        ToolbarItemGroup {
          Button {
            model.revealArchive()
          } label: {
            Label("Open Archive", systemImage: "folder")
          }
          .disabled(model.settings.archivePath.isEmpty)
          .help("Open your notebook archive in Finder")
          SettingsLink { Label("Settings", systemImage: "gearshape") }.help("Settings")
          Button {
            Task { await model.sync() }
          } label: {
            Label("Sync Now", systemImage: "arrow.triangle.2.circlepath")
          }
          .disabled(!model.canSync)
          .help("Sync now (⌘R)")
        }
      }
    }
    .frame(minWidth: 860, minHeight: 580)
  }
}

private struct SidebarDeviceStatus: View {
  @ObservedObject var model: AppModel

  private var connected: Bool { model.selectedDevice?.state == "device" }
  private var statusColor: Color {
    switch model.selectedDevice?.state {
    case "device": return .green
    case "unauthorized", "offline": return .orange
    default: return .secondary
    }
  }
  private var tabletName: String {
    if let device = model.selectedDevice {
      return device.model.replacingOccurrences(
        of: "(?<=[a-z])(?=[A-Z0-9])", with: " ", options: .regularExpression)
    }
    return model.settings.deviceSerial.isEmpty ? "Your BOOX tablet" : "BOOX tablet"
  }

  var body: some View {
    VStack(alignment: .leading, spacing: 12) {
      HStack(spacing: 10) {
        Image(systemName: connected ? "ipad" : "cable.connector")
          .font(.system(size: 20, weight: .regular))
          .foregroundStyle(.secondary)
          .frame(width: 32, height: 36)
          .accessibilityHidden(true)
        VStack(alignment: .leading, spacing: 4) {
          Text(tabletName).font(.system(size: 12, weight: .semibold))
            .lineLimit(1).truncationMode(.middle)
          HStack(spacing: 5) {
            Circle().fill(statusColor).frame(width: 6, height: 6).accessibilityHidden(true)
            Text(model.connectionTitle).font(.caption).foregroundStyle(.secondary)
              .fixedSize(horizontal: false, vertical: true)
          }
        }
        Spacer(minLength: 0)
      }
      Divider()
      Label("Tablet stays unchanged", systemImage: "lock.shield")
        .font(.system(size: 10)).foregroundStyle(.secondary)
        .help("BOOX Sync only reads your tablet. It never changes or deletes its notebooks.")
    }
    .padding(12)
    .frame(maxWidth: .infinity, alignment: .leading)
    .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 10))
    .overlay {
      RoundedRectangle(cornerRadius: 10).strokeBorder(Color.primary.opacity(0.06), lineWidth: 0.5)
    }
    .accessibilityElement(children: .combine)
  }
}

struct SetupView: View {
  @ObservedObject var model: AppModel
  var body: some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 24) {
        Image(systemName: "book.closed").font(.system(size: 48)).foregroundStyle(.tint)
        VStack(alignment: .leading, spacing: 8) {
          Text("Your notebooks, safely on your Mac.").font(.largeTitle.bold())
          Text(
            "BOOX Sync keeps local copies, readable PDFs, and five previous revisions. Your tablet's notebooks stay untouched."
          )
          .font(.title3).foregroundStyle(.secondary)
        }
        GroupBox {
          VStack(alignment: .leading, spacing: 18) {
            setupStep(
              "1", "Connect your BOOX",
              "Use a USB data cable. Enable USB debugging on the tablet and accept this Mac when prompted."
            )
            setupStep(
              "2", "Choose a tablet and archive folder",
              "Open Settings to select the device and a folder on this Mac or an attached disk.")
            setupStep(
              "3", "Set your schedule",
              "Choose an interval and optional allowed hours. Sync Now is always available when connected."
            )
          }.padding(12)
        }
        HStack {
          SettingsLink { Label("Set Up BOOX Sync", systemImage: "gearshape") }.buttonStyle(
            .borderedProminent
          ).controlSize(.large)
          Button("Refresh Devices") { Task { await model.poll() } }
        }
        if let message = model.connectionError { Text(message).foregroundStyle(.orange) }
        Text(
          "\(model.devices.count) connected Android device(s) detected. Live notebook access is verified on Note Air 3C firmware 4.2; other models may expose different staging data."
        )
        .font(.caption).foregroundStyle(.secondary)
      }.padding(32).frame(maxWidth: 760, alignment: .leading)
    }
  }
  private func setupStep(_ number: String, _ title: String, _ message: String) -> some View {
    HStack(alignment: .top, spacing: 14) {
      Text(number).font(.headline).frame(width: 28, height: 28).background(
        Color.accentColor.opacity(0.12), in: Circle())
      VStack(alignment: .leading, spacing: 4) {
        Text(title).font(.headline)
        Text(message).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
      }
    }
  }
}

struct OverviewView: View {
  @ObservedObject var model: AppModel
  var body: some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 22) {
        HStack(alignment: .top) {
          VStack(alignment: .leading, spacing: 6) {
            Text("BOOX Sync").font(.title.weight(.semibold))
            Text(model.scheduleTitle).foregroundStyle(.secondary)
          }
          Spacer()
          Image(systemName: "books.vertical").font(.system(size: 30, weight: .light))
            .foregroundStyle(.secondary).accessibilityHidden(true)
        }
        HStack(alignment: .top, spacing: 14) {
          infoCard(
            "Tablet", symbol: "cable.connector", value: model.connectionTitle,
            detail: model.selectedDevice?.model ?? model.settings.deviceSerial)
          infoCard(
            "Last sync", symbol: "clock",
            value: model.lastFinished?.formatted(date: .abbreviated, time: .shortened) ?? "Not yet",
            detail: model.lastReportStatus == "failed"
              ? "Finished with issues — see Activity"
              : model.librarySummary)
        }
        GroupBox("Schedule") {
          VStack(alignment: .leading, spacing: 10) {
            scheduleRow(
              "Automatic sync", value: model.settings.schedule.automatic ? "On" : "Paused")
            scheduleRow(
              "Interval", value: "Every \(model.settings.schedule.intervalSeconds) seconds")
            scheduleRow(
              "Next sync (Mac time)",
              value: model.nextRun?.formatted(date: .abbreviated, time: .shortened) ?? "Paused")
            Text(
              "Automatic runs require the tablet to be connected and the Mac to be awake. Sync Now overrides allowed hours."
            ).font(.caption).foregroundStyle(.secondary)
          }.frame(maxWidth: .infinity, alignment: .leading).padding(10)
        }
        GroupBox("Archive") {
          VStack(alignment: .leading, spacing: 10) {
            Label(model.settings.archivePath, systemImage: "folder")
              .lineLimit(2).truncationMode(.middle).textSelection(.enabled)
            Text(
              "\(ByteCountFormatter.string(fromByteCount: model.freeBytes, countStyle: .file)) available · \(model.settings.keepRevisions) previous revisions per notebook"
            )
            .font(.caption).foregroundStyle(.secondary)
            HStack {
              Button("Open in Finder") { model.revealArchive() }
              Spacer()
              Text(
                model.settings.automaticPDF
                  ? "PDFs generated automatically" : "PDFs generated when opened"
              ).font(.caption)
            }
          }.frame(maxWidth: .infinity, alignment: .leading).padding(10)
        }
        let approximate = model.notebooks.filter { $0.status == "approximate" }.count
        if approximate > 0 {
          Label(
            "\(approximate) saved notebooks have approximate PDFs. Select a notebook to see details. Originals are kept.",
            systemImage: "info.circle"
          )
          .font(.callout).foregroundStyle(.secondary)
        }
      }.padding(28)
    }
  }
  private func scheduleRow(_ title: String, value: String) -> some View {
    HStack(alignment: .firstTextBaseline, spacing: 20) {
      Text(title).foregroundStyle(.secondary)
      Spacer(minLength: 0)
      Text(value).multilineTextAlignment(.trailing).monospacedDigit()
    }
  }
  private func infoCard(_ title: String, symbol: String, value: String, detail: String) -> some View
  {
    GroupBox {
      VStack(alignment: .leading, spacing: 9) {
        Label(title, systemImage: symbol).font(.caption).foregroundStyle(.secondary)
        Text(value).font(.title3.weight(.semibold)).monospacedDigit()
        Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(2)
      }.frame(maxWidth: .infinity, minHeight: 76, alignment: .topLeading).padding(10)
    }
  }
}

struct LibraryView: View {
  @ObservedObject var model: AppModel
  @Environment(\.openWindow) private var openWindow
  @State private var selection: String?
  @State private var search = ""
  @State private var scope: LibraryScope = .onTablet
  private var filtered: [Notebook] {
    model.notebooks.filter {
      scope.includes($0) && (search.isEmpty || $0.title.localizedCaseInsensitiveContains(search))
    }
  }
  private var selected: Notebook? { filtered.first { $0.id == selection } }
  var body: some View {
    VStack(spacing: 0) {
      VStack(alignment: .leading, spacing: 8) {
        Picker("Show notebooks", selection: $scope) {
          ForEach(LibraryScope.allCases) { item in
            Text("\(item.rawValue) (\(model.notebooks.filter { item.includes($0) }.count))").tag(
              item)
          }
        }.pickerStyle(.segmented)
        if let checked = model.inventoryCheckedAt {
          Text(
            "Tablet list checked \(checked.formatted(date: .abbreviated, time: .shortened)) · Saved copies are kept when you delete a notebook."
          )
          .font(.caption).foregroundStyle(.secondary)
        } else {
          Text(
            "Sync to check the tablet's current notebooks. Saved copies are available under All."
          )
          .font(.caption).foregroundStyle(.secondary)
        }
      }.padding()
      if filtered.isEmpty {
        ContentUnavailableView(
          emptyTitle, systemImage: "books.vertical",
          description: Text(
            scope == .onTablet
              ? "Connect your tablet and choose Sync Now to refresh its list."
              : "Saved notebooks remain available even after deletion on the tablet."))
      } else {
        Table(filtered, selection: $selection) {
          TableColumn("Notebook") { note in Label(note.title, systemImage: "book.closed") }
          TableColumn("Location") { note in Text(note.presence.label).foregroundStyle(.secondary) }
            .width(125)
          TableColumn("PDF") { note in
            Text(note.status.capitalized).foregroundStyle(
              note.status == "ok" ? Color.secondary : Color.orange)
          }.width(100)
          TableColumn("Pages") { note in Text(note.pages.map(String.init) ?? "—") }.width(55)
        }
        .contextMenu(forSelectionType: String.self) { ids in
          if let id = ids.first, let note = model.notebooks.first(where: { $0.id == id }) {
            Button("Open PDF") { Task { await model.open(note) } }.disabled(
              model.busy || !note.hasArchive)
            Button("Recovery History…") { showHistory(note) }.disabled(
              model.busy || !note.hasArchive)
            Button("Reveal in Finder") { model.reveal(note) }.disabled(!note.hasArchive)
          }
        } primaryAction: { ids in
          if let id = ids.first, let note = model.notebooks.first(where: { $0.id == id }),
            note.hasArchive, !model.busy
          {
            Task { await model.open(note) }
          }
        }
        if let note = selected {
          VStack(alignment: .leading, spacing: 8) {
            Text(note.title).font(.headline)
            if note.isArchived {
              Text("Saved on this Mac. This copy and its recovery history are retained.")
                .font(.caption).foregroundStyle(.secondary)
            }
            if !note.reasons.isEmpty {
              Text("Approximation: " + note.reasons.joined(separator: ", ")).font(.caption)
                .foregroundStyle(.orange)
            }
            if let error = note.error { Text(error).font(.caption).foregroundStyle(.orange) }
            HStack {
              Button("Open PDF") { Task { await model.open(note) } }.buttonStyle(.borderedProminent)
                .disabled(model.busy || !note.hasArchive)
              Button("Recovery History…") { showHistory(note) }.disabled(
                model.busy || !note.hasArchive)
              Button("Reveal in Finder") { model.reveal(note) }.disabled(!note.hasArchive)
            }
          }.frame(maxWidth: .infinity, alignment: .leading).padding().background(.bar)
        }
      }
    }.searchable(text: $search, prompt: "Find a notebook")
      .onChange(of: scope) { _, _ in selection = nil }
  }

  private var emptyTitle: String {
    if !search.isEmpty { return "No matching notebooks" }
    if scope == .onTablet {
      return model.inventoryCheckedAt == nil
        ? "Tablet list not checked yet" : "No notebooks on tablet"
    }
    return scope == .archived ? "No archived notebooks" : "No saved notebooks yet"
  }

  private func showHistory(_ note: Notebook) {
    model.historyNote = note
    openWindow(id: "recovery")
  }
}

struct ActivityView: View {
  @ObservedObject var model: AppModel
  var body: some View {
    VStack(alignment: .leading, spacing: 16) {
      HStack {
        VStack(alignment: .leading, spacing: 4) {
          Text("Last sync report").font(.title2.bold())
          Text("A failed notebook does not prevent other notebooks from being archived.")
            .foregroundStyle(.secondary)
        }
        Spacer()
        Button("Export Diagnostics…") { Task { await model.exportDiagnostics() } }.disabled(
          model.busy)
      }.padding([.horizontal, .top], 24)
      if model.outcomes.isEmpty {
        ContentUnavailableView("No sync report yet", systemImage: "clock.arrow.circlepath")
      } else {
        List(Array(model.outcomes.enumerated()), id: \.offset) { _, outcome in
          HStack(alignment: .top, spacing: 12) {
            let failed = outcome["status"] as? String == "failed"
            Image(systemName: failed ? "exclamationmark.circle" : "checkmark.circle")
              .foregroundStyle(failed ? .orange : .green)
            VStack(alignment: .leading, spacing: 4) {
              Text(outcome["title"] as? String ?? "Notebook").font(.headline)
              if let error = outcome["error"] as? String {
                Text(error).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
              }
              let reasons = outcome["reasons"] as? [String] ?? []
              if !reasons.isEmpty {
                Text(reasons.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
              }
            }
            Spacer()
            Text(
              outcome["status"] as? String == "inactive"
                ? "Inactive on tablet" : (outcome["status"] as? String ?? "").capitalized
            ).font(.caption).foregroundStyle(
              .secondary)
          }.padding(.vertical, 5)
        }
      }
    }
  }
}
