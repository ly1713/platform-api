// NAS 上验收本次改动的核心功能：关注列表「归档」→ 归档箱 → 复原。
// 全程真实鼠标/键盘事件，不调 API 抄近路。
import { launch, newPage, goto, evaluate, clickAt, clickSelector, typeText, shot } from './cdp.mjs'
import { writeFile } from 'node:fs/promises'
import { setTimeout as sleep } from 'node:timers/promises'

const BASE = 'http://192.168.2.4:9100'
const PASSWORD = process.env.NAS_PASSWORD
const SHOTS = '/tmp/nas-accept/shots'
const steps = []
const errors = []

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
  if (m.method === 'Network.responseReceived' && m.params.response.status >= 500) {
    errors.push(`HTTP ${m.params.response.status} ${m.params.response.url}`)
  }
})

const DEFAULT_SCOPE = 'button, a, .ui-btn, .ui-tab, .ui-nav-item'

/** 点文案匹配的可见元素（真实鼠标事件）。 */
async function clickText(text, { exact = false, scope = DEFAULT_SCOPE } = {}) {
  const box = await evaluate(page, `(() => {
    const t = ${JSON.stringify(text)};
    const nodes = [...document.querySelectorAll(${JSON.stringify(scope)})];
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

/** 在给定容器选择器里点文案匹配的按钮。 */
async function clickIn(containerSel, text) {
  const box = await evaluate(page, `(() => {
    const c = document.querySelector(${JSON.stringify(containerSel)});
    if (!c) return null;
    const b = [...c.querySelectorAll('button')].find(x => (x.textContent||'').includes(${JSON.stringify(text)}));
    if (!b) return null;
    b.scrollIntoView({block:'center'});
    const r = b.getBoundingClientRect();
    return { x: r.x + r.width/2, y: r.y + r.height/2, text: b.textContent.trim() };
  })()`)
  if (!box) return null
  await clickAt(page, box.x, box.y)
  return box
}

/** 浮层几何：必须 position:fixed 且盒子完整落在视口内。 */
async function modalGeom(sel) {
  return await evaluate(page, `(() => {
    const m = document.querySelector(${JSON.stringify(sel)});
    if (!m) return { present: false };
    const cs = getComputedStyle(m);
    const target = m.querySelector('.ui-modal-box') || m;
    const r = target.getBoundingClientRect();
    return {
      present: true,
      position: cs.position,
      inViewport: r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight,
      rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
      viewport: [innerWidth, innerHeight],
    };
  })()`)
}

try {
  if (!PASSWORD) throw new Error('缺少 NAS_PASSWORD')

  // 1. 登录
  await goto(page, `${BASE}/#/login`)
  await sleep(2000)
  await clickSelector(page, 'input[type=password]')
  await typeText(page, PASSWORD)
  await clickText('进入工作台')
  await sleep(3500)
  log('NAS 登录', !(await evaluate(page, `!!document.querySelector('input[type=password]')`)), await evaluate(page, 'location.hash'))

  // 2. 关注列表
  await goto(page, `${BASE}/#/followed-creators`)
  await sleep(5000)
  const list = await evaluate(page, `(() => {
    const sel = document.querySelector('.sort-select');
    const find = document.querySelector('.find-input');
    const arch = [...document.querySelectorAll('.bar, .filter-row')]
      .flatMap(b => [...b.querySelectorAll('button')])
      .find(x => /归档/.test(x.textContent||''));
    return {
      cards: [...document.querySelectorAll('.ui-card')].length,
      sortText: sel ? sel.innerText.trim() : null,
      hasSearch: !!find,
      searchPlaceholder: find ? find.getAttribute('placeholder') : null,
      archiveEntry: arch ? arch.textContent.trim() : null,
    };
  })()`)
  log('关注列表渲染卡片', list.cards > 0, `${list.cards} 张`)
  log('默认排序=最新关注', list.sortText === '最新关注', list.sortText)
  log('当前列表有搜索框', list.hasSearch, list.searchPlaceholder)
  log('工具栏有「归档」入口', !!list.archiveEntry, list.archiveEntry)
  await shot(page, `${SHOTS}/10-followed-list.png`)

  if (list.cards === 0) throw new Error('没有卡片，无法继续归档验收')

  // 3. 记住第一张卡片的博主名（取链接/首个 span，避开徽章文字）
  const name = await evaluate(page, `(() => {
    const c = document.querySelector('.ui-card');
    const n = c.querySelector('.name');
    if (!n) return '';
    const a = n.querySelector('a');
    if (a) return a.innerText.trim();
    const s = [...n.querySelectorAll('span')].find(x => !x.classList.contains('ui-badge'));
    return s ? s.innerText.trim() : n.innerText.trim();
  })()`)
  log('取到首条博主名', !!name, name)

  // 4. 点卡片上的「归档」
  const archBtn = await clickIn('.ui-card', '归档')
  if (!archBtn) throw new Error('卡片上找不到归档按钮')
  await sleep(1500)

  // 5. 确认弹层必须真的浮在视口里（本次修复的核心）
  const confirmGeom = await modalGeom('.ui-modal')
  log(
    '归档确认弹层 position:fixed 且在视口内（核心修复）',
    confirmGeom.present && confirmGeom.position === 'fixed' && confirmGeom.inViewport,
    JSON.stringify(confirmGeom)
  )
  const confirmTitle = await evaluate(page, `(() => {
    const h = document.querySelector('.ui-modal .ui-panel-head h2');
    const hasCancel = !![...document.querySelectorAll('.ui-modal .ui-modal-foot button')].find(b => /取消/.test(b.textContent||''));
    const hasConfirm = !![...document.querySelectorAll('.ui-modal .ui-modal-foot button')].find(b => /归档/.test(b.textContent||''));
    return { title: h ? h.innerText.trim() : null, hasCancel, hasConfirm, body: (document.querySelector('.ui-modal-body')?.innerText||'').slice(0,120) };
  })()`)
  log('确认弹层写明归档后果', !!confirmTitle.hasConfirm && !!confirmTitle.hasCancel, JSON.stringify(confirmTitle))
  await shot(page, `${SHOTS}/11-archive-confirm.png`)

  // 6. 确认归档
  const confirmed = await clickIn('.ui-modal .ui-modal-foot', '归档')
  if (!confirmed) throw new Error('确认弹层里找不到归档按钮')
  await sleep(4000)

  const afterArchive = await evaluate(page, `(() => {
    const cards = [...document.querySelectorAll('.ui-card')];
    return {
      count: cards.length,
      stillListed: ${JSON.stringify(name)} ? cards.some(c => (c.innerText||'').includes(${JSON.stringify(name)})) : null,
      modalGone: !document.querySelector('.ui-modal'),
    };
  })()`)
  log('归档后当前列表少一条', afterArchive.count === list.cards - 1, `${list.cards} -> ${afterArchive.count}`)
  log('归档的博主不再出现在当前列表', afterArchive.stillListed === false, `stillListed=${afterArchive.stillListed}`)
  log('确认弹层已关闭', afterArchive.modalGone)

  // 7. 打开归档箱
  const opened = await clickText('归档', { exact: false })
  if (!opened) throw new Error('找不到归档入口')
  await sleep(3500)
  const boxGeom = await modalGeom('.ui-modal')
  const archivePanel = await evaluate(page, `(() => {
    const box = document.querySelector('.archive-box');
    if (!box) return { present: false };
    const items = [...box.querySelectorAll('.archive-item')].map(li => ({
      name: li.querySelector('.archive-meta .name')?.innerText.trim() || '',
      stats: li.querySelector('.archive-meta .stats')?.innerText.trim() || '',
      hasAvatar: !!li.querySelector('.ava'),
      restore: [...li.querySelectorAll('button')].find(b => /复原/.test(b.textContent||''))?.textContent.trim() || null,
      hasArchivedAt: /归档|取消关注|恢复/.test(li.querySelector('.archive-meta .stats')?.innerText || ''),
    }));
    return {
      present: true,
      title: box.querySelector('.ui-panel-head h2')?.innerText.trim() || null,
      badge: box.querySelector('.ui-panel-head .ui-badge')?.innerText.trim() || null,
      hasSearch: !!box.querySelector('.archive-search input'),
      items,
    };
  })()`)
  log('归档箱打开且浮在视口内', boxGeom.present && archivePanel.present && boxGeom.inViewport, JSON.stringify(boxGeom))
  log('归档箱有搜索框', archivePanel.hasSearch)
  log(
    '归档箱显示头像+昵称+粉丝+复原',
    archivePanel.items.some(i => i.hasAvatar && i.restore && /粉丝/.test(i.stats)),
    JSON.stringify(archivePanel.items.slice(0, 3))
  )
  log(
    '被归档的博主在归档箱里',
    archivePanel.items.some(i => name && i.name.includes(name)),
    `${name} / 归档共 ${archivePanel.items.length} 条（角标 ${archivePanel.badge}）`
  )
  await shot(page, `${SHOTS}/12-archive-box.png`)

  // 8. 在归档箱里搜索（真实键盘输入）
  const kw = (name || '').slice(0, 2)
  if (kw) {
    await clickSelector(page, '.archive-search input')
    await typeText(page, kw)
    await sleep(1800)
    const filtered = await evaluate(page, `(() => {
      const box = document.querySelector('.archive-box');
      return {
        items: box ? [...box.querySelectorAll('.archive-item')].map(li => li.querySelector('.archive-meta .name')?.innerText.trim() || '') : [],
        empty: box ? (box.querySelector('.archive-empty')?.innerText.trim() || '') : '',
      };
    })()`)
    log('归档箱搜索能筛', filtered.items.length >= 1 && filtered.items.every(n => n.includes(kw)), `${kw} -> ${filtered.items.length} 条 ${JSON.stringify(filtered.items.slice(0,3))}`)
    await shot(page, `${SHOTS}/13-archive-search.png`)
    // 清空搜索
    await clickSelector(page, '.archive-search .find-clear')
    await sleep(1200)
  }

  // 9. 复原
  const beforeRestore = await evaluate(page, `document.querySelectorAll('.archive-item').length`)
  const restored = await clickIn('.archive-item', '复原')
  if (!restored) throw new Error('归档箱里找不到复原按钮')
  await sleep(4500)
  const afterRestore = await evaluate(page, `(() => {
    const box = document.querySelector('.archive-box');
    return {
      items: box ? box.querySelectorAll('.archive-item').length : 0,
      empty: box ? (document.querySelector('.archive-empty')?.innerText.trim() || '') : '',
    };
  })()`)
  log('复原后离开归档箱', afterRestore.items < beforeRestore, `${beforeRestore} -> ${afterRestore.items}`)

  // 10. 关掉归档箱，回到关注列表
  const closed = await clickIn('.archive-box .ui-panel-head', '关闭')
  if (!closed) await clickText('关闭', { exact: true })
  await sleep(3000)
  const back = await evaluate(page, `(() => {
    const cards = [...document.querySelectorAll('.ui-card')];
    return {
      count: cards.length,
      listed: cards.some(c => (c.innerText||'').includes(${JSON.stringify(name)})),
      boxGone: !document.querySelector('.archive-box'),
    };
  })()`)
  log('复原的博主回到当前列表', back.count === list.cards && back.listed === true, `${afterArchive.count} -> ${back.count}, listed=${back.listed}`)
  log('归档箱已关闭', back.boxGone)
  await shot(page, `${SHOTS}/14-restored.png`)

  // 11. 当前列表搜索框真实输入
  await clickSelector(page, '.filter-row .find-input')
  const kw2 = (name || '').slice(0, 2)
  await typeText(page, kw2)
  await sleep(1800)
  const searched = await evaluate(page, `(() => {
    const cards = [...document.querySelectorAll('.ui-card')];
    return { count: cards.length, all: cards.every(c => (c.innerText||'').includes(${JSON.stringify(kw2)})) };
  })()`)
  log('当前列表搜索能筛', searched.count >= 1 && searched.count <= back.count && searched.all, `"${kw2}" -> ${searched.count}/${back.count}`)
  await shot(page, `${SHOTS}/15-search.png`)
} catch (e) {
  log('脚本异常', false, String(e?.stack || e?.message || e))
}

await shot(page, `${SHOTS}/19-final.png`)
const cleanErrors = errors.filter((e) => !/Manifest|demo\.example\.com|media\/avatars|favicon|ERR_CONNECTION_REFUSED/i.test(e))
log('无页面报错', cleanErrors.length === 0, cleanErrors.slice(0, 3).join(' | '))
await writeFile('/tmp/nas-accept/feature-result.json', JSON.stringify({ steps, errors: cleanErrors }, null, 2))
process.exit(0)
