import * as vscode from 'vscode';
import { ProfileManager } from '../profiles';
import { ServiceLifecycleManager } from '../service';
import { ReviewStatus } from '../types';
export declare class StatusTreeDataProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
    private _onDidChangeTreeData;
    readonly onDidChangeTreeData: vscode.Event<vscode.TreeItem | undefined | null | void>;
    status: ReviewStatus | null;
    serviceManager: ServiceLifecycleManager | null;
    profileManager: ProfileManager | null;
    constructor(serviceManager?: ServiceLifecycleManager | null, profileManager?: ProfileManager | null);
    refresh(status?: ReviewStatus | null): void;
    getTreeItem(element: vscode.TreeItem): vscode.TreeItem;
    getChildren(element?: vscode.TreeItem): vscode.TreeItem[];
}
