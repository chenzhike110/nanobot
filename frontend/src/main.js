import './style.css'
import { marked } from 'marked'

const app = document.querySelector('#app')

app.innerHTML = `
  <div class="app-shell">
    <header class="topbar">
      <div class="brand-wrap">
        <div id="appTitle" class="brand">nanobot Web</div>
        <div class="subtitle">多模态对话与图片管理</div>
      </div>
      <label class="chat-id-wrap">
        <span>Chat ID</span>
        <input id="chatId" value="default" autocomplete="off" />
      </label>
    </header>

    <main class="feed-shell">
      <div id="messages" class="messages"></div>
      <div id="emptyState" class="empty-state">发送一条消息，或先上传多张图片开始对话。</div>
    </main>

    <footer class="composer-shell">
      <div id="uploadList" class="upload-list"></div>
      <div class="composer">
        <textarea id="promptInput" placeholder="给 nanobot 发送消息"></textarea>
        <div class="composer-actions">
          <div class="left-actions">
            <input id="fileInput" type="file" accept="image/*" multiple hidden />
            <button id="uploadBtn" class="ghost-btn" type="button">上传图片</button>
            <button id="reloadBtn" class="ghost-btn" type="button">刷新历史</button>
          </div>
          <div class="right-actions">
            <span id="status" class="status">就绪</span>
            <button id="sendBtn" class="primary-btn" type="button">发送</button>
          </div>
        </div>
      </div>
    </footer>
  </div>
`

const els = {
  appTitle: document.querySelector('#appTitle'),
  chatId: document.querySelector('#chatId'),
  messages: document.querySelector('#messages'),
  emptyState: document.querySelector('#emptyState'),
  promptInput: document.querySelector('#promptInput'),
  uploadBtn: document.querySelector('#uploadBtn'),
  fileInput: document.querySelector('#fileInput'),
  uploadList: document.querySelector('#uploadList'),
  reloadBtn: document.querySelector('#reloadBtn'),
  sendBtn: document.querySelector('#sendBtn'),
  status: document.querySelector('#status'),
}

const state = {
  title: 'nanobot Web',
  cursor: 0,
  activeChatId: 'default',
  attachments: [],
  eventSource: null,
  polling: false,
  isNearBottom: true,
  awaitingResponse: false,
  liveStream: null,
  openPanels: {},
  progress: {
    textParts: [],
    toolHints: [],
    thinkingParts: [],
  },
}

marked.setOptions({
  breaks: true,
  gfm: true,
})

function setStatus(text) {
  els.status.textContent = text
}

function resetProgressState() {
  state.progress = {
    textParts: [],
    toolHints: [],
    thinkingParts: [],
  }
}

function escapeHtml(value) {
  return String(value || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
}

function stripThinkArtifacts(value) {
  let text = String(value || '')
  text = text.replace(/<think>[\s\S]*?<\/think>/gi, '')
  text = text.replace(/^[\s\S]*?<\/think>/i, '')
  text = text.replace(/<think>[\s\S]*$/i, '')
  text = text.replace(/<\/?think>/gi, '')
  return text.trim()
}

function extractThinkingText(item) {
  const fromMeta = stripThinkArtifacts(item?.metadata?._thinking || '')
  if (fromMeta) return fromMeta
  return ''
}

function renderMarkdown(value) {
  const cleaned = stripThinkArtifacts(value)
  if (!cleaned) return ''
  return marked.parse(cleaned)
}

function isNearBottom() {
  const threshold = 120
  return window.innerHeight + window.scrollY >= document.body.scrollHeight - threshold
}

function scrollToBottom(behavior = 'smooth') {
  window.scrollTo({ top: document.body.scrollHeight, behavior })
}

function uniquePush(list, value) {
  if (!value) return
  if (!list.includes(value)) {
    list.push(value)
  }
}

function panelOpenAttr(id) {
  return state.openPanels[id] ? ' open' : ''
}

function messageDomId(item) {
  return `message-${item.id}`
}

function renderAssets(media = []) {
  if (!media.length) return ''
  return `
    <div class="media-grid">
      ${media.map((asset) => {
        const label = escapeHtml(asset.caption || asset.id || 'image')
        if ((asset.mime_type || '').startsWith('image/') && asset.web_url) {
          return `
            <a class="asset-card" href="${asset.web_url}" target="_blank" rel="noreferrer">
              <img src="${asset.web_url}" alt="${label}" loading="lazy" />
              <div class="asset-caption">${label}</div>
            </a>
          `
        }
        const href = asset.web_url || '#'
        return `
          <a class="asset-card asset-file" href="${href}" target="_blank" rel="noreferrer">
            <div class="asset-caption">${label}</div>
          </a>
        `
      }).join('')}
    </div>
  `
}

function renderMessage(item) {
  const role = item.role || 'assistant'
  const cleanedContent = stripThinkArtifacts(item.content || '')
  const thinking = extractThinkingText(item)
  const panelId = `message-thinking-${item.id}`
  const meta = new Date(item.timestamp || Date.now()).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  })
  const progressClass = item.metadata && item.metadata._progress ? ' progress' : ''
  const thinkingHtml = thinking
    ? `
      <details class="thinking-panel" data-panel-id="${panelId}"${panelOpenAttr(panelId)}>
        <summary>查看 think</summary>
        <div class="content markdown-body">${renderMarkdown(thinking)}</div>
      </details>
    `
    : ''
  return `
    <article id="${messageDomId(item)}" class="message ${role}${progressClass}">
      <div class="avatar">${role === 'user' ? 'U' : 'AI'}</div>
      <div class="bubble">
        <div class="content markdown-body">${renderMarkdown(cleanedContent)}</div>
        ${thinkingHtml}
        ${renderAssets(item.media)}
        <div class="meta">${meta}</div>
      </div>
    </article>
  `
}

