"""执行阶段只使用 js / js_reset，并且只能附着产品浏览器。"""

import json
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

APP = "local.qa.platform.browser"
CACHE = Path.home() / "Library" / "Caches" / "local.qa.platform"
APP_BUNDLE = CACHE / "QA Platform Browser.app"
BINARY_NAME = "QAPlatformBrowser"
GET_APP = re.compile(r"""getApp\(\s*['"]([^'"]+)['"]""")


class OcuError(RuntimeError):
    pass


def assert_product_js(code: str, app: str | None = None) -> None:
    allowed = app or APP
    apps = GET_APP.findall(code or "")
    if any(name != allowed for name in apps):
        raise OcuError("控件环境错误")


def ensure_browser(cache: Path = CACHE) -> Path:
    binary = cache / "QA Platform Browser.app" / "Contents" / "MacOS" / BINARY_NAME
    if binary.is_file() and not binary.is_symlink():
        return binary
    return compile_browser(cache=cache)


def compile_browser(source: Path | None = None, cache: Path = CACHE) -> Path:
    """编译到产品缓存目录。不碰 local.qa.runner。"""
    if source is None:
        source = Path(__file__).resolve().parents[1] / "browser.swift"
    if "local.qa.runner" in str(cache):
        raise OcuError("不能覆盖演示浏览器")
    bundle = cache / "QA Platform Browser.app"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    binary = macos / BINARY_NAME
    plist = {
        "CFBundleIdentifier": APP,
        "CFBundleExecutable": BINARY_NAME,
        "CFBundleName": "QA Platform Browser",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1",
    }
    (bundle / "Contents").mkdir(parents=True, exist_ok=True)
    with (bundle / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(plist, handle)
    compiler = shutil.which("swiftc")
    if not compiler:
        raise OcuError("找不到 swiftc")
    subprocess.run(
        [compiler, "-O", "-o", str(binary), str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


def browser_argv(binary: Path, url: str) -> list[str]:
    return [str(binary), url]


def repl_argv(native: str) -> list[str] | None:
    """ocu mcp 没有 js。脚本通道在 Node REPL 适配器上。"""
    node = shutil.which("node")
    if not node or not native:
        return None
    roots = [Path.home() / ".grok/npm-global/lib/node_modules/open-computer-use"]
    native_path = Path(native)
    if native_path.exists():
        roots = [*native_path.resolve().parents, *roots]
    for root in roots:
        adapter = root / "scripts/node-repl/open-computer-use-repl.mjs"
        if adapter.is_file():
            return [node, str(adapter), "--", native, "mcp"]
    return None


class _Peer:
    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self.buffer = bytearray()
        self.call_id = 0

    def send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        self.process.stdin.flush()

    def rpc(self, method: str, params: dict) -> dict:
        import select
        import time

        self.call_id += 1
        current = self.call_id
        self.send({"id": current, "method": method, "params": params})
        deadline = time.monotonic() + 25
        assert self.process.stdout is not None
        fd = self.process.stdout.fileno()
        while True:
            line, separator, rest = self.buffer.partition(b"\n")
            if separator:
                self.buffer = bytearray(rest)
                result = json.loads(line)
                if result.get("id") != current:
                    continue
                if "error" in result:
                    raise OcuError("MCP 调用失败")
                return result["result"]
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise OcuError("MCP 调用超时")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise OcuError("open-computer-use MCP 已退出")
            self.buffer.extend(chunk)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()


class ProductSession:
    """真实 js 会话。测试注入替身，不走这里。"""

    def __init__(self) -> None:
        command = shutil.which("ocu") or shutil.which("open-computer-use")
        if not command:
            command = str(Path.home() / ".grok" / "bin" / "ocu")
        env = os.environ.copy()
        env.pop("DEEPSEEK_API_KEY", None)
        env.pop("YUNXIAO_TOKEN", None)
        env["OPEN_COMPUTER_USE_AGENT_SOCKET_NAMESPACE"] = "qa-platform"
        init = {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "qa-platform", "version": "0.1"},
        }
        self.native = _Peer([command, "mcp"], env)
        self.native.rpc("initialize", init)
        self.native.send({"method": "notifications/initialized"})
        argv = repl_argv(command)
        if not argv:
            self.native.close()
            raise OcuError("没有可用的 computer use 脚本通道")
        self.script = _Peer(argv, env)
        self.script.rpc("initialize", init)
        self.script.send({"method": "notifications/initialized"})
        self.process = self.script.process

    def list_apps(self) -> str:
        result = self.native.rpc("tools/call", {"name": "list_apps", "arguments": {}})
        if not isinstance(result, dict):
            return ""
        return "\n".join(
            str(item.get("text") or "")
            for item in result.get("content") or []
            if isinstance(item, dict) and item.get("type") == "text"
        )

    def get_app_state(self, app: str, include_screenshot: bool = False) -> dict:
        result = self.native.rpc("tools/call", {"name": "get_app_state", "arguments": {
            "app": app,
            "include_screenshot": include_screenshot,
            "text_limit": 12000,
        }})
        return result if isinstance(result, dict) else {}

    def query(self, app: str, text: str, role: str = "") -> dict:
        arguments = {"app": app, "text": text, "exact": False, "limit": 24}
        if role:
            arguments["role"] = role
        result = self.native.rpc("tools/call", {"name": "query", "arguments": arguments})
        return result if isinstance(result, dict) else {"content": []}

    def click(self, app: str, index: str) -> dict:
        result = self.native.rpc("tools/call", {"name": "click", "arguments": {
            "app": app,
            "element_index": str(index),
        }})
        return result if isinstance(result, dict) else {"content": []}

    def set_value(self, app: str, index: str, value: str) -> dict:
        result = self.native.rpc("tools/call", {"name": "set_value", "arguments": {
            "app": app,
            "element_index": str(index),
            "value": value,
        }})
        return result if isinstance(result, dict) else {"content": []}

    def scroll(self, app: str, index: str, direction: str = "down", pages: float = 1) -> dict:
        result = self.native.rpc("tools/call", {"name": "scroll", "arguments": {
            "app": app,
            "element_index": str(index),
            "direction": direction,
            "pages": pages,
        }})
        return result if isinstance(result, dict) else {"content": []}

    def press_key(self, app: str, key: str) -> dict:
        result = self.native.rpc("tools/call", {"name": "press_key", "arguments": {
            "app": app,
            "key": key,
        }})
        return result if isinstance(result, dict) else {"content": []}

    def reset(self) -> None:
        self.script.rpc("tools/call", {"name": "js_reset", "arguments": {}})

    def run(self, code: str, app: str | None = None) -> dict:
        assert_product_js(code, app)
        result = self.script.rpc("tools/call", {"name": "js", "arguments": {"code": code}})
        return result if isinstance(result, dict) else {"content": []}

    def close(self) -> None:
        self.script.close()
        self.native.close()
