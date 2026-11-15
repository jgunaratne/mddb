'use strict';

/** mddb web app: file tree, tag filter, full-text search and viewer. */

const API = 'api/documents';
const SEARCH_DEBOUNCE_MS = 200;
const AUTO_EXPAND_LIMIT = 60;
const SELECTION_KEY = 'mddb.selected';

const state = {
  query: '',
  tags: new Set(),
  documents: [],
  total: 0,
  allTags: [],
  listError: null,
  selectedPath: null,
  document: null,
  documentError: null,
  viewMode: 'rendered',
  collapsed: new Set(),
  expanded: new Set(),
  storeIsEmpty: false,
  // Paths ticked in the file list, used as the chat context.
  selected: loadSelection(),
};

const el = {
  search: document.getElementById('search-input'),
  refresh: document.getElementById('refresh-button'),
  tagsSection: document.getElementById('tags-section'),
  tagList: document.getElementById('tag-list'),
  clearTags: document.getElementById('clear-tags-button'),
  filesHeading: document.getElementById('files-heading'),
  filesCount: document.getElementById('files-count'),
  fileList: document.getElementById('file-list'),
  viewer: document.getElementById('viewer'),
};

let listRequest = 0;
let documentRequest = 0;

// --- selection ---------------------------------------------------------------

function loadSelection() {
  try {
    const stored = JSON.parse(localStorage.getItem(SELECTION_KEY) || '[]');
    return new Set(stored.filter((path) => typeof path === 'string'));
  } catch {
    return new Set();
  }
}

/** Selects or deselects documents and updates everything that shows it. */
function setSelected(paths, selected) {
  for (const path of paths) {
    if (selected) state.selected.add(path);
    else state.selected.delete(path);
  }
  selectionChanged();
}

function selectionChanged() {
  localStorage.setItem(SELECTION_KEY, JSON.stringify([...state.selected]));
  syncSelectionControls();
  document.dispatchEvent(new CustomEvent('mddb:selection'));
}

/** Brings the checkboxes in the list and viewer in line with the selection. */
function syncSelectionControls() {
  for (const box of el.fileList.querySelectorAll('input[data-path]')) {
    box.checked = state.selected.has(box.dataset.path);
  }
  for (const box of el.fileList.querySelectorAll('input[data-folder]')) {
    const inside = state.documents.filter((doc) =>
      doc.path.startsWith(box.dataset.folder),
    );
    const count = inside.filter((doc) => state.selected.has(doc.path)).length;
    box.checked = inside.length > 0 && count === inside.length;
    box.indeterminate = count > 0 && count < inside.length;
  }
  const toggle = el.viewer.querySelector('[data-select-toggle]');
  if (toggle && state.document) {
    const on = state.selected.has(state.document.path);
    toggle.setAttribute('aria-pressed', String(on));
    toggle.textContent = on ? 'In chat context' : 'Add to chat';
  }
}

function selectBox(props) {
  return h('input', {type: 'checkbox', class: 'select-box', ...props});
}

// --- helpers ---------------------------------------------------------------

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === false || value == null) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child != null));
  return node;
}

function encodePath(path) {
  return path.split('/').map(encodeURIComponent).join('/');
}

function hashFor(path) {
  return '#/' + encodePath(path);
}

function pathFromHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  if (!raw) return null;
  try {
    return raw.split('/').map(decodeURIComponent).join('/');
  } catch {
    return null;
  }
}

