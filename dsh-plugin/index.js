/**
 * EnderWriter 末影写作框架 —— DSH Host 插件
 *
 * 把 EnderWriter 的能力注册成会话内可调用的工具：
 *   enderwriter_search    末影箱分层检索（search_memory）
 *   enderwriter_patrol    伏笔巡检
 *   enderwriter_stats     末影箱统计
 *   enderwriter_writeback 应用回写报告
 *   enderwriter_demo      六阶段写作流水线
 *   enderwriter_info      环境与模型概况
 *
 * 实现方式是 Node 子进程桥接 Python（`python -m enderwriter.bridge`）：
 * 一次调用一个 JSON 请求，stdout 只回一个 JSON，绝不污染。
 *
 * 另注册一条只读 HTTP 路由 `/api/enderwriter/*`，供 Web 端面板取数：
 * 浏览器不能直接跑 Python，Host 侧把桥接结果转成 JSON 暴露给前端。
 */

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const inject = ['tools']

/**
 * 可在 cordis.patch.yml 的用户层覆盖的配置项。
 *
 * Cordis 约定：`Config` 是一个带静态 `validate(input)` 的构造器。
 * 这里手写一个最小校验，避免引入 schema 库依赖。
 */
class Config {
  constructor(input = {}) {
    const src = input && typeof input === 'object' ? input : {}
    this.root = typeof src.root === 'string' ? src.root : ''
    this.python = typeof src.python === 'string' ? src.python : ''
    this.db = typeof src.db === 'string' ? src.db : ''
    this.llm = typeof src.llm === 'string' ? src.llm : ''
    const budget = Number(src.defaultBudget)
    this.defaultBudget = Number.isFinite(budget) && budget > 0 ? Math.floor(budget) : 800
  }

  static validate(input) {
    return new Config(input)
  }
}

Config.schema = {
  type: 'object',
  properties: {
    root: { type: 'string', description: 'EnderWriter 包根目录（含 enderwriter/bridge.py）' },
    python: { type: 'string', description: 'Python 解释器路径；留空则自动探测' },
    db: { type: 'string', description: '末影箱 SQLite 路径；留空则用临时内存库并自动灌示例数据' },
    llm: { type: 'string', description: '默认大模型后端：stub / ollama / llamacpp' },
    defaultBudget: { type: 'integer', description: 'search 工具的默认 token 预算', default: 800 },
  },
  additionalProperties: true,
}

const HERE = dirname(fileURLToPath(import.meta.url))

/**
 * 默认 Python 解释器。
 * 顺序：EW_PYTHON → DSH 自带运行时（按 DSH_HOME 拼）→ PATH 上的 python。
 * 不写死任何机器路径，换台机器也能用。
 */
