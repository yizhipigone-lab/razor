"""
通达信公式 → Python (Pandas + MyTT) 翻译器 V2

架构: 递归下降解析器（替代正则），翻译目标为调用 app.indicators.MyTT 函数。

相比 V1 的改进:
1. 正确处理任意深度嵌套函数（V1 的 [^,]+ 正则在嵌套逗号处截断）
2. 自动获得 MyTT 全部 40+ 函数覆盖（V1 仅 10 个）
3. 与手写策略数值一致（同源于 MyTT）
4. 支持 := / : 两种赋值、{注释}、CODELIKE 等字符串函数

接口保持兼容: translator.translate(formula, name) → python_code_str
"""
import ast
import re
from dataclasses import dataclass, field
from typing import List, Optional, Union


# ─── TDX 函数 → MyTT 函数映射 ───────────────────────────────────
# MyTT 函数签名见 app/indicators/MyTT.py
TDX_FUNC_MAP = {
    # 0级核心
    'MA': 'MA', 'EMA': 'EMA', 'SMA': 'SMA', 'WMA': 'WMA', 'DMA': 'DMA',
    'REF': 'REF', 'DIFF': 'DIFF', 'STD': 'STD', 'SUM': 'SUM',
    'HHV': 'HHV', 'LLV': 'LLV', 'HHVBARS': 'HHVBARS', 'LLVBARS': 'LLVBARS',
    'AVEDEV': 'AVEDEV', 'SLOPE': 'SLOPE', 'FORCAST': 'FORCAST', 'LAST': 'LAST',
    'CONST': 'CONST', 'IF': 'IF', 'MAX': 'MAX', 'MIN': 'MIN', 'ABS': 'ABS',
    'LN': 'LN', 'POW': 'POW', 'SQRT': 'SQRT',
    'SIN': 'SIN', 'COS': 'COS', 'TAN': 'TAN',
    # 1级应用
    'COUNT': 'COUNT', 'EVERY': 'EVERY', 'EXIST': 'EXIST', 'FILTER': 'FILTER',
    'BARSLAST': 'BARSLAST', 'BARSLASTCOUNT': 'BARSLASTCOUNT', 'BARSSINCEN': 'BARSSINCEN',
    'CROSS': 'CROSS', 'LONGCROSS': 'LONGCROSS', 'VALUEWHEN': 'VALUEWHEN',
    'BETWEEN': 'BETWEEN', 'TOPRANGE': 'TOPRANGE', 'LOWRANGE': 'LOWRANGE',
    # 2级技术指标（多返回值指标需特殊处理）
    'MACD': 'MACD', 'KDJ': 'KDJ', 'RSI': 'RSI', 'BOLL': 'BOLL',
    'ATR': 'ATR', 'CCI': 'CCI', 'WR': 'WR', 'BIAS': 'BIAS',
    'BBI': 'BBI', 'MTM': 'MTM', 'ROC': 'ROC', 'OBV': 'OBV',
    'VR': 'VR', 'CR': 'CR', 'TRIX': 'TRIX', 'EMV': 'EMV',
    'DPO': 'DPO', 'BRAR': 'BRAR', 'MASS': 'MASS', 'EXPMA': 'EXPMA',
    'MFI': 'MFI', 'ASI': 'ASI', 'DMI': 'DMI', 'DFMA': 'DFMA',
    # ATAN: MyTT 无，映射到 np.arctan
    'ATAN': '__atan__',
}

# 多返回值指标：翻译为元组解包
MULTI_RETURN = {
    'MACD': ('DIF', 'DEA', 'MACD'),
    'KDJ': ('K', 'D', 'J'),
    'RSI': ('RSI1',),
    'BOLL': ('UPPER', 'MID', 'LOWER'),
    'WR': ('WR1', 'WR2'),
    'BIAS': ('BIAS1', 'BIAS2', 'BIAS3'),
    'ATR': ('ATR',),
    'DMI': ('PDI', 'MDI', 'ADX', 'ADXR'),
}

# 变量映射: TDX 价量关键字 → Python 变量
VAR_MAP = {
    'CLOSE': 'c', 'C': 'c',
    'OPEN': 'o', 'O': 'o',
    'HIGH': 'h', 'H': 'h',
    'LOW': 'l', 'L': 'l',
    'VOL': 'v', 'V': 'v',
    'AMOUNT': 'amount',
}

# 不翻译为 MyTT 调用的特殊函数（生成内联辅助代码）
SPECIAL_FUNCS = {'CODELIKE', 'NAMELIKE', 'INBLOCK', 'PLOYLINE', 'DRAWLINE', 'DRAWICON'}