function resetLiveStream() {
  state.liveStream = null
  const existing = document.getElementById('message-stream-live')
  if (existing) existing.remove()
}

function hasLiveStreamCard() {
  return Boolean(
    state.liveStream
    && (state.liveStream.content || state.liveStream.thinking || state.awaitingResponse),
  )
}

function renderLiveStreamCard() {
  const existing = document.getElementById('message-stream-live')
  if (!hasLiveStreamCard()) {
    if (existing) existing.remove()
    return
  }

  const panelId = 'live-stream-thinking'
  const mergedThinking = [
    ...state.progress.thinkingParts,
    state.liveStream.thinking,
  ].filter(Boolean).join('\n\n')
  const thinkingHtml = mergedThinking
    ? `
      <details class="thinking-panel" data-panel-id="${panelId}"${panelOpenAttr(panelId)}>
        <summary>查看 think</summary>
        <div class="content markdown-body">${renderMarkdown(mergedThinking)}</div>
      </details>
    `
    : ''
  const toolsHtml = state.progress.toolHints.length
    ? `<div class="progress-tools"><span class="progress-label">工具</span>${state.progress.toolHints.map((hint) => `<code>${escapeHtml(hint)}</code>`).join('')}</div>`
    : ''
  const summary = state.liveStream.content
    ? renderMarkdown(state.liveStream.content)
    : (mergedThinking ? '思考中...' : '正在处理你的请求...')
  const html = `
    <article id="message-stream-live" class="message assistant progress live-progress">
      <div class="avatar">AI</div>
      <div class="bubble">
        <div class="content markdown-body">${summary}</div>
        ${toolsHtml}
        ${thinkingHtml}
        <div class="typing-row">
          <span class="typing-dot"></span>
          <span class="typing-dot"></span>
          <span class="typing-dot"></span>
        </div>
        <div class="meta">流式生成中...</div>
      </div>
    </article>
  `
  if (existing) {
    existing.outerHTML = html
  } else {
    els.messages.insertAdjacentHTML('beforeend', html)
  }
}

function applyStreamEvent(item) {
  const streamId = item.metadata?._stream_id || 'default'
  const kind = item.metadata?._stream_kind || 'text_delta'
  if (!state.liveStream || state.liveStream.id !== streamId) {
    state.liveStream = { id: streamId, content: '', thinking: '' }
  }
  if (kind === 'reset') {
    resetLiveStream()
    return
  }
  if (kind === 'thinking_delta') {
    state.liveStream.thinking += item.content || ''
  } else if (kind === 'text_delta') {
    state.liveStream.content += item.content || ''
  }
  state.awaitingResponse = true
  renderLiveStreamCard()
  if (state.isNearBottom) {
    scrollToBottom('auto')
  }
}

