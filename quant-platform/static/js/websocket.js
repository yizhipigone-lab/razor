// static/js/websocket.js
/**
 * WebSocket 连接管理模块
 */

export class WebSocketManager {
  constructor(url, options = {}) {
    this.url = url;
    this.options = {
      reconnectInterval: 3000,
      maxReconnectAttempts: 5,
      ...options,
    };
    this.ws = null;
    this.connected = false;
    this.reconnectAttempts = 0;
    this.messageHandlers = new Map();
    this.connectPromise = null;
    this.messageQueue = [];
    this._lastSubscribeMsg = null;
  }

  connect() {
    return new Promise((resolve, reject) => {
      // 如果已经在连接中，返回现有 Promise
      if (this.connectPromise) {
        return this.connectPromise;
      }

      this.connectPromise = new Promise((innerResolve, innerReject) => {
        this.ws = new WebSocket(this.url);
        this.ws.onopen = () => {
          this.connected = true;
          this.reconnectAttempts = 0;
          // 发送所有缓存消息
          while (this.messageQueue.length > 0) {
            const msg = this.messageQueue.shift();
            this.ws.send(JSON.stringify(msg));
          }
          // 断线重连后恢复上一次订阅（仅当队列中无订阅消息时）
          if (this._lastSubscribeMsg && !this._sentLastSub) {
            this._sentLastSub = true;
            this.ws.send(JSON.stringify(this._lastSubscribeMsg));
          }
          innerResolve(this.ws);
          resolve(this.ws);
        };
        this.ws.onclose = () => {
          this.connected = false;
          this._sentLastSub = false;  // 允许下次重连后重新订阅
          this.scheduleReconnect();
        };
        this.ws.onerror = () => {
          // onclose 也会触发 scheduleReconnect，不重复计数
          innerReject(new Error('WebSocket error'));
          reject(new Error('WebSocket error'));
        };
        this.ws.onmessage = (e) => this.handleMessage(e);
      });

      return this.connectPromise;
    });
  }

  handleMessage(event) {
    const data = JSON.parse(event.data);
    const handler = this.messageHandlers.get(data.type);
    if (handler) {
      handler(data);
    }
  }

  onMessage(type, handler) {
    this.messageHandlers.set(type, handler);
  }

  offMessage(type) {
    this.messageHandlers.delete(type);
  }

  sendMessage(type, data) {
    // 记住最后一次订阅，断线重连时自动恢复
    if (type === 'subscribe') {
      this._lastSubscribeMsg = { type, data };
    }
    if (this.connected && this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type, data }));
    } else {
      this.messageQueue.push({ type, data });
    }
  }

  scheduleReconnect() {
    if (this.reconnectAttempts < this.options.maxReconnectAttempts) {
      const delay = this.options.reconnectInterval * (this.reconnectAttempts + 1);
      setTimeout(() => {
        this.reconnectAttempts++;
        this.connect().catch(() => {
          console.warn('Reconnect attempt failed');
        });
      }, delay);
    } else {
      console.error('Max reconnection attempts exceeded');
    }
  }

  close() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
      this.connected = false;
      this.connectPromise = null;
    }
  }
}

export const wsManager = new WebSocketManager(`ws://${location.host}/ws`);