# ─── Tokenizer ──────────────────────────────────────────────────
@dataclass
class Token:
    type: str  # 'ID' 'NUM' 'STR' 'OP' 'ASSIGN' 'EOF'
    value: str


def _tokenize(text: str) -> List[Token]:
    """将 TDX 公式分词。已先 upper()，故关键字统一大写。"""
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # 跳过空白
        if ch.isspace():
            i += 1
            continue
        # 块注释 { ... }
        if ch == '{':
            j = text.find('}', i)
            i = (j + 1) if j >= 0 else n
            continue
        # 行注释 // 或 (* *)
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            j = text.find('\n', i)
            i = n if j < 0 else j
            continue
        if ch == '(' and i + 1 < n and text[i + 1] == '*':
            j = text.find('*)', i)
            i = (j + 2) if j >= 0 else n
            continue
        # 字符串 '...'
        if ch == "'":
            j = i + 1
            while j < n and text[j] != "'":
                j += 1
            tokens.append(Token('STR', text[i + 1:j]))
            i = j + 1
            continue
        # 标识符: 字母/中文/下划线 + 数字
        if ch.isalpha() or ch == '_' or '一' <= ch <= '鿿':
            j = i
            while j < n and (text[j].isalnum() or text[j] == '_' or '一' <= text[j] <= '鿿'):
                j += 1
            tokens.append(Token('ID', text[i:j]))
            i = j
            continue
        # 数字
        if ch.isdigit() or (ch == '.' and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] == '.'):
                j += 1
            tokens.append(Token('NUM', text[i:j]))
            i = j
            continue
        # 赋值 := 或 :
        if ch == ':' and i + 1 < n and text[i + 1] == '=':
            tokens.append(Token('ASSIGN', ':='))
            i += 2
            continue
        if ch == ':':
            tokens.append(Token('ASSIGN', ':'))
            i += 1
            continue
        # 双字符运算符须在单字符之前判断
        if ch == '>' and i + 1 < n and text[i + 1] == '=':
            tokens.append(Token('OP', '>='))
            i += 2
            continue
        if ch == '<' and i + 1 < n and text[i + 1] == '=':
            tokens.append(Token('OP', '<='))
            i += 2
            continue
        if ch == '&' and i + 1 < n and text[i + 1] == '&':
            tokens.append(Token('OP', '&'))
            i += 2
            continue
        if ch == '|' and i + 1 < n and text[i + 1] == '|':
            tokens.append(Token('OP', '|'))
            i += 2
            continue
        # 单字符运算符
        if ch in '+-*/()<>;,':
            tokens.append(Token('OP', ch))
            i += 1
            continue
        if ch == '=':
            tokens.append(Token('OP', '=='))
            i += 1
            continue
        # 未知字符，跳过
        i += 1
    tokens.append(Token('EOF', ''))
    return tokens


# ─── AST 节点 ───────────────────────────────────────────────────
@dataclass
class Num: value: str
@dataclass
class Str: value: str
@dataclass
class Var: name: str
@dataclass
class BinOp:
    op: str
    left: object
    right: object
@dataclass
class UnaryOp:
    op: str
    operand: object
@dataclass
class Call:
    func: str
    args: list
@dataclass
class Assign:
    name: str
    value: object
    is_output: bool  # True 表示 `:` 输出变量


