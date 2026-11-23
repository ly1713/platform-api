// NAS v1.0.13 回归：逐个路由真实加载，确认页面能渲染、关键接口没 5xx、控制台没报错。
import { launch, newPage, goto, evaluate, clickAt, clickSelector, typeText, shot } from './cdp.mjs'
import { writeFile } from 'node:fs/promises'
import { setTimeout as sleep } from 'node:timers/promises'

const BASE = 'http://192.168.2.4:9100'
const PASSWORD = process.env.NAS_PASSWORD
const SHOTS = '/tmp/nas-accept/shots'
const steps = []
const errors = []
const apiFails = []

function log(name, ok, detail) {
  steps.push({ step: name, ok, detail: detail ?? '' })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`)
}

await launch()
const page = await newPage()
page.ws.on('message', (raw) => {
  const m = JSON.parse(raw.toString())
  if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') errors.push(m.params.entry.text)
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails?.text || 'exception')
  if (m.method === 'Network.responseReceived') {
    const r = m.params.response
    if (r.status >= 500 && r.url.includes('/api/')) apiFails.push(`${r.status} ${r.url}`)
  }
})

async function clickText(text) {
  const box = await evaluate(page, `(() => {
    const t = ${JSON.stringify(text)};
    const el = [...document.querySelectorAll('button, a, .ui-btn')].find(n => {
      if (!n.offsetParent && getComputedStyle(n).position !== 'fixed') return false;
      return (n.textContent||'').trim().includes(t);
    });
    if (!el) return null;
    el.scrollIntoView({block:'center'});
    const r = el.getBoundingClientRect();
    return { x: r.x + r.width/2, y: r.y + r.height/2 };
  })()`)
  if (!box) return null
  await clickAt(page, box.x, box.y)
  return box
}

try {
  if (!PASSWORD) throw new Error('缺少 NAS_PASSWORD')
  await goto(page, `${BASE}/#/login`)
  await sleep(2000)
  await clickSelector(page, 'input[type=password]')
  await typeText(page, PASSWORD)
  await clickText('进入工作台')
  await sleep(3500)
  log('登录', !(await evaluate(page, `!!document.querySelector('input[type=password]')`)))

  // 版本守卫：非登录态访问接口必须 401，不能裸奔
  const guard = await evaluate(page, `(async () => {
    // 用真正需要鉴权的接口；/api/system/version 是 public_router（登录页与升级器要读），故意不设防
    const r = await fetch('/api/followed-creators', { credentials: 'omit' });
    return { status: r.status };
  })()`)
  log('未带会话访问接口被拒', guard.status === 401 || guard.status === 403, JSON.stringify(guard))

  const routes = [
    ['overview', '总览'],
    ['accounts', '账号中心'],
    ['crawl', '关键词采集'],
    ['followed-creators', '关注列表'],
    ['assets', '素材库'],
    ['pipeline', '处理队列'],
    ['models', '模型接入'],
    ['system', '系统设置'],
  ]
  for (const [route, expect] of routes) {
    const before = errors.length
    await goto(page, `${BASE}/#/${route}`)
    await sleep(4000)
    const state = await evaluate(page, `(() => {
      const t = document.body.innerText || '';
      const main = document.querySelector('.ui-page, main, .layout-main');
      return {
        hash: location.hash,
        hasExpect: t.includes(${JSON.stringify(expect)}),
        textLen: t.length,
        rendered: main ? main.getBoundingClientRect().height > 0 : false,
        emptyErr: /加载失败|请求失败|无法连接|服务器错误|Internal Server Error/.test(t),
        snippet: t.replace(/\\s+/g,' ').slice(0, 90),
      };
    })()`)
    const newErrs = errors.slice(before)
    log(
      `路由 ${route}`,
      state.hasExpect && state.rendered && !state.emptyErr && newErrs.length === 0,
      JSON.stringify({ hash: state.hash, rendered: state.rendered, emptyErr: state.emptyErr, newErrs: newErrs.slice(0, 2) })
    )
    await shot(page, `${SHOTS}/2${routes.findIndex((r) => r[0] === route)}-route-${route}.png`)
  }

  // 素材库真实数据：确认存量数据在页面上读得到（不是空壳）
  await goto(page, `${BASE}/#/assets`)
  await sleep(5000)
  const assets = await evaluate(page, `(() => {
    const t = document.body.innerText || '';
    const rows = document.querySelectorAll('.ui-card, .asset-card, tbody tr').length;
    const m = t.match(/(\\d[\\d,]*)\\s*(条|个)/);
    return { rows, num: m ? m[0] : null, len: t.length };
  })()`)
  log('素材库读到真实数据', assets.rows > 0 || !!assets.num, JSON.stringify(assets))
  await shot(page, `${SHOTS}/29-assets-data.png`)
} catch (e) {
  log('脚本异常', false, String(e?.stack || e?.message || e))
}

const cleanErrors = errors.filter((e) => !/Manifest|demo\.example\.com|media\/avatars|favicon|ResizeObserver/i.test(e))
log('无 5xx 接口', apiFails.length === 0, apiFails.slice(0, 4).join(' | '))
log('无页面报错', cleanErrors.length === 0, cleanErrors.slice(0, 4).join(' | '))
await writeFile('/tmp/nas-accept/regress-result.json', JSON.stringify({ steps, apiFails, errors: cleanErrors }, null, 2))
process.exit(0)
