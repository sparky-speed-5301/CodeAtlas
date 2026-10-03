// Behavioral Phase 10C coverage, also invoked by test_sync.js and pytest.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const http = require('http');
const { EventEmitter } = require('events');
const serviceModule = fs.existsSync(path.join(__dirname, 'out', 'service.js')) ? './out/service' : './service';
const profilesModule = fs.existsSync(path.join(__dirname, 'out', 'profiles.js')) ? './out/profiles' : './profiles';
const { ServiceLifecycleManager, validateServiceUrl, parseServiceCommand, safeHealth } = require(serviceModule);
const { ProfileManager, validateProfile, DEFAULT_PROFILE } = require(profilesModule);

const health = (pid = 1234) => ({ status: 'ok', service: 'codeatlas-service', version: '0.1.0', pid, active_reviews: 0, provider: 'mock' });
const tick = () => new Promise(resolve => setImmediate(resolve));
function childProcess(pid = 1234) {
  const child = new EventEmitter();
  Object.assign(child, { pid, stdout: new EventEmitter(), stderr: new EventEmitter(), signals: [] });
  child.kill = signal => { child.signals.push(signal); queueMicrotask(() => child.emit('exit', null, signal)); return true; };
  return child;
}
function startup(child = childProcess()) {
  let spawned = false;
  return {
    child,
    spawn: (executable, args, options) => {
      assert.strictEqual(options.shell, false);
      assert.ok(args.includes('--host'));
      assert.strictEqual(args[args.indexOf('--host') + 1], '127.0.0.1');
      assert.notStrictEqual(options.cwd, '/workspace');
      spawned = true;
      return child;
    },
    client: { getHealth: async () => { if (!spawned) throw new Error('offline'); return health(child.pid); } },
    timeoutMs: 150,
  };
}

