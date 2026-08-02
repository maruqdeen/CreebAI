import './app.css'
import { api, ApiError, count, seconds, session, toLogin, when } from './api.js'

/* ───────────────────────────────────────────────────────────────────────────
   Support Desk — interaction layer.

   Every figure on these pages comes from the Python service in ../bot. Nothing
   is invented client-side: if the service cannot be reached the page says so
   rather than rendering zeroes that would read as facts.
   ─────────────────────────────────────────────────────────────────────────── */

const $ = (sel, root = document) => root.querySelector(sel)
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel))
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches

const CHANNEL_LABEL = { telegram: 'Telegram', email: 'Email' }
const PAGE = document.body.dataset.page

/* ── Session guard ────────────────────────────────────────────────────────
   Redirect before any page work starts, so a signed-out visitor never sees a
   flash of the queue. The API enforces this too — this is only about not
   rendering a page that is about to 401 on every request. */

if (PAGE !== 'login' && !session.signedIn) {
  toLogin()
}

/* ── Toasts ──────────────────────────────────────────────────────────────── */

function toast(message, tone = 'neutral') {
  const rail = $('#toasts')
  if (!rail) return

  const colors = {
    neutral: 'border-edge text-ink',
    filed: 'border-filed text-filed',
    stamp: 'border-stamp text-stamp',
  }

  const el = document.createElement('div')
  el.className = `toast pointer-events-auto bg-leaf border ${colors[tone] || colors.neutral} rounded-sheet px-3.5 py-2.5 text-xs font-semibold shadow-[3px_3px_0_-1px_var(--color-paper),3px_3px_0_0_var(--color-edge)] flex items-start gap-2.5`

  const dot = document.createElement('span')
  dot.className = 'w-1.5 h-1.5 rounded-full bg-current mt-1.5 shrink-0'
  const text = document.createElement('span')
  text.className = 'flex-1'
  text.textContent = message // never innerHTML: this carries API and user text
  el.append(dot, text)
  rail.appendChild(el)

  setTimeout(() => {
    el.style.transition = 'opacity 200ms, transform 200ms'
    el.style.opacity = '0'
    el.style.transform = 'translateY(0.25rem)'
    setTimeout(() => el.remove(), 220)
  }, 4200)
}

$$('[data-toast]').forEach((btn) =>
  btn.addEventListener('click', () => toast(btn.dataset.toast))
)

async function withLoading(btn, work, minMs = 320) {
  btn.dataset.loading = 'true'
  btn.setAttribute('aria-busy', 'true')
  const started = Date.now()
  try {
    return await work()
  } finally {
    const wait = Math.max(0, minMs - (Date.now() - started))
    setTimeout(() => {
      delete btn.dataset.loading
      btn.removeAttribute('aria-busy')
    }, wait)
  }
}

/* ── Reaching the service ────────────────────────────────────────────────── */

const offlineBanner = $('[data-offline]')

function setOffline(isOffline) {
  offlineBanner?.classList.toggle('hidden', !isOffline)
}

/** Runs a request, showing the offline banner instead of throwing at the user. */
async function load(work, { onError } = {}) {
  try {
    const result = await work()
    setOffline(false)
    return result
  } catch (error) {
    if (error instanceof ApiError && error.offline) {
      setOffline(true)
    } else {
      toast(error.message, 'stamp')
    }
    onError?.(error)
    return null
  }
}

$('[data-retry]')?.addEventListener('click', () => window.location.reload())

// The date in every page head is today's, not a hard-coded one.
$$('[data-today]').forEach((el) => {
  el.textContent = new Date().toLocaleDateString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  })
})

/** Unanswered tallies on the index tabs, on every page. */
function setTally(name, waiting) {
  const el = $(`[data-tally="${name}"]`)
  if (!el) return
  el.textContent = String(waiting)
  el.title = `${waiting} unanswered`
  el.classList.toggle('text-stamp', waiting > 0)
  el.classList.toggle('text-ink-3', waiting === 0)
}

