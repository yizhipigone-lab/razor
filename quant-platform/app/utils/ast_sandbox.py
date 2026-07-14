"""
AST 沙箱: strategy_coder / StrategyFactory 加载用户策略代码前静态校验

白名单 + 黑名单双保险（纵深防御，配合 exec 时的受限 __builtins__）:
- 黑名单: 禁止导入的模块名、禁止的函数名、禁止的属性访问
- 增补: 禁止 dunder 属性访问（防 __class__.__bases__ 等逃逸链）
"""
import ast


FORBIDDEN_MODULES = {
    'os', 'sys', 'subprocess', 'shutil', 'socket', 'http',
    'urllib', 'requests', 'ftplib', 'smtplib', 'asyncio',
    'importlib', 'ctypes', 'multiprocessing', 'signal',
    'pathlib', 'io', 'tempfile', 'pickle', 'marshal', 'runpy', 'pkgutil',
}

FORBIDDEN_FUNCTIONS = {
    '__import__', 'eval', 'exec', 'compile', 'open',
    'getattr', 'setattr', 'delattr', 'type',
    'globals', 'locals', 'vars', 'breakpoint',
}

# 禁止访问的 dunder 属性（防对象模型遍历逃逸）
FORBIDDEN_ATTRS = {
    '__class__', '__bases__', '__subclasses__', '__base__',
    '__globals__', '__builtins__', '__code__', '__closure__',
    '__dict__', '__mro__', '__init__', '__import__',
}


def validate_strategy_code(code: str) -> tuple[bool, str]:
    """
    校验用户提交的策略代码
    Returns: (ok, message)
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误: {e}"

    for node in ast.walk(tree):
        # 检查 import
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split('.')[0]
                if root in FORBIDDEN_MODULES:
                    return False, f"禁止导入模块: {alias.name}"
        if isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split('.')[0]
                if root in FORBIDDEN_MODULES:
                    return False, f"禁止从 {node.module} 导入"
            for alias in node.names:
                if alias.name in FORBIDDEN_FUNCTIONS:
                    return False, f"禁止导入函数: {alias.name}"
        # 检查函数调用
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_FUNCTIONS:
                    return False, f"禁止调用函数: {node.func.id}"
        # 检查属性访问（防 dunder 逃逸链）
        if isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                return False, f"禁止访问属性: {node.attr}"
            if node.attr.startswith('__') and node.attr.endswith('__'):
                return False, f"禁止访问 dunder 属性: {node.attr}"
        # 检查 Name 节点中的 dunder 引用
        if isinstance(node, ast.Name):
            if node.id in FORBIDDEN_ATTRS:
                return False, f"禁止引用 dunder 名称: {node.id}"
    return True, "OK"
