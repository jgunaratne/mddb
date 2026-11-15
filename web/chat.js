'use strict';

/**
 * mddb chat panel: asks a Gemini model questions with the stored documents in
 * its context. Uses the helpers and `state` defined in app.js.
 */

const TOKEN_KEY = 'mddb.token';
const CHAT_OPEN_KEY = 'mddb.chatOpen';
const WIDE_LAYOUT = window.matchMedia('(min-width: 1100px)');

const chat = {
  enabled: false,
  model: null,
  loadError: null,
  needsToken: false,
  // Each message: {role, text, error, context, failed}.
  messages: [],
  busy: false,
  abort: null,
};

const chatEl = {
  panel: document.getElementById('chat'),
  toggle: document.getElementById('chat-toggle'),
  close: document.getElementById('chat-close'),
  model: document.getElementById('chat-model'),
  newChat: document.getElementById('chat-new'),
  scope: document.getElementById('chat-scope-select'),
  selection: document.getElementById('chat-selection'),
  messages: document.getElementById('chat-messages'),
  form: document.getElementById('chat-form'),
  input: document.getElementById('chat-input'),
  send: document.getElementById('chat-send'),
};

// --- open / close ------------------------------------------------------------

function setChatOpen(open, {remember = true} = {}) {
  document.body.classList.toggle('chat-closed', !open);
  chatEl.toggle.setAttribute('aria-expanded', String(open));
  if (remember) localStorage.setItem(CHAT_OPEN_KEY, open ? '1' : '0');
  if (open && remember) chatEl.input.focus();
}

function initialChatOpen() {
  const stored = localStorage.getItem(CHAT_OPEN_KEY);
  // Open by default where there is room for a third column.
  return stored === null ? WIDE_LAYOUT.matches : stored === '1';
}

// --- rendering ---------------------------------------------------------------

function contextNote(context) {
  if (!context) return null;
  const count = context.documents;
  let text = `${count} document${count === 1 ? '' : 's'} in context`;
  const omitted = context.omitted.length;
  const note = h('p', {class: 'chat-note'}, text);
  if (omitted) {
    note.append(
      h(
        'span',
        {class: 'error', title: context.omitted.join('\n')},
        ` · ${omitted} left out (too large to fit)`,
      ),
    );
  }
  return note;
}

function renderMessageBody(message) {
  if (message.role === 'user') {
    return h('div', {class: 'chat-text'}, message.text);
  }
  if (!message.text && !message.error) {
    return h('p', {class: 'muted chat-thinking'}, 'Reading the documents…');
  }
  return renderMarkdownBody(message.text, {inline: true});
}

function renderMessage(message) {
  const body = h('div', {class: 'chat-body'}, renderMessageBody(message));
  const node = h(
    'div',
    {class: `chat-message ${message.role}`},
    h('div', {class: 'chat-role'}, message.role === 'user' ? 'You' : 'mddb'),
    body,
    message.error
      ? h('p', {class: 'chat-note error', role: 'alert'}, message.error)
      : null,
    message.role === 'model' ? contextNote(message.context) : null,
  );
  message.node = node;
  return node;
}

function renderChat() {
  chatEl.model.textContent = chat.model || '';
  chatEl.newChat.hidden = chat.messages.length === 0;
  const usable = chat.enabled && !chat.needsToken;
  chatEl.input.disabled = !usable;
  chatEl.scope.disabled = !usable;
  chatEl.send.disabled = !usable;
  chatEl.send.textContent = chat.busy ? 'Stop' : 'Send';

  if (chat.loadError) {
    chatEl.messages.replaceChildren(
      h('p', {class: 'chat-empty error', role: 'alert'}, chat.loadError),
    );
  } else if (!chat.enabled) {
    chatEl.messages.replaceChildren(
      h(
        'div',
        {class: 'chat-empty'},
        h('p', {}, 'Chat is not enabled on this server.'),
        h(
          'p',
          {class: 'muted'},
          'Start mddb with a Gemini API key in the environment:',
        ),
        h('pre', {class: 'raw'}, h('code', {}, 'GEMINI_API_KEY=… python3 -m mddb')),
      ),
    );
  } else if (chat.needsToken) {
    chatEl.messages.replaceChildren(renderTokenForm());
  } else if (chat.messages.length === 0) {
    chatEl.messages.replaceChildren(
      h(
        'div',
        {class: 'chat-empty'},
        h('p', {}, 'Ask a question about your documents.'),
        h(
          'p',
          {class: 'muted'},
          'The documents chosen under Context are given to the model with ' +
            'every question.',
        ),
      ),
    );
  } else {
    chatEl.messages.replaceChildren(...chat.messages.map(renderMessage));
    chatEl.messages.scrollTop = chatEl.messages.scrollHeight;
  }
}