/** The On Duty block reports what the service says, never a hard-coded "live". */
if (PAGE !== 'login')
  load(() => api.health()).then((health) => {
  const set = (name, text, ok) => {
    const value = $(`[data-duty="${name}"]`)
    if (value) value.textContent = text
    const dot = $(`[data-duty-dot="${name}"]`)
    if (dot) {
      dot.classList.remove('bg-edge')
      dot.classList.add(ok ? 'bg-filed' : 'bg-edge')
    }
  }

  if (!health) {
    set('telegram', 'offline', false)
    set('email', 'offline', false)
    const model = $('[data-duty="model"]')
    if (model) model.textContent = 'offline'
    return
  }

  set('telegram', health.telegram_configured ? 'live' : 'no token', health.telegram_configured)
  // The inbound email relay is not built; saying "live" here would be a lie.
  set('email', 'not built', false)
  const model = $('[data-duty="model"]')
  if (model) model.textContent = health.model.replace(/^openai\//, '')
})

/* ── Login ───────────────────────────────────────────────────────────────── */

const loginForm = $('[data-login-form]')
if (loginForm) {
  const errorBox = $('[data-login-error]')
  const submit = $('[data-login-submit]', loginForm)

  const showError = (message) => {
    errorBox.textContent = message
    errorBox.classList.remove('hidden')
  }

  // Already signed in and arriving at the login page: go where they meant.
  if (session.signedIn) {
    api
      .me()
      .then(() => {
        const next = new URLSearchParams(location.search).get('next')
        location.replace(next && next.startsWith('/') ? next : '/index.html')
      })
      .catch(() => session.clear())
  }

  loginForm.addEventListener('submit', (event) => {
    event.preventDefault()
    errorBox.classList.add('hidden')

    const username = $('#username', loginForm).value.trim()
    const password = $('#password', loginForm).value

    if (!username || !password) {
      showError('Enter your username and password.')
      return
    }

    withLoading(submit, async () => {
      try {
        const result = await api.login(username, password)
        session.set(result.token)
        const next = new URLSearchParams(location.search).get('next')
        location.replace(next && next.startsWith('/') ? next : '/index.html')
      } catch (error) {
        if (error instanceof ApiError && error.offline) {
          showError(
            'Cannot reach the support service. Check it is running, then try again.'
          )
        } else {
          showError(error.message)
        }
        $('#password', loginForm).value = ''
        $('#password', loginForm).focus()
      }
    }, 400)
  })
}

/* ── Signing out ─────────────────────────────────────────────────────────── */

$$('[data-logout]').forEach((btn) =>
  btn.addEventListener('click', () => {
    session.clear()
    location.replace('/login.html')
  })
)

/* ── Dashboard ───────────────────────────────────────────────────────────── */

if (PAGE === 'dashboard') {
  const setText = (key, value) => {
    const el = $(`[data-stat="${key}"]`)
    if (el) el.textContent = value
  }

  const renderAsked = (channel, items) => {
    const list = $(`[data-asked="${channel}"]`)
    if (!list) return
    list.replaceChildren()

    if (!items?.length) {
      list.appendChild($('#tpl-asked-empty').content.cloneNode(true))
      return
    }

    items.forEach((item, index) => {
      const row = $('#tpl-asked').content.cloneNode(true)
      const li = row.querySelector('li')
      li.dataset.channel = channel
      $('[data-field="rank"]', row).textContent = String(index + 1)

      const question = $('[data-field="question"]', row)
      question.textContent = item.question
      question.title = item.question

      $('[data-field="count"]', row).textContent = count(item.count)

      const state = $('[data-field="state"]', row)
      state.textContent = item.in_kb ? 'In KB' : 'Not in KB'
      state.classList.add(item.in_kb ? 'stamp-answered' : 'stamp-unanswered')

      list.appendChild(row)
    })
  }

  const renderGaps = (terms) => {
    const wrap = $('[data-gaps]')
    if (!wrap) return
    wrap.replaceChildren()

    if (!terms?.length) {
      const none = document.createElement('p')
      none.className = 'text-xs text-ink-3'
      none.textContent =
        'Nothing yet. Terms your users ask about that your knowledge base does not cover will collect here.'
      wrap.appendChild(none)
      return
    }

    terms.forEach(({ term, count: n }) => {
      const token = document.createElement('span')
      token.className = 'token'
      token.textContent = term
      const tally = document.createElement('span')
      tally.className = 'text-ink-3'
      tally.textContent = `×${n}`
      token.appendChild(tally)
      wrap.appendChild(token)
    })
  }

  const render = (data) => {
    const byChannel = Object.fromEntries(data.channels.map((c) => [c.channel, c]))
    const telegram = byChannel.telegram ?? {}
    const email = byChannel.email ?? {}

    setText('total', count(data.total_answered))
    setText('email-answered', count(email.answered))
    setText('telegram-answered', count(telegram.answered))
    setText('email-unanswered', count(email.unanswered))
    setText('telegram-unanswered', count(telegram.unanswered))
    setText('email-median', seconds(email.median_reply_ms))
    setText('telegram-median', seconds(telegram.median_reply_ms))

    const waiting = (telegram.unanswered ?? 0) + (email.unanswered ?? 0)
    const link = $('[data-waiting-link]')
    if (link) {
      link.textContent =
        waiting === 0
          ? 'Nothing is waiting for you.'
          : `${waiting} quer${waiting === 1 ? 'y is' : 'ies are'} waiting for you.`
      link.classList.toggle('hidden', waiting === 0 && !link.dataset.keep)
    }

    setTally('telegram', telegram.unanswered ?? 0)
    setTally('email', email.unanswered ?? 0)

    const total = (telegram.answered ?? 0) + (email.answered ?? 0)
    const tgShare = total ? Math.round(((telegram.answered ?? 0) / total) * 100) : 0
    $('[data-meter="telegram"]').style.width = `${tgShare}%`
    $('[data-meter="email"]').style.width = `${100 - tgShare}%`
    $('[data-meter-label]').setAttribute(
      'aria-label',
      total
        ? `Channel split: Telegram ${tgShare} percent, Email ${100 - tgShare} percent`
        : 'Channel split: nothing answered yet'
    )
    setText('total-note', total ? 'answered' : 'nothing answered yet')

    renderAsked('email', data.most_asked?.email)
    renderAsked('telegram', data.most_asked?.telegram)
    renderGaps(data.new_keywords)
  }

  const periodBar = $('[data-period]')
  let period = 'week'

  const refresh = async () => {
    const data = await load(() => api.dashboard(period))
    if (data) render(data)
  }

  periodBar?.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-period-btn]')
    if (!btn || btn.dataset.periodBtn === period) return

    period = btn.dataset.periodBtn
    $$('[data-period-btn]', periodBar).forEach((b) =>
      b.setAttribute('aria-selected', String(b === btn))
    )
    const label = period === 'week' ? 'This week' : 'This month'
    $$('[data-period-label]').forEach((el) => (el.textContent = label))
    const word = $('[data-period-word]')
    if (word) word.textContent = period === 'week' ? 'this week' : 'this month'
    refresh()
  })

  refresh()

  // The digest sender is not built yet; say so rather than showing a next-send
  // date that will never arrive.
  load(() => api.settings('telegram')).then((settings) => {
    const note = $('[data-digest-note]')
    if (!note) return
    if (!settings) {
      note.textContent = 'Could not read the schedule.'
      return
    }
    const to = settings.digest_to || 'nobody yet'
    note.textContent =
      `Weekly and monthly summaries of the most-asked questions, addressed to ${to}. ` +
      `The scheduler that sends them is not built yet — the list above is the live version.`
  })
}

