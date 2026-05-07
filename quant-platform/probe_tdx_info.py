from pytdx2.hq import TdxHq_API

def probe():
    api = TdxHq_API()
    if api.connect("180.153.18.170", 7709):
        # 尝试获取财务数据（行业分类通常包含在内）
        info = api.get_finance_info(0, "000001")
        print("平安银行财务元数据采样:")
        print(info)
        api.disconnect()

if __name__ == '__main__':
    probe()
