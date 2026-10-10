# -*- coding: utf-8 -*-
"""一键跑完全部八项测试（Python 四项 + Node 四项），任何一项不过就返回非 0。

本地：python tests/run_all.py            （Node 不在 PATH 时用环境变量 NODE 指定 node 可执行文件）
CI：  .github/workflows/tests.yml 里直接调用本脚本，daily.yml 的发布依赖它通过。
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = os.environ.get("NODE") or shutil.which("node")

PY = [
    ("mm_test_amp", [sys.executable, "tests/mm_test_amp.py"]),
    ("mm_test_groups", [sys.executable, "tests/mm_test_groups.py"]),
    ("mm_test_render", [sys.executable, "tests/mm_test_render.py"]),
    ("mm_test_rsi", [sys.executable, "-m", "unittest", "tests.mm_test_rsi"]),
]
JS = ["mm_test_ebk", "mm_test_edit", "mm_test_homebtn", "mm_test_rs"]


def main():
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    jobs = list(PY)
    if NODE:
        jobs += [(n, [NODE, f"tests/{n}.js"]) for n in JS]
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
            print((r.stderr or "")[-3000:])
    print(f"\n{len(jobs) - len(failed)}/{len(jobs)} 项通过" + (f"；未通过：{', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
