// static/js/utils.js
/**
 * 工具函数模块
 */

/**
 * 格式化价格
 * @param {number|string} price - 价格
 * @returns {string} 格式化后的价格字符串
 */
export function formatPrice(price) {
  const numPrice = Number(price);
  return numPrice > 0 ? numPrice.toFixed(2) : '--';
}

/**
 * 格式化百分比
 * @param {number|string} pct - 百分比值
 * @returns {string} 格式化后的百分比字符串
 */
export function formatPercent(pct) {
  const numPct = Number(pct);
  if (isNaN(numPct)) return '--';
  return `${numPct > 0 ? '+' : ''}${numPct.toFixed(2)}%`;
}

/**
 * 获取涨跌幅颜色类
 * @param {number|string} pct - 百分比值
 * @returns {string} CSS 类名
 */
export function getTrendClass(pct) {
  return Number(pct) >= 0 ? 'up' : 'down';
}

/**
 * 格式化代码，支持多种格式转换
 * @param {string} code - 证券代码
 * @returns {string} 格式化后的代码
 */
export function formatCode(code) {
  if (!code) return '';

  const cleanedCode = String(code).trim().toUpperCase();

  if (cleanedCode.includes('.')) {
    return cleanedCode;
  }

  // 尝试自动推断交易所
  if (cleanedCode.startsWith('6') || cleanedCode === '000001') {
    return `${cleanedCode}.SH`;
  } else if (cleanedCode.startsWith(('0', '3')) || cleanedCode.startsWith('399')) {
    return `${cleanedCode}.SZ`;
  } else if (cleanedCode.startsWith('5')) {
    return `${cleanedCode}.SH`;
  }

  return cleanedCode;
}

/**
 * 创建通知
 * @param {string} type - 通知类型 (success, info, warning, error)
 * @param {string} message - 通知内容
 * @param {number} duration - 持续时间(毫秒)，0 表示永久显示
 */
export function createNotification(type, message, duration = 3000) {
  const notification = document.createElement('div');
  notification.className = `notification notification-${type}`;
  notification.textContent = message;

  // 添加样式
  Object.assign(notification.style, {
    position: 'fixed',
    top: '20px',
    right: '20px',
    padding: '12px 16px',
    borderRadius: '8px',
    color: '#fff',
    fontWeight: '500',
    fontSize: '14px',
    boxShadow: '0 4px 12px rgba(0, 0, 0, 0.15)',
    zIndex: '9999',
    maxWidth: '350px',
    wordBreak: 'break-word',
  });

  // 类型对应的背景色
  const bgColors = {
    success: '#3fb950',
    info: '#58a6ff',
    warning: '#d29922',
    error: '#f85149',
  };

  notification.style.backgroundColor = bgColors[type] || bgColors.info;

  // 显示通知
  document.body.appendChild(notification);

  // 自动隐藏
  if (duration > 0) {
    setTimeout(() => {
      if (notification.parentNode) {
        notification.parentNode.removeChild(notification);
      }
    }, duration);
  }

  return notification;
}