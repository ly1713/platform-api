// 真实浏览器驱动一键升级（不使用任何 API 直调）：
// 设置管理员密码 → 进入工作台 → 系统设置 → 版本与升级 → 检查更新 → 升级到 vX → 确认。
// 截图 + 关键文本落盘到 OUT，作为证据。
const { chromium } = await import('/home/ztb/.local/lib/node_modules/@playwright/mcp/node_modules/playwright/index.mjs')
const fs = await import('node:fs')

const BASE = process.env.BASE || 'http://127.0.0.1:9399'
const PASSWORD = process.env.PW_PASSWORD || 'e2e-admin-pass'
const OUT = process.env.OUT || '/tmp/e2e-ops19/evidence'
fs.mkdirSync(OUT, { recursive: true })

const report = { base: BASE, consoleErrors: [], pageErrors: [], apiErrors: [], steps: [] }
const shot = async (page, name) => {
  const path = `${OUT}/${name}.png`
  await page.screenshot({ path, fullPage: true })
  report.steps.push({ name, shot: path })
}

const browser = await chromium.launch({
  executablePath: '/home/ztb/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome',
  args: ['--no-sandbox'],
})
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('console', (m) => m.type() === 'error' && report.consoleErrors.push(m.text()))
page.on('pageerror', (e) => report.pageErrors.push(String(e)))
page.on('response', (r) => {
  if (r.url().includes('/api/') && r.status() >= 400) {
    report.apiErrors.push({ url: r.url(), status: r.status() })
  }
})

try {
  // 1) 首次设置密码
  await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle' })
  await page.waitForSelector('input[type=password]', { timeout: 30000 })
  const firstRun = (await page.locator('h2:has-text("首次设置")').count()) > 0
  report.firstRun = firstRun
  await page.fill('input[type=password] >> nth=0', PASSWORD)
  if (firstRun) await page.fill('input[type=password] >> nth=1', PASSWORD)
  await page.click('button[type=submit]')
  await page.waitForURL('**/#/overview', { timeout: 30000 })
  await shot(page, '01-overview')

  // 2) 系统设置 → 版本与升级
  await page.click('.ui-nav a:has-text("系统设置")')
  await page.waitForSelector('h1:has-text("系统设置")', { timeout: 20000 })
  await page.click('.ui-tab:has-text("版本与升级")')
  await page.waitForSelector('.el-descriptions', { timeout: 20000 })
  report.versionBefore = await page.evaluate(() => {
    const out = {}
    for (const tr of document.querySelectorAll('.el-descriptions__body tr')) {
      const cells = [...tr.querySelectorAll('th,td')].map((x) => x.innerText.trim())
      if (cells.length >= 2) out[cells[0]] = cells[1]
    }
    return out
  })
  await shot(page, '02-version-before')

  // 3) 检查更新
  await page.click('button:has-text("检查更新")')
  await page.waitForSelector('text=有新版本', { timeout: 60000 })
  await shot(page, '03-check-update')
  const target = page.locator('button:has-text("升级到")')
  report.target = (await target.innerText()).trim()

  // 4) 真实点击升级 + 确认
  await target.click()
  await page.waitForSelector('.el-message-box', { timeout: 20000 })
  await shot(page, '04-confirm-dialog')
  await page.click('.el-message-box button:has-text("开始升级")')
  await page.waitForTimeout(1500)
  await shot(page, '05-upgrade-started')

  // 5) 轮询页面（升级期间 API 会短暂不可达）
  let finalText = ''
  for (let i = 0; i < 120; i++) {
    await page.waitForTimeout(5000)
    try {
      await page.reload({ waitUntil: 'networkidle', timeout: 30000 })
      if (page.url().includes('/login') || (await page.locator('input[type=password]').count())) {
        await page.fill('input[type=password] >> nth=0', PASSWORD)
        await page.click('button[type=submit]')
        await page.waitForURL('**/#/overview', { timeout: 20000 })
        await page.click('.ui-nav a:has-text("系统设置")')
        await page.click('.ui-tab:has-text("版本与升级")')
      }
      finalText = await page.locator('body').innerText()
      if (/升级成功|升级完成|升级失败|已自动回滚/.test(finalText)) break
    } catch (e) {
      report.steps.push({ poll: i, error: String(e).slice(0, 200) })
    }
  }
  report.finalText = finalText.slice(-3000)
  await shot(page, '06-upgrade-done')
  report.versionAfter = await page.evaluate(() => {
    const out = {}
    for (const tr of document.querySelectorAll('.el-descriptions__body tr')) {
      const cells = [...tr.querySelectorAll('th,td')].map((x) => x.innerText.trim())
      if (cells.length >= 2) out[cells[0]] = cells[1]
    }
    return out
  })
  report.passed = /升级成功|升级完成/.test(finalText)
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
console.log(JSON.stringify({ passed: report.passed, target: report.target, versionBefore: report.versionBefore, versionAfter: report.versionAfter, apiErrors: report.apiErrors, pageErrors: report.pageErrors, finalText: (report.finalText || '').slice(-600) }, null, 2))
process.exit(report.passed ? 0 : 1)
