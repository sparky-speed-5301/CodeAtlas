import { Profile } from './types';
export declare const DEFAULT_PROFILE: Readonly<Profile>;
export declare const ALIASES: Record<string, keyof Profile>;
export declare const ALLOWED_PROFILE_KEYS: Set<string>;
export declare const FORBIDDEN_PROFILE_KEYS: Set<string>;
export declare function validateProfileName(name: unknown): string;
export declare function validateProfile(name: unknown, rawProfile: unknown): Profile;
export declare function configurationPath(root: string, relativePath: unknown): string;
export declare function readWorkspaceConfiguration(root: string | null, relativePath: string): Record<string, unknown> | undefined;
export declare class ProfileManager {
    workspaceRoot: string | null;
    userProfiles: Record<string, unknown>;
    baseSettings: Record<string, unknown>;
    profilePath: string;
    activeProfileName: string;
    error: string | null;
    constructor(workspaceRoot?: string | null, userProfiles?: Record<string, unknown>, baseSettings?: Record<string, unknown>);
    loadProfiles(customPath?: string): Record<string, Profile>;
    getActiveProfile(profiles?: Record<string, Profile>): Profile;
    select(name: string, profiles?: Record<string, Profile>): Profile;
}