function renderProgressCard() {
  const existing = document.getElementById('message-progress-live')
  if (hasLiveStreamCard()) {
    if (existing) existing.remove()
    return
  }
  const hasText = state.progress.textParts.length > 0
  const hasTools = state.progress.toolHints.length > 0
  const hasThinking = state.progress.thinkingParts.length > 0
  const shouldShow = state.awaitingResponse || hasText || hasTools || hasThinking

  if (!shouldShow) {
    if (existing) existing.remove()
    return
  }

  const summaryText = hasText
    ? escapeHtml(state.progress.textParts.join('\n'))
    : (hasThinking ? '思考中...' : '正在处理你的请求...')
  const toolsHtml = hasTools
    ? `<div class="progress-tools"><span class="progress-label">工具</span>${state.progress.toolHints.map((hint) => `<code>${escapeHtml(hint)}</code>`).join('')}</div>`
    : ''
  const thinkingHtml = hasThinking
    ? `
      <details class="thinking-panel" data-panel-id="progress-thinking"${panelOpenAttr('progress-thinking')}>
        <summary>查看 think</summary>
        <div class="content markdown-body">${renderMarkdown(state.progress.thinkingParts.join('\n\n'))}</div>
      </details>
    `
    : ''

  const html = `
    <article id="message-progress-live" class="message assistant progress live-progress">
      <div class="avatar">AI</div>
      <div class="bubble">
        <div class="content">${summaryText}</div>
        ${toolsHtml}
        ${thinkingHtml}
        <div class="typing-row">
          <span class="typing-dot"></span>
          <span class="typing-dot"></span>
          <span class="typing-dot"></span>
        </div>
        <div class="meta">${state.awaitingResponse ? '正在等待模型回复...' : '处理中'}</div>
      </div>
    </article>
  `

  if (existing) {
    existing.outerHTML = html
  } else {
    els.messages.insertAdjacentHTML('beforeend', html)
  }
}

async function appendMessages(messages, replace = false) {
  const shouldStick = replace || isNearBottom()
  if (replace) {
    els.messages.innerHTML = ''
    resetProgressState()
  }

  for (const item of messages || []) {
    if (item.metadata && item.metadata._stream) {
      applyStreamEvent(item)
      continue
    }
    const isProgress = Boolean(item.metadata && item.metadata._progress)
    if (isProgress) {
      const thinking = stripThinkArtifacts(item.metadata?._thinking || '')
      if (item.metadata?._tool_hint) {
        uniquePush(state.progress.toolHints, item.content || '')
      } else {
        uniquePush(state.progress.textParts, stripThinkArtifacts(item.content || ''))
      }
      if (thinking) {
        uniquePush(state.progress.thinkingParts, thinking)
      }
      continue
    }

    if ((item.role || 'assistant') !== 'user') {
      state.awaitingResponse = false
      resetProgressState()
      resetLiveStream()
    }
    const existing = document.getElementById(messageDomId(item))
    const html = renderMessage(item)
    if (existing) {
      existing.outerHTML = html
    } else {
      els.messages.insertAdjacentHTML('beforeend', html)
    }
  }

  renderProgressCard()
  renderLiveStreamCard()

  const hasMessages = els.messages.children.length > 0
  els.emptyState.style.display = hasMessages ? 'none' : 'block'
  if (hasMessages && shouldStick) {
    scrollToBottom(replace ? 'auto' : 'smooth')
  }
  state.isNearBottom = isNearBottom()
}

function renderAttachmentChip(asset) {
  const label = escapeHtml(asset.caption || asset.id || 'image')
  return `
    <div class="upload-chip">
      <button type="button" data-remove="${asset.id}" aria-label="移除图片">×</button>
      <img src="${asset.web_url || ''}" alt="${label}" />
      <div class="upload-chip-caption">${label}</div>
    </div>
  `
}

function renderAttachments() {
  els.uploadList.innerHTML = state.attachments.map(renderAttachmentChip).join('')
}

async function fetchJson(url, options = undefined) {
  const res = await fetch(url, options)
  const data = await res.json()
  if (!res.ok) {
    throw new Error(data.error || 'request failed')
  }
  return data
}

async function loadConfig() {
  const data = await fetchJson('/config')
  state.title = data.title || state.title
  document.title = state.title
  els.appTitle.textContent = state.title
}

async function loadHistory() {
  const chatId = (els.chatId.value || 'default').trim() || 'default'
  state.activeChatId = chatId
  const data = await fetchJson(`/history?chat_id=${encodeURIComponent(chatId)}`)
  state.awaitingResponse = false
  resetLiveStream()
  await appendMessages((data.messages || []).filter((item) => !(item.metadata && item.metadata._progress)), true)
  state.cursor = data.next_cursor || 0
  setStatus('历史已同步')
}

function closeEventStream() {
  if (state.eventSource) {
    state.eventSource.close()
    state.eventSource = null
  }
}