# ─── 递归下降解析器 ──────────────────────────────────────────────
class _Parser:
    def __init__(self, tokens: List[Token]):
        self.toks = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.toks[self.pos]

    def next(self) -> Token:
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def expect_op(self, op: str):
        t = self.next()
        if t.type != 'OP' or t.value != op:
            raise SyntaxError(f"期望 '{op}'，实际 '{t.value}' (type={t.type})")

    def parse_program(self) -> List[Assign]:
        """解析整段公式为赋值语句列表。末尾无赋值的裸表达式视为输出信号。"""
        stmts = []
        while self.peek().type != 'EOF':
            stmt = self._parse_statement()
            if stmt is not None:
                stmts.append(stmt)
        return stmts

    def _parse_statement(self) -> Optional[Assign]:
        t = self.peek()
        # 跳过多余分号
        if t.type == 'OP' and t.value == ';':
            self.next()
            return None
        # 形如 NAME := expr 或 NAME : expr
        if t.type == 'ID':
            # 向前看: ID 后紧跟 ASSIGN
            if self.pos + 1 < len(self.toks) and self.toks[self.pos + 1].type == 'ASSIGN':
                name = self.next().value
                assign_tok = self.next()
                expr = self._parse_expr()
                self._consume_semicolon()
                return Assign(name=name, value=expr, is_output=(assign_tok.value == ':'))
        # 裸表达式 → 输出信号
        expr = self._parse_expr()
        self._consume_semicolon()
        return Assign(name='_signal_out', value=expr, is_output=True)

    def _consume_semicolon(self):
        if self.peek().type == 'OP' and self.peek().value == ';':
            self.next()

    # 表达式优先级: OR < AND < 比较 < 加减 < 乘除 < 一元 < 原子
    def _parse_expr(self):
        return self._parse_or()

    def _parse_or(self):
        left = self._parse_and()
        while self.peek().type == 'ID' and self.peek().value == 'OR':
            self.next()
            right = self._parse_and()
            left = BinOp('|', left, right)
        return left

    def _parse_and(self):
        left = self._parse_compare()
        while self.peek().type == 'ID' and self.peek().value == 'AND':
            self.next()
            right = self._parse_compare()
            left = BinOp('&', left, right)
        return left

    def _parse_compare(self):
        left = self._parse_add()
        while self.peek().type == 'OP' and self.peek().value in ('>', '<', '>=', '<=', '=='):
            op = self.next().value
            right = self._parse_add()
            left = BinOp(op, left, right)
        return left

    def _parse_add(self):
        left = self._parse_mul()
        while self.peek().type == 'OP' and self.peek().value in ('+', '-'):
            op = self.next().value
            right = self._parse_mul()
            left = BinOp(op, left, right)
        return left

    def _parse_mul(self):
        left = self._parse_unary()
        while self.peek().type == 'OP' and self.peek().value in ('*', '/'):
            op = self.next().value
            right = self._parse_unary()
            left = BinOp(op, left, right)
        return left

    def _parse_unary(self):
        if self.peek().type == 'OP' and self.peek().value in ('+', '-'):
            op = self.next().value
            operand = self._parse_unary()
            return UnaryOp(op, operand)
        return self._parse_atom()

    def _parse_atom(self):
        t = self.peek()
        if t.type == 'NUM':
            self.next()
            return Num(t.value)
        if t.type == 'STR':
            self.next()
            return Str(t.value)
        if t.type == 'OP' and t.value == '(':
            self.next()
            expr = self._parse_expr()
            self.expect_op(')')
            return expr
        if t.type == 'ID':
            name = self.next().value
            # 函数调用
            if self.peek().type == 'OP' and self.peek().value == '(':
                self.next()
                args = []
                if not (self.peek().type == 'OP' and self.peek().value == ')'):
                    args.append(self._parse_expr())
                    while self.peek().type == 'OP' and self.peek().value == ',':
                        self.next()
                        args.append(self._parse_expr())
                self.expect_op(')')
                return Call(func=name, args=args)
            # 变量
            return Var(name=name)
        raise SyntaxError(f"意外的 token: {t.value} (type={t.type})")


