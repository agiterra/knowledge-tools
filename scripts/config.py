"""
Knowledge vault configuration loader.

Resolves the vault directory from (in priority order):
  1. --vault CLI arg (parsed by each script)
  2. KNOWLEDGE_VAULT environment variable
  3. .knowledge/ in CWD (default)

Reads <vault>/config.json for vault-specific settings.
Falls back to defaults if config.json doesn't exist.
"""

import json
import os

_vault_dir_override = None
_cache = None


def set_vault_dir(path):
    """Override vault directory (called by CLI arg parsing)."""
    global _vault_dir_override, _cache
    _vault_dir_override = path
    _cache = None  # invalidate config cache


def vault_dir():
    """Return the active vault directory."""
    if _vault_dir_override:
        return _vault_dir_override
    return os.environ.get('KNOWLEDGE_VAULT', '.knowledge')


def load():
    """Load and cache config from <vault>/config.json."""
    global _cache
    if _cache is not None:
        return _cache

    config_file = os.path.join(vault_dir(), 'config.json')
    _cache = {}
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                _cache = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return _cache


def vault_dirs():
    """Return list of directories to index for enrichment.

    Always includes the vault directory. Additional directories come from
    config.json `extra_dirs` array.
    """
    cfg = load()
    vd = vault_dir()
    dirs = [vd]
    for d in cfg.get('extra_dirs', []):
        if os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs
