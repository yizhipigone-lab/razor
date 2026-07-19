// Playwright 最小配置 — 仅跑 tests/e2e/ 下的前端往返端到端测试。
// 跑法: npx playwright test --config tests/e2e/playwright.config.js
//       BASE_URL=http://localhost:8000 npx playwright test --config tests/e2e/playwright.config.js
// @ts-check
const path = require('path');

/** @type {import('@playwright/test').PlaywrightTestConfig} */
module.exports = {
  testDir: path.join(__dirname),
  fullyParallel: false,          // 风控配置往返有状态依赖, 必须串行
  retries: 0,                    // 失败不重试(避免反复改 risk 配置)
  workers: 1,
  timeout: 60_000,
  use: {
    headless: true,
    baseURL: process.env.BASE_URL || 'http://localhost:8000',
    actionTimeout: 15_000,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
};
