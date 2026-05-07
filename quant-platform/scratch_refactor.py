import re

def main():
    with open("server.py", "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Find the lines to extract into backtest.py
    # Lines 482 - 693
    # Lines 1018 - 1116
    # Lines 1160 - 1206
    # I will do this manually in python
    pass

if __name__ == "__main__":
    main()
