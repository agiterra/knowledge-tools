/**
 * Knowledge vault configuration loader.
 *
 * Resolves the vault directory from (in priority order):
 *   1. setVaultDir() override (programmatic)
 *   2. KNOWLEDGE_VAULT environment variable
 *   3. .knowledge/ in CWD (default)
 *
 * Reads <vault>/config.json for vault-specific settings.
 */

import { existsSync, readFileSync } from "fs";
import { join, resolve } from "path";

let vaultDirOverride: string | null = null;
let configCache: Record<string, unknown> | null = null;

/** Override vault directory (called by CLI arg parsing). */
export function setVaultDir(path: string): void {
  vaultDirOverride = resolve(path);
  configCache = null;
}

/** Return the active vault directory. */
export function vaultDir(): string {
  if (vaultDirOverride) return vaultDirOverride;
  return process.env.KNOWLEDGE_VAULT ?? ".knowledge";
}

/** Load and cache config from <vault>/config.json. */
export function loadConfig(): Record<string, unknown> {
  if (configCache !== null) return configCache;

  const configFile = join(vaultDir(), "config.json");
  configCache = {};
  if (existsSync(configFile)) {
    try {
      configCache = JSON.parse(readFileSync(configFile, "utf-8"));
    } catch {
      // Malformed or unreadable — use empty config
    }
  }
  return configCache;
}

/**
 * Return list of directories to index for enrichment.
 * Always includes the vault directory. Additional directories come from
 * config.json `extra_dirs` array.
 */
export function vaultDirs(): string[] {
  const cfg = loadConfig();
  const vd = vaultDir();
  const dirs = [vd];
  const extraDirs = (cfg.extra_dirs as string[] | undefined) ?? [];
  for (const d of extraDirs) {
    if (existsSync(d) && !dirs.includes(d)) {
      dirs.push(d);
    }
  }
  return dirs;
}
