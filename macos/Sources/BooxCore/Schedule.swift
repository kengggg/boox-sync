import Foundation

public struct SyncSchedule: Codable, Equatable {
  public var automatic = true
  public var intervalSeconds = 30
  public var limitedHours = false
  /// Monday = 0, Sunday = 6. An overnight window belongs to its starting day.
  public var weekdays: Set<Int> = Set(0...6)
  public var startMinute = 8 * 60
  public var endMinute = 18 * 60
  public var timeZoneID = "local"

  public init() {}

  public var calendar: Calendar {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone =
      timeZoneID == "local"
      ? .autoupdatingCurrent : TimeZone(identifier: timeZoneID) ?? .autoupdatingCurrent
    return calendar
  }

  public func allows(_ date: Date) -> Bool {
    guard limitedHours else { return true }
    let c = calendar
    let minute = c.component(.hour, from: date) * 60 + c.component(.minute, from: date)
    let day = (c.component(.weekday, from: date) + 5) % 7
    if startMinute < endMinute {
      return weekdays.contains(day) && minute >= startMinute && minute < endMinute
    }
    if startMinute > endMinute {
      return (minute >= startMinute && weekdays.contains(day))
        || (minute < endMinute && weekdays.contains((day + 6) % 7))
    }
    return false
  }

  public func nextAllowed(onOrAfter date: Date) -> Date? {
    if allows(date) { return date }
    let c = calendar
    // Calendar arithmetic handles spring-forward gaps and repeated fall hours.
    for offset in 0...8 {
      guard let day = c.date(byAdding: .day, value: offset, to: c.startOfDay(for: date)) else {
        continue
      }
      let candidates = [Calendar.RepeatedTimePolicy.first, .last].compactMap { policy in
        c.date(
          bySettingHour: startMinute / 60, minute: startMinute % 60,
          second: 0, of: day, matchingPolicy: .nextTime,
          repeatedTimePolicy: policy, direction: .forward)
      }.sorted()
      if let candidate = candidates.first(where: { $0 >= date && allows($0) }) { return candidate }
    }
    return nil
  }

  public func nextRun(after lastFinished: Date?, now: Date = Date()) -> Date? {
    guard automatic else { return nil }
    let due = max(now, lastFinished?.addingTimeInterval(Double(intervalSeconds)) ?? now)
    return nextAllowed(onOrAfter: due)
  }

  public func validate() throws {
    guard (10...86400).contains(intervalSeconds) else {
      throw SettingsError("Choose a sync interval between 10 seconds and 24 hours.")
    }
    guard (0..<1440).contains(startMinute), (0..<1440).contains(endMinute),
      weekdays.isSubset(of: Set(0...6))
    else { throw SettingsError("Invalid sync hours.") }
    if limitedHours && (weekdays.isEmpty || startMinute == endMinute) {
      throw SettingsError(
        "Choose at least one day and different start/end times, or turn off allowed hours.")
    }
    guard timeZoneID == "local" || TimeZone(identifier: timeZoneID) != nil else {
      throw SettingsError("Choose a valid time zone.")
    }
  }
}

public struct SettingsError: LocalizedError {
  public let message: String
  public init(_ message: String) { self.message = message }
  public var errorDescription: String? { message }
}
