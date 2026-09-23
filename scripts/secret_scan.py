"""Block commits that contain secrets (specs.md §10.2, build_guide "no secrets in git").

Scans the files staged for commit (or, with --all, every tracked file) for
patterns that look like real credentials. Run automatically by the
pre-commit hook in .githooks/; run by hand with:

    .venv/Scripts/python scripts/secret_scan.py --all
"""

import re
import subprocess
import sys

# (label, regex). Kept deliberately specific so docs that *mention* a key
# name (e.g. "ANTHROPIC_API_KEY") don't trip it; only values that look real.
PATTERNS = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("Apify token", re.compile(r"apify_api_[A-Za-z0-9]{20,}")),
    ("Firecrawl key", re.compile(r"\bfc-[a-f0-9]{24,}\b")),
    ("Supabase secret key", re.compile(r"sb_secret_[A-Za-z0-9_\-]{20,}")),
    ("JWT (e.g. Supabase service/anon key)", re.compile(r"eyJ[A-Za-z0-9_\-]{15,}\.eyJ[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{10,}")),
    # A Postgres URL with a real-looking inline password (not a <placeholder>).
    ("Postgres URL with password", re.compile(r"postgres(?:ql)?://[^\s:/@<>]+:(?!<)[^\s@<>]{8,}@")),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

NEVER_COMMIT = re.compile(r"(^|/)\.env(\.[^/]*)?$")
ALLOWED_ENV_FILES = {".env.example"}


def git_files(all_tracked: bool) -> list[str]:
    if all_tracked:
        cmd = ["git", "ls-files"]
    else:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def read_file(path: str, all_tracked: bool) -> str:
    if all_tracked:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                return f.read()
        except OSError:
            return ""
    # Scan exactly what is staged, not the working copy.
    result = subprocess.run(["git", "show", f":{path}"], capture_output=True)
    return result.stdout.decode("utf-8", errors="ignore")


def main() -> int:
    all_tracked = "--all" in sys.argv
    problems: list[str] = []
    for path in git_files(all_tracked):
        name = path.replace("\\", "/")
        if NEVER_COMMIT.search(name) and name.rsplit("/", 1)[-1] not in ALLOWED_ENV_FILES:
            problems.append(f"{path}: env files must never be committed")
            continue
        text = read_file(path, all_tracked)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    # Never echo the secret itself; location + type is enough.
                    problems.append(f"{path}:{lineno}: looks like a {label}")
    if problems:
        print("Secret scan FAILED; commit blocked:")
        for p in problems:
            print("  " + p)
        print("Remove the secret (use .env / Render env vars), then commit again.")
        return 1
    print(f"Secret scan passed ({'all tracked' if all_tracked else 'staged'} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
