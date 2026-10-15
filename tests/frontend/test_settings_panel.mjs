/**
 * 前端「模型设置面板」的 DOM 集成测试。
 *
 * 用 jsdom 真实加载 web_test/index.html 并执行其中的内联脚本，再用打桩的 fetch
 * 模拟后端（严格按 docs/前端模型接入配置-契约.md 的响应格式），驱动设置面板的
 * 交互，验证：面板结构与初始状态、已下线的供应商预设 / 高级参数彻底移除、
 * 响应格式下拉、测试/保存/清除的请求与载荷、前端校验、密钥不写入 localStorage、
 * 联网搜索（可选，默认折叠）、模式徽标与气泡元信息渲染。
 *
 * 运行方式（Node 18+）：
 *     cd tests/frontend && npm install jsdom && node test_settings_panel.mjs
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

// ---------------------------------------------------------------- 契约响应桩

// 真实后端快照仍带 temperature / max_tokens（面板已不再使用），响应格式为新增字段。
// [注意] 未配置时真实后端给的 timeout 是 0.0 而不是 30.0（见
// common/model_manager.py 的 snapshot：config 为 None 时取 0.0）。
// 这里必须照实模仿，否则会掩盖「输入框被预填 0、前端校验拦死请求」这类缺陷。
const LLM_SNAPSHOT_MOCK = {
  configured: false, mode: 'mock', source: 'none', base_url: '', model: '',
  response_format: 'text', temperature: 0.0, max_tokens: 1024, timeout: 0.0,
  api_key_configured: false, api_key_masked: '', env_api_key_present: false,
  updated_at: null, active_model_class: 'MockChat',
}

const LLM_SNAPSHOT_ACTIVE = {
  configured: true, mode: 'model', source: 'runtime',
  base_url: 'https://api.deepseek.com/v1', model: 'deepseek-flash',
  response_format: 'text', temperature: 0.0, max_tokens: 1024, timeout: 30.0,
  api_key_configured: true, api_key_masked: 'sk-...9999', env_api_key_present: false,
  updated_at: '2026-01-01T00:00:00+00:00', active_model_class: 'DeepSeekChat',
}

const SEARCH_SNAPSHOT = {
  configured: false, mode: 'unconfigured', source: 'none', base_url: '',
  max_results: 5, timeout: 10.0, api_key_configured: false, api_key_masked: '',
  env_api_key_present: false, updated_at: null,
}

// api_version 是后端为「旧进程未注册 /api/settings/*」这一故障新增的版本标记：
// 页面靠它判断后端新旧（缺失即旧版，弹出警示条）。正常路径的桩必须带上它，
// 否则旧后端警示条会污染其它断言。
const HEALTH = {
  status: 'ok', model_mode: 'mock', model: '', base_url: '', search_configured: false,
  api_version: 'test',
}

/** 记录所有请求，并按路径返回契约响应（未打桩的 URL 直接抛错，用于发现多余请求）。 */
const calls = []
let llmSnapshot = LLM_SNAPSHOT_MOCK

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }
}

async function stubFetch(url, options = {}) {
  const method = options.method || 'GET'
  const body = options.body ? JSON.parse(options.body) : undefined
  calls.push({ url, method, body })

  if (url === '/api/settings/llm' && method === 'GET') {
    return jsonResponse({ ok: true, message: '', error_code: null, data: llmSnapshot })
  }
  if (url === '/api/settings/llm' && method === 'PUT') {
    if (!body.api_key) {
      return jsonResponse({ ok: false, message: '请填写 API Key', error_code: 'LLM_KEY_REQUIRED', data: null }, 422)
    }
    llmSnapshot = {
      ...LLM_SNAPSHOT_ACTIVE,
      base_url: body.base_url,
      model: body.model,
      response_format: body.response_format,
      timeout: body.timeout == null ? LLM_SNAPSHOT_ACTIVE.timeout : body.timeout,
    }
    return jsonResponse({ ok: true, message: '模型配置已生效', error_code: null, data: llmSnapshot })
  }
  if (url === '/api/settings/llm/test' && method === 'POST') {
    if (body.model === 'bad-model') {
      return jsonResponse({
        ok: false, message: '模型名不存在：请检查模型名是否正确', error_code: 'LLM_MODEL_NOT_FOUND',
        data: { latency_ms: null, model: body.model },
      })
    }
    return jsonResponse({
      ok: true, message: '连接成功', error_code: null,
      data: { ok: true, latency_ms: 812, model: body.model, reply: 'pong' },
    })
  }
  if (url === '/api/settings/llm' && method === 'DELETE') {
    llmSnapshot = LLM_SNAPSHOT_MOCK
    return jsonResponse({ ok: true, message: '已清除运行时模型配置，当前为 Mock 离线模式', error_code: null, data: llmSnapshot })
  }
  if (url === '/api/settings/search' && method === 'GET') {
    return jsonResponse({ ok: true, message: '', error_code: null, data: SEARCH_SNAPSHOT })
  }
  if (url === '/api/settings/search/test' && method === 'POST') {
    return jsonResponse({ ok: true, message: '连接成功', error_code: null, data: { ok: true, latency_ms: 240, result_count: 1 } })
  }
  if (url === '/api/settings/search' && method === 'PUT') {
    return jsonResponse({
      ok: true, message: '搜索配置已生效', error_code: null,
      data: { ...SEARCH_SNAPSHOT, configured: true, mode: 'model', source: 'runtime', base_url: body.base_url },
    })
  }
  if (url === '/api/settings/search' && method === 'DELETE') {
    return jsonResponse({ ok: true, message: '已清除运行时搜索配置', error_code: null, data: SEARCH_SNAPSHOT })
  }
  if (url === '/health/live') {
    return jsonResponse({ ...HEALTH, model_mode: llmSnapshot.mode, model: llmSnapshot.model })
  }
  if (url === '/api/chat') {
    return jsonResponse({
      reply: '好的，已为您处理。', blocked: false, moderation: {}, tasks: [], agent: 'R1',
      trace: { session_id: 's', steps: 1, tool_names: ['transfer_by_name'], model_mode: 'model', model_name: 'deepseek-flash', agent: 'R1' },
    })
  }
  throw new Error('未打桩的请求: ' + method + ' ' + url)
}

