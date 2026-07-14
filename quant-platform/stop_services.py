"""停止 Eurica Quant 本项目所有相关进程（端口 + 工作目录）"""
import psutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET_PORTS = {8888: "API服务", 8001: "实盘交易", 5173: "前端Vite"}

def kill_proc(proc, source):
    try:
        proc.kill()
        proc.wait(timeout=3)
        print(f"  [OK] {proc.name()}(PID={proc.pid}) {source}")
        return True
    except psutil.NoSuchProcess:
        return True
    except Exception as e:
        print(f"  [ERR] PID={proc.pid}: {e}")
        return False


def main():
    print("=" * 50)
    print("  Eurica Quant - 停止所有服务")
    print("=" * 50)

    killed = set()
    ok = True

    # 1. 按端口杀
    for port, name in TARGET_PORTS.items():
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr.port == port and conn.status == "LISTEN":
                pid = conn.pid
                if pid and pid not in killed:
                    try:
                        proc = psutil.Process(pid)
                        kill_proc(proc, f"端口{port}({name})")
                        killed.add(pid)
                    except psutil.NoSuchProcess:
                        pass
        if not any(c.laddr.port == port and c.status == "LISTEN"
                   for c in psutil.net_connections(kind="inet")):
            print(f"  [--] 端口{port}({name}) 未运行")

    # 2. 按工作目录杀（本项目下的 Python/Node 进程）
    for proc in psutil.process_iter(['pid', 'name', 'cwd']):
        try:
            cwd = proc.info.get('cwd')
            if cwd and Path(cwd).resolve().is_relative_to(ROOT):
                pid = proc.info['pid']
                if pid and pid not in killed:
                    kill_proc(proc, f"工作目录({cwd})")
                    killed.add(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    print(f"\n已停止 {len(killed)} 个进程。")
    if len(killed) == 0:
        print("没有发现运行中的项目服务。")


if __name__ == "__main__":
    main()