async function runTests(vscode) {
  if (!vscode) {
    vscode = require('./test_sync').mockVsCode;
  }
  const extModule = fs.existsSync(path.join(__dirname, 'out', 'extension.js')) ? './out/extension' : './extension';
  const ext = require(extModule);
  let count = 0;
  const test = async (name, fn) => { await fn(); count++; console.log(`✓ ${name}`); };
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codeatlas-extension-'));
  fs.mkdirSync(path.join(root, '.codeatlas'));
  const write = (name, value) => fs.writeFileSync(path.join(root, '.codeatlas', name), JSON.stringify(value));
  try {
    await test('External discovery: explicit URL, then workspace URL, no implicit fallback', async () => {
      write('service.json', { url: 'http://127.0.0.1:9876' });
      const manager = new ServiceLifecycleManager({ workspaceRoot: root, serviceUrl: 'http://127.0.0.1:8765', serviceMode: 'managed' });
      assert.strictEqual(manager.resolveServiceUrl(), 'http://127.0.0.1:8765');
      manager.explicitServiceUrl = null;
      assert.strictEqual(manager.resolveServiceUrl(), 'http://127.0.0.1:9876');
      write('service.json', { host: 'localhost', port: 9000 });
      assert.strictEqual(manager.resolveServiceUrl(), 'http://127.0.0.1:9000');
      write('service.json', { host: 'localhost', port: 80 });
      assert.strictEqual(manager.resolveServiceUrl(), 'http://127.0.0.1');
      fs.unlinkSync(path.join(root, '.codeatlas/service.json'));
      assert.strictEqual(manager.resolveServiceUrl(), null);
      await manager.discover();
      assert.strictEqual(manager.lastHealthStatus, 'not_configured');
      assert.match(manager.message, /Start Local Service/);
    });

    await test('Discovery priority prevents auto-start when a configured service is unreachable', async () => {
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed', autoStartService: true,
        serviceUrl: 'http://127.0.0.1:9000', clientFactory: () => ({ getHealth: async () => { throw new Error('secret-output'); } }) });
      manager.startManagedService = async () => assert.fail('must not fall through to startup');
      await assert.rejects(manager.discover(), /health failed/);
      assert.ok(!manager.lastErrorMessage.includes('secret-output'));
    });

    await test('Managed auto-start requires both explicit opt-ins', async () => {
      for (const [mode, auto, expected] of [['external', true, 0], ['managed', false, 0], ['managed', true, 1], ['disabled', true, 0]]) {
        const manager = new ServiceLifecycleManager({ serviceMode: mode, autoStartService: auto });
        let starts = 0;
        manager.startManagedService = async () => { starts++; };
        await manager.discover();
        assert.strictEqual(starts, expected);
      }
    });

    await test('Invalid URLs and workspace service schema fail closed without reflecting secrets', async () => {
      for (const value of ['https://127.0.0.1:8765', 'http://example.com', 'http://0.0.0.0', 'http://127.1',
        'http://127.0.0.1:0', 'http://127.0.0.1:99999', 'http://secret@localhost', 'http://localhost/?token=secret',
        'http://localhost/path', 'http://localhost/#secret', 'http://localhost\\@example.com', '\nhttp://localhost']) {
        assert.throws(() => validateServiceUrl(value));
        assert.throws(() => new ext.CodeAtlasClient(value), error => !error.message.includes('token=secret'));
      }
      assert.strictEqual(validateServiceUrl('http://[::1]:8765/'), 'http://[::1]:8765');
      write('service.json', { url: 'http://127.0.0.1:8765', token: 'secret' });
      assert.throws(() => new ServiceLifecycleManager({ workspaceRoot: root }).resolveServiceUrl(), /Invalid workspace/);
      fs.unlinkSync(path.join(root, '.codeatlas/service.json'));
    });

    await test('Health validates identity and stores only bounded non-secret metadata', async () => {
      const manager = new ServiceLifecycleManager({ serviceUrl: 'http://127.0.0.1:8765' });
      await manager.checkHealth({ getHealth: async () => ({ ...health(), prompt: 'sensitive', token: 'sensitive', provider: 'sk-sensitive' }) });
      assert.strictEqual(manager.lastHealthStatus, 'healthy');
      assert.ok(manager.lastHealthCheck);
      assert.strictEqual(manager.lastHealth.provider, 'unknown');
      assert.ok(!JSON.stringify(manager.lastHealth).includes('sensitive'));
      for (const raw of [{}, { status: 'ok' }, { ...health(), status: 'failed' }, { ...health(), service: 'other' }]) {
        await assert.rejects(manager.checkHealth({ getHealth: async () => raw }));
        assert.strictEqual(manager.lastHealth, null);
      }
    });

    await test('Managed startup verifies owned PID, uses direct spawn, and deduplicates concurrent starts', async () => {
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed' });
      const options = startup();
      const first = manager.startManagedService(options);
      const second = manager.startManagedService(options);
      assert.strictEqual(first, second);
      const result = await first;
      assert.strictEqual(result.started, true);
      assert.strictEqual(manager.owned, true);
      assert.strictEqual(manager.lastHealthStatus, 'healthy');
      const stopped = await manager.stopManagedService();
      assert.strictEqual(stopped.stopped, true);
      assert.deepStrictEqual(options.child.signals, ['SIGTERM']);
      assert.strictEqual(manager.owned, false);
      assert.strictEqual((await manager.stopManagedService()).stopped, false);
    });

    await test('Status view exposes lifecycle, ownership, health metadata, and active profile', async () => {
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed' });
      const profiles = new ProfileManager(null, { ci: {} });
      profiles.select('ci');
      const view = new ext.StatusTreeDataProvider(manager, profiles);
      const observed = [];
      manager.onChange = () => observed.push(manager.lastHealthStatus);
      await manager.startManagedService(startup());
      const labels = view.getChildren().map(item => item.label).join('\n');
      for (const value of ['Service Mode: managed', 'managed (PID: 1234)', 'Service URL: http://127.0.0.1:8765',
        'Service Health: healthy', 'Service Version: 0.1.0', 'Active Reviews: 0', 'Service Provider: mock',
        'Active Profile: ci', 'Last Health Check:', 'Managed CodeAtlas service is ready.']) assert.ok(labels.includes(value), value);
      assert.ok(observed.includes('starting'));
      manager.lastErrorMessage = 'Test startup error';
      assert.ok(view.getChildren().some(item => item.label.includes('Test startup error')));
      await manager.dispose();
    });

    await test('Existing external process is reused and never killed', async () => {
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed' });
      const result = await manager.startManagedService({ client: { getHealth: async () => health(9999) }, spawn: () => assert.fail('already occupied') });
      assert.strictEqual(result.started, false);
      assert.strictEqual(manager.owned, false);
      assert.strictEqual((await manager.stopManagedService()).stopped, false);
      const unowned = childProcess(9999);
      manager.managedProcess = unowned; // Even a stale handle is not authority to kill.
      await manager.dispose();
      assert.deepStrictEqual(unowned.signals, []);
    });

    await test('Startup timeout bounds hanging health calls and cleans up only the owned child', async () => {
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed' });
      const options = startup();
      let calls = 0;
      options.client.getHealth = () => ++calls === 1 ? Promise.reject(new Error('offline')) : new Promise(() => {});
      const start = Date.now();
      await assert.rejects(manager.startManagedService(options), /startup timed out/);
      assert.ok(Date.now() - start < 1500);
      assert.deepStrictEqual(options.child.signals, ['SIGTERM']);
      assert.strictEqual(manager.owned, false);
    });

    await test('Wrong-PID health response never marks a managed service ready', async () => {
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed' });
      const options = startup();
      let calls = 0;
      options.client.getHealth = async () => { if (++calls === 1) throw new Error(); return health(777); };
      await assert.rejects(manager.startManagedService(options), /timed out/);
      assert.strictEqual(manager.lastHealth, null);
      assert.deepStrictEqual(options.child.signals, ['SIGTERM']);
    });

    await test('Spawn errors and zero/nonzero/signal crashes report safe errors', async () => {
      for (const code of [0, 1, null]) {
        const crashes = [];
        const manager = new ServiceLifecycleManager({ serviceMode: 'managed', onCrash: message => crashes.push(message) });
        const options = startup();
        await manager.startManagedService(options);
        options.child.stderr.emit('data', 'API_KEY=super-secret raw prompt and provider output');
        options.child.emit('exit', code, code === null ? 'SIGTERM' : null);
        assert.strictEqual(manager.lastHealthStatus, 'crashed');
        assert.strictEqual(manager.owned, false);
        assert.strictEqual(crashes.length, 1);
        assert.ok(!manager.lastErrorMessage.includes('super-secret'));
        await assert.rejects(manager.checkHealth({ getHealth: async () => { throw new Error(); } }));
        assert.strictEqual(manager.lastHealthStatus, 'crashed');
      }
      const options = startup(childProcess(undefined));
      options.child.pid = undefined;
      options.client.getHealth = async () => { throw new Error(); };
      const spawn = options.spawn;
      options.spawn = (...args) => { const child = spawn(...args); queueMicrotask(() => child.emit('error', new Error('secret command'))); return child; };
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed' });
      await assert.rejects(manager.startManagedService(options), /exited unexpectedly/);
      assert.strictEqual(manager.owned, false);
    });

    await test('Captured process output is bounded and never retains secrets or prompts across chunks', async () => {
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed' });
      const options = startup();
      await manager.startManagedService(options);
      options.child.stdout.emit('data', 'CodeAtlas service listening on http://127.0.0.1:8765 (press Ctrl+C to stop)\n');
      assert.match(manager.stdoutBuffer, /CodeAtlas service listening/);
      for (let i = 0; i < 1000; i++) {
        options.child.stdout.emit('data', Buffer.from('sk-'));
        options.child.stdout.emit('data', Buffer.from('secret token provider_output raw_prompt'));
        options.child.stderr.emit('data', Buffer.from('CAT-APP-secret credentials'));
      }
      assert.ok(Buffer.byteLength(manager.stdoutBuffer) <= manager.maxLogBytes);
      assert.ok(Buffer.byteLength(manager.stderrBuffer) <= manager.maxLogBytes);
      assert.ok(!/secret|raw_prompt|CAT-APP/.test(manager.stdoutBuffer + manager.stderrBuffer));
      options.child.emit('error', new Error('kill failed'));
      assert.strictEqual(manager.owned, true, 'an error after spawn is not proof of process exit');
      await manager.dispose();
    });

    await test('Restart awaits child exit; late events cannot clear replacement ownership', async () => {
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed' });
      const first = startup();
      await manager.startManagedService(first);
      const second = startup(childProcess(4567));
      await manager.restartManagedService(second);
      first.child.emit('exit', 1);
      assert.strictEqual(manager.managedPid, 4567);
      assert.strictEqual(manager.owned, true);
      await manager.dispose();
      assert.deepStrictEqual(second.child.signals, ['SIGTERM']);
    });

    await test('Stop during startup cancels readiness and prevents resurrection', async () => {
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed' });
      const options = startup();
      let release;
      let calls = 0;
      options.client.getHealth = async () => { if (++calls === 1) throw new Error(); return new Promise(resolve => { release = resolve; }); };
      const pending = manager.startManagedService(options);
      const rejected = assert.rejects(pending, /cancelled|crashed/);
      await tick();
      await manager.stopManagedService();
      release(health());
      await rejected;
      assert.strictEqual(manager.owned, false);
      assert.notStrictEqual(manager.lastHealthStatus, 'healthy');
    });

    await test('Disabled mode has no discovery, spawn, HTTP request, or review', async () => {
      const manager = new ServiceLifecycleManager({ serviceMode: 'disabled', serviceUrl: 'invalid', autoStartService: true });
      assert.strictEqual(await manager.discover(), null);
      assert.strictEqual((await manager.checkHealth({ getHealth: () => assert.fail() })).status, 'disabled');
      await assert.rejects(manager.startManagedService({ spawn: () => assert.fail() }), /disabled/);
      const client = new ext.CodeAtlasClient(null);
      client.enabled = false;
      await assert.rejects(client.startReview(root), /disabled/);
    });

    await test('Managed command parsing rejects shell syntax, host overrides, and untrusted startup', async () => {
      assert.deepStrictEqual(parseServiceCommand('"/opt/Python Env/python" -m codeatlas.cli serve').args, ['-m', 'codeatlas.cli', 'serve']);
      for (const command of ['codeatlas serve; whoami', 'sh -c codeatlas', 'codeatlas serve --host 0.0.0.0', 'python -c evil', 'codeatlas serve | tee log', 'codeatlas serve $TOKEN']) {
        assert.throws(() => parseServiceCommand(command));
      }
      await assert.rejects(new ServiceLifecycleManager({ trusted: false }).startManagedService(), /trusted workspace/);
      await assert.rejects(new ServiceLifecycleManager({ serviceHost: '0.0.0.0' }).startManagedService(), /localhost/);
    });

    await test('User/workspace profiles load with workspace precedence and safe defaults', async () => {
      write('profiles.json', { shared: { timeout: 45, max_findings: 12 }, workspace: { enable_patch_suggestions: true } });
      const manager = new ProfileManager(root, { shared: { timeout: 20, enableTestExecution: true }, user: { model: 'test-model' } });
      const loaded = manager.loadProfiles();
      assert.strictEqual(loaded.shared.timeout, 45);
      assert.strictEqual(loaded.shared.maxFindings, 12);
      assert.strictEqual(loaded.shared.enableTestExecution, false, 'workspace replaces rather than merges a user profile');
      assert.strictEqual(loaded.shared.githubDryRun, true);
      assert.strictEqual(loaded.user.model, 'test-model');
      assert.deepStrictEqual(loaded.default, DEFAULT_PROFILE);
      manager.select('workspace');
      assert.strictEqual(manager.getActiveProfile().enablePatchSuggestions, true);
      manager.select('default');
      assert.strictEqual(manager.getActiveProfile().enablePatchSuggestions, false);
      assert.throws(() => manager.select('missing'), /not found/);
      fs.unlinkSync(path.join(root, '.codeatlas/profiles.json'));
    });

    await test('Profile schema rejects invalid types, bounds, aliases, unknown and secret-like fields', async () => {
      for (const value of [null, [], 'mock', { provider: 'other' }, { timeout: '30' }, { timeout: Infinity },
        { timeout: 0 }, { timeout: 301 }, { maxFindings: 201 }, { maxFindings: 1.5 }, { enableLiveReviewer: 'false' },
        { enableTestExecution: 1 }, { githubDryRun: null }, { commentMode: 'post' }, { model: { token: 'secret' } },
        { model: 'raw prompt with spaces' }, { model: 'ghp_secret' }, { model: 'sk-secret' }, { model: 'CAT-APP-secret' },
        { maxFindings: 5, max_findings: 5 }, { autoReview: true }, { serviceCommand: 'evil' }]) {
        assert.throws(() => validateProfile('test', value));
      }
      for (const key of ['api_key', 'approval_token', 'githubToken', 'credentials', 'password', 'raw_prompt', 'providerOutput']) {
        assert.throws(() => validateProfile('test', { [key]: 'sensitive' }), error => /secret-like field/.test(error.message) && !error.message.includes('sensitive'));
      }
      for (const key of ['autoApply', 'auto_apply', 'allow_apply', 'merge', 'autoMerge', 'allowMerge']) {
        assert.throws(() => validateProfile('test', { [key]: true }), /automatic patch application or merge/);
      }
      assert.throws(() => validateProfile('ghp_secret', {}));
      assert.throws(() => validateProfile('__proto__', {}));
    });

    await test('Configuration reads reject traversal, symlinks, malformed and oversized data', async () => {
      const manager = new ProfileManager(root);
      for (const filename of ['../secrets.json', '/etc/passwd', '.codeatlas/../source.json', 'src/source.json']) {
        assert.throws(() => manager.loadProfiles(filename));
      }
      const filename = path.join(root, '.codeatlas/profiles.json');
      fs.writeFileSync(filename, '{ "model": "super-secret"');
      assert.throws(() => manager.loadProfiles(), error => !error.message.includes('super-secret'));
      fs.writeFileSync(filename, 'x'.repeat(65537));
      assert.throws(() => manager.loadProfiles());
      fs.unlinkSync(filename);
      try {
        fs.symlinkSync(path.join(root, 'outside.json'), filename);
        assert.throws(() => manager.loadProfiles());
        fs.unlinkSync(filename);
      } catch (err) {
        if (err.code !== 'EPERM' && err.code !== 'ENOSYS') throw err;
      }
      for (const value of [null, [], 'bad', 10]) {
        write('profiles.json', value);
        assert.throws(() => manager.loadProfiles());
      }
      fs.unlinkSync(filename);
    });

    await test('Real Python localhost service startup, health identity, restart, and shutdown', async () => {
      const listener = net.createServer();
      await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
      const port = listener.address().port;
      await new Promise(resolve => listener.close(resolve));
      const python = process.env.CODEATLAS_TEST_PYTHON || 'python';
      const manager = new ServiceLifecycleManager({ serviceMode: 'managed', servicePort: port,
        serviceCommand: `"${python}" -m codeatlas.cli serve` });
      try {
        const result = await manager.startManagedService();
        assert.strictEqual(result.started, true);
        assert.strictEqual(result.health.pid, manager.managedPid);
        assert.strictEqual(result.health.active_reviews, 0);
        assert.strictEqual(result.health.provider, 'deterministic');
        const pid = manager.managedPid;
        await manager.restartManagedService();
        assert.notStrictEqual(manager.managedPid, pid);
        assert.strictEqual((await manager.checkHealth()).status, 'ok');
      } finally { await manager.dispose(); }
      assert.strictEqual(manager.owned, false);
    });

    await test('HTTP client bounds errors and never echoes credential-bearing responses', async () => {
      const server = http.createServer((req, res) => { res.writeHead(400); res.end(JSON.stringify({ message: 'CAT-APP-sensitive sk-sensitive' })); });
      await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
      const client = new ext.CodeAtlasClient(`http://127.0.0.1:${server.address().port}`);
      try { await assert.rejects(client.getHealth(), error => /HTTP 400/.test(error.message) && !/sensitive/.test(error.message)); }
      finally { client.dispose(); await new Promise(resolve => server.close(resolve)); }
    });

    await test('Profile command persists selection, honors custom path, and never auto-reviews', async () => {
      const original = { ...vscode.workspace };
      const originalWindow = { ...vscode.window };
      const methods = {};
      for (const key of ['getHealth', 'startReview', 'getReview', 'getFindings', 'getFindingDetail', 'validateProposal']) methods[key] = ext.CodeAtlasClient.prototype[key];
      const commands = new Map();
      const views = new Map();
      const messages = [];
      let selectionListener;
      let configurationListener;
      const settings = { serviceUrl: 'http://127.0.0.1:9012', healthCheckInterval: 0, activeProfile: 'default', profilePath: '.codeatlas/custom.json' };
      const updates = [];
      let reviews = 0;
      let reviewOptions;
      let validation;
      const finding = { id: 'sync-finding', severity: 'high', category: 'security', file: 'src/auth.py', line: 42, start_line: 42, end_line: 42, claim: 'test', status: 'open' };
      write('custom.json', { ci: { timeout: 7, maxFindings: 8, enablePatchSuggestions: true, enableTestExecution: true, enableFullSuiteExecution: true }, live: { provider: 'live' } });
      vscode.ConfigurationTarget = { Global: 1, Workspace: 2 };
      vscode.workspace.workspaceFolders = [{ uri: { fsPath: root } }];
      vscode.workspace.getConfiguration = () => ({ get: key => settings[key], inspect: key => ({ globalValue: key === 'profiles' ? {} : settings[key] }),
        update: async (key, value, target) => { settings[key] = value; updates.push([key, value, target]); } });
      vscode.workspace.onDidChangeConfiguration = listener => { configurationListener = listener; return { dispose() {} }; };
      vscode.window.registerTreeDataProvider = (id, view) => { views.set(id, view); return { dispose() {} }; };
      vscode.window.onDidChangeTextEditorSelection = listener => { selectionListener = listener; return { dispose() {} }; };
      vscode.window.showQuickPick = async items => items.find(item => item.profileName === 'ci');
      vscode.window.showErrorMessage = async message => { messages.push(message); };
      vscode.window.showInputBox = async () => 'CAT-APP-test-scoped';
      vscode.commands.registerCommand = (id, handler) => { commands.set(id, handler); return { dispose() {} }; };
      ext.CodeAtlasClient.prototype.getHealth = async () => health();
      ext.CodeAtlasClient.prototype.startReview = async (repo, options) => { reviews++; reviewOptions = options; return { run_id: 'r1', status: 'preparing' }; };
      ext.CodeAtlasClient.prototype.getReview = async () => ({ run_id: 'r1', status: 'completed' });
      ext.CodeAtlasClient.prototype.getFindings = async () => ({ findings: [finding] });
      ext.CodeAtlasClient.prototype.getFindingDetail = async () => ({ ...finding, context: { changed_lines: [42] } });
      ext.CodeAtlasClient.prototype.validateProposal = async (...args) => { validation = args; return { valid: false, errors: [] }; };
      let session;
      try {
        session = ext.activate({ subscriptions: [] });
        await session.ready;
        assert.strictEqual(reviews, 0);
        for (const name of ['startLocalService', 'stopLocalService', 'restartLocalService', 'selectConfigurationProfile', 'openConfiguration', 'checkServiceHealth']) {
          assert.ok(commands.has(`codeatlas.${name}`));
        }
        await commands.get('codeatlas.selectConfigurationProfile')();
        assert.deepStrictEqual(updates, [['activeProfile', 'ci', 2]]);
        assert.strictEqual(session.profileManager.getActiveProfile().timeout, 7);
        assert.strictEqual(reviews, 0);
        assert.ok(session.statusProvider.getChildren().some(item => item.label === 'Active Profile: ci'));
        await commands.get('codeatlas.checkServiceHealth')();
        assert.strictEqual(reviews, 0);
        await commands.get('codeatlas.startReview')();
        assert.strictEqual(reviews, 1);
        assert.strictEqual(reviewOptions.provider_timeout, 7);
        assert.strictEqual(reviewOptions.max_findings, 8);
        assert.strictEqual(reviewOptions.allow_patch_suggestions, true);
        await commands.get('codeatlas.validateApprovedFix')({ proposal_id: 'prop-test' });
        assert.deepStrictEqual(validation, ['prop-test', 'CAT-APP-test-scoped', true, true]);
        await new Promise(resolve => setTimeout(resolve, 1100));
        const editor = { document: { uri: { fsPath: `${root}/src/auth.py` }, lineCount: 100 }, selection: { active: { line: 41 } }, setDecorations() {} };
        vscode.window.activeTextEditor = editor;
        selectionListener({ textEditor: editor });
        await tick();
        assert.strictEqual(views.get('codeatlas.findingsView').activeFindingId, finding.id);
        assert.deepStrictEqual(views.get('codeatlas.contextView').context.changed_lines, [42]);
        editor.selection.active.line = 90;
        selectionListener({ textEditor: editor });
        await tick();
        assert.strictEqual(views.get('codeatlas.findingsView').activeFindingId, null);
        session.profileManager.select('live');
        await commands.get('codeatlas.startReview')();
        assert.strictEqual(reviews, 1, 'live provider needs a separate explicit enable flag');
        assert.ok(messages.some(message => /enableLiveReviewer=true/.test(message)));
        settings.serviceMode = 'disabled';
        configurationListener({ affectsConfiguration: key => ['codeatlas', 'codeatlas.serviceMode'].includes(key) });
        await tick();
        await commands.get('codeatlas.startReview')();
        assert.strictEqual(reviews, 1, 'disabled must block review even with a client mock');
        assert.strictEqual(session.client.enabled, false);
        assert.ok(messages.some(message => /disabled/.test(message)));
      } finally {
        await ext.deactivate();
        Object.assign(vscode.workspace, original);
        Object.assign(vscode.window, originalWindow);
        Object.assign(ext.CodeAtlasClient.prototype, methods);
        fs.unlinkSync(path.join(root, '.codeatlas/custom.json'));
      }
    });

    await test('Extension deactivation stops owned child and clears timers without starting a review', async () => {
      const original = vscode.workspace.getConfiguration;
      vscode.workspace.getConfiguration = () => ({ get: key => key === 'healthCheckInterval' ? 0 : undefined });
      const session = ext.activate({ subscriptions: [] });
      try {
        await session.ready;
        const options = startup();
        await session.serviceManager.startManagedService(options);
        await ext.deactivate();
        assert.deepStrictEqual(options.child.signals, ['SIGTERM']);
        assert.strictEqual(session.serviceManager.owned, false);
        assert.strictEqual(session.client.enabled, false);
        await assert.rejects(session.serviceManager.startManagedService(options), /shutting down/);
      } finally { await session.dispose(); vscode.workspace.getConfiguration = original; }
    });

    await test('Activation distinguishes the URL default from explicit settings and auto-starts without reviewing', async () => {
      const getConfiguration = vscode.workspace.getConfiguration;
      const start = ServiceLifecycleManager.prototype.startManagedService;
      const review = ext.CodeAtlasClient.prototype.startReview;
      const options = startup();
      const values = { serviceMode: 'managed', autoStartService: false, serviceUrl: 'http://127.0.0.1:8765', healthCheckInterval: 0 };
      vscode.workspace.getConfiguration = () => ({ get: key => values[key], inspect: key => ({ defaultValue: values[key] }) });
      let starts = 0;
      ServiceLifecycleManager.prototype.startManagedService = function () { starts++; return start.call(this, options); };
      ext.CodeAtlasClient.prototype.startReview = () => assert.fail('activation must not start reviews');
      try {
        const idle = ext.activate({ subscriptions: [] });
        await idle.ready;
        assert.strictEqual(idle.serviceManager.currentUrl, null);
        assert.strictEqual(starts, 0);
        await ext.deactivate();
        values.autoStartService = true;
        const managed = ext.activate({ subscriptions: [] });
        await managed.ready;
        assert.strictEqual(starts, 1);
        assert.strictEqual(managed.serviceManager.owned, true);
        await ext.deactivate();
        assert.deepStrictEqual(options.child.signals, ['SIGTERM']);
      } finally {
        await ext.deactivate();
        vscode.workspace.getConfiguration = getConfiguration;
        ServiceLifecycleManager.prototype.startManagedService = start;
        ext.CodeAtlasClient.prototype.startReview = review;
      }
    });
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
  console.log(`${count} Phase 10C behavioral tests passed (plus 16 synchronization regressions).`);
}

module.exports = { runTests };
if (require.main === module) {
  runTests().catch(err => {
    console.error('Test execution failed:', err);
    process.exitCode = 1;
  });
}