// ---------------------------------------------------------------- 装载页面

const html = readFileSync(htmlPath, 'utf8')
const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8000/',
  beforeParse(window) {
    window.fetch = stubFetch
    window.confirm = () => true
  },
})

const { window } = dom
const { document } = window

// 等待脚本内的异步初始化（refreshModeBadge 等）跑完
const settle = () => new Promise((r) => setTimeout(r, 30))
await settle()

const $ = (id) => document.getElementById(id)
const click = (el) => el.dispatchEvent(new window.Event('click', { bubbles: true }))

const panel = document.querySelector('.settings-panel')
const modelSection = document.querySelector('.settings-body > section.settings-section')
const footerButtons = () => [...document.querySelectorAll('.settings-panel > footer button')]

// ---------------------------------------------------------------- 用例

section('页面结构与初始状态')
check('设置面板默认隐藏', $('settingsOverlay').hidden === true)
// [回归护栏] 作者样式表里的 `.settings-overlay { display: flex }` 会压过浏览器默认
// 样式表的 `[hidden] { display: none }`（作者普通声明 > UA 普通声明，与权重无关），
// 于是弹窗即使带 hidden 属性也会全屏渲染。jsdom 的层叠模型与真实浏览器不同
// （它按权重比较，会误报 display:none），因此这里退而对样式表文本做断言。
const styleText = document.querySelector('style').textContent
// 先剥掉 CSS 注释再断言：注释里也会出现 `[hidden] { display: none }` 这样的说明文字，
// 不剥掉的话「删掉真正的规则」也照样能匹配上，护栏就形同虚设。
const cssRules = styleText.replace(/\/\*[\s\S]*?\*\//g, '')
check('遮罩层使用 display:flex（所以必须显式覆盖 [hidden]）',
  /\.settings-overlay\s*\{[^}]*display:\s*flex/.test(cssRules))
check('样式表显式把 [hidden] 置为 display:none（否则弹窗会一直显示）',
  /\[hidden\][^{]*\{[^}]*display:\s*none/.test(cssRules),
  (cssRules.match(/\[hidden\][^{]*\{[^}]*\}/) || ['(未找到)'])[0])
check('顶栏存在设置按钮', !!$('settingsBtn'))
check('API Key 输入框为 password 类型', $('llmApiKey').type === 'password')
check('初始模式徽标已由 /health/live 填充', /Mock/.test($('modeBadgeText').textContent), $('modeBadgeText').textContent)
check('页面加载时请求过 /health/live', calls.some((c) => c.url === '/health/live'))

section('已下线的控件彻底移除')
check('llmProvider 已删除', $('llmProvider') === null)
check('llmProviderNote 已删除', $('llmProviderNote') === null)
check('llmModelOptions 已删除', $('llmModelOptions') === null)
check('llmTemperature 已删除', $('llmTemperature') === null)
check('llmMaxTokens 已删除', $('llmMaxTokens') === null)
check('面板内无 temperature / max_tokens 输入',
  panel.querySelectorAll('#llmTemperature, #llmMaxTokens, [name="temperature"], [name="max_tokens"]').length === 0)
check('面板内无供应商下拉', panel.querySelectorAll('select#llmProvider, datalist').length === 0)

section('模型表单结构')
const modelControls = [...modelSection.querySelectorAll('input, select, textarea')]
check('模型表单恰好 5 个控件', modelControls.length === 5, modelControls.map((el) => el.id))
check('模型表单控件与约定一致',
  modelControls.map((el) => el.id).join(',') === 'llmBaseUrl,llmApiKey,llmModel,llmResponseFormat,llmTimeout',
  modelControls.map((el) => el.id))
check('Base URL 占位符为示例地址', $('llmBaseUrl').placeholder === 'https://example.com/v1', $('llmBaseUrl').placeholder)
check('API Key 占位符正确', $('llmApiKey').placeholder === '请输入 API Key', $('llmApiKey').placeholder)
check('模型名占位符正确', $('llmModel').placeholder === '模型名称', $('llmModel').placeholder)

const formatOptions = [...$('llmResponseFormat').options]
check('响应格式恰好 3 个选项', formatOptions.length === 3, formatOptions.length)
check('响应格式取值与顺序正确',
  formatOptions.map((o) => o.value).join(',') === 'text,json_object,json_schema',
  formatOptions.map((o) => o.value))
check('响应格式文案与顺序正确',
  formatOptions.map((o) => o.textContent).join(',') === '文本（推荐）,JSON 对象,JSON Schema',
  formatOptions.map((o) => o.textContent))
check('响应格式默认选中 text', $('llmResponseFormat').value === 'text', $('llmResponseFormat').value)

check('超时是常显的数字输入框', $('llmTimeout').tagName === 'INPUT' && $('llmTimeout').type === 'number')
check('超时范围 1~300', $('llmTimeout').min === '1' && $('llmTimeout').max === '300', [$('llmTimeout').min, $('llmTimeout').max])
check('超时留空时用占位符提示默认值', $('llmTimeout').placeholder === '30', $('llmTimeout').placeholder)
check('响应格式与超时同处一行',
  $('llmResponseFormat').closest('.settings-row') === $('llmTimeout').closest('.settings-row'))

section('面板头部与底部按钮')
check('标题为「模型设置」', $('settingsTitle').textContent.trim() === '模型设置', $('settingsTitle').textContent)
check('模型徽标位于面板头部',
  $('llmBadge').parentElement === document.querySelector('.settings-panel > header'),
  $('llmBadge').parentElement && $('llmBadge').parentElement.tagName)
check('头部徽标紧邻标题', $('llmBadge').previousElementSibling === $('settingsTitle'))
check('关闭按钮在头部右侧',
  document.querySelector('.settings-panel > header > #settingsClose') === $('settingsClose'))
check('面板底部恰好 3 个按钮', footerButtons().length === 3, footerButtons().map((b) => b.textContent))
check('底部按钮文案与顺序为 清除配置 / 测试连接 / 保存并启用',
  footerButtons().map((b) => b.textContent.trim()).join('|') === '清除配置|测试连接|保存并启用',
  footerButtons().map((b) => b.textContent.trim()))
check('清除配置为危险样式', $('llmClear').classList.contains('danger'))
check('保存并启用为主按钮', $('llmSave').classList.contains('primary'))
check('底部不含搜索按钮',
  !footerButtons().some((b) => ['searchClear', 'searchTest', 'searchSave'].includes(b.id)))

section('联网搜索为可选折叠块')
check('searchDetails 是 details 元素', $('searchDetails').tagName === 'DETAILS', $('searchDetails').tagName)
check('searchDetails 默认折叠', $('searchDetails').open === false)
check('summary 文案含「可选」', /可选/.test($('searchDetails').querySelector('summary').textContent),
  $('searchDetails').querySelector('summary').textContent)
check('summary 携带搜索徽标', $('searchDetails').querySelector('summary #searchBadge') === $('searchBadge'))
const searchDesc = $('searchDetails').querySelector('.desc').textContent
check('说明文字明确这是可选功能', /可选/.test(searchDesc), searchDesc)
check('说明文字说明转账/账单无需搜索', /转账/.test(searchDesc) && /账单/.test(searchDesc), searchDesc)
check('搜索测试按钮在折叠块内', $('searchDetails').contains($('searchTest')))
check('搜索保存按钮在折叠块内', $('searchDetails').contains($('searchSave')))
check('搜索清除按钮在折叠块内', $('searchDetails').contains($('searchClear')))

section('打开面板并加载配置')
const settingsCallsBefore = calls.filter((c) => c.url.startsWith('/api/settings/')).length
click($('settingsBtn'))
await settle()
const openedCalls = calls.filter((c) => c.url.startsWith('/api/settings/')).slice(settingsCallsBefore)
check('面板已显示', $('settingsOverlay').hidden === false)
check('拉取过模型配置', calls.some((c) => c.url === '/api/settings/llm' && c.method === 'GET'))
check('拉取过搜索配置', calls.some((c) => c.url === '/api/settings/search' && c.method === 'GET'))
check('打开面板恰好发起 2 个 /api/settings/* 请求', openedCalls.length === 2,
  openedCalls.map((c) => c.method + ' ' + c.url))
check('两个请求分别是模型与搜索配置',
  openedCalls.map((c) => c.method + ' ' + c.url).sort().join(' | ') === 'GET /api/settings/llm | GET /api/settings/search',
  openedCalls.map((c) => c.method + ' ' + c.url))
check('未请求已下线的供应商预设接口', !calls.some((c) => c.url.includes('presets')))
check('未配置时 Key 提示为“必填”', /必填/.test($('llmKeyHint').textContent), $('llmKeyHint').textContent)
check('未配置时面板徽标显示「未配置模型」', $('llmBadgeText').textContent === '未配置模型', $('llmBadgeText').textContent)
check('未配置时清除按钮置灰', $('llmClear').disabled === true)
check('打开面板不自动展开搜索详情', $('searchDetails').open === false)

// [回归] 未配置时真实后端快照给的 timeout 是 0.0；若按 `!= null` 回填，输入框会变成
// "0"，前端校验随即以「超时需在 1 ~ 300 秒之间」拦下请求 —— 用户连「测试连接」都点不动。
// 这里断言「刚打开面板时超时框是空的」，并且「不碰超时框也能真的发出请求」。
section('未配置快照下仍可直接测试连接（回归：超时被预填 0 会拦死请求）')
check('刚打开面板时超时输入框为空（而不是 0）', $('llmTimeout').value === '', $('llmTimeout').value)
$('llmBaseUrl').value = 'https://api.deepseek.com/v1'
$('llmModel').value = 'deepseek-flash'
$('llmApiKey').value = 'sk-first-run-9999'
const beforeFreshTest = calls.length
click($('llmTest'))
await settle()
const freshTestCall = calls.slice(beforeFreshTest).filter((c) => c.url === '/api/settings/llm/test')
check('未配置环境下点「测试连接」确实发出请求', freshTestCall.length === 1, calls.slice(beforeFreshTest))
check('未被本地超时校验拦下', !/超时/.test($('llmMsg').textContent), $('llmMsg').textContent)
check('超时留空时载荷为 null（由后端套用默认 30 秒）',
  freshTestCall.length === 1 && freshTestCall[0].body.timeout === null,
  freshTestCall.length === 1 ? freshTestCall[0].body : null)

section('API Key 显示/隐藏切换')
check('初始为隐藏', $('llmApiKey').type === 'password')
click($('llmKeyToggle'))
check('点击后变明文', $('llmApiKey').type === 'text')
check('按钮文案变为“隐藏”', $('llmKeyToggle').textContent === '隐藏')
click($('llmKeyToggle'))
check('再次点击恢复隐藏', $('llmApiKey').type === 'password')

section('前端校验：非法输入不发请求')
$('llmBaseUrl').value = 'not-a-url'
$('llmModel').value = 'm'
$('llmApiKey').value = 'k-1234567890'
$('llmTimeout').value = '30'
const beforeInvalid = calls.length
click($('llmTest'))
await settle()
check('非法 URL 未发起请求', calls.length === beforeInvalid, calls.slice(beforeInvalid))
check('内联提示 http:// 要求', /http/.test($('llmMsg').textContent), $('llmMsg').textContent)
check('提示条以错误样式展示', $('llmMsg').className.includes('error'), $('llmMsg').className)
check('校验失败不改变清除按钮状态', $('llmClear').disabled === true)

$('llmBaseUrl').value = 'https://api.deepseek.com/v1'
$('llmModel').value = ''
const beforeNoModel = calls.length
click($('llmTest'))
await settle()
check('缺模型名未发起请求', calls.length === beforeNoModel)
check('提示需要模型名', /模型名/.test($('llmMsg').textContent), $('llmMsg').textContent)

$('llmBaseUrl').value = 'https://user:pass@api.deepseek.com/v1'
$('llmModel').value = 'deepseek-flash'
const beforeCreds = calls.length
click($('llmTest'))
await settle()
check('内嵌凭据的 URL 未发起请求', calls.length === beforeCreds)
check('提示不要内嵌用户名密码', /用户名或密码/.test($('llmMsg').textContent), $('llmMsg').textContent)
$('llmBaseUrl').value = 'https://api.deepseek.com/v1'

$('llmTimeout').value = '999'
const beforeTimeout = calls.length
click($('llmTest'))
await settle()
check('超时越界未发起请求', calls.length === beforeTimeout)
check('提示超时范围', /超时/.test($('llmMsg').textContent), $('llmMsg').textContent)
$('llmTimeout').value = '30'

section('测试连接')
$('llmModel').value = 'deepseek-flash'
click($('llmTest'))
await settle()
const testCall = calls.filter((c) => c.url === '/api/settings/llm/test').pop()
check('以 POST 调用 /api/settings/llm/test', testCall && testCall.method === 'POST')
check('测试载荷字段恰好为约定的 5 个',
  JSON.stringify(Object.keys(testCall.body).sort()) === JSON.stringify(['api_key', 'base_url', 'model', 'response_format', 'timeout']),
  Object.keys(testCall.body))
check('测试载荷 base_url 正确', testCall.body.base_url === 'https://api.deepseek.com/v1')
check('测试载荷 api_key 正确', testCall.body.api_key === 'k-1234567890')
check('测试载荷 model 正确', testCall.body.model === 'deepseek-flash')
check('测试载荷 response_format 默认 text', testCall.body.response_format === 'text', testCall.body.response_format)
check('测试载荷 timeout 为数字', testCall.body.timeout === 30, testCall.body.timeout)
check('成功提示含耗时', /812/.test($('llmMsg').textContent), $('llmMsg').textContent)
check('成功提示为 success 样式', $('llmMsg').className.includes('success'), $('llmMsg').className)

$('llmTimeout').value = ''
click($('llmTest'))
await settle()
const blankTimeoutCall = calls.filter((c) => c.url === '/api/settings/llm/test').pop()
check('超时留空时载荷为 null（由后端套用默认值）', blankTimeoutCall.body.timeout === null, blankTimeoutCall.body.timeout)
$('llmTimeout').value = '30'

$('llmModel').value = 'bad-model'
click($('llmTest'))
await settle()
check('失败提示含错误码', /LLM_MODEL_NOT_FOUND/.test($('llmMsg').textContent), $('llmMsg').textContent)
check('失败提示为 error 样式', $('llmMsg').className.includes('error'))
check('测试失败后清除按钮仍置灰', $('llmClear').disabled === true)

section('保存并启用')
$('llmModel').value = 'deepseek-flash'
$('llmResponseFormat').value = 'json_object'
$('llmTimeout').value = '45'
click($('llmSave'))
await settle()
const putCall = calls.filter((c) => c.url === '/api/settings/llm' && c.method === 'PUT').pop()
check('以 PUT 调用 /api/settings/llm', !!putCall)
check('PUT 载荷字段恰好为约定的 5 个',
  JSON.stringify(Object.keys(putCall.body).sort()) === JSON.stringify(['api_key', 'base_url', 'model', 'response_format', 'timeout']),
  Object.keys(putCall.body))
check('PUT 载荷 response_format 为用户所选', putCall.body.response_format === 'json_object', putCall.body.response_format)
check('PUT 载荷 timeout 为数字', putCall.body.timeout === 45, putCall.body.timeout)
check('保存后面板徽标含模型模式与模型名',
  /模型模式 · deepseek-flash/.test($('llmBadgeText').textContent), $('llmBadgeText').textContent)
check('保存后顶栏徽标同步', /模型模式/.test($('modeBadgeText').textContent), $('modeBadgeText').textContent)
check('保存成功后清空 Key 输入框', $('llmApiKey').value === '')
check('保存后清除按钮可用', $('llmClear').disabled === false)
check('保存后回填服务端返回的响应格式', $('llmResponseFormat').value === 'json_object', $('llmResponseFormat').value)

section('本地偏好记忆（且绝不存 Key）')
const stored = window.localStorage.getItem('bank_agent.llm.pref') || ''
check('已写入偏好', stored.length > 0, stored)
const prefKeys = Object.keys(JSON.parse(stored)).sort()
check('偏好只含 base_url / model / response_format',
  JSON.stringify(prefKeys) === JSON.stringify(['base_url', 'model', 'response_format']), prefKeys)
check('偏好含 base_url', /api\.deepseek\.com/.test(stored), stored)
check('偏好含 response_format', /"response_format":"json_object"/.test(stored), stored)
check('偏好不含 API Key', !stored.includes('k-1234567890') && !stored.includes('sk-user-typed-9999'), stored)
const allStorage = Object.keys(window.localStorage).map((k) => window.localStorage.getItem(k)).join('|')
check('localStorage 整体不含 Key', !allStorage.includes('k-1234567890') && !allStorage.includes('sk-user-typed-9999'))

section('清除配置（需二次确认）')
let confirmAsked = false
window.confirm = (msg) => { confirmAsked = true; return msg.includes('清除'); }
click($('llmClear'))
await settle()
check('弹出过确认框', confirmAsked)
check('以 DELETE 调用 /api/settings/llm', calls.some((c) => c.url === '/api/settings/llm' && c.method === 'DELETE'))
check('清除后面板徽标显示「未配置模型」', $('llmBadgeText').textContent === '未配置模型', $('llmBadgeText').textContent)
check('清除后清除按钮重新置灰', $('llmClear').disabled === true)

window.confirm = () => false
const beforeCancel = calls.length
click($('llmClear'))
await settle()
check('取消确认时不发请求', calls.length === beforeCancel)

section('联网搜索接入（可选）')
$('searchDetails').open = true
$('searchBaseUrl').value = 'https://api.tavily.com/search'
$('searchApiKey').value = 'tvly-1234567890'
click($('searchTest'))
await settle()
check('搜索测试以 POST 调用', calls.some((c) => c.url === '/api/settings/search/test' && c.method === 'POST'))
check('搜索测试成功提示含耗时', /240/.test($('searchMsg').textContent), $('searchMsg').textContent)

click($('searchSave'))
await settle()
const searchPut = calls.filter((c) => c.url === '/api/settings/search' && c.method === 'PUT').pop()
check('搜索保存以 PUT 调用', !!searchPut)
check('搜索载荷含 max_results', 'max_results' in searchPut.body, Object.keys(searchPut.body))
check('搜索状态标签变为已配置', /已配置/.test($('searchBadgeText').textContent), $('searchBadgeText').textContent)

window.confirm = () => true
click($('searchClear'))
await settle()
check('搜索清除以 DELETE 调用', calls.some((c) => c.url === '/api/settings/search' && c.method === 'DELETE'))
check('搜索清除后提示已清除', /清除/.test($('searchMsg').textContent), $('searchMsg').textContent)

section('关闭面板')
click($('settingsClose'))
check('面板已隐藏', $('settingsOverlay').hidden === true)
click($('settingsBtn'))
await settle()
check('可再次打开', $('settingsOverlay').hidden === false)
document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
check('Esc 可关闭', $('settingsOverlay').hidden === true)

section('聊天区联动：气泡元信息展示模型模式')
$('input').value = '给张三转账100元'
click($('send'))
await new Promise((r) => setTimeout(r, 80))
const metas = document.querySelectorAll('#chat .meta')
const lastMeta = metas[metas.length - 1]
check('产生了助手气泡元信息', !!lastMeta)
check('元信息含模式徽标', !!lastMeta && !!lastMeta.querySelector('.mode-badge'))
check('徽标文案含模型模式与模型名', !!lastMeta && /模型模式 · deepseek-flash/.test(lastMeta.textContent), lastMeta && lastMeta.textContent)
check('仍保留工具调用信息', !!lastMeta && /transfer_by_name/.test(lastMeta.textContent), lastMeta && lastMeta.textContent)

section('后端不可用时的健壮性')
window.fetch = async () => { throw new Error('模拟网络中断') }
click($('settingsBtn'))
await settle()
check('面板仍能打开', $('settingsOverlay').hidden === false)
check('展示了读取失败的友好提示', $('llmMsg').hidden === false && $('llmMsg').textContent.length > 0, $('llmMsg').textContent)

// ---------------------------------------------------------------- 失败诊断与超时
//
// 上面所有用例共用一个 JSDOM 实例，这里改用「每次重新装载一份页面」的方式，
// 以便替换 fetch 打桩、并把 REQUEST_TIMEOUTS 缩短到生产值的几十分之一
// （页面脚本启动时会用 window.REQUEST_TIMEOUTS 覆盖自己的默认值），
// 免得测试真的等满 8 秒。生产默认值另有断言看护。

/** 注入的超时：100ms / 200ms，对应文案「0.1 秒」。 */
const FAST_TIMEOUTS = { snapshot: 100, probe: 200 };

/** 记录被注入页面的 alert 调用次数（页面不允许弹 alert，只允许页面内提示）。 */
const alertCalls = { count: 0 };

/** 用给定 fetch 打桩重新装载一份页面；timeouts 省略时用页面自己的生产默认值。 */
async function loadFreshPage(fetchImpl, timeouts) {
  const fresh = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://127.0.0.1:8000/',
    beforeParse(window) {
      window.fetch = fetchImpl;
      window.confirm = () => true;
      window.alert = () => { alertCalls.count += 1; };
      if (timeouts) window.REQUEST_TIMEOUTS = Object.assign({}, timeouts);
    },
  });
  await new Promise((r) => setTimeout(r, 40));
  return fresh;
}

