// NAS 前端按钮升级：完全按用户路径走，不直接调升级 API。
// 登录 → 系统设置 → 版本与升级 → 检查更新 → 升级到 v1.0.13 → 开始升级。
import { launch, newPage, goto, evaluate, clickAt, clickSelector, typeText, shot } from './cdp.mjs'
import { writeFile } from 'node:fs/promises'
import { setTimeout as sleep } from 'node:timers/promises'

const BASE = 'http://192.168.2.4:9100'
const PASSWORD = process.env.NAS_PASSWORD
const TARGET = process.env.TARGET_VERSION || 'v1.0.13'
const SHOTS = '/tmp/nas-accept/shots'
const steps = []
const errors = []

function log(name, ok, detail) {
  steps.push({ step: name, ok, detail: detail ?? '' })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`)
}

if (!PASSWORD) {
  console.error('缺少 NAS_PASSWORD')
  process.exit(2)
}

await launch()
const page = await newPage()
page.ws.on('message', (raw) => {
  const m = JSON.parse(raw.toString())
  if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') errors.push(m.params.entry.text)
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails?.text || 'exception')
  if (m.method === 'Network.responseReceived' && m.params.response.status >= 500) {
    errors.push(`HTTP ${m.params.response.status} ${m.params.response.url}`)
  }
})

/** 点击文案匹配的可见按钮/链接，用真实鼠标事件。 */
async function clickText(text, { exact = false } = {}) {
  const box = await evaluate(page, `(() => {
    const t = ${JSON.stringify(text)};
    const nodes = [...document.querySelectorAll('button, a, .ui-btn, .ui-tab, .ui-nav-item')];
    const el = nodes.find(n => {
      if (!n.offsetParent && getComputedStyle(n).position !== 'fixed') return false;
      const s = (n.textContent || '').trim();
      return ${exact ? 's === t' : 's.includes(t)'};
    });
    if (!el) return null;
    el.scrollIntoView({block:'center'});
    const r = el.getBoundingClientRect();
    return { x: r.x + r.width/2, y: r.y + r.height/2, text: (el.textContent||'').trim().slice(0,60) };
  })()`)
  if (!box) return null
  await clickAt(page, box.x, box.y)
  return box
}

async function readVersion() {
  return await evaluate(page, `(() => {
    const t = document.body.innerText;
    return {
      version: (t.match(/v\\\\d+\\\\.\\\\d+\\\\.\\\\d+/g)||[])[0] || null,
      commitVerified: t.includes('提交已核验'),
      onlyVersion: t.includes('仅核验版本'),
      hasBackup: t.includes('备份'),
      snippet: (t.match(/[^\\\\n]*(版本|提交|备份|核验)[^\\\\n]*/g)||[]).slice(0,8),
    };
  })()`)
}

try {
  // 1. 登录
  await goto(page, `${BASE}/#/login`)
  await sleep(2000)
  await clickSelector(page, 'input[type=password]')
  await typeText(page, PASSWORD)
  const ok = await clickText('进入工作台')
  await sleep(3000)
  const loggedIn = !(await evaluate(page, `!!document.querySelector('input[type=password]')`))
  log('登录进入工作台', !!ok && loggedIn, await evaluate(page, 'location.hash'))
  await shot(page, `${SHOTS}/01-login.png`)

  // 2. 系统设置
  const settings = await clickText('系统设置')
  await sleep(2000)
  log('打开系统设置', !!settings, settings?.text ?? '')

  // 3. 版本与升级
  const tab = await clickText('版本与升级')
  await sleep(2500)
  const before = await readVersion()
  log('打开版本与升级', !!tab, JSON.stringify(before.snippet))
  await shot(page, `${SHOTS}/02-version-before.png`)

  // 4. 检查更新
  const check = await clickText('检查更新')
  await sleep(6000)
  const afterCheck = await evaluate(page, `(() => {
    const text = document.body.innerText;
    return {
      hasTarget: text.includes(${JSON.stringify(TARGET)}),
      hasUpgradeBtn: !!([...document.querySelectorAll('button')].find(b => /升级|更新到/.test(b.textContent||''))),
      snippet: (text.match(/[^\\\\n]*(发现|最新|可用|新版本)[^\\\\n]*/g)||[]).slice(0,4),
    };
  })()`)
  log('检查更新拿到新版本', afterCheck.hasTarget, JSON.stringify(afterCheck.snippet))
  await shot(page, `${SHOTS}/03-check-update.png`)

  // 5. 点「升级到 vX」
  const upBtn = await clickText(`升级到 ${TARGET}`)
  await sleep(1500)
  log('点击升级到目标版本', !!upBtn, upBtn?.text ?? '未找到升级按钮')
  await shot(page, `${SHOTS}/04-upgrade-confirm.png`)

  // 6. 确认框「开始升级」
  const start = await clickText('开始升级')
  await sleep(2000)
  log('确认框点开始升级', !!start, start?.text ?? '')
  await shot(page, `${SHOTS}/05-upgrade-progress.png`)

  // 7. 等升级完成（最多 20 分钟）
  let done = false
  let finalSnippet = ''
  for (let i = 0; i < 120; i++) {
    await sleep(10000)
    const st = await evaluate(page, `(() => {
      const t = document.body.innerText;
      return {
        done: /升级完成|升级成功/.test(t),
        failed: /升级失败|回滚失败/.test(t),
        ops: (t.match(/[^\\\\n]*(升级|拉取|重建|完成|失败)[^\\\\n]*/g)||[]).slice(0,6),
      };
    })()`)
    finalSnippet = JSON.stringify(st.ops)
    if (st.done || st.failed) {
      done = st.done && !st.failed
      log(st.done ? '页面报告升级完成' : '页面报告升级失败', st.done, finalSnippet)
      break
    }
  }
  if (!done) log('等待升级完成（超时）', false, finalSnippet)
  await shot(page, `${SHOTS}/06-upgrade-done.png`)

  // 8. 终态核对
  await sleep(5000)
  const final = await readVersion()
  log('终态显示新版本', final.version === TARGET, JSON.stringify(final))
} catch (e) {
  log('脚本异常', false, String(e?.message || e))
}

await shot(page, `${SHOTS}/99-final.png`)
const cleanErrors = errors.filter(
  (e) => !/Manifest|demo\.example\.com|media\/avatars|favicon/i.test(e)
)
log('无页面报错', cleanErrors.length === 0, cleanErrors.slice(0, 4).join(' | '))
await writeFile(
  '/tmp/nas-accept/upgrade-result.json',
  JSON.stringify({ steps, errors: cleanErrors }, null, 2)
)
process.exit(0)