function connectEventStream() {
  closeEventStream()
  if (!window.EventSource) return
  const chatId = (els.chatId.value || 'default').trim() || 'default'
  const url = `/events?chat_id=${encodeURIComponent(chatId)}&since=${encodeURIComponent(state.cursor)}`
  const source = new EventSource(url)
  state.eventSource = source

  source.onmessage = async (event) => {
    try {
      const item = JSON.parse(event.data)
      await appendMessages([item])
      state.cursor = Math.max(state.cursor, item.id || 0)
      if (item.metadata && item.metadata._stream) {
        setStatus('流式生成中...')
      } else {
        setStatus('已同步最新回复')
      }
    } catch (error) {
      setStatus(error.message || '流式事件解析失败')
    }
  }

  source.onopen = () => {
    setStatus('实时连接已建立')
  }

  source.onerror = () => {
    setStatus('实时连接中断，等待重连...')
  }
}

async function poll() {
  if (state.polling) return
  state.polling = true
  try {
    const chatId = (els.chatId.value || 'default').trim() || 'default'
    if (chatId !== state.activeChatId) {
      await loadHistory()
    } else {
      const data = await fetchJson(`/poll?chat_id=${encodeURIComponent(chatId)}&since=${state.cursor}`)
      await appendMessages(data.messages || [])
      state.cursor = data.next_cursor || state.cursor
      if ((data.messages || []).length) {
        setStatus('已同步最新回复')
      } else if (state.awaitingResponse) {
        setStatus('等待模型回复中...')
      }
    }
  } catch (error) {
    setStatus(error.message || '轮询失败')
  } finally {
    state.polling = false
    window.setTimeout(poll, 1400)
  }
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '')
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

async function uploadSingleFile(file) {
  const contentBase64 = await readFileAsBase64(file)
  const data = await fetchJson('/upload', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      filename: file.name,
      mime_type: file.type || 'application/octet-stream',
      purpose: 'both',
      caption: file.name,
      content_base64: contentBase64,
    }),
  })
  return data.asset
}

async function handleUploads(files) {
  if (!files.length) return
  setStatus(`上传中: ${files.length} 张图片`)
  try {
    for (const file of files) {
      const asset = await uploadSingleFile(file)
      state.attachments.push(asset)
    }
    renderAttachments()
    setStatus(`已附加 ${state.attachments.length} 张图片`)
  } catch (error) {
    setStatus(error.message || '上传失败')
  } finally {
    els.fileInput.value = ''
  }
}

async function sendMessage() {
  const chatId = (els.chatId.value || 'default').trim() || 'default'
  const content = els.promptInput.value.trim()
  if (!content && !state.attachments.length) return
  try {
    setStatus('发送中...')
    const data = await fetchJson('/message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sender_id: chatId,
        chat_id: chatId,
        content,
        media: state.attachments,
      }),
    })
    if (data.message) {
      await appendMessages([data.message])
      state.cursor = Math.max(state.cursor, data.message.id || 0)
    }
    state.isNearBottom = true
    state.awaitingResponse = true
    els.promptInput.value = ''
    state.attachments = []
    resetLiveStream()
    renderAttachments()
    renderProgressCard()
    renderLiveStreamCard()
    setStatus('等待回复...')
    if (state.isNearBottom) {
      scrollToBottom('smooth')
    }
  } catch (error) {
    state.awaitingResponse = false
    resetProgressState()
    resetLiveStream()
    renderProgressCard()
    renderLiveStreamCard()
    setStatus(error.message || '发送失败')
  }
}

els.uploadBtn.addEventListener('click', () => els.fileInput.click())
els.fileInput.addEventListener('change', (event) => {
  handleUploads([...(event.target.files || [])])
})
els.reloadBtn.addEventListener('click', async () => {
  await loadHistory()
  connectEventStream()
})
els.sendBtn.addEventListener('click', sendMessage)
els.chatId.addEventListener('change', async () => {
  await loadHistory()
  connectEventStream()
})
els.uploadList.addEventListener('click', (event) => {
  const target = event.target
  if (!(target instanceof HTMLElement)) return
  const removeId = target.dataset.remove
  if (!removeId) return
  state.attachments = state.attachments.filter((asset) => asset.id !== removeId)
  renderAttachments()
  setStatus(state.attachments.length ? `已附加 ${state.attachments.length} 张图片` : '就绪')
})
els.promptInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    sendMessage()
  }
})

window.addEventListener('scroll', () => {
  state.isNearBottom = isNearBottom()
})

document.addEventListener('toggle', (event) => {
  const target = event.target
  if (!(target instanceof HTMLDetailsElement)) return
  const panelId = target.dataset.panelId
  if (!panelId) return
  state.openPanels[panelId] = target.open
}, true)

async function bootstrap() {
  try {
    await loadConfig()
    state.isNearBottom = true
    await loadHistory()
    if (window.EventSource) {
      connectEventStream()
    } else {
      poll()
    }
  } catch (error) {
    setStatus(error.message || '初始化失败')
  }
}

bootstrap()