/** 打开设置面板（触发 loadSettings）。 */
function openPanel(window) {
  window.document.getElementById('settingsBtn').dispatchEvent(new window.Event('click', { bubbles: true }));
}

/** 一份「一切正常」的 fetch 打桩：模型已配置、搜索未配置。 */
async function healthyFetch(url) {
  if (url === '/api/settings/llm') return jsonResponse({ ok: true, message: '', error_code: null, data: LLM_SNAPSHOT_ACTIVE });
  if (url === '/api/settings/search') return jsonResponse({ ok: true, message: '', error_code: null, data: SEARCH_SNAPSHOT });
  if (url === '/health/live') return jsonResponse({ ...HEALTH, model_mode: 'model', model: 'deepseek-flash' });
  throw new Error('未打桩的请求: ' + url);
}

section('超时配置：生产默认值不因测试注入而改变')
{
  const domDefault = await loadFreshPage(healthyFetch); // 不注入 => 页面自己的默认值
  const t = domDefault.window.REQUEST_TIMEOUTS;
  check('页面暴露可覆盖的 REQUEST_TIMEOUTS 常量', !!t && typeof t === 'object', t);
  check('GET 快照类默认超时为 8000ms', t && t.snapshot === 8000, t);
  check('POST/PUT 探测类默认超时为 60000ms', t && t.probe === 60000, t);
}