function renderTokenForm() {
  const input = h('input', {
    type: 'password',
    id: 'chat-token-input',
    autocomplete: 'off',
    placeholder: 'Access token',
  });
  const form = h(
    'form',
    {class: 'chat-empty chat-token-form'},
    h('p', {}, 'This server needs its access token for chat.'),
    h(
      'p',
      {class: 'muted'},
      'It is the token the server was started with. It is kept in this ' +
        'browser only.',
    ),
    h('label', {class: 'visually-hidden', for: 'chat-token-input'}, 'Access token'),
    input,
    h('button', {class: 'button primary', type: 'submit'}, 'Save'),
  );
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const token = input.value.trim();
    if (!token) return;
    localStorage.setItem(TOKEN_KEY, token);
    chat.needsToken = false;
    renderChat();
    chatEl.input.focus();
  });
  return form;
}

/** Re-renders only the message that is being streamed. */
function updateStreaming(message) {
  if (message.renderQueued) return;
  message.renderQueued = true;
  requestAnimationFrame(() => {
    message.renderQueued = false;
    if (!message.node || !message.node.isConnected) return;
    const box = chatEl.messages;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    message.node.replaceWith(renderMessage(message));
    if (atBottom) box.scrollTop = box.scrollHeight;
  });
}

// --- selected documents --------------------------------------------------------

let lastSelectionSize = state.selected.size;

/** Shows which documents are ticked and keeps the Context choice in step. */
function renderSelection() {
  const paths = [...state.selected].sort();
  const option = chatEl.scope.querySelector('option[value="selected"]');
  option.textContent = paths.length
    ? `Selected documents (${paths.length})`
    : 'Selected documents';

  // Ticking the first file switches the chat to the selection; clearing the
  // selection switches it back.
  if (lastSelectionSize === 0 && paths.length > 0) {
    chatEl.scope.value = 'selected';
  } else if (paths.length === 0 && chatEl.scope.value === 'selected') {
    chatEl.scope.value = 'all';
  }
  lastSelectionSize = paths.length;

  const show = chatEl.scope.value === 'selected';
  chatEl.selection.hidden = !show;
  if (!show) return;
  if (paths.length === 0) {
    chatEl.selection.replaceChildren(
      h(
        'p',
        {class: 'chat-note'},
        'Tick files or folders in the list on the left to choose what the ' +
          'chat can see.',
      ),
    );
    return;
  }
  chatEl.selection.replaceChildren(
    h(
      'ul',
      {class: 'chat-chips', 'aria-label': 'Selected documents'},
      ...paths.map((path) =>
        h(
          'li',
          {class: 'chat-chip', title: path},
          h('a', {href: hashFor(path)}, path.split('/').pop()),
          h(
            'button',
            {
              type: 'button',
              'aria-label': `Remove ${path} from the chat context`,
              onclick: () => setSelected([path], false),
            },
            '×',
          ),
        ),
      ),
    ),
    h(
      'button',
      {
        type: 'button',
        class: 'link-button',
        onclick: () => setSelected([...state.selected], false),
      },
      'Clear selection',
    ),
  );
}

document.addEventListener('mddb:selection', renderSelection);
chatEl.scope.addEventListener('change', renderSelection);

// --- sending -----------------------------------------------------------------