async function fetchJson(url) {
  const response = await fetch(url, {headers: {Accept: 'application/json'}});
  let body = null;
  try {
    body = await response.json();
  } catch {
    // Fall through to the status-based error below.
  }
  if (!response.ok) {
    throw new Error((body && body.error) || `Request failed (${response.status})`);
  }
  return body;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function isFiltering() {
  return Boolean(state.query.trim()) || state.tags.size > 0;
}

// --- data ------------------------------------------------------------------

async function loadList() {
  const request = ++listRequest;
  const params = new URLSearchParams({limit: '5000'});
  if (state.query.trim()) params.set('q', state.query.trim());
  for (const tag of state.tags) params.append('tag', tag);
  try {
    const [list, tags] = await Promise.all([
      fetchJson(`${API}?${params}`),
      fetchJson('api/tags'),
    ]);
    if (request !== listRequest) return;
    state.documents = list.documents;
    state.total = list.total;
    state.allTags = tags.tags;
    state.listError = null;
    if (!isFiltering()) state.storeIsEmpty = list.total === 0;
    if (!isFiltering() && list.total === list.documents.length) {
      // The full list is known: forget selected documents that are gone.
      const existing = new Set(list.documents.map((doc) => doc.path));
      const gone = [...state.selected].filter((path) => !existing.has(path));
      if (gone.length) setSelected(gone, false);
    }
    // Drop selected tags that no longer exist anywhere.
    const known = new Set(tags.tags.map((entry) => entry.tag));
    const stale = [...state.tags].filter((tag) => !known.has(tag));
    if (stale.length) {
      stale.forEach((tag) => state.tags.delete(tag));
      loadList();
      return;
    }
  } catch (error) {
    if (request !== listRequest) return;
    state.listError = error.message;
  }
  renderSidebar();
  if (!state.selectedPath) renderViewer();
}

async function loadDocument(path) {
  const request = ++documentRequest;
  state.selectedPath = path;
  state.document = null;
  state.documentError = null;
  renderSidebar();
  if (!path) {
    renderViewer();
    return;
  }
  try {
    const doc = await fetchJson(`${API}/${encodePath(path)}`);
    if (request !== documentRequest) return;
    state.document = doc;
  } catch (error) {
    if (request !== documentRequest) return;
    state.documentError = error.message;
  }
  renderViewer();
  el.viewer.scrollTop = 0;
}

// --- sidebar ---------------------------------------------------------------

function renderSidebar() {
  renderTags();
  renderFiles();
}

function renderTags() {
  el.tagsSection.hidden = state.allTags.length === 0;
  el.clearTags.hidden = state.tags.size === 0;
  el.tagList.replaceChildren(
    ...state.allTags.map(({tag, count}) =>
      h(
        'button',
        {
          type: 'button',
          class: 'tag-chip',
          'aria-pressed': String(state.tags.has(tag)),
          onclick: () => toggleTag(tag),
        },
        tag,
        h('span', {class: 'tag-count'}, String(count)),
      ),
    ),
  );
}

function toggleTag(tag) {
  if (state.tags.has(tag)) state.tags.delete(tag);
  else state.tags.add(tag);
  loadList();
}

function renderFiles() {
  const searching = Boolean(state.query.trim());
  el.filesHeading.textContent = searching ? 'Results' : 'Files';
  el.filesCount.textContent = state.listError ? '' : String(state.total);

  if (state.listError) {
    el.fileList.replaceChildren(
      h('p', {class: 'notice error', role: 'alert'}, state.listError),
    );
    return;
  }
  if (state.documents.length === 0) {
    el.fileList.replaceChildren(
      h(
        'p',
        {class: 'notice'},
        isFiltering() ? 'No documents match.' : 'No documents yet.',
      ),
    );
    return;
  }
  const more =
    state.total > state.documents.length
      ? h(
          'p',
          {class: 'notice'},
          `Showing the first ${state.documents.length} of ${state.total}.`,
        )
      : null;
  el.fileList.replaceChildren(
    searching ? renderResults() : renderTree(buildTree(state.documents)),
    ...(more ? [more] : []),
  );
  syncSelectionControls();
}

function renderResults() {
  return h(
    'ul',
    {class: 'results'},
    ...state.documents.map((doc) => {
      const snippet = h('span', {class: 'result-snippet'});
      snippet.innerHTML = DOMPurify.sanitize(doc.snippet_html || '', {
        ALLOWED_TAGS: ['mark'],
        ALLOWED_ATTR: [],
      });
      return h(
        'li',
        {class: 'select-row'},
        selectBox({
          dataset: {path: doc.path},
          'aria-label': `Use ${doc.path} in chat`,
          onchange: (event) => setSelected([doc.path], event.target.checked),
        }),
        h(
          'a',
          {
            class: 'result',
            href: hashFor(doc.path),
            'aria-current': doc.path === state.selectedPath ? 'page' : null,
          },
          h('span', {class: 'result-title'}, doc.title),
          h('span', {class: 'result-path'}, doc.path),
          snippet,
        ),
      );
    }),
  );
}

function buildTree(documents) {
  const root = {folders: new Map(), files: []};
  for (const doc of documents) {
    const segments = doc.path.split('/');
    const name = segments.pop();
    let node = root;
    let prefix = '';
    for (const segment of segments) {
      prefix += segment + '/';
      if (!node.folders.has(segment)) {
        node.folders.set(segment, {path: prefix, folders: new Map(), files: []});
      }
      node = node.folders.get(segment);
    }
    node.files.push({name, doc});
  }
  return root;
}

function isFolderOpen(folderPath) {
  if (state.selectedPath && state.selectedPath.startsWith(folderPath)) {
    return !state.collapsed.has(folderPath);
  }
  if (state.collapsed.has(folderPath)) return false;
  if (state.expanded.has(folderPath)) return true;
  // With filters on, or a small store, show everything expanded.
  return isFiltering() || state.total <= AUTO_EXPAND_LIMIT;
}

function renderTree(node) {
  const folders = [...node.folders.entries()].sort(([a], [b]) =>
    a.localeCompare(b),
  );
  const files = [...node.files].sort((a, b) => a.name.localeCompare(b.name));
  return h(
    'ul',
    {class: 'tree'},
    ...folders.map(([name, folder]) => {
      const details = h(
        'details',
        {open: isFolderOpen(folder.path)},
        h(
          'summary',
          {class: 'tree-folder'},
          selectBox({
            dataset: {folder: folder.path},
            'aria-label': `Use everything in ${folder.path} in chat`,
            onchange: (event) =>
              setSelected(
                state.documents
                  .filter((doc) => doc.path.startsWith(folder.path))
                  .map((doc) => doc.path),
                event.target.checked,
              ),
          }),
          h('span', {class: 'tree-folder-name'}, name),
        ),
        renderTree(folder),
      );
      details.addEventListener('toggle', () => {
        const set = details.open ? state.expanded : state.collapsed;
        const other = details.open ? state.collapsed : state.expanded;
        set.add(folder.path);
        other.delete(folder.path);
      });
      return h('li', {}, details);
    }),
    ...files.map(({name, doc}) =>
      h(
        'li',
        {class: 'select-row'},
        selectBox({
          dataset: {path: doc.path},
          'aria-label': `Use ${doc.path} in chat`,
          onchange: (event) => setSelected([doc.path], event.target.checked),
        }),
        h(
          'a',
          {
            class: 'tree-file',
            href: hashFor(doc.path),
            title: doc.title,
            'aria-current': doc.path === state.selectedPath ? 'page' : null,
          },
          name,
        ),
      ),
    ),
  );
}

// --- viewer ----------------------------------------------------------------

function renderViewer() {
  if (state.documentError) {
    el.viewer.replaceChildren(
      h(
        'div',
        {class: 'placeholder'},
        h('h1', {}, 'Could not open document'),
        h('p', {class: 'error', role: 'alert'}, state.documentError),
        h('p', {class: 'muted'}, state.selectedPath),
      ),
    );
    return;
  }
  if (state.selectedPath && !state.document) {
    el.viewer.replaceChildren(h('p', {class: 'muted loading'}, 'Loading…'));
    return;
  }
  if (!state.document) {
    el.viewer.replaceChildren(renderWelcome());
    document.title = 'mddb';
    return;
  }

  const doc = state.document;
  document.title = `${doc.title} · mddb`;
  const body =
    state.viewMode === 'raw'
      ? h('pre', {class: 'raw'}, h('code', {}, doc.content))
      : renderMarkdown(doc);

  el.viewer.replaceChildren(
    h(
      'header',
      {class: 'doc-header'},
      renderBreadcrumb(doc.path),
      h(
        'div',
        {class: 'doc-meta'},
        h('span', {}, `Updated ${formatTime(doc.updated_at)}`),
        h('span', {}, formatBytes(doc.size_bytes)),
        h(
          'div',
          {class: 'doc-tags'},
          ...doc.tags.map((tag) =>
            h(
              'button',
              {
                type: 'button',
                class: 'tag-chip small',
                title: `Filter by tag “${tag}”`,
                'aria-pressed': String(state.tags.has(tag)),
                onclick: () => toggleTag(tag),
              },
              tag,
            ),
          ),
        ),
        h('span', {class: 'spacer'}),
        h(
          'div',
          {class: 'segmented', role: 'group', 'aria-label': 'View mode'},
          ...['rendered', 'raw'].map((mode) =>
            h(
              'button',
              {
                type: 'button',
                'aria-pressed': String(state.viewMode === mode),
                onclick: () => {
                  state.viewMode = mode;
                  renderViewer();
                },
              },
              mode === 'rendered' ? 'Rendered' : 'Raw',
            ),
          ),
        ),
        h(
          'button',
          {
            type: 'button',
            class: 'button',
            dataset: {selectToggle: ''},
            'aria-pressed': String(state.selected.has(doc.path)),
            onclick: () =>
              setSelected([doc.path], !state.selected.has(doc.path)),
          },
          state.selected.has(doc.path) ? 'In chat context' : 'Add to chat',
        ),
        h(
          'a',
          {
            class: 'button',
            href: `${API}/${encodePath(doc.path)}?raw=1`,
            download: doc.path.split('/').pop(),
          },
          'Download',
        ),
      ),
    ),
    body,
  );
}

function renderBreadcrumb(path) {
  const segments = path.split('/');
  const parts = [];
  segments.forEach((segment, index) => {
    if (index > 0) parts.push(h('span', {class: 'crumb-sep'}, '/'));
    parts.push(
      h(
        'span',
        {class: index === segments.length - 1 ? 'crumb current' : 'crumb'},
        segment,
      ),
    );
  });
  return h('div', {class: 'breadcrumb', 'aria-label': 'Document path'}, ...parts);
}

function splitFrontMatter(content) {
  const match = /^---[ \t]*\r?\n([\s\S]*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/.exec(
    content,
  );
  if (!match) return {frontMatter: null, body: content};
  return {frontMatter: match[1], body: content.slice(match[0].length)};
}

function renderMarkdown(doc) {
  const {frontMatter, body} = splitFrontMatter(doc.content);
  const article = renderMarkdownBody(body, {basePath: doc.path});
  if (frontMatter !== null) {
    article.prepend(
      h(
        'details',
        {class: 'front-matter', open: true},
        h('summary', {}, 'Front matter'),
        h('pre', {}, h('code', {}, frontMatter)),
      ),
    );
  }
  return article;
}

/**
 * Renders untrusted markdown to a sanitized element.
 *
 * basePath is the document the text belongs to, used to resolve relative links
 * to other documents. With inline set (chat messages), images are dropped and
 * headings get no ids.
 */
function renderMarkdownBody(markdown, {basePath = '', inline = false} = {}) {
  const article = h('article', {class: 'markdown'});
  const html = marked.parse(markdown, {gfm: true});
  const forbidden = ['style', 'form', 'button', 'textarea', 'select'];
  article.innerHTML = DOMPurify.sanitize(html, {
    USE_PROFILES: {html: true},
    FORBID_TAGS: inline ? [...forbidden, 'img', 'picture', 'video', 'audio'] : forbidden,
    FORBID_ATTR: ['style'],
  });
  // The only inputs kept are the read-only checkboxes of task lists.
  for (const input of article.querySelectorAll('input')) {
    if (input.type === 'checkbox') {
      const checked = input.checked;
      for (const {name} of [...input.attributes]) input.removeAttribute(name);
      input.type = 'checkbox';
      input.checked = checked;
      input.disabled = true;
    } else {
      input.remove();
    }
  }
  // Give headings GitHub-style ids so in-page links like #some-heading work.
  const usedIds = new Set();
  const headings = inline ? [] : article.querySelectorAll('h1, h2, h3, h4, h5, h6');
  for (const heading of headings) {
    const base =
      heading.textContent
        .trim()
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s_-]/gu, '')
        .replace(/\s/g, '-') || 'section';
    let id = base;
    for (let n = 1; usedIds.has(id); n++) id = `${base}-${n}`;
    usedIds.add(id);
    heading.id = id;
  }
  // External links open in a new tab, links to other .md documents navigate
  // inside the app, and in-page anchors scroll (the hash is used for routing).
  for (const link of article.querySelectorAll('a[href]')) {
    const href = link.getAttribute('href');
    if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//')) {
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
    } else if (!href.startsWith('#')) {
      const target = resolveRelative(basePath, href);
      if (target) link.setAttribute('href', hashFor(target));
    } else {
      // In-page anchors would clobber the hash route; scroll instead.
      link.dataset.anchor = href.slice(1);
      link.removeAttribute('href');
      link.tabIndex = 0;
      link.setAttribute('role', 'link');
    }
  }
  return article;
}