section('超时配置：探测类前端超时必须比后端更宽（否则慢网关会假超时）')
{
  const w = (await loadFreshPage(healthyFetch)).window; // 生产默认值，无注入
  const probe = w.probeTimeoutMs;
  check('页面暴露 probeTimeoutMs', typeof probe === 'function');
  // 后端按请求体里的 timeout 等待上游（契约 1~300 秒），前端至少要多留余量
  check('timeout=120 秒时前端 > 120 秒', probe({ timeout: 120 }) > 120000, probe({ timeout: 120 }));
  check('timeout=120 秒时前端 >= 130 秒（多留 10 秒余量）', probe({ timeout: 120 }) >= 130000, probe({ timeout: 120 }));
  check('timeout=300 秒（契约上限）时前端 > 300 秒', probe({ timeout: 300 }) > 300000, probe({ timeout: 300 }));
  // 未填 => 后端套用默认 30 秒，前端仍不得低于 60 秒下限
  check('timeout=null（留空，后端默认 30 秒）时前端为 60000ms', probe({ timeout: null }) === 60000, probe({ timeout: null }));
  check('timeout 缺失时前端为 60000ms', probe({}) === 60000, probe({}));
  check('timeout 非法（NaN）时回落到默认 30 秒口径', probe({ timeout: 'abc' }) === 60000, probe({ timeout: 'abc' }));
  check('timeout=10 秒（低于下限）时仍保持 60000ms 下限', probe({ timeout: 10 }) === 60000, probe({ timeout: 10 }));

  // 注入时按注入值走，保证上面那些用例不会真的等满几十秒
  const wFast = (await loadFreshPage(healthyFetch, FAST_TIMEOUTS)).window;
  check('注入 REQUEST_TIMEOUTS 时按注入值走（不被放大）',
    wFast.probeTimeoutMs({ timeout: 300 }) === FAST_TIMEOUTS.probe, wFast.probeTimeoutMs({ timeout: 300 }));
}