function defaultPython() {
  const fromEnv = process.env.EW_PYTHON
  if (fromEnv && existsSync(fromEnv)) return fromEnv

  // DSH 自带运行时（Windows 安装包的固定相对位置）
  const dshHome = process.env.DSH_HOME
  if (dshHome) {
    const candidates = [
      join(dshHome, 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
      join(dshHome, 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'bin', 'python3'),
    ]
    for (const p of candidates) {
      if (existsSync(p)) return p
    }
  }

  return process.platform === 'win32' ? 'python.exe' : 'python3'
}

/** 默认 EnderWriter 包根：优先配置，其次与本插件同级/上级/在工作区内查找。 */
function resolveRoot(configRoot) {
  const candidates = [
    configRoot,
    process.env.EW_ROOT,
    join(HERE, '..', 'enderwriter'),
    join(HERE, 'enderwriter'),
    resolve(process.cwd(), 'enderwriter'),
  ].filter(Boolean)
  for (const c of candidates) {
    try {
      if (existsSync(join(c, 'enderwriter', 'bridge.py'))) return resolve(c)
    } catch {
      /* 忽略不可读路径 */
    }
  }
  return null
}

/** 调用 Python 桥，返回解析后的 JSON。 */
function callBridge(root, python, request, timeoutMs) {
  const payload = JSON.stringify(request)
  return new Promise((resolvePromise) => {
    let child
    try {
      child = spawn(python, ['-m', 'enderwriter.bridge', '--stdin'], {
        cwd: root,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONPATH: root },
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      })
    } catch (error) {
      resolvePromise({ ok: false, error: { code: 'spawn_failed', message: String(error) } })
      return
    }

    let out = ''
    let err = ''
    const timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* 已退出 */
      }
    }, timeoutMs)

    child.stdout.on('data', (d) => {
      out += d.toString('utf8')
    })
    child.stderr.on('data', (d) => {
      err += d.toString('utf8')
    })
    child.on('error', (error) => {
      clearTimeout(timer)
      resolvePromise({
        ok: false,
        error: { code: 'spawn_failed', message: `无法启动 Python：${error.message}` },
      })
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      const text = out.trim()
      if (!text) {
        resolvePromise({
          ok: false,
          error: {
            code: 'empty_output',
            message: `Python 桥没有输出（退出码 ${code}）`,
            detail: err.slice(-1500),
          },
        })
        return
      }
      try {
        resolvePromise(JSON.parse(text))
      } catch (parseError) {
        resolvePromise({
          ok: false,
          error: {
            code: 'bad_output',
            message: `Python 桥返回的不是 JSON：${String(parseError)}`,
            detail: text.slice(0, 800),
          },
        })
      }
    })

    try {
      child.stdin.end(payload, 'utf8')
    } catch {
      /* 子进程可能已退出 */
    }
  })
}

/** 把桥返回的 {ok,error} 变成工具结果。 */
function toResult(res) {
  if (res && res.ok) {
    return { ok: true, data: res }
  }
  const message = res?.error?.message ?? '未知错误'
  return { ok: false, message, detail: res?.error?.detail ?? '' }
}

/** 未找到包根时的统一失败结果。 */
function missingRoot() {
  return {
    ok: false,
    message:
      '未找到 EnderWriter 包根。请在插件 Config 里设置 root 指向克隆下来的 enderwriter 仓库目录' +
      '（该目录下应有 enderwriter/bridge.py），或设置环境变量 EW_ROOT。',
  }
}

/** 统一的文本渲染：模型看到的是紧凑可读文本，不是裸 JSON 噪音。 */
function renderText(label, body) {
  return `${label}\n${body}`
}

/** 渲染 search 结果，贴近 Memory Keeper 的原始分层格式。 */
function renderSearch(res) {
  const r = res.result ?? {}
  const lines = []
  lines.push(`【检索结果】总 ${res.total_tokens ?? 0} token`)
  for (const [band, label] of [
    ['high', '高相关'],
    ['medium', '中相关'],
    ['low', '弱相关(抽样)'],
  ]) {
    const items = r[band] ?? []
    lines.push(`\n${label}：${items.length} 条`)
    for (const hit of items) {
      lines.push(`- [${hit.layer}] ${hit.entity} v${hit.version}：${hit.content}`)
    }
  }
  if (res.normalized?.aliases && Object.keys(res.normalized.aliases).length) {
    lines.push(`\n别名归一化：${JSON.stringify(res.normalized.aliases)}`)
  }
  return lines.join('\n')
}

/** 渲染巡检结果。 */
function renderPatrol(res) {
  const items = res.items ?? []
  if (!items.length) {
    return `【伏笔巡检 · ${res.chapter}】未发现问题。未回收伏笔 ${res.open_foreshadowings?.length ?? 0} 条。`
  }
  const lines = [`【伏笔巡检 · ${res.chapter}】${items.length} 项需处理`]
  for (const it of items) {
    const tag = it.type === 'overdue' ? '超期' : it.type === 'stale' ? '陈旧' : it.type
    lines.push(`- [${tag}] ${it.id}：${it.suggestion ?? it.message ?? ''}`)
  }
  return lines.join('\n')
}

