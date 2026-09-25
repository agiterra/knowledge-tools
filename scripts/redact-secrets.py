#!/usr/bin/env python3
"""Redact secret-shaped values from a text stream before it is written to disk.
Reads stdin, writes redacted stdout. Two sources of truth:
  1. This process's ENV — any var whose NAME looks secret (SECRET/TOKEN/KEY/PASSWORD/PASSWD/PWD/
     CREDENTIAL/PRIVATE) with a value >= 6 chars has its literal value masked <REDACTED:NAME>.
     The precompact hook inherits the lane's env, which is exactly where the leaked value lives.
  2. Known token SHAPES (github, slack, anthropic, aws, JWT-ish, PEM private keys) as a backstop.
Idempotent, streaming, keeps JSON valid (only replaces substrings inside values).
Added 2026-09-05 after a Vercel bypass secret rode a precompact transcript into the git-tracked vault
and propagated to every clone (Brioche 603156)."""
import os, re, sys

SECRET_NAME = re.compile(r'(SECRET|TOKEN|KEY|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE)', re.I)
# names that match the pattern but are NOT secrets (avoid masking harmless values fleet-wide)
NAME_DENY = re.compile(r'^(.*_PUBLIC_KEY|.*PUBKEY|.*KEY_PATH|.*KEY_FILE|.*_ID|KEYCHAIN.*)$', re.I)

def env_secrets():
    out = []
    for name, val in os.environ.items():
        if not val or len(val) < 6:
            continue
        if SECRET_NAME.search(name) and not NAME_DENY.match(name):
            out.append((name, val))
    # longest value first so a value that contains another is masked whole
    out.sort(key=lambda kv: len(kv[1]), reverse=True)
    return out

SHAPE = [
    ("GITHUB_TOKEN", re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}')),
    ("GITHUB_PAT",   re.compile(r'github_pat_[A-Za-z0-9_]{20,}')),
    ("SLACK_TOKEN",  re.compile(r'xox[baprs]-[A-Za-z0-9-]{10,}')),
    ("ANTHROPIC",    re.compile(r'sk-ant-[A-Za-z0-9_-]{20,}')),
    ("OPENAI",       re.compile(r'sk-[A-Za-z0-9]{20,}')),
    ("AWS_AKID",     re.compile(r'AKIA[0-9A-Z]{16}')),
    ("PEM_PRIVATE",  re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)),
    # 2026-09-25 (j:1845): a public transcript carried 99 Wire JWTs; raw 0x+64-hex is the EVM private-key shape.
    ("JWT",          re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')),
    ("HEX64_KEY",    re.compile(r'(?<![0-9A-Za-z])0x[0-9a-fA-F]{64}(?![0-9A-Za-z])')),
    ("LINEAR_API",   re.compile(r'lin_(?:api|oauth|wh)_[A-Za-z0-9]{20,}')),
]

def main():
    data = sys.stdin.buffer.read().decode("utf-8", "replace")
    for name, val in env_secrets():
        if val in data:
            data = data.replace(val, f"<REDACTED:{name}>")
    for label, rx in SHAPE:
        data = rx.sub(f"<REDACTED:{label}>", data)
    sys.stdout.write(data)

if __name__ == "__main__":
    main()
