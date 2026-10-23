const $ = id => document.getElementById(id);
const byteLength = value => new TextEncoder().encode(value).length;
const order = ['raw_json_string_arguments', 'normalized_json_string_arguments', 'object_arguments', 'medium_alias', 'numeric_effort_62', 'numeric_effort_42', 'mid_conversation_system', 'basic_chat'];
let data;
let cases = [];
let selectedCase;
let view = 'prompts';
let showWhitespace = false;

function announce(message, error = false) {
  $('status').textContent = message;
  $('status').classList.toggle('error', error);
}

function exactMatch(item) {
  return item.reference.error === null && item.template.error === null &&
    typeof item.reference.output === 'string' && typeof item.template.output === 'string' &&
    item.reference.output === item.template.output;
}

function resultLabel(item) {
  const match = exactMatch(item);
  if (match !== item.observed_match) return 'FIXTURE INTEGRITY WARNING';
  if (match !== item.expected_match) return match ? 'UNEXPECTED MATCH' : 'UNEXPECTED DIFFERENCE';
  return match ? 'EXACT MATCH' : 'EXPECTED DIFFERENCE';
}

function visibleText(text) {
  return showWhitespace ? text.replaceAll(' ', '·').replaceAll('\t', '⇥\t').replaceAll('\n', '↵\n') : text;
}

function renderLiteral(element, text, highlight = false) {
  const shown = visibleText(text);
  if (!highlight) { element.textContent = shown; return; }
  const fragment = document.createDocumentFragment();
  let last = 0;
  for (const match of shown.matchAll(/<[^>]*>/gu)) {
    fragment.append(document.createTextNode(shown.slice(last, match.index)));
    const span = document.createElement('span'); span.className = 'special-token'; span.textContent = match[0];
    fragment.append(span); last = match.index + match[0].length;
  }
  fragment.append(document.createTextNode(shown.slice(last)));
  element.replaceChildren(fragment);
}

function linkFor(item, nextView = view) {
  const url = new URL(location.href);
  url.hash = new URLSearchParams({ case: item.id, view: nextView, ...(showWhitespace ? { ws: '1' } : {}) }).toString();
  return url.href;
}

function updateAddress() {
  if (!selectedCase) return;
  history.replaceState(null, '', linkFor(selectedCase));
}

function renderCaseList() {
  const fragment = document.createDocumentFragment();
  cases.forEach((item, index) => {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'case-button';
    button.dataset.caseId = item.id; button.setAttribute('aria-current', String(item.id === selectedCase.id));
    const number = document.createElement('span'); number.className = 'case-number'; number.textContent = String(index + 1).padStart(2, '0');
    const name = document.createElement('span'); name.className = 'case-name'; name.textContent = item.title;
    const dot = document.createElement('span'); dot.className = `case-dot${exactMatch(item) ? '' : ' difference'}`; dot.setAttribute('aria-hidden', 'true');
    button.append(number, name, dot);
    button.addEventListener('click', () => selectCase(item.id));
    button.addEventListener('keydown', event => {
      if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? cases.length - 1 : (index + (event.key === 'ArrowUp' ? -1 : 1) + cases.length) % cases.length;
      selectCase(cases[next].id, true);
    });
    fragment.append(button);
  });
  $('case-list').replaceChildren(fragment);
}

function renderRenderer(name, result) {
  const error = $(`${name}-error`), output = $(`${name}-output`), meta = $(`${name}-meta`);
  const hasOutput = result.error === null && typeof result.output === 'string';
  error.hidden = !result.error;
  error.replaceChildren();
  if (result.error) {
    const type = document.createElement('strong'); type.textContent = result.error.type;
    error.append(type, document.createTextNode(result.error.message));
  }
  output.classList.toggle('empty-output', !hasOutput);
  if (hasOutput) {
    renderLiteral(output, result.output, true);
    meta.textContent = `${byteLength(result.output)} UTF-8 bytes · LF at end: ${result.output.endsWith('\n') ? 'yes' : 'no'}`;
  } else {
    output.textContent = 'No prompt was returned.';
    meta.textContent = 'Renderer raised an error. No output text to compare.';
  }
  $(`copy-${name}`).disabled = !hasOutput;
}

