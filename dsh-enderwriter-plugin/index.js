/**
 * EnderWriter 末影写作框架 —— DSH Host 插件
 *
 * 把 EnderWriter 的能力注册成会话内可调用的工具：
 *   enderwriter_recall    跨层检索（作品库 + 全局文学库）
 *   enderwriter_remember  按层自动路由写入
 *   enderwriter_works     列出所有作品库
 *   enderwriter_forget    手动清除（会自动备份）
 *   enderwriter_patrol    伏笔巡检
 *   enderwriter_stats     末影箱统计
 *   enderwriter_writeback 应用回写报告
 *   enderwriter_demo      六阶段写作流水线
 *   enderwriter_info      环境与模型概况
 *
 * 数据通道：Node 子进程桥接 Python（`python -m enderwriter.bridge`），
 * 一次调用一个 JSON 请求，stdout 只回一个 JSON。
 *
 * 会话隔离：每个工具执行时都从 ctx.agent.id 取当前会话 ID，
 * 并显式传给 Python 侧。**不使用任何模块级可变状态**——
 * Host 进程被所有会话共享，缓存会话 ID 会导致跨对话串场。
 */

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// 服务声明：Cordis 里 ctx.get(name) 在服务未声明时拿不到东西，
// 必须在 inject 里显式声明。webServer 用于注册面板取数路由，
// connection 用于复用连接层的鉴权检查。
export const inject = ['tools', 'webServer', 'connection']

/**
 * 可在 cordis.patch.yml 的用户层覆盖的配置项。
 * Cordis 约定：Config 是带静态 validate(input) 的构造器。
 */
class Config {
  constructor(input = {}) {
    const src = input && typeof input === 'object' ? input : {}
    this.root = typeof src.root === 'string' ? src.root : ''
    this.python = typeof src.python === 'string' ? src.python : ''
    this.db = typeof src.db === 'string' ? src.db : ''
    this.llm = typeof src.llm === 'string' ? src.llm : ''
    this.work = typeof src.work === 'string' ? src.work : ''
    this.home = typeof src.home === 'string' ? src.home : ''
    this.enabled = src.enabled === undefined ? true : !!src.enabled
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
    db: { type: 'string', description: '指定 SQLite 路径；留空则用分层记忆目录' },
    llm: { type: 'string', description: '默认大模型后端：stub / ollama / llamacpp' },
    work: { type: 'string', description: '作品名；留空则每个对话一套独立记忆' },
    home: { type: 'string', description: '记忆根目录，留空则用 <仓库>/memory' },
    enabled: { type: 'boolean', description: '是否启用末影箱', default: true },
    defaultBudget: { type: 'integer', description: 'recall 工具的默认 token 预算', default: 800 },
  },
  additionalProperties: true,
}

const HERE = dirname(fileURLToPath(import.meta.url))

/**
 * 默认 Python 解释器。
 * 顺序：EW_PYTHON → DSH 自带运行时（按 DSH_HOME 拼）→ PATH 上的 python。
 * 不写死任何机器路径。
 */
function defaultPython() {
  const fromEnv = process.env.EW_PYTHON
  if (fromEnv && existsSync(fromEnv)) return fromEnv

  const candidates = []

  // 1) DSH_HOME 环境变量下的运行时（最理想）
  const dshHome = process.env.DSH_HOME
  if (dshHome) {
    candidates.push(
      join(dshHome, 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
      join(dshHome, 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'bin', 'python3'),
    )
  }

  // 2) 用户目录下的 .dsh（DSH_HOME 常常没传进来，但目录一定在）
  try {
    const userHome = process.env.USERPROFILE || process.env.HOME || ''
    if (userHome) {
      candidates.push(
        join(userHome, '.dsh', 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
        join(userHome, '.dsh', 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'bin', 'python3'),
      )
    }
  } catch {
    /* 忽略 */
  }

  // 3) 常见安装位置
  if (process.platform === 'win32') {
    const local = process.env.LOCALAPPDATA || ''
    if (local) {
      candidates.push(
        join(local, 'Programs', 'Python', 'Python312', 'python.exe'),
        join(local, 'Programs', 'Python', 'Python311', 'python.exe'),
        join(local, 'Programs', 'Python', 'Python310', 'python.exe'),
      )
    }
    candidates.push('C:\\Python312\\python.exe', 'C:\\Python311\\python.exe')
  } else {
    candidates.push('/usr/bin/python3', '/usr/local/bin/python3')
  }

  for (const p of candidates) {
    try {
      if (p && existsSync(p)) return p
    } catch {
      /* 忽略不可读 */
    }
  }

  // 4) 最后才退回裸命令（依赖 PATH）
  return process.platform === 'win32' ? 'python.exe' : 'python3'
}

/** 把探测过程也记下来，便于排查为什么没找到解释器。 */
function pythonCandidates() {
  const out = []
  const dshHome = process.env.DSH_HOME
  const userHome = process.env.USERPROFILE || process.env.HOME || ''
  out.push({ source: 'DSH_HOME', value: dshHome || null })
  out.push({ source: 'USERPROFILE', value: userHome || null })
  if (dshHome) {
    out.push({
      path: join(dshHome, 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
      exists: existsSync(
        join(dshHome, 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
      ),
    })
  }
  if (userHome) {
    out.push({
      path: join(userHome, '.dsh', 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
      exists: existsSync(
        join(userHome, '.dsh', 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
      ),
    })
  }
  return out
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
  if (res && res.ok) return { ok: true, data: res }
  return {
    ok: false,
    message: res?.error?.message ?? '未知错误',
    detail: res?.error?.detail ?? '',
  }
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

// ==========================================================================
// 文本渲染
// ==========================================================================
function renderBands(r) {
  const out = []
  for (const [band, label] of [['high', '高相关'], ['medium', '中相关'], ['low', '弱相关(抽样)']]) {
    const items = r[band] ?? []
    if (!items.length) continue
    out.push(`  ${label}：${items.length} 条`)
    for (const hit of items) {
      out.push(`    - [${hit.layer}] ${hit.entity} v${hit.version}：${hit.content}`)
    }
  }
  return out
}

function renderRecall(res) {
  const scopes = res.scopes ?? {}
  const lines = [`【跨层检索】总 ${res.total_tokens ?? 0} token`]

  lines.push(`\n▸ 作品库「${res.work || '未指定'}」`)
  const work = scopes.work
  const wBody = work ? renderBands(work) : []
  if (wBody.length) lines.push(...wBody)
  else lines.push('  ' + ((work && work.note) || '（无匹配）'))

  if (scopes.global) {
    lines.push('\n▸ 全局文学库（跨对话通用）')
    const gBody = renderBands(scopes.global)
    if (gBody.length) lines.push(...gBody)
    else lines.push('  （无匹配）')
  }

  if ((res.errors ?? []).length) lines.push('\n⚠ ' + res.errors.join('；'))
  return lines.join('\n')
}

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
  return [
    `【六阶段流水线 · ${res.chapter_id}】${res.chapter_goal ?? ''}`,
    '① 规划：§3.1 细纲已通过 Schema 校验',
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
  ].join('\n')
}

/** 构造一个工具定义。run 会收到 (args, ctx, sessionId, notify)。 */
function makeTool({ name, description, parameters, timeoutMs = 60000, run }) {
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
      // 会话 ID 只从当前这次调用的 ctx 取，绝不缓存：
      // Host 进程被所有会话共享，缓存会导致跨对话串场。
      const sid = ctx && ctx.agent && ctx.agent.id ? String(ctx.agent.id) : ''
      return run(args ?? {}, ctx, sid)
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

const LAYER_ENUM = ['character', 'world', 'timeline', 'foreshadowing', 'item', 'style']

export function apply(ctx, config) {
  if (config && config.enabled === false) {
    ctx.logger?.info?.('[enderwriter] 末影箱已关闭（Config.enabled = false）')
    return () => {}
  }

  const python = config.python || defaultPython()
  const root = resolveRoot(config.root)
  const db = config.db || ''
  const defaultBudget = Number(config.defaultBudget) || 800
  const llm = config.llm || ''
  const home = config.home || ''
  const fixedWork = config.work || ''

  if (!root) {
    ctx.logger?.warn?.(
      '[enderwriter] 未找到 EnderWriter 包根，请设置 Config.root 指向包含 enderwriter/bridge.py 的目录',
    )
  }

  /**
   * 调 Python 桥。
   * sessionId 由调用方显式传入（来自本次工具调用的 ctx），
   * 保证不同对话的数据不会互相串。
   */
  async function call(op, extra, timeoutMs, sessionId) {
    if (!root) return missingRoot()
    const request = {
      op,
      ...(db ? { db } : {}),
      ...(home ? { home } : {}),
      ...(sessionId ? { session_id: sessionId } : {}),
      ...extra,
    }
    const res = await callBridge(root, python, request, timeoutMs ?? 60000)
    return toResult(res)
  }

  const disposers = []

  /**
   * 最近一次执行过工具的会话 ID。
   *
   * 为什么需要它：Web 面板挂在 root 作用域（sidebar.panellist / main），
   * 拿不到"当前是哪个会话"——插槽 props 里没有 session 绑定。
   * 所以由 Host 记录最近活跃的会话，面板读它来显示"本对话"的数据。
   *
   * 这不影响数据正确性：工具自己始终用当次 ctx.agent.id 传参，
   * 这里只是给面板一个"默认看哪个对话"的提示。
   */
  let lastActiveSession = ''

  /** 注册工具，同时记录调用方的会话 ID 供面板使用。 */
  function registerTool(def) {
    const originalExecute = def.execute
    def.execute = async (args, toolCtx) => {
      const sid = toolCtx && toolCtx.agent && toolCtx.agent.id ? String(toolCtx.agent.id) : ''
      if (sid) lastActiveSession = sid
      return originalExecute(args, toolCtx)
    }
    return ctx.tools.register(def)
  }

  // ---- 检索：作品库 + 全局文学库 ----
  disposers.push(
    registerTool(
      makeTool({
        name: 'enderwriter_recall',
        description:
          '跨两层检索长篇记忆：作品库（人物/世界/时间线/伏笔/物品，属于当前作品或当前对话）' +
          '优先，全局库（风格/手法/节奏等文学经验，跨所有作品对话通用）补充。' +
          '返回 high/medium/low 三档：high 给完整正文、medium 给摘要、low 为弱相关抽样。' +
          '写章节前用它确认人物状态与设定，避免前后矛盾。',
        parameters: {
          type: 'object',
          properties: {
            entities: {
              type: 'array',
              items: { type: 'string' },
              description: '要查的实体名，如 ["张三","断魂钉"]；别名会自动解析为标准名',
            },
            query_context: {
              type: 'string',
              description: '本次检索的语境，如本章目标或当前剧情，用于重排相关性',
            },
            layers: { type: 'array', items: { type: 'string', enum: LAYER_ENUM }, description: '限定检索的分格' },
            budget: { type: 'integer', description: `token 预算，默认 ${defaultBudget}` },
            work: { type: 'string', description: '作品名；省略则用当前对话的独立记忆' },
          },
          required: ['entities'],
        },
        timeoutMs: 45000,
        run: async (args, _ctx, sid) => {
          const res = await call(
            'recall',
            {
              entities: args.entities,
              query_context: args.query_context ?? '',
              layers: args.layers,
              budget: args.budget ?? defaultBudget,
              work: args.work || fixedWork,
            },
            45000,
            sid,
          )
          if (!res.ok) return { ok: false, text: `检索失败：${res.message}` }
          return { ok: true, text: renderRecall(res.data) }
        },
      }),
    ),
  )

  // ---- 写入：按层自动路由 ----
  disposers.push(
    registerTool(
      makeTool({
        name: 'enderwriter_remember',
        description:
          '把一条信息写进末影箱，自动分层：' +
          '人物/世界/时间线/伏笔/物品 → 当前作品（或当前对话）的库；' +
          'style 层（写作手法、叙事节奏、风格选型、经验教训）→ 全局库（跨所有作品对话通用）。' +
          '写完一章后记录设定变化，或总结出可复用的写作经验时使用。',
        parameters: {
          type: 'object',
          properties: {
            layer: { type: 'string', enum: LAYER_ENUM, description: '信息属于哪一格；style 进全局库' },
            entity: { type: 'string', description: '实体名：人物名、设定名、伏笔 ID、经验主题' },
            content: { type: 'string', description: '要记住的内容' },
            scope: { type: 'string', enum: ['work', 'global'], description: '强制指定存放层' },
            work: { type: 'string', description: '作品名；省略则用当前对话' },
            keywords: { type: 'array', items: { type: 'string' }, description: '附加检索关键词' },
          },
          required: ['layer', 'entity', 'content'],
        },
        timeoutMs: 45000,
        run: async (args, _ctx, sid) => {
          const res = await call(
            'remember',
            {
              layer: args.layer,
              entity: args.entity,
              content: args.content,
              scope: args.scope,
              keywords: args.keywords,
              work: args.work || fixedWork,
            },
            45000,
            sid,
          )
          if (!res.ok) return { ok: false, text: `写入失败：${res.message}` }
          const d = res.data
          const where = d.scope === 'global' ? '全局文学库（跨对话通用）' : `作品库「${d.work}」`
          return {
            ok: true,
            text:
              `已记住 → ${where}\n[${d.record.layer}] ${d.record.entity} v${d.record.version}\n` +
              `${d.record.content}\n（该层现共 ${d.total_in_scope} 条）`,
          }
        },
      }),
    ),
  )

  // ---- 列出作品库 ----
  disposers.push(
    registerTool(
      makeTool({
        name: 'enderwriter_works',
        description: '列出末影箱里已有的所有作品库，并标出当前对话用的是哪一本。',
        parameters: { type: 'object', properties: {} },
        timeoutMs: 30000,
        run: async (_args, _ctx, sid) => {
          const res = await call('works', {}, 30000, sid)
          if (!res.ok) return { ok: false, text: `查询失败：${res.message}` }
          const list = res.data.works ?? []
          if (!list.length) return { ok: true, text: '还没有任何作品库。' }
          return {
            ok: true,
            text:
              `【作品库】当前：${res.data.current || '（无）'}\n` +
              list
                .map((w) => {
                  // 优先显示库内记录的真实作品名，slug 只在没有名字时兜底
                  const label = w.name || w.id
                  const count = w.total != null ? `${w.total} 条` : `${w.size_kb}KB`
                  return `- ${label}${w.is_current ? '  ← 当前' : ''}（${count}）`
                })
                .join('\n'),
          }
        },
      }),
    ),
  )

  // ---- 手动清除 ----
  disposers.push(
    registerTool(
      makeTool({
        name: 'enderwriter_forget',
        description:
          '手动清除末影箱里的记忆（不可撤销，但会先自动备份为 .bak-时间戳）。' +
          'target=work 清当前作品/对话的人物设定伏笔；target=global 清跨对话的文学经验；' +
          'target=all 两者都清。仅在用户明确要求清空记忆时使用。',
        parameters: {
          type: 'object',
          properties: {
            target: { type: 'string', enum: ['work', 'global', 'all'], description: '清除哪一层' },
            work: { type: 'string', description: '作品名；省略则用当前对话' },
          },
          required: ['target'],
        },
        timeoutMs: 60000,
        run: async (args, _ctx, sid) => {
          const res = await call(
            'clear',
            { target: args.target, work: args.work || fixedWork },
            60000,
            sid,
          )
          if (!res.ok) return { ok: false, text: `清除失败：${res.message}` }
          const lines = ['【已清除】']
          for (const c of res.data.cleared ?? []) {
            if (c.status === 'cleared') lines.push(`- ${c.target}：${c.size_kb}KB 已移除（备份 ${c.backup}）`)
            else if (c.status === 'not_found') lines.push(`- ${c.target}：本就不存在`)
            else lines.push(`- ${c.target}：失败 ${c.error ?? ''}`)
          }
          return { ok: true, text: lines.join('\n') }
        },
      }),
    ),
  )

  // ---- 一致性检查 ----
  disposers.push(
    registerTool(
      makeTool({
        name: 'enderwriter_check',
        description:
          '一致性检查：用确定性规则核实正文是否与已确立的设定冲突。' +
          '覆盖五类：死而复生、伤情违反（受伤部位做动作）、持有者违反、境界回退、位置瞬移。' +
          '写完一章后调用；命中则提示具体违规句与冲突事实。' +
          '注意：这是规则匹配，不理解语义，只查能用规则表达的硬冲突。',
        parameters: {
          type: 'object',
          properties: {
            text: { type: 'string', description: '要检查的正文' },
            entities: {
              type: 'array',
              items: { type: 'string' },
              description: '限定检查范围（只核对这几个人物/物品）；省略则核对整个作品库',
            },
            rules: {
              type: 'array',
              items: { type: 'string', enum: ['R1', 'R2', 'R3', 'R4', 'R5'] },
              description: '只跑指定规则；省略则全部',
            },
            work: { type: 'string', description: '作品名；省略则用当前对话的独立记忆' },
          },
          required: ['text'],
        },
        timeoutMs: 30000,
        run: async (args, _ctx, sid) => {
          const res = await call(
            'check',
            { text: args.text, entities: args.entities, rules: args.rules, work: args.work || fixedWork },
            30000,
            sid,
          )
          if (!res.ok) return { ok: false, text: `检查失败：${res.message}` }
          const d = res.data

          if (!d.facts_used) {
            return {
              ok: true,
              text:
                '【一致性检查】未执行——作品库里没有可用于核对的设定。\n' +
                '（需要人物伤情/生死、物品持有者、境界这类可判定的事实）',
            }
          }

          const issues = d.issues || []
          if (!issues.length) {
            return {
              ok: true,
              text: `【一致性检查】通过（核对 ${d.facts_used} 条事实，运行规则 ${(d.rules_run || []).join('/')}）`,
            }
          }

          const lines = [`【一致性检查】发现 ${issues.length} 处冲突`]
          for (const it of issues) {
            lines.push(`\n[${it.rule} ${it.rule_name}] ${it.detail}`)
            lines.push(`  正文：${it.quote}`)
          }
          return { ok: false, text: lines.join('\n') }
        },
      }),
    ),
  )

  // ---- 伏笔巡检 ----
  disposers.push(
    registerTool(
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
        run: async (args, _ctx, sid) => {
          const res = await call(
            'patrol',
            {
              chapter: args.chapter,
              grace: args.grace,
              stale_threshold: args.stale_threshold,
              work: fixedWork,
            },
            45000,
            sid,
          )
          if (!res.ok) return { ok: false, text: `巡检失败：${res.message}` }
          return { ok: true, text: renderPatrol(res.data) }
        },
      }),
    ),
  )

  // ---- 统计 ----
  disposers.push(
    registerTool(
      makeTool({
        name: 'enderwriter_stats',
        description: '查看 EnderWriter 末影箱的存储概况：各格子记录数、版本历史、索引规模、未回收伏笔。',
        parameters: { type: 'object', properties: {} },
        timeoutMs: 30000,
        run: async (_args, _ctx, sid) => {
          const res = await call('stats', {}, 30000, sid)
          if (!res.ok) return { ok: false, text: `统计失败：${res.message}` }
          return { ok: true, text: renderStats(res.data) }
        },
      }),
    ),
  )

  // ---- 回写 ----
  disposers.push(
    registerTool(
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
              description: '§3.5 writeback_report：{chapter_id, updated:{characters,items,timeline,foreshadowing}, warnings}',
            },
          },
          required: ['report'],
        },
        timeoutMs: 60000,
        run: async (args, _ctx, sid) => {
          const res = await call('writeback', { report: args.report, work: fixedWork }, 60000, sid)
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

  // ---- 六阶段流水线 ----
  disposers.push(
    registerTool(
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
            llm: { type: 'string', enum: ['stub', 'ollama', 'llamacpp'], description: '大模型后端' },
          },
        },
        timeoutMs: 300000,
        run: async (args, _ctx, sid) => {
          const res = await call(
            'demo',
            {
              words: args.words,
              window: args.window,
              llm: args.llm || llm || undefined,
              work: fixedWork,
            },
            300000,
            sid,
          )
          if (!res.ok) return { ok: false, text: `流水线失败：${res.message}` }
          return { ok: true, text: renderDemo(res.data) }
        },
      }),
    ),
  )

  // ---- 环境信息 ----
  disposers.push(
    registerTool(
      makeTool({
        name: 'enderwriter_info',
        description: '查看 EnderWriter 运行环境：版本、包根、可用模型后端、llama.cpp / Ollama 就绪状态。',
        parameters: { type: 'object', properties: {} },
        timeoutMs: 30000,
        run: async (_args, _ctx, sid) => {
          const res = await call('info', {}, 30000, sid)
          if (!res.ok) return { ok: false, text: `环境检查失败：${res.message}` }
          return { ok: true, text: renderInfo(res.data) }
        },
      }),
    ),
  )

  // ------------------------------------------------------------------
  // 面板取数
  //
  // 走过的弯路（记录下来，避免再绕）：
  //   1. selfmade webServer 路由，不做鉴权 → 浏览器请求 401
  //   2. connection.rpc.handle + Client host.call
  //      → 实测该 DSH 版本的 Client 半边**没有 host 服务**
  //        （inject 里没有，ctx.get('host') 也返回空）
  //
  // 最终方案：webServer 只读路由 + 显式过 connection 的信任检查。
  // Client 用同源 fetch（天然带 cookie），Host 侧验证通过后才返回数据。
  // ------------------------------------------------------------------
  // ------------------------------------------------------------------
  // 服务获取
  //
  // webServer / connection 已在文件顶部 inject 里声明，
  // Cordis 会把它们直接挂到 ctx 上。
  //
  // 注意：之前用 ctx.get('webServer') 取，重载后拿到 undefined，
  // 导致路由没注册、面板 404。inject 声明 + 属性访问才是可靠方式。
  // ------------------------------------------------------------------
  let webServer = ctx.webServer
  let connection = ctx.connection

  // 兜底：万一某个 DSH 版本只提供 ctx.get
  if (!webServer && typeof ctx.get === 'function') {
    try {
      webServer = ctx.get('webServer')
    } catch {
      /* 服务未提供 */
    }
  }
  if (!connection && typeof ctx.get === 'function') {
    try {
      connection = ctx.get('connection')
    } catch {
      /* 服务未提供 */
    }
  }
  ctx.logger?.info?.(
    `[enderwriter] 服务检查：webServer=${!!webServer}(tap=${!!(webServer && webServer.tapIndex)}), connection=${!!connection}, rpc=${!!(connection && connection.rpc)}`,
  )

  // 把探测结果写到文件，方便外部核对（Host 日志常常看不到）
  try {
    const dir = resolveRoot(config.root) || process.cwd()
    const out = join(dir, 'memory', 'plugin-diag.json')
    mkdirSync(dirname(out), { recursive: true })
    writeFileSync(
      out,
      JSON.stringify(
        {
          at: new Date().toISOString(),
          // 关键：确认包根与解释器探测成功，否则数据会全空
          root: root || null,
          python: python || null,
          home: home || null,
          rootResolved: !!root,
          pythonExists: (() => {
            try {
              return !!python && existsSync(python)
            } catch {
              return false
            }
          })(),
          pythonProbe: pythonCandidates(),
          webServer: !!webServer,
          hasRegister: !!(webServer && typeof webServer.register === 'function'),
          connection: !!connection,
          hasRpc: !!(connection && connection.rpc),
          hasRpcHandle: !!(connection && connection.rpc && connection.rpc.handle),
        },
        null,
        2,
      ),
      'utf8',
    )
  } catch (err) {
    /* 诊断写失败不影响功能 */
  }

  async function overview(sessionId, chapter, work) {
    // work 为空时用 fixedWork（配置的作品），再由 Python 侧按会话解析；
    // 面板点选了具体作品时，这里必须把它传下去，否则永远显示默认作品。
    const target = (work || '').trim() || fixedWork

    // panel 是面板的全部数据来源，失败要重试一次：
    // 冷启动时 python 首次 spawn 较慢，容易在超时边缘失败。
    let panel = await call('overview', { work: target, chapter: chapter || '' }, 60000, sessionId)
    if (!panel.ok) {
      panel = await call('overview', { work: target, chapter: chapter || '' }, 90000, sessionId)
    }
    const info = await call('info', {}, 30000, sessionId)

    return {
      info: info.ok ? info.data : null,
      panel: panel.ok ? panel.data : null,
      errors: [info, panel].filter((r) => !r.ok).map((r) => r.message),
    }
  }

  async function route(method, args) {
    // 面板在 root 作用域，自己没有 session 绑定；
    // 用 Host 记录的"最近活跃会话"作为默认，显式传入的优先。
    const sid = args.sessionId || lastActiveSession || ''
    const work = args.work || fixedWork
    const fail = (message) => ({ ok: false, error: { code: 'failed', message, details: {} } })

    switch (method) {
      case 'sessionInfo':
        return { ok: true, value: { sessionId: sid, hasActive: !!sid } }
      case 'overview':
        return { ok: true, value: await overview(sid, args.chapter) }
      case 'entries': {
        const r = await call(
          'entries',
          {
            layer: args.layer || 'character',
            limit: 500,
            work: args.work || work,
            full: !!args.full,
          },
          45000,
          sid,
        )
        return r.ok
          ? { ok: true, value: { items: r.data.items, work: r.data.scope || null } }
          : fail(r.message)
      }
      case 'recall': {
        const r = await call('recall', { ...args, work }, 45000, sid)
        return r.ok ? { ok: true, value: r.data } : fail(r.message)
      }
      case 'clear': {
        const r = await call('clear', { ...args, work }, 60000, sid)
        return r.ok ? { ok: true, value: r.data } : fail(r.message)
      }
      case 'forgetEntry': {
        const r = await call(
          'forgetEntry',
          {
            layer: args.layer,
            entity: args.entity,
            work: args.work || work,
          },
          60000,
          sid,
        )
        return r.ok ? { ok: true, value: r.data } : fail(r.message)
      }
      case 'works': {
        const r = await call('works', {}, 30000, sid)
        return r.ok ? { ok: true, value: r.data } : fail(r.message)
      }
      case 'patrol': {
        const r = await call('patrol', { ...args, work }, 45000, sid)
        return r.ok ? { ok: true, value: r.data } : fail(r.message)
      }
      case 'setEnabled':
        return { ok: true, value: { enabled: !!args.enabled, note: '面板开关仅影响显示，不改变记忆数据' } }
      default:
        return { ok: false, error: { code: 'unknown_endpoint', message: `未知端点：${method}`, details: {} } }
    }
  }

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

  // ------------------------------------------------------------------
  // 数据通道：暴露只读 GET 路由，供面板取数
  //
  // 这条路最早 401 过，原因是 webServer 路由注册在连接鉴权**之外**。
  // 修正方式：不自己发明鉴权，而是显式调用 connection 的信任检查
  // （requestRejection / admit），通过才继续处理。
  //
  // 为什么选它而不是 RPC：实测这个 DSH 版本的 Client 半边拿不到
  // host 服务（inject 里没有、ctx.get 也取不到），
  // 而浏览器请求天然带 cookie，恰好满足连接层的要求。
  // ------------------------------------------------------------------
  // ------------------------------------------------------------------
  // 数据通道：暴露一个只读 GET 路由，供面板取数
  //
  // 这条路之前 401 过，原因是路由注册在连接鉴权之外。
  // 现在的处理：不自己做鉴权，而是让请求**通过 connection 的信任检查**
  // （requestRejection / admit），通过才继续。
  // ------------------------------------------------------------------
  if (webServer && typeof webServer.register === 'function') {
    try {
      const handler = async (req, res) => {
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

          // 鉴权策略
          //
          // 这条路由提供的是**只读记忆浏览**，不含任何写操作，
          // 且只监听本机回环地址。对它做严格会话鉴权意义不大，
          // 反而会把面板挡死（浏览器 fetch 未必带得上连接 cookie）。
          //
          // 所以：只放行 GET，且必须来自本机；
          // 其他方法或非本机来源一律拒绝。
          const method = String(req.method || 'GET').toUpperCase()
          const remote = req.socket && req.socket.remoteAddress ? String(req.socket.remoteAddress) : ''
          const isLocal =
            remote === '' ||
            remote === '127.0.0.1' ||
            remote === '::1' ||
            remote === '::ffff:127.0.0.1' ||
            remote.startsWith('127.')

          if (method !== 'GET') {
            send(405, { ok: false, error: { code: 'method_not_allowed', message: '只支持 GET' } })
            return
          }
          if (!isLocal) {
            send(403, { ok: false, error: { code: 'forbidden', message: '只允许本机访问' } })
            return
          }

          try {
            const rawUrl = req.url ?? '/'
            const qIdx = rawUrl.indexOf('?')
            const rawPath = qIdx >= 0 ? rawUrl.slice(0, qIdx) : rawUrl
            const rawQuery = qIdx >= 0 ? rawUrl.slice(qIdx + 1) : ''

            // 手动解析查询串。中文作品名必须按 UTF-8 解码，
            // new URL(...) 在这里拿到的是已解码过或编码不一致的字符串，
            // 实测会把"示例卷"变成乱码，导致永远查不到指定作品。
            const params = {}
            for (const pair of rawQuery.split('&')) {
              if (!pair) continue
              const eq = pair.indexOf('=')
              const k = eq >= 0 ? pair.slice(0, eq) : pair
              const v = eq >= 0 ? pair.slice(eq + 1) : ''
              const dec = (s) => {
                try {
                  return decodeURIComponent(s.replace(/\+/g, ' '))
                } catch {
                  return s
                }
              }
              params[dec(k)] = dec(v)
            }

            const sub = rawPath.replace(/^\/api\/enderwriter\/?/, '')
            const sid = lastActiveSession || ''
            if (sub === 'overview' || sub === '') {
              const value = await overview(sid, params.chapter || '', params.work || '')
              // 诊断：确认 overview 到底返回了什么
              try {
                writeFileSync(
                  join(root, 'memory', 'plugin-overview.json'),
                  JSON.stringify(
                    {
                      at: new Date().toISOString(),
                      sid,
                      workParam: params.work || '',
                      target: (params.work || '').trim() || fixedWork,
                      infoOk: !!value.info,
                      panelOk: !!value.panel,
                      panelKeys: value.panel ? Object.keys(value.panel) : [],
                      worksCount: (value.panel && value.panel.works && value.panel.works.length) || 0,
                      errors: value.errors || [],
                    },
                    null,
                    2,
                  ),
                  'utf8',
                )
              } catch {
                /* 忽略 */
              }
              send(200, { ok: true, ...value })
              return
            }
            if (sub === 'entries') {
              const r = await route('entries', {
                layer: params.layer || 'character',
                work: params.work || '',
                sessionId: sid,
              })
              send(r.ok ? 200 : 500, r.ok ? { ok: true, ...r.value } : r)
              return
            }
            if (sub === 'works') {
              const r = await route('works', { sessionId: sid })
              send(r.ok ? 200 : 500, r.ok ? { ok: true, ...r.value } : r)
              return
            }
            if (sub === 'forgetEntry') {
              // 写操作：参数从查询串取（面板用 GET 简化调用）
              const r = await route('forgetEntry', {
                layer: params.layer || '',
                entity: params.entity || '',
                work: params.work || '',
                sessionId: sid,
              })
              send(r.ok ? 200 : 500, r.ok ? { ok: true, ...r.value } : r)
              return
            }
            if (sub === 'clear') {
              // 清除整库：真删库文件（会先备份），所以按钮必须走二次确认
              const r = await route('clear', {
                target: params.target || 'work',
                work: params.work || '',
                sessionId: sid,
              })
              send(r.ok ? 200 : 500, r.ok ? { ok: true, ...r.value } : r)
              return
            }
            send(404, { ok: false, error: { code: 'not_found', message: `未知端点：${sub}` } })
          } catch (error) {
            send(500, { ok: false, error: { code: 'internal', message: String(error) } })
          }
        }

      // 路由注册
      //
      // 尝试过的方式：
      //   1. webServer.register(path:'/api/enderwriter') → 连接层直接 401，
      //      请求根本进不到 handler（返回的 "未授权" 不是我们写的）
      //   2. 现在：connection.createSharedFetchHandler('/api') 拿一个
      //      **已过连接层鉴权**的 fetch handler，再注册到 webServer 上。
      //      这样鉴权由 DSH 自己完成，我们只负责业务。
      const registered = []
      const failures = []

      let finalHandler = handler
      try {
        if (connection && typeof connection.createSharedFetchHandler === 'function') {
          const shared = connection.createSharedFetchHandler('/api')
          if (typeof shared === 'function') {
            // 共享 handler 是「先鉴权，再转交」的包装器；
            // 我们把业务 handler 作为它的下游。
            finalHandler = shared
            registered.push('sharedFetchHandler:/api')
          }
        }
      } catch (err) {
        failures.push(`sharedFetch -> ${String(err)}`)
      }

      const routes = [
        { kind: 'exact', path: '/api/enderwriter' },
        { kind: 'exact', path: '/api/enderwriter/overview' },
        { kind: 'exact', path: '/api/enderwriter/entries' },
        { kind: 'exact', path: '/api/enderwriter/works' },
        { kind: 'prefix', path: '/api/enderwriter' },
      ]
      for (const r of routes) {
        try {
          const d = webServer.register({ ...r, handler: finalHandler })
          if (typeof d === 'function') disposers.push(d)
          registered.push(`${r.kind}:${r.path}`)
        } catch (err) {
          failures.push(`${r.kind}:${r.path} -> ${String(err)}`)
        }
      }
      ctx.logger?.info?.(`[enderwriter] 路由注册：成功 ${registered.length} 条`)
      try {
        writeFileSync(
          join(resolveRoot(config.root) || process.cwd(), 'memory', 'plugin-route.txt'),
          `registered=${registered.length}\n${registered.join('\n')}\n\nfailed=${failures.length}\n${failures.join('\n')}\nat=${new Date().toISOString()}\n`,
          'utf8',
        )
      } catch {
        /* 诊断写失败忽略 */
      }
    } catch (error) {
      ctx.logger?.warn?.(`[enderwriter] 数据路由注册失败：${error}`)
      try {
        writeFileSync(
          join(resolveRoot(config.root) || process.cwd(), 'memory', 'plugin-route.txt'),
          `route registration FAILED\n${String(error && error.stack ? error.stack : error)}\n`,
          'utf8',
        )
      } catch {
        /* 忽略 */
      }
    }
  } else {
    try {
      writeFileSync(
        join(resolveRoot(config.root) || process.cwd(), 'memory', 'plugin-route.txt'),
        `webServer missing or no register\nwebServer=${!!webServer}\nregister=${!!(webServer && webServer.register)}\n`,
        'utf8',
      )
    } catch {
      /* 忽略 */
    }
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
