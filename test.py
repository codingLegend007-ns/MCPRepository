"""
get_git_github_values.py

Produces:
  - owner        : repository owner/org (from 'origin' remote)
  - repo         : repository name (from 'origin' remote)
  - local_user   : git local user.name (git config user.name)
  - last_author  : last commit author name and email
  - head_branch  : current branch name (or 'HEAD' if detached)
  - head_sha     : current commit SHA (HEAD)
  - default_branch : repository default branch (from GitHub API if token provided)
  - base_ref     : suggested base (default_branch or origin/<default_branch>)
  - base_sha     : SHA of base ref (if resolvable)

Usage:
  # using env var:
  export GITHUB_TOKEN=ghp_xxx   # optional but recommended
  python get_git_github_values.py

  # or provide token as arg:
  python get_git_github_values.py --token ghp_xxx

Notes:
 - Run this inside a git working tree (has .git).
 - If origin remote is missing or commands fail, script raises a helpful error.
"""

import os
import re
import subprocess
import json
import argparse
from typing import Optional, Tuple
import requests

GITHUB_API_BASE = "https://api.github.com"


def run_git(*args: str, cwd: Optional[str] = None) -> str:
    """Run git command and return stdout (stripped). Raises RuntimeError on failure."""
    try:
        out = subprocess.check_output(("git",) + args, stderr=subprocess.STDOUT, cwd=cwd)
        return out.decode().strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git {' '.join(args)} failed: {e.output.decode().strip()}")


def parse_remote_owner_repo(remote_url: str) -> Tuple[str, str]:
    """
    Parse remote URL to (owner, repo). Handles:
      - git@github.com:owner/repo.git
      - https://github.com/owner/repo.git
      - ssh://git@github.com/owner/repo.git
    """
    s = remote_url.strip()
    # strip trailing .git if present
    if s.endswith(".git"):
        s = s[:-4]

    # SSH style git@github.com:owner/repo
    m = re.search(r"[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)$", s)
    if m:
        return m.group("owner"), m.group("repo")

    raise ValueError(f"Cannot parse owner/repo from remote URL: {remote_url}")