// Preserve all blank lines and an empty terminal line, so a trailing LF is
// visible as a distinct line boundary rather than silently disappearing.
function lineOperations(left, right) {
  const a = left.split('\n'), b = right.split('\n');
  const lengths = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i--) for (let j = b.length - 1; j >= 0; j--) {
    lengths[i][j] = a[i] === b[j] ? lengths[i + 1][j + 1] + 1 : Math.max(lengths[i + 1][j], lengths[i][j + 1]);
  }
  const rows = []; let i = 0, j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      rows.push({ kind: 'same', old: i + 1, next: j + 1, text: a[i], terminal: a[i] === '' && i === a.length - 1 && j === b.length - 1 && left.endsWith('\n') && right.endsWith('\n') }); i++; j++;
    } else if (i < a.length && (j === b.length || lengths[i + 1][j] >= lengths[i][j + 1])) {
      rows.push({ kind: 'removed', old: i + 1, next: null, text: a[i], terminal: a[i] === '' && i === a.length - 1 && left.endsWith('\n') }); i++;
    } else {
      rows.push({ kind: 'added', old: null, next: j + 1, text: b[j], terminal: b[j] === '' && j === b.length - 1 && right.endsWith('\n') }); j++;
    }
  }
  return rows;
}

function renderDiff() {
  const item = selectedCase;
  const left = item.reference, right = item.template;
  const target = $('line-diff'); target.replaceChildren();
  if (left.error || right.error || typeof left.output !== 'string' || typeof right.output !== 'string') {
    $('diff-message').textContent = 'A text diff needs two successful outputs. Inspect the renderer error and the other output in Rendered prompts.';
    const summary = document.createElement('div'); summary.className = 'diff-error-summary';
    for (const [label, result] of [['Python reference', left], ['Base Jinja', right]]) {
      const title = document.createElement('strong'); title.textContent = `${label}: ${result.error ? result.error.type : 'output returned'}`;
      summary.append(title);
      if (result.error) summary.append(document.createTextNode(result.error.message + '\n\n'));
    }
    target.append(summary);
    return;
  }
  $('diff-message').textContent = exactMatch(item) ? 'The exact prompt strings match in this saved case.' : `${resultLabel(item).toLowerCase().replace(/^./, char => char.toUpperCase())}. Python: ${byteLength(left.output)} UTF-8 bytes. Jinja: ${byteLength(right.output)} UTF-8 bytes.`;
  const fragment = document.createDocumentFragment();
  for (const row of lineOperations(left.output, right.output)) {
    const element = document.createElement('div'); element.className = `diff-row ${row.kind}${row.terminal ? ' eof' : ''}`;
    element.dataset.kind = row.kind;
    const old = document.createElement('span'); old.textContent = row.old ?? ''; old.setAttribute('aria-hidden', 'true');
    const next = document.createElement('span'); next.textContent = row.next ?? ''; next.setAttribute('aria-hidden', 'true');
    const sign = document.createElement('span'); sign.className = 'diff-sign'; sign.textContent = row.kind === 'removed' ? '−' : row.kind === 'added' ? '+' : ' '; sign.setAttribute('aria-hidden', 'true');
    const content = document.createElement('code'); content.dataset.rawLine = row.text;
    content.textContent = row.terminal ? '[empty terminal line / final LF]' : visibleText(row.text) || '[empty line]';
    element.append(old, next, sign, content); fragment.append(element);
  }
  target.append(fragment);
}