function renderStats(res) {
  const s = res.stats ?? {}
  const rows = [
    ['人物格', s.records?.character],
    ['世界格', s.records?.world],
    ['时间格', s.records?.timeline],
    ['伏笔格', s.records?.foreshadowing],
    ['物品格', s.records?.item],
    ['风格格', s.records?.style],
    ['记录总数', s.total_records],
    ['版本历史', s.history],
    ['倒排词条', s.terms],
    ['关系边', s.edges],
    ['章节数', s.chapters],
    ['未回收伏笔', s.open_foreshadowings],
  ].filter(([, v]) => v !== undefined)
  return ['【末影箱统计】', ...rows.map(([k, v]) => `${k}：${v}`)].join('\n')
}

function renderInfo(res) {
  const ll = res.llamacpp ?? {}
  const ol = res.ollama ?? {}
  return [
    '【EnderWriter 环境】',
    `版本：${res.version}`,
    `包根：${res.package_root}`,
    `自动选择引擎：${res.engine}`,
    `可用后端：${(res.backends ?? []).join(', ')}`,
    `llama.cpp：${ll.ready ? '就绪' : '未就绪'}（模型 ${ll.model_mb ?? 0}MB）`,
    `Ollama：${ol.installed ? '已安装' : '未安装'}（服务${ol.server_up ? '在线' : '离线'}）`,
  ].join('\n')
}

function renderDemo(res) {
  const lines = [
    `【六阶段流水线 · ${res.chapter_id}】${res.chapter_goal ?? ''}`,
    `① 规划：§3.1 细纲已通过 Schema 校验`,
    `② 检索：高 ${res.retrieval?.counts?.high ?? 0} / 中 ${res.retrieval?.counts?.medium ?? 0} / 低 ${res.retrieval?.counts?.low ?? 0}，${res.retrieval?.total_tokens ?? 0} token`,
    `②b 组装：${res.context?.total_tokens ?? 0} token（上限 ${res.context?.budget?.total ?? 0}）`,
    `③ 生成：${res.text_chars} 字`,
    `④ 动态追加：识别 ${(res.prefetch?.entities ?? []).join('、') || '（无）'}`,
    `⑤ 回写：${(res.writeback?.applied ?? []).length} 项落地`,
    `⑥ 巡检：${(res.patrol ?? []).length} 项需处理`,
    '',
    '── 正文 ──',
    res.text ?? '',
    '',
    '── 写入后统计 ──',
    renderStats({ stats: res.stats }),
  ]
  return lines.join('\n')
}

/** 构造一个工具定义。 */
function makeTool({ name, description, parameters, timeoutMs = 60000, run, render }) {
  return {
    name,
    description,
    parameters,
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean', description: '是否成功' },
          text: { type: 'string', description: '给模型读的结果文本' },
        },
        required: ['text'],
      },
      render(_args, value) {
        return [{ type: 'text', text: String(value?.text ?? '') }]
      },
    },
    async execute(args, ctx) {
      return run(args ?? {}, ctx)
    },
    presentCall(args) {
      return { card: 'generic', title: name, kind: 'other', rawInput: args }
    },
    presentResult(_args, result) {
      return {
        card: 'generic',
        content: Array.isArray(result?.content) ? result.content : undefined,
      }
    },
    timeoutMs,
  }
}