# ─── 代码生成器 ─────────────────────────────────────────────────
class _CodeGen:
    def __init__(self):
        self.used_mytt_funcs: set = set()
        self.used_specials: list = []  # 需要内联辅助的特殊函数
        self.bool_paren_depth = 0

    def gen(self, stmts: List[Assign]) -> str:
        lines = []
        signal_var = None
        for i, stmt in enumerate(stmts):
            py_name = self._py_var_name(stmt.name)
            if stmt.is_output and i == len(stmts) - 1:
                signal_var = py_name
            expr_code = self._gen_expr(stmt.value)
            lines.append(f"        {py_name} = {expr_code}")
        if signal_var is None and stmts:
            signal_var = self._py_var_name(stmts[-1].name)
        return self._wrap_template(lines, signal_var)

    def _py_var_name(self, name: str) -> str:
        """TDX 变量名 → Python 小写下划线风格。"""
        if name in VAR_MAP:
            return VAR_MAP[name]
        if name == '_signal_out':
            return '_signal_out'
        return name.lower()

    def _gen_expr(self, node) -> str:
        if isinstance(node, Num):
            return node.value
        if isinstance(node, Str):
            return f"'{node.value}'"
        if isinstance(node, Var):
            if node.name in VAR_MAP:
                return VAR_MAP[node.name]
            # 其他标识符当普通变量（小写）
            return node.name.lower()
        if isinstance(node, UnaryOp):
            operand = self._gen_expr(node.operand)
            return f"({node.op}{operand})"
        if isinstance(node, BinOp):
            left = self._gen_expr(node.left)
            right = self._gen_expr(node.right)
            # 布尔运算需加括号防优先级问题
            if node.op in ('&', '|'):
                return f"({left} {node.op} {right})"
            return f"({left} {node.op} {right})"
        if isinstance(node, Call):
            return self._gen_call(node)
        raise ValueError(f"未知节点类型: {type(node)}")

    def _gen_call(self, node: Call) -> str:
        fname = node.func
        args_code = [self._gen_expr(a) for a in node.args]

        # 特殊函数
        if fname == 'CODELIKE':
            if not args_code:
                return "False"
            self.used_specials.append('CODELIKE')
            return f"_code_like(codes, {args_code[0]})"
        if fname == 'NAMELIKE':
            if not args_code:
                return "False"
            self.used_specials.append('NAMELIKE')
            return f"_name_like(names, {args_code[0]})"
        if fname == 'ATAN':
            return f"np.arctan({args_code[0]})"
        if fname in ('PLOYLINE', 'DRAWLINE', 'DRAWICON'):
            # 绘图函数：返回 value 参数（降级处理，附注释）
            return f"{args_code[1] if len(args_code) > 1 else (args_code[0] if args_code else 'None')}  # NOTE: {fname} 绘图函数已降级为返回值序列"

        # MyTT 映射
        mytt_name = TDX_FUNC_MAP.get(fname)
        if mytt_name is None:
            # 未知函数：原样保留，附警告注释
            return f"{fname.lower()}({', '.join(args_code)})  # WARN: 未知函数 {fname}，请人工核对"
        self.used_mytt_funcs.add(mytt_name)
        return f"{mytt_name}({', '.join(args_code)})"

    def _wrap_template(self, body_lines: list, signal_var: Optional[str]) -> str:
        mytt_imports = sorted(self.used_mytt_funcs)
        import_line = (
            f"from app.indicators import {', '.join(mytt_imports)}"
            if mytt_imports else "# 无 MyTT 依赖"
        )
        helpers = ""
        if 'CODELIKE' in self.used_specials or 'NAMELIKE' in self.used_specials:
            helpers = """
def _code_like(codes, prefix):
    return codes.str.startswith(str(prefix))

def _name_like(names, pattern):
    return names.str.contains(str(pattern), na=False)
"""
        signal_expr = signal_var or "pd.Series(False, index=bars.index)"
        return f'''import pandas as pd
import numpy as np
from app.screener.strategies.base import BaseStrategy
{import_line}
{helpers}

class TdxTranslatedStrategy(BaseStrategy):
    """
    通达信公式自动转译策略 (V2 递归下降解析器)
    """

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        bars = df.copy()
        bars = bars.sort_values(['code', 'date']).reset_index(drop=True)
        c = bars['close']
        o = bars['open']
        h = bars['high']
        l = bars['low']
        v = bars['volume']
        # 注: TDX 的 VOL 单位是"手"(1手=100股)，若数据源 volume 单位是"股"需在此除以100
        codes = bars['code'] if 'code' in bars.columns else pd.Series(index=bars.index)
        names = bars.get('name', pd.Series(index=bars.index))

{chr(10).join(body_lines)}

        signal_raw = {signal_expr}
        # MyTT 函数返回 numpy 数组，统一转为 Series 以便 fillna
        signal = pd.Series(signal_raw, index=bars.index) if not isinstance(signal_raw, pd.Series) else signal_raw
        signal = signal.fillna(False).astype(bool)
        return bars[signal][['code', 'date']].copy() if 'code' in bars.columns else bars[signal].copy()
'''


# ─── 对外接口 ───────────────────────────────────────────────────
class TDXTranslator:
    """
    通达信公式 -> Python (Pandas + MyTT) 翻译器 V2
    """

    def translate(self, tdx_code: str, strategy_name: str = "TdxStrategy") -> str:
        """执行翻译，输出完整的 Python 策略类源码。"""
        # 统一转大写处理关键字（保留字符串内容）
        normalized = self._normalize(tdx_code)
        tokens = _tokenize(normalized)
        parser = _Parser(tokens)
        stmts = parser.parse_program()
        gen = _CodeGen()
        code = gen.gen(stmts)
        # 替换类名
        code = code.replace('TdxTranslatedStrategy', f"{strategy_name}Strategy")
        return code

    def _normalize(self, text: str) -> str:
        """转大写但保护字符串字面量内容。"""
        result = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "'":
                j = i + 1
                while j < len(text) and text[j] != "'":
                    j += 1
                result.append(text[i:j + 1])
                i = j + 1
            else:
                result.append(ch.upper())
                i += 1
        return ''.join(result)


translator = TDXTranslator()


def translate_tdx_to_pandas(name, formula):
    """兼容旧接口。"""
    return translator.translate(formula, name)