/* ── Queue pages ─────────────────────────────────────────────────────────── */

const channel = document.body.dataset.channel
const log = $('[data-log]')

if (channel && log) {
  const skeleton = $('[data-log-skeleton]')
  const emptyState = $('[data-empty]')
  const logHead = $('[data-log-head]')
  const counter = $('[data-queue-count]')
  const filterBar = $('[data-filter]')

  /** ref → the query object the composer reads. */
  const queries = new Map()
  let filter = 'unanswered'

  const emptyCopy = {
    unanswered: {
      title: 'The queue is clear',
      body:
        'Every question matched something in your knowledge base. Anything the bot ' +
        "can't answer — or anything a rule holds back — lands here.",
    },
    answered: {
      title: 'Nothing answered yet',
      body: 'Questions the bot handles on its own will be listed here.',
    },
    all: {
      title: 'Nothing logged yet',
      body:
        'Once the bot is in your group, every question it takes an interest in ' +
        'appears here — answered or not.',
    },
  }

  const renderRow = (query) => {
    const row = $('#tpl-query').content.cloneNode(true)
    const li = row.querySelector('li')
    li.dataset.channel = query.channel
    li.dataset.state = query.state
    li.dataset.ref = query.ref

    $('[data-field="ref"]', row).textContent = query.ref

    const body = $('[data-field="body"]', row)
    body.textContent = query.body
    body.title = query.body

    const meta = $('[data-field="meta"]', row)
    const bits = [query.user_handle, when(query.received_at)].filter(Boolean)
    meta.textContent = bits.join('  ·  ')

    const confidence = $('[data-field="confidence"]', row)
    if (query.confidence === null || query.confidence === undefined) {
      confidence.textContent = '—'
      confidence.classList.add('text-ink-3')
      confidence.title = 'No model call — captured by the question heuristic'
    } else {
      confidence.textContent = query.confidence.toFixed(2)
      confidence.classList.add(query.state === 'answered' ? 'text-filed' : 'text-stamp')
    }

    const cell = $('[data-field="disposition"]', row)
    if (query.state === 'answered') {
      const stamp = document.createElement('span')
      stamp.className = 'stamp stamp-answered'
      stamp.textContent = query.answered_by === 'admin' ? 'Filed' : 'Answered'
      cell.appendChild(stamp)
    } else {
      cell.appendChild($('#tpl-reply-button').content.cloneNode(true))
    }

    return row
  }

  const render = (rows) => {
    queries.clear()
    rows.forEach((q) => queries.set(q.ref, q))

    log.replaceChildren()
    rows.forEach((q) => log.appendChild(renderRow(q)))

    skeleton?.classList.add('hidden')
    log.classList.toggle('hidden', rows.length === 0)
    logHead?.classList.toggle('md:grid', rows.length > 0)

    if (emptyState) {
      emptyState.classList.toggle('hidden', rows.length > 0)
      const copy = emptyCopy[filter]
      $('[data-empty-title]', emptyState).textContent = copy.title
      $('[data-empty-body]', emptyState).textContent = copy.body
      const action = $('[data-empty-action]', emptyState)
      if (action) action.href = `/settings.html?channel=${channel}`
    }
  }

  const setCounter = (waiting) => {
    if (!counter) return
    counter.textContent = waiting === 0 ? 'All clear' : `${waiting} waiting`
    counter.classList.toggle('stamp-unanswered', waiting > 0)
    counter.classList.toggle('stamp-answered', waiting === 0)
    setTally(channel, waiting)
  }

  const refresh = async () => {
    const rows = await load(() => api.queries(channel, filter), {
      onError: () => {
        skeleton?.classList.add('hidden')
        log.classList.add('hidden')
        emptyState?.classList.add('hidden')
        logHead?.classList.remove('md:grid')
      },
    })
    if (!rows) return

    render(rows)

    // The waiting count is the unanswered total, whatever filter is showing.
    if (filter === 'unanswered') {
      setCounter(rows.length)
    } else {
      const all = await load(() => api.queries(channel, 'unanswered'))
      if (all) setCounter(all.length)
    }
  }

  filterBar?.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-filter-btn]')
    if (!btn || btn.dataset.filterBtn === filter) return

    filter = btn.dataset.filterBtn
    $$('[data-filter-btn]', filterBar).forEach((b) =>
      b.setAttribute('aria-selected', String(b === btn))
    )
    skeleton?.classList.remove('hidden')
    log.classList.add('hidden')
    emptyState?.classList.add('hidden')
    refresh()
  })

  // Channel summary boxes share the dashboard endpoint.
  load(() => api.dashboard('week')).then((data) => {
    if (!data) return
    const mine = data.channels.find((c) => c.channel === channel) ?? {}
    const set = (key, value) => {
      const el = $(`[data-stat="${key}"]`)
      if (el) el.textContent = value
    }
    set('answered', count(mine.answered))
    set('unanswered', count(mine.unanswered))
    set('median', seconds(mine.median_reply_ms))

    // Both index-tab tallies, so the other channel is never left stale.
    data.channels.forEach((c) => setTally(c.channel, c.unanswered ?? 0))

    const top = data.most_asked?.[channel]?.[0]
    const question = $('[data-asked-question]')
    const askedCount = $('[data-asked-count]')
    const askedState = $('[data-asked-state]')
    if (top && question) {
      question.textContent = top.question
      askedCount.textContent = `${top.count} ask${top.count === 1 ? '' : 's'}`
      askedState.textContent = top.in_kb ? 'Answered from KB' : 'Not in KB'
      askedState.classList.remove('stamp-neutral')
      askedState.classList.add(top.in_kb ? 'stamp-answered' : 'stamp-unanswered')
    }
  })

  load(() => api.settings(channel)).then((settings) => {
    const link = $('[data-bot-link]')
    if (link && settings?.bot_link) link.textContent = settings.bot_link
  })

  refresh()

  /* ── Composer ──────────────────────────────────────────────────────────── */

  const dialog = $('#composer')
  if (dialog) {
    const draft = $('[data-composer-draft]', dialog)
    const draftLabel = $('[data-draft-label]', dialog)
    const draftNote = $('[data-draft-note]', dialog)
    const kbCheck = $('[data-file-kb]', dialog)
    const kbTitle = $('[data-kb-title]', dialog)
    const kbTitleWrap = $('[data-kb-title-wrap]', dialog)
    const sendBtn = $('[data-send]', dialog)

    let active = null // the query object
    let trigger = null // the button that opened it

    const setEdited = (edited) => {
      draft.dataset.edited = String(edited)
      draftLabel.textContent = edited ? 'Draft — your reply' : 'Draft — carbon copy'
      draftLabel.classList.toggle('!text-carbon', !edited)
      draftNote.classList.toggle('hidden', edited)
    }

    const fill = (query) => {
      $('[data-composer-ref]', dialog).textContent = query.ref
      $('[data-composer-channel]', dialog).textContent =
        CHANNEL_LABEL[query.channel] ?? query.channel
      $('[data-composer-from]', dialog).textContent = query.user_handle || 'someone'
      $('[data-composer-when]', dialog).textContent = when(query.received_at)
      $('[data-composer-body]', dialog).textContent = query.body
      $('[data-composer-reason]', dialog).textContent = query.reason || '—'

      // A flow row arrives with the region, the app version and what was tried
      // already collected — showing it saves asking the same questions again.
      const transcriptWrap = $('[data-composer-transcript-wrap]', dialog)
      transcriptWrap.classList.toggle('hidden', !query.transcript)
      if (query.transcript) {
        $('[data-composer-transcript]', dialog).textContent = query.transcript
      }

      const wrap = $('[data-composer-confidence-wrap]', dialog)
      const hasConfidence = query.confidence !== null && query.confidence !== undefined
      wrap.classList.toggle('hidden', !hasConfidence)
      if (hasConfidence) {
        $('[data-composer-confidence]', dialog).textContent = query.confidence.toFixed(2)
      }

      draft.value = query.draft || ''
      kbTitle.value = query.topic || query.body.slice(0, 60)
      kbCheck.checked = true
      kbTitleWrap.classList.remove('hidden')
      // A row captured without a model call has no draft; that is not an edit.
      setEdited(false)
    }

    const open = (btn) => {
      const ref = btn.closest('[data-ref]')?.dataset.ref
      const query = queries.get(ref)
      if (!query) return

      active = query
      trigger = btn
      fill(query)
      dialog.showModal()
      draft.focus({ preventScroll: true })
      draft.setSelectionRange(0, 0)
    }

    log.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-reply]')
      if (btn) open(btn)
    })

    draft.addEventListener('input', () => {
      if (draft.dataset.edited === 'false') setEdited(true)
    })

    kbCheck.addEventListener('change', () =>
      kbTitleWrap.classList.toggle('hidden', !kbCheck.checked)
    )

    const close = () => {
      dialog.close()
      trigger?.focus({ preventScroll: true })
    }

    $('[data-composer-close]', dialog).addEventListener('click', close)
    $('[data-skip]', dialog).addEventListener('click', close)
    dialog.addEventListener('cancel', (event) => {
      event.preventDefault()
      close()
    })

    $('[data-regenerate]', dialog).addEventListener('click', (event) => {
      const btn = event.currentTarget
      withLoading(
        btn,
        async () => {
          const result = await load(() => api.redraft(active.ref))
          if (!result) return
          if (result.error) {
            toast(`The model could not be reached: ${result.error}`, 'stamp')
            return
          }
          draft.value = result.draft
          active.draft = result.draft
          active.confidence = result.confidence
          setEdited(false)
          toast(
            result.covered_by_kb
              ? `Redrafted at ${result.confidence.toFixed(2)} confidence.`
              : 'Redrafted, but the knowledge base still does not cover this.'
          )
        },
        600
      )
    })

    sendBtn.addEventListener('click', () => {
      const text = draft.value.trim()
      if (!text) {
        toast('Write a reply first.', 'stamp')
        draft.focus()
        return
      }

      const filed = kbCheck.checked
      const title = kbTitle.value.trim()
      const ref = active.ref

      withLoading(sendBtn, async () => {
        const result = await load(() =>
          api.reply(ref, { text, file_to_kb: filed, kb_title: title || null })
        )
        if (!result) return

        const row = log.querySelector(`[data-ref="${CSS.escape(ref)}"]`)
        if (row) {
          const cell = $('[data-field="disposition"]', row)
          const stamp = document.createElement('span')
          stamp.className = 'stamp stamp-answered' + (reduced ? '' : ' stamp-press')
          stamp.textContent = filed ? 'Filed' : 'Answered'
          cell.replaceChildren(stamp)
          row.dataset.state = 'answered'

          if (filter === 'unanswered' && !reduced) {
            row.dataset.filed = 'true'
            setTimeout(refresh, 780)
          } else {
            refresh()
          }
        } else {
          refresh()
        }

        close()

        if (!result.delivered) {
          // Never claim a send that did not happen.
          toast(`Saved, but not delivered: ${result.delivery_error}`, 'stamp')
        } else if (filed) {
          const promoted = result.promoted_keywords ?? []
          toast(
            promoted.length
              ? `Sent and filed. Added ${promoted.join(', ')} to your keywords, so the bot catches this next time.`
              : 'Sent and filed. The bot answers this one itself now.',
            'filed'
          )
        } else {
          toast('Reply sent.')
        }
      })
    })
  }
}

