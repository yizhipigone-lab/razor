from pytdx2.hq import TdxHq_API

def probe():
    api = TdxHq_API()
    if api.connect("180.153.18.170", 7709):
        # 批量获取三只代表股：平安银行、万科A、宁德时代
        # 0 代表深市，1 代表沪市
        quotes = api.get_security_quotes([(0, "000001"), (0, "000002"), (0, "300750")])
        for q in quotes:
            print(f"代码: {q['code']} | 名称: {q.get('name', 'N/A')} | 行业字段: {q.get('industry', 'N/A')}")
        api.disconnect()

if __name__ == '__main__':
    probe()
