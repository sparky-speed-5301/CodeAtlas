/**
 * test_smoke.js - Capability-checking launcher for real VS Code extension-host smoke tests.
 * Phase 10F.
 */
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const { runTests } = require('@vscode/test-electron');

const EXTENSION_ROOT = path.resolve(__dirname);
const SMOKE_RUNNER_PATH = path.join(EXTENSION_ROOT, 'test', 'smoke_runner.js');

/**
 * Locate full VS Code binary on the host machine.
 */
function findVsCodeExecutable() {
  if (process.env.VSCODE_PATH && fs.existsSync(process.env.VSCODE_PATH)) {
    return process.env.VSCODE_PATH;
  }

  // Windows candidate locations
  if (process.platform === 'win32') {
    const candidates = [
      path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Microsoft VS Code', 'Code.exe'),
      'C:\\Program Files\\Microsoft VS Code\\Code.exe',
      'C:\\Program Files (x86)\\Microsoft VS Code\\Code.exe',
    ];

    try {
      const whereOutput = execSync('where.exe code', { stdio: ['ignore', 'pipe', 'ignore'], encoding: 'utf8' }).trim();
      if (whereOutput) {
        const firstLine = whereOutput.split(/\r?\n/)[0].trim();
        const candidateNearCodeCmd = path.resolve(path.dirname(firstLine), '..', 'Code.exe');
        if (fs.existsSync(candidateNearCodeCmd)) {
          candidates.unshift(candidateNearCodeCmd);
        }
      }
    } catch (_) {}

    for (const c of candidates) {
      if (c && fs.existsSync(c)) {
        return c;
      }
    }
  } else if (process.platform === 'darwin') {
    const candidates = [
      '/Applications/Visual Studio Code.app/Contents/MacOS/Electron',
      path.join(process.env.HOME || '', 'Applications/Visual Studio Code.app/Contents/MacOS/Electron'),
    ];
    for (const c of candidates) {
      if (fs.existsSync(c)) return c;
    }
  } else {
    // Linux
    const candidates = ['/usr/bin/code', '/usr/share/code/code', '/snap/bin/code'];
    try {
      const whichOutput = execSync('which code', { stdio: ['ignore', 'pipe', 'ignore'], encoding: 'utf8' }).trim();
      if (whichOutput && fs.existsSync(whichOutput)) {
        candidates.unshift(whichOutput);
      }
    } catch (_) {}

    for (const c of candidates) {
      if (fs.existsSync(c)) return c;
    }
  }

  return null;
}

async function main() {
  console.log('=== CodeAtlas Phase 10F Real Extension-Host Smoke Validation ===\n');

  // Capability check
  const vscodePath = findVsCodeExecutable();
  if (!vscodePath) {
    console.log('[Smoke] SKIPPED: Full VS Code binary is unavailable in this environment.');
    console.log('[Smoke] Reason: Neither VSCODE_PATH nor a system VS Code executable was found.');
    console.log('[Smoke] Headless workflow tests remain authoritative in this environment.');
    return;
  }

  console.log(`[Smoke] Capability check passed: Found VS Code executable at:\n  ${vscodePath}\n`);
  console.log('[Smoke] Launching extension inside real VS Code host...');

  try {
    await runTests({
      vscodeExecutablePath: vscodePath,
      extensionDevelopmentPath: EXTENSION_ROOT,
      extensionTestsPath: SMOKE_RUNNER_PATH,
      launchArgs: [
        '--disable-extensions',
        '--disable-gpu',
        '--disable-updates',
        '--no-sandbox',
      ],
    });
    console.log('\n✓ [Smoke] Real VS Code extension-host smoke test PASSED successfully.');
  } catch (err) {
    // Check if error is due to headless/display environment capability (e.g. Linux without X11)
    const errMsg = err.message || '';
    if (
      errMsg.includes('cannot open display') ||
      errMsg.includes('No DISPLAY') ||
      errMsg.includes('x11') ||
      errMsg.includes('EACCES')
    ) {
      console.log(`\n[Smoke] SKIPPED: Real extension-host could not initialize display/GUI in this environment.`);
      console.log(`[Smoke] Reason: ${errMsg}`);
      return;
    }

    console.error(`\n[Smoke] FAILED: Real extension-host smoke test encountered an error: ${errMsg}`);
    throw err;
  }
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}

module.exports = {
  findVsCodeExecutable,
  main,
};
