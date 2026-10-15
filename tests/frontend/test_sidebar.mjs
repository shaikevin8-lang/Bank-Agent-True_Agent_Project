/**
 * 前端「会话侧边栏」的 DOM 集成测试。
 *
 * 用 jsdom 真实加载 web_test/index.html 并执行其中的内联脚本，再用打桩的 fetch
 * 模拟后端（严格按 /api/sessions 的响应格式），驱动侧边栏的交互，验证：
 * 页面结构、空态、历史会话加载与恢复、新建会话、点击切换、发送消息后自动标题、
 * 重命名、删除（含二次确认与删除当前会话后的去向）、侧边栏折叠与记忆、
 * 以及后端不可用时的降级行为。
 *
 * 运行方式（Node 18+）：
 *     cd tests/frontend && npm install jsdom && node test_sidebar.mjs
 *
 * 依赖 jsdom 仅为测试用，不进入产品运行时；index.html 本身零外部依赖。
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { JSDOM } from 'jsdom'

const here = dirname(fileURLToPath(import.meta.url))
const htmlPath = resolve(here, '../../Struct_of_Agent/web_test/index.html')

let failures = 0
let checks = 0

function check(name, condition, extra) {
  checks += 1
  if (condition) {
    console.log(`  ok   ${name}`)
  } else {
    failures += 1
    console.log(`  FAIL ${name}${extra === undefined ? '' : ' -> ' + JSON.stringify(extra)}`)
  }
}

function section(title) {
  console.log(`\n== ${title} ==`)
}

const settle = (ms = 40) => new Promise((r) => setTimeout(r, ms))

// ---------------------------------------------------------------- 打桩后端
//
// 语义与 Struct_of_Agent/web_test/session_store.py 对齐：
//   - 列表按最近活跃倒序；
//   - 新建会话时清理还没有说过话的空会话；
//   - 首条用户消息成为标题，用户手动改过名后不再被覆盖；
//   - 删除会话会一并删除其消息。

const backend = { sessions: [], messages: {}, seq: 0 }
const calls = []

const LLM_SNAPSHOT = {
  configured: false, mode: 'mock', source: 'none', base_url: '', model: '',
  response_format: 'text', temperature: 0.0, max_tokens: 1024, timeout: 0.0,
  api_key_configured: false, api_key_masked: '', env_api_key_present: false,
  updated_at: null, active_model_class: 'MockChat',
}

const SEARCH_SNAPSHOT = {
  configured: false, mode: 'unconfigured', source: 'none', base_url: '',
  max_results: 5, timeout: 10.0, api_key_configured: false, api_key_masked: '',
  env_api_key_present: false, updated_at: null,
}

const HEALTH = {
  status: 'ok', model_mode: 'mock', model: '', base_url: '', search_configured: false,
  api_version: 'test',
}

function resetBackend() {
  backend.sessions = []
  backend.messages = {}
  backend.seq = 0
  calls.length = 0
}

function nowIso() {
  return new Date().toISOString().slice(0, 19)
}

function findSession(id) {
  return backend.sessions.filter((s) => s.id === id)[0] || null
}

/** 直接种一个已有会话（模拟服务端重启后仍在的历史数据）。 */
function seedSession(title, turns) {
  backend.seq += 1
  const session = {
    id: 'web-t' + backend.seq,
    title: title,
    created_at: nowIso(),
    updated_at: nowIso(),
    message_count: 0,
    preview: '',
    title_set_by_user: false,
  }
  backend.sessions.unshift(session)
  backend.messages[session.id] = []
  ;(turns || []).forEach((turn) => appendMessage(session.id, turn.role, turn.content, turn))
  // 种子的标题以传入值为准：真实后端里「首条消息定标题」只作用于会话的第一条
  // 用户消息，而这里模拟的是「已经命名好的历史会话」。
  session.title = title
  return session
}

function appendMessage(id, role, content, meta) {
  let session = findSession(id)
  if (!session) {
    backend.seq += 1
    session = {
      id, title: '新会话', created_at: nowIso(), updated_at: nowIso(),
      message_count: 0, preview: '', title_set_by_user: false,
    }
    backend.sessions.unshift(session)
    backend.messages[id] = []
  }
  const list = backend.messages[id]
  const firstUser = role === 'user' && !list.some((m) => m.role === 'user')
  list.push({
    role,
    content,
    created_at: nowIso(),
    agent: (meta && meta.agent) || null,
    model_mode: (meta && meta.model_mode) || null,
    model_name: (meta && meta.model_name) || null,
    tools: (meta && meta.tools) || [],
  })
  session.message_count = list.length
  session.preview = content
  session.updated_at = nowIso()
  if (firstUser && !session.title_set_by_user) {
    session.title = content.length > 24 ? content.slice(0, 24) + '…' : content
  }
  // 最近活跃的排到最前（与后端 order_by updated_at desc 等价）
  backend.sessions.splice(backend.sessions.indexOf(session), 1)
  backend.sessions.unshift(session)
}

