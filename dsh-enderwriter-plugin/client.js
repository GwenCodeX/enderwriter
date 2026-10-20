/**
 * EnderWriter —— Web 端可视化面板（Client 半边）
 *
 * 两个注册点：
 *   sidebar.panellist  id='enderwriter'  → 侧边栏图标按钮
 *   main               key='enderwriter' → 中央面板
 * 两者用同一个 id，DSH 会让点击图标自动切到对应面板。
 *
 * 取数走 DSH 的包私有 RPC（Client 半边 → 本包 Host 半边），
 * 通道由框架配对，不需要自己搭 HTTP 路由。
 */
window.__ModuleLoader__.load({
  id: 'dsh-enderwriter',
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
     *
     * Builtin 清单里 `React` 是靠 `require('react')` 拿到的，
     * 同列表的 `host` / `styles` / `ctx` 也是同一机制——它们是
     * 动态 Client 半边可 require 的符号，不在 ctx 上、也不在 globalThis 上。
     *
     * 这里按「require 优先 → ctx 属性 → 全局」的顺序探测，并把每一步结果
     * 记进 probeLog，失败时能直接看出哪条路不通。
     */
    let hostRpc = null
    let stylesRpc = null
    const probeLog = []

    function _try(fn, label) {
      try {
        const v = fn()
        if (v) {
          probeLog.push(`${label}: ok`)
          return v
        }
        probeLog.push(`${label}: 空`)
      } catch (err) {
        probeLog.push(`${label}: ${(err && err.message) || err}`.slice(0, 90))
      }
      return null
    }

    /**
     * 获取 host RPC 与 styles。
     *
     * 这两者是 Cordis 服务。两种取法各有代价：
     *   * 写进 inject —— 宿主不提供时插件永久 pending，插槽都不注册（图标消失）
     *   * ctx.get(name) —— 服务缺失时可能抛异常，但可以 try 住
     * 面板取数是"锦上添花"，图标存在才是底线，所以选 ctx.get 软获取。
     */
    function probeCtx(ctx) {
      const getter = (name) => {
        // 先试 ctx.get（软获取），再试属性访问（可能因未 inject 而抛）
        try {
          if (typeof ctx.get === 'function') {
            const v = ctx.get(name)
            if (v) return v
          }
        } catch {
          /* 服务未注册 */
        }
        try {
          if (typeof ctx.has === 'function' && !ctx.has(name)) return null
          const v = ctx[name]
          if (v) return v
        } catch {
          /* 未 inject，正常 */
        }
        return null
      }

      if (!hostRpc) {
        const h = getter('host')
        if (h && typeof h.call === 'function') hostRpc = h.call.bind(h)
        else if (typeof h === 'function') hostRpc = h
      }
      if (!stylesRpc) {
        const s = getter('styles')
        if (s && typeof s.insert === 'function') stylesRpc = s
      }
      if (!hostRpc) {
        probeLog.push('host: 未取到（面板将无法取数）')
      }
    }

    async function callHost(method, args) {
      const payload = args ?? {}

      // 通道 1：包私有 RPC（若这个 DSH 版本提供 host 服务）
      if (hostRpc) {
        try {
          const raw = await hostRpc(method, payload)
          if (raw && typeof raw === 'object' && 'ok' in raw && 'value' in raw) {
            if (!raw.ok) throw new Error(raw.error?.message ?? '主机返回失败')
            return raw.value
          }
          return raw
        } catch (err) {
          console.warn('[enderwriter] host RPC 调用失败，改走 HTTP：', err)
        }
      }

      // 通道 2：Host 注册的同源只读路由（主通道）
      // 浏览器请求天然带 cookie，Host 侧会过连接层的信任检查。
      const enc = (v) => encodeURIComponent(v == null ? '' : String(v))
      const qs =
        method === 'overview'
          ? `?chapter=${enc(payload.chapter)}&work=${enc(payload.work)}`
          : method === 'entries'
            ? `?layer=${enc(payload.layer || 'character')}&work=${enc(payload.work)}`
            : method === 'clear'
              ? `?target=${enc(payload.target)}&work=${enc(payload.work)}`
              : method === 'forgetEntry'
                ? `?layer=${enc(payload.layer)}&entity=${enc(payload.entity)}&work=${enc(payload.work)}`
                : method === 'history'
                  ? `?layer=${enc(payload.layer)}&entity=${enc(payload.entity)}&work=${enc(payload.work)}`
                  : method === 'revert'
                    ? `?layer=${enc(payload.layer)}&entity=${enc(payload.entity)}&version=${enc(payload.version)}&work=${enc(payload.work)}`
                    : method === 'archive'
                      ? `?layer=${enc(payload.layer)}&entity=${enc(payload.entity)}&archived=${payload.archived ? 1 : 0}&work=${enc(payload.work)}`
                      : method === 'usage'
                        ? `?layer=${enc(payload.layer)}&work=${enc(payload.work)}`
                        : ''
      const url = `/api/enderwriter/${method}${qs}`
      // 写操作用 POST：正文可能上千字，走查询串会被 URL 长度限制截断。
      const WRITE_OPS = { update: 1, forgetEntry: 1, clear: 1, revert: 1, archive: 1 }
      const isWrite = !!WRITE_OPS[method]
      const target = isWrite ? `/api/enderwriter/${method}` : url

      let res
      try {
        res = await fetch(target, {
          method: isWrite ? 'POST' : 'GET',
          headers: isWrite
            ? { Accept: 'application/json', 'Content-Type': 'application/json' }
            : { Accept: 'application/json' },
          credentials: 'same-origin',
          ...(isWrite ? { body: JSON.stringify(payload) } : {}),
        })
      } catch (netErr) {
        throw new Error(`网络请求失败(${target})：${(netErr && netErr.message) || netErr}`)
      }
      if (!res.ok) {
        let detail = ''
        try {
          detail = (await res.text()).slice(0, 200)
        } catch {
          /* 忽略 */
        }
        throw new Error(`HTTP ${res.status} ${target} ${detail}`)
      }
      const json = await res.json()
      if (json && json.ok === false) {
        throw new Error((json.error && json.error.message) || '接口返回失败')
      }
      return json
    }

    // ================================================================
    // 样式表：类名驱动，动画 / 过渡 / hover 都写在这里
    // 颜色一律走宿主主题令牌，跟随明暗切换
    // ================================================================
    const CSS = `
/* ==========================================================================
   EnderWriter 末影箱 · 视觉样式
   风格基准：DSH「插件设置」页
     * 无重边框 —— 靠留白与细分隔线分组
     * 行式布局：左图标方块 + 中标题/说明 + 右动作
     * 标题分三级：页面标题 / 分组小标题 / 行标题
     * 图标 = 36px 圆角方块，浅底
     * 悬停只做极轻微的背景变化，不做位移
   ========================================================================== */

/* ---------- 容器 ---------- */
.ew-root {
  height: 100%;
  overflow-y: auto;
  overflow-x: hidden;
  background: var(--dsw-alias-bg-base);
  color: var(--dsw-alias-label-primary);
  font-variant-numeric: tabular-nums;
}
.ew-inner {
  max-width: 820px;
  margin: 0 auto;
  padding: 28px 24px 96px;
  animation: ew-in .24s cubic-bezier(.22,.61,.36,1) both;
}
@keyframes ew-in {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: none; }
}

/* ---------- 页面标题 ---------- */
.ew-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 24px;
}
.ew-title {
  font-size: 17px;
  font-weight: 600;
  margin: 0 0 4px;
  letter-spacing: -.005em;
  display: flex;
  align-items: center;
  gap: 6px;
}
.ew-sub {
  font-size: 12.5px;
  color: var(--dsw-alias-label-secondary);
  line-height: 1.55;
}
.ew-head-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
  align-items: center;
  flex: 0 0 auto;
}

/* ---------- 分组小标题 ---------- */
.ew-section-title {
  font-size: 12.5px;
  font-weight: 600;
  margin: 26px 0 10px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--dsw-alias-label-primary);
}
.ew-section-title:first-child { margin-top: 0; }

/* ---------- 状态条 ---------- */
.ew-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  margin-bottom: 16px;
  padding: 9px 12px;
  border-radius: 8px;
  background: var(--dsw-alias-bg-layer-1);
  animation: ew-drop .18s ease both;
}
@keyframes ew-drop {
  from { opacity: 0; transform: translateY(-3px); }
  to   { opacity: 1; transform: none; }
}
.ew-status.is-error {
  color: var(--dsw-alias-state-error-primary);
  background: color-mix(in srgb, var(--dsw-alias-state-error-primary) 8%, transparent);
}
.ew-spinner {
  width: 12px; height: 12px; flex: 0 0 auto;
  border: 2px solid var(--dsw-alias-border-l2);
  border-top-color: var(--dsw-alias-brand-primary);
  border-radius: 50%;
  animation: ew-spin .7s linear infinite;
}
@keyframes ew-spin { to { transform: rotate(360deg); } }

/* ---------- 按钮 ---------- */
.ew-btn {
  font: inherit;
  font-size: 12.5px;
  padding: 5px 12px;
  border-radius: 7px;
  cursor: pointer;
  white-space: nowrap;
  border: 1px solid var(--dsw-alias-border-l1);
  background: transparent;
  color: var(--dsw-alias-label-primary);
  transition: background .13s ease, border-color .13s ease, color .13s ease;
}
.ew-btn:hover:not(:disabled) {
  background: var(--dsw-alias-bg-layer-2);
  border-color: var(--dsw-alias-border-l2);
}
.ew-btn:active:not(:disabled) { background: var(--dsw-alias-bg-layer-1); }
.ew-btn:disabled { opacity: .4; cursor: not-allowed; }
.ew-btn:focus-visible {
  outline: 2px solid var(--dsw-alias-brand-primary);
  outline-offset: 2px;
}
.ew-btn.is-active {
  border-color: var(--dsw-alias-brand-primary);
  color: var(--dsw-alias-brand-primary);
  background: color-mix(in srgb, var(--dsw-alias-brand-primary) 10%, transparent);
  font-weight: 550;
}
.ew-btn.is-primary {
  border-color: var(--dsw-alias-brand-primary);
  background: var(--dsw-alias-brand-primary);
  color: #fff;
  font-weight: 550;
}
.ew-btn.is-primary:hover:not(:disabled) {
  background: var(--dsw-alias-brand-primary);
  filter: brightness(1.07);
}
.ew-btn.is-danger {
  color: var(--dsw-alias-state-error-primary);
  border-color: color-mix(in srgb, var(--dsw-alias-state-error-primary) 40%, transparent);
}
.ew-btn.is-danger:hover:not(:disabled) {
  background: var(--dsw-alias-state-error-primary);
  border-color: var(--dsw-alias-state-error-primary);
  color: #fff;
}

/* ---------- 区块选择（行式，仿插件列表） ---------- */
.ew-pick-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 4px;
}
.ew-pick {
  display: flex;
  align-items: center;
  gap: 13px;
  width: 100%;
  text-align: left;
  font: inherit;
  cursor: pointer;
  padding: 11px 10px;
  border-radius: 9px;
  border: none;
  background: transparent;
  color: var(--dsw-alias-label-primary);
  transition: background .13s ease;
}
.ew-pick:hover { background: var(--dsw-alias-bg-layer-1); }
.ew-pick:active { background: var(--dsw-alias-bg-layer-2); }
.ew-pick:focus-visible {
  outline: 2px solid var(--dsw-alias-brand-primary);
  outline-offset: -2px;
}
.ew-pick-icon {
  flex: 0 0 auto;
  width: 36px;
  height: 36px;
  border-radius: 9px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  background: var(--dsw-alias-bg-layer-2);
  border: 1px solid var(--dsw-alias-border-l1);
}
.ew-pick-main { flex: 1; min-width: 0; }
.ew-pick-title {
  font-size: 13.5px;
  font-weight: 550;
  margin-bottom: 2px;
  display: flex;
  align-items: center;
  gap: 7px;
}
.ew-pick-sub {
  font-size: 12px;
  line-height: 1.5;
  color: var(--dsw-alias-label-secondary);
}
.ew-pick-count {
  flex: 0 0 auto;
  font-size: 12.5px;
  color: var(--dsw-alias-label-secondary);
  padding: 2px 9px;
  border-radius: 999px;
  background: var(--dsw-alias-bg-layer-2);
  min-width: 34px;
  text-align: center;
}

/* ---------- 统计卡（弱化边框，仿设置项分组） ---------- */
.ew-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  gap: 8px;
  margin-bottom: 6px;
}
.ew-card {
  border: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-alias-bg-layer-1);
  border-radius: 9px;
  padding: 12px 14px;
  transition: background .13s ease, border-color .13s ease;
}
.ew-card:hover {
  background: var(--dsw-alias-bg-layer-2);
  border-color: var(--dsw-alias-border-l2);
}
.ew-stat-num {
  font-size: 19px;
  font-weight: 600;
  line-height: 1.2;
  animation: ew-num .3s cubic-bezier(.22,.61,.36,1) both;
}
@keyframes ew-num {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: none; }
}
.ew-stat-label {
  font-size: 11.5px;
  margin-top: 2px;
  color: var(--dsw-alias-label-secondary);
}

/* ---------- 分层区块（无边框，用间距分组） ---------- */
.ew-layer {
  margin-top: 24px;
  animation: ew-in .24s cubic-bezier(.22,.61,.36,1) both;
}
.ew-layer-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 14px;
}
.ew-layer-title {
  font-size: 14.5px;
  font-weight: 600;
  margin-bottom: 3px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.ew-layer-name { color: var(--dsw-alias-brand-primary); }
.ew-layer.is-global .ew-layer-name { color: var(--dsw-alias-state-success-primary); }
.ew-layer.is-tech .ew-layer-name { color: var(--dsw-alias-state-warn-primary); }
.ew-layer-hint {
  font-size: 12px;
  line-height: 1.55;
  color: var(--dsw-alias-label-secondary);
}

/* ---------- 标签 ---------- */
.ew-tag {
  display: inline-block;
  font-size: 10.5px;
  font-weight: 500;
  padding: 1px 7px;
  border-radius: 5px;
  background: var(--dsw-alias-bg-layer-2);
  color: var(--dsw-alias-label-secondary);
}

/* ---------- 大库分组标题 ---------- */
.ew-group-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin: 26px 0 8px;
  padding-bottom: 7px;
  border-bottom: 1px solid var(--dsw-alias-border-l1);
}
.ew-group-head:first-child { margin-top: 8px; }
.ew-group-title {
  font-size: 13.5px;
  font-weight: 650;
  letter-spacing: .01em;
}
.ew-group-hint {
  font-size: 11.5px;
  color: var(--dsw-alias-label-secondary);
  line-height: 1.5;
}

/* ---------- 搜索框 ---------- */
.ew-search {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 14px;
  padding: 0 11px;
  height: 34px;
  border-radius: 8px;
  border: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-alias-bg-layer-1);
  transition: border-color .13s ease, box-shadow .13s ease;
}
.ew-search:focus-within {
  border-color: var(--dsw-alias-brand-primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--dsw-alias-brand-primary) 12%, transparent);
}
.ew-search-icon {
  flex: 0 0 auto;
  font-size: 14px;
  line-height: 1;
  color: var(--dsw-alias-label-secondary);
  opacity: .65;
}
.ew-search-input {
  flex: 1;
  min-width: 0;
  font: inherit;
  font-size: 12.5px;
  height: 100%;
  border: none;
  outline: none;
  background: transparent;
  color: var(--dsw-alias-label-primary);
}
.ew-search-input::placeholder { color: var(--dsw-alias-label-secondary); opacity: .8; }
.ew-search-input::-webkit-search-cancel-button { display: none; }
.ew-search-clear {
  flex: 0 0 auto;
  font: inherit;
  font-size: 15px;
  line-height: 1;
  width: 19px;
  height: 19px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: none;
  border-radius: 50%;
  cursor: pointer;
  background: var(--dsw-alias-bg-layer-2);
  color: var(--dsw-alias-label-secondary);
  transition: background .13s ease, color .13s ease;
}
.ew-search-clear:hover {
  background: var(--dsw-alias-state-error-primary);
  color: #fff;
}

/* ---------- 列表行（细分隔线，仿设置项） ---------- */
.ew-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 8px;
  border-radius: 7px;
  transition: background .13s ease;
}
.ew-row:hover { background: var(--dsw-alias-bg-layer-1); }
.ew-row-entry {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 8px;
  border-radius: 7px;
  transition: background .13s ease;
}
.ew-row-entry:hover { background: var(--dsw-alias-bg-layer-1); }
.ew-row-entry .ew-row-body { white-space: pre-wrap; word-break: break-word; }
.ew-row-name {
  font-size: 12.5px;
  font-weight: 550;
  margin-bottom: 2px;
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.ew-row-body {
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--dsw-alias-label-secondary);
}
.ew-body-full { color: var(--dsw-alias-label-primary) !important; }

/* ---------- 徽标 ---------- */
.ew-badge {
  flex: 0 0 auto;
  font-size: 10.5px;
  font-weight: 500;
  padding: 1px 7px;
  border-radius: 5px;
  background: var(--dsw-alias-bg-layer-2);
  color: var(--dsw-alias-label-secondary);
  white-space: nowrap;
}

/* ---------- 空态 ---------- */
.ew-empty {
  padding: 36px 16px;
  text-align: center;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--dsw-alias-label-secondary);
  animation: ew-in .24s ease both;
}
.ew-empty-icon {
  font-size: 22px;
  opacity: .3;
  margin-bottom: 8px;
}

/* ---------- 骨架屏 ---------- */
.ew-skel {
  border-radius: 9px;
  background: linear-gradient(
    100deg,
    var(--dsw-alias-bg-layer-1) 30%,
    var(--dsw-alias-bg-layer-2) 50%,
    var(--dsw-alias-bg-layer-1) 70%
  );
  background-size: 220% 100%;
  animation: ew-shimmer 1.3s ease-in-out infinite;
}
@keyframes ew-shimmer {
  from { background-position: 180% 0; }
  to   { background-position: -40% 0; }
}

/* ---------- Toast ---------- */
.ew-toast-wrap {
  position: fixed;
  left: 50%;
  bottom: 26px;
  transform: translateX(-50%);
  z-index: 60;
  display: flex;
  flex-direction: column;
  gap: 7px;
  align-items: center;
  pointer-events: none;
}
.ew-toast {
  font-size: 12.5px;
  padding: 8px 16px;
  border-radius: 8px;
  background: var(--dsw-alias-bg-overlay);
  color: var(--dsw-alias-label-primary);
  border: 1px solid var(--dsw-alias-border-l2);
  box-shadow: 0 10px 28px -14px rgba(0,0,0,.45);
  animation: ew-toast .2s cubic-bezier(.22,.61,.36,1) both;
  max-width: 72vw;
}
.ew-toast.is-ok  { border-color: var(--dsw-alias-state-success-primary); }
.ew-toast.is-err { border-color: var(--dsw-alias-state-error-primary); }
@keyframes ew-toast {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: none; }
}

/* ---------- 确认弹层 ---------- */
.ew-scrim {
  position: fixed;
  inset: 0;
  /* 层级要压过宿主 UI：DSH 自己的浮层 z-index 很高，
     之前用 70 会被盖住，表现为"点了按钮没反应"。 */
  z-index: 2147483000;
  background: rgba(0, 0, 0, .42);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  animation: ew-fade .14s ease both;
}
@keyframes ew-fade { from { opacity: 0; } to { opacity: 1; } }
.ew-dialog {
  width: min(400px, 100%);
  background: var(--dsw-alias-bg-overlay);
  border: 1px solid var(--dsw-alias-border-l2);
  border-radius: 12px;
  padding: 20px 20px 14px;
  box-shadow: 0 24px 56px -20px rgba(0, 0, 0, .5);
  animation: ew-dialog .2s cubic-bezier(.22,.61,.36,1) both;
}
@keyframes ew-dialog {
  from { opacity: 0; transform: translateY(8px) scale(.98); }
  to   { opacity: 1; transform: none; }
}
.ew-dialog-title {
  font-size: 14.5px;
  font-weight: 600;
  margin-bottom: 9px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.ew-dialog-title.is-danger { color: var(--dsw-alias-state-error-primary); }
.ew-dialog-body {
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--dsw-alias-label-secondary);
  margin-bottom: 16px;
  white-space: pre-wrap;
}
.ew-dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

/* ---------- 输入 ---------- */
.ew-input {
  font: inherit;
  font-size: 12.5px;
  padding: 5px 10px;
  width: 112px;
  border-radius: 7px;
  border: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-alias-bg-base);
  color: var(--dsw-alias-label-primary);
  transition: border-color .13s ease, box-shadow .13s ease;
}
.ew-input:focus {
  outline: none;
  border-color: var(--dsw-alias-brand-primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--dsw-alias-brand-primary) 12%, transparent);
}

/* ---------- 无障碍：减少动效 ---------- */
@media (prefers-reduced-motion: reduce) {
  .ew-inner, .ew-card, .ew-stat-num, .ew-layer, .ew-status,
  .ew-toast, .ew-dialog, .ew-empty { animation: none !important; }
  .ew-card, .ew-btn, .ew-row, .ew-row-entry, .ew-pick, .ew-search { transition: none !important; }
  .ew-skel { animation: none !important; }
}
`

    const LAYERS = [
      { key: 'character', label: '人物格', cn: '人物' },
      { key: 'world', label: '世界格', cn: '世界' },
      { key: 'timeline', label: '时间格', cn: '时间线' },
      { key: 'foreshadowing', label: '伏笔格', cn: '伏笔' },
      { key: 'item', label: '物品格', cn: '物品' },
      { key: 'style', label: '风格格', cn: '风格' },
    ]

    //: 作品库负责的格子（跟着作品走）
    const WORK_LAYERS = LAYERS.filter((l) => l.key !== 'style')
    //: 全局文学库负责的格子（跨作品共享）
    const GLOBAL_LAYERS = LAYERS.filter((l) => l.key === 'style')
    //: 特殊作用域标识：进入"全局文学库"视图
    const GLOBAL_SCOPE = '__global__'
    //: 特殊作用域标识：进入"技术库"视图
    const TECH_SCOPE = '__tech__'
    //: 特殊作用域标识：进入"技术断言"视图（同一 tech.db，只看 assertion 层）
    const ASSERT_SCOPE = '__assert__'

    const STATUS_LABEL = {
      planted: '已埋',
      reinforced: '已强化',
      resolved: '已回收',
      abandoned: '已废弃',
    }

    // ---- 样式（全部走宿主主题令牌，跟随明暗切换）----
    // 样式全部走 CSS 类名（见上方 CSS 常量），这里只保留少量动态样式
    const cx = (...names) => names.filter(Boolean).join(' ')

    function statusClassName(status) {
      if (status === 'resolved') return 'success'
      if (status === 'abandoned') return 'idle'
      if (status === 'reinforced') return 'brand'
      return 'warn'
    }

    const STATUS_COLOR = {
      resolved: 'var(--dsw-alias-state-success-primary)',
      abandoned: 'var(--dsw-alias-state-idle-primary)',
      reinforced: 'var(--dsw-alias-brand-primary)',
      planted: 'var(--dsw-alias-state-warn-primary)',
    }

    const STATUS_LABEL_CN = {
      planted: '已埋',
      reinforced: '已强化',
      resolved: '已回收',
      abandoned: '已废弃',
    }

    // ---- 小组件 ----
    function StatCard(props) {
      return h('div', { className: 'ew-card' },
        h('div', { className: 'ew-stat-num', style: { animationDelay: (props.delay || 0) + 'ms' } },
          String(props.value == null ? 0 : props.value)),
        h('div', { className: 'ew-stat-label' }, props.label))
    }

    function ForeshadowRow(props) {
      const fs = props.item
      const color = STATUS_COLOR[fs.status] || STATUS_COLOR.planted
      return h('div', { className: 'ew-row' },
        h('span', {
          className: 'ew-badge',
          style: { color, borderColor: color },
        }, STATUS_LABEL_CN[fs.status] || fs.status),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('div', { className: 'ew-row-name' },
            fs.id,
            h('span', { className: 'ew-sub', style: { marginLeft: 8, fontWeight: 400 } },
              '埋于 ' + (fs.planted_chapter || '—') + ' · 计划 ' + (fs.planned_resolve || '—') +
              (fs.resolved_chapter ? ' · 回收于 ' + fs.resolved_chapter : ''))),
          h('div', { className: 'ew-row-body' }, fs.content)))
    }

    function EntryRow(props) {
      const it = props.item
      return h('div', { className: 'ew-row' },
        h('span', { className: 'ew-badge' }, 'v' + it.version),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('div', { className: 'ew-row-name' },
            it.entity,
            it.chapter_id
              ? h('span', { className: 'ew-sub', style: { marginLeft: 8, fontWeight: 400 } }, it.chapter_id)
              : null),
          h('div', { className: 'ew-row-body' }, it.content)))
    }

    function Empty(props) {
      return h('div', { className: 'ew-empty' },
        props.icon ? h('div', { className: 'ew-empty-icon' }, props.icon) : null,
        h('div', null, props.text),
        props.hint ? h('div', { style: { fontSize: 12, marginTop: 6, opacity: .8 } }, props.hint) : null)
    }

    // 骨架屏
    function Skeleton() {
      const cards = []
      for (let i = 0; i < 6; i++) {
        cards.push(h('div', { key: i, className: 'ew-skel', style: { height: 76 } }))
      }
      return h('div', null,
        h('div', { className: 'ew-grid' }, cards),
        h('div', { className: 'ew-skel', style: { height: 42, marginBottom: 10 } }),
        h('div', { className: 'ew-skel', style: { height: 92 } }))
    }

    // Toast
    function Toast(props) {
      return h('div', { className: 'ew-toast-wrap' },
        (props.toasts || []).map(function (t) {
          return h('div', {
            key: t.id,
            className: cx('ew-toast', t.kind === 'ok' ? 'is-ok' : t.kind === 'err' ? 'is-err' : null),
          }, t.text)
        }))
    }

    // 确认弹层
    /**
     * 危险操作确认弹层。
     *
     * 二次确认机制：危险操作（删除/清除）必须**先勾选确认框**，
     * 「确认删除」按钮才会变为可用。这样即使误点，也要多走一步，
     * 避免手滑把整部作品的记忆清空。
     */
    function ConfirmDialog(props) {
      if (!props.open) return null
      const danger = props.danger !== false
      const armed = props.armed !== false
      const checked = !!props.checked
      const canConfirm = !armed || checked

      return h('div', {
        className: 'ew-scrim',
        onClick: function (e) { if (e.target === e.currentTarget) props.onCancel() },
      },
        h('div', { className: 'ew-dialog', role: 'dialog', 'aria-modal': 'true' },
          h('div', {
            className: danger ? 'ew-dialog-title is-danger' : 'ew-dialog-title',
          },
            danger ? '⚠ ' : '',
            props.title),
          h('div', { className: 'ew-dialog-body' }, props.body),

          // 二次确认：勾选框
          armed
            ? h('label', {
                style: {
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 9,
                  marginBottom: 16,
                  fontSize: 12.5,
                  lineHeight: 1.5,
                  cursor: 'pointer',
                  userSelect: 'none',
                  color: 'var(--dsw-alias-label-secondary)',
                },
              },
                h('input', {
                  type: 'checkbox',
                  checked: checked,
                  onChange: function (e) { props.onCheck && props.onCheck(e.target.checked) },
                  style: { marginTop: 2, cursor: 'pointer', flex: '0 0 auto' },
                }),
                h('span', null, props.confirmHint || '我已确认，知道此操作不可撤销'))
            : null,

          h('div', { className: 'ew-dialog-actions' },
            h('button', { className: 'ew-btn', onClick: props.onCancel }, '取消'),
            h('button', {
              className: danger ? 'ew-btn is-danger' : 'ew-btn is-primary',
              disabled: !canConfirm,
              onClick: function () { if (canConfirm) props.onConfirm() },
            }, props.confirmText || '确定'))))
    }

    function Panel() {
      const [data, setData] = useState(null)
      const [loading, setLoading] = useState(true)
      const [error, setError] = useState(null)
      const [busy, setBusy] = useState(null)
      // 面板不强制章节：留空 = 不做超期巡检，只展示已记录的伏笔状态。
      // 需要巡检时从工具（enderwriter_patrol）指定章节即可。
      const chapter = ''
      const [layer, setLayer] = useState('character')
      const [entries, setEntries] = useState(null)
      const [entriesLoading, setEntriesLoading] = useState(false)
      const [workOverride, setWorkOverride] = useState('')
      // 两级导航：viewingWork 为空 → 显示"选择作品"；非空 → 显示该作品详情。
      // 特殊值 GLOBAL_SCOPE 表示进入"全局文学库"。
      const [viewingWork, setViewingWork] = useState('')
      // 已展开的条目 entity 集合
      const [expanded, setExpanded] = useState({})
      const [toasts, setToasts] = useState([])
      const [confirm, setConfirm] = useState(null)
      // 版本历史弹层：{ layer, entity, loading, versions[], error }
      const [historyFor, setHistoryFor] = useState(null)
      // 编辑弹层：{ layer, entity, draft, saving }
      const [editing, setEditing] = useState(null)
      // 搜索词：在当前区块的条目里做本地过滤（名称 + 内容）
      const [search, setSearch] = useState('')

      /**
       * 按搜索词过滤条目。
       * 命中名称的排在前面，只命中正文的排在后面，保持原始顺序稳定。
       */
      const filterBySearch = function (items, pickName, pickBody) {
        const q = search.trim().toLowerCase()
        if (!q) return items
        const hitName = []
        const hitBody = []
        for (const it of items) {
          const name = String(pickName(it) || '').toLowerCase()
          const body = String(pickBody(it) || '').toLowerCase()
          if (name.includes(q)) hitName.push(it)
          else if (body.includes(q)) hitBody.push(it)
        }
        return hitName.concat(hitBody)
      }

      const currentWork = workOverride

      function toast(text, kind) {
        const id = Date.now() + Math.random()
        setToasts(function (prev) { return prev.concat([{ id, text, kind: kind || 'info' }]) })
        setTimeout(function () {
          setToasts(function (prev) { return prev.filter(function (t) { return t.id !== id }) })
        }, 2600)
      }

      const load = useCallback(async (chap) => {
        setLoading(true)
        setError(null)
        try {
          // 选择页 / 全局库视图不需要按作品取数，传空 work 即可
          // 特殊作用域（全局库 / 技术案例 / 技术断言）不按作品取数，
          // 传空让 Python 走默认解析
          const scopeWork =
            viewingWork === GLOBAL_SCOPE ||
            viewingWork === TECH_SCOPE ||
            viewingWork === ASSERT_SCOPE
              ? ''
              : viewingWork
          setData(await callHost('overview', { chapter: chap, work: scopeWork }))
        } catch (e) {
          setError('读取失败：' + (e && e.message ? e.message : e))
        } finally {
          setLoading(false)
        }
      }, [currentWork, viewingWork])

      useEffect(function () { load(chapter) }, [load, chapter])

      useEffect(function () {
        // 只在进入具体作品时才加载条目
        if (!viewingWork || viewingWork === GLOBAL_SCOPE) return undefined
        let alive = true
        setEntriesLoading(true)
        callHost('entries', { layer: layer, work: viewingWork })
          .then(function (j) { if (alive) { setEntries(j); setEntriesLoading(false) } })
          .catch(function () { if (alive) { setEntries(null); setEntriesLoading(false) } })
        return function () { alive = false }
      }, [layer, currentWork, viewingWork])

      const isEnabled = data ? data.enabled !== false : true
      const info = (data && data.info) || {}
      const panel = (data && data.panel) || {}
      const workBlock = panel.work_block || {}
      const globalBlock = panel.global_block || {}
      const worksList = panel.works || []
      const workStats = workBlock.stats || {}
      const globalStats = globalBlock.stats || {}
      const workRecords = workStats.records || {}
      const patrol = workBlock.patrol || {}
      const foreshadowings = workBlock.foreshadowings || []
      const lessons = globalBlock.lessons || []
      const techBlock = panel.tech_block || {}
      const techNotes = techBlock.notes || []
      // 同一个 tech.db 里混着两类东西：事故复盘（style 层）与可执行断言（assertion 层）。
      // UI 上分开成「技术案例」「技术断言」两个入口，语义完全不同。
      const techCases = techNotes.filter(function (n) { return (n.layer || 'style') !== 'assertion' })
      const techAssertions = techNotes.filter(function (n) { return n.layer === 'assertion' })
      const isSessionScoped = String(workBlock.name || '').indexOf('对话-') === 0

      const workTotal = useMemo(function () {
        return WORK_LAYERS.reduce(function (sum, l) { return sum + (workRecords[l.key] || 0) }, 0)
      }, [workRecords])

      function toggleEnabled() {
        const next = !isEnabled
        setBusy(next ? '正在启用…' : '正在停用…')
        callHost('setEnabled', { enabled: next })
          .then(function () {
            toast(next ? '末影箱已启用' : '末影箱已停用', 'ok')
            return load(chapter)
          })
          .catch(function (e) { toast('切换失败：' + (e.message || e), 'err') })
          .then(function () { setBusy(null) })
      }

      /**
       * 弹出清除确认。
       *
       * 二次确认：弹层里必须勾选确认框才能点「确认清除」。
       *
       * 注意 work 参数：必须用**当前正在浏览的作品**（viewingWork），
       * 而不是写目标 workOverride。之前传错导致后端收到空 work，
       * 直接返回"需要指定 work"，前端看起来就是"点了没反应"。
       */
      function askClear(target, label, scopeWork) {
        const scopeText =
          target === 'global'
            ? '将删除全局文学库里全部跨对话的写作经验。'
            : target === 'tech'
              ? '将删除技术库里的全部工程笔记。'
              : target === 'all'
                ? '将同时删除当前作品的记忆与全局文学库的全部经验。'
                : '将删除该区块里的人物、世界、时间线、伏笔、物品等全部记录。'

        // 技术库与全局库不依赖 work；作品库必须带上
        const needWork = target === 'work'
        const workArg = scopeWork || (viewingWork && viewingWork.indexOf('__') !== 0 ? viewingWork : '')

        if (needWork && !workArg) {
          toast('无法确定要清除哪个作品', 'err')
          return
        }

        setConfirm({
          // 必须显式 open:true，ConfirmDialog 用 props.open 判断是否渲染，
          // 少了这个字段弹层永远不出现，表现出来就是"点了没反应"。
          open: true,
          danger: true,
          armed: true,
          checked: false,
          title: '清除' + label,
          body:
            scopeText +
            '\n\n清除前会自动备份为 .bak-时间戳 文件，但界面上无法撤销。',
          confirmHint: '我已确认要清除' + label,
          confirmText: '确认清除',
          onCheck: function (v) {
            setConfirm(function (prev) {
              return prev ? Object.assign({}, prev, { checked: !!v }) : prev
            })
          },
          onConfirm: function () {
            setConfirm(null)
            setBusy('正在清除 ' + label + ' …')
            callHost('clear', { target: target, work: workArg })
              .then(function (res) {
                const n = (res && res.ok_count) || 0
                if (n > 0) toast(label + ' 已清除（' + n + ' 个库文件已备份）', 'ok')
                else toast(label + ' 本来是空的', 'ok')
                setEntries(null)
                setSearch('')
                load(chapter)
              })
              .catch(function (e) {
                toast('清除失败：' + (e && e.message ? e.message : e), 'err')
              })
              .then(function () { setBusy(null) })
          },
          onCancel: function () { setConfirm(null) },
        })
      }

      /**
       * 删除单条记忆。
       *
       * 词条是写作资产，所以同样走二次确认；
       * 正文里带上词条名和内容摘要，避免删错。
       */
      function askDeleteEntry(rowKey, layerName, entity, preview) {
        setConfirm({
          // key 表示"在对应的条目行内展开确认"，
          // 而不是显示在面板顶部——顶部会跑到用户视野之外。
          key: rowKey,
          open: true,
          danger: true,
          armed: true,
          checked: false,
          title: '删除词条「' + entity + '」',
          body:
            '图层：' + layerName + '\n' +
            (preview ? '内容：' + String(preview).slice(0, 120) + (String(preview).length > 120 ? '…' : '') + '\n\n' : '\n') +
            '删除后该词条不再参与检索。会先备份整个库为 .bak 文件。',
          confirmHint: '我已确认要删除「' + entity + '」',
          confirmText: '确认删除',
          onCheck: function (v) {
            setConfirm(function (prev) {
              return prev ? Object.assign({}, prev, { checked: !!v }) : prev
            })
          },
          onConfirm: function () {
            setConfirm(null)
            setBusy('正在删除「' + entity + '」…')
            callHost('forgetEntry', { layer: layerName, entity: entity, work: viewingWork })
              .then(function (res) {
                const removed = res && res.removed
                if (removed) toast('已删除「' + entity + '」', 'ok')
                else toast('没有找到「' + entity + '」，可能已被删除', 'err')
                setEntries(null)
                load(chapter)
              })
              .catch(function (e) {
                toast('删除失败：' + (e && e.message ? e.message : e), 'err')
              })
              .then(function () { setBusy(null) })
          },
          onCancel: function () { setConfirm(null) },
        })
      }

      /**
       * 打开版本历史。
       *
       * 每次覆写都会追加一个版本，旧内容永不丢失——
       * 所以 AI 可以放心改写设定，人随时能查「改成了什么、什么时候改的」。
       */
      function openHistory(key, layerName, entity) {
        // 再点一次收起；同时只展开一个
        setEditing(null)
        setHistoryFor({ key: key, layer: layerName, entity: entity, loading: true, versions: [] })
        callHost('history', { layer: layerName, entity: entity, work: viewingWork })
          .then(function (res) {
            setHistoryFor(function (prev) {
              // 期间用户可能已经切到别的条目，忽略过期的响应
              if (!prev || prev.key !== key) return prev
              return Object.assign({}, prev, {
                loading: false,
                versions: (res && res.versions) || [],
                currentVersion: (res && res.current_version) || 0,
              })
            })
          })
          .catch(function (e) {
            setHistoryFor(function (prev) {
              if (!prev || prev.key !== key) return prev
              return Object.assign({}, prev, {
                loading: false,
                versions: [],
                error: (e && e.message) || String(e),
              })
            })
          })
      }

      /** 回滚到某个历史版本：旧内容写成新版本，历史全部保留。 */
      function doRevert(layerName, entity, version) {
        setConfirm({
          open: true,
          danger: true,
          armed: true,
          checked: false,
          title: '回滚「' + entity + '」到 v' + version,
          body:
            '会把这个版本的内容**写成新的一版**，当前内容和后面的历史都保留，不会丢。\n\n' +
            '回滚本身也可以再回滚。',
          confirmHint: '我确认回滚到 v' + version,
          confirmText: '确认回滚',
          onCheck: function (v) {
            setConfirm(function (prev) {
              return prev ? Object.assign({}, prev, { checked: !!v }) : prev
            })
          },
          onConfirm: function () {
            setConfirm(null)
            setBusy('正在回滚「' + entity + '」…')
            callHost('revert', {
              layer: layerName,
              entity: entity,
              version: version,
              work: viewingWork,
            })
              .then(function (res) {
                toast('已回滚到 v' + version + '（新版本 v' + (res && res.new_version) + '）', 'ok')
                setHistoryFor(null)
                setEntries(null)
                load(chapter)
              })
              .catch(function (e) {
                toast('回滚失败：' + ((e && e.message) || e), 'err')
              })
              .then(function () { setBusy(null) })
          },
          onCancel: function () { setConfirm(null) },
        })
      }

      /**
       * 直接编辑一条记忆的正文（**原地展开**，不弹窗）。
       *
       * 保存会生成**新版本**，旧版本全部保留（可在「历史」里回看/回滚）。
       * 所以改错了不用怕——文字永远不会被覆盖掉。
       */
      function openEdit(key, layerName, entity, content) {
        // 同时只展开一个：点编辑会收起历史
        setHistoryFor(null)
        setEditing({
          key: key,
          layer: layerName,
          entity: entity,
          draft: String(content || ''),
          saving: false,
        })
      }

      function saveEdit() {
        const ed = editing
        if (!ed || ed.saving) return
        setEditing(Object.assign({}, ed, { saving: true }))
        callHost('update', {
          layer: ed.layer,
          entity: ed.entity,
          content: ed.draft,
          note: 'edit',
          work: viewingWork,
        })
          .then(function (res) {
            toast('已保存为 v' + ((res && res.version) || '?') + '（旧版本保留）', 'ok')
            setEditing(null)
            setEntries(null)
            load(chapter)
          })
          .catch(function (e) {
            toast('保存失败：' + ((e && e.message) || e), 'err')
            setEditing(Object.assign({}, ed, { saving: false }))
          })
      }

      /** 关闭弹层类组件（保留定义以兼容旧调用，实际已改为原地展开）。 */
      function EditDialog(props) {
        return null
      }

      /** 版本历史：已改为原地展开，不再弹层。 */
      function HistoryDialog(props) {
        return null
      }

      const header = h('div', { className: 'ew-head' },
        h('div', null,
          h('h2', { className: 'ew-title' }, '末影箱'),
          h('div', { className: 'ew-sub' },
            isEnabled
              ? (info.llamacpp && info.llamacpp.ready
                  ? '本地模型就绪 · ' + Math.round(info.llamacpp.model_mb || 0) + 'MB'
                  : '本地模型未就绪 · 走离线 Stub')
              : '已停用')),
        h('div', { className: 'ew-head-actions' },
          h('button', {
            className: cx('ew-btn', isEnabled ? '' : 'is-primary'),
            onClick: toggleEnabled,
            disabled: !!busy,
          }, isEnabled ? '停用末影箱' : '启用末影箱')))

      // 状态行：错误 / 忙碌 / 加载中，都没有则为 null
      const statusLine = error
        ? h('div', {
            className: 'ew-status is-error',
            style: { flexWrap: 'wrap', wordBreak: 'break-all', userSelect: 'text' },
          }, '⚠ ', error)
        : busy
          ? h('div', { className: 'ew-status' }, h('span', { className: 'ew-spinner' }), busy)
          : loading
            ? h('div', { className: 'ew-status' }, h('span', { className: 'ew-spinner' }), '正在读取末影箱…')
            : null

      // 停用态：只显示说明和启用按钮
      if (!isEnabled) {
        return h('div', { className: 'ew-root' },
          h('div', { className: 'ew-inner' },
            header,
            h(Empty, {
              icon: '⏸',
              text: '末影箱已停用',
              hint: '停用后不再记录写作数据；重新启用即可恢复原有记忆。',
            }),
            h(Toast, { toasts: toasts })))
      }

      // ============================================================
      // 两级导航
      //   viewingWork === ''           → 作品选择页
      //   viewingWork === GLOBAL_SCOPE → 全局文学库详情
      //   其它                          → 该作品的详情页
      // ============================================================
      const atPicker = !viewingWork
      const atGlobal = viewingWork === GLOBAL_SCOPE
      const atTech = viewingWork === TECH_SCOPE
      const atAssert = viewingWork === ASSERT_SCOPE
      const atWork = !atPicker && !atGlobal && !atTech && !atAssert

      // 分层标题：标题 + 名称 + 标签 + 说明 + 清除按钮
      const layHead = function (opts) {
        return h('div', { className: 'ew-layer-head' },
          h('div', { style: { flex: 1 } },
            h('div', { className: 'ew-layer-title' },
              opts.title,
              opts.name ? h('span', { className: 'ew-layer-name' }, opts.name) : null,
              opts.tag ? h('span', { className: 'ew-tag' }, opts.tag) : null),
            h('div', { className: 'ew-layer-hint' }, opts.hint)),
          opts.clearLabel
            ? h('button', {
                className: 'ew-btn is-danger',
                disabled: !!busy || !opts.canClear,
                onClick: opts.onClear,
              }, opts.clearLabel)
            : null)
      }

      // ---- 返回按钮 ----
      const backBar = h('div', {
        style: { display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 },
      },
        h('button', {
          className: 'ew-btn',
          onClick: function () { setViewingWork('') },
        }, '← 所有内容'),
        h('span', { className: 'ew-sub' },
          atGlobal
            ? '全局文学库'
            : atTech
              ? '技术案例'
              : atAssert
                ? '技术断言'
                : (workBlock.name || viewingWork)))

      // ---- 作品选择页 ----
      const pickerItem = function (opts) {
        return h('button', {
          key: opts.key,
          className: 'ew-pick',
          onClick: opts.onClick,
        },
          h('div', { className: 'ew-pick-icon' }, opts.icon || '·'),
          h('div', { className: 'ew-pick-main' },
            h('div', { className: 'ew-pick-title' },
              opts.title,
              opts.tag ? h('span', { className: 'ew-tag' }, opts.tag) : null),
            h('div', { className: 'ew-pick-sub' }, opts.sub)),
          h('div', { className: 'ew-pick-count' }, String(opts.count == null ? 0 : opts.count)))
      }

      // ---- 分组标题 ----
      const groupHead = function (title, hint) {
        return h('div', { className: 'ew-group-head' },
          h('div', { className: 'ew-group-title' }, title),
          hint ? h('div', { className: 'ew-group-hint' }, hint) : null)
      }

      const picker = h('div', { style: { marginTop: 4 } },
        // ============ 文学大库 ============
        groupHead('文学大库', '人物 / 世界 / 时间线 / 伏笔 / 物品 + 写作经验'),
        h('div', { className: 'ew-pick-list' },
          // 当前对话
          pickerItem({
            key: 'session',
            icon: '◈',
            title: isSessionScoped ? '本对话' : '本作品',
            tag: isSessionScoped ? '独立' : '共享',
            sub: isSessionScoped
              ? '人物 / 世界 / 时间线 / 伏笔 / 物品 · 只属于这个对话'
              : '人物 / 世界 / 时间线 / 伏笔 / 物品',
            count: workTotal,
            onClick: function () { setViewingWork(workBlock.name || currentWork) },
          }),
          // 其它作品
          worksList
            .filter(function (w) {
              const nm = w.name || w.id
              return !(w.is_current && isSessionScoped)
            })
            .map(function (w) {
              const nm = w.name || w.id
              return pickerItem({
                key: w.id,
                icon: '▤',
                title: nm,
                sub: '作品库',
                count: w.total,
                onClick: function () { setViewingWork(nm) },
              })
            }),
          // 全局文学库
          pickerItem({
            key: 'global',
            icon: '✦',
            title: '全局文学库',
            tag: '跨作品',
            sub: '写作手法 / 叙事节奏 / 风格选型 / 经验教训',
            count: globalStats.total_records || 0,
            onClick: function () { setViewingWork(GLOBAL_SCOPE) },
          })),

        // ============ 代码大库 ============
        groupHead('代码大库', '技术案例 + 可执行断言（跨项目通用）'),
        h('div', { className: 'ew-pick-list' },
          techBlock.exists
            ? pickerItem({
                key: 'tech',
                icon: '⚙',
                title: '技术案例',
                tag: '跨项目',
                sub: '事故复盘：发生了什么、怎么定位、怎么修',
                count: techCases.length,
                onClick: function () { setViewingWork(TECH_SCOPE) },
              })
            : null,
          techBlock.exists
            ? pickerItem({
                key: 'assert',
                icon: '⛨',
                title: '技术断言',
                tag: '护栏',
                sub: '写代码/动手前会自动命中的可判定规则',
                count: techAssertions.length,
                onClick: function () { setViewingWork(ASSERT_SCOPE) },
              })
            : null))

      // ---- 全局文学库详情 ----
      // ============================================================
      // 统一详情视图：三个区块（作品 / 全局文学 / 技术）用同一套结构
      //   头部（标题+说明+清除） → 搜索框 → 统计 → 条目列表
      // 这样点进任何一个区块，界面骨架都一致。
      // ============================================================
      const searchBox = function (placeholder) {
        return h('div', { className: 'ew-search' },
          h('span', { className: 'ew-search-icon' }, '⌕'),
          h('input', {
            className: 'ew-search-input',
            type: 'search',
            value: search,
            placeholder: placeholder || '搜索名称或内容…',
            onChange: function (e) { setSearch(e.target.value) },
            onKeyDown: function (e) { if (e.key === 'Escape') setSearch('') },
          }),
          search
            ? h('button', {
                className: 'ew-search-clear',
                onClick: function () { setSearch('') },
                title: '清空搜索',
              }, '×')
            : null)
      }

      // 条目渲染：名称 + 徽标 + 摘要 + 展开
      const entryRow = function (opts) {
        const key = opts.key
        const open = !!expanded[key]
        const body = String(opts.body || '')
        const long = body.length > 180
        // 原地展开：编辑 / 历史都在这一行下面展开，不用遮罩弹层。
        // 之前用 position:fixed 的 modal 在宿主面板里不显示（层级/包含块问题），
        // 改成正常文档流后必然可见。
        const ed = editing && editing.key === key ? editing : null
        const hf = historyFor && historyFor.key === key ? historyFor : null
        // 删除确认也在这一行内展开——放在面板顶部会跑到视野外
        const cf = confirm && confirm.key === key ? confirm : null

        return h('div', {
          key: key,
          className: 'ew-row-entry',
          style: (ed || hf || cf) ? { display: 'block', background: 'var(--dsw-alias-bg-layer-1)' } : null,
        },
          h('div', { style: { display: 'flex', alignItems: 'flex-start', gap: 10 } },
            h('span', { className: 'ew-badge' }, opts.badge),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('div', { className: 'ew-row-name' },
                opts.name,
                opts.meta
                  ? h('span', { className: 'ew-sub', style: { fontWeight: 400 } }, opts.meta)
                  : null,
                // 已回收归档 = 曾被检索进写作上下文（不删内容，只标记）
                opts.archived
                  ? h('span', {
                      className: 'ew-badge',
                      title: opts.hitCount
                        ? '曾被检索 ' + opts.hitCount + ' 次' + (opts.lastHit ? '，最近 ' + opts.lastHit : '')
                        : '曾被检索进写作上下文',
                      style: {
                        color: 'var(--dsw-alias-state-success-primary)',
                        background: 'transparent',
                        border: '1px solid color-mix(in srgb, var(--dsw-alias-state-success-primary) 45%, transparent)',
                      },
                    }, '已回收')
                  : null),

              // 删除确认 → 在这一行内展开；编辑中 → 文本框；否则正文
              cf
                ? h('div', {
                    style: {
                      marginTop: 8,
                      padding: '12px 14px',
                      borderRadius: 9,
                      border: '1px solid var(--dsw-alias-state-error-primary)',
                      background: 'color-mix(in srgb, var(--dsw-alias-state-error-primary) 8%, transparent)',
                    },
                  },
                    h('div', {
                      className: 'ew-dialog-title is-danger',
                      style: { marginBottom: 8, fontSize: 13.5 },
                    }, '⚠ ', cf.title),
                    h('div', {
                      className: 'ew-dialog-body',
                      style: { marginBottom: 10, whiteSpace: 'pre-wrap', fontSize: 12.5 },
                    }, cf.body),
                    h('label', {
                      style: {
                        display: 'flex',
                        alignItems: 'flex-start',
                        gap: 8,
                        marginBottom: 10,
                        fontSize: 12.5,
                        cursor: 'pointer',
                        userSelect: 'none',
                        color: 'var(--dsw-alias-label-secondary)',
                      },
                    },
                      h('input', {
                        type: 'checkbox',
                        checked: !!cf.checked,
                        onChange: function (e) { cf.onCheck && cf.onCheck(e.target.checked) },
                        style: { marginTop: 2, cursor: 'pointer', flex: '0 0 auto' },
                      }),
                      h('span', null, cf.confirmHint || '我已确认，知道此操作不可撤销')),
                    h('div', { style: { display: 'flex', gap: 8, justifyContent: 'flex-end' } },
                      h('button', { className: 'ew-btn', onClick: cf.onCancel }, '取消'),
                      h('button', {
                        className: 'ew-btn is-danger',
                        disabled: !cf.checked,
                        onClick: function () { if (cf.checked) cf.onConfirm() },
                      }, cf.confirmText || '确认删除')))
                : ed
                ? h('textarea', {
                    className: 'ew-input',
                    style: {
                      width: '100%',
                      minHeight: 200,
                      fontFamily: 'inherit',
                      fontSize: 13,
                      lineHeight: 1.65,
                      resize: 'vertical',
                      padding: '10px 12px',
                      boxSizing: 'border-box',
                      marginTop: 6,
                    },
                    value: ed.draft,
                    onChange: function (e) { opts.onEditChange(e.target.value) },
                  })
                : h('div', { className: open ? 'ew-row-body ew-body-full' : 'ew-row-body' },
                    open || !long ? body : body.slice(0, 180) + '…'),

              // 历史版本列表（原地展开）
              hf
                ? h('div', {
                    style: {
                      marginTop: 10,
                      paddingTop: 10,
                      borderTop: '1px solid var(--dsw-alias-border-l1)',
                    },
                  },
                    h('div', { className: 'ew-section-title', style: { margin: '0 0 8px' } },
                      '版本历史 · ' + (hf.entity || '')),
                    hf.loading
                      ? h('div', { className: 'ew-status' }, h('span', { className: 'ew-spinner' }), '正在读取…')
                      : hf.error
                        ? h('div', { className: 'ew-dialog-body' }, '读取失败：' + hf.error)
                        : (hf.versions || []).map(function (v) {
                            return h('div', {
                              key: v.version,
                              className: 'ew-row',
                              style: v.current ? { background: 'var(--dsw-alias-bg-layer-2)' } : null,
                            },
                              h('span', { className: 'ew-badge' }, 'v' + v.version),
                              h('div', { style: { flex: 1, minWidth: 0 } },
                                h('div', { className: 'ew-row-name' },
                                  v.current ? '当前版本' : (v.change || '历史版本'),
                                  h('span', { className: 'ew-sub', style: { fontWeight: 400 } },
                                    v.chars + ' 字 · ' + (v.updated_at || ''))),
                                h('div', { className: 'ew-row-body', style: { whiteSpace: 'pre-wrap' } },
                                  String(v.content || '').slice(0, 200) +
                                    (String(v.content || '').length > 200 ? '…' : '')),
                                v.current
                                  ? null
                                  : h('button', {
                                      className: 'ew-btn',
                                      style: { marginTop: 6, fontSize: 12, padding: '3px 10px' },
                                      onClick: function () {
                                        opts.onRevert(v.version)
                                      },
                                    }, '回滚到此版本')))
                          }))
                : null,

              // 行内动作（删除确认展开时隐藏，确认块里有自己的按钮）
              cf ? null : h('div', { style: { display: 'flex', gap: 6, marginTop: 6, flexWrap: 'wrap' } },
                ed
                  ? [
                      h('span', { key: 'cnt', className: 'ew-sub', style: { marginRight: 4 } }, ed.draft.length + ' 字'),
                      h('button', {
                        key: 'save',
                        className: 'ew-btn is-primary',
                        style: { fontSize: 12, padding: '3px 12px' },
                        disabled: ed.saving,
                        onClick: opts.onEditSave,
                      }, ed.saving ? '保存中…' : '保存'),
                      h('button', {
                        key: 'cancel',
                        className: 'ew-btn',
                        style: { fontSize: 12, padding: '3px 10px' },
                        disabled: ed.saving,
                        onClick: opts.onEditCancel,
                      }, '取消'),
                    ]
                  : [
                      long
                        ? h('button', {
                            key: 'exp',
                            className: 'ew-btn',
                            style: { fontSize: 12, padding: '3px 10px' },
                            onClick: function () {
                              setExpanded(function (prev) {
                                const next = Object.assign({}, prev)
                                if (next[key]) delete next[key]
                                else next[key] = true
                                return next
                              })
                            },
                          }, open ? '收起' : '展开全文')
                        : null,
                      opts.onEdit
                        ? h('button', {
                            key: 'edit',
                            className: 'ew-btn',
                            style: { fontSize: 12, padding: '3px 10px' },
                            title: '直接编辑正文（保存为新版本）',
                            onClick: opts.onEdit,
                          }, '编辑')
                        : null,
                      opts.onHistory
                        ? h('button', {
                            key: 'hist',
                            className: 'ew-btn',
                            style: { fontSize: 12, padding: '3px 10px' },
                            title: '查看历史版本，可回滚',
                            onClick: opts.onHistory,
                          }, hf ? '收起历史' : '历史 v' + (opts.version || 1))
                        : null,
                      opts.onDelete
                        ? h('button', {
                            key: 'del',
                            className: 'ew-btn is-danger',
                            style: { fontSize: 12, padding: '3px 10px' },
                            title: '删除这条记忆',
                            onClick: opts.onDelete,
                          }, '删除')
                        : null,
                    ].filter(Boolean)))))
      }

      // 搜索结果为空时的统一空态
      const searchEmpty = h(Empty, {
        icon: '⌕',
        text: '没有匹配「' + search.trim() + '」的条目',
        hint: '可以换个关键词，或点上方 × 清空搜索',
      })

      // ---- 全局文学库详情 ----
      const globalItems = filterBySearch(
        lessons,
        function (l) { return l.entity },
        function (l) { return l.content },
      )
      const globalDetail = h('div', null,
        h('div', { className: 'ew-layer is-global' },
          layHead({
            title: '全局文学库',
            name: '',
            tag: '跨对话',
            hint: '写作手法 / 叙事节奏 / 风格选型 / 经验教训 —— 跨所有对话共享。',
            clearLabel: '清除全部经验',
            canClear: globalBlock.exists,
            onClear: function () { askClear('global', '全局文学库') },
          }),
          globalBlock.exists
            ? h('div', null,
                searchBox('搜索写作经验…'),
                h('div', { className: 'ew-grid' },
                  h(StatCard, { label: '经验条数', value: globalStats.total_records, delay: 0 }),
                  h(StatCard, { label: '版本历史', value: globalStats.history, delay: 40 })),
                globalItems.length
                  ? h('div', null, globalItems.map(function (l) {
                      return entryRow({
                        key: 'g:' + l.entity,
                        badge: 'v' + l.version,
                        name: l.entity,
                        body: l.content,
                        onDelete: function () {
                          askDeleteEntry('g:' + l.entity, 'style', l.entity, l.content)
                        },
                      })
                    }))
                  : (search.trim()
                      ? searchEmpty
                      : h(Empty, { icon: '·', text: '还没有积累写作经验' })))
            : h(Empty, { icon: '✦', text: '全局文学库还没建立' })))

      // ---- 代码大库：技术案例 / 技术断言 ----
      //
      // 两者存在同一个 tech.db 里，靠 layer 区分：
      //   style      → 事故复盘（叙述，供人读）
      //   assertion  → 可执行断言（规则，供工具匹配）
      // UI 上是两个入口，因为语义完全不同：一个用来"查过去"，一个用来"防未来"。
      const showingAssert = atAssert
      const codeItems = showingAssert ? techAssertions : techCases
      const codeFiltered = filterBySearch(
        codeItems,
        function (n) { return n.entity },
        function (n) { return n.content },
      )

      // 断言多显示一行「何时触发」：代码特征 vs 任务意图
      const assertionRow = function (n) {
        const body = String(n.content || '')
        return entryRow({
          key: 'assert:' + n.entity,
          badge: 'v' + (n.version || 1),
          name: n.entity,
          meta: n.severity ? String(n.severity) : '',
          body: body,
          onDelete: function () {
            askDeleteEntry('assert:' + n.entity, 'assertion', n.entity, body)
          },
        })
      }

      const codeDetail = h('div', null,
        h('div', { className: 'ew-layer is-tech' },
          layHead({
            title: showingAssert ? '技术断言' : '技术案例',
            name: '',
            tag: showingAssert ? '护栏' : '跨项目',
            hint: showingAssert
              ? '可机械判定的规则。写代码时会命中；也可用 enderwriter_guard 主动查。'
              : '事故复盘：发生了什么、怎么定位、怎么修。断言由这些案例提炼而来。',
            clearLabel: showingAssert ? '' : '清除全部技术笔记',
            canClear: showingAssert ? false : techNotes.length > 0,
            onClear: function () { if (!showingAssert) askClear('tech', '技术库') },
          }),
          codeItems.length
            ? h('div', null,
                searchBox(showingAssert ? '搜索断言（规则 / 修法）…' : '搜索技术案例…'),
                h('div', { className: 'ew-grid' },
                  h(StatCard, {
                    label: showingAssert ? '断言条数' : '案例条数',
                    value: codeItems.length,
                    delay: 0,
                  }),
                  showingAssert
                    ? h(StatCard, {
                        label: '高危（high）',
                        value: codeItems.filter(function (n) { return n.content && n.content.length > 0 }).length,
                        delay: 40,
                      })
                    : null),
                codeFiltered.length
                  ? h('div', null, codeFiltered.map(showingAssert ? assertionRow : function (n) {
                      return entryRow({
                        key: 'tech:' + n.entity,
                        badge: n.layer || 'style',
                        name: n.entity,
                        body: n.content,
                        onDelete: function () {
                          askDeleteEntry('tech:' + n.entity, n.layer || 'style', n.entity, n.content)
                        },
                      })
                    }))
                  : (search.trim()
                      ? searchEmpty
                      : h(Empty, {
                          icon: '·',
                          text: showingAssert ? '还没有断言' : '还没有技术案例',
                        })))
            : h(Empty, {
                icon: '·',
                text: showingAssert ? '还没有断言' : '还没有技术案例',
                hint: showingAssert
                  ? '用 enderwriter_assert 写入，或让我从案例里提炼'
                  : '踩过的坑记下来，之后可以提炼成断言',
              })))

      // 兼容旧变量名
      const techDetail = codeDetail

      // ---- 作品详情（同样结构）----
      const meta = workBlock.exists ? workBlock : null
      const workItems = filterBySearch(
        (entries && entries.items) || [],
        function (it) { return it.entity },
        function (it) { return it.content },
      )
      const entryList = entriesLoading
        ? h('div', null,
            h('div', { className: 'ew-skel', style: { height: 42, marginBottom: 8 } }),
            h('div', { className: 'ew-skel', style: { height: 42 } }))
        : workItems.length
          ? h('div', null, workItems.map(function (it) {
              const body = String(it.content || '')
              const u = it.usage || {}
              const rowKey = 'w:' + it.entity
              return entryRow({
                key: rowKey,
                badge: 'v' + (it.version || 1),
                name: it.entity,
                meta: (it.chars || body.length) + ' 字' + (it.chapter_id ? ' · ' + it.chapter_id : ''),
                body: body,
                version: it.version || 1,
                archived: !!u.archived,
                hitCount: u.hit_count,
                lastHit: u.last_hit_at,
                onEdit: function () { openEdit(rowKey, layer, it.entity, body) },
                onEditChange: function (v) {
                  setEditing(function (prev) {
                    return prev && prev.key === rowKey
                      ? Object.assign({}, prev, { draft: v })
                      : prev
                  })
                },
                onEditSave: saveEdit,
                onEditCancel: function () { setEditing(null) },
                onHistory: function () {
                  if (historyFor && historyFor.key === rowKey) setHistoryFor(null)
                  else openHistory(rowKey, layer, it.entity)
                },
                onRevert: function (v) { doRevert(layer, it.entity, v) },
                onDelete: function () {
                  askDeleteEntry(rowKey, layer, it.entity, body)
                },
              })
            }))
          : (search.trim()
              ? searchEmpty
              : h(Empty, { icon: '·', text: '这个格子还是空的' }))

      const workDetail = h('div', null,
        h('div', { className: 'ew-layer' },
          layHead({
            title: isSessionScoped ? '本对话' : '作品',
            name: isSessionScoped ? '' : (workBlock.name || ''),
            tag: isSessionScoped ? '独立' : '共享',
            hint: isSessionScoped
              ? '人物 / 世界 / 时间线 / 伏笔 / 物品 —— 只属于这个对话。'
              : '人物 / 世界 / 时间线 / 伏笔 / 物品 —— 多个对话可共享。',
            clearLabel: isSessionScoped ? '清除本对话记忆' : '清除本作品',
            canClear: !!meta,
            onClear: function () {
              askClear('work', isSessionScoped ? '本对话的记忆' : '作品库「' + workBlock.name + '」')
            },
          }),

          meta
            ? h('div', null,
                searchBox('搜索' + (isSessionScoped ? '本对话' : '本作品') + '的记忆…'),
                h('div', { className: 'ew-grid' },
                  h(StatCard, { label: '记录总数', value: workTotal, delay: 0 }),
                  h(StatCard, { label: '版本历史', value: workStats.history, delay: 40 }),
                  h(StatCard, { label: '倒排词条', value: workStats.terms, delay: 80 }),
                  h(StatCard, { label: '关系边', value: workStats.edges, delay: 120 }),
                  h(StatCard, { label: '未回收伏笔', value: workStats.open_foreshadowings, delay: 160 })),

                h('div', { className: 'ew-section-title' }, '格子分布'),
                h('div', { className: 'ew-grid' },
                  WORK_LAYERS.map(function (l, i) {
                    return h(StatCard, {
                      key: l.key,
                      label: l.label,
                      value: workRecords[l.key] || 0,
                      delay: i * 35,
                    })
                  })),

                patrol.chapter
                  ? h('div', null,
                      h('div', { className: 'ew-section-title' },
                        '伏笔巡检 · ' + patrol.chapter,
                        (patrol.items || []).length
                          ? h('span', { className: 'ew-badge', style: { color: 'var(--dsw-alias-state-warn-primary)', borderColor: 'var(--dsw-alias-state-warn-primary)' } },
                              (patrol.items || []).length + ' 项需处理')
                          : h('span', { className: 'ew-badge', style: { color: 'var(--dsw-alias-state-success-primary)', borderColor: 'var(--dsw-alias-state-success-primary)' } },
                              '一切正常')),
                      (patrol.items || []).length === 0
                        ? h(Empty, { icon: '✓', text: '没有超期或陈旧的伏笔' })
                        : h('div', null, (patrol.items || []).map(function (it, i) {
                            return h('div', { key: i, className: 'ew-row' },
                              h('span', {
                                className: 'ew-badge',
                                style: {
                                  color: it.type === 'overdue'
                                    ? 'var(--dsw-alias-state-error-primary)'
                                    : 'var(--dsw-alias-state-warn-primary)',
                                  borderColor: it.type === 'overdue'
                                    ? 'var(--dsw-alias-state-error-primary)'
                                    : 'var(--dsw-alias-state-warn-primary)',
                                },
                              }, it.type === 'overdue' ? '超期' : it.type === 'stale' ? '陈旧' : it.type),
                              h('div', { style: { flex: 1, minWidth: 0 } },
                                h('div', { className: 'ew-row-name' }, it.id),
                                h('div', { className: 'ew-row-body' }, it.suggestion || '')))
                          })))
                  : null,

                h('div', { className: 'ew-section-title' }, '全部伏笔 · ' + foreshadowings.length),
                foreshadowings.length
                  ? h('div', null, foreshadowings.map(function (fs) {
                      return h(ForeshadowRow, { key: fs.id, item: fs })
                    }))
                  : h(Empty, { icon: '·', text: '还没有记录伏笔' }),

                h('div', { className: 'ew-section-title' },
                  '浏览条目',
                  search.trim()
                    ? h('span', { className: 'ew-badge' },
                        workItems.length + ' / ' + ((entries && entries.items) || []).length)
                    : null),
                h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 } },
                  WORK_LAYERS.map(function (l) {
                    return h('button', {
                      key: l.key,
                      className: cx('ew-btn', l.key === layer ? 'is-active' : ''),
                      onClick: function () { setLayer(l.key) },
                    }, l.cn)
                  })),
                entryList)
            : h(Empty, {
                icon: '✦',
                text: '这个区块还没有记忆',
                hint: '写作时我会自动把人物、设定、伏笔记进来；也可以直接让我记。',
              })))

      /**
       * 确认条（原地显示，不弹层）。
       *
       * 之前用 position:fixed 的遮罩，在宿主面板里渲染了却看不见——
       * 表现出来就是"点了按钮没反应"。改成常驻文档流的确认条，必然可见。
       *
       * 危险操作仍需勾选确认框才能提交，防止误点。
       */
      const confirmBar = (confirm && !confirm.key) ? (function () {
        const c = confirm
        const armed = c.armed !== false
        const checked = !!c.checked
        const canConfirm = !armed || checked
        return h('div', {
          style: {
            margin: '0 0 16px',
            padding: '14px 16px',
            borderRadius: 10,
            border: '1px solid var(--dsw-alias-state-error-primary)',
            background: 'color-mix(in srgb, var(--dsw-alias-state-error-primary) 8%, transparent)',
          },
        },
          h('div', {
            className: 'ew-dialog-title is-danger',
            style: { marginBottom: 8 },
          }, '⚠ ', c.title),
          h('div', {
            className: 'ew-dialog-body',
            style: { marginBottom: 12, whiteSpace: 'pre-wrap' },
          }, c.body),
          armed
            ? h('label', {
                style: {
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 9,
                  marginBottom: 12,
                  fontSize: 12.5,
                  lineHeight: 1.5,
                  cursor: 'pointer',
                  userSelect: 'none',
                  color: 'var(--dsw-alias-label-secondary)',
                },
              },
                h('input', {
                  type: 'checkbox',
                  checked: checked,
                  onChange: function (e) { c.onCheck && c.onCheck(e.target.checked) },
                  style: { marginTop: 2, cursor: 'pointer', flex: '0 0 auto' },
                }),
                h('span', null, c.confirmHint || '我已确认，知道此操作不可撤销'))
            : null,
          h('div', { style: { display: 'flex', gap: 8, justifyContent: 'flex-end' } },
            h('button', { className: 'ew-btn', onClick: c.onCancel }, '取消'),
            h('button', {
              className: 'ew-btn is-danger',
              disabled: !canConfirm,
              onClick: function () { if (canConfirm) c.onConfirm() },
            }, c.confirmText || '确定')))
      })() : null

      return h('div', { className: 'ew-root' },
        h('div', { className: 'ew-inner' },
          header,
          statusLine,

          h('div', { style: { display: 'flex', gap: 10, alignItems: 'center', margin: '16px 0 10px' } },
            h('button', { className: 'ew-btn', onClick: function () { load(chapter) } }, '刷新'),
            h('span', { className: 'ew-sub' }, '每次打开面板会自动更新')),

          // 确认条放在内容之前：删除/清除的确认要一眼看到
          confirmBar,

          atPicker
            ? (loading && !data ? h(Skeleton) : picker)
            : h('div', null,
                backBar,
                loading && !data
                  ? h(Skeleton)
                  : (atGlobal
                    ? globalDetail
                    : (atTech || atAssert)
                      ? codeDetail
                      : workDetail)),

          h(Toast, { toasts: toasts })))
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
        return Panel()
      } catch (err) {
        const msg = String(err && err.message ? err.message : err)
        return h('div', { className: 'ew-root' },
          h('div', { className: 'ew-inner' },
            h('div', { className: 'ew-section-title' }, '末影箱面板渲染出错'),
            h('div', {
              className: 'ew-row-body',
              style: { color: 'var(--dsw-alias-state-error-primary)' },
            }, msg),
            h('div', { className: 'ew-row-body', style: { marginTop: 12 } },
              '工具（检索 / 巡检 / 写作）不受影响，仍可在对话里使用。')))
      }
    }

    return {
      // 只声明确实存在的服务。
      // Cordis 里 inject 写一个宿主不提供的服务，插件会**永久 pending**，
      // 连插槽都不注册（侧边栏图标直接消失）——这比取不到数据更糟。
      // host / styles 改用 ctx.get 软获取：拿不到就打日志，不影响注册。
      inject: ['slots'],
      apply(ctx) {
        // 整个 apply 包在 try 里：Client 插件激活期抛异常会拖垮宿主 UI，
        // 宁可这个面板不显示，也不能影响 DSH 本身。
        try {
          // 兜底探测 RPC / styles（factory 阶段拿不到的，这里补）
          probeCtx(ctx)
          if (!hostRpc) {
            console.warn('[enderwriter] 未取到 host RPC，探测记录：', probeLog.join(' ; '))
          }

          // 注入样式表。
          //
          // 两条路：
          //   1. styles 服务（最佳，随插件卸载自动清理）
          //   2. 直接往 <head> 插 <style>（兜底，样式一样生效，
          //      只是卸载时要自己摘掉）
          // 之前只走第 1 条，styles 拿不到时整个面板变成浏览器默认样式，
          // 所以这里必须有兜底。
          try {
            if (stylesRpc && typeof stylesRpc.insert === 'function') {
              ctx.effect(() => stylesRpc.insert(CSS))
            } else if (typeof document !== 'undefined' && document.head) {
              const el = document.createElement('style')
              el.id = 'enderwriter-panel-css'
              el.textContent = CSS
              const old = document.getElementById('enderwriter-panel-css')
              if (old) old.remove()
              document.head.appendChild(el)
              ctx.effect(() => () => {
                const cur = document.getElementById('enderwriter-panel-css')
                if (cur) cur.remove()
              })
              console.warn('[enderwriter] styles 服务不可用，已用原生 <style> 兜底注入')
            } else {
              console.warn('[enderwriter] 无法注入样式：既没有 styles 服务也没有 document')
            }
          } catch (err) {
            console.warn('[enderwriter] 样式注入失败：', err)
          }

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
        } catch (err) {
          console.error('[enderwriter] 面板注册失败（不影响 DSH 其他功能）：', err)
        }
      },
    }
  },
})
