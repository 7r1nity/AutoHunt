"""Print a source-only technical inventory; never import the running application.

Run from any directory with Python 3.12+. --check validates the checked-in
reference against source. Environment values and runtime data are never read.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "autohunt/agent/app"
DOCUMENT = ROOT / "autohunt/docs/technical-reference.md"


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", "")


def link(path, line=1):
    relative = Path(path).relative_to(ROOT / "autohunt").as_posix()
    return f"[{relative}:{line}](../{relative}#L{line})"


def expression(node):
    return ast.unparse(node) if node is not None else ""


def table(headings, rows):
    return ["| " + " | ".join(headings) + " |", "| " + " | ".join("---" for _ in headings) + " |",
            *["| " + " | ".join(cell(item) for item in row) + " |" for row in rows], ""]


def build():
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    paths = sorted(ROOT / p for p in tracked if p.startswith("autohunt/agent/app/") and p.endswith(".py"))
    trees = {p: ast.parse(p.read_text(encoding="utf-8-sig")) for p in paths}
    source_count = sum(len(p.read_text(encoding="utf-8-sig").splitlines()) for p in paths)
    rows = ["# AutoHunt 源码技术索引", "",
        "> 由 `scripts/technical-reference.py` 静态解析受 Git 跟踪的源码生成；不导入应用，不读取环境变量值、数据库、简历或日志。",
        "> 与 [技术报告](technical-report-2026-10-23.md) 配套。行号为生成时位置；仅列定义不能证明生产路径已验收。", "",
        f"后端生产 Python 文件：**{len(paths)}**；物理行数（含注释、空行）：**{source_count:,}**。代码量不是质量或测试覆盖率指标。", ""]
    api = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "app" and decorator.func.attr in {"get", "post", "put", "delete", "patch"}:
                    api.append((decorator.func.attr.upper(), expression(decorator.args[0]), node.name, link(path, node.lineno)))
    rows += ["## 1. 后端 HTTP 路由", "", f"静态装饰器定义共 {len(api)} 项。变量路径保留表达式，权限和限流以中间件及处理函数为准；不假设每个接口都适合 MCP 调用。", ""]
    rows += table(["方法", "路径/表达式", "处理函数", "源码"], api)
    tools = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AgentToolSpec" and len(node.args) >= 5:
                tools.append((node.args[0].value, node.args[3].value, expression(node.args[4]), node.args[1].value, link(path, node.lineno)))
    rows += ["## 2. Agent 工具注册表", "", f"静态注册 {len(tools)} 个工具。effect 是恢复/执行分类，不等同最终风险等级；例如搜索可被归类 external，但普通只读搜索的动作风险是 LOW。", ""]
    rows += table(["工具", "effect", "执行函数", "模型可见说明", "源码"], tools)
    mcp = []
    for path, tree in trees.items():
        if path.relative_to(APP).as_posix() != "mcp/tools.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "boundary_tool" for d in node.decorator_list):
                mcp.append((node.name, ast.get_docstring(node) or "", link(path, node.lineno)))
    rows += ["## 3. stdio MCP 工具", "", f"独立 MCP 工具面静态注册 {len(mcp)} 项，并非上面的 Agent 注册表的透明复制。", ""]
    rows += table(["工具", "说明", "源码"], mcp)
    environments = set()
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "getenv" and node.args and isinstance(node.args[0], ast.Constant):
                environments.add((node.args[0].value, expression(node.args[1]) if len(node.args) > 1 else "None", link(path, node.lineno)))
    rows += ["## 4. 环境变量静态读取点", "", "下表是代码中的默认表达式，不是本机实际配置。动态拼接的配置族另见报告；不要据此公开 `.env`。", ""]
    rows += table(["变量", "默认表达式", "源码"], sorted(environments))
    sql = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for match in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)\s*\(", node.value, re.I):
                    start = match.end(); depth = 1; end = start
                    while end < len(node.value) and depth:
                        depth += (node.value[end] == "(") - (node.value[end] == ")"); end += 1
                    definition = re.sub(r"\s+", " ", node.value[start:end - 1]).strip()
                    sql.append((match.group(1), definition, link(path, node.lineno)))
    rows += ["## 5. 数据表初始定义", "", "静态提取 CREATE TABLE；增量 ALTER/迁移可能添加字段，完整演进以各 Store 初始化函数为准。仅从源码读取，没有连接个人数据库。", ""]
    rows += table(["表", "初始列与约束", "源码"], sorted(sql))
    frontend = [ROOT / p for p in tracked if p.startswith("autohunt/frontend/") and p.endswith((".ts", ".tsx", ".css"))]
    rows += ["## 6. 前端文件", ""]
    rows += table(["文件", "物理行数"], [(link(p), len(p.read_text(encoding="utf-8-sig").splitlines())) for p in frontend])
    rows += ["## 7. 后端完整模块与公共入口", "", "公共类/函数及类方法为静态导航，不意味着所有历史兼容函数仍被主链路调用；以报告的运行链路为准。下划线开头的内部函数不逐项展开，但保留其所在文件。", ""]
    for path, tree in trees.items():
        public = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                methods = [f"{n.name}:{n.lineno}" for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")]
                public.append(f"{node.name}:{node.lineno}" + (" → " + ", ".join(methods) if methods else ""))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                public.append(f"{node.name}:{node.lineno}")
        rows += [f"### {path.relative_to(APP).as_posix()}", "", link(path), "", "\n".join(f"- `{entry}`" for entry in public) or "无公开类/函数定义；可能为包入口、常量或内部实现。", ""]
    return "\n".join(rows).rstrip() + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    document = build()
    if args.check:
        if not DOCUMENT.exists() or DOCUMENT.read_text(encoding="utf-8") != document:
            raise SystemExit("Technical reference differs from tracked source; regenerate it.")
        print("Technical reference matches tracked source.")
    else:
        print(document, end="")