function createSession(title) {
  // 清理遗留的空会话占位
  for (let i = backend.sessions.length - 1; i >= 0; i -= 1) {
    if (backend.sessions[i].message_count === 0) {
      delete backend.messages[backend.sessions[i].id]
      backend.sessions.splice(i, 1)
    }
  }
  backend.seq += 1
  const session = {
    id: 'web-t' + backend.seq,
    title: title || '新会话',
    created_at: nowIso(), updated_at: nowIso(),
    message_count: 0, preview: '', title_set_by_user: !!title,
  }
  backend.sessions.unshift(session)
  backend.messages[session.id] = []
  return session
}

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

const ok = (message, data) => jsonResponse({ ok: true, message: message || '', error_code: null, data })
const fail = (message, errorCode, status) =>
  jsonResponse({ ok: false, message, error_code: errorCode, data: null }, status)

const CHAT_TRACE = {
  session_id: 's', steps: 1, tool_names: ['transfer_by_name'],
  model_mode: 'model', model_name: 'deepseek-flash', agent: 'R1',
}

async function stubFetch(url, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const body = options.body ? JSON.parse(options.body) : undefined
  calls.push({ url, method, body })

  if (url === '/health/live') return jsonResponse(HEALTH)
  if (url === '/api/settings/llm') return ok('', LLM_SNAPSHOT)
  if (url === '/api/settings/search') return ok('', SEARCH_SNAPSHOT)

  if (url === '/api/sessions' && method === 'GET') {
    return ok('', { sessions: backend.sessions.map((s) => ({ ...s })) })
  }
  if (url === '/api/sessions' && method === 'POST') {
    return ok('已新建会话', { session: createSession(body && body.title) })
  }
  if (url.indexOf('/api/sessions/') === 0) {
    const id = decodeURIComponent(url.slice('/api/sessions/'.length))
    const session = findSession(id)
    if (method === 'DELETE') {
      if (!session) return fail('会话不存在或已被删除', 'SESSION_NOT_FOUND', 404)
      backend.sessions = backend.sessions.filter((s) => s.id !== id)
      delete backend.messages[id]
      return ok('会话已删除', { deleted: id })
    }
    if (!session) return fail('会话不存在或已被删除', 'SESSION_NOT_FOUND', 404)
    if (method === 'PATCH') {
      session.title = body.title
      session.title_set_by_user = true
      return ok('已重命名', { session: { ...session } })
    }
    return ok('', { session: { ...session }, messages: (backend.messages[id] || []).slice() })
  }
  if (url === '/api/chat') {
    appendMessage(body.session_id, 'user', body.message)
    appendMessage(body.session_id, 'assistant', '好的，已为您处理。', { agent: 'R1', model_mode: 'model', model_name: 'deepseek-flash', tools: ['transfer_by_name'] })
    return jsonResponse({
      reply: '好的，已为您处理。', blocked: false, moderation: {}, tasks: [], agent: 'R1', trace: CHAT_TRACE,
    })
  }
  throw new Error('未打桩的请求: ' + method + ' ' + url)
}

// ---------------------------------------------------------------- 页面装载

const html = readFileSync(htmlPath, 'utf8')

/** 装载一份新页面；fetchImpl 缺省用上面那份完整打桩。 */
async function loadPage(fetchImpl, beforeParse) {
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://127.0.0.1:8000/',
    beforeParse(window) {
      window.fetch = fetchImpl || stubFetch
      window.confirm = () => true
      if (beforeParse) beforeParse(window)
    },
  })
  await settle(60)
  return dom
}

const $ = (w, id) => w.document.getElementById(id)
const items = (w) => [...w.document.querySelectorAll('.session-item')]
const titles = (w) => items(w).map((el) => el.querySelector('.session-title').textContent)
const activeId = (w) => {
  const el = w.document.querySelector('.session-item.active')
  return el ? el.dataset.id : null
}
const click = (w, el) => el.dispatchEvent(new w.Event('click', { bubbles: true }))
const dblclick = (w, el) => el.dispatchEvent(new w.Event('dblclick', { bubbles: true }))
const key = (w, el, k) => el.dispatchEvent(new w.KeyboardEvent('keydown', { key: k, bubbles: true }))
const chatText = (w) => $ (w, 'chat').textContent

// ---------------------------------------------------------------- 1. 结构

