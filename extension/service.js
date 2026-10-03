/**
 * @deprecated Legacy Phase 10C JavaScript implementation.
 * Retained temporarily for comparison.
 * The packaged extension loads the TypeScript-compiled output from ./out/service.js.
 */
const { spawn } = require('child_process');
const os = require('os');
const { readWorkspaceConfiguration } = require('./profiles');

const SETUP_MESSAGE = 'Configure codeatlas.serviceUrl or .codeatlas/service.json, or use CodeAtlas: Start Local Service. Managed auto-start requires serviceMode=managed and autoStartService=true.';

function validateServiceUrl(value) {
  // Exact authority syntax avoids URL parser normalization, credentials, paths, and queries.
  if (typeof value !== 'string' || !/^http:\/\/(127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?\/?$/.test(value)) {
    throw new Error('Invalid service URL: use an HTTP localhost URL without credentials, path, query, or fragment.');
  }
  const parsed = new URL(value);
  if (parsed.port && (Number(parsed.port) < 1 || Number(parsed.port) > 65535)) throw new Error('Invalid service URL port.');
  return parsed.origin;
}

function managedUrl(host, port) {
  if (!['127.0.0.1', 'localhost'].includes(host)) throw new Error('Managed service must bind to localhost (127.0.0.1).');
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Service port must be an integer from 1 to 65535.');
  // Bind a numeric address rather than allowing name resolution to choose an interface.
  return validateServiceUrl(`http://127.0.0.1:${port}`);
}

function parseServiceCommand(command) {
  if (typeof command !== 'string' || command.length > 1024 || /[\r\n\0;&|<>`$]/.test(command)) {
    throw new Error('Invalid serviceCommand. Use codeatlas serve or python -m codeatlas.cli serve; shell syntax is not supported.');
  }
  const parts = command.match(/"[^"\r\n]+"|'[^'\r\n]+'|[^\s"']+/g) || [];
  if (parts.join(' ') !== command.trim().replace(/\s+/g, ' ')) throw new Error('Invalid serviceCommand quoting.');
  const args = parts.map(part => /^['"]/.test(part) ? part.slice(1, -1) : part);
  const executable = args.shift();
  const base = (executable || '').replace(/\\/g, '/').split('/').pop();
  const direct = /^codeatlas(?:\.exe)?$/i.test(base) && args.join(' ') === 'serve';
  const python = /^python(?:[0-9.]+)?(?:\.exe)?$/i.test(base) && args.join(' ') === '-m codeatlas.cli serve';
  if (!direct && !python) throw new Error('serviceCommand must launch codeatlas serve or python -m codeatlas.cli serve. Quote executable paths containing spaces.');
  return { executable, args };
}

function safeHealth(raw) {
  if (!raw || raw.status !== 'ok' || raw.service !== 'codeatlas-service' ||
      typeof raw.version !== 'string' || !/^\d{1,4}\.\d{1,4}\.\d{1,4}$/.test(raw.version)) {
    throw new Error('Endpoint did not return a valid CodeAtlas /health response.');
  }
  return {
    status: 'ok', service: 'codeatlas-service', version: raw.version,
    pid: Number.isSafeInteger(raw.pid) && raw.pid > 0 ? raw.pid : null,
    active_reviews: Number.isSafeInteger(raw.active_reviews) && raw.active_reviews >= 0 ? raw.active_reviews : null,
    provider: ['mock', 'live', 'deterministic', 'mixed'].includes(raw.provider) ? raw.provider : 'unknown',
  };
}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function bounded(promise, ms) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Service health check timed out.')), Math.max(1, ms));
    })]);
  } finally { clearTimeout(timer); }
}

class ServiceLifecycleManager {
  constructor(options = {}) {
    this.serviceMode = options.serviceMode ?? 'external';
    this.serviceHost = options.serviceHost ?? '127.0.0.1';
    this.servicePort = options.servicePort ?? 8765;
    this.serviceCommand = options.serviceCommand ?? 'codeatlas serve';
    this.explicitServiceUrl = options.serviceUrl ?? null;
    this.autoStartService = options.autoStartService === true;
    this.workspaceRoot = options.workspaceRoot ?? null;
    this.trusted = options.trusted !== false;
    this.clientFactory = options.clientFactory || (url => new (require('./extension').CodeAtlasClient)(url));
    this.onChange = options.onChange || (() => {});
    this.onCrash = options.onCrash || (() => {});
    this.managedProcess = null;
    this.managedPid = null;
    this.owned = false;
    this.currentUrl = null;
    this.lastHealth = null;
    this.lastHealthStatus = 'unknown';
    this.lastHealthCheck = null;
    this.lastErrorMessage = null;
    this.message = SETUP_MESSAGE;
    this.stdoutBuffer = '';
    this.stderrBuffer = '';
    this.maxLogBytes = 8192;
    this._epoch = 0;
    this._disposed = false;
    this._starting = null;
    this._stopping = null;
  }

  resolveServiceUrl() {
    if (!['external', 'managed', 'disabled'].includes(this.serviceMode)) throw new Error('Invalid service mode.');
    if (this.serviceMode === 'disabled') return null;
    if (this.explicitServiceUrl !== null && this.explicitServiceUrl !== '') return validateServiceUrl(this.explicitServiceUrl);
    const data = readWorkspaceConfiguration(this.workspaceRoot, '.codeatlas/service.json');
    if (data !== undefined) {
      if (!data || typeof data !== 'object' || Array.isArray(data) ||
          Object.keys(data).some(key => !['url', 'host', 'port'].includes(key)) ||
          (Object.hasOwn(data, 'url') && (Object.hasOwn(data, 'host') || Object.hasOwn(data, 'port')))) {
        throw new Error('Invalid workspace service configuration. Use only url, or host and port.');
      }
      if (Object.hasOwn(data, 'url')) return validateServiceUrl(data.url);
      return managedUrl(data.host ?? '127.0.0.1', data.port);
    }
    return this.currentUrl;
  }

  async discover() {
    if (this._disposed) return null;
    if (this.serviceMode === 'disabled') {
      this.currentUrl = null;
      this.lastHealth = null;
      this.lastHealthStatus = 'disabled';
      this.message = 'CodeAtlas service is disabled. Enable a service mode in configuration.';
      this.onChange();
      return null;
    }
    try {
      this.currentUrl = this.resolveServiceUrl();
      if (this.currentUrl) await this.checkHealth();
      else if (this.serviceMode === 'managed' && this.autoStartService) await this.startManagedService();
      else {
        this.lastHealthStatus = 'not_configured';
        this.message = SETUP_MESSAGE;
        this.onChange();
      }
      return this.currentUrl;
    } catch (error) {
      this.lastErrorMessage = error.message;
      if (this.lastHealthStatus !== 'crashed') this.lastHealthStatus = 'error';
      this.onChange();
      throw error;
    }
  }

  async checkHealth(client = null) {
    if (this.serviceMode === 'disabled' || this._disposed) {
      this.lastHealthStatus = 'disabled';
      this.lastHealth = null;
      this.onChange();
      return { status: 'disabled' };
    }
    const epoch = this._epoch;
    let target;
    try {
      target = this.resolveServiceUrl();
      if (!target) throw new Error(SETUP_MESSAGE);
    } catch (error) {
      this.currentUrl = null;
      this.lastHealth = null;
      this.lastHealthStatus = 'error';
      this.lastHealthCheck = new Date().toISOString();
      this.lastErrorMessage = error.message;
      this.onChange();
      throw error;
    }
    if (target !== this.currentUrl) {
      this.currentUrl = target;
      this.lastHealth = null;
      this.onChange();
    }
    try {
      const health = safeHealth(await bounded((client || this.clientFactory(target)).getHealth(1000), 1000));
      if (this._disposed || epoch !== this._epoch) return { status: 'stale' };
      if (this.owned && health.pid !== this.managedPid) throw new Error('Service identity changed.');
      this.currentUrl = target;
      this.lastHealth = health;
      this.lastHealthStatus = 'healthy';
      this.lastHealthCheck = new Date().toISOString();
      this.lastErrorMessage = null;
      this.message = 'CodeAtlas service is ready.';
      this.onChange();
      return health;
    } catch (_) {
      if (epoch !== this._epoch || this._disposed) return { status: 'stale' };
      this.lastHealth = null;
      this.lastHealthCheck = new Date().toISOString();
      // Preserve the crash diagnosis across subsequent unsuccessful probes.
      if (this.lastHealthStatus !== 'crashed') {
        this.lastHealthStatus = 'unreachable';
        this.lastErrorMessage = 'CodeAtlas /health failed. Check that the configured local service is running.';
      }
      this.onChange();
      throw new Error(this.lastErrorMessage);
    }
  }

  startManagedService(options = {}) {
    if (this._starting) return this._starting;
    this._starting = this._start(options).catch(error => {
      if (!this._disposed && !['stopped', 'disabled', 'crashed'].includes(this.lastHealthStatus)) {
        this.lastErrorMessage = error.message;
        this.lastHealthStatus = 'error';
        this.onChange();
      }
      throw error;
    }).finally(() => { this._starting = null; });
    return this._starting;
  }

  async _start(options) {
    const epoch = this._epoch;
    if (this._disposed) throw new Error('CodeAtlas extension is shutting down.');
    if (this.serviceMode === 'disabled') throw new Error('CodeAtlas service mode is disabled in settings.');
    if (!this.trusted) throw new Error('Managed service startup requires a trusted workspace.');
    if (this._stopping) await this._stopping;
    if (epoch !== this._epoch || this._disposed) throw new Error('Service startup cancelled.');
    if (this.serviceMode === 'disabled') throw new Error('CodeAtlas service mode is disabled in settings.');
    if (this.owned && this.managedProcess) {
      const health = await this.checkHealth();
      return { started: false, alreadyRunning: true, pid: this.managedPid, url: this.currentUrl, health };
    }
    const target = managedUrl(this.serviceHost, this.servicePort);
    const resolved = this.resolveServiceUrl();
    if (resolved && resolved !== target && resolved !== target.replace('127.0.0.1', 'localhost')) {
      throw new Error('Discovered service URL differs from the managed host/port. Align the settings before starting a local service.');
    }
    const { executable, args } = parseServiceCommand(this.serviceCommand);
    const client = options.client || this.clientFactory(target);
    // Reuse a pre-existing CodeAtlas service; it remains unowned and is never stopped.
    try {
      const health = safeHealth(await bounded(client.getHealth(500), 500));
      if (epoch !== this._epoch || this._disposed) throw new Error();
      this.currentUrl = target;
      this.lastHealth = health;
      this.lastHealthStatus = 'healthy';
      this.lastHealthCheck = new Date().toISOString();
      this.message = 'Using existing local CodeAtlas service (external / unowned).';
      this.lastErrorMessage = null;
      this.onChange();
      return { started: false, alreadyRunning: true, url: target, health };
    } catch (_) { /* Only a matching owned child may become ready below. */ }
    if (epoch !== this._epoch || this._disposed) throw new Error('Service startup cancelled.');
    const timeoutMs = options.timeoutMs ?? 10000;
    const deadline = Date.now() + timeoutMs;
    this.currentUrl = target;
    this.stdoutBuffer = this.stderrBuffer = '';
    this.lastHealth = null;
    this.lastErrorMessage = null;
    this.lastHealthStatus = 'starting';
    this.message = 'Starting local CodeAtlas service; waiting for /health.';
    this.onChange();
    let child;
    try {
      child = (options.spawn || spawn)(executable, [...args, '--host', '127.0.0.1', '--port', String(this.servicePort)], {
        shell: false, cwd: os.homedir(), windowsHide: true,
        env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONSAFEPATH: '1' },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (_) {
      this.lastHealthStatus = 'error';
      this.lastErrorMessage = 'Could not launch CodeAtlas. Check serviceCommand and the installed Python environment.';
      this.onChange();
      throw new Error(this.lastErrorMessage);
    }
    this.managedProcess = child;
    this.managedPid = child.pid;
    this.owned = true;
    for (const stream of ['stdout', 'stderr']) {
      child[stream]?.on('data', chunk => {
        // Unknown process output may contain prompts or secrets, even across chunks.
        // Retain only exact, known-safe lifecycle banners and capture metadata.
        const text = chunk.toString();
        const safeBanner = /^(?:CodeAtlas service listening on http:\/\/127\.0\.0\.1:[0-9]{1,5} \(press Ctrl\+C to stop\)|\n?Shutting down CodeAtlas service\.\.\.)\r?\n?$/.test(text);
        const field = `${stream}Buffer`;
        this[field] = (this[field] + (safeBanner ? text : `[${stream}: ${Buffer.byteLength(chunk)} bytes suppressed]\n`)).slice(-this.maxLogBytes);
      });
    }
    let exited = false;
    let intentional = false;
    let finishExit;
    const exitPromise = new Promise(resolve => { finishExit = resolve; });
    child._codeatlasExit = exitPromise;
    child._codeatlasStop = () => { intentional = true; };
    const onExit = code => {
      if (exited) return;
      exited = true;
      finishExit();
      if (this.managedProcess !== child) return;
      this.managedProcess = null;
      this.managedPid = null;
      this.owned = false;
      this.lastHealth = null;
      if (!intentional) {
        this._epoch++;
        this.lastHealthStatus = 'crashed';
        this.lastErrorMessage = `CodeAtlas service exited unexpectedly${Number.isInteger(code) ? ` (code ${code})` : ''}. Use Start Local Service to retry.`;
        this.onCrash(this.lastErrorMessage);
      }
      this.onChange();
    };
    child.once('exit', onExit);
    child.on('error', () => {
      if (!child.pid) onExit(null); // Spawn failure: no process was created.
      else if (this.managedProcess === child) {
        // A failed kill can also emit 'error'; retain ownership until actual exit.
        this.lastErrorMessage = 'Owned CodeAtlas process reported an operating-system error.';
        this.onChange();
      }
    });
    while (Date.now() < deadline && !exited && epoch === this._epoch && !this._disposed) {
      try {
        this.lastHealthCheck = new Date().toISOString();
        const remaining = Math.max(1, deadline - Date.now());
        const health = safeHealth(await bounded(client.getHealth(Math.min(500, remaining)), remaining));
        if (health.pid === child.pid && !exited && epoch === this._epoch && !this._disposed && Date.now() < deadline) {
          this.lastHealth = health;
          this.lastHealthStatus = 'healthy';
          this.lastHealthCheck = new Date().toISOString();
          this.message = 'Managed CodeAtlas service is ready.';
          this.onChange();
          return { started: true, pid: child.pid, url: target, health };
        }
      } catch (_) { /* Retry until the absolute startup deadline. */ }
      await delay(Math.min(100, Math.max(0, deadline - Date.now())));
    }
    if (exited) throw new Error(this.lastErrorMessage || 'Service crashed during startup.');
    if (epoch !== this._epoch || this._disposed) throw new Error('Service startup cancelled.');
    await this.stopManagedService();
    this.lastHealthStatus = 'error';
    this.lastErrorMessage = 'Managed service startup timed out waiting for a matching CodeAtlas /health response.';
    this.onChange();
    throw new Error(this.lastErrorMessage);
  }

  async stopManagedService() {
    this._epoch++;
    if (this._stopping) return this._stopping;
    const child = this.managedProcess;
    if (!this.owned || !child) return { stopped: false, message: 'No owned service process is running.' };
    this._stopping = (async () => {
      const pid = this.managedPid;
      child._codeatlasStop();
      this.lastHealthStatus = 'stopping';
      this.onChange();
      try {
        child.kill('SIGTERM');
        try { await bounded(child._codeatlasExit, 1500); }
        catch (_) {
          if (this.managedProcess === child) child.kill('SIGKILL');
          await bounded(child._codeatlasExit, 500);
        }
      } catch (_) {
        this.lastErrorMessage = 'Owned service did not exit within the shutdown deadline.';
        this.onChange();
        throw new Error(this.lastErrorMessage);
      }
      this.lastHealth = null;
      this.lastHealthStatus = 'stopped';
      this.lastErrorMessage = null;
      this.message = 'Managed CodeAtlas service stopped.';
      this.onChange();
      return { stopped: true, pid };
    })().finally(() => { this._stopping = null; });
    return this._stopping;
  }

  async restartManagedService(options = {}) {
    await this.stopManagedService();
    await this.waitForStartup();
    return this.startManagedService(options);
  }

  async waitForStartup() {
    if (this._starting) await this._starting.catch(() => {});
  }

  async dispose() {
    this._disposed = true;
    await this.stopManagedService();
    await this.waitForStartup();
  }
}

module.exports = { ServiceLifecycleManager, validateServiceUrl, parseServiceCommand, safeHealth, SETUP_MESSAGE };