/** Resolves a relative link to another .md document, or returns null. */
function resolveRelative(fromPath, href) {
  const clean = href.split('#')[0].split('?')[0];
  if (!/\.md$/i.test(clean)) return null;
  let decoded;
  try {
    decoded = decodeURIComponent(clean);
  } catch {
    return null;
  }
  const stack = decoded.startsWith('/') ? [] : fromPath.split('/').slice(0, -1);
  for (const segment of decoded.split('/')) {
    if (!segment || segment === '.') continue;
    if (segment === '..') {
      if (!stack.length) return null;
      stack.pop();
    } else {
      stack.push(segment);
    }
  }
  return stack.join('/');
}

function renderWelcome() {
  const origin = location.origin;
  if (!state.storeIsEmpty) {
    return h(
      'div',
      {class: 'placeholder'},
      h('h1', {}, 'mddb'),
      h(
        'p',
        {class: 'muted'},
        'Pick a file on the left, filter by tag, or search across everything.',
      ),
    );
  }
  return h(
    'div',
    {class: 'placeholder'},
    h('h1', {}, 'No documents yet'),
    h('p', {class: 'muted'}, 'Send a markdown file to the API to get started:'),
    h(
      'pre',
      {class: 'raw'},
      h(
        'code',
        {},
        `curl -X PUT '${origin}/api/documents/notes/hello.md?tags=example,notes' \\\n` +
          `  -H 'Content-Type: text/markdown' \\\n` +
          `  --data-binary @hello.md`,
      ),
    ),
  );
}

