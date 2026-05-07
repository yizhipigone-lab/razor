// static/js/error-handler.js
/**
 * 错误处理和降级方案模块
 */

import { wsManager } from './websocket.js';
import { createNotification } from './utils.js';

export class ErrorHandler {
  static handleWebSocketError(error) {
    console.error('WebSocket 错误:', error);
    createNotification('error', '连接失败，正在尝试重新连接...');
    this.enableFallbackMode();
  }

  static handleMarketDataError(error) {
    console.error('行情数据错误:', error);
    createNotification('warning', '行情数据更新失败');
  }

  static handleSubscriptionError(error, codes) {
    console.error('订阅失败:', error);
    createNotification('warning', `部分股票代码无效: ${codes.join(',')}`);
  }

  static enableFallbackMode() {
    const refreshBtn = document.getElementById('refreshMarket');
    if (refreshBtn) {
      refreshBtn.disabled = false;
    }
  }

  static disableFallbackMode() {
    const refreshBtn = document.getElementById('refreshMarket');
    if (refreshBtn) {
      refreshBtn.disabled = true;
    }
  }

  static async attemptManualRefresh() {
    try {
      const response = await fetch('/api/market/quotes');
      const data = await response.json();

      if (data.quotes) {
        createNotification('info', '手动刷新成功');
      }
    } catch (error) {
      console.error('手动刷新失败:', error);
      createNotification('error', '刷新失败');
    }
  }
}