"""
repo_fetcher.py — Fetch and process a public GitHub repository's contents.

Strategy:
1. GitHub Trees API (recursive) for full file tree in one call.
2. Filter: skip binary, lock files, dependency dirs, large files (>100KB).
3. Prioritize: Tier 1 (README, manifests, configs), Tier 2 (source), Tier 3 (tests/docs).
4. Budget-aware selection: estimate which files fit in ~100K chars (~25K tokens).
5. Concurrent fetch with semaphore for speed.
6. Assemble: directory tree + metadata + file contents in priority order.
"""

import asyncio
import os
import re
import logging
import subprocess
from pathlib import PurePosixPath
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MAX_TOTAL_CHARS = 100_000
MAX_FILE_CHARS = 15_000
GITHUB_API = "https://api.github.com"
CONCURRENT_REQUESTS = 10
MAX_FILES_TO_FETCH = 80

SKIP_DIRS: set[str] = {
    "node_modules", ".git", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
    ".venv", "venv", "env", ".env", "vendor", "dist", "build", ".next", ".nuxt",
    "out", "target", ".idea", ".vscode", ".gradle", ".cache", ".eggs", "egg-info",
    "site-packages", "coverage", ".coverage", "htmlcov", ".terraform", ".serverless",
}

SKIP_EXTENSIONS: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".tiff",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll", ".exe", ".class",
    ".jar", ".war", ".whl", ".egg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac", ".ogg",
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".lock", ".map", ".min.js", ".min.css",
}

SKIP_FILENAMES: set[str] = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "Gemfile.lock", "Pipfile.lock", "poetry.lock", "composer.lock", "cargo.lock",
    ".DS_Store", "Thumbs.db", ".gitattributes",
}

TIER1_FILENAMES: set[str] = {
    "readme", "readme.md", "readme.rst", "readme.txt",
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "cargo.toml", "go.mod", "go.sum", "build.gradle", "pom.xml",
    "gemfile", "mix.exs", "project.clj",
    "makefile", "cmakelists.txt",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", "requirements.txt", "environment.yml",
    "tsconfig.json", "vite.config.ts", "vite.config.js",
    "webpack.config.js", "rollup.config.js",
    "next.config.js", "next.config.mjs", "nuxt.config.ts", "angular.json",
    "license", "license.md", "license.txt",
    "contributing.md", "changelog.md",
}

TIER3_DIRS: set[str] = {
    "test", "tests", "spec", "specs", "__tests__",
    "examples", "example", "samples", "sample",
    "docs", "doc", "documentation",
    ".github", ".circleci", ".gitlab",
    "scripts", "tools", "benchmarks",
}


async def fetch_repo_contents(github_url: str) -> str:
    owner, repo = _parse_github_url(github_url)
    logger.info("Fetching repo: %s/%s", owner, repo)

    async with httpx.AsyncClient(
        timeout=30.0, limits=httpx.Limits(max_connections=CONCURRENT_REQUESTS),
    ) as client:
        meta = await _get_repo_metadata(client, owner, repo)
        tree = await _get_file_tree(client, owner, repo, meta["default_branch"])
        files = _filter_and_prioritize(tree)
        if not files:
            return ""

        all_paths = [f["path"] for f in tree if f["type"] == "blob"]
        dir_tree = _build_directory_tree(all_paths)

        context_parts: list[str] = []
        chars_used = 0

        tree_section = f"## Directory Tree\n\n```\n{dir_tree}\n```\n"
        context_parts.append(tree_section)
        chars_used += len(tree_section)

        if meta.get("description"):
            s = f"## Repository Description\n\n{meta['description']}\n"
            context_parts.append(s)
            chars_used += len(s)
        if meta.get("language"):
            s = f"## Primary Language\n\n{meta['language']}\n"
            context_parts.append(s)
            chars_used += len(s)

        files_to_fetch = _select_files_within_budget(files, budget=MAX_TOTAL_CHARS - chars_used)
        logger.info("Selected %d of %d candidate files to fetch", len(files_to_fetch), len(files))

        branch = meta["default_branch"]
        semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

        async def _fetch_one(fi):
            async with semaphore:
                content = await _fetch_file_content(client, owner, repo, fi["path"], branch)
                return fi["path"], content

        results = await asyncio.gather(*[_fetch_one(f) for f in files_to_fetch])
        content_map = {path: content for path, content in results}

        for fi in files_to_fetch:
            if chars_used >= MAX_TOTAL_CHARS:
                break
            content = content_map.get(fi["path"])
            if content is None:
                continue
            if len(content) > MAX_FILE_CHARS:
                content = content[:MAX_FILE_CHARS] + "\n\n... [truncated]"
            remaining = MAX_TOTAL_CHARS - chars_used
            section = f"## File: {fi['path']}\n\n```\n{content}\n```\n"
            if len(section) > remaining:
                tc = content[: max(0, remaining - 200)]
                section = f"## File: {fi['path']}\n\n```\n{tc}\n... [truncated]\n```\n"
                context_parts.append(section)
                chars_used += len(section)
                break
            context_parts.append(section)
            chars_used += len(section)

    logger.info("Built context: %d chars from %d file sections", chars_used, len(context_parts) - 1)
    return "\n".join(context_parts)


