import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from core.logger import get_logger
from database.duckdb_manager import db

log = get_logger("StrategyFactory")

STRATEGY_DIR = Path(__file__).parent.parent / "screener" / "strategies"

class StrategyFactory:
    """
    策略管理引擎：负责 Docstring 提取、热加载和代码验证
    """

    def sync_local_strategies(self):
        """扫描本地目录并将策略信息同步到数据库"""
        log.info("同步本地策略目录...")
        py_files = list(STRATEGY_DIR.glob("*.py"))
        
        for p in py_files:
            if p.name in ("__init__.py", "base.py"): 
                continue
                
            strategy_name = p.stem
            try:
                # 动态加载并提取 Docstring
                desc = self.extract_docstring(p)
                # 读取物理代码内容
                with open(p, "r", encoding="utf-8") as f:
                    code_content = f.read()
                # 存入数据库
                db.upsert_strategy(
                    name=strategy_name,
                    description=desc,
                    code_path=str(p.relative_to(Path(__file__).parent.parent.parent)),
                    code_content=code_content # 补齐这一行
                )
            except Exception as e:
                log.error(f"解析策略 {strategy_name} 失败: {e}")

    def extract_docstring(self, file_path: Path) -> str:
        """动态加载模块并寻找 BaseStrategy 子类"""
        module_name = f"app.screener.strategies.{file_path.stem}"
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
            
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        # 寻找继承自 BaseStrategy 的类
        from app.screener.strategies.base import BaseStrategy
        for _, obj in inspect.getmembers(module):
            if inspect.isclass(obj) and issubclass(obj, BaseStrategy) and obj is not BaseStrategy:
                return obj.__doc__.strip() if obj.__doc__ else "已成功识别策略类（暂无描述）"
        return "未发现有效策略类，请确保类继承了 BaseStrategy"

    def save_and_reload(self, name: str, code_content: str):
        """物理保存并刷新数据库"""
        file_path = STRATEGY_DIR / f"{name}.py"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code_content)
        
        # 重新同步元数据
        desc = self.extract_docstring(file_path)
        db.upsert_strategy(
            name=name, 
            description=desc, 
            code_path=str(file_path.relative_to(Path(__file__).parent.parent.parent)),
            code_content=code_content # 确保保存时也回填内容
        )
        return {"status": "ok", "description": desc}

    def delete_local_strategy(self, name: str):
        """物理删除策略文件并清理数据库"""
        file_path = STRATEGY_DIR / f"{name}.py"
        if file_path.exists():
            file_path.unlink()
            log.info(f"物理删除策略文件: {file_path}")
        
    def test_run(self, code_content: str, test_df=None):
        """在零风险环境下测试策略代码是否可跑通（V4.0 全兼容识别版）"""
        import pandas as pd
        import numpy as np
        
        if test_df is None:
            # 制造全兼容 Mock 数据
            test_df = pd.DataFrame({
                "close": np.random.randn(300).cumsum() + 10,
                "open": np.random.randn(300).cumsum() + 10,
                "high": np.random.randn(300).cumsum() + 11,
                "low": np.random.randn(300).cumsum() + 9,
                "volume": np.random.randint(100, 1000, size=300),
                "vol": np.random.randint(100, 1000, size=300)
            })
            # 复制大写列名，防止大小写敏感策略报错
            test_df["Close"] = test_df["close"]
            test_df["Open"] = test_df["open"]
            test_df["High"] = test_df["high"]
            test_df["Low"] = test_df["low"]
            test_df["Volume"] = test_df["volume"]
            
            test_df["date"] = pd.date_range("2024-01-01", periods=300)
            test_df["code"] = "000001.SH" 
            print(f"DEBUG: [test_run] Mock Data Ready, Bars: {len(test_df)}")

        try:
            local_vars = {"pd": pd, "np": np}
            exec(code_content, local_vars)
            
            result_df = pd.DataFrame()
            
            # --- 探测器 A: 寻找类模式 (BaseStrategy 子类) ---
            from app.screener.strategies.base import BaseStrategy
            strategy_cls = None
            for v in local_vars.values():
                if inspect.isclass(v) and issubclass(v, BaseStrategy) and v is not BaseStrategy:
                    strategy_cls = v
                    break
            
            if strategy_cls:
                log.info(f"🧬 [探测器] 识别到类继承策略: {strategy_cls.__name__}")
                inst = strategy_cls()
                result_df = inst.generate_signals(test_df)
            
            # --- 探测器 B: 寻找函数模式 (signal) ---
            elif 'signal' in local_vars and callable(local_vars['signal']):
                log.info("📡 [探测器] 识别到函数式策略: signal()")
                result_df = local_vars['signal'](test_df)
            
            # --- 探测器 C: 寻找桥接函数 (generate_signals) ---
            elif 'generate_signals' in local_vars and callable(local_vars['generate_signals']):
                log.info("📡 [探测器] 识别到桥接函数: generate_signals()")
                result_df = local_vars['generate_signals'](test_df)
            
            else:
                return {"status": "error", "message": "未发现有效入口（需要 BaseStrategy 子类或 signal 函数）"}

            # 确保返回结果是格式化的
            sig_count = len(result_df) if result_df is not None and not result_df.empty else 0
            return {
                "status": "ok", 
                "total_signals": sig_count,
                "message": f"探测到 {sig_count} 个模拟信号",
                "is_empty": sig_count == 0
            }

        except Exception as e:
            import traceback
            err_detail = traceback.format_exc()
            log.error(f"测算引擎内源性崩溃: {e}\n{err_detail}")
            return {"status": "error", "message": f"引擎执行报错: {str(e)}"}

factory = StrategyFactory()