function contextPaths() {
  const scope = chatEl.scope.value;
  if (scope === 'all') return {paths: null};
  if (scope === 'selected') {
    if (state.selected.size === 0) {
      return {error: 'Tick some files in the list first, or change Context.'};
    }
    return {paths: [...state.selected]};
  }
  if (scope === 'open') {
    if (!state.document) return {error: 'Open a document first, or change Context.'};
    return {paths: [state.document.path]};
  }
  if (state.documents.length === 0) {
    return {error: 'No documents are listed. Clear the filter or change Context.'};
  }
  return {paths: state.documents.map((doc) => doc.path)};
}

async function sendMessage(text) {
  const {paths, error} = contextPaths();
  const question = {role: 'user', text};
  const answer = {role: 'model', text: '', error: null, context: null};
  if (error) {
    answer.error = error;
    question.failed = answer.failed = true;
    chat.messages.push(question, answer);
    renderChat();
    return;
  }

  // Exchanges that failed are shown but not sent back to the model.
  const history = [...chat.messages.filter((m) => !m.failed), question].map(
    ({role, text}) => ({role, text}),
  );
  chat.messages.push(question, answer);
  chat.busy = true;
  chat.abort = new AbortController();
  renderChat();

  const headers = {'Content-Type': 'application/json'};
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const body = {messages: history};
  if (paths) body.paths = paths;

  try {
    const response = await fetch('api/chat', {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal: chat.abort.signal,
    });
    if (response.status === 401) {
      // Ask for the token and let the user send the question again.
      localStorage.removeItem(TOKEN_KEY);
      chat.messages.splice(-2, 2);
      chat.needsToken = true;
      chatEl.input.value = text;
      return;
    }
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        message = (await response.json()).error || message;
      } catch {
        // Keep the status-based message.
      }
      throw new Error(message);
    }
    await readEvents(response, answer);
    if (!answer.text && !answer.error) answer.error = 'The model returned no answer.';
  } catch (err) {
    answer.error =
      err.name === 'AbortError' ? 'Stopped.' : err.message || 'Request failed.';
  } finally {
    // An exchange without any answer text is not part of the conversation.
    if (!answer.text) question.failed = answer.failed = true;
    chat.busy = false;
    chat.abort = null;
    renderChat();
  }
}

async function readEvents(response, answer) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const handle = (line) => {
    if (!line.trim()) return;
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      return;
    }
    if (event.type === 'context') answer.context = event;
    else if (event.type === 'text') answer.text += event.text;
    else if (event.type === 'error') answer.error = event.message;
    updateStreaming(answer);
  };
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream: true});
    const lines = buffer.split('\n');
    buffer = lines.pop();
    lines.forEach(handle);
  }
  handle(buffer + decoder.decode());
}

// --- events ------------------------------------------------------------------

chatEl.form.addEventListener('submit', (event) => {
  event.preventDefault();
  if (chat.busy) {
    chat.abort.abort();
    return;
  }
  const text = chatEl.input.value.trim();
  if (!text) return;
  chatEl.input.value = '';
  sendMessage(text);
});

chatEl.input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!chat.busy) chatEl.form.requestSubmit();
  }
});

chatEl.newChat.addEventListener('click', () => {
  if (chat.abort) chat.abort.abort();
  chat.messages = [];
  renderChat();
  chatEl.input.focus();
});

chatEl.toggle.addEventListener('click', () => {
  setChatOpen(document.body.classList.contains('chat-closed'));
});
chatEl.close.addEventListener('click', () => setChatOpen(false));

// Document links in answers navigate inside the app; on narrow screens the
// panel covers the viewer, so get it out of the way.
chatEl.messages.addEventListener('click', (event) => {
  const link = event.target.closest('a[href^="#/"]');
  if (link && !WIDE_LAYOUT.matches) setChatOpen(false, {remember: false});
});

async function initChat() {
  setChatOpen(initialChatOpen(), {remember: false});
  try {
    const info = await fetchJson('api/chat');
    chat.enabled = info.enabled;
    chat.model = info.model;
    chat.needsToken =
      info.enabled && info.token_required && !localStorage.getItem(TOKEN_KEY);
  } catch (error) {
    chat.loadError = error.message;
  }
  if (state.selected.size > 0) chatEl.scope.value = 'selected';
  renderSelection();
  renderChat();
}

initChat();