section('回归护栏：健康响应包仍照常应用快照')
{
  const domOk = await loadFreshPage(healthyFetch, FAST_TIMEOUTS);
  const { window: w } = domOk;
  const d = w.document;
  const $w = (id) => d.getElementById(id);
  openPanel(w);
  await settle();
  check('健康包下 Base URL 被回填', $w('llmBaseUrl').value === 'https://api.deepseek.com/v1', $w('llmBaseUrl').value);
  check('健康包下模型名被回填', $w('llmModel').value === 'deepseek-flash', $w('llmModel').value);
  check('健康包下模型徽标为「模型模式 · deepseek-flash」',
    $w('llmBadgeText').textContent === '模型模式 · deepseek-flash', $w('llmBadgeText').textContent);
  check('健康包下不显示错误提示', $w('llmMsg').hidden === true, $w('llmMsg').textContent);
  check('健康包下清除按钮可用（mode=model）', $w('llmClear').disabled === false);
  check('健康包下搜索徽标为「未配置」', $w('searchBadgeText').textContent === '未配置', $w('searchBadgeText').textContent);
}

section('失败诊断：旧后端对 /api/settings/* 返回 FastAPI 默认 404')
{
  const notFoundFetch = async (url) => {
    if (url === '/health/live') return jsonResponse(HEALTH);
    if (url.indexOf('/api/settings/') === 0) return jsonResponse({ detail: 'Not Found' }, 404);
    throw new Error('未打桩的请求: ' + url);
  };
  const dom404 = await loadFreshPage(notFoundFetch, FAST_TIMEOUTS);
  const { window: w } = dom404;
  const d = w.document;
  const $w = (id) => d.getElementById(id);
  openPanel(w);
  await settle();

  const msg = $w('llmMsg');
  check('404 时 #llmMsg 可见', msg.hidden === false, msg.textContent);
  check('404 时 #llmMsg 带 error 样式', msg.className.includes('error'), msg.className);
  check('404 时 #llmMsg 含状态码 404', /404/.test(msg.textContent), msg.textContent);
  check('404 时 #llmMsg 带出 detail 原文（Not Found）', /Not Found/.test(msg.textContent), msg.textContent);
  check('404 时 #llmMsg 含重启提示 python web_test/server.py',
    msg.textContent.includes('python web_test/server.py'), msg.textContent);
  check('404 时 #llmMsg 是单行文案', !/[\r\n]/.test(msg.textContent), msg.textContent);

  check('404 时模型徽标落到「读取失败」', $w('llmBadgeText').textContent === '读取失败', $w('llmBadgeText').textContent);
  check('404 时模型徽标为灰色 mock 样式', $w('llmBadge').className === 'mode-badge mode-mock', $w('llmBadge').className);
  check('404 时搜索徽标不再是「读取中…」', $w('searchBadgeText').textContent !== '读取中…', $w('searchBadgeText').textContent);
  check('404 时搜索徽标落到「读取失败」', $w('searchBadgeText').textContent === '读取失败', $w('searchBadgeText').textContent);
  check('404 时搜索提示条也可见且含 404',
    $w('searchMsg').hidden === false && /404/.test($w('searchMsg').textContent), $w('searchMsg').textContent);
}

