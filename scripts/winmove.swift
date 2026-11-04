// winmove <pid> <x> <y> — 把进程 pid 的第一个窗口挪到全局坐标 (x, y)（左上角原点，点）。
//
// 给 maa_playcover.settle_game_window() 用：PlayCover 每次拉起明日方舟都把窗口开在
// 当前活动的那块屏上，不会回到 MAA-Game 虚拟屏。走辅助功能（AX）接口，需要调用方
// （Hermes gateway 的 Python）有「辅助功能」权限；没有就退出码 3，不弹任何东西。
//
// 退出码：0 挪好了；1 参数错；2 找不到窗口；3 没有辅助功能权限；4 设置位置失败。

import ApplicationServices
import Foundation

let args = CommandLine.arguments
guard args.count == 4, let pid = pid_t(args[1]), let x = Double(args[2]), let y = Double(args[3]) else {
    FileHandle.standardError.write("usage: winmove <pid> <x> <y>\n".data(using: .utf8)!)
    exit(1)
}
guard AXIsProcessTrusted() else {
    print("not trusted")
    exit(3)
}
let app = AXUIElementCreateApplication(pid)
var value: CFTypeRef?
guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) == .success,
      let windows = value as? [AXUIElement], let window = windows.first else {
    print("no window")
    exit(2)
}
var point = CGPoint(x: x, y: y)
guard let axPoint = AXValueCreate(.cgPoint, &point) else { exit(4) }
let result = AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, axPoint)
if result != .success {
    print("set position failed: \(result.rawValue)")
    exit(4)
}
print("moved")