function renderView() {
  for (const name of ['prompts', 'history', 'diff']) {
    const active = name === view;
    $(`tab-${name}`).setAttribute('aria-selected', String(active));
    $(`tab-${name}`).tabIndex = active ? 0 : -1;
    $(`panel-${name}`).hidden = !active;
  }
  $('show-whitespace').checked = showWhitespace;
  $('whitespace-legend').hidden = !showWhitespace;
  renderRenderer('reference', selectedCase.reference);
  renderRenderer('template', selectedCase.template);
  renderLiteral($('history-output'), JSON.stringify(selectedCase.history, null, 2));
  renderDiff();
}

function selectCase(id, focus = false) {
  const item = cases.find(candidate => candidate.id === id);
  if (!item) return;
  selectedCase = item;
  $('case-index').textContent = `${String(cases.indexOf(item) + 1).padStart(2, '0')} / ${String(cases.length).padStart(2, '0')}`;
  $('case-title').textContent = item.title;
  $('case-summary').textContent = item.summary;
  $('mode-value').textContent = item.config.thinking_mode;
  $('effort-value').textContent = item.config.reasoning_effort === null ? 'default' : String(item.config.reasoning_effort);
  const match = exactMatch(item), coherent = match === item.observed_match;
  $('result-badge').textContent = resultLabel(item);
  $('result-badge').className = `result-badge${match ? '' : ' difference'}${coherent ? '' : ' integrity-error'}`;
  $('diff-dot').hidden = match;
  $('normalization-label').textContent = item.normalization.applied ? 'NORMALIZATION APPLIED' : 'INPUT CONTRACT';
  $('normalization-text').textContent = item.normalization.explanation;
  const related = cases.find(candidate => candidate.id === item.normalization.related_case_id);
  $('related-case').hidden = !related;
  $('related-case').textContent = related ? `Compare: ${related.title} ↗` : '';
  renderCaseList(); renderView(); updateAddress();
  announce(`${item.title}. ${resultLabel(item).toLowerCase().replace(/^./, char => char.toUpperCase())}.`);
  if (focus) $('case-list').querySelector(`[data-case-id="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
}

function exportCase() {
  return {
    schema_version: data.schema_version, title: data.title, scope: data.scope,
    sources: data.sources, encoder_bytes_identical: data.encoder_bytes_identical,
    rendering_environment: data.rendering_environment, case: selectedCase
  };
}

async function copyText(text, success) {
  try { await navigator.clipboard.writeText(text); announce(success); }
  catch {
    const dialog = document.createElement('dialog'); dialog.className = 'copy-dialog';
    const title = document.createElement('h2'); title.textContent = 'Copy the exact text';
    const explanation = document.createElement('p'); explanation.textContent = 'Clipboard access is unavailable. Select this text and use your device’s Copy command.';
    const textarea = document.createElement('textarea'); textarea.readOnly = true; textarea.value = text; textarea.setAttribute('aria-label', 'Exact text to copy');
    const close = document.createElement('button'); close.type = 'button'; close.textContent = 'Close'; close.addEventListener('click', () => dialog.close());
    dialog.append(title, explanation, textarea, close); document.body.append(dialog);
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    dialog.showModal(); textarea.focus(); textarea.select();
    announce('Clipboard access is unavailable. Exact text is selected in the copy dialog.');
  }
}

for (const name of ['reference', 'template']) $(`copy-${name}`).addEventListener('click', () => {
  const result = selectedCase[name];
  if (!result.error && typeof result.output === 'string') copyText(result.output, 'Exact original prompt copied, without display markers.');
});
$('copy-history').addEventListener('click', () => copyText(JSON.stringify(selectedCase.history, null, 2), 'Exact synthetic history copied.'));
$('copy-case').addEventListener('click', () => copyText(JSON.stringify(exportCase(), null, 2), 'Case and source provenance copied.'));
$('copy-link').addEventListener('click', () => copyText(linkFor(selectedCase), 'Case link copied. It contains only this saved case and view.'));
$('related-case').addEventListener('click', () => selectCase(selectedCase.normalization.related_case_id));
$('download-case').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify(exportCase(), null, 2) + '\n'], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob), anchor = document.createElement('a');
  anchor.href = url; anchor.download = `deepseek-${selectedCase.id}.json`; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  announce('Saved case downloaded with its source revisions and hashes.');
});

for (const name of ['prompts', 'history', 'diff']) {
  $(`tab-${name}`).addEventListener('click', () => { view = name; renderView(); updateAddress(); });
  $(`tab-${name}`).addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const options = ['prompts', 'history', 'diff'], index = options.indexOf(name);
    view = options[event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (index + (event.key === 'ArrowLeft' ? -1 : 1) + 3) % 3];
    renderView(); updateAddress(); $(`tab-${view}`).focus();
  });
}
$('show-whitespace').addEventListener('change', () => { showWhitespace = $('show-whitespace').checked; renderView(); updateAddress(); });

function renderSources() {
  $('source-list').replaceChildren();
  for (const source of data.sources) {
    const article = document.createElement('article'); article.className = 'source-item';
    const heading = document.createElement('div'); heading.className = 'source-title';
    const link = document.createElement('a'); link.textContent = `${source.label} ↗`;
    link.href = source.url.startsWith('https://huggingface.co/') ? source.url : '#sources'; link.target = '_blank'; link.rel = 'noreferrer';
    const pin = document.createElement('span'); pin.textContent = source.revision.slice(0, 10);
    heading.append(link, pin);
    const hash = document.createElement('p'); hash.className = 'source-hash';
    const rev = document.createElement('span'); rev.className = 'full-pin';
    const revLabel = document.createElement('strong'); revLabel.textContent = 'REVISION '; rev.append(revLabel, document.createTextNode(source.revision));
    const digest = document.createElement('span'); digest.className = 'full-pin';
    const hashLabel = document.createElement('strong'); hashLabel.textContent = 'SHA-256 '; digest.append(hashLabel, document.createTextNode(source.sha256));
    hash.append(rev, digest); article.append(heading, hash); $('source-list').append(article);
  }
  $('encoder-note').textContent = data.encoder_bytes_identical ? 'The base and 1017 Python encoder artifacts are byte-identical at these pins. The input contract is the useful comparison.' : 'Inspect each pinned artifact and its exact checksum before comparing results.';
  $('environment-output').textContent = JSON.stringify(data.rendering_environment, null, 2);
  $('fixture-scope').textContent = data.scope;
}

function readLocation() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ''));
  const requested = params.get('case');
  view = ['prompts', 'history', 'diff'].includes(params.get('view')) ? params.get('view') : 'prompts';
  showWhitespace = params.get('ws') === '1';
  selectCase(cases.some(item => item.id === requested) ? requested : 'raw_json_string_arguments');
  if (requested && !cases.some(item => item.id === requested)) announce('That saved case was not found. Showing the raw-string example.');
}

try {
  const response = await fetch('fixtures.json', { cache: 'no-store' });
  if (!response.ok) throw new Error('Saved cases could not be loaded.');
  data = await response.json();
  if (data.schema_version !== 1 || !Array.isArray(data.cases) || data.cases.length !== 8 || !Array.isArray(data.sources)) throw new Error('The saved fixture file has an unsupported format.');
  cases = [...data.cases].sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
  $('case-count').textContent = cases.length;
  $('match-count').textContent = cases.filter(exactMatch).length;
  $('difference-count').textContent = cases.filter(item => !exactMatch(item) && !item.expected_match).length;
  renderSources(); readLocation();
  $('loading').hidden = true; $('case-content').hidden = false;
  window.addEventListener('hashchange', readLocation);
} catch (error) {
  for (const name of ['case-count', 'match-count', 'difference-count']) $(name).textContent = 'unavailable';
  $('loading').textContent = `${error.message} Reload this page to try again.`;
  $('loading').classList.add('error'); announce('Saved examples are unavailable.', true);
}
