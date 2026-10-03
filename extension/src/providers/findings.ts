import * as vscode from 'vscode';
import { Finding } from '../types';

export class FindingsTreeDataProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private _onDidChangeTreeData: vscode.EventEmitter<vscode.TreeItem | undefined | null | void>;
  readonly onDidChangeTreeData: vscode.Event<vscode.TreeItem | undefined | null | void>;
  findings: Finding[];
  activeFindingId: string | null;
  filterSeverity: string | null;
  filterCategory: string | null;

  constructor() {
    this._onDidChangeTreeData = new vscode.EventEmitter<vscode.TreeItem | undefined | null | void>();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this.findings = [];
    this.activeFindingId = null;
    this.filterSeverity = null;
    this.filterCategory = null;
  }

  refresh(findings?: Finding[], activeFindingId?: string | null): void {
    this.findings = findings || [];
    if (activeFindingId !== undefined) {
      this.activeFindingId = activeFindingId;
    }
    this._onDidChangeTreeData.fire();
  }

  setActiveFinding(id: string | null): void {
    this.activeFindingId = id;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: vscode.TreeItem): vscode.TreeItem[] {
    if (element) return [];

    let filtered = this.findings;
    if (this.filterSeverity) {
      filtered = filtered.filter(
        (f) => (f.severity || '').toLowerCase() === this.filterSeverity!.toLowerCase()
      );
    }
    if (this.filterCategory) {
      filtered = filtered.filter(
        (f) => (f.category || '').toLowerCase() === this.filterCategory!.toLowerCase()
      );
    }

    if (!filtered.length) {
      return [
        new vscode.TreeItem(
          this.findings.length ? 'No findings matching filter' : 'No findings found'
        ),
      ];
    }

    return filtered.map((f) => {
      const badge = `[${(f.severity || 'info').toUpperCase()}]`;
      const isActive = this.activeFindingId && f.id === this.activeFindingId;
      const activeMark = isActive ? ' ◀ ACTIVE' : '';
      const dismissedMark = f.dismissed ? ' (Dismissed)' : '';
      const label = `${badge} ${f.category}: ${f.file}:${f.line}${activeMark}${dismissedMark}`;
      const item: any = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
      item.description = f.claim;
      item.tooltip = `Claim: ${f.claim}\nConfidence: ${f.confidence ?? 'unknown'}\nEvidence: ${
        f.evidence_strength ?? 'unknown'
      }\nStatus: ${f.status ?? 'open'}`;
      item.command = {
        command: 'codeatlas.openFindingLocation',
        title: 'Open Finding',
        arguments: [f],
      };
      item.contextValue = 'findingItem';
      item.finding = f;
      return item;
    });
  }
}
