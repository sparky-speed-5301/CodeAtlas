import * as vscode from 'vscode';
import { ProfileManager } from '../profiles';
import { ServiceLifecycleManager } from '../service';
import { ReviewStatus } from '../types';

export class StatusTreeDataProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private _onDidChangeTreeData: vscode.EventEmitter<vscode.TreeItem | undefined | null | void>;
  readonly onDidChangeTreeData: vscode.Event<vscode.TreeItem | undefined | null | void>;
  status: ReviewStatus | null;
  serviceManager: ServiceLifecycleManager | null;
  profileManager: ProfileManager | null;

  constructor(
    serviceManager: ServiceLifecycleManager | null = null,
    profileManager: ProfileManager | null = null
  ) {
    this._onDidChangeTreeData = new vscode.EventEmitter<vscode.TreeItem | undefined | null | void>();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this.status = null;
    this.serviceManager = serviceManager;
    this.profileManager = profileManager;
  }

  refresh(status?: ReviewStatus | null): void {
    if (status !== undefined) {
      this.status = status;
    }
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: vscode.TreeItem): vscode.TreeItem[] {
    if (element) return [];

    const items: vscode.TreeItem[] = [];

    // Service & Discovery Status
    const configuredMode = this.serviceManager?.serviceMode ?? 'external';
    const mode = ['external', 'managed', 'disabled'].includes(configuredMode)
      ? configuredMode
      : 'invalid';
    const sUrl = this.serviceManager ? this.serviceManager.currentUrl : null;
    const ownership =
      this.serviceManager && this.serviceManager.owned
        ? `managed (PID: ${this.serviceManager.managedPid})`
        : 'external / unowned';
    const healthStatus = this.serviceManager ? this.serviceManager.lastHealthStatus : 'unknown';
    const activeProf = this.profileManager ? this.profileManager.activeProfileName : 'default';

    items.push(new vscode.TreeItem(`Service Mode: ${mode} (${ownership})`));
    items.push(new vscode.TreeItem(`Service URL: ${sUrl || 'none'}`));
    items.push(new vscode.TreeItem(`Service Health: ${healthStatus}`));
    items.push(new vscode.TreeItem(`Active Profile: ${activeProf}`));
    const h = this.serviceManager?.lastHealth;
    items.push(new vscode.TreeItem(`Service Version: ${h?.version ?? 'unknown'}`));
    items.push(new vscode.TreeItem(`Active Reviews: ${h?.active_reviews ?? 'unknown'}`));
    items.push(new vscode.TreeItem(`Service Provider: ${h?.provider ?? 'unknown'}`));
    items.push(
      new vscode.TreeItem(
        `Last Health Check: ${this.serviceManager?.lastHealthCheck ?? 'not checked'}`
      )
    );
    if (this.serviceManager?.message) items.push(new vscode.TreeItem(this.serviceManager.message));
    if (this.profileManager?.error) {
      items.push(new vscode.TreeItem(`Profile Error: ${this.profileManager.error}`));
    }

    if (this.serviceManager && this.serviceManager.lastErrorMessage) {
      items.push(new vscode.TreeItem(`Service Error: ${this.serviceManager.lastErrorMessage}`));
    }

    // Review Run Status
    if (!this.status) {
      items.push(new vscode.TreeItem('--- No active review run ---'));
      return items;
    }

    items.push(new vscode.TreeItem('--- Active Review ---'));
    const itemStatus = new vscode.TreeItem(`Status: ${this.status.status}`);
    itemStatus.description = this.status.progress_text;
    items.push(itemStatus);

    if (this.status.run_id) {
      items.push(new vscode.TreeItem(`Run ID: ${this.status.run_id}`));
    }
    if (this.status.policy_decision) {
      items.push(new vscode.TreeItem(`Policy Decision: ${this.status.policy_decision}`));
    }
    if (this.status.finding_counts) {
      const fc = this.status.finding_counts;
      items.push(
        new vscode.TreeItem(
          `Findings: ${fc.total || 0} (Blocker: ${fc.blocker || 0}, High: ${fc.high || 0}, Med: ${
            fc.medium || 0
          }, Low: ${fc.low || 0})`
        )
      );
    }
    items.push(new vscode.TreeItem(`Tests Status: ${this.status.test_status || 'not_run'}`));
    items.push(
      new vscode.TreeItem(
        `Patch Validation: ${this.status.patch_validation_status || 'not_run'}`
      )
    );

    return items;
  }
}
