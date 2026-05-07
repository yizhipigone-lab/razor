import re
import pandas as pd

class TDXTranslator:
    """
    通达信公式 -> Python (Pandas) 高级翻译器
    支持多行解析、复杂金叉逻辑处理、以及基础数学函数
    """
    
    def translate(self, tdx_code: str, strategy_name: str = "TdxStrategy") -> str:
        """执行翻译，输出完整的 Python 策略类源码"""
        # 统一转大写，清理冗余
        tdx_code = tdx_code.upper().replace('\r', '')
        lines = tdx_code.split('\n')
        
        final_signal_expr = "None"
        calc_lines = []
        
        # 预定义核心变量映射
        calc_lines.append("        # 基础数据重对齐")
        calc_lines.append("        bars = df.copy()")
        calc_lines.append("        bars.sort_values(['code', 'date'], inplace=True)")
        calc_lines.append("        c = bars['close']")
        calc_lines.append("        o = bars['open']")
        calc_lines.append("        h = bars['high']")
        calc_lines.append("        l = bars['low']")
        calc_lines.append("        v = bars['volume']")
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('{'): continue
            
            # 兼容：A:=... 或 A:... 或 直接表达式
            expr_part = line
            var_name = None
            
            if ':=' in line:
                var_name, expr_part = line.split(':=', 1)
            elif ':' in line:
                var_name, expr_part = line.split(':', 1)
            
            # 清理末尾分号
            expr_part = expr_part.rstrip(';')
            
            # 执行核心转换逻辑
            converted = self._convert_expr(expr_part)
            
            if var_name:
                v_clean = var_name.strip().lower()
                calc_lines.append(f"        {v_clean} = {converted}")
                final_signal_expr = v_clean
            else:
                final_signal_expr = converted

        # 封装为 Python 策略类
        template = f"""import pandas as pd
import numpy as np
from app.screener.strategies.base import BaseStrategy

class {strategy_name}Strategy(BaseStrategy):
    \"\"\"
    通达信自动转译策略: {strategy_name}
    原始公式:
    {tdx_code}
    \"\"\"
    
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty: return pd.DataFrame()
        
{chr(10).join(calc_lines)}
        
        # 最终信号过滤
        bars['buy_signal'] = {final_signal_expr}
        return bars[bars['buy_signal'] == True][['code', 'date']].copy()
"""
        return template

    def _convert_expr(self, expr: str) -> str:
        """递归转换通达信嵌套函数为 Pandas 链式调用"""
        
        # 基础关键字替换 (带边界检查)
        rep = {
            'CLOSE': 'c', 'OPEN': 'o', 'HIGH': 'h', 'LOW': 'l', 'VOL': 'v',
            'C': 'c', 'O': 'o', 'H': 'h', 'L': 'l', 'V': 'v',
            'AND': '&', 'OR': '|', '=': '=='
        }
        for k, v in rep.items():
            expr = re.sub(rf'\b{k}\b', v, expr)
        
        # 函数映射 (正则)
        func_patterns = [
            (r'MA\(([^,]+),(\d+)\)', r"\1.rolling(\2).mean()"),
            (r'EMA\(([^,]+),(\d+)\)', r"\1.ewm(span=\2, adjust=False).mean()"),
            (r'REF\(([^,]+),(\d+)\)', r"\1.shift(\2)"),
            (r'LLV\(([^,]+),(\d+)\)', r"\1.rolling(\2).min()"),
            (r'HHV\(([^,]+),(\d+)\)', r"\1.rolling(\2).max()"),
            (r'ABS\(([^,]+)\)', r"\1.abs()"),
            (r'MAX\(([^,]+),([^,]+)\)', r"np.maximum(\1, \2)"),
            (r'MIN\(([^,]+),([^,]+)\)', r"np.minimum(\1, \2)"),
            # 金叉逻辑特调：CROSS(A, B)
            (r'CROSS\(([^,]+),([^,]+)\)', r"((\1 > \2) & (\1.shift(1) <= \2.shift(1)))"),
            # 条件逻辑：IF(A, B, C) -> np.where(A, B, C)
            (r'IF\(([^,]+),([^,]+),([^,]+)\)', r"np.where(\1, \2, \3)"),
        ]
        
        # 进行多次迭代以支持嵌套，如 MA(C, 5) 的 C 已转换的情况
        for _ in range(3):
            for pattern, subst in func_patterns:
                expr = re.sub(pattern, subst, expr)
                
        return expr

translator = TDXTranslator()

def translate_tdx_to_pandas(name, formula):
    return translator.translate(formula, name)
