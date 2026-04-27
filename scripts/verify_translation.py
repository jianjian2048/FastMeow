"""验证翻译只动 docstring 和注释，代码 AST 严格等价。

用法: python scripts/verify_translation.py

对每个被修改的 .py 文件：
  1. 从 HEAD 读取原文
  2. 从工作树读取改后版本
  3. 解析两边 AST
  4. 移除所有 docstring 节点（模块/函数/类首位的 Expr(Constant(str))）
  5. ast.dump 比较
任何不等价 → 报错并列文件 / 节点路径。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FILES = [
    "examples/basic_echo.py",
    "examples/broadcast.py",
    "examples/command_bot.py",
    "examples/multi_account.py",
    "tests/__init__.py",
    "tests/_smoke_e2e.py",
    "tests/_smoke_supervisor.py",
    "tests/_smoke_transport.py",
    "tests/test_app.py",
    "tests/test_context.py",
    "tests/test_dispatcher.py",
    "tests/test_filters.py",
    "tests/test_manifest.py",
    "tests/test_router.py",
    "tests/test_types.py",
]


def strip_docstrings(tree: ast.AST) -> ast.AST:
    """递归移除 Module/FunctionDef/AsyncFunctionDef/ClassDef 的首位字符串表达式。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return tree


def get_head_content(rel: str) -> str:
    out = subprocess.run(
        ["git", "show", f"HEAD:{rel}"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return out.stdout.decode("utf-8")


def main() -> int:
    failed: list[str] = []
    for rel in FILES:
        path = ROOT / rel
        new_src = path.read_text(encoding="utf-8")
        old_src = get_head_content(rel)
        try:
            old_tree = strip_docstrings(ast.parse(old_src, filename=f"HEAD:{rel}"))
            new_tree = strip_docstrings(ast.parse(new_src, filename=rel))
        except SyntaxError as e:
            print(f"FAIL  {rel}: SyntaxError {e}")
            failed.append(rel)
            continue

        old_dump = ast.dump(old_tree, annotate_fields=True, include_attributes=False)
        new_dump = ast.dump(new_tree, annotate_fields=True, include_attributes=False)

        if old_dump == new_dump:
            print(f"OK    {rel}")
        else:
            print(f"FAIL  {rel}: AST mismatch (code structure changed)")
            # 提示：找出第一处差异附近的字符位置
            for i, (a, b) in enumerate(zip(old_dump, new_dump, strict=False)):
                if a != b:
                    ctx_start = max(0, i - 60)
                    print(f"  diff @ char {i}")
                    print(f"  HEAD: ...{old_dump[ctx_start:i+80]}...")
                    print(f"  NEW : ...{new_dump[ctx_start:i+80]}...")
                    break
            else:
                # 长度不同
                print(f"  length: HEAD={len(old_dump)} NEW={len(new_dump)}")
            failed.append(rel)

    print()
    if failed:
        print(f"FAILED: {len(failed)}/{len(FILES)} files")
        for f in failed:
            print(f"  - {f}")
        return 1
    print(f"PASS: {len(FILES)}/{len(FILES)} files AST-equivalent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