section('页面结构：左侧侧边栏 + 右侧对话区')
{
  resetBackend()
  const dom = await loadPage()
  const w = dom.window
  const d = w.document

  check('存在侧边栏容器', !!d.querySelector('aside.sidebar'))
  check('存在两栏布局容器 .app', !!d.querySelector('.app'))
  check('对话区被包在 .chat-column 内', !!d.querySelector('.chat-column > main#scroll'))
  check('侧边栏在对话区之前（左侧）',
    d.querySelector('.app').firstElementChild.classList.contains('sidebar'))
  check('存在「新建会话」按钮', !!$(w, 'newSessionBtn'))
  check('新建会话按钮文案正确', $(w, 'newSessionBtn').textContent.trim() === '新建会话',
    $(w, 'newSessionBtn').textContent)
  check('存在会话列表容器', !!$(w, 'sessionList'))
  check('存在侧边栏开合按钮', !!$(w, 'sidebarToggle'))
  check('存在移动端遮罩', !!$(w, 'sidebarBackdrop'))
  check('原有顶栏设置按钮仍在', !!$(w, 'settingsBtn'))
  check('原有输入框仍在', !!$(w, 'input'))

  const css = d.querySelector('style').textContent.replace(/\/\*[\s\S]*?\*\//g, '')
  // 折叠类挂在 <html> 上：<head> 的首帧脚本必须在样式生效前就能定好状态
  check('样式表定义了折叠态（负外边距把侧边栏推出视野）',
    /html\.sidebar-collapsed\s+\.sidebar\s*\{[^}]*margin-left:\s*-264px/.test(css))
  check('折叠类由 <head> 的首帧脚本设置（避免窄屏闪出抽屉）',
    /localStorage\.getItem\('bank_agent\.sidebar'\)[\s\S]{0,400}sidebar-collapsed/.test(
      d.querySelector('head script').textContent))
  check('样式表定义了移动端抽屉媒体查询', /@media\s*\(max-width:\s*860px\)/.test(css))
  check('样式表用 100dvh 适配移动端可视视口', /height:\s*100dvh/.test(css))
  check('隐藏的图标按钮不接收点击（opacity:0 挡不住指针）',
    /\.session-actions\s*\{[^}]*pointer-events:\s*none/.test(css))
  check('存在会话操作提示位（失败不写进对话记录）', !!$(w, 'sidebarAlert'))
  check('会话条目可聚焦（键盘用户能切换会话）',
    items(w).length === 0 || items(w)[0].getAttribute('tabindex') === '0')
  check('开合按钮带 aria-expanded', $(w, 'sidebarToggle').hasAttribute('aria-expanded'))
}

// ---------------------------------------------------------------- 2. 空态

section('空态：没有任何历史会话')
{
  resetBackend()
  const dom = await loadPage()
  const w = dom.window

  check('页面加载时拉取过会话列表',
    calls.some((c) => c.url === '/api/sessions' && c.method === 'GET'))
  check('列表为空时显示空态', !!$(w, 'sessionEmpty'))
  check('空态文案提示可以新建会话', /新建会话/.test($(w, 'sessionEmpty').textContent),
    $(w, 'sessionEmpty').textContent)
  check('空态下不产生会话条目', items(w).length === 0)
  check('对话区显示欢迎语', /我是银行智能助手/.test(chatText(w)), chatText(w).slice(0, 60))
  check('空态下没有当前会话（不会凭空建会话）',
    !calls.some((c) => c.url === '/api/sessions' && c.method === 'POST'))
}

// ---------------------------------------------------------------- 3. 历史加载

section('加载历史：恢复上次打开的会话并渲染其消息')
{
  resetBackend()
  const older = seedSession('旧会话', [
    { role: 'user', content: '旧会话的问题' },
    { role: 'assistant', content: '旧会话的回答', agent: 'R2' },
  ])
  const newer = seedSession('新会话', [
    { role: 'user', content: '新会话的问题' },
    { role: 'assistant', content: '新会话的回答', agent: 'R1' },
  ])

  const dom = await loadPage()
  const w = dom.window

  check('侧边栏列出全部历史会话', items(w).length === 2, titles(w))
  check('列表顺序为最近活跃在前', titles(w).join('|') === '新会话|旧会话', titles(w))
  check('默认激活最近活跃的会话', activeId(w) === newer.id, activeId(w))
  check('对话区渲染了该会话的历史消息', /新会话的问题/.test(chatText(w)) && /新会话的回答/.test(chatText(w)),
    chatText(w))
  check('历史消息不串到别的会话', !/旧会话的问题/.test(chatText(w)))
  // 精确断言：Agent 标签必须出现在助手消息的元信息里。
  // 注意 .meta 是消息气泡的**兄弟节点**（addMessage 里 append 到 #chat），不是子节点。
  const metas = [...$(w, 'chat').querySelectorAll('.meta')]
  const tag = metas.map((el) => el.querySelector('.tag')).filter(Boolean)[0]
  check('历史助手消息保留了 Agent 标签',
    !!tag && /智能转账|账单分析/.test(tag.textContent), tag && tag.textContent)
  check('Agent 标签紧跟助手气泡（不是挂在用户消息后面）',
    !!tag && !!tag.closest('.meta').previousElementSibling
      && tag.closest('.meta').previousElementSibling.classList.contains('assistant'),
    tag && tag.closest('.meta').previousElementSibling
      && tag.closest('.meta').previousElementSibling.className)
  check('当前会话 ID 写入 localStorage',
    w.localStorage.getItem('bank_agent.active_session') === newer.id,
    w.localStorage.getItem('bank_agent.active_session'))
  check('条目上显示了消息条数', /2 条消息/.test(items(w)[0].textContent), items(w)[0].textContent)

  // 重新装载页面（模拟刷新）：应恢复到上次的会话
  const dom2 = await loadPage(stubFetch, (window) => {
    window.localStorage.setItem('bank_agent.active_session', older.id)
  })
  const w2 = dom2.window
  check('刷新后恢复 localStorage 里记住的会话', activeId(w2) === older.id, activeId(w2))
  check('刷新后渲染的是该会话的消息', /旧会话的问题/.test(chatText(w2)), chatText(w2))
  check('刷新后不残留其它会话的内容', !/新会话的问题/.test(chatText(w2)))
}

// ---------------------------------------------------------------- 4. 新建会话

section('新建会话')
{
  resetBackend()
  seedSession('已有的会话', [{ role: 'user', content: '已有内容' }])
  const dom = await loadPage()
  const w = dom.window

  const before = items(w).length
  click(w, $(w, 'newSessionBtn'))
  await settle()

  check('点击后以 POST 调用 /api/sessions',
    calls.some((c) => c.url === '/api/sessions' && c.method === 'POST'))
  check('列表新增一个会话', items(w).length === before + 1, titles(w))
  check('新会话排在列表最前', titles(w)[0] === '新会话', titles(w))
  check('新会话被激活', activeId(w) === items(w)[0].dataset.id, activeId(w))
  check('对话区清空为欢迎语', /我是银行智能助手/.test(chatText(w)) && !/已有内容/.test(chatText(w)),
    chatText(w).slice(0, 80))
  check('新会话 ID 写入 localStorage',
    w.localStorage.getItem('bank_agent.active_session') === activeId(w))
}

// ---------------------------------------------------------------- 5. 发消息

section('发送消息：复用当前会话并自动生成标题')
{
  resetBackend()
  seedSession('待续聊的会话', [{ role: 'user', content: '之前说过的话' }])
  const dom = await loadPage()
  const w = dom.window
  const sessionId = activeId(w)

  $(w, 'input').value = '给张三转账100元'
  click(w, $(w, 'send'))
  await settle(80)

  const chatCall = calls.filter((c) => c.url === '/api/chat').pop()
  check('发出了对话请求', !!chatCall)
  check('对话请求复用了当前会话 ID', chatCall && chatCall.body.session_id === sessionId,
    chatCall && chatCall.body)
  check('对话区出现用户气泡', /给张三转账100元/.test(chatText(w)), chatText(w))
  check('对话区出现助手回复', /好的，已为您处理/.test(chatText(w)), chatText(w))
  check('已说过话的会话标题不被改写', titles(w).includes('待续聊的会话'), titles(w))
  check('会话消息条数刷新', /3 条消息/.test(items(w)[0].textContent), items(w)[0].textContent)
}

section('发送消息：新会话的标题取首条用户消息')
{
  resetBackend()
  const dom = await loadPage()
  const w = dom.window

  check('初始没有会话', items(w).length === 0)
  $(w, 'input').value = '看看我这个月的消费分类'
  click(w, $(w, 'send'))
  await settle(80)

  check('发送前自动新建了会话',
    calls.some((c) => c.url === '/api/sessions' && c.method === 'POST'))
  check('新会话标题取自首条消息', titles(w)[0] === '看看我这个月的消费分类', titles(w))
  check('对话请求带上了新建的会话 ID',
    calls.filter((c) => c.url === '/api/chat').pop().body.session_id === items(w)[0].dataset.id)
  check('对话区显示该消息', /看看我这个月的消费分类/.test(chatText(w)))
}

// ---------------------------------------------------------------- 6. 切换会话

section('点击历史会话切换')
{
  resetBackend()
  const first = seedSession('会话甲', [
    { role: 'user', content: '甲的问题' },
    { role: 'assistant', content: '甲的回答' },
  ])
  const second = seedSession('会话乙', [
    { role: 'user', content: '乙的问题' },
    { role: 'assistant', content: '乙的回答' },
  ])
  const dom = await loadPage()
  const w = dom.window

  check('初始激活最近活跃的「会话乙」', activeId(w) === second.id, activeId(w))
  const target = items(w).filter((el) => el.dataset.id === first.id)[0]
  click(w, target)
  await settle()

  check('以 GET 拉取该会话详情',
    calls.some((c) => c.url === '/api/sessions/' + first.id && c.method === 'GET'))
  check('激活态切换到被点击的会话', activeId(w) === first.id, activeId(w))
  check('对话区换成该会话的消息', /甲的问题/.test(chatText(w)) && !/乙的问题/.test(chatText(w)),
    chatText(w))
  check('切换后 localStorage 同步',
    w.localStorage.getItem('bank_agent.active_session') === first.id)
  check('重复点击当前会话不再发请求', (() => {
    const n = calls.length
    click(w, items(w).filter((el) => el.dataset.id === first.id)[0])
    return calls.length === n
  })())
}

// ---------------------------------------------------------------- 7. 重命名

section('重命名会话')
{
  resetBackend()
  const session = seedSession('原名', [{ role: 'user', content: '内容' }])
  const dom = await loadPage()
  const w = dom.window

  const titleEl = items(w)[0].querySelector('.session-title')
  dblclick(w, titleEl)
  const input = items(w)[0].querySelector('.session-rename')
  check('双击标题进入编辑态', !!input)
  check('输入框预填原标题', input && input.value === '原名', input && input.value)

  input.value = '改过的名字'
  key(w, input, 'Enter')
  await settle()

  const patch = calls.filter((c) => c.url === '/api/sessions/' + session.id && c.method === 'PATCH').pop()
  check('以 PATCH 提交新标题', !!patch && patch.body.title === '改过的名字', patch && patch.body)
  check('列表标题已更新', titles(w).includes('改过的名字'), titles(w))
  check('编辑态已退出', !items(w)[0].querySelector('.session-rename'))
}

section('重命名：Esc 取消不发请求')
{
  resetBackend()
  seedSession('保持原样', [{ role: 'user', content: '内容' }])
  const dom = await loadPage()
  const w = dom.window

  dblclick(w, items(w)[0].querySelector('.session-title'))
  const input = items(w)[0].querySelector('.session-rename')
  input.value = '不该生效'
  key(w, input, 'Escape')
  await settle()

  check('Esc 后未发出 PATCH', !calls.some((c) => c.method === 'PATCH'))
  check('标题保持原样', titles(w).includes('保持原样'), titles(w))
  check('编辑态已退出', !items(w)[0].querySelector('.session-rename'))
}

// ---------------------------------------------------------------- 8. 删除

section('删除会话')
{
  resetBackend()
  const first = seedSession('会话甲', [{ role: 'user', content: '甲' }])
  const second = seedSession('会话乙', [{ role: 'user', content: '乙' }])
  const dom = await loadPage()
  const w = dom.window

  check('初始两个会话', items(w).length === 2)
  const deleteBtn = items(w).filter((el) => el.dataset.id === second.id)[0]
    .querySelector('[data-act="delete"]')
  check('每个条目都有删除按钮', !!deleteBtn)
  click(w, deleteBtn)
  await settle()

  check('以 DELETE 调用接口',
    calls.some((c) => c.url === '/api/sessions/' + second.id && c.method === 'DELETE'))
  check('被删除的条目从列表消失', !titles(w).includes('会话乙'), titles(w))
  check('剩余会话仍在', titles(w).includes('会话甲'), titles(w))
}

section('删除当前会话后自动切到下一个')
{
  resetBackend()
  const first = seedSession('会话甲', [{ role: 'user', content: '甲的问题' }])
  const second = seedSession('会话乙', [{ role: 'user', content: '乙的问题' }])
  const dom = await loadPage()
  const w = dom.window

  check('当前是最近活跃的「会话乙」', activeId(w) === second.id, activeId(w))
  click(w, items(w).filter((el) => el.dataset.id === second.id)[0].querySelector('[data-act="delete"]'))
  await settle()

  check('删除当前会话后激活剩下的会话', activeId(w) === first.id, activeId(w))
  check('对话区换成剩下会话的内容', /甲的问题/.test(chatText(w)), chatText(w))
}

section('删除最后一个会话后回到欢迎态')
{
  resetBackend()
  seedSession('唯一会话', [{ role: 'user', content: '唯一内容' }])
  const dom = await loadPage()
  const w = dom.window

  click(w, items(w)[0].querySelector('[data-act="delete"]'))
  await settle()

  check('列表变空并显示空态', items(w).length === 0 && !!$(w, 'sessionEmpty'))
  check('对话区回到欢迎语', /我是银行智能助手/.test(chatText(w)), chatText(w).slice(0, 60))
  check('清空了 localStorage 里的当前会话',
    !w.localStorage.getItem('bank_agent.active_session'),
    w.localStorage.getItem('bank_agent.active_session'))
}

section('删除需二次确认；取消则不发请求')
{
  resetBackend()
  seedSession('别删我', [{ role: 'user', content: '内容' }])
  let asked = ''
  const dom = await loadPage(stubFetch, (window) => {
    window.confirm = (message) => { asked = message; return false }
  })
  const w = dom.window

  click(w, items(w)[0].querySelector('[data-act="delete"]'))
  await settle()

  check('弹出了确认框', /别删我/.test(asked), asked)
  check('取消时不发 DELETE 请求', !calls.some((c) => c.method === 'DELETE'))
  check('会话仍在列表中', titles(w).includes('别删我'), titles(w))
}

// ---------------------------------------------------------------- 9. 侧边栏开合

section('侧边栏开合与记忆')
{
  resetBackend()
  const dom = await loadPage()
  const w = dom.window
  // 折叠类挂在 <html> 上（<head> 首帧脚本与页面脚本改的是同一个元素）
  const root = w.document.documentElement

  check('宽屏下默认展开', !root.classList.contains('sidebar-collapsed'))
  click(w, $(w, 'sidebarToggle'))
  check('点击后收起', root.classList.contains('sidebar-collapsed'))
  check('收起状态写入 localStorage',
    w.localStorage.getItem('bank_agent.sidebar') === 'collapsed',
    w.localStorage.getItem('bank_agent.sidebar'))
  check('收起后 aria-expanded 同步为 false',
    $(w, 'sidebarToggle').getAttribute('aria-expanded') === 'false',
    $(w, 'sidebarToggle').getAttribute('aria-expanded'))
  click(w, $(w, 'sidebarToggle'))
  check('再次点击展开', !root.classList.contains('sidebar-collapsed'))
  check('展开状态写入 localStorage',
    w.localStorage.getItem('bank_agent.sidebar') === 'open')

  // 记忆生效：记住「收起」后重新装载页面
  const dom2 = await loadPage(stubFetch, (window) => {
    window.localStorage.setItem('bank_agent.sidebar', 'collapsed')
  })
  check('刷新后沿用记住的收起状态',
    dom2.window.document.documentElement.classList.contains('sidebar-collapsed'))
}

// ---------------------------------------------------------------- 10. 降级

section('后端不可用时的降级')
{
  resetBackend()
  const deadFetch = async (url) => {
    if (url === '/api/sessions') throw new Error('模拟网络中断')
    return stubFetch(url)
  }
  const dom = await loadPage(deadFetch)
  const w = dom.window

  check('会话接口失败时仍显示欢迎语', /我是银行智能助手/.test(chatText(w)), chatText(w).slice(0, 60))
  check('会话接口失败时侧边栏明确提示读取失败',
    !!$(w, 'sessionEmpty') && /读取失败/.test($(w, 'sessionEmpty').textContent),
    $(w, 'sessionEmpty') && $(w, 'sessionEmpty').textContent)
  check('读取失败的占位使用错误配色', $(w, 'sessionEmpty').classList.contains('error'))
  check('会话接口失败不影响设置按钮', !!$(w, 'settingsBtn'))
  check('页面没有崩溃（输入框可用）', !!$(w, 'input') && $(w, 'input').disabled === false)
}

section('会话接口返回旧后端 404 时不弹 alert')
{
  resetBackend()
  let alerts = 0
  const oldFetch = async (url, options) => {
    if (url.indexOf('/api/sessions') === 0) return jsonResponse({ detail: 'Not Found' }, 404)
    return stubFetch(url, options)
  }
  const dom = await loadPage(oldFetch, (window) => {
    window.alert = () => { alerts += 1 }
  })
  const w = dom.window

  check('旧后端下页面仍可用', !!$(w, 'newSessionBtn'))
  check('未使用 alert 打扰用户', alerts === 0, alerts)
  check('侧边栏给出可读的失败提示（而不是一片空白）',
    !!$(w, 'sessionEmpty') && /读取失败/.test($(w, 'sessionEmpty').textContent),
    $(w, 'sessionEmpty') && $(w, 'sessionEmpty').textContent)
}

// ---------------------------------------------------------------- 11. 竞态

/*
 * 上面所有用例的桩响应都是「立刻 resolve」的，因此只能在静止态下断言。
 * 而侧边栏真正容易出错的地方恰恰在途中：请求还没回来时用户又点了别的东西。
 * 这一节用一个可控闸门把指定请求挂起，复现「迟到响应覆盖较新状态」这一类缺陷。
 */

section('竞态：迟到的响应不能覆盖较新的选择')
{
  resetBackend()
  const a = seedSession('会话A', [{ role: 'user', content: 'AAA-QUESTION' }])
  const b = seedSession('会话B', [{ role: 'user', content: 'BBB-QUESTION' }])
  const c = seedSession('会话C', [{ role: 'user', content: 'CCC-QUESTION' }])

  // 挂起「读取会话A详情」的那次请求
  let releaseA = null
  const gateFetch = async (url, options) => {
    if (url === '/api/sessions/' + a.id) {
      await new Promise((resolve) => { releaseA = resolve })
    }
    return stubFetch(url, options)
  }
  const dom = await loadPage(gateFetch)
  const w = dom.window
  // 列表顺序 [C, B, A]，默认激活最近活跃的 C；A 与 B 都不是当前会话
  check('初始激活最近活跃的会话', activeId(w) === c.id, activeId(w))

  // 点 A（详情被挂起）→ 立刻点 B（详情立刻返回并渲染）
  click(w, items(w).find((el) => el.dataset.id === a.id))
  click(w, items(w).find((el) => el.dataset.id === b.id))
  await settle(60)
  check('切换后高亮的是后点的会话', activeId(w) === b.id, activeId(w))
  check('切换后对话区是后点的会话', /BBB-QUESTION/.test(chatText(w)), chatText(w).slice(0, 80))

  // 现在放行 A 的迟到响应
  releaseA()
  await settle(60)
  check('迟到的详情响应不改写对话区', !/AAA-QUESTION/.test(chatText(w)), chatText(w).slice(0, 80))
  check('迟到的详情响应不改写高亮', activeId(w) === b.id, activeId(w))
  check('高亮与对话区保持一致（不变量）',
    activeId(w) === b.id && /BBB-QUESTION/.test(chatText(w)))
}

section('竞态：在途对话期间切换会话，回复不能落到别人头上')
{
  resetBackend()
  const x = seedSession('会话X', [{ role: 'user', content: 'XXX-OLD' }])
  const y = seedSession('会话Y', [{ role: 'user', content: 'YYY-OLD' }])

  let releaseChat = null
  const gateFetch = async (url, options) => {
    if (url === '/api/chat') {
      await new Promise((resolve) => { releaseChat = resolve })
    }
    return stubFetch(url, options)
  }
  const dom = await loadPage(gateFetch)
  const w = dom.window

  // 当前是 Y（最近活跃）；在 Y 里发消息，然后在回复到达前切到 X
  check('初始激活最近活跃的会话', activeId(w) === y.id, activeId(w))
  $(w, 'input').value = '在Y里问的问题'
  click(w, $(w, 'send'))
  await settle(60)
  check('对话请求已在途', !!releaseChat)

  click(w, items(w).find((el) => el.dataset.id === x.id))
  await settle(60)
  check('已切到会话X', activeId(w) === x.id, activeId(w))
  check('会话X的对话区只有X的内容', /XXX-OLD/.test(chatText(w)) && !/YYY-OLD/.test(chatText(w)))

  releaseChat()
  await settle(60)
  check('在途回复不追加到会话X的对话区', !/好的，已为您处理/.test(chatText(w)), chatText(w).slice(0, 100))
  check('切换后高亮仍是会话X', activeId(w) === x.id, activeId(w))
}

section('竞态：新建会话在途时点历史会话，选择不能被抢走')
{
  resetBackend()
  const x = seedSession('历史会话X', [{ role: 'user', content: 'XXX-QUESTION' }])
  const y = seedSession('历史会话Y', [{ role: 'user', content: 'YYY-QUESTION' }])

  let releaseCreate = null
  const gateFetch = async (url, options) => {
    if (url === '/api/sessions' && (options || {}).method === 'POST') {
      await new Promise((resolve) => { releaseCreate = resolve })
    }
    return stubFetch(url, options)
  }
  const dom = await loadPage(gateFetch)
  const w = dom.window

  // 列表顺序 [Y, X]，默认激活 Y；这里要点的是「另一个」会话 X
  check('初始激活历史会话Y', activeId(w) === y.id, activeId(w))
  click(w, $(w, 'newSessionBtn'))
  await settle(20)
  check('新建请求已在途', !!releaseCreate)

  click(w, items(w).find((el) => el.dataset.id === x.id))
  await settle(20)
  check('已切到会话X', activeId(w) === x.id, activeId(w))

  releaseCreate()
  await settle(60)
  check('迟到的新建响应不抢走用户的选择', activeId(w) === x.id, activeId(w))
  check('对话区仍是用户选中的会话', /XXX-QUESTION/.test(chatText(w)), chatText(w).slice(0, 80))
  check('高亮与对话区保持一致（不变量）',
    activeId(w) === x.id && /XXX-QUESTION/.test(chatText(w)))
}

section('竞态：新建在途时发送，消息不能被静默丢弃')
{
  resetBackend()
  // 一个会话都没有：页面处于「没有当前会话」的状态，发送必须先新建
  const resolvers = []
  const gateFetch = async (url, options) => {
    if (url === '/api/sessions' && (options || {}).method === 'POST') {
      await new Promise((resolve) => { resolvers.push(resolve) })
    }
    return stubFetch(url, options)
  }
  const dom = await loadPage(gateFetch)
  const w = dom.window
  check('空态下没有当前会话', activeId(w) === null)

  click(w, $(w, 'newSessionBtn'))
  await settle(20)
  check('新建请求已在途', resolvers.length === 1, resolvers.length)

  // 新建还没回来就发送：旧实现里 createSession 的布尔守卫会返回 null，消息被丢掉
  $(w, 'input').value = '这条消息不能丢'
  click(w, $(w, 'send'))
  await settle(20)

  resolvers.splice(0).forEach((resolve) => resolve())
  await settle(80)

  check('发送没有被静默丢弃（/api/chat 被调用）',
    calls.some((c) => c.url === '/api/chat'), calls.map((c) => c.url).join(','))
  check('消息出现在对话区', /这条消息不能丢/.test(chatText(w)), chatText(w).slice(0, 80))
  check('新会话被激活', activeId(w) !== null, activeId(w))
}

section('竞态：迟到的列表快照不能让已删除的会话复活')
{
  resetBackend()
  const x = seedSession('会话X', [{ role: 'user', content: 'X' }])
  const y = seedSession('会话Y', [{ role: 'user', content: 'Y' }])

  // 第 2 次 GET /api/sessions 会被挂起（删除前的旧快照）
  let listCalls = 0
  let releaseList = null
  const gateFetch = async (url, options) => {
    if (url === '/api/sessions' && (options || {}).method !== 'POST') {
      listCalls += 1
      if (listCalls === 2) {
        // 先取一份「删除前」的快照，再挂起
        const snapshot = await stubFetch(url, options)
        await new Promise((resolve) => { releaseList = resolve })
        return snapshot
      }
    }
    return stubFetch(url, options)
  }
  const dom = await loadPage(gateFetch)
  const w = dom.window
  check('初始列出两个会话', items(w).length === 2, titles(w))

  // 触发第 2 次列表请求并把它挂起（模拟「发消息后的刷新」）
  $(w, 'input').value = '触发一次列表刷新'
  click(w, $(w, 'send'))
  await settle(60)
  check('第二次列表请求已挂起', !!releaseList)

  // 挂起期间删除会话Y，第 3 次列表请求会立刻返回最新快照
  click(w, items(w).find((el) => el.dataset.id === y.id).querySelector('[data-act="delete"]'))
  await settle(60)
  check('删除后列表只剩会话X', titles(w).join('|') === '会话X', titles(w))

  // 放行那份「删除前」的旧快照
  releaseList()
  await settle(60)
  check('迟到的旧快照不让已删除的会话复活', !titles(w).includes('会话Y'), titles(w))
}

section('重命名编辑期间列表重绘不会打断输入')
{
  resetBackend()
  seedSession('会话X', [{ role: 'user', content: 'X' }])
  const dom = await loadPage()
  const w = dom.window

  const titleEl = items(w)[0].querySelector('.session-title')
  dblclick(w, titleEl)
  const input = w.document.querySelector('.session-rename')
  check('双击标题进入编辑态', !!input)

  // 编辑期间发生一次列表重绘（例如消息发送后的刷新）
  input.value = '还没打完的名'
  await w.loadSessions()
  await settle(40)

  check('列表重绘不会销毁编辑框', w.document.querySelector('.session-rename') === input)
  check('编辑框里的半截标题没被强制提交', !calls.some((c) => c.method === 'PATCH'),
    JSON.stringify(calls.filter((c) => c.method === 'PATCH')))

  // 编辑结束后补上挂起的重绘，且提交的是最终值
  key(w, input, 'Enter')
  await settle(40)
  check('回车提交最终标题', calls.some((c) => c.method === 'PATCH' && c.body.title === '还没打完的名'),
    JSON.stringify(calls.filter((c) => c.method === 'PATCH')))
  check('提交后不再残留编辑框', !w.document.querySelector('.session-rename'))
}

section('会话操作失败走侧边栏提示，不写进对话记录')
{
  resetBackend()
  seedSession('会话X', [{ role: 'user', content: 'X-QUESTION' }])
  const failingFetch = async (url, options) => {
    if (url.indexOf('/api/sessions/') === 0 && (options || {}).method === 'DELETE') {
      return fail('会话不存在或已被删除', 'SESSION_NOT_FOUND', 404)
    }
    return stubFetch(url, options)
  }
  const dom = await loadPage(failingFetch)
  const w = dom.window

  click(w, items(w)[0].querySelector('[data-act="delete"]'))
  await settle(60)

  check('失败提示出现在侧边栏', !$(w, 'sidebarAlert').hidden
    && /删除会话失败/.test($(w, 'sidebarAlert').textContent), $(w, 'sidebarAlert').textContent)
  check('失败提示没有混进对话记录', !/删除会话失败/.test(chatText(w)), chatText(w).slice(0, 80))
  check('对话区内容保持不变', /X-QUESTION/.test(chatText(w)))
}

// ---------------------------------------------------------------- 汇总

console.log(`\n${checks - failures}/${checks} 项通过`)
if (failures > 0) {
  console.log(`${failures} 项失败`)
  process.exit(1)
}
console.log('全部通过')