/* ── Settings ────────────────────────────────────────────────────────────── */

const channelTabs = $('[data-channel-tabs]')
if (channelTabs) {
  let current = 'telegram'
  const loaded = new Set()

  const panel = (name) => $(`[data-channel-panel="${name}"]`)

  const fillPanel = (name, settings) => {
    const root = panel(name)
    if (!root || !settings) return

    const kb = $('[data-counted]', root)
    if (kb) {
      kb.value = settings.kb_text || ''
      kb.dispatchEvent(new Event('input'))
    }

    const range = $('[data-threshold]', root)
    if (range) {
      range.value = settings.reply_threshold
      range.dispatchEvent(new Event('input'))
    }

    const link = $('[data-field-bot-link]', root)
    if (link) link.value = settings.bot_link || ''

    const digestTo = $('[data-field-digest-to]', root)
    if (digestTo) digestTo.value = settings.digest_to || ''

    const weekly = $('[data-field-digest-weekly]', root)
    if (weekly) weekly.checked = settings.digest_weekly
    const monthly = $('[data-field-digest-monthly]', root)
    if (monthly) monthly.checked = settings.digest_monthly

    const tokens = $('[data-token-list]', root)
    if (tokens) {
      tokens.replaceChildren()
      settings.keywords.forEach((term) => tokens.appendChild(makeToken(term)))
    }

    const rules = $('[data-rules]', root)
    if (rules) {
      rules.replaceChildren()
      settings.rules.forEach((rule) => rules.appendChild(makeRule(rule)))
    }
  }

  const show = async (name) => {
    current = name
    $$('[data-channel-tab]', channelTabs).forEach((tab) =>
      tab.setAttribute('aria-selected', String(tab.dataset.channelTab === name))
    )
    $$('[data-channel-panel]').forEach((p) =>
      p.classList.toggle('hidden', p.dataset.channelPanel !== name)
    )
    history.replaceState(null, '', `?channel=${name}`)

    if (!loaded.has(name)) {
      const settings = await load(() => api.settings(name))
      if (settings) {
        fillPanel(name, settings)
        loaded.add(name)
      }
    }
  }

  channelTabs.addEventListener('click', (event) => {
    const tab = event.target.closest('[data-channel-tab]')
    if (tab) show(tab.dataset.channelTab)
  })

  channelTabs.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return
    const tabs = $$('[data-channel-tab]', channelTabs)
    const index = tabs.indexOf(document.activeElement)
    if (index === -1) return
    const next =
      tabs[(index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length]
    next.focus()
    show(next.dataset.channelTab)
    event.preventDefault()
  })

  const requested = new URLSearchParams(location.search).get('channel')
  show(requested === 'email' ? 'email' : 'telegram')

  // Connection status comes from the service, because the panel cannot hold
  // secrets — the token lives in bot/.env and is never sent here.
  load(() => api.health()).then((health) => {
    if (!health) return
    $$('[data-connection-status]').forEach((el) => {
      const forChannel = el.dataset.connectionStatus
      const ok = forChannel === 'telegram' ? health.telegram_configured : false
      el.textContent = ok ? 'Connected' : 'Not connected'
      el.classList.toggle('stamp-answered', ok)
      el.classList.toggle('stamp-unanswered', !ok)
    })
    const model = $('[data-model-name]')
    if (model) model.textContent = health.model
  })

  /* Save */
  $$('[data-channel-panel]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      event.preventDefault()
      const name = form.dataset.channelPanel
      const btn = $('[data-save]', form)

      const payload = {
        kb_text: $('[data-counted]', form)?.value ?? '',
        reply_threshold: Number($('[data-threshold]', form)?.value ?? 0.62),
        bot_link: $('[data-field-bot-link]', form)?.value || null,
        digest_to: $('[data-field-digest-to]', form)?.value || null,
        digest_weekly: $('[data-field-digest-weekly]', form)?.checked ?? true,
        digest_monthly: $('[data-field-digest-monthly]', form)?.checked ?? true,
        keywords: $$('.token', form).map((t) => t.dataset.term),
        rules: $$('[data-rules] li', form).map((li, position) => ({
          ref: li.dataset.ref || `R-${String(position + 1).padStart(2, '0')}`,
          text: $('input[data-rule-text]', li)?.value ?? '',
          triggers: $('input[data-rule-triggers]', li)?.value ?? '',
          active: true,
        })),
      }

      withLoading(btn, async () => {
        const saved = await load(() => api.saveSettings(name, payload))
        if (saved) {
          toast('Settings saved. The next message uses them.', 'filed')
        }
      }, 400)
    })

    form.addEventListener('reset', (event) => {
      event.preventDefault()
      loaded.delete(form.dataset.channelPanel)
      show(form.dataset.channelPanel)
      toast('Changes discarded.')
    })
  })

  /* Derive keywords from the knowledge base */
  $$('[data-suggest-keywords]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const form = btn.closest('[data-channel-panel]')
      const name = form.dataset.channelPanel
      const kbText = $('[data-counted]', form)?.value ?? ''

      if (!kbText.trim()) {
        toast('Write the knowledge base first — the keywords come from it.', 'stamp')
        return
      }

      withLoading(btn, async () => {
        const rows = await load(() =>
          api.suggestKeywords(name, { kb_text: kbText, exclude_existing: true })
        )
        if (!rows) return
        if (!rows.length) {
          toast('Nothing new to suggest — the subjects are already covered.')
          return
        }

        const list = $('[data-token-list]', form)
        rows.forEach(({ term }) => list.appendChild(makeToken(term)))
        toast(
          `Added ${rows.length} keyword${rows.length === 1 ? '' : 's'} from the knowledge base. Remove any you don't want, then Save.`
        )
      }, 500)
    })
  })
}

