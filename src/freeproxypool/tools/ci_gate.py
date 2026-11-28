# -*- coding: utf-8 -*-
"""分层 CI 门禁（借鉴 docs/09 P2-1）。

把检查拆成四层，每层独立可跑、职责清晰：

============  ====================  ===============================
层            内容                 何时需要跑
============  ====================  ===============================
``lint``      语法与导入验证        任何 ``.py`` 改动
``test``      离线自检套件          任何 ``.py`` / 依赖文件改动
``audit``     依赖漏洞扫描          依赖文件改动（或定期跑）
``build``     镜像构建              部署相关文件改动 / 主分支
============  ====================  ===============================

**为什么分层**：旧做法是把自检塞进 Dockerfile 的 checker 阶段 ——
一次构建全跑，镜像构建慢、反馈慢，改个文档也要等全套检查跑完。
分层之后，"改一行 Python"几十秒就能知道有没有坏，
而"只改文档"根本不用跑测试。

用法::

    python -m freeproxypool.tools.ci_gate                      # 跑全部层
    python -m freeproxypool.tools.ci_gate --layers lint,test   # 只跑指定层
    python -m freeproxypool.tools.ci_gate --paths a.py b.md    # 按改动路径决定跑哪些层
    python -m freeproxypool.tools.ci_gate --list               # 列出层与触发规则

退出码：全部通过 0；任一层失败 1（``--strict`` 时"跳过"也算失败）。

**只借鉴结构，不借鉴内容**：分层这一层来自 hproxy 的三个独立 workflow，
但那份 workflow 里藏着凭据外发的恶意后门（见 docs/09 §2.2）——
本文件是**从零写**的，不含任何外发逻辑，也不会读任何凭据。
"""
import argparse
import fnmatch
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from freeproxypool.core.version import find_source_root

#: 全部层
LAYERS = ("lint", "test", "audit", "build")

#: 改动路径 → 需要跑的层（按顺序匹配，命中即累加）
TRIGGER_RULES = (
    ("*.py", ("lint", "test")),
    ("requirements*.txt", ("lint", "test", "audit")),
    ("Dockerfile", ("lint", "test", "build")),
    ("docker-compose.yml", ("lint", "test", "build")),
    ("deploy/**", ("lint", "test", "build")),
    ("tools/**", ("lint", "test")),
    ("VERSION", ("lint", "test")),
    ("*.md", ()),
)

#: 层说明（``--list`` 用）
LAYER_DESC = {
    "lint": "语法与导入验证（compileall）",
    "test": "离线自检套件（proxyPool.py selftest）",
    "audit": "依赖漏洞扫描（pip-audit）",
    "build": "镜像构建（docker build）",
}

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def _match(path: str, pattern: str) -> bool:
    """路径是否命中规则（支持 ``dir/**`` 前缀写法）。"""
    norm = path.replace("\\", "/")
    if pattern.endswith("/**"):
        return norm.startswith(pattern[:-3].lstrip("/") + "/")
    return fnmatch.fnmatch(norm, pattern)


def layers_for_paths(paths) -> list:
    """按改动路径算出需要跑哪些层。"""
    layers = []
    for path in paths or []:
        for pattern, wanted in TRIGGER_RULES:
            if _match(path, pattern):
                for layer in wanted:
                    if layer not in layers:
                        layers.append(layer)
    return [x for x in LAYERS if x in layers]


def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


def _run(cmd, cwd, env=None) -> int:
    """执行子进程（输出直接继承，CI 日志里能看到实时进度）。"""
    print("  $ %s" % " ".join(cmd))
    try:
        return subprocess.call(cmd, cwd=cwd, env=env)
    except OSError as exc:
        print("  执行失败：%s" % exc)
        return 127


# ---------- 各层的实现 ----------


def layer_lint(root: str) -> str:
    """语法与导入验证。"""
    code = _run([sys.executable, "-m", "compileall", "-q", root], root)
    return PASS if code == 0 else FAIL