// --- events ----------------------------------------------------------------

let searchTimer = null;
el.search.addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = el.search.value;
    loadList();
  }, SEARCH_DEBOUNCE_MS);
});

el.search.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && el.search.value) {
    el.search.value = '';
    state.query = '';
    loadList();
  } else if (event.key === 'Enter') {
    clearTimeout(searchTimer);
    state.query = el.search.value;
    loadList().then(() => {
      if (state.query.trim() && state.documents.length) {
        location.hash = hashFor(state.documents[0].path);
      }
    });
  }
});

document.addEventListener('keydown', (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
  if (event.key === '/' && !typing && !event.ctrlKey && !event.metaKey) {
    event.preventDefault();
    el.search.focus();
    el.search.select();
  }
});

el.clearTags.addEventListener('click', () => {
  state.tags.clear();
  loadList();
});

el.refresh.addEventListener('click', () => {
  loadList();
  if (state.selectedPath) loadDocument(state.selectedPath);
});

function followAnchor(event) {
  const link = event.target.closest('[data-anchor]');
  if (!link) return;
  if (event.type === 'keydown' && event.key !== 'Enter') return;
  event.preventDefault();
  const id = link.dataset.anchor;
  const target =
    el.viewer.querySelector(`[id="${CSS.escape(id)}"]`) ||
    el.viewer.querySelector(`[name="${CSS.escape(id)}"]`);
  if (target) target.scrollIntoView({behavior: 'smooth', block: 'start'});
}
el.viewer.addEventListener('click', followAnchor);
el.viewer.addEventListener('keydown', followAnchor);

window.addEventListener('hashchange', () => loadDocument(pathFromHash()));

loadList();
loadDocument(pathFromHash());
