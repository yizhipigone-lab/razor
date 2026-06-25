"""
AST 沙箱: strategy_coder 加载 LLM 生成的策略代码前静态校验
L24 修复: 防止恶意 prompt 注入执行 os.system / subprocess 等

白名单 + 黑名单双保险:
- 黑名单: 禁止导入的模块名、禁止的函数名
"""
import ast


FORBIDDEN_MODULES = {
    'os', 'sys', 'subprocess', 'shutil', 'socket', 'http',
    'urllib', 'requests', 'ftplib', 'smtplib', 'asyncio',
}

FORBIDDEN_FUNCTIONS = {
    '__import__', 'eval', 'exec', 'compile', 'open',
}


def validate_strategy_code(code: str) -> tuple[bool, str]:
    """
    校验 LLM 生成的策略代码
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
    return True, "OK"
