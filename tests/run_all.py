# -*- coding: utf-8 -*-
"""默认离线运行八项测试（Python 四项 + Node 四项），任何一项不过就返回非 0。

本地：python tests/run_all.py（可用环境变量 NODE 指定 node 可执行文件）
护栏自测：python tests/run_all.py --self-test-offline（不运行任何业务测试）
单项入口：--offline-python script.py [args] / -m unittest [args] / -c code [args]
          --offline-node script.js [args] / -e code [args]
CI：.github/workflows/tests.yml 直接调用本脚本，daily.yml 的发布依赖它通过。
护栏用于防止测试意外联网，不是针对恶意代码、原生扩展或另起进程的安全沙箱。
"""
import os
import shutil
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.abspath(__file__)
NODE = os.environ.get("NODE") or shutil.which("node")
OFFLINE_EXIT = 86

PY = [
    ("mm_test_amp", [sys.executable, "tests/mm_test_amp.py"]),
    ("mm_test_groups", [sys.executable, "tests/mm_test_groups.py"]),
    ("mm_test_render", [sys.executable, "tests/mm_test_render.py"]),
    ("mm_test_rsi", [sys.executable, "-m", "unittest", "tests.mm_test_rsi"]),
]
JS = ["mm_test_ebk", "mm_test_edit", "mm_test_homebtn", "mm_test_rs"]


