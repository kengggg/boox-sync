import AppKit
import BooxCore
import SwiftUI

struct SettingsWindow: View {
  @ObservedObject var model: AppModel
  @State private var draft = AppSettings()
  @State private var saving = false
  @State private var message: String?
  @State private var tab = "archive"
  @State private var confirmCopy = false

  var body: some View {
    VStack(spacing: 0) {
      TabView(selection: $tab) {
        archive.tag("archive").tabItem { Label("Archive", systemImage: "folder") }
        schedule.tag("schedule").tabItem { Label("Schedule", systemImage: "clock") }
        device.tag("device").tabItem { Label("Device", systemImage: "ipad") }
        recovery.tag("recovery").tabItem {
          Label("PDF & Recovery", systemImage: "clock.arrow.circlepath")
        }
        general.tag("general").tabItem { Label("General", systemImage: "gearshape") }
      }
      .padding(.top, 10)
      Divider()
      HStack {
        if let message = message ?? model.error {
          Text(message).font(.caption).foregroundStyle(.secondary).lineLimit(3)
        }
        Spacer()
        Button("Revert") {
          draft = model.settings
          message = nil
        }.disabled(saving || model.busy)
        Button(saving ? "Saving…" : "Save Settings") {
          saving = true
          message = nil
          Task {
            do {
              try await model.save(draft)
              draft = model.settings
              message = "Settings saved."
            } catch { message = error.localizedDescription }
            saving = false
          }
        }.buttonStyle(.borderedProminent).disabled(saving || model.busy)
          .keyboardShortcut("s", modifiers: .command)
      }.padding(18)
    }
    .frame(width: 670, height: 590)
    .onAppear {
      NSApp.activate()
      draft = model.settings
      Task { await model.poll() }
    }
    .onChange(of: model.settings) { _, value in if !saving { draft = value } }
    .confirmationDialog(
      "Copy the current archive and switch folders?", isPresented: $confirmCopy,
      titleVisibility: .visible
    ) {
      Button("Choose Destination…") {
        Task {
          if let target = await model.chooseFolder() {
            await model.copyArchive(to: target)
            draft = model.settings
            message = model.error ?? "Archive copied and verified. The original remains available."
          }
        }
      }
    } message: {
      Text(
        "Choose an empty folder. Every file is verified before switching. The original archive stays in place as a recovery copy."
      )
    }
  }

  private var archive: some View {
    Form {
      Section("Notebook archive") {
        TextField(
          "Folder path",
          text: Binding(
            get: { draft.archivePath },
            set: { path in
              draft.archivePath = path
              draft.archiveID = ""
              draft.archiveBookmark = nil
            }), prompt: Text("Absolute path to an existing folder")
        )
        .textFieldStyle(.roundedBorder)
        HStack {
          Button("Choose Folder…") {
            Task {
              if let url = await model.chooseFolder() {
                draft.archivePath = url.path
                draft.archiveID = ""
                draft.archiveBookmark = nil
                message =
                  "Save Settings to use this folder. Existing archive files stay where they are."
              }
            }
          }
          Button("Reveal Current Folder") { model.revealArchive() }.disabled(
            model.settings.archivePath.isEmpty)
        }
        Text(
          "The selected folder holds notebook sources, PDFs, covers, and recovery history. If its disk is unavailable, syncing pauses."
        )
        .font(.caption).foregroundStyle(.secondary)
      }
      Section("Change locations") {
        Button("Copy Current Archive & Switch…") { confirmCopy = true }.disabled(
          model.settings.archiveID.isEmpty || model.busy)
        Text(
          "Use this to transfer your archive to another disk. Choosing a different folder above simply switches to that folder without copying."
        )
        .font(.caption).foregroundStyle(.secondary)
      }
    }.formStyle(.grouped).disabled(saving || model.busy)
  }

