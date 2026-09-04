import Foundation
import Security
import AppKit

// File-based Keychain deliberately: this source-built helper has no provisioned
// access group. Never pass item contents in argv or print them on an error path.
let args = CommandLine.arguments
if args.count == 2 && args[1] == "observe-lock" {
    let owner = getppid()
    Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
        if getppid() != owner { exit(0) }
    }
    let center = DistributedNotificationCenter.default()
    let workspace = NSWorkspace.shared.notificationCenter
    func notify() { FileHandle.standardOutput.write(Data("lock\n".utf8)) }
    let lock = center.addObserver(forName: NSNotification.Name("com.apple.screenIsLocked"), object: nil, queue: .main) { _ in notify() }
    let inactive = workspace.addObserver(forName: NSWorkspace.sessionDidResignActiveNotification, object: nil, queue: .main) { _ in notify() }
    let sleep = workspace.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { _ in notify() }
    withExtendedLifetime([lock, inactive, sleep]) { RunLoop.main.run() }
    exit(0)
}
guard args.count == 3 else { exit(64) }
let operation = args[1], service = args[2]
let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service,
    kSecAttrAccount as String: "vault",
    kSecAttrSynchronizable as String: false
]
var status: OSStatus = errSecParam
switch operation {
case "get":
    var lookup = query
    lookup[kSecReturnData as String] = true
    lookup[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    status = SecItemCopyMatching(lookup as CFDictionary, &result)
    if status == errSecSuccess, let data = result as? Data { FileHandle.standardOutput.write(data) }
case "put":
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard data.count <= 131072 else { exit(65) }
    status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
    if status == errSecItemNotFound {
        var item = query
        item[kSecValueData as String] = data
        status = SecItemAdd(item as CFDictionary, nil)
    }
case "delete": status = SecItemDelete(query as CFDictionary)
default: exit(64)
}
if status == errSecItemNotFound { exit(44) }
if status != errSecSuccess {
    FileHandle.standardError.write(Data("Keychain operation failed (\(status)).\n".utf8))
    exit(1)
}