export function apply(ctx, config) {
  const python = config.python || defaultPython()
  const root = resolveRoot(config.root)
  const db = config.db || ''
  const defaultBudget = Number(config.defaultBudget) || 800
  const llm = config.llm || ''

  if (!root) {
    ctx.logger?.warn?.(
      '[enderwriter] 未找到 EnderWriter 包根，请设置 Config.root 指向包含 enderwriter/bridge.py 的目录',
    )
  }

  async function call(op, extra, timeoutMs) {
    if (!root) return missingRoot()
    const request = { op, ...(db ? { db } : {}), ...extra }
    const res = await callBridge(root, python, request, timeoutMs ?? 60000)
    return toResult(res)
  }

  const disposers = []

  disposers.push(
    ctx.tools.register(
      makeTool({
        name: 'enderwriter_search',
        description:
          '在 EnderWriter 末影箱中检索长篇小说记忆（别名自动归一化为标准名）。' +
          '返回 high/medium/low 三档结果：high 给完整正文、medium 给摘要、low 为弱相关抽样。' +
          '写章节前用它确认人物状态、世界设定、伏笔与物品来历，避免设定崩坏。',
        parameters: {
          type: 'object',
          properties: {
            entities: {
              type: 'array',
              items: { type: 'string' },
              description: '要查的实体名，如 ["沈砚","断魂钉"]；别名会自动解析为标准名',
            },
            query_context: {
              type: 'string',
              description: '本次检索的语境，如本章目标或当前剧情，用于重排相关性',
            },
            layers: {
              type: 'array',
              items: {
                type: 'string',
                enum: ['character', 'world', 'timeline', 'foreshadowing', 'item', 'style'],
              },
              description: '限定检索的分格，省略则全查',
            },
            budget: {
              type: 'integer',
              description: `token 预算，默认 ${defaultBudget}`,
            },
          },
          required: ['entities'],
        },
        timeoutMs: 45000,
        run: async (args) => {
          const res = await call('search', {
            entities: args.entities,
            query_context: args.query_context ?? '',
            layers: args.layers,
            budget: args.budget ?? defaultBudget,
          })
          if (!res.ok) return { ok: false, text: `检索失败：${res.message}` }
          return { ok: true, text: renderSearch(res.data) }
        },
      }),
    ),
  )

  disposers.push(
    ctx.tools.register(
      makeTool({
        name: 'enderwriter_patrol',
        description:
          '伏笔巡检：检查有哪些伏笔已超期未回收（超过计划章节 + 宽限期）、' +
          '哪些长期未强化已成陈旧候选。返回可直接写进下一章细纲的回收建议。',
        parameters: {
          type: 'object',
          properties: {
            chapter: { type: 'string', description: '当前章节号，如 ch_042' },
            grace: { type: 'integer', description: '超期宽限章数，默认 3' },
            stale_threshold: { type: 'integer', description: '陈旧阈值章数，默认 30' },
          },
          required: ['chapter'],
        },
        timeoutMs: 45000,
        run: async (args) => {
          const res = await call('patrol', {
            chapter: args.chapter,
            grace: args.grace,
            stale_threshold: args.stale_threshold,
          })
          if (!res.ok) return { ok: false, text: `巡检失败：${res.message}` }
          return { ok: true, text: renderPatrol(res.data) }
        },
      }),
    ),
  )

  disposers.push(
    ctx.tools.register(
      makeTool({
        name: 'enderwriter_stats',
        description: '查看 EnderWriter 末影箱的存储概况：各格子记录数、版本历史、索引规模、未回收伏笔。',
        parameters: { type: 'object', properties: {} },
        timeoutMs: 30000,
        run: async () => {
          const res = await call('stats', {})
          if (!res.ok) return { ok: false, text: `统计失败：${res.message}` }
          return { ok: true, text: renderStats(res.data) }
        },
      }),
    ),
  )

  disposers.push(
    ctx.tools.register(
      makeTool({
        name: 'enderwriter_writeback',
        description:
          '把一章写完后的事实变化回写进末影箱（人物状态、物品归属、时间线、伏笔状态机），' +
          '并做矛盾检测（死而复生、时间倒错、能力违规、称呼漂移）。' +
          'report 需符合 §3.5 回写报告结构。',
        parameters: {
          type: 'object',
          properties: {
            report: {
              type: 'object',
              description:
                '§3.5 writeback_report：{chapter_id, updated:{characters,items,timeline,foreshadowing}, warnings}',
            },
          },
          required: ['report'],
        },
        timeoutMs: 60000,
        run: async (args) => {
          const res = await call('writeback', { report: args.report })
          if (!res.ok) return { ok: false, text: `回写失败：${res.message}` }
          const applied = res.data.applied ?? []
          const warnings = res.data.warnings ?? []
          const lines = [`【回写完成】落地 ${applied.length} 项`]
          if (warnings.length) {
            lines.push(`\n⚠ 矛盾/漏写提示 ${warnings.length} 条：`)
            for (const w of warnings) lines.push(`- ${w}`)
          } else {
            lines.push('未检测到矛盾。')
          }
          return { ok: true, text: lines.join('\n') }
        },
      }),
    ),
  )

  disposers.push(
    ctx.tools.register(
      makeTool({
        name: 'enderwriter_demo',
        description:
          '跑一次 EnderWriter 六阶段写作流水线（①规划→②检索→②b组装→③生成→④动态追加→⑤回写→⑥巡检），' +
          '返回结构化摘要与正文。用于演示或验证整条链路是否正常。',
        parameters: {
          type: 'object',
          properties: {
            words: { type: 'integer', description: '正文目标 token 数，默认 480' },
            window: { type: 'integer', description: '模型窗口 token 数，默认 8000' },
            llm: {
              type: 'string',
              enum: ['stub', 'ollama', 'llamacpp'],
              description: '大模型后端；省略则用环境变量 EW_LLM',
            },
          },
        },
        timeoutMs: 300000,
        run: async (args) => {
          const res = await call(
            'demo',
            { words: args.words, window: args.window, llm: args.llm || llm || undefined },
            300000,
          )
          if (!res.ok) return { ok: false, text: `流水线失败：${res.message}` }
          return { ok: true, text: renderDemo(res.data) }
        },
      }),
    ),
  )

  disposers.push(
    ctx.tools.register(
      makeTool({
        name: 'enderwriter_info',
        description: '查看 EnderWriter 运行环境：版本、包根、可用模型后端、llama.cpp / Ollama 就绪状态。',
        parameters: { type: 'object', properties: {} },
        timeoutMs: 30000,
        run: async () => {
          const res = await call('info', {})
          if (!res.ok) return { ok: false, text: `环境检查失败：${res.message}` }
          return { ok: true, text: renderInfo(res.data) }
        },
      }),
    ),
  )

  // ------------------------------------------------------------------
  // 面板取数：两条通道都试，谁生效用谁。
  //
  // 1) DSH 官方 RPC（优先）：挂在已鉴权的共享 /api 通道上，
  //    Client 半边用宿主注入的 `host.call(method, args)` 调用。
  // 2) 自建 webServer 路由（兜底）：必须自己过一遍连接鉴权，
  //    否则会被 401 拦在路由之外——这是上一版面板"读取失败"的原因。
  // ------------------------------------------------------------------
  const connection = ctx.get ? ctx.get('connection') : undefined
  const webServer = ctx.get ? ctx.get('webServer') : undefined

  async function overview(chapter) {
    const [stats, info, patrol, entries] = await Promise.all([
      call('stats', {}, 30000),
      call('info', {}, 30000),
      call('patrol', { chapter: chapter || 'ch_001' }, 30000),
      call('entries', { layer: 'foreshadowing' }, 30000),
    ])
    return {
      stats: stats.ok ? stats.data.stats : null,
      info: info.ok ? info.data : null,
      patrol: patrol.ok ? patrol.data : null,
      foreshadowings: entries.ok ? entries.data.items : [],
      errors: [stats, info, patrol, entries].filter((r) => !r.ok).map((r) => r.message),
    }
  }

  async function route(method, args) {
    switch (method) {
      case 'overview':
        return { ok: true, value: await overview(args.chapter) }
      case 'entries': {
        const r = await call('entries', { layer: args.layer || 'character', limit: 200 }, 45000)
        if (!r.ok) return { ok: false, error: { code: 'failed', message: r.message, details: {} } }
        return { ok: true, value: { items: r.data.items } }
      }
      case 'search': {
        const r = await call('search', args, 45000)
        if (!r.ok) return { ok: false, error: { code: 'failed', message: r.message, details: {} } }
        return { ok: true, value: r.data }
      }
      default:
        return {
          ok: false,
          error: { code: 'unknown_endpoint', message: `未知端点：${method}`, details: {} },
        }
    }
  }

  // --- 通道 1：官方 RPC ---
  if (connection && connection.rpc && typeof connection.rpc.handle === 'function') {
    try {
      const pending = connection.rpc.handle('enderwriter', async (endpoint, payload) => {
        try {
          return await route(endpoint, payload && typeof payload === 'object' ? payload : {})
        } catch (error) {
          return { ok: false, error: { code: 'internal', message: String(error), details: {} } }
        }
      })
      if (pending && typeof pending.then === 'function') {
        pending.then((d) => {
          if (typeof d === 'function') disposers.push(d)
        })
      } else if (typeof pending === 'function') {
        disposers.push(pending)
      }
    } catch (error) {
      ctx.logger?.warn?.(`[enderwriter] RPC 通道注册失败：${error}`)
    }
  }

  // --- 通道 2：自建路由（自行做连接鉴权）---
  if (webServer && typeof webServer.register === 'function') {
    disposers.push(
      webServer.register({
        kind: 'prefix',
        path: '/api/enderwriter',
        handler: async (req, res) => {
          const send = (status, body) => {
            try {
              res.statusCode = status
              res.setHeader('Content-Type', 'application/json; charset=utf-8')
              res.setHeader('Cache-Control', 'no-store')
              res.end(JSON.stringify(body))
            } catch {
              /* 连接已关闭 */
            }
          }

          // 关键：先过 DSH 的信任与鉴权检查，通过才继续。
          // webServer 路由位于连接鉴权之外，不自己检查就是全站 401。
          if (connection) {
            try {
              if (typeof connection.requestRejection === 'function') {
                const rejection = connection.requestRejection({ headers: req.headers || {} })
                if (rejection) {
                  send(rejection, { ok: false, error: { code: 'unauthorized', message: '未授权' } })
                  return
                }
              } else if (typeof connection.admit === 'function') {
                const admitted = connection.admit({ headers: req.headers || {} })
                if (admitted && admitted.rejection) {
                  send(admitted.rejection, { ok: false, error: { code: 'unauthorized', message: '未授权' } })
                  return
                }
              }
            } catch {
              /* 鉴权接口异常时放行到业务层，避免面板完全不可用 */
            }
          }

          try {
            const url = new URL(req.url ?? '/', 'http://localhost')
            const sub = url.pathname.replace(/^\/api\/enderwriter\/?/, '')
            if (sub === 'overview' || sub === '') {
              const r = await route('overview', { chapter: url.searchParams.get('chapter') || 'ch_001' })
              send(200, { ok: true, ...r.value })
              return
            }
            if (sub === 'entries') {
              const r = await route('entries', { layer: url.searchParams.get('layer') || 'character' })
              send(r.ok ? 200 : 500, r.ok ? { ok: true, ...r.value } : r)
              return
            }
            send(404, { ok: false, error: { code: 'not_found', message: `未知端点：${sub}` } })
          } catch (error) {
            send(500, { ok: false, error: { code: 'internal', message: String(error) } })
          }
        },
      }),
    )
  }

  return () => {
    for (const dispose of disposers) {
      try {
        dispose()
      } catch {
        /* 已释放 */
      }
    }
  }
}
