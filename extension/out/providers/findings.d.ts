import * as vscode from 'vscode';
import { Finding } from '../types';
export declare class FindingsTreeDataProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
    private _onDidChangeTreeData;
    readonly onDidChangeTreeData: vscode.Event<vscode.TreeItem | undefined | null | void>;
    findings: Finding[];
    activeFindingId: string | null;
    filterSeverity: string | null;
    filterCategory: string | null;
    constructor();
    refresh(findings?: Finding[], activeFindingId?: string | null): void;
    setActiveFinding(id: string | null): void;
    getTreeItem(element: vscode.TreeItem): vscode.TreeItem;
    getChildren(element?: vscode.TreeItem): vscode.TreeItem[];
}
