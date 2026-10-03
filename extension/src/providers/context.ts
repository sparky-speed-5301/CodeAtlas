import * as vscode from 'vscode';
import { Finding, FindingContext } from '../types';

export class ContextTreeDataProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private _onDidChangeTreeData: vscode.EventEmitter<vscode.TreeItem | undefined | null | void>;
  readonly onDidChangeTreeData: vscode.Event<vscode.TreeItem | undefined | null | void>;
  context: FindingContext | null;
  activeFinding: Finding | null;
  cursorLine: number | null;
  multipleFindingsCount: number;

  constructor() {
    this._onDidChangeTreeData = new vscode.EventEmitter<vscode.TreeItem | undefined | null | void>();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this.context = null;
    this.activeFinding = null;
    this.cursorLine = null;
    this.multipleFindingsCount = 0;
  }

  refresh(
    context: FindingContext | null,
    activeFinding: Finding | null = null,
    cursorLine: number | null = null,
    multipleFindingsCount: number = 0
  ): void {
    this.context = context;
    this.activeFinding = activeFinding;
    this.cursorLine = cursorLine;
    this.multipleFindingsCount = multipleFindingsCount;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: vscode.TreeItem): vscode.TreeItem[] {
    if (element) return [];

    if (!this.context) {
      if (this.cursorLine) {
        return [
          new vscode.TreeItem(`No finding at cursor (line ${this.cursorLine})`),
          new vscode.TreeItem(
            'Move cursor to a flagged line or select a finding to view context'
          ),
        ];
      }
      return [new vscode.TreeItem('Select a finding or place cursor on a flagged line')];
    }

    const items: vscode.TreeItem[] = [];

    if (this.activeFinding) {
      const f = this.activeFinding;
      const badge = `[${(f.severity || 'INFO').toUpperCase()}]`;
      const label = `Active: ${badge} ${f.category} (${f.file}:${f.line})`;
      const fItem = new vscode.TreeItem(label);
      fItem.description = f.claim;
      items.push(fItem);

      if (this.multipleFindingsCount > 1) {
        items.push(
          new vscode.TreeItem(
            `Multiple findings on this line: ${this.multipleFindingsCount} (showing primary)`
          )
        );
      }
    }

    const lines = this.context.changed_lines || [];
    items.push(new vscode.TreeItem(`Changed lines: ${lines.length ? lines.join(', ') : 'none'}`));

    if (this.context.containing_symbol) {
      const s = this.context.containing_symbol;
      items.push(
        new vscode.TreeItem(
          `Symbol: ${s.kind || 'symbol'} ${s.name || ''} (lines ${s.start_line}-${s.end_line})`
        )
      );
    } else {
      items.push(new vscode.TreeItem('Symbol: none'));
    }

    const impCount = (this.context.relevant_imports || []).length;
    items.push(new vscode.TreeItem(`Relevant imports: ${impCount}`));

    const refCount = (this.context.relevant_references || []).length;
    items.push(new vscode.TreeItem(`Relevant references: ${refCount}`));

    const tests = this.context.related_tests || [];
    items.push(new vscode.TreeItem(`Related tests: ${tests.length ? tests.join(', ') : 'none'}`));

    const cands = (this.context.retrieved_context_candidates || []).length;
    items.push(new vscode.TreeItem(`Retrieved context candidates: ${cands}`));

    items.push(
      new vscode.TreeItem(
        `Truncation status: ${this.context.truncation_status ? 'truncated' : 'full'}`
      )
    );

    const ev = (this.context.evidence_sources || []).join(', ');
    items.push(new vscode.TreeItem(`Evidence sources: ${ev || 'deterministic'}`));

    return items;
  }
}
