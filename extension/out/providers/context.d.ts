import * as vscode from 'vscode';
import { Finding, FindingContext } from '../types';
export declare class ContextTreeDataProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
    private _onDidChangeTreeData;
    readonly onDidChangeTreeData: vscode.Event<vscode.TreeItem | undefined | null | void>;
    context: FindingContext | null;
    activeFinding: Finding | null;
    cursorLine: number | null;
    multipleFindingsCount: number;
    constructor();
    refresh(context: FindingContext | null, activeFinding?: Finding | null, cursorLine?: number | null, multipleFindingsCount?: number): void;
    getTreeItem(element: vscode.TreeItem): vscode.TreeItem;
    getChildren(element?: vscode.TreeItem): vscode.TreeItem[];
}
