// 真实浏览器驱动一键升级（不使用任何 API 直调触发）：
// 设置管理员密码 → 进入工作台 → 系统设置 → 版本与升级 → 检查更新 → 升级到 vX → 确认。
//
// 升级的**触发**完全来自真实点击；轮询阶段读取边车 /upgrade/status 只是观察
// 完成时机（页面上的进度条读的也是同一个端点），避免盲等 10 分钟。
const { chromium } = await import('/home/ztb/.local/lib/node_modules/@playwright/mcp/node_modules/playwright/index.mjs')
const fs = await import('node:fs')

const BASE = process.env.BASE || 'http://127.0.0.1:9399'
const UPDATER = process.env.UPDATER || 'http://127.0.0.1:9299'
const TOKEN = process.env.UPDATER_TOKEN || 'e2e-token'
const PASSWORD = process.env.PW_PASSWORD || 'e2e-admin-pass'
const OUT = process.env.OUT || '/tmp/e2e-ops19/evidence'
fs.mkdirSync(OUT, { recursive: true })

const report = { base: BASE, consoleErrors: [], pageErrors: [], apiErrors: [], steps: [] }
const shot = async (page, name) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true })
  report.steps.push({ name, shot: `${OUT}/${name}.png` })
}
const jobStatus = async () => {
  const r = await fetch(`${UPDATER}/upgrade/status`, { headers: { 'X-Updater-Token': TOKEN } })
  return r.json()
}
const readVersionTable = (page) =>
  page.evaluate(() => {
    const out = {}
    for (const tr of document.querySelectorAll('.el-descriptions__body tr')) {
      const cells = [...tr.querySelectorAll('th,td')].map((x) => x.innerText.trim())
      if (cells.length >= 2) out[cells[0]] = cells[1]
    }
    return out
  })
const gotoVersionTab = async (page) => {
  // 注意：设置页里本来就有隐藏的密码输入框（凭据表单），不能用 count() 判断登录态，
  // 必须用可见性。
  const needLogin = page.url().includes('/login') || (await page.locator('input[type=password]:visible').count()) > 0
  if (needLogin) {
    await page.fill('input[type=password] >> nth=0', PASSWORD)
    await page.click('button[type=submit]')
    await page.waitForURL('**/#/overview', { timeout: 30000 })
  }
  await page.click('.ui-nav a:has-text("系统设置")')
  await page.waitForSelector('h1:has-text("系统设置")', { timeout: 20000 })
  await page.click('.ui-tab:has-text("版本与升级")')
  await page.waitForSelector('.el-descriptions', { timeout: 20000 })
}

const browser = await chromium.launch({
  executablePath: '/home/ztb/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome',
  args: ['--no-sandbox'],
})
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('console', (m) => m.type() === 'error' && report.consoleErrors.push(m.text()))
page.on('pageerror', (e) => report.pageErrors.push(String(e)))
page.on('response', (r) => {
  if (r.url().includes('/api/') && r.status() >= 400) report.apiErrors.push({ url: r.url(), status: r.status() })
})

try {
  await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle' })
  await page.waitForSelector('input[type=password]', { timeout: 30000 })
  report.firstRun = (await page.locator('h2:has-text("首次设置")').count()) > 0
  await page.fill('input[type=password] >> nth=0', PASSWORD)
  if (report.firstRun) await page.fill('input[type=password] >> nth=1', PASSWORD)
  await page.click('button[type=submit]')
  await page.waitForURL('**/#/overview', { timeout: 30000 })
  await shot(page, '01-overview')

  await gotoVersionTab(page)
  report.versionBefore = await readVersionTable(page)
  await shot(page, '02-version-before')

  await page.click('button:has-text("检查更新")')
  await page.waitForSelector('text=有新版本', { timeout: 60000 })
  await shot(page, '03-check-update')
  const target = page.locator('button:has-text("升级到")')
  report.target = (await target.innerText()).trim()

  await target.click()
  await page.waitForSelector('.el-message-box', { timeout: 20000 })
  await shot(page, '04-confirm-dialog')
  await page.click('.el-message-box button:has-text("开始升级")')
  report.clicked = true
  await page.waitForTimeout(2000)
  await shot(page, '05-upgrade-started')

  // 观察完成（触发动作已经由真实点击完成，这里只判断何时该刷新页面取结果）
  let status = 'running'
  for (let i = 0; i < 90; i++) {
    await page.waitForTimeout(4000)
    try {
      const d = await jobStatus()
      status = d?.job?.status || 'running'
      if (status !== 'running') break
    } catch {}
  }
  report.finalStatus = status
  report.job = (await jobStatus().catch(() => ({})))?.job || null

  await page.reload({ waitUntil: 'networkidle', timeout: 40000 })
  await gotoVersionTab(page)
  await page.waitForTimeout(1500)
  report.finalText = (await page.locator('body').innerText()).slice(-3000)
  report.versionAfter = await readVersionTable(page)
  await shot(page, '06-upgrade-done')
  report.passed = status === 'success' && /升级成功/.test(report.finalText)
} catch (err) {
  report.error = String(err)
  report.passed = false
  try {
    await shot(page, '99-error')
  } catch {}
} finally {
  await browser.close()
}

fs.writeFileSync(`${OUT}/result.json`, JSON.stringify(report, null, 2))
console.log(
  JSON.stringify(
    {
      passed: report.passed,
      target: report.target,
      finalStatus: report.finalStatus,
      from: report.job?.from_version,
      to: report.job?.version,
      commit_verified: report.job?.commit_verified,
      versionBefore: report.versionBefore,
      versionAfter: report.versionAfter,
      apiErrors: report.apiErrors,
      pageErrors: report.pageErrors,
      error: report.error,
    },
    null,
    2,
  ),
)
process.exit(report.passed ? 0 : 1)