section('失败诊断：新版后端返回 BACKEND_OUTDATED 包时不重复拼接重启提示')
{
  // 新版后端对「未注册的 /api/settings/* 路径」也会回 404，但它给的是统一响应包，
  // message 里已经写明旧版本与重启办法；前端若再追加一次提示就会出现同一句话两遍。
  const outdatedFetch = async (url) => {
    if (url === '/health/live') return jsonResponse(HEALTH);
    if (url.indexOf('/api/settings/') === 0) {
      return jsonResponse({
        ok: false,
        message: '当前后端服务是旧版本，未注册该接口；请重启服务：python web_test/server.py',
        error_code: 'BACKEND_OUTDATED',
        data: null,
      }, 404);
    }
    throw new Error('未打桩的请求: ' + url);
  };
  const domOutdated = await loadFreshPage(outdatedFetch, FAST_TIMEOUTS);
  const { window: w } = domOutdated;
  const d = w.document;
  const $w = (id) => d.getElementById(id);
  openPanel(w);
  await settle();

  const text = $w('llmMsg').textContent;
  check('BACKEND_OUTDATED 包保留后端原文', /当前后端服务是旧版本/.test(text), text);
  check('BACKEND_OUTDATED 包保留错误码', /BACKEND_OUTDATED/.test(text), text);
  check('BACKEND_OUTDATED 包的重启提示只出现一次',
    text.split('python web_test/server.py').length - 1 === 1, text);
}