def _install_python_guard():
    import atexit
    import _socket
    import socket

    violations = []
    write = os.write
    exit_process = os._exit

    def record(kind, target):
        message = f"{kind} {target}"
        violations.append(message)
        return f"[OFFLINE] 禁止未 mock 的网络请求：{message}"

    def finish():
        if violations:
            try:
                report = f"\n[OFFLINE] 检测到 {len(violations)} 次联网违规：\n  " + "\n  ".join(violations) + "\n"
                write(2, report.encode("utf-8", errors="backslashreplace"))
                sys.stdout.flush()
            finally:
                # atexit 中抛异常不会改变成功退出码，必须显式结束进程。
                exit_process(OFFLINE_EXIT)

    # 先注册，后于测试自己的 atexit 回调执行，捕获退出清理阶段的违规。
    atexit.register(finish)

    def deny_socket(sock, event, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address is not None:
            host, port = address[:2]
            host = f"[{host}]" if ":" in str(host) else host
            raise OSError(record(event, f"socket://{host}:{port}"))

    def audit(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.sendmsg"}:
            deny_socket(args[0], event, args[1])
        elif event in {"socket.getaddrinfo", "socket.gethostbyname",
                       "socket.gethostbyaddr", "socket.getnameinfo"}:
            raise OSError(record(event, f"dns://{args!r}"))

    # CPython 审计钩子也覆盖 _socket、connect_ex 和保存下来的原始方法。
    # 不给回环地址开白名单；AF_UNIX 本地 IPC 不受限制。
    sys.addaudithook(audit)

    # C 层 connect/sendto 的审计事件可能晚于主机名解析，另加前置拦截。
    # _socket.socket 本身不可变，用子类替换其导出，同时保留正常 socket API。
    class GuardedSocket(_socket.socket):
        pass

    def wrap_socket(name, address_index):
        original = getattr(_socket.socket, name)

        def guarded(sock, *args, **kwargs):
            if args and (address_index < 0 or len(args) > address_index):
                deny_socket(sock, f"socket.{name}", args[address_index])
            return original(sock, *args, **kwargs)

        return guarded

    for name, index in (("connect", 0), ("connect_ex", 0), ("sendto", -1), ("sendmsg", 3)):
        if hasattr(_socket.socket, name):
            guarded = wrap_socket(name, index)
            setattr(GuardedSocket, name, guarded)
            setattr(socket.socket, name, guarded)
    _socket.socket = GuardedSocket
    socket.SocketType = GuardedSocket
    if hasattr(_socket, "SocketType"):
        _socket.SocketType = GuardedSocket

    import requests

    def request(session, method, url, *args, **kwargs):
        raise requests.exceptions.ConnectionError(record(f"requests {method}", url))

    def send(session, request, *args, **kwargs):
        raise requests.exceptions.ConnectionError(record("requests.send", request.url))

    requests.sessions.Session.request = request
    requests.sessions.Session.send = send


def _offline_python(args):
    import runpy
    import types

    if not args or (args[0] in ("-m", "-c") and len(args) < 2):
        print("用法：--offline-python script.py [args] | -m module [args] | -c code [args]", file=sys.stderr)
        return 2
    sys.dont_write_bytecode = True
    _install_python_guard()
    if args[0] == "-m":
        sys.argv = args[1:]
        sys.path[0] = os.getcwd()
        runpy.run_module(args[1], run_name="__main__", alter_sys=True)
    elif args[0] == "-c":
        sys.argv = ["-c", *args[2:]]
        sys.path[0] = ""
        main_module = types.ModuleType("__main__")
        sys.modules["__main__"] = main_module
        exec(compile(args[1], "<string>", "exec"), main_module.__dict__)
    else:
        sys.argv = list(args)
        sys.path[0] = os.path.dirname(os.path.abspath(args[0]))
        runpy.run_path(args[0], run_name="__main__")
    return 0


NODE_BOOTSTRAP = r"""
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const net = require('node:net');
const tls = require('node:tls');
const http = require('node:http');
const https = require('node:https');
const http2 = require('node:http2');
const dgram = require('node:dgram');
const dns = require('node:dns');
const violations = [];
const write = fs.writeSync.bind(fs);

function target(value, protocol = 'socket:') {
  if (typeof value === 'string' || value instanceof URL) return String(value);
  if (Array.isArray(value)) return target(value[0], protocol);
  if (value && typeof value === 'object') {
    if (value.url) return value.url;
    const host = value.hostname || value.host || 'localhost';
    const port = value.port == null ? '' : ':' + value.port;
    return `${value.protocol || protocol}//${host}${port}${value.path || ''}`;
  }
  return String(value);
}
function block(kind, value) {
  const message = `${kind} ${value}`;
  violations.push(message);
  // exit 回调内也可能吞错；即时记录并置失败码，不只依赖最终汇总。
  process.exitCode = 86;
  write(2, '[OFFLINE] ' + message + '\n');
  throw new Error('[OFFLINE] 禁止未 mock 的网络请求：' + message);
}
process.on('exit', () => {
  if (violations.length) {
    process.exitCode = 86;
    write(2, `\n[OFFLINE] 检测到 ${violations.length} 次联网违规：\n  ${violations.join('\n  ')}\n`);
  }
});

globalThis.fetch = async (input) => block('fetch', target(input, 'https:'));
if (globalThis.WebSocket) {
  globalThis.WebSocket = class {
    constructor(url) { block('WebSocket', String(url)); }
  };
}
for (const [mod, protocol] of [[http, 'http:'], [https, 'https:']]) {
  for (const method of ['request', 'get']) {
    mod[method] = (input) => block(protocol + method, target(input, protocol));
  }
}
net.Socket.prototype.connect = function (...args) {
  const address = typeof args[0] === 'number'
    ? `socket://${typeof args[1] === 'string' ? args[1] : 'localhost'}:${args[0]}`
    : target(args[0]);
  return block('net.connect', address);
};
tls.connect = (...args) => block('tls.connect', typeof args[0] === 'number'
  ? `tls://${typeof args[1] === 'string' ? args[1] : 'localhost'}:${args[0]}`
  : target(args[0], 'tls:'));
http2.connect = (authority) => block('http2.connect', String(authority));
for (const method of ['send', 'connect']) {
  dgram.Socket.prototype[method] = function (...args) {
    return block('dgram.' + method, JSON.stringify(args.filter(v => typeof v !== 'function')));
  };
}
for (const mod of [dns, dns.promises, dns.Resolver.prototype, dns.promises.Resolver.prototype]) {
  for (const method of Object.getOwnPropertyNames(mod)) {
    if (/^(lookup|resolve|reverse)/.test(method) && typeof mod[method] === 'function') {
      mod[method] = (...args) => block('dns.' + method, 'dns://' + String(args[0]));
    }
  }
}
// 让后续 ESM 内建模块导入也看到已替换的导出。
Module.syncBuiltinESMExports();
const args = process.argv.slice(1);
if (!args.length || (args[0] === '-e' && args.length < 2)) {
  throw new Error('用法：--offline-node script.js [args] | -e code [args]');
}
if (args[0] === '-e') {
  process.argv = [process.execPath, ...args.slice(2)];
  const main = new Module('[offline-eval]');
  main.filename = path.join(process.cwd(), '[offline-eval]');
  main.paths = Module._nodeModulePaths(process.cwd());
  main._compile(args[1], main.filename);
} else {
  process.argv = [process.execPath, path.resolve(args[0]), ...args.slice(1)];
  Module.runMain(process.argv[1]);
}
"""


def _python_command(args):
    return [sys.executable, "-B", RUNNER, "--offline-python", *args]


def _node_command(args):
    return [NODE, "-e", NODE_BOOTSTRAP, "--", *args]


def _child_env():
    return dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")


class OfflineGuardTests(unittest.TestCase):
    """供护栏自测经真实的 python -m unittest 入口加载，不导入业务模块。"""

    def test_argv(self):
        # unittest.__main__ 会把 runpy 设置的文件名改成解释器的 -m 启动名。
        self.assertTrue(sys.argv[0].endswith(" -m unittest"), sys.argv[0])
        self.assertEqual(sys.argv[1:], ["tests.run_all.OfflineGuardTests", "-v"])
        self.assertEqual(sys.path[0], ROOT)

    def test_offline(self):
        self.assertEqual(sum([1, 2, 3]), 6)


def _offline_probe(args):
    if len(args) != 2 or args[0] != "参数 空格" or sys.argv != [args[1], "--offline-probe", *args]:
        raise AssertionError(f"脚本参数不正确：{sys.argv!r}")
    if sys.path[0] != os.path.dirname(RUNNER) or __name__ != "__main__":
        raise AssertionError("脚本入口或导入路径不正确")
    print("DIRECT_SCRIPT_OK")
    return 0


def _self_test_offline():
    # 所有源码都驻留内存；requests 测试额外封住传输层，socket 测试用已关闭
    # 的句柄，DNS 用数值地址或合成审计事件。即使护栏回归也不尝试真实外网。
    checks = []

    def python_check(name, code, expected=0, markers=()):
        checks.append((name, _python_command(["-c", code]), expected, markers))

    python_check("Python 纯离线与 -c 参数", """
import sys
assert sys.argv == ['-c']
assert sys.path[0] == ''
assert __name__ == '__main__'
assert sum(range(5)) == 10
print('OFFLINE_OK')
""", markers=("OFFLINE_OK",))
    for label, entry in (("绝对路径", RUNNER), ("相对路径", "tests/run_all.py")):
        checks.append((f"Python direct script 参数（{label}）",
                       _python_command([entry, "--offline-probe", "参数 空格", entry]),
                       0, ("DIRECT_SCRIPT_OK",)))
    checks.append(("Python unittest -m 入口", _python_command(["-m", "unittest", "tests.run_all.OfflineGuardTests", "-v"]),
                   0, ("Ran 2 tests", "OK")))
    python_check("Python 显式 mock", """
import requests
import socket
from unittest.mock import patch
with patch.object(requests, 'get', return_value='get-mock'):
    assert requests.get('https://offline.invalid/mocked-get') == 'get-mock'
with patch.object(requests.sessions.Session, 'request', return_value='session-mock'):
    assert requests.Session().post('https://offline.invalid/mocked-session') == 'session-mock'
prepared = requests.Request('GET', 'https://offline.invalid/mocked-send').prepare()
with patch.object(requests.sessions.Session, 'send', return_value='send-mock'):
    assert requests.Session().send(prepared) == 'send-mock'
with patch.object(requests, 'get', side_effect=requests.ConnectionError('模拟离线错误')):
    try:
        requests.get('https://offline.invalid/mocked-error')
    except requests.ConnectionError:
        pass
with patch.object(socket.socket, 'connect', return_value=None):
    with socket.socket() as sock:
        sock.connect(('192.0.2.1', 443))
print('MOCK_OK')
""", markers=("MOCK_OK",))
    swallowed = """
import requests
from unittest.mock import patch
with patch.object(requests.adapters.HTTPAdapter, 'send', side_effect=AssertionError('不应触及传输层')):
    actions = [
        lambda: requests.get('https://offline.invalid/swallowed-get'),
        lambda: requests.Session().post('https://offline.invalid/swallowed-post'),
        lambda: requests.Session().send(requests.Request('GET', 'https://offline.invalid/prepared-send').prepare()),
    ]
    for action in actions:
        try:
            action()
        except requests.RequestException:
            pass
print('SWALLOWED_OK')
"""
    request_markers = ("SWALLOWED_OK", "https://offline.invalid/swallowed-get",
                       "https://offline.invalid/swallowed-post", "https://offline.invalid/prepared-send")
    python_check("Python 吞掉 requests 错误仍失败", swallowed, OFFLINE_EXIT, request_markers)
    python_check("Python sys.exit(0) 不能掩盖违规", swallowed + "\nimport sys; sys.exit(0)",
                 OFFLINE_EXIT, request_markers)
    python_check("Python unittest 显示 OK 也不能掩盖违规", """
import requests
import unittest
from unittest.mock import patch
class SwallowedTest(unittest.TestCase):
    def test_swallowed(self):
        with patch.object(requests.adapters.HTTPAdapter, 'send', side_effect=AssertionError('不应触及传输层')):
            try:
                requests.get('https://offline.invalid/unittest-swallowed')
            except requests.RequestException:
                pass
unittest.main()
""", OFFLINE_EXIT, ("Ran 1 test", "OK", "https://offline.invalid/unittest-swallowed"))
    python_check("Python 后台线程中的违规", """
import requests
import threading
from unittest.mock import patch
def work():
    try:
        requests.get('https://offline.invalid/thread')
    except requests.RequestException:
        pass
with patch.object(requests.adapters.HTTPAdapter, 'send', side_effect=AssertionError('不应触及传输层')):
    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
print('THREAD_OK')
""", OFFLINE_EXIT, ("THREAD_OK", "https://offline.invalid/thread"))
    python_check("Python 退出回调中的违规", """
import atexit
import requests
from unittest.mock import patch
@atexit.register
def late_request():
    with patch.object(requests.adapters.HTTPAdapter, 'send', side_effect=AssertionError('不应触及传输层')):
        try:
            requests.get('https://offline.invalid/at-exit')
        except requests.RequestException:
            pass
    print('ATEXIT_OK')
""", OFFLINE_EXIT, ("ATEXIT_OK", "https://offline.invalid/at-exit"))
    for method in ("connect", "connect_ex", "sendto"):
        python_check(f"Python 底层 _socket.{method}", f"""
import _socket
sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM if '{method}' == 'sendto' else _socket.SOCK_STREAM)
sock.close()
try:
    if '{method}' == 'sendto':
        sock.sendto(b'x', ('192.0.2.1', 443))
    else:
        getattr(sock, '{method}')(('192.0.2.1', 443))
except OSError:
    pass
print('SOCKET_SWALLOWED_OK')
""", OFFLINE_EXIT, ("SOCKET_SWALLOWED_OK", "socket://192.0.2.1:443"))
    python_check("Python socket 主机名在解析前拦截", """
import _socket
import socket
from types import SimpleNamespace
fake = SimpleNamespace(family=socket.AF_INET)
for cls in (socket.socket, _socket.socket):
    for method in ('connect', 'connect_ex'):
        try:
            getattr(cls, method)(fake, ('offline.invalid', 443))
        except OSError:
            pass
print('BEFORE_DNS_OK')
""", OFFLINE_EXIT, ("BEFORE_DNS_OK", "socket://offline.invalid:443"))
    python_check("Python 原始 C 方法的审计兜底", """
import _socket
original = _socket.socket.__mro__[1]
sock = original(_socket.AF_INET, _socket.SOCK_STREAM)
sock.close()
try:
    original.connect(sock, ('192.0.2.1', 443))
except OSError:
    pass
print('AUDIT_OK')
""", OFFLINE_EXIT, ("AUDIT_OK", "socket://192.0.2.1:443"))
    python_check("Python DNS 拦截", """
import socket
import sys
for action in (
    lambda: socket.getaddrinfo('192.0.2.1', 443, flags=socket.AI_NUMERICHOST),
    lambda: socket.gethostbyname('192.0.2.1'),
    lambda: sys.audit('socket.gethostbyaddr', '192.0.2.1'),
    lambda: sys.audit('socket.getnameinfo', ('192.0.2.1', 443)),
):
    try:
        action()
    except OSError:
        pass
print('DNS_SWALLOWED_OK')
""", OFFLINE_EXIT, ("DNS_SWALLOWED_OK", "socket.getaddrinfo", "socket.gethostbyname",
                       "socket.gethostbyaddr", "socket.getnameinfo", "dns://"))
    python_check("Python 保留业务非零退出码", "import sys; sys.exit(7)", 7)
    python_check("Python 保留未捕获异常", "raise RuntimeError('EXPECTED_ERROR')", 1, ("EXPECTED_ERROR",))

    if NODE:
        def node_check(name, code, expected=0, markers=()):
            checks.append((name, _node_command(["-e", code, "参数 空格"]), expected, markers))

        node_check("Node 纯离线、参数与显式 mock", """
const assert = require('node:assert/strict');
assert.deepEqual(process.argv.slice(1), ['参数 空格']);
global.fetch = async () => ({ok: true});
fetch('https://offline.invalid/mocked').then(r => {
  assert.equal(r.ok, true);
  console.log('NODE_MOCK_OK');
});
""", markers=("NODE_MOCK_OK",))
        node_check("Node 吞掉 fetch 错误仍失败", """
require('node:net').Socket.prototype.connect = () => { throw new Error('不应触及传输层'); };
fetch('https://offline.invalid/node-fetch').catch(() => console.log('FETCH_SWALLOWED_OK'));
""", OFFLINE_EXIT, ("FETCH_SWALLOWED_OK", "https://offline.invalid/node-fetch"))
        node_check("Node HTTP/HTTPS 违规与 process.exit(0)", """
require('node:net').Socket.prototype.connect = () => { throw new Error('不应触及传输层'); };
for (const protocol of ['http', 'https']) {
  for (const method of ['get', 'request']) {
    try { require('node:' + protocol)[method](protocol + '://offline.invalid/' + method); }
    catch (_) {}
  }
}
console.log('HTTP_SWALLOWED_OK');
process.exit(0);
""", OFFLINE_EXIT, ("HTTP_SWALLOWED_OK", "http://offline.invalid/get", "http://offline.invalid/request",
                       "https://offline.invalid/get", "https://offline.invalid/request"))
        node_check("Node 底层 TCP/UDP 拦截", """
try { require('node:net').connect({host: '192.0.2.1', port: -1}); } catch (_) {}
const sock = require('node:dgram').createSocket('udp4');
try { sock.send('x', -1, '192.0.2.1'); } catch (_) {}
try { sock.close(); } catch (_) {}
console.log('NODE_SOCKET_OK');
""", OFFLINE_EXIT, ("NODE_SOCKET_OK", "net.connect socket://192.0.2.1:-1", "dgram.send"))
        node_check("Node DNS 拦截", """
(async () => {
  try { await require('node:dns').promises.lookup('192.0.2.1'); } catch (_) {}
  console.log('NODE_DNS_OK');
})();
""", OFFLINE_EXIT, ("NODE_DNS_OK", "dns.lookup dns://192.0.2.1"))
        node_check("Node 保留业务非零退出码", "process.exit(7)", 7)
    else:
        print("找不到 node（可设置环境变量 NODE），无法完成 Node 护栏自测")
        return 1

    failed = []
    for name, command, expected, markers in checks:
        try:
            result = subprocess.run(command, cwd=ROOT, env=_child_env(), capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=20)
            output = result.stdout + result.stderr
            ok = (result.returncode == expected and all(marker in output for marker in markers)
                  and (("[OFFLINE]" in result.stderr) == (expected == OFFLINE_EXIT)))
        except subprocess.TimeoutExpired:
            ok = False
            output = "护栏自测超时"
            result = None
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failed.append(name)
            print(f"预期退出码 {expected}，实际 {result.returncode if result else 'timeout'}\n{output}")
    print(f"\n护栏自测 {len(checks) - len(failed)}/{len(checks)} 项通过（未运行业务测试）")
    return 1 if failed else 0


def main():
    args = sys.argv[1:]
    if args:
        if args[0] == "--offline-python":
            return _offline_python(args[1:])
        if args[0] == "--offline-node":
            if not NODE:
                print("找不到 node（可设置环境变量 NODE）", file=sys.stderr)
                return 1
            return subprocess.run(_node_command(args[1:]), env=_child_env()).returncode
        if args == ["--self-test-offline"]:
            return _self_test_offline()
        if args[0] == "--offline-probe":
            return _offline_probe(args[1:])
        print("未知参数；可使用 --self-test-offline、--offline-python 或 --offline-node", file=sys.stderr)
        return 2

    env = _child_env()
    jobs = [(name, _python_command(cmd[1:])) for name, cmd in PY]
    if NODE:
        jobs += [(n, _node_command([f"tests/{n}.js"])) for n in JS]
    else:
        print("找不到 node（可设置环境变量 NODE），Node 测试无法运行")
        return 1
    failed = []
    for name, cmd in jobs:
        t0 = time.time()
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = r.returncode == 0
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({time.time() - t0:.1f}s)")
        if not ok:
            failed.append(name)
            print((r.stdout or "")[-3000:])
            # 不截断 stderr，避免漏掉退出阶段汇总的违规 URL。
            print(r.stderr or "")
    print(f"\n{len(jobs) - len(failed)}/{len(jobs)} 项通过" + (f"；未通过：{', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