  private var schedule: some View {
    Form {
      Section {
        Toggle("Sync automatically while connected", isOn: $draft.schedule.automatic)
        Picker("Sync interval", selection: $draft.schedule.intervalSeconds) {
          ForEach([15, 30, 60, 120, 300, 900, 1800, 3600], id: \.self) { value in
            Text(value < 60 ? "Every \(value) seconds" : "Every \(value / 60) minute(s)").tag(value)
          }
        }
        Toggle("Limit automatic sync to allowed hours", isOn: $draft.schedule.limitedHours)
      }
      if draft.schedule.limitedHours {
        Section("Allowed hours") {
          HStack {
            ForEach(
              Array(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].enumerated()), id: \.offset
            ) { index, name in
              Toggle(
                name,
                isOn: Binding(
                  get: { draft.schedule.weekdays.contains(index) },
                  set: { value in
                    if value {
                      draft.schedule.weekdays.insert(index)
                    } else {
                      draft.schedule.weekdays.remove(index)
                    }
                  })
              ).toggleStyle(.button)
            }
          }
          DatePicker(
            "From", selection: minuteBinding(\.startMinute), displayedComponents: .hourAndMinute)
          DatePicker(
            "Until", selection: minuteBinding(\.endMinute), displayedComponents: .hourAndMinute)
          Picker("Time zone", selection: $draft.schedule.timeZoneID) {
            Text("Follow this Mac").tag("local")
            Text("UTC").tag("UTC")
            ForEach(TimeZone.knownTimeZoneIdentifiers, id: \.self) {
              Text($0.replacingOccurrences(of: "_", with: " ")).tag($0)
            }
          }
          Text(
            "Overnight windows belong to the day they start. An active sync finishes after the window closes."
          ).font(.caption).foregroundStyle(.secondary)
        }
      }
      Section {
        if let next = draft.schedule.nextRun(after: model.lastFinished) {
          LabeledContent(
            "Next eligible run (Mac time)",
            value: next.formatted(date: .abbreviated, time: .shortened))
        }
        Text(
          "Sync Now overrides this schedule. Automatic syncing resumes after wake when the tablet is connected and the time window allows it."
        )
        .font(.caption).foregroundStyle(.secondary)
      }
    }.formStyle(.grouped).disabled(saving || model.busy)
  }

  private func minuteBinding(_ key: WritableKeyPath<SyncSchedule, Int>) -> Binding<Date> {
    Binding(
      get: {
        let minutes = draft.schedule[keyPath: key]
        return Calendar.current.date(
          bySettingHour: minutes / 60, minute: minutes % 60, second: 0, of: Date()) ?? Date()
      },
      set: { date in
        draft.schedule[keyPath: key] =
          Calendar.current.component(.hour, from: date) * 60
          + Calendar.current.component(.minute, from: date)
      })
  }

  private var device: some View {
    Form {
      Section("Connected devices") {
        Picker("BOOX tablet", selection: $draft.deviceSerial) {
          Text("Choose a tablet").tag("")
          if !draft.deviceSerial.isEmpty
            && !model.devices.contains(where: { $0.serial == draft.deviceSerial })
          {
            Text("\(draft.deviceSerial) — disconnected").tag(draft.deviceSerial)
          }
          ForEach(model.devices) { device in
            Text("\(device.model) — \(device.serial) (\(device.state))").tag(device.serial)
          }
        }
        Button("Refresh Devices") { Task { await model.poll() } }
        LabeledContent("Saved device status", value: model.connectionTitle)
        if let error = model.connectionError { Text(error).foregroundStyle(.orange) }
      }
      Section("Connect over USB") {
        Text(
          "Enable USB debugging in the tablet's developer settings. Connect with a USB data cable, unlock the tablet, and accept this computer."
        )
        Text(
          "Only the selected device is read. BOOX Sync does not install apps, erase notebooks, or send touch input to the tablet."
        )
        .font(.caption).foregroundStyle(.secondary)
        Text(
          "Note Air 3C firmware 4.2 has been tested. Other BOOX firmware may not expose all notebook content; missing content is reported instead of replacing a good archive."
        )
        .font(.caption).foregroundStyle(.secondary)
      }
    }.formStyle(.grouped).disabled(saving || model.busy)
  }

  private var recovery: some View {
    Form {
      Section("PDF generation") {
        Toggle("Generate PDFs automatically after syncing", isOn: $draft.automaticPDF)
        Text(
          "PDF generation runs in the background. Approximation warnings stay visible in the notebook list."
        ).font(.caption).foregroundStyle(.secondary)
      }
      Section("Recovery history") {
        Stepper(
          "Keep \(draft.keepRevisions) previous revisions per notebook",
          value: $draft.keepRevisions, in: 1...100)
        Text(
          "This is in addition to the current copy. Lowering the limit removes older recovery copies on a later successful update. Notebook deletion on the tablet never deletes the Mac archive."
        )
        .font(.caption).foregroundStyle(.secondary)
      }
      Section("Connection timeout") {
        Stepper(
          "\(draft.adbTimeoutSeconds) seconds per operation", value: $draft.adbTimeoutSeconds,
          in: 10...600, step: 10)
      }
    }.formStyle(.grouped).disabled(saving || model.busy)
  }

  private var general: some View {
    Form {
      Section("Startup and menu bar") {
        Toggle(
          "Launch at login", isOn: Binding(get: { model.loginEnabled }, set: { model.setLogin($0) })
        )
        Text(
          "Login behavior changes immediately and may require approval in macOS System Settings."
        ).font(.caption).foregroundStyle(.secondary)
        Toggle("Show BOOX Sync in the menu bar", isOn: $draft.showMenuBar)
        Text("Closing the main window keeps BOOX Sync running. Use Quit to stop it.").font(.caption)
          .foregroundStyle(.secondary)
      }
      Section("Notifications") {
        Toggle("Notify when syncing needs attention", isOn: $draft.notifyErrors)
        Toggle("Notify after successful syncs", isOn: $draft.notifySuccess)
      }
      Section("Diagnostics") {
        Button("Export Diagnostics…") { Task { await model.exportDiagnostics() } }.disabled(
          model.settings.archiveID.isEmpty || model.busy)
        Text(
          "Exports versions and status counts. Notebook contents, titles, device serial numbers, and local paths are excluded."
        ).font(.caption).foregroundStyle(.secondary)
        LabeledContent(
          "Version",
          value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
            ?? "0.2.0")
      }
    }.formStyle(.grouped).disabled(saving || model.busy)
  }
}
