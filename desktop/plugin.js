/**
 * localsend — Hermes desktop plugin (desktop half of the localsend package).
 *
 * A right-side pane with the thing you actually want at a glance: is the
 * receiver up, who is on the network, and what has arrived. Plus a status-bar
 * chip and palette commands so the receiver can be flipped without opening a
 * chat. Talks to the package's own backend (../dashboard/plugin_api.py) through
 * ctx.rest — namespace-scoped by construction.
 *
 * Disk plugins load uncompiled: only jsx()/jsxs(), never JSX syntax, and only
 * the @hermes/plugin-sdk / react / react/jsx-runtime specifiers resolve.
 */

import {
  host,
  cn,
  icons,
  useQuery,
  Button,
  StatusDot,
  PANES_AREA,
  STATUSBAR_AREAS,
  PALETTE_AREA,
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'localsend'
const POLL_MS = 5000

function fmtBytes(bytes) {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n < 0) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

function fmtWhen(value) {
  if (!value) return ''
  const then = typeof value === 'number' ? value * 1000 : Date.parse(value)
  if (!Number.isFinite(then)) return ''
  const mins = Math.round((Date.now() - then) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

function Section({ title, action, children }) {
  return jsxs('div', {
    className: 'mt-3 border-t border-(--ui-stroke-secondary) pt-2',
    children: [
      jsxs('div', {
        className: 'mb-1 flex items-center justify-between',
        children: [
          jsx('div', {
            className: 'text-[0.6875rem] font-medium tracking-wide text-(--ui-text-secondary)',
            children: title,
          }),
          action || null,
        ],
      }),
      children,
    ],
  })
}

function Meta({ label, value, mono }) {
  return jsxs('div', {
    className: 'flex items-baseline justify-between gap-2 py-0.5',
    children: [
      jsx('div', { className: 'shrink-0 text-(--ui-text-quaternary)', children: label }),
      jsx('div', {
        className: cn('truncate text-right', mono && 'font-mono text-[0.6875rem]'),
        children: value == null || value === '' ? '—' : value,
      }),
    ],
  })
}

function LocalSendPane(props) {
  const ctx = props.ctx
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')

  const status = useQuery({
    queryKey: [ID, 'status'],
    queryFn: () => ctx.rest('/status'),
    refetchInterval: POLL_MS,
  })
  const devices = useQuery({
    queryKey: [ID, 'devices'],
    queryFn: () => ctx.rest('/devices?timeout_s=3'),
    refetchInterval: POLL_MS * 4,
  })

  const data = status.data || {}
  const running = Boolean(data.running)
  const unreachable = Boolean(status.error) || data.ok === false
  const received = data.received || []
  const peers = (devices.data && devices.data.peers) || []
  const warnings = [...(data.errors || [])]

  async function act(path, body, message) {
    setBusy(true)
    setNotice('')
    try {
      const result = await ctx.rest(path, body ? { method: 'POST', body } : { method: 'POST' })
      if (result && result.ok === false) {
        setNotice(result.error || 'the backend refused the request')
        host.notify({ kind: 'error', message: result.error || 'LocalSend request failed' })
      } else if (message) {
        host.notify({ kind: 'info', message })
      }
      await status.refetch()
      await devices.refetch()
    } catch (error) {
      const text = String((error && error.message) || error)
      setNotice(text)
      host.notify({ kind: 'error', message: text })
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-y-auto p-3 text-xs',
    children: [
      jsxs('div', {
        className: 'mb-1 flex items-center justify-between',
        children: [
          jsx('div', {
            className: 'text-[0.6875rem] font-medium tracking-wide text-(--ui-text-secondary)',
            children: 'LOCALSEND',
          }),
          jsxs('div', {
            className: 'flex items-center gap-1.5',
            children: [
              jsx(StatusDot, { tone: unreachable ? 'neutral' : running ? 'ok' : 'neutral' }),
              jsx('span', {
                className: 'text-(--ui-text-quaternary)',
                children: unreachable ? 'backend off' : running ? 'receiving' : 'stopped',
              }),
            ],
          }),
        ],
      }),

      unreachable
        ? jsx('div', {
            className: 'py-2 text-(--ui-text-quaternary)',
            children:
              'Backend not reachable. Add localsend to plugins.enabled in config.yaml and restart the gateway.',
          })
        : null,

      jsx(Meta, { label: 'alias', value: data.alias }),
      jsx(Meta, { label: 'port', value: data.port, mono: true }),
      jsx(Meta, {
        label: 'address',
        value: (data.addresses || []).length ? `${data.addresses[0]}:${data.port}` : '—',
        mono: true,
      }),
      jsx(Meta, { label: 'inbox', value: (data.inbox || '').replace(/^\/Users\/[^/]+/, '~'), mono: true }),
      data.pin_required ? jsx(Meta, { label: 'pin', value: 'required' }) : null,

      jsxs('div', {
        className: 'mt-2 flex flex-wrap gap-1.5',
        children: [
          jsx(Button, {
            size: 'sm',
            variant: running ? 'secondary' : 'default',
            disabled: busy || unreachable || running,
            onClick: () => act('/start', {}, 'LocalSend receiver started'),
            children: 'Start',
          }),
          jsx(Button, {
            size: 'sm',
            variant: 'secondary',
            disabled: busy || unreachable || !running,
            onClick: () => act('/stop', null, 'LocalSend receiver stopped'),
            children: 'Stop',
          }),
          jsx(Button, {
            size: 'sm',
            variant: 'ghost',
            disabled: busy,
            onClick: () => act('/devices?timeout_s=4', null, 'Device scan requested'),
            children: 'Scan',
          }),
        ],
      }),

      notice
        ? jsx('div', { className: 'mt-2 text-(--ui-text-quaternary)', children: notice })
        : null,

      jsx(Section, {
        title: `NEARBY DEVICES${devices.isLoading ? '' : ` · ${peers.length}`}`,
        children: peers.length
          ? peers.slice(0, 6).map((peer) => jsxs('div', {
              key: peer.fingerprint || peer.ip,
              className: 'flex items-center justify-between gap-2 py-0.5',
              children: [
                jsxs('div', {
                  className: 'flex min-w-0 items-center gap-1.5',
                  children: [
                    jsx(StatusDot, { tone: 'info' }),
                    jsx('span', { className: 'truncate', children: peer.alias || peer.ip }),
                  ],
                }),
                jsx('span', {
                  className: 'shrink-0 font-mono text-[0.6875rem] text-(--ui-text-quaternary)',
                  children: peer.protocol === 'https' ? 'https' : 'http',
                }),
              ],
            }))
          : jsx('div', {
              className: 'py-1 text-(--ui-text-quaternary)',
              children: devices.isLoading
                ? 'Scanning…'
                : 'No devices found. Open LocalSend on the phone and hit Scan.',
            }),
      }),

      jsx(Section, {
        title: `INBOX · ${received.length}`,
        action: received.length
          ? jsx('button', {
              type: 'button',
              className: 'text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)',
              title: 'Reveal in Finder',
              onClick: () => ctx.os.revealPath(data.inbox || ''),
              children: 'reveal',
            })
          : null,
        children: received.length
          ? received.slice(0, 8).map((file) => jsxs('div', {
              key: file.path || file.name,
              className: 'flex items-center justify-between gap-2 py-0.5',
              children: [
                jsxs('div', {
                  className: 'flex min-w-0 items-center gap-1.5',
                  children: [
                    jsx(icons.FileText, { size: 12 }),
                    jsx('span', { className: 'truncate', children: file.name }),
                  ],
                }),
                jsx('span', {
                  className: 'shrink-0 text-[0.6875rem] text-(--ui-text-quaternary)',
                  children: `${fmtBytes(file.bytes)} · ${fmtWhen(file.mtime || file.received_at)}`,
                }),
              ],
            }))
          : jsx('div', {
              className: 'py-1 text-(--ui-text-quaternary)',
              children: 'Nothing received yet.',
            }),
      }),

      warnings.length
        ? jsx('div', {
            className: 'mt-2 text-(--ui-text-quaternary)',
            children: warnings.slice(0, 2).join('; '),
          })
        : null,

      jsx('div', {
        className: 'mt-auto pt-3 text-(--ui-text-quaternary)',
        children: 'Phones send to this address while the receiver is on.',
      }),
    ],
  })
}

function StatusChip(props) {
  const ctx = props.ctx
  const status = useQuery({
    queryKey: [ID, 'status'],
    queryFn: () => ctx.rest('/status'),
    refetchInterval: POLL_MS * 3,
  })
  const data = status.data || {}
  const running = Boolean(data.running)
  const count = (data.received || []).length
  return jsxs('button', {
    type: 'button',
    className: 'flex items-center gap-1.5 text-[0.6875rem] text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)',
    title: running ? `LocalSend receiving on port ${data.port}` : 'LocalSend receiver stopped',
    onClick: () => host.navigate('/settings/plugins'),
    children: [
      jsx(StatusDot, { tone: running ? 'ok' : 'neutral' }),
      jsx('span', { children: count ? `localsend · ${count}` : 'localsend' }),
    ],
  })
}

export default {
  id: ID,
  name: 'LocalSend',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'localsend',
      data: { placement: 'right', width: '300px' },
      render: () => jsx(LocalSendPane, { ctx }),
    })

    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 150,
      render: () => jsx(StatusChip, { ctx }),
    })

    const command = (id, label, run, keywords) =>
      ctx.register({
        id,
        area: PALETTE_AREA,
        data: {
          id: `${ID}.${id}`,
          label,
          keywords: keywords || ['localsend', 'airdrop', 'transfer'],
          run,
        },
      })

    command('start', 'LocalSend: start receiver', () =>
      ctx.rest('/start', { method: 'POST', body: {} }).then((result) => {
        if (result && result.ok === false) host.notify({ kind: 'error', message: result.error })
        else host.notify({ kind: 'info', message: 'LocalSend receiver started' })
      }),
    )
    command('stop', 'LocalSend: stop receiver', () =>
      ctx.rest('/stop', { method: 'POST' }).then(() => host.notify({ kind: 'info', message: 'LocalSend receiver stopped' })),
    )
    command('scan', 'LocalSend: scan for devices', () =>
      ctx.rest('/devices?timeout_s=4').then((result) => {
        const count = result && result.peers ? result.peers.length : 0
        host.notify({ kind: 'info', message: `LocalSend: ${count} device${count === 1 ? '' : 's'} found` })
      }),
    )
  },
}