/* ── Settings form controls ──────────────────────────────────────────────── */

function makeToken(term) {
  const token = document.createElement('span')
  token.className = 'token'
  token.dataset.term = term
  token.append(document.createTextNode(term))

  const remove = document.createElement('button')
  remove.type = 'button'
  remove.textContent = '×'
  remove.setAttribute('aria-label', `Remove ${term}`)
  token.appendChild(remove)
  return token
}

function makeRule(rule) {
  const li = document.createElement('li')
  li.className = 'p-3 space-y-2'
  li.dataset.ref = rule.ref

  const top = document.createElement('div')
  top.className = 'flex items-start gap-3'

  const ref = document.createElement('span')
  ref.className = 'log-ref pt-2 w-9 shrink-0'
  ref.textContent = rule.ref

  const text = document.createElement('input')
  text.type = 'text'
  text.className = 'field flex-1'
  text.value = rule.text
  text.setAttribute('aria-label', `Rule ${rule.ref}`)
  text.dataset.ruleText = ''

  const remove = document.createElement('button')
  remove.type = 'button'
  remove.className = 'btn btn-ghost !p-1.5 shrink-0'
  remove.dataset.ruleRemove = ''
  remove.setAttribute('aria-label', `Remove rule ${rule.ref}`)
  remove.innerHTML =
    '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m3.5 3.5 9 9m0-9-9 9" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>'

  top.append(ref, text, remove)

  const triggerRow = document.createElement('div')
  triggerRow.className = 'flex items-center gap-3 pl-12'
  const label = document.createElement('span')
  label.className = 'micro shrink-0'
  label.textContent = 'Holds on'
  const triggers = document.createElement('input')
  triggers.type = 'text'
  triggers.className = 'field field-mono flex-1'
  triggers.value = rule.triggers || ''
  triggers.placeholder = 'words that force this rule — leave blank for advisory only'
  triggers.setAttribute('aria-label', `Trigger words for rule ${rule.ref}`)
  triggers.dataset.ruleTriggers = ''
  triggerRow.append(label, triggers)

  li.append(top, triggerRow)
  return li
}

