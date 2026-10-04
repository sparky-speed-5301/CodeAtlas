// Minimal, observable VS Code boundary. Commands/providers/rendering are the compiled runtime.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const Module = require('module');

function compiledEntry(root) {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const entry = path.resolve(root, manifest.main);
  assert.strictEqual(entry, path.join(root, 'out', 'extension.js'),
    'Phase 10E requires package.json.main to resolve to out/extension.js');
  assert.ok(fs.existsSync(entry) && fs.statSync(entry).isFile(),
    'Phase 10E compiled output missing; run npm run compile (no legacy fallback)');
  return entry;
}

function createHost(root, settings) {
  const commands = new Map(), views = new Map(), panels = [], messages = [], inputs = [], picks = [];
  const updates = [], clipboard = [], editors = new Map(), intervals = new Map();
  const activeListeners = [], cursorListeners = [];
  let nextInput, nextPick;
  const disposable = () => ({ dispose() {} });
  const subscribe = listeners => (fn, _this, subscriptions) => {
    listeners.push(fn);
    const d = { dispose() { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); } };
    subscriptions?.push(d);
    return d;
  };
  class Range {
    constructor(sl, sc, el, ec) {
      assert.ok([sl, sc, el, ec].every(n => Number.isInteger(n) && n >= 0), 'Invalid editor range');
      this.start = { line: sl, character: sc }; this.end = { line: el, character: ec };
    }
  }
  const vscode = {
    TreeItem: class { constructor(label) { this.label = label; } },
    TreeItemCollapsibleState: { None: 0 }, ViewColumn: { Beside: 2 },
    ConfigurationTarget: { Global: 1, Workspace: 2 },
    OverviewRulerLane: { Right: 4 }, TextEditorRevealType: { InCenter: 1 }, Range,
    Selection: class { constructor(start, end) { this.start = start; this.end = end; this.active = end; } },
    MarkdownString: class { constructor() { this.value = ''; } appendMarkdown(s) { this.value += s; } },
    EventEmitter: class {
      constructor() { this.listeners = []; this.event = subscribe(this.listeners); }
      fire(value) { this.listeners.forEach(fn => fn(value)); }
      dispose() { this.listeners.length = 0; }
    },
    Uri: { joinPath: (base, rel) => ({ fsPath: path.join(base.fsPath, rel) }) },
    env: { clipboard: { writeText: async text => { clipboard.push(text); } } },
    commands: {
      registerCommand(id, handler) { commands.set(id, handler); return { dispose() { commands.delete(id); } }; },
      async executeCommand(id, ...args) { assert.ok(commands.has(id), `Unregistered command: ${id}`); return commands.get(id)(...args); },
    },
    workspace: {
      isTrusted: true, workspaceFolders: [{ uri: { fsPath: root } }],
      getConfiguration: () => ({ get: key => settings[key], inspect: key => ({ globalValue: settings[key] }),
        update: async (key, value) => { settings[key] = value; updates.push([key, value]); } }),
      onDidChangeConfiguration: disposable, onDidChangeTextDocument: disposable,
      async openTextDocument(uri) {
        const text = fs.readFileSync(uri.fsPath, 'utf8');
        return { uri, lineCount: text.split('\n').length };
      },
    },
    window: {
      activeTextEditor: null,
      createTextEditorDecorationType: options => ({ options, dispose() {} }),
      registerTreeDataProvider(id, provider) { views.set(id, provider); return disposable(); },
      onDidChangeActiveTextEditor: subscribe(activeListeners),
      onDidChangeTextEditorSelection: subscribe(cursorListeners),
      async showInformationMessage(text) { messages.push({ kind: 'info', text }); },
      async showWarningMessage(text) { messages.push({ kind: 'warning', text }); },
      async showErrorMessage(text) { messages.push({ kind: 'error', text }); },
      async showInputBox(options) { inputs.push(options); const v = nextInput; nextInput = undefined; return v; },
      async showQuickPick(items, options) { picks.push({ items, options }); return nextPick?.(items); },
      async showTextDocument(document) {
        let editor = editors.get(document.uri.fsPath);
        if (!editor) {
          editor = { document, selection: { active: { line: 0 } }, decorations: new Map(),
            revealRange(range) { this.revealed = range; },
            setDecorations(type, decorations) {
              for (const d of decorations) {
                assert.ok(d.range.start.line < document.lineCount && d.range.end.line < document.lineCount);
              }
              this.decorations.set(type, decorations);
            } };
          editors.set(document.uri.fsPath, editor);
        }
        vscode.window.activeTextEditor = editor;
        activeListeners.forEach(fn => fn(editor));
        return editor;
      },
      createWebviewPanel(_type, title) {
        const listeners = [], disposeListeners = [];
        const panel = { title, visible: true,
          webview: { html: '', onDidReceiveMessage: subscribe(listeners) },
          onDidDispose: subscribe(disposeListeners),
          async message(action) { for (const fn of listeners) await fn({ action }); },
          dispose() { disposeListeners.forEach(fn => fn()); } };
        panels.push(panel); return panel;
      },
    },
  };
  const originalRequire = Module.prototype.require;
  const originalInterval = global.setInterval, originalClear = global.clearInterval;
  return {
    vscode, commands, views, panels, messages, inputs, picks, updates, clipboard, intervals,
    install() {
      Module.prototype.require = function (id) { return id === 'vscode' ? vscode : originalRequire.apply(this, arguments); };
      // Advance only recurring extension polls explicitly. Real HTTP/deadline timers remain real.
      global.setInterval = fn => { const id = {}; intervals.set(id, fn); return id; };
      global.clearInterval = id => { intervals.delete(id); };
    },
    restore() { Module.prototype.require = originalRequire; global.setInterval = originalInterval; global.clearInterval = originalClear; },
    command(name, ...args) { return vscode.commands.executeCommand(`codeatlas.${name}`, ...args); },
    async poll() { for (const fn of [...intervals.values()]) await fn(); },
    input(value) { nextInput = value; }, pick(fn) { nextPick = fn; },
    cursor(editor, line) { editor.selection = { active: { line } }; cursorListeners.forEach(fn => fn({ textEditor: editor })); },
    labels(view) { return views.get(`codeatlas.${view}View`).getChildren().map(item => item.label); },
  };
}

module.exports = { compiledEntry, createHost };
