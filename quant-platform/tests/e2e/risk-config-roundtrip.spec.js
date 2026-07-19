// @ts-check
/**
 * 前端风控配置往返端到端测试。
 *
 * 目的: 防止 saveRiskSettings / loadSettings 的 /100 单位 bug 回归。
 *       2026-07-20 该 bug 在多轮审计(07-13/15/19)漏查, 后补的回归保护。
 *
 * 链路:
 *   填 -60 → 点"保存此卡" → GET /api/settings 断言存 -60(不是 -0.6)
 *   → reload → 断言输入框回显 -60(不是 -0.6)
 *   任一跳变成 -0.6 = /100 bug 回归。
 *
 * 跑法见 README.md。实盘(mode=live)交易时段勿跑(会短暂改 hard_stop)。
 */
const { test, expect, request } = require('@playwright/test');

test.describe.serial('风控配置往返 (前端 /100 回归保护)', () => {
  /** @type {import('@playwright/test').APIRequestContext} */
  let api;
  let origHardStop;

  test.beforeAll(async () => {
    api = await request.newContext({
      baseURL: process.env.BASE_URL || 'http://localhost:8000',
    });
    const r = await api.get('/api/settings');
    const body = await r.json();
    origHardStop = body.risk?.hard_stop_loss_pct;
    // eslint-disable-next-line no-console
    console.log(`[备份] hard_stop_loss_pct = ${origHardStop}`);
  });

  test.afterAll(async () => {
    if (api && origHardStop !== undefined) {
      await api.post('/api/settings/risk-params', {
        data: { hard_stop: origHardStop },
      });
      // eslint-disable-next-line no-console
      console.log(`[恢复] hard_stop_loss_pct = ${origHardStop}`);
    }
    await api?.dispose();
  });

  test('填 -60 保存 → GET 存 -60 (saveRiskSettings 未误 /100)', async ({ page }) => {
    await page.goto(process.env.BASE_URL || 'http://localhost:8000');
    const stopInput = page.locator('#set-stop');
    await stopInput.waitFor({ timeout: 15000 });
    await page.waitForTimeout(1200);  // 给 loadSettings 填充现有值的时间

    // force: set-stop 可能在折叠 tab 内, 强制填值(saveRiskSettings 靠点按钮触发, 不依赖 input 事件)
    await stopInput.fill('-60', { force: true });
    await page.getByRole('button', { name: '保存此卡' }).click();
    await page.waitForTimeout(2500);  // 等 POST /api/settings/risk-params + settings.save 落盘

    const r = await api.get('/api/settings');
    const stored = (await r.json()).risk?.hard_stop_loss_pct;
    // eslint-disable-next-line no-console
    console.log(`[存入] hard_stop_loss_pct = ${stored}`);
    expect(
      Number(stored),
      `填 -60 应存 -60; 实际 ${stored}。若 = -0.6 → saveRiskSettings 又多了 /100(回归 bug)`,
    ).toBe(-60);
  });

  test('reload 后回显 -60 (loadSettings 未误 *100)', async ({ page }) => {
    // 依赖上一 test 已把 settings 存成 -60 (describe.serial 顺序执行)
    await page.goto(process.env.BASE_URL || 'http://localhost:8000');
    const stopInput = page.locator('#set-stop');
    await stopInput.waitFor({ timeout: 15000 });
    await page.waitForTimeout(1200);
    const displayed = await stopInput.inputValue();
    // eslint-disable-next-line no-console
    console.log(`[回显] set-stop = ${displayed}`);
    expect(
      Number(displayed),
      `应回显 -60; 实际 ${displayed}。若 = -0.6 → loadSettings/保存链路又有 /100`,
    ).toBe(-60);
  });
});
