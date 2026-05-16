"""通过端口号停止 Eurica Quant 服务。"""
import psutil
import sys

TARGET_PORTS = {8888: "P9-API (main.py)", 8081: "P9-Proxy (qmt_proxy_server.py)"}


def find_and_kill(port: int, name: str) -> bool:
    for conn in psutil.net_connections(kind="inet"):
        if conn.laddr.port == port and conn.status == "LISTEN":
            pid = conn.pid
            try:
                proc = psutil.Process(pid)
                proc_str = f"{proc.name()}(PID={pid})"
                proc.kill()
                proc.wait(timeout=3)
                print(f"  [OK] {name} ({proc_str}) 已停止")
                return True
            except psutil.NoSuchProcess:
                print(f"  [INFO] {name} 端口 {port} 的进程已退出")
                return True
            except Exception as e:
                print(f"  [ERROR] 无法停止 {name} 端口 {port}: {e}")
                return False
    print(f"  [INFO] {name} 端口 {port} 未在运行")
    return True


def main():
    print("正在停止 P9 服务...")
    ok = True
    for port, name in TARGET_PORTS.items():
        if not find_and_kill(port, name):
            ok = False
    if ok:
        print("\n所有服务已停止。")
    else:
        print("\n部分服务停止失败，请手动检查。")
        sys.exit(1)


if __name__ == "__main__":
    main()
