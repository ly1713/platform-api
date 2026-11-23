// 极简 CDP 客户端：真 Chromium、真 Input 事件，不依赖 playwright 包。
import { WebSocket } from 'ws'
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'

const CHROME = '/home/ztb/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome'
const PORT = 9333
const BASE = 'http://127.0.0.1:8099'

export async function launch() {
  const proc = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`,
    '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--window-size=1440,1000', '--hide-scrollbars',
    'about:blank',
  ], { stdio: 'ignore', detached: true })
  proc.unref()
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`)
      if (r.ok) break
    } catch {}
    await sleep(250)
  }
  return proc
}

export async function newPage() {
  const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' })
  const target = await r.json()
  const ws = new WebSocket(target.webSocketDebuggerUrl, { maxPayload: 256 * 1024 * 1024 })
  await new Promise((res, rej) => { ws.once('open', res); ws.once('error', rej) })
  let id = 0
  const pending = new Map()
  const events = []
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw.toString())
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id)
      pending.delete(msg.id)
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result)
    } else if (msg.method) events.push(msg)
  })
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id
    pending.set(mid, { resolve, reject })
    ws.send(JSON.stringify({ id: mid, method, params }))
  })
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Log.enable')
  await send('Network.enable')
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  })
  return { ws, send, events, close: () => ws.close() }
}

export async function goto(page, url) {
  await page.send('Page.navigate', { url })
  // 等 load + 微任务稳定，避免在 Vue 还没挂载时点空
  for (let i = 0; i < 80; i++) {
    await sleep(150)
    const { result } = await page.send('Runtime.evaluate', {
      expression: 'document.readyState', returnByValue: true,
    })
    if (result.value === 'complete') break
  }
  await sleep(600)
}

export async function evaluate(page, expression) {
  const { result, exceptionDetails } = await page.send('Runtime.evaluate', {
    expression, returnByValue: true, awaitPromise: true,
  })
  if (exceptionDetails) throw new Error(JSON.stringify(exceptionDetails))
  return result.value
}

/** 真实的鼠标按下/抬起（Input 域），不是 el.click()。 */
export async function clickAt(page, x, y) {
  await page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, buttons: 0 })
  await page.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 })
  await page.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 })
  await sleep(350)
}

export async function clickSelector(page, selector) {
  const box = await evaluate(page, `(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return { invisible: true, r: [r.x, r.y, r.width, r.height] };
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  })()`)
  if (!box) throw new Error(`找不到元素：${selector}`)
  if (box.invisible) throw new Error(`元素尺寸为 0：${selector} ${JSON.stringify(box.r)}`)
  await clickAt(page, box.x, box.y)
}

export async function typeText(page, text) {
  for (const ch of text) {
    await page.send('Input.dispatchKeyEvent', { type: 'keyDown', text: ch })
    await page.send('Input.dispatchKeyEvent', { type: 'keyUp', text: ch })
  }
  await sleep(350)
}

export async function shot(page, path) {
  const { data } = await page.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  const { writeFile } = await import('node:fs/promises')
  await writeFile(path, Buffer.from(data, 'base64'))
  return path
}