def _select_files_within_budget(files, budget):
    selected = []
    estimated = 0
    for f in files:
        if len(selected) >= MAX_FILES_TO_FETCH:
            break
        est = min(f["size"], MAX_FILE_CHARS) + 50
        if estimated + est > budget * 1.5:
            break
        selected.append(f)
        estimated += est
    return selected


def _get_github_token() -> Optional[str]:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _build_headers():
    headers = {"Accept": "application/vnd.github+json"}
    token = _get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get_repo_metadata(client, owner, repo):
    headers = _build_headers()
    resp = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=headers)
    if resp.status_code == 404:
        raise FileNotFoundError(f"Repository '{owner}/{repo}' not found. Make sure the URL is correct and the repository is public.")
    if resp.status_code == 403:
        raise PermissionError(f"Access denied for repository '{owner}/{repo}'. The repository may be private or the API rate limit has been exceeded.")
    if resp.status_code != 200:
        raise RuntimeError(f"GitHub API error ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    return {"default_branch": data.get("default_branch", "main"), "description": data.get("description", ""), "language": data.get("language", "")}


async def _get_file_tree(client, owner, repo, branch):
    headers = _build_headers()
    resp = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1", headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch file tree ({resp.status_code}): {resp.text[:300]}")
    return resp.json().get("tree", [])


async def _fetch_file_content(client, owner, repo, path, branch):
    headers = _build_headers()
    headers["Accept"] = "application/vnd.github.raw+json"
    try:
        resp = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}?ref={branch}", headers=headers)
        if resp.status_code != 200:
            return None
        try:
            return resp.text
        except UnicodeDecodeError:
            return None
    except Exception:
        return None


def _filter_and_prioritize(tree):
    results = []
    for item in tree:
        if item["type"] != "blob":
            continue
        path = item["path"]
        size = item.get("size", 0)
        parts = PurePosixPath(path).parts
        filename = parts[-1] if parts else ""
        ext = PurePosixPath(filename).suffix.lower()
        if any(p.lower() in SKIP_DIRS for p in parts[:-1]):
            continue
        if ext in SKIP_EXTENSIONS:
            continue
        if filename.lower() in {s.lower() for s in SKIP_FILENAMES}:
            continue
        if size > 100_000:
            continue
        if filename.startswith(".") and filename.lower() not in {
            ".env.example", ".dockerignore", ".gitignore",
            ".eslintrc.js", ".eslintrc.json", ".prettierrc", ".babelrc", ".editorconfig",
        }:
            continue
        tier = _get_tier(path, filename, parts)
        results.append({"path": path, "size": size, "tier": tier})
    results.sort(key=lambda f: (f["tier"], f["size"]))
    return results


def _get_tier(path, filename, parts):
    fl = filename.lower()
    if fl in TIER1_FILENAMES or fl.startswith("readme"):
        return 1
    if set(p.lower() for p in parts[:-1]) & TIER3_DIRS:
        return 3
    return 2


def _build_directory_tree(paths, max_lines=200):
    tree_dict = {}
    for path in sorted(paths):
        node = tree_dict
        for part in path.split("/"):
            node = node.setdefault(part, {})
    lines = []

    def _render(node, prefix="", depth=0):
        if len(lines) >= max_lines:
            lines.append(f"{prefix}... (truncated)")
            return
        items = sorted(node.items(), key=lambda x: (bool(x[1]), x[0]))
        for i, (name, children) in enumerate(items):
            is_last = i == len(items) - 1
            lines.append(f"{prefix}{'└── ' if is_last else '├── '}{name}")
            if children:
                _render(children, prefix + ("    " if is_last else "│   "), depth + 1)

    _render(tree_dict)
    return "\n".join(lines)


def _parse_github_url(url):
    match = re.match(r"https?://github\.com/([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$", url.strip())
    if not match:
        raise ValueError(f"Invalid GitHub URL: '{url}'. Expected format: https://github.com/<owner>/<repo>")
    return match.group(1), match.group(2)