section('失败诊断：fetch 永不 settle 时按超时返回（徽标不卡在「读取中…」）')
{
  const hangFetch = async (url) => {
    if (url === '/health/live') return jsonResponse(HEALTH);
    return new Promise(() => {}); // 永不 settle：模拟连接被吞掉 / 进程卡住
  };
  const domHang = await loadFreshPage(hangFetch, FAST_TIMEOUTS);
  const { window: w } = domHang;
  const d = w.document;
  const $w = (id) => d.getElementById(id);
  openPanel(w);
  await new Promise((r) => setTimeout(r, 300)); // > 注入的 100ms 快照超时

  const msg = $w('llmMsg');
  check('超时后 #llmMsg 可见且为 error 样式', msg.hidden === false && msg.className.includes('error'), msg.className);
  check('超时后 #llmMsg 含「超时」', /超时/.test(msg.textContent), msg.textContent);
  check('超时后 #llmMsg 带 TIMEOUT 错误码', /TIMEOUT/.test(msg.textContent), msg.textContent);
  check('超时文案用的是生效的超时值（注入的 0.1 秒）', /0\.1 秒/.test(msg.textContent), msg.textContent);
  check('超时文案提示检查服务进程', /服务进程/.test(msg.textContent), msg.textContent);
  check('超时后 #searchBadgeText 不是「读取中…」',
    $w('searchBadgeText').textContent !== '读取中…', $w('searchBadgeText').textContent);
  check('超时后搜索徽标落到「读取失败」', $w('searchBadgeText').textContent === '读取失败', $w('searchBadgeText').textContent);
  check('超时后模型徽标落到「读取失败」', $w('llmBadgeText').textContent === '读取失败', $w('llmBadgeText').textContent);
}

section('失败诊断：fetch 以 AbortError 失败也算超时')
{
  const abortFetch = async (url) => {
    if (url === '/health/live') return jsonResponse(HEALTH);
    const err = new Error('The operation was aborted.');
    err.name = 'AbortError';
    throw err;
  };
  const domAbort = await loadFreshPage(abortFetch, FAST_TIMEOUTS);
  const { window: w } = domAbort;
  const d = w.document;
  const $w = (id) => d.getElementById(id);
  openPanel(w);
  await settle();

  const msg = $w('llmMsg');
  check('AbortError 时 #llmMsg 含「超时」', /超时/.test(msg.textContent), msg.textContent);
  check('AbortError 时 #llmMsg 带 TIMEOUT 错误码', /TIMEOUT/.test(msg.textContent), msg.textContent);
  check('AbortError 不被误报为「无法连接后端服务」', !/无法连接后端服务/.test(msg.textContent), msg.textContent);
  check('AbortError 时搜索徽标不再是「读取中…」',
    $w('searchBadgeText').textContent !== '读取中…', $w('searchBadgeText').textContent);
}