// Secret fields. Values are never read or logged here.
$$('[data-secret-toggle]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const input = document.getElementById(btn.dataset.secretToggle)
    const shown = input.type === 'text'
    input.type = shown ? 'password' : 'text'
    btn.textContent = shown ? 'Show' : 'Hide'
    btn.setAttribute('aria-pressed', String(!shown))
  })
})

// Keyword tokens.
$$('[data-token-input]').forEach((input) => {
  const list = input.closest('div').querySelector('[data-token-list]')

  const add = (raw) => {
    const value = raw.trim().replace(/,$/, '').toLowerCase()
    if (!value) return
    const existing = $$('.token', list).map((t) => t.dataset.term)
    if (existing.includes(value)) return
    list.appendChild(makeToken(value))
  }

  input.addEventListener('keydown', (event) => {
    if (event.key === ',' || event.key === 'Enter') {
      event.preventDefault()
      input.value.split(',').forEach(add)
      input.value = ''
    } else if (event.key === 'Backspace' && input.value === '') {
      $$('.token', list).pop()?.remove()
    }
  })
  input.addEventListener('blur', () => {
    input.value.split(',').forEach(add)
    input.value = ''
  })
})

document.addEventListener('click', (event) => {
  const remove = event.target.closest('[data-token-list] .token button')
  if (remove) remove.closest('.token').remove()
})

// Rules.
$$('[data-rule-add]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const list = btn.previousElementSibling
    const next = $$('li', list).length + 1
    const li = makeRule({ ref: `R-${String(next).padStart(2, '0')}`, text: '', triggers: '' })
    list.appendChild(li)
    li.querySelector('input').focus()
  })
})

document.addEventListener('click', (event) => {
  const btn = event.target.closest('[data-rule-remove]')
  if (btn) btn.closest('li').remove()
})

// Threshold.
$$('[data-threshold]').forEach((range) => {
  const out = range.parentElement.querySelector('[data-threshold-out]')
  const update = () => (out.textContent = Number(range.value).toFixed(2))
  range.addEventListener('input', update)
  update()
})

// Character counters on the knowledge bases.
$$('[data-counted]').forEach((area) => {
  const out = $(`[data-count-for="${area.id}"]`)
  const update = () => {
    const n = area.value.length
    out.textContent = `${n.toLocaleString()} character${n === 1 ? '' : 's'}`
  }
  area.addEventListener('input', update)
  update()
})
