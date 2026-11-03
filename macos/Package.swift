// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "BOOXSync",
  platforms: [.macOS(.v14)],
  products: [.executable(name: "BOOXSync", targets: ["BooxApp"])],
  targets: [
    .target(name: "BooxCore"),
    .executableTarget(name: "BooxApp", dependencies: ["BooxCore"]),
    .testTarget(name: "BooxCoreTests", dependencies: ["BooxCore"]),
  ],
  swiftLanguageModes: [.v5]
)
