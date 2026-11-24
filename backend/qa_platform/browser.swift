import AppKit
import WebKit

final class ConsoleRelay: NSObject, WKScriptMessageHandler {
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "qaConsole",
              let body = message.body as? [String: Any],
              let level = body["level"] as? String,
              let text = body["text"] as? String,
              let path = ProcessInfo.processInfo.environment["QA_PLATFORM_CONSOLE_LOG"],
              !path.isEmpty
        else { return }
        let url = URL(fileURLWithPath: path)
        let payload = (try? JSONSerialization.data(withJSONObject: ["level": level, "text": text])) ?? Data()
        var line = payload
        line.append(0x0A)
        if FileManager.default.fileExists(atPath: path),
           let handle = try? FileHandle(forWritingTo: url) {
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: line)
            try? handle.close()
        } else {
            try? line.write(to: url)
        }
    }
}

final class Browser: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    let start: URL
    let relay = ConsoleRelay()
    var window: NSWindow!

    init(_ start: URL) { self.start = start }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let script = WKUserScript(source: Self.consoleHook, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        let content = WKUserContentController()
        content.addUserScript(script)
        content.add(relay, name: "qaConsole")
        let config = WKWebViewConfiguration()
        config.userContentController = content
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 1100, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "QA Platform"
        window.contentView = web
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        web.load(URLRequest(url: start))
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(Self.allowed(action.request.url, start: start) ? .allow : .cancel)
    }

    static func allowed(_ url: URL?, start: URL) -> Bool {
        guard let url, let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else { return false }
        guard let host = url.host, let startHost = start.host,
              host.compare(startHost, options: .caseInsensitive) == .orderedSame else { return false }
        if let explicit = start.port {
            return url.port == explicit
        }
        if scheme == "https" {
            return url.port == nil || url.port == 443
        }
        return url.port == nil || url.port == 80
    }

    static let consoleHook = """
    (function () {
      function send(level, args) {
        try {
          var text = Array.prototype.map.call(args, function (item) { return String(item); }).join(" ");
          window.webkit.messageHandlers.qaConsole.postMessage({level: level, text: text});
        } catch (error) {}
      }
      ["log", "warn", "error"].forEach(function (level) {
        var original = console[level];
        console[level] = function () {
          send(level, arguments);
          if (original) { return original.apply(console, arguments); }
        };
      });
    })();
    """

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let application = NSApplication.shared
application.setActivationPolicy(.regular)
guard CommandLine.arguments.count > 1, let start = URL(string: CommandLine.arguments[1]) else {
    fputs("缺少启动地址\n", stderr)
    exit(1)
}
let browser = Browser(start)
application.delegate = browser
application.run()