def layer_test(root: str) -> str:
    """离线自检套件（不连 Redis / MySQL，不产生网络请求）。"""
    env = dict(os.environ)
    src = os.path.join(root, "src")
    if os.path.isdir(src):
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    code = _run([sys.executable, "-m", "freeproxypool", "selftest"], root, env=env)
    return PASS if code == 0 else FAIL


def layer_audit(root: str) -> str:
    """依赖漏洞扫描。没装 pip-audit 就跳过（由 CI 负责装）。"""
    if not _has("pip-audit"):
        print("  未找到 pip-audit，跳过（CI 里应先安装它）")
        return SKIP
    code = _run([sys.executable, "-m", "pip_audit", "-r",
                 os.path.join(root, "requirements.txt")], root)
    return PASS if code == 0 else FAIL


def layer_build(root: str) -> str:
    """镜像构建。没装 docker 就跳过。"""
    if not _has("docker"):
        print("  未找到 docker，跳过镜像构建")
        return SKIP
    code = _run(["docker", "build", "-t", "fpp:ci", "."], root)
    return PASS if code == 0 else FAIL


LAYER_FUNCS = {
    "lint": layer_lint,
    "test": layer_test,
    "audit": layer_audit,
    "build": layer_build,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="分层 CI 门禁（借鉴 docs/09 P2-1）")
    parser.add_argument("--layers", default="",
                        help="要跑哪些层，逗号分隔（默认全部）")
    parser.add_argument("--paths", nargs="*", default=[],
                        help="本次改动的文件路径；给了就按规则决定跑哪些层")
    parser.add_argument("--paths-file", default="",
                        help="从文件读改动路径清单（一行一个；CI 里最省事）")
    parser.add_argument("--list", action="store_true", help="列出层与触发规则")
    parser.add_argument("--strict", action="store_true",
                        help="把「跳过」也算失败（CI 里建议开启）")
    args = parser.parse_args()

    if args.list:
        print("层：")
        for layer in LAYERS:
            print("  %-6s %s" % (layer, LAYER_DESC[layer]))
        print("触发规则（改动路径 → 需要跑的层）：")
        for pattern, wanted in TRIGGER_RULES:
            print("  %-22s → %s" % (pattern, "、".join(wanted) or "（不跑任何层）"))
        return 0

    paths = list(args.paths)
    if args.paths_file:
        try:
            # utf-8-sig：兼容带 BOM 的路径清单（PowerShell 的 Set-Content 会写 BOM）
            with open(args.paths_file, "r", encoding="utf-8-sig") as fp:
                paths.extend([line.strip() for line in fp if line.strip()])
        except OSError as exc:
            print("读不到路径清单 %s：%s" % (args.paths_file, exc))
            return 2

    if paths:
        wanted = layers_for_paths(paths)
        print("按改动路径触发：%s" % ("、".join(paths)))
        if not wanted:
            print("没有需要跑的层（纯文档改动）")
            return 0
    elif args.layers:
        wanted = [x.strip() for x in args.layers.split(",") if x.strip()]
    else:
        wanted = list(LAYERS)

    unknown = [x for x in wanted if x not in LAYERS]
    if unknown:
        print("未知层：%s（可用：%s）" % ("、".join(unknown), "、".join(LAYERS)))
        return 2

    root = find_source_root()
    if root is None or not (root / "VERSION").is_file():
        print("运行门禁检查需要完整的 FreeProxyPool 源码目录。请从项目源码运行。", file=sys.stderr)
        return 2

    print("=" * 56)
    print("FreeProxyPool 分层门禁")
    print("=" * 56)
    results = {}
    for layer in wanted:
        print("\n[%s] %s" % (layer, LAYER_DESC[layer]))
        started = time.time()
        results[layer] = LAYER_FUNCS[layer](str(root))
        print("  → %s（%.1fs）" % (results[layer], time.time() - started))

    print("\n" + "=" * 56)
    for layer in wanted:
        print("  %-6s %s" % (layer, results[layer]))
    failed = [x for x in wanted if results[x] == FAIL]
    skipped = [x for x in wanted if results[x] == SKIP]
    if failed:
        print("失败层：%s" % "、".join(failed))
        return 1
    if skipped and args.strict:
        print("跳过层（--strict 视为失败）：%s" % "、".join(skipped))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