def github_api_get(path: str, token: Optional[str]) -> dict:
    url = GITHUB_API_BASE.rstrip("/") + path
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "get-git-github-values-script/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def try_map_email_to_login(email: str, token: Optional[str]) -> Optional[str]:
    """
    Best-effort mapping from commit author email -> GitHub login using the Search Commits API.
    This endpoint requires a special Accept header (cloak-preview). It may not always return a mapping.
    If token omitted or search fails, returns None.
    """
    if not token:
        return None
    # Search commits by author-email (requires 'application/vnd.github.cloak-preview' accept)
    q = f"author-email:{email}"
    url = f"{GITHUB_API_BASE}/search/commits?q={requests.utils.requote_uri(q)}"
    headers = {
        "Accept": "application/vnd.github.cloak-preview",
        "Authorization": f"Bearer {token}",
        "User-Agent": "get-git-github-values-script/1.0",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("items") or []
        # items' structure: each item may contain 'author' dict with 'login'
        for it in items:
            author = it.get("author")
            if author and author.get("login"):
                return author.get("login")
        return None
    except Exception:
        return None


def main(token: Optional[str] = None):
    # 1) Ensure we're inside a git repo
    try:
        run_git("rev-parse", "--is-inside-work-tree")
    except Exception as e:
        raise SystemExit(f"Not inside a git repository: {e}")

    # 2) remote origin url
    try:
        remote_url = run_git("remote", "get-url", "origin")
    except Exception:
        # fallback: list remotes and pick the first
        remotes = run_git("remote").splitlines()
        if not remotes:
            raise SystemExit("No git remote found. Make sure 'origin' remote exists or add a remote.")
        remote_name = remotes[0].strip()
        remote_url = run_git("remote", "get-url", remote_name)

    owner, repo = parse_remote_owner_repo(remote_url)

    # 3) local configured user.name (may be empty)
    local_user = ""
    try:
        local_user = run_git("config", "user.name")
    except Exception:
        local_user = ""

    # 4) last commit author name and email
    try:
        last_author_name = run_git("log", "-1", "--pretty=format:%an")
        last_author_email = run_git("log", "-1", "--pretty=format:%ae")
    except Exception:
        last_author_name = ""
        last_author_email = ""

    # 5) head branch and head sha
    try:
        head_branch = run_git("rev-parse", "--abbrev-ref", "HEAD")
    except Exception:
        head_branch = "HEAD"  # detached

    try:
        head_sha = run_git("rev-parse", "HEAD")
    except Exception:
        head_sha = ""

    # 6) fetch remote refs lightly (try to ensure origin/<branch> exists)
    # Not forcing full fetch; just try `git remote show origin` to see HEAD branch mapping
    default_branch = None
    base_sha = None

    # First try to get default branch from GitHub API if token provided
    if token:
        try:
            repo_meta = github_api_get(f"/repos/{owner}/{repo}", token)
            default_branch = repo_meta.get("default_branch")
        except Exception:
            default_branch = None

    # If no token or API failed, try to detect via origin/HEAD or remote show
    if not default_branch:
        try:
            # symbolic-ref can work if origin/HEAD is set locally
            out = run_git("symbolic-ref", "refs/remotes/origin/HEAD")
            # refs/remotes/origin/<branch>
            default_branch = out.rsplit("/", 1)[-1]
        except Exception:
            # fallback to parsing `git remote show origin`
            try:
                info = run_git("remote", "show", "origin")
                # look for "HEAD branch: <name>"
                m = re.search(r"HEAD branch: (.+)", info)
                if m:
                    default_branch = m.group(1).strip()
            except Exception:
                default_branch = None

    # final fallback
    if not default_branch:
        default_branch = "main"  # common default; user can override if needed

    # Attempt to compute base ref and base SHA
    # Prefer origin/<default_branch> if available locally; else fallback to GitHub API if token
    # First check if origin/<default_branch> exists locally and get its SHA
    try:
        base_sha = run_git("rev-parse", f"origin/{default_branch}")
        base_ref = f"origin/{default_branch}"
    except Exception:
        # try local branch named default_branch
        try:
            base_sha = run_git("rev-parse", default_branch)
            base_ref = default_branch
        except Exception:
            # fallback: ask GitHub API for branch commit SHA
            if token:
                try:
                    branch_data = github_api_get(f"/repos/{owner}/{repo}/branches/{default_branch}", token)
                    base_sha = branch_data.get("commit", {}).get("sha")
                    base_ref = default_branch
                except Exception:
                    base_sha = None
                    base_ref = default_branch
            else:
                base_sha = None
                base_ref = default_branch

    # 7) optionally try mapping last_author_email->github login (best-effort, requires token)
    mapped_login = None
    if last_author_email and token:
        mapped_login = try_map_email_to_login(last_author_email, token)

    result = {
        "owner": owner,
        "repo": repo,
        "remote_url": remote_url,
        "local_user": local_user,
        "last_author_name": last_author_name,
        "last_author_email": last_author_email,
        "mapped_last_author_github_login": mapped_login,
        "head_branch": head_branch,
        "head_sha": head_sha,
        "default_branch": default_branch,
        "base_ref": base_ref if 'base_ref' in locals() else None,
        "base_sha": base_sha,
    }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--token", "-t", help="GitHub personal access token (or set GITHUB_TOKEN env var)", default=None)
    args = p.parse_args()
    token = args.token or os.environ.get("GITHUB_TOKEN")
    try:
        main(token=token)
    except SystemExit as e:
        print(f"Error: {e}")
        raise SystemExit(1)
    except Exception as e:
        print(f"Unhandled error: {e}")
        raise SystemExit(2)
