// static/js/ui-renderer.js
/**
 * UI 渲染器模块
 */

import { formatPrice, formatPercent, getTrendClass } from './utils.js';

export class UIRenderer {
  constructor() {
    this.indexIds = ['sh', 'sz', 'cy', 'zz500', 'a500'];
  }

  /**
   * 更新指数栏
   * @param {string} id - 指数 ID (sh, sz, cy, zz500, a500)
   * @param {Object} data - 指数数据
   */
  updateIndex(id, data) {
    const price = document.getElementById(`${id}-price`);
    const pct = document.getElementById(`${id}-pct`);

    if (price) {
      price.textContent = formatPrice(data.lastPrice || data.price);
    }

    if (pct) {
      pct.textContent = formatPercent(data.priceChangeRatio || data.change_pct || 0);
      pct.className = getTrendClass(data.priceChangeRatio || data.change_pct || 0);
    }
  }

  /**
   * 更新指数栏的所有指数
   * @param {Object} quotes - 所有指数行情数据
   */
  updateIndices(quotes) {
    this.indexIds.forEach(id => {
      const code = this.getIndexCode(id);
      if (quotes[code]) {
        this.updateIndex(id, quotes[code]);
      }
    });
  }

  /**
   * 根据指数 ID 获取对应的证券代码
   * @param {string} id - 指数 ID
   * @returns {string} 证券代码
   */
  getIndexCode(id) {
    const indexMapping = {
      sh: '000001.SH',
      sz: '399001.SZ',
      cy: '399006.SZ',
      zz500: '000905.SH',
      a500: '000510.SH',
    };
    return indexMapping[id];
  }

  /**
   * 更新表格行的价格和涨跌幅
   * @param {Element} tr - 表格行元素
   * @param {Object} info - 股票行情数据
   */
  updateQuoteRow(tr, info) {
    const pEl = tr.querySelector('.live-price');
    const pctEl = tr.querySelector('.live-pct');

    if (!pEl && !pctEl) return;

    const price = info.lastPrice || info.price;
    const preClose = info.lastClose || info.preClose;
    const changePercent = info.priceChangeRatio || info.change_pct;

    if (pEl) {
      pEl.textContent = formatPrice(price);
    }

    if (pctEl) {
      const pct = changePercent !== undefined
        ? changePercent
        : preClose && price
        ? ((price - preClose) / preClose * 100)
        : 0;

      pctEl.textContent = formatPercent(pct);
      pctEl.className = getTrendClass(pct);
    }
  }

  /**
   * 更新指定表格的所有行情数据
   * @param {string} selector - CSS 选择器
   * @param {Object} data - 行情数据
   */
  updateTableRows(selector, data) {
    const rows = document.querySelectorAll(selector);
    rows.forEach((tr) => {
      const code = tr.getAttribute('data-code');
      if (!code) return;

      const quote = this.findQuote(data, code);
      if (quote) {
        this.updateQuoteRow(tr, quote);
      }
    });
  }

  /**
   * 查找股票行情数据（支持多种代码格式）
   * @param {Object} data - 行情数据
   * @param {string} code - 股票代码
   * @returns {Object|null} 股票行情数据
   */
  findQuote(data, code) {
    return data[code] || data[code + '.SH'] || data[code + '.SZ'] || null;
  }
}