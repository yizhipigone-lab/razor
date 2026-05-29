// static/js/market-updater.js
/**
 * 行情更新器模块
 */

import { wsManager } from './websocket.js?v=3';
import { UIRenderer } from './ui-renderer.js?v=2';
import { createNotification } from './utils.js';

export class MarketUpdater {
  constructor() {
    this.uiRenderer = new UIRenderer();
    this.lastUpdateTime = 0;
    this.updateThrottleInterval = 300; // 300ms 节流
    this.pendingUpdates = new Map();
    this.initialize();
  }

  initialize() {
    wsManager.onMessage('market_quotes', (data) => {
      this.handleMarketQuotes(data);
    });
    wsManager.onMessage('connection_status', (data) => {
      this.handleConnectionStatus(data);
    });

    // 连接成功后发送订阅请求
    this.sendSubscribeRequest();
  }

  sendSubscribeRequest() {
    const codes = this.collectAllCodes();
    wsManager.sendMessage('subscribe', { codes });
  }

  /**
   * 重新订阅（自选股变更后调用）
   */
  resubscribe() {
    this.sendSubscribeRequest();
  }

  collectAllCodes() {
    // 订阅所有指数
    const indexCodes = ['000001.SH', '399001.SZ', '399006.SZ', '000905.SH', '000510.SH'];
    // 订阅自选股
    const watchlistCodes = this.getWatchlistCodes();
    // 订阅持仓股
    const positionCodes = this.getPositionCodes();
    // 合并所有代码
    return [...new Set([...indexCodes, ...watchlistCodes, ...positionCodes])];
  }

  getWatchlistCodes() {
    // 从页面中获取自选股代码
    const watchlistRows = document.querySelectorAll('#watchlist-tbody tr.wl-row');
    const codes = [];

    watchlistRows.forEach((tr) => {
      const code = tr.getAttribute('data-code');
      if (code) {
        codes.push(code);
      }
    });

    return codes;
  }

  getPositionCodes() {
    // 从页面中获取持仓股代码
    const positionRows = document.querySelectorAll('#sim-pos-tbody tr.pos-row');
    const codes = [];

    positionRows.forEach((tr) => {
      const code = tr.getAttribute('data-code');
      if (code) {
        codes.push(code);
      }
    });

    return codes;
  }

  handleMarketQuotes(msg) {
    // 提取行情数据（msg 为完整 WebSocket 消息 {type, data}）
    const data = msg.data || msg;
    window._lastQuotes = data;
    const now = Date.now();

    // 节流处理
    if (now - this.lastUpdateTime < this.updateThrottleInterval) {
      for (const [k, v] of Object.entries(data)) {
        this.pendingUpdates.set(k, v);
      }
      return;
    }

    this.lastUpdateTime = now;

    // 合并待处理更新和当前更新
    const updates = { ...Object.fromEntries(this.pendingUpdates), ...data };
    this.pendingUpdates.clear();

    try {
      // 更新指数栏
      this.uiRenderer.updateIndices(updates);

      // 更新表格行
      this.updateTableRows(updates);
    } catch (error) {
      console.error('处理行情数据时出错:', error);
      createNotification('warning', '行情数据更新失败');
    }
  }

  updateTableRows(data) {
    // 使用 requestAnimationFrame 优化渲染
    requestAnimationFrame(() => {
      this.uiRenderer.updateTableRows('#watchlist-tbody tr.wl-row', data);
      this.uiRenderer.updateTableRows('#sim-pos-tbody tr', data);
      this.uiRenderer.updateTableRows('#sim-trade-tbody tr', data);
      this.uiRenderer.updateTableRows('.radar-stock-link', data);
    });
  }

  handleConnectionStatus(data) {
    const status = data.status;
    switch (status) {
      case 'connected':
        createNotification('info', '行情服务器连接成功');
        break;
      case 'disconnected':
        createNotification('error', '行情服务器连接断开');
        break;
      case 'reconnecting':
        createNotification('warning', '行情服务器正在重连...');
        break;
    }
  }
}

export const marketUpdater = new MarketUpdater();