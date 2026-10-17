/**
 * EnderWriter —— Web 端可视化面板（Client 半边）
 *
 * 两个注册点：
 *   sidebar.panellist  id='enderwriter'  → 侧边栏图标按钮
 *   main               key='enderwriter' → 中央面板
 * 两者用同一个 id，DSH 会让点击图标自动切到对应面板。
 *
 * 数据来自 Host 注册的只读路由 /api/enderwriter/overview。
 */
window.__ModuleLoader__.load({
  id: '@local/enderwriter',
  factory(require) {
    const React = require('react')
    // 动态 Client 半边的 React 未必导出全部 hook：缺什么就退化成朴素实现，
    // 宁可少一次 memo，也不要让整个面板因 "undefined is not a function" 而空白。
    const useState = React.useState
    const useEffect = React.useEffect
    const useCallback = React.useCallback || ((fn) => fn)
    const useMemo = React.useMemo || ((fn) => fn())
    const h = React.createElement

    const ENDPOINT = '/api/enderwriter/overview'

    /**
     * Client → Host 的包私有 RPC。
     * 宿主会向本包注入 `host.call(method, args)`（见 Builtin 的 host 条目），
     * 它走已鉴权的共享通道，因此不需要我们自己带凭据。
     */
    function hostCall() {
      const g = typeof globalThis !== 'undefined' ? globalThis : {}
      if (g.host && typeof g.host.call === 'function') return g.host.call
      // 某些加载器把 host 放在 window 上
      if (typeof window !== 'undefined' && window.host && typeof window.host.call === 'function') {
        return window.host.call
      }
      return null
    }

    async function callHost(method, args) {
      const payload = args ?? {}
      const tried = []

      // 首选：宿主注入的包私有 RPC（自动继承鉴权）
      const call = hostCall()
      if (call) {
        try {
          const raw = await call(method, payload)
          if (raw && typeof raw === 'object' && 'ok' in raw && 'value' in raw) {
            if (!raw.ok) throw new Error(raw.error?.message ?? '主机返回失败')
            return raw.value
          }
          return raw
        } catch (err) {
          tried.push(`host.call: ${err && err.message ? err.message : err}`)
          console.warn('[enderwriter] host.call 失败：', err)
        }
      } else {
        tried.push('host.call: 宿主未注入')
      }

      // 兜底：同源 HTTP（带 cookie）
      const qs = method === 'overview'
        ? `?chapter=${encodeURIComponent(payload.chapter ?? 'ch_001')}`
        : `?layer=${encodeURIComponent(payload.layer ?? 'character')}`
      const url = `${ENDPOINT.replace(/\/overview$/, '')}/${method}${qs}`
      try {
        const res = await fetch(url, {
          headers: { Accept: 'application/json' },
          credentials: 'same-origin',
        })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json = await res.json()
        if (!json.ok) throw new Error((json.errors ?? []).join('；') || '接口返回失败')
        return json
      } catch (err) {
        tried.push(`HTTP ${url}: ${err && err.message ? err.message : err}`)
      }

      // 两条都失败：把诊断信息带出去，让面板显示具体原因
      throw new Error(tried.join(' ｜ '))
    }

    const LAYERS = [
      { key: 'character', label: '人物格', cn: '人物' },
      { key: 'world', label: '世界格', cn: '世界' },
      { key: 'timeline', label: '时间格', cn: '时间线' },
      { key: 'foreshadowing', label: '伏笔格', cn: '伏笔' },
      { key: 'item', label: '物品格', cn: '物品' },
      { key: 'style', label: '风格格', cn: '风格' },
    ]

    const STATUS_LABEL = {
      planted: '已埋',
      reinforced: '已强化',
      resolved: '已回收',
      abandoned: '已废弃',
    }

    // ---- 样式（全部走宿主主题令牌，跟随明暗切换）----
    const styles = {
      root: {
        height: '100%',
        overflow: 'auto',
        background: 'var(--dsw-alias-bg-base)',
        color: 'var(--dsw-alias-label-primary)',
        font: 'inherit',
      },
      inner: { maxWidth: 980, margin: '0 auto', padding: '28px 24px 64px' },
      header: { display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 4 },
      title: { fontSize: 20, fontWeight: 600, margin: 0 },
      sub: { fontSize: 13, color: 'var(--dsw-alias-label-secondary)' },
      toolbar: {
        display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap',
        margin: '18px 0 20px',
      },
      card: {
        border: '1px solid var(--dsw-alias-border-l1)',
        background: 'var(--dsw-alias-bg-layer-1)',
        borderRadius: 10,
        padding: '14px 16px',
      },
      grid: {
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))',
        gap: 10,
        marginBottom: 24,
      },
      statNum: { fontSize: 22, fontWeight: 600, lineHeight: 1.2 },
      statLabel: { fontSize: 12, color: 'var(--dsw-alias-label-secondary)', marginTop: 2 },
      sectionTitle: { fontSize: 14, fontWeight: 600, margin: '26px 0 10px' },
      row: {
        display: 'flex', alignItems: 'flex-start', gap: 10,
        padding: '10px 0',
        borderTop: '1px solid var(--dsw-alias-border-l1)',
      },
      badge: {
        flex: '0 0 auto',
        fontSize: 11,
        padding: '2px 7px',
        borderRadius: 999,
        border: '1px solid var(--dsw-alias-border-l2)',
        color: 'var(--dsw-alias-label-secondary)',
        whiteSpace: 'nowrap',
      },
      name: { fontSize: 13, fontWeight: 600, marginBottom: 3 },
      body: { fontSize: 13, lineHeight: 1.6, color: 'var(--dsw-alias-label-secondary)' },
      btn: {
        font: 'inherit',
        fontSize: 13,
        padding: '5px 12px',
        borderRadius: 7,
        cursor: 'pointer',
        border: '1px solid var(--dsw-alias-border-l2)',
        background: 'var(--dsw-alias-bg-layer-1)',
        color: 'var(--dsw-alias-label-primary)',
      },
      btnActive: {
        borderColor: 'var(--dsw-alias-brand-primary)',
        color: 'var(--dsw-alias-brand-primary)',
      },
      input: {
        font: 'inherit',
        fontSize: 13,
        padding: '5px 10px',
        borderRadius: 7,
        width: 110,
        border: '1px solid var(--dsw-alias-border-l2)',
        background: 'var(--dsw-alias-bg-base)',
        color: 'var(--dsw-alias-label-primary)',
      },
      warn: { color: 'var(--dsw-alias-state-warn-primary)' },
      ok: { color: 'var(--dsw-alias-state-success-primary)' },
      err: { color: 'var(--dsw-alias-state-error-primary)' },
      empty: {
        padding: '28px 0',
        textAlign: 'center',
        fontSize: 13,
        color: 'var(--dsw-alias-label-secondary)',
      },
    }

    function statusColor(status) {
      if (status === 'resolved') return 'var(--dsw-alias-state-success-primary)'
      if (status === 'abandoned') return 'var(--dsw-alias-state-idle-primary)'
      if (status === 'reinforced') return 'var(--dsw-alias-brand-primary)'
      return 'var(--dsw-alias-state-warn-primary)'
    }

    function StatCard(props) {
      return h('div', { style: styles.card },
        h('div', { style: styles.statNum }, String(props.value ?? 0)),
        h('div', { style: styles.statLabel }, props.label))
    }

    function ForeshadowRow(props) {
      const fs = props.item
      return h('div', { style: styles.row },
        h('span', {
          style: { ...styles.badge, color: statusColor(fs.status), borderColor: statusColor(fs.status) },
        }, STATUS_LABEL[fs.status] ?? fs.status),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('div', { style: styles.name },
            fs.id,
            h('span', { style: { ...styles.statLabel, marginLeft: 8 } },
              `埋于 ${fs.planted_chapter ?? '—'} · 计划 ${fs.planned_resolve ?? '—'}`,
              fs.resolved_chapter ? ` · 回收于 ${fs.resolved_chapter}` : '')),
          h('div', { style: styles.body }, fs.content)))
    }

    function EntryRow(props) {
      const it = props.item
      return h('div', { style: styles.row },
        h('span', { style: styles.badge }, `v${it.version}`),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('div', { style: styles.name },
            it.entity,
            it.chapter_id
              ? h('span', { style: { ...styles.statLabel, marginLeft: 8 } }, it.chapter_id)
              : null),
          h('div', { style: styles.body }, it.content)))
    }

    function Panel() {
      const [data, setData] = useState(null)
      const [loading, setLoading] = useState(true)
      const [error, setError] = useState(null)
      const [chapter, setChapter] = useState('ch_016')
      const [layer, setLayer] = useState('character')
      const [entries, setEntries] = useState(null)

      const load = useCallback(async (chap) => {
        setLoading(true)
        setError(null)
        try {
          const data = await callHost('overview', { chapter: chap })
          setData(data)
        } catch (e) {
          setError(String(e && e.message ? e.message : e))
        } finally {
          setLoading(false)
        }
      }, [])

      useEffect(() => { load(chapter) }, [load, chapter])

      useEffect(() => {
        let alive = true
        setEntries(null)
        callHost('entries', { layer })
          .then((j) => { if (alive && j) setEntries(j) })
          .catch(() => { if (alive) setEntries(null) })
        return () => { alive = false }
      }, [layer])

      const stats = data?.stats ?? {}
      const patrol = data?.patrol ?? {}
      const foreshadowings = data?.foreshadowings ?? []
      const info = data?.info ?? {}

      const records = stats.records ?? {}
      const total = useMemo(
        () => LAYERS.reduce((sum, l) => sum + (records[l.key] ?? 0), 0),
        [records],
      )

      // 注意：这里**不提前 return**。即使还在加载或已出错，
      // 也要把标题栏与状态行渲染出来，让用户看到面板确实在工作，
      // 而不是一整片空白（空白会被误认为插件没生效）。
      const statusLine = error
        ? h('div', { style: { ...styles.body, ...styles.err, marginTop: 8 } },
            `读取失败：${error}`)
        : loading
          ? h('div', { style: { ...styles.body, marginTop: 8 } }, '正在读取末影箱…')
          : null

      return h('div', { style: styles.root },
        h('div', { style: styles.inner },
          // 标题
          h('div', { style: styles.header },
            h('h2', { style: styles.title }, '末影箱'),
            h('span', { style: styles.sub },
              info.llamacpp?.ready
                ? `本地模型就绪 · ${Math.round(info.llamacpp.model_mb ?? 0)}MB`
                : '本地模型未就绪 · 走离线 Stub')),
          statusLine,

          // 工具栏
          h('div', { style: styles.toolbar },
            h('span', { style: styles.sub }, '当前章节'),
            h('input', {
              style: styles.input,
              value: chapter,
              onChange: (e) => setChapter(e.target.value),
              onKeyDown: (e) => { if (e.key === 'Enter') load(chapter) },
              placeholder: 'ch_016',
            }),
            h('button', { style: styles.btn, onClick: () => load(chapter) }, '刷新')),

          // 概览数字
          h('div', { style: styles.grid },
            h(StatCard, { label: '记录总数', value: total }),
            h(StatCard, { label: '版本历史', value: stats.history }),
            h(StatCard, { label: '倒排词条', value: stats.terms }),
            h(StatCard, { label: '关系边', value: stats.edges }),
            h(StatCard, { label: '章节', value: stats.chapters }),
            h(StatCard, { label: '未回收伏笔', value: stats.open_foreshadowings })),

          // 各格子分布
          h('div', { style: styles.sectionTitle }, '七个格子'),
          h('div', { style: styles.grid },
            LAYERS.map((l) => h(StatCard, {
              key: l.key, label: l.label, value: records[l.key] ?? 0,
            }))),

          // 巡检
          h('div', { style: styles.sectionTitle },
            '伏笔巡检 · ' + (patrol.chapter ?? chapter),
            patrol.count
              ? h('span', { style: { ...styles.warn, marginLeft: 8, fontWeight: 400, fontSize: 13 } },
                  `${patrol.count} 项需处理`)
              : h('span', { style: { ...styles.ok, marginLeft: 8, fontWeight: 400, fontSize: 13 } },
                  '一切正常')),
          (patrol.items ?? []).length === 0
            ? h('div', { style: styles.empty }, '没有超期或陈旧的伏笔。')
            : h('div', {}, patrol.items.map((it, i) =>
                h('div', { key: i, style: styles.row },
                  h('span', {
                    style: {
                      ...styles.badge,
                      color: it.type === 'overdue'
                        ? 'var(--dsw-alias-state-error-primary)'
                        : 'var(--dsw-alias-state-warn-primary)',
                    },
                  }, it.type === 'overdue' ? '超期' : it.type === 'stale' ? '陈旧' : it.type),
                  h('div', { style: { flex: 1, minWidth: 0 } },
                    h('div', { style: styles.name }, it.id),
                    h('div', { style: styles.body }, it.suggestion ?? ''))))),

          // 全部伏笔
          h('div', { style: styles.sectionTitle }, `全部伏笔 · ${foreshadowings.length}`),
          h('div', {}, foreshadowings.map((fs) =>
            h(ForeshadowRow, { key: fs.id, item: fs }))),

          // 格子浏览
          h('div', { style: styles.sectionTitle }, '浏览条目'),
          h('div', { style: styles.toolbar },
            LAYERS.map((l) => h('button', {
              key: l.key,
              style: l.key === layer ? { ...styles.btn, ...styles.btnActive } : styles.btn,
              onClick: () => setLayer(l.key),
            }, l.cn))),
          entries && entries.items && entries.items.length
            ? h('div', {}, entries.items.map((it) =>
                h(EntryRow, { key: it.entity, item: it })))
            : h('div', { style: styles.empty }, '这个格子还是空的。')))
    }

    // ---- 侧边栏图标 ----
    function PanelIcon(props) {
      const size = props && props.size ? props.size : 20
      const active = props && props.active
      const color = active ? 'var(--dsw-alias-brand-primary)' : 'currentColor'
      return h('svg', {
        viewBox: '0 0 24 24', width: size, height: size, fill: 'none',
        'aria-hidden': true, style: { display: 'block' },
      },
        h('path', {
          d: 'M3 8.5 5 4.5h14l2 4v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z',
          stroke: color, strokeWidth: 1.6, strokeLinejoin: 'round',
        }),
        h('path', { d: 'M3 8.5h18', stroke: color, strokeWidth: 1.6 }),
        h('rect', {
          x: 10, y: 11, width: 4, height: 4, rx: 1,
          fill: color, opacity: 0.9,
        }))
    }

    // ---- 出错兜底：任何异常都显示成文本，而不是留一块空白 ----
    // 不用 class 错误边界：动态 Client 半边的 React 未必暴露 Component。
    function SafePanel() {
      try {
        // 直接以函数方式求值，异常在本帧内就能捕获
        return Panel()
      } catch (err) {
        const msg = String(err && err.message ? err.message : err)
        return h('div', { style: styles.root }, h('div', { style: styles.inner },
          h('div', { style: styles.sectionTitle }, '末影箱面板渲染出错'),
          h('div', { style: { ...styles.body, ...styles.err } }, msg),
          h('div', { style: { ...styles.body, marginTop: 12 } },
            '工具（检索 / 巡检 / 写作）不受影响，仍可在对话里使用。')))
      }
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
          name: 'sidebar.panellist',
          id: 'enderwriter',
          order: 20,
          label: '末影箱',
        }, PanelIcon))

        ctx.slots.inject('main', () => ctx.slots.register({
          name: 'main',
          key: 'enderwriter',
        }, SafePanel))
      },
    }
  },
})
