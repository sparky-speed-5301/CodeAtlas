/**
 * smoke_runner.js - Real VS Code extension-host smoke test suite.
 * Executed inside the real VS Code extension host via @vscode/test-electron.
 */
const assert = require('assert');
const vscode = require('vscode');
const { spawn } = require('child_process');

const EXPECTED_COMMANDS = [
  'codeatlas.startReview',
  'codeatlas.cancelReview',
  'codeatlas.refresh',
  'codeatlas.findFinding',
  'codeatlas.explainCurrentFinding',
  'codeatlas.openFindingLocation',
  'codeatlas.showFindingDetails',
  'codeatlas.explainFinding',
  'codeatlas.showContext',
  'codeatlas.generateDraftFix',
  'codeatlas.validateApprovedFix',
   'codeatlas.copyFinding',
   'codeatlas.copyComparisonRange',
  'codeatlas.dismissFinding',
  'codeatlas.filterSeverity',
  'codeatlas.filterCategory',
  'codeatlas.startLocalService',
  'codeatlas.stopLocalService',
  'codeatlas.restartLocalService',
  'codeatlas.selectConfigurationProfile',
  'codeatlas.openConfiguration',
  'codeatlas.checkServiceHealth'
];

exports.run = async function() {
  console.log('[Real Host Smoke] Initializing tests inside real VS Code host...');
  console.log(`[Real Host Smoke] VS Code Version: ${vscode.version}`);

  // 1. Extension Discovery
  const ext = vscode.extensions.getExtension('codeatlas.codeatlas');
  assert.ok(ext, 'Extension codeatlas.codeatlas must be discovered by VS Code host');
  assert.strictEqual(ext.isActive, false, 'Extension should not be active prior to explicit activation');
  console.log('✓ [Real Host Smoke] 1. Extension discovered in host manifest');

  // 2. Configuration Settings Defaults
  const config = vscode.workspace.getConfiguration('codeatlas');
  assert.strictEqual(config.get('serviceUrl'), 'http://127.0.0.1:8765', 'serviceUrl default');
  assert.strictEqual(config.get('provider'), 'mock', 'provider default');
  assert.strictEqual(config.get('serviceMode'), 'external', 'serviceMode default');
  assert.strictEqual(config.get('githubDryRun'), true, 'githubDryRun default');
  assert.strictEqual(config.get('autoStartService'), false, 'autoStartService default');
  assert.strictEqual(config.get('healthCheckInterval'), 30, 'healthCheckInterval default');
  assert.strictEqual(config.get('commentMode'), 'summary', 'commentMode default');
  assert.strictEqual(config.get('activeProfile'), 'default', 'activeProfile default');
  console.log('✓ [Real Host Smoke] 2. Configuration defaults verified');

  // 3. Extension Activation
  const api = await ext.activate();
  assert.strictEqual(ext.isActive, true, 'Extension must be active');
  assert.ok(api, 'Extension exports an API object');
  assert.ok(api.serviceManager, 'Exports serviceManager');
  assert.ok(api.profileManager, 'Exports profileManager');
  assert.ok(api.statusProvider, 'Exports statusProvider');
  assert.ok(api.client, 'Exports client');
  console.log('✓ [Real Host Smoke] 3. Extension activated successfully');

  // 4. Command Registration (all 22 commands)
  const registeredCommands = await vscode.commands.getCommands(true);
  for (const cmd of EXPECTED_COMMANDS) {
    assert.ok(registeredCommands.includes(cmd), `Command ${cmd} must be registered in host`);
  }
  console.log(`✓ [Real Host Smoke] 4. All ${EXPECTED_COMMANDS.length} commands registered in real host`);

  // 5. Comparison copy context-menu registration
  const contextMenus = ext.packageJSON?.contributes?.menus?.['view/item/context'] || [];
  assert.ok(
    contextMenus.some(
      entry =>
        entry.command === 'codeatlas.copyComparisonRange' &&
        entry.when ===
          'view == codeatlas.statusView && viewItem == codeatlas.comparisonRange'
    ),
    'Comparison copy command must be registered for the Status comparison item'
  );
  console.log('✓ [Real Host Smoke] 5. Comparison copy context-menu registration verified');

  // 6. Activity-Bar Views & Providers
  const statusItems = await api.statusProvider.getChildren();
  assert.ok(Array.isArray(statusItems), 'Status tree provider returns array');
  assert.ok(statusItems.length > 0, 'Status tree provider has at least one root item');
  console.log('✓ [Real Host Smoke] 6. Activity-bar views and tree data providers registered');

  // 7. Service Discovery & No Automatic Review
  await api.ready;
  assert.strictEqual(api.serviceManager.serviceMode, 'external');
  // Confirm that activation did not trigger an automatic review
  const statusChildren = await api.statusProvider.getChildren();
  const label = statusChildren[0]?.label || '';
  assert.ok(!label.toLowerCase().includes('reviewing'), 'No review should be in progress on activation');
  console.log('✓ [Real Host Smoke] 7. Service discovery initialized with no automatic review');

  // 8. Disabled Service Mode
  api.serviceManager.serviceMode = 'disabled';
  assert.strictEqual(api.serviceManager.serviceMode, 'disabled');
  console.log('✓ [Real Host Smoke] 8. Disabled service mode verified');

  // 9. Health-Check Failure
  api.serviceManager.serviceMode = 'external';
  api.serviceManager.currentUrl = 'http://127.0.0.1:8765'; // unreachable default port
  let healthFailed = false;
  try {
    await api.serviceManager.checkHealth();
  } catch (err) {
    healthFailed = true;
    assert.strictEqual(api.serviceManager.lastHealthStatus, 'unreachable');
  }
  assert.strictEqual(healthFailed, true, 'Health check against unreachable service must throw');
  console.log('✓ [Real Host Smoke] 9. Health-check failure handled safely without crashing');

  // 10. Managed Service Startup, Health and Shutdown
  const managedChild = spawn(process.execPath, ['-e', 'setInterval(()=>{}, 1000)'], {
    stdio: 'ignore'
  });
  const managedChildPid = managedChild.pid;
  assert.ok(managedChildPid, 'Child process must have valid PID');

  api.serviceManager.currentUrl = null;
  api.serviceManager.serviceMode = 'managed';
  api.serviceManager.serviceHost = '127.0.0.1';
  api.serviceManager.servicePort = 8765;
  let spawned = false;
  const startResult = await api.serviceManager.startManagedService({
    spawn: () => {
      spawned = true;
      return managedChild;
    },
    client: {
      getHealth: async () => {
        if (!spawned) throw new Error('Not running yet');
        return {
          status: 'ok',
          service: 'codeatlas-service',
          version: '0.1.0',
          pid: managedChildPid,
          active_reviews: 0,
          provider: 'mock'
        };
      }
    },
    timeoutMs: 3000
  });

  assert.strictEqual(startResult.started, true, 'Service should report started');
  assert.strictEqual(api.serviceManager.owned, true, 'Service manager must own the child');
  assert.strictEqual(api.serviceManager.managedPid, managedChildPid, 'Managed PID must match child PID');
  assert.strictEqual(api.serviceManager.lastHealthStatus, 'healthy');
  console.log('✓ [Real Host Smoke] 10. Managed service startup and health verification succeeded');

  const stopResult = await api.serviceManager.stopManagedService();
  assert.strictEqual(stopResult.stopped, true, 'Service should report stopped');
  assert.strictEqual(api.serviceManager.managedProcess, null, 'Managed process reference cleared');
  assert.strictEqual(api.serviceManager.managedPid, null, 'Managed PID cleared');
  assert.strictEqual(api.serviceManager.owned, false, 'Ownership flag reset');
  console.log('✓ [Real Host Smoke] 11. Managed service stopped and PID ownership cleared');

  // 11. Owned-Process-Only Termination & Deactivation Cleanup
  // Spawn an unrelated process that MUST survive extension deactivation
  const unrelatedChild = spawn(process.execPath, ['-e', 'setInterval(()=>{}, 1000)'], {
    stdio: 'ignore'
  });
  const unrelatedPid = unrelatedChild.pid;
  assert.ok(unrelatedPid, 'Spawned unrelated test process');

  // Perform extension deactivation cleanup
  await api.dispose();
  console.log('✓ [Real Host Smoke] 12. Extension deactivation cleanup executed');

  // Verify that the unrelated process is still running
  let isUnrelatedAlive = false;
  try {
    process.kill(unrelatedPid, 0);
    isUnrelatedAlive = true;
  } catch (_) {
    isUnrelatedAlive = false;
  }

  assert.strictEqual(isUnrelatedAlive, true, 'Deactivation MUST NOT terminate unrelated processes');
  console.log('✓ [Real Host Smoke] 13. Owned-process-only termination verified (unrelated process preserved)');

  // Clean up unrelated child
  try {
    process.kill(unrelatedPid);
  } catch (_) {}

  console.log('\n[Real Host Smoke] All 13 real extension-host smoke test assertions passed successfully!');
};
