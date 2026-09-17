/**
 * Offline contract check for desktop/plugin.js.
 *
 * Loads the plugin with a stubbed @hermes/plugin-sdk / react, calls register()
 * against a mock ctx, then actually invokes every render() and every palette
 * `run()` to catch ReferenceError / bad-prop / missing-import bugs without
 * launching Electron.
 */

import { mkdirSync, writeFileSync, rmSync, copyFileSync } from 'node:fs'
import { join as joinPath } from 'node:path'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const ROOT = joinPath(tmpdir(), 'hermes-localsend-desktop-check')
const SRC = process.argv[2] || 'desktop/plugin.js'
rmSync(ROOT, { recursive: true, force: true })
mkdirSync(join(ROOT, 'node_modules', '@hermes'), { recursive: true })
mkdirSync(join(ROOT, 'node_modules', 'react'), { recursive: true })

const pkg = (name, body) => {
  const dir = join(ROOT, 'node_modules', name)
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'package.json'), JSON.stringify({ name, type: 'module', main: 'index.js' }))
  writeFileSync(join(dir, 'index.js'), body)
}

// Minimal but honest stubs: every export the plugin may pull must exist, and
// anything missing should fail loudly rather than silently render nothing.
pkg('@hermes/plugin-sdk', `
const fn = () => () => null
export const host = { notify: () => 'id', notifyError: () => {}, navigate: () => {}, request: async () => ({}), log: () => {} }
export const cn = (...a) => a.filter(Boolean).join(' ')
export const icons = new Proxy({}, { get: () => function Icon() { return null } })
export const useQuery = () => ({ data: globalThis.__LS_STATUS__ ?? {}, isLoading: false, error: null, refetch: async () => {} })
export const useValue = (a) => (a && a.get ? a.get() : undefined)
export const atom = (v) => ({ get: () => v, set: () => {} })
export const Button = function Button() { return null }
export const StatusDot = function StatusDot() { return null }
export const Badge = function Badge() { return null }
export const PANES_AREA = 'panes'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const PALETTE_AREA = 'palette'
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const KEYBINDS_AREA = 'keybinds'
export default { useQuery, host, cn, icons }
`)

pkg('react', `
export const useState = (initial) => [typeof initial === 'function' ? initial() : initial, () => {}]
export const useEffect = () => {}
export const useMemo = (f) => f()
export const useRef = (v) => ({ current: v })
export default { useState, useEffect, useMemo, useRef }
`)
// react/jsx-runtime is imported as a subpath, so react needs an exports map.
writeFileSync(
  join(ROOT, 'node_modules', 'react', 'package.json'),
  JSON.stringify({ name: 'react', type: 'module', main: 'index.js',
                   exports: { '.': './index.js', './jsx-runtime': './jsx-runtime.js' } }),
)

writeFileSync(join(ROOT, 'node_modules', 'react', 'jsx-runtime.js'), `
export const jsx = (type, props, key) => ({ $$typeof: 'element', type, props: props ?? {}, key })
export const jsxs = jsx
export const Fragment = Symbol.for('react.fragment')
`)

// ---- drive the plugin ------------------------------------------------------
// The plugin must sit inside the stub tree so Node resolves the stubbed
// @hermes/plugin-sdk / react from it, exactly as the app resolves the real ones.
const LOCAL = join(ROOT, 'plugin.mjs')
copyFileSync(SRC, LOCAL)
const plugin = (await import(LOCAL)).default
const problems = []
if (plugin.id !== 'localsend') problems.push(`unexpected id ${plugin.id}`)
if (typeof plugin.register !== 'function') problems.push('register() missing')

const contributions = []
const paletteRuns = []
const restCalls = []
const fakeCtx = {
  rest: async (path, opts) => {
    restCalls.push({ path, opts })
    if (path.startsWith('/status')) {
      return {
        ok: true,
        running: true,
        alias: 'Hermes (test)',
        port: 53317,
        addresses: ['192.168.1.10'],
        inbox: '/Users/x/Downloads/LocalSend',
        pin_required: false,
        received: [{ name: 'a.png', path: '/tmp/a.png', bytes: 2048, mtime: Date.now() / 1000 }],
        peers: [{ alias: 'Phone', ip: '192.168.1.20', protocol: 'http', fingerprint: 'f' }],
        errors: [],
      }
    }
    if (path.startsWith('/devices')) return { ok: true, peers: [{ alias: 'Phone', ip: '192.168.1.20', protocol: 'https' }] }
    return { ok: true }
  },
  os: { revealPath: async () => true },
  register: (c) => contributions.push(c),
  registerMany: (list) => list.forEach((c) => contributions.push(c)),
  storage: { get: (_k, d) => d, set: () => {} },
  onEvent: () => () => {},
}

plugin.register(fakeCtx)
console.log(`register() -> ${contributions.length} contribution(s)`)
for (const c of contributions) {
  const area = c.area
  console.log(`  area=${area} id=${c.id}`)
  if (area === 'panes') {
    if (!c.title) problems.push('pane has no title')
    if (!(c.data && c.data.placement)) problems.push('pane has no placement')
    try {
      const tree = c.render()
      if (!tree) problems.push('pane render returned nothing')
    } catch (error) {
      problems.push(`pane render threw: ${error.message}`)
    }
  } else if (area === 'statusBar.right') {
    try {
      if (!c.render()) problems.push('status chip render returned nothing')
    } catch (error) {
      problems.push(`status chip render threw: ${error.message}`)
    }
  } else if (area === 'palette') {
    if (!c.data || !c.data.id || !c.data.label) problems.push('palette entry missing id/label')
    paletteRuns.push(c.data.run)
  }
}

for (const run of paletteRuns) {
  try {
    await run()
  } catch (error) {
    problems.push(`palette run threw: ${error.message}`)
  }
}

// Also exercise the pane in its "receiver stopped / backend down" states.
for (const variant of [
  { ok: true, running: false, received: [], peers: [], addresses: [], errors: [] },
  { ok: false, error: 'backend not mounted', received: [], peers: [] },
]) {
  globalThis.__LS_STATUS__ = variant
  const fresh = (await import(`${LOCAL}?v=${Math.random()}`)).default
  const pane = []
  fresh.register({ ...fakeCtx, register: (c) => pane.push(c) })
  const paneTree = pane.find((c) => c.area === 'panes')
  try {
    paneTree.render()
  } catch (error) {
    problems.push(`pane render threw in state ${JSON.stringify(variant).slice(0, 40)}: ${error.message}`)
  }
}

console.log(`rest paths exercised: ${[...new Set(restCalls.map((c) => c.path.split('?')[0]))].join(', ')}`)
if (problems.length) {
  console.log('PROBLEMS:')
  for (const p of problems) console.log('  -', p)
  process.exit(1)
}
console.log('RESULT: PASS — plugin loads, registers, renders in all states')
