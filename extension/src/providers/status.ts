import * as vscode from 'vscode';
import { ProfileManager } from '../profiles';
import { ServiceLifecycleManager } from '../service';
import { ReviewStatus } from '../types';

// Only genuine git revision identifiers are displayed; anything else the
// service sends in these fields renders as unresolved rather than trusted.
const SHA_PATTERN = /^[0-9a-f]{7,64}$/i;
const FULL_SHA_PATTERN = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i;
const SHA_DISPLAY_CHARS = 12;
const COMPARISON_CONTEXT_VALUE = 'codeatlas.comparisonRange';

export interface ResolvedShas {
  base: string;
  head: string;
}

function resolvedShasFrom(status: ReviewStatus | null): ResolvedShas | null {
  if (status?.status === 'cancelled') return null;
  const base = status?.base_commit;
  const head = status?.head_commit;
  if (
    typeof base === 'string' &&
    typeof head === 'string' &&
    SHA_PATTERN.test(base) &&
    SHA_PATTERN.test(head)
  ) {
    return { base, head };
  }
  return null;
}

function shortSha(sha: string | undefined | null): string {
  return sha ? sha.slice(0, SHA_DISPLAY_CHARS) : 'unresolved';
}

export class StatusTreeDataProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private _onDidChangeTreeData: vscode.EventEmitter<vscode.TreeItem | undefined | null | void>;
  readonly onDidChangeTreeData: vscode.Event<vscode.TreeItem | undefined | null | void>;
  status: ReviewStatus | null;
  serviceManager: ServiceLifecycleManager | null;
  profileManager: ProfileManager | null;
  lastReviewBase: string | null;
  /** SHAs of the active run only; null whenever absent, stale, or cleared. */
  lastReviewShas: ResolvedShas | null;
  /** Status updates for any other run are ignored while a run is active. */
  activeRunId: string | null;
  private clearedRunId: string | null = null;
  private comparisonItems = new WeakMap<vscode.TreeItem, { runId: string; range: string }>();

  constructor(
    serviceManager: ServiceLifecycleManager | null = null,
    profileManager: ProfileManager | null = null
  ) {
    this._onDidChangeTreeData = new vscode.EventEmitter<vscode.TreeItem | undefined | null | void>();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this.status = null;
    this.serviceManager = serviceManager;
    this.profileManager = profileManager;
    this.lastReviewBase = null;
    this.lastReviewShas = null;
    this.activeRunId = null;
  }

  setActiveRunId(runId: string | null): void {
    if (this.activeRunId === runId) return;
    if (runId !== null) this.clearedRunId = null;
    this.activeRunId = runId;
    this.lastReviewShas = null;
    this._onDidChangeTreeData.fire();
  }

  refresh(status?: ReviewStatus | null): void {
    if (status !== undefined) {
      if (status && this.activeRunId && status.run_id !== this.activeRunId) return;
      if (status === null) this.activeRunId = null;
      this.status = status;
      // Re-derive on every status update: a run without resolved revisions
      // (pending, cancelled, stale) clears the displayed SHAs immediately.
      this.lastReviewShas =
        status?.run_id && status.run_id === this.clearedRunId
          ? null
          : resolvedShasFrom(status);
    }
    this._onDidChangeTreeData.fire();
  }

  clearShas(): void {
    // A late response from a cleared (cancelled/crashed) run cannot restore it.
    this.clearedRunId = this.status?.run_id ?? null;
    this.lastReviewShas = null;
    this._onDidChangeTreeData.fire();
  }

  getComparisonRange(runId: string | null, item?: vscode.TreeItem): string | null {
    const shas = this.lastReviewShas;
    if (
      !runId || runId !== this.activeRunId || runId !== this.status?.run_id ||
      this.status.status === 'cancelled' || runId === this.clearedRunId || !shas ||
      ![shas.base, shas.head].every(sha =>
        typeof sha === 'string' && (sha.length === 40 || sha.length === 64) &&
        FULL_SHA_PATTERN.test(sha)
      )
    ) return null;
    const range = `${shas.base}...${shas.head}`;
    if (item !== undefined) {
      const comparison = this.comparisonItems.get(item);
      if (comparison?.runId !== runId || comparison.range !== range) return null;
    }
    return range;
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
    items.push(new vscode.TreeItem(`Base Branch: ${this.lastReviewBase ?? 'main (default)'}`));
    // Resolved revisions: abbreviated display, full value in the hover tooltip.
    const shas = this.lastReviewShas;
    const baseItem = new vscode.TreeItem(`Base SHA: ${shortSha(shas?.base)}`);
    const headItem = new vscode.TreeItem(`Head SHA: ${shortSha(shas?.head)}`);
    if (shas) {
      baseItem.tooltip = `Full base commit: ${shas.base}`;
      headItem.tooltip = `Full head commit: ${shas.head}`;
      items.push(baseItem, headItem);
      const comparisonItem = new vscode.TreeItem(
        `Comparison: ${shortSha(shas.base)}...${shortSha(shas.head)}`
      );
      comparisonItem.contextValue = COMPARISON_CONTEXT_VALUE;
      comparisonItem.tooltip = `Full comparison range: ${shas.base}...${shas.head}`;
      if (this.status.run_id) {
        this.comparisonItems.set(comparisonItem, {
          runId: this.status.run_id,
          range: `${shas.base}...${shas.head}`,
        });
      }
      items.push(comparisonItem);
    } else {
      baseItem.tooltip = 'Base revision has not been resolved for this run.';
      headItem.tooltip = 'Head revision has not been resolved for this run.';
      items.push(baseItem, headItem);
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