section('失败诊断：后端完全不可用时顶栏徽标不卡在「读取中…」')
{
  const deadFetch = async () => { throw new Error('模拟网络中断'); };
  const domDead = await loadFreshPage(deadFetch, FAST_TIMEOUTS);
  await new Promise((r) => setTimeout(r, 80));
  const { window: w } = domDead;
  const badgeText = w.document.getElementById('modeBadgeText').textContent;
  check('顶栏徽标不再是「读取中…」', badgeText !== '读取中…', badgeText);
  check('顶栏徽标显示「后端不可用」', badgeText === '后端不可用', badgeText);
}

section('页面加载即识别旧后端（/health/live 无 api_version）')
{
  // 旧进程的真实响应：只有 status，没有 api_version（因此 /api/settings/* 全是 404）
  const oldBackendFetch = async (url) => {
    if (url === '/health/live') return jsonResponse({ status: 'ok' });
    if (url.indexOf('/api/settings/') === 0) return jsonResponse({ detail: 'Not Found' }, 404);
    throw new Error('未打桩的请求: ' + url);
  };
  alertCalls.count = 0;
  const domOld = await loadFreshPage(oldBackendFetch, FAST_TIMEOUTS);
  await new Promise((r) => setTimeout(r, 80)); // 等页面加载时的 /health/live 回来
  const { window: w } = domOld;
  const d = w.document;
  const banner = d.getElementById('staleBackendBanner');

  check('页面里存在警示条元素', !!banner);
  check('旧后端（无 api_version）时警示条可见', !!banner && banner.hidden === false);
  check('警示条文案含「旧版本」', !!banner && /旧版本/.test(banner.textContent), banner && banner.textContent);
  check('警示条给出可执行命令 python web_test/server.py',
    !!banner && banner.textContent.includes('python web_test/server.py'), banner && banner.textContent);
  check('警示条提到重启脚本 web_test\\restart_server.ps1',
    !!banner && banner.textContent.includes('web_test\\restart_server.ps1'), banner && banner.textContent);
  check('警示条不依赖 alert()', alertCalls.count === 0, alertCalls.count);

  // 页面其它行为不受影响：面板照常能打开、聊天区仍在
  openPanel(w);
  await settle();
  check('旧后端下仍能打开设置面板', d.getElementById('settingsOverlay').hidden === false);
  check('旧后端下面板内给出 404 + 重启提示',
    /404/.test(d.getElementById('llmMsg').textContent) &&
    d.getElementById('llmMsg').textContent.includes('python web_test/server.py'),
    d.getElementById('llmMsg').textContent);
}

section('后端为新版（/health/live 带 api_version）时警示条保持隐藏')
{
  const domNew = await loadFreshPage(healthyFetch, FAST_TIMEOUTS);
  await new Promise((r) => setTimeout(r, 80));
  const { window: w } = domNew;
  const d = w.document;
  const banner = d.getElementById('staleBackendBanner');
  check('新版后端下警示条隐藏', !!banner && banner.hidden === true, banner && banner.hidden);
  check('新版后端下顶栏徽标正常显示模式', /模型模式/.test(d.getElementById('modeBadgeText').textContent),
    d.getElementById('modeBadgeText').textContent);
  openPanel(w);
  await settle();
  check('打开面板后警示条仍隐藏', d.getElementById('staleBackendBanner').hidden === true);
  check('新版后端下面板无错误提示', d.getElementById('llmMsg').hidden === true, d.getElementById('llmMsg').textContent);
}

section('服务重启为新版后警示条自动消失')
{
  let apiVersion = null; // null = 旧后端
  const flipFetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    if (url === '/health/live') {
      return jsonResponse(apiVersion ? Object.assign({}, HEALTH, { api_version: apiVersion }) : { status: 'ok' });
    }
    if (url === '/api/settings/llm' && method === 'DELETE') {
      return jsonResponse({ ok: true, message: '已清除运行时模型配置，当前为 Mock 离线模式', error_code: null, data: LLM_SNAPSHOT_MOCK });
    }
    if (url.indexOf('/api/settings/') === 0) return jsonResponse({ detail: 'Not Found' }, 404);
    throw new Error('未打桩的请求: ' + method + ' ' + url);
  };
  const domFlip = await loadFreshPage(flipFetch, FAST_TIMEOUTS);
  const { window: w } = domFlip;
  const d = w.document;
  await new Promise((r) => setTimeout(r, 80));
  check('重启前警示条可见', d.getElementById('staleBackendBanner').hidden === false);

  apiVersion = 'test'; // 用户重启了服务
  openPanel(w);
  await settle();
  d.getElementById('llmClear').dispatchEvent(new w.Event('click', { bubbles: true }));
  await settle();
  check('服务重启为新版后警示条自动隐藏', d.getElementById('staleBackendBanner').hidden === true);
}

// ---------------------------------------------------------------- 汇总

console.log(`\n${checks - failures}/${checks} 项通过`)
if (failures > 0) {
  console.log(`${failures} 项失败`)
  process.exit(1)
}
console.log('全部通过')
