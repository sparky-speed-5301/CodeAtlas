import * as vscode from 'vscode';
import { ProfileManager } from '../profiles';
import { ServiceLifecycleManager } from '../service';
import { ReviewStatus } from '../types';
export interface ResolvedShas {
    base: string;
    head: string;
}
export declare class StatusTreeDataProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
    private _onDidChangeTreeData;
    readonly onDidChangeTreeData: vscode.Event<vscode.TreeItem | undefined | null | void>;
    status: ReviewStatus | null;
    serviceManager: ServiceLifecycleManager | null;
    profileManager: ProfileManager | null;
    lastReviewBase: string | null;
    /** SHAs of the active run only; null whenever absent, stale, or cleared. */
    lastReviewShas: ResolvedShas | null;
    /** Status updates for any other run are ignored while a run is active. */
    activeRunId: string | null;
    private clearedRunId;
    private comparisonItems;
    constructor(serviceManager?: ServiceLifecycleManager | null, profileManager?: ProfileManager | null);
    setActiveRunId(runId: string | null): void;
    refresh(status?: ReviewStatus | null): void;
    clearShas(): void;
    getComparisonRange(runId: string | null, item?: vscode.TreeItem): string | null;
    getTreeItem(element: vscode.TreeItem): vscode.TreeItem;
    getChildren(element?: vscode.TreeItem): vscode.TreeItem[];
}
