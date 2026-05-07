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
          innerResolve(this.ws);
          resolve(this.ws);
        };
        this.ws.onclose = () => {
          this.connected = false;
          this.scheduleReconnect();
        };
        this.ws.onerror = (error) => {
          this.reconnectAttempts++;
          innerReject(error);
          reject(error);
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
    if (this.connected) {
      this.ws.send(JSON.stringify({ type, data }));
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