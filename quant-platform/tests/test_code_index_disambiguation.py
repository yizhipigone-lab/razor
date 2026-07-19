"""代码-指数歧义消解的单测(2026-07-16 修复"平安银行又变成上证指数")。

锁死两条不变量:
- 000001.SZ(平安银行) 必须解成深市股票,严禁被当成 000001.SH(上证指数)。
- 000001.SH / 裸码指数场景仍能正确走沪市指数。

覆盖两处根因:
1. app/utils/xtquant_compat.py 的 is_index_code —— 带后缀的码必须返回 False
   (后缀已确定身份,.SZ=股票;旧实现剥后缀导致 000001.SZ 被误判为上证指数)。
2. app/live_trader/qmt_wrapper.py 的 _format_quote_code —— 传给 xtdata 的查询码
   必须保留 .SZ 后缀,不能把 000001.SZ 查成 000001.SH 指数。
"""
from app.utils.xtquant_compat import is_index_code


class TestIsIndexCode:
    def test_bare_known_index_is_index(self):
        # 裸码 000001 在 _CSI_INDEX_CODES(上证指数) → True
        assert is_index_code("000001") is True
        assert is_index_code("000300") is True
        assert is_index_code("000905") is True

    def test_bare_stock_is_not_index(self):
        # 裸码 600000(浦发银行)/000002(万科) 不在指数表 → False
        assert is_index_code("600000") is False
        assert is_index_code("000002") is False

    def test_suffixed_code_never_index_even_if_bare_collides(self):
        # ★核心★ 带后缀的码后缀已确定身份,必须返回 False,不再按裸码反查指数表。
        # 000001.SZ = 平安银行(股票),不得被误判为上证指数。
        assert is_index_code("000001.SZ") is False
        assert is_index_code("000001.SH") is False
        assert is_index_code("000300.SH") is False
        assert is_index_code("600000.SH") is False

    def test_empty_and_none(self):
        assert is_index_code("") is False
        assert is_index_code(None) is False  # type: ignore[arg-type]


class TestFormatQuoteCode:
    """qmt_wrapper._format_quote_code: 决定传给 xtdata.get_full_tick 的查询码。"""

    def test_sz_stock_keeps_sz_suffix(self):
        # ★核心★ 000001.SZ(平安银行) 必须查 .SZ,不能查成 .SH 指数
        from app.live_trader.qmt_wrapper import _format_quote_code
        assert _format_quote_code("000001.SZ") == "000001.SZ"

    def test_sh_stock_keeps_sh_suffix(self):
        from app.live_trader.qmt_wrapper import _format_quote_code
        assert _format_quote_code("600000.SH") == "600000.SH"

    def test_bare_ambiguous_index_goes_sh(self):
        # 裸码 000001(指数表成员) → 走 .SH(上证指数);指数查询场景用裸码时仍正确
        from app.live_trader.qmt_wrapper import _format_quote_code
        assert _format_quote_code("000001") == "000001.SH"
        assert _format_quote_code("000300") == "000300.SH"

    def test_bare_stock_gets_proper_suffix(self):
        # 裸码 600000 → .SH;000002 → .SZ
        from app.live_trader.qmt_wrapper import _format_quote_code
        assert _format_quote_code("600000") == "600000.SH"
        assert _format_quote_code("000002") == "000002.SZ"

    def test_etf_keeps_suffix(self):
        from app.live_trader.qmt_wrapper import _format_quote_code
        assert _format_quote_code("159226.SZ") == "159226.SZ"
