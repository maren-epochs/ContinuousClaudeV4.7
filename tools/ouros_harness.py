"""Ouros Harness — programmatic code execution with external function bridge.

Runs agent-written Python in an ouros sandbox. External functions (exa_search,
nia_docs, etc.) pause execution, call the real API outside the sandbox, and
resume with results. The model never sees raw API responses — only the computed
output from the agent's Python code.

Usage:
    # Execute code with access to research tools
    python ouros_harness.py --file /tmp/agent-code.py

    # With session persistence
    python ouros_harness.py --file code.py --session dive-auth --storage thoughts/shared/dives

    # Continue a session (an existing saved session loads by default;
    # --load is accepted but no longer required)
    python ouros_harness.py --file code.py --session dive-auth

    # Discard saved state and start the session fresh
    python ouros_harness.py --file code.py --session dive-auth --reset

    # List variables in a session
    python ouros_harness.py --session dive-auth --list-vars

    # Get a variable as JSON
    python ouros_harness.py --session dive-auth --get-var research

    # Fork a session
    python ouros_harness.py --session dive-auth --fork approach-a
"""

import argparse
import asyncio
import io
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

# Sandbox output is arbitrary text - search results routinely contain emoji. A
# Windows console defaults to cp1252, so printing them raises UnicodeEncodeError
# and kills the run after the work is already done. Force UTF-8 and never fail on
# a character we cannot represent.
for _stream in (sys.stdout, sys.stderr):
    try:
        if isinstance(_stream, io.TextIOWrapper):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# External function registry
# ---------------------------------------------------------------------------


def _load_env():
    """Load API keys from ~/.claude/.env if present."""
    env_path = Path.home() / ".claude" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class ExaCallOptions:
    """Sandbox exa_search() keywords after the query, in positional order."""

    num_results: int = 5
    category: str | None = None
    domains: list | None = None
    with_text: bool = False
    start_date: str | None = None


async def _call_exa_search(query, *args, **kwargs):
    """Bridge to the real Exa API; args/kwargs are ExaCallOptions fields."""
    o = ExaCallOptions(*args, **kwargs)
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from exa_search import exa_search, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "EXA_API_KEY not found"}

    params = {"query": query, "num_results": o.num_results, "with_text": o.with_text}
    if o.category:
        params["category"] = o.category
    if o.domains and isinstance(o.domains, list):
        params["domains"] = o.domains
    if o.start_date:
        params["start_date"] = o.start_date

    return await exa_search(**params)


def _call_exa_search_sync(*args, **kwargs):
    """Sync wrapper — ouros external functions must be sync."""
    return asyncio.run(_call_exa_search(*args, **kwargs))


async def _call_nia_search(
    query,
    repositories=None,
    data_sources=None,
    search_mode="unified",
    include_sources=True,
):
    """Bridge to the real Nia documentation search API.

    Args:
        query: Search query string
        repositories: Optional list of repository identifiers
        data_sources: Optional list of documentation source IDs
        search_mode: 'unified' (default, searches everything), 'repositories', or 'data_sources'
        include_sources: Whether to include source metadata in results
    """
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from nia_docs import search_query, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "NIA_API_KEY not found"}

    messages = [{"role": "user", "content": query}]
    return await search_query(
        messages=messages,
        repositories=repositories,
        data_sources=data_sources,
        search_mode=search_mode,
        include_sources=include_sources,
    )


def _call_nia_search_sync(*args, **kwargs):
    """Sync wrapper — ouros external functions must be sync."""
    return asyncio.run(_call_nia_search(*args, **kwargs))


async def _call_nia_universal(query, limit=10):
    """Bridge to Nia universal search — searches all 10k+ public indexed sources."""
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from nia_docs import search_universal, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "NIA_API_KEY not found"}

    return await search_universal(query=query, limit=limit)


def _call_nia_universal_sync(*args, **kwargs):
    """Sync wrapper for nia_universal."""
    return asyncio.run(_call_nia_universal(*args, **kwargs))


async def _call_nia_web(query, category=None, time_range=None):
    """Bridge to Nia web search."""
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from nia_docs import search_web, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "NIA_API_KEY not found"}

    return await search_web(query=query, category=category, time_range=time_range)


def _call_nia_web_sync(*args, **kwargs):
    """Sync wrapper for nia_web."""
    return asyncio.run(_call_nia_web(*args, **kwargs))


async def _call_nia_package(package, query, registry="npm", limit=10):
    """Bridge to Nia package-specific semantic search.

    Args:
        package: Package name (e.g. 'express', 'fastapi')
        query: What to search for within the package
        registry: 'npm', 'py_pi', 'crates_io', 'go_modules'
        limit: Max results (default 10)
    """
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from nia_docs import search_package_hybrid, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "NIA_API_KEY not found"}

    return await search_package_hybrid(
        package=package, query=query, registry=registry, limit=limit
    )


def _call_nia_package_sync(*args, **kwargs):
    """Sync wrapper for nia_package."""
    return asyncio.run(_call_nia_package(*args, **kwargs))


async def _call_nia_package_grep(package, pattern, registry="npm", limit=10):
    """Bridge to Nia package regex search.

    Args:
        package: Package name (e.g. 'express', 'fastapi')
        pattern: Regex pattern to search for in package source
        registry: 'npm', 'py_pi', 'crates_io', 'go_modules'
        limit: Max results (default 10)
    """
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from nia_docs import search_package_grep, load_api_key

    api_key = load_api_key()
    if not api_key:
        return {"error": "NIA_API_KEY not found"}

    return await search_package_grep(
        package=package, pattern=pattern, registry=registry, limit=limit
    )


def _call_nia_package_grep_sync(*args, **kwargs):
    """Sync wrapper for nia_package_grep."""
    return asyncio.run(_call_nia_package_grep(*args, **kwargs))


# nia_help table: function name -> description/args/returns. _call_nia_help
# hands out a deep copy so a caller mutating its result never changes this.
_NIA_HELP = {
    "research_package": {
        "description": "ALL-IN-ONE: Research a package for building a context block. Runs nia_search + nia_package + nia_package_grep + exa_search, filters noise, returns compact structured data. USE THIS FIRST.",
        "args": {
            "package": "Package name, e.g. 'express' (required)",
            "version": "Version string, e.g. '5.1.0' (optional, improves search)",
            "registry": "'npm', 'py_pi', 'crates_io', 'go_modules' (default 'npm')",
            "max_results": "Max results per search call (default 3)",
            "max_chars": "Max chars per result content (default 600)",
        },
        "returns": "dict with sections: nia_answer, official_docs, source_patterns, deprecations, guides",
    },
    "nia_search": {
        "description": "Search indexed docs and repositories. Returns answer + ranked results.",
        "args": {
            "query": "Search query (required)",
            "repositories": "List of repo identifiers (optional)",
            "data_sources": "List of doc source IDs (optional)",
            "search_mode": "'unified' (default, all sources), 'repositories', 'data_sources'",
            "include_sources": "Include source metadata (default True)",
        },
        "returns": "dict with 'answer' (synthesized markdown), 'results' (ranked docs with content, score, source)",
    },
    "nia_universal": {
        "description": "Search all 10k+ public indexed sources. Broader than nia_search.",
        "args": {
            "query": "Search query (required)",
            "limit": "Max results (default 10)",
        },
    },
    "nia_web": {
        "description": "Web search via Nia.",
        "args": {
            "query": "Search query (required)",
            "category": "Filter category (optional)",
            "time_range": "Time filter (optional)",
        },
    },
    "nia_package": {
        "description": "Semantic search WITHIN a specific package's source code.",
        "args": {
            "package": "Package name, e.g. 'express' (required)",
            "query": "What to search for (required)",
            "registry": "'npm', 'py_pi', 'crates_io', 'go_modules' (default 'npm')",
            "limit": "Max results (default 10)",
        },
    },
    "nia_package_grep": {
        "description": "Regex search WITHIN a specific package's source code.",
        "args": {
            "package": "Package name (required)",
            "pattern": "Regex pattern (required)",
            "registry": "'npm', 'py_pi', 'crates_io', 'go_modules' (default 'npm')",
            "limit": "Max results (default 10)",
        },
    },
    "exa_search": {
        "description": "Semantic web search via Exa AI. Good for blogs, guides, broader coverage.",
        "args": {
            "query": "Search query (required)",
            "num_results": "Number of results (default 5)",
            "category": "Filter: 'research paper', 'github', 'news', etc. (optional)",
            "domains": "List of domains to restrict to (optional)",
            "with_text": "Include full page text (default False)",
            "start_date": "Filter: published after YYYY-MM-DD (optional)",
        },
    },
    "llm_call": {
        "description": "Call an LM as a sub-query (RLM recursive call). You choose the backend explicitly. Returns text.",
        "args": {
            "prompt": "Text prompt to send (required)",
            "model": "Model ID — depends on backend. Anthropic: claude-haiku-4-5-20251001, claude-sonnet-4-6. OpenAI: gpt-4o-mini. LM Studio: whatever is loaded. OpenRouter: anthropic/claude-sonnet-4-6, etc.",
            "max_tokens": "Max response tokens (default 1000)",
            "system": "Optional system prompt",
            "temperature": "Sampling temperature (default 0.0)",
            "backend": "'anthropic' (default), 'openai', 'local' (LM Studio), 'openrouter'",
        },
        "returns": "str — the model's text response",
    },
    "agent_call": {
        "description": "Spawn a headless agent with full tool access (RLM recursive call). The agent can read/write files, run commands, iterate. Returns final output.",
        "args": {
            "prompt": "Task description for the agent (required)",
            "agent": "'claude-code' (default) or 'codex'",
            "model": "Model override: 'sonnet', 'opus', 'haiku' (optional)",
            "max_turns": "Max agent turns (default 25; claude-code only)",
            "timeout": "Max seconds to wait (default 600 = 10 min)",
            "cwd": "Working directory for the agent (optional)",
        },
        "returns": "str — the agent's final text output",
    },
    "read_file": {
        "description": "Read a file from the host filesystem (subject to security policy).",
        "args": {"path": "File path (required)"},
    },
    "write_file": {
        "description": "Write content to a file (subject to security policy). Binary-safe: content may be str (UTF-8) or bytes.",
        "args": {
            "path": "File path (required)",
            "content": "str or bytes content (required)",
        },
    },
    "glob_files": {
        "description": "Find files matching a glob pattern (root and results filtered by the read policy; secrets never returned).",
        "args": {
            "pattern": "Glob pattern (required)",
            "path": "Base directory (default '.')",
        },
    },
    "run_command": {
        "description": "Run an allowlisted command WITHOUT a shell: tldr, grep, rg, wc, echo, git log/diff/show/blame, cargo build/test/clippy, npm test/run, python -m pytest, uv run python. No pipes, chaining, redirection or substitution.",
        "args": {
            "cmd": "Command string or argv list (required)",
            "timeout": "Timeout in seconds (default 30)",
        },
    },
    "run_python": {
        "description": "Execute Python on the HOST CPython (py -3.13, full DS stack: pandas, numpy, matplotlib, polars, duckdb, sklearn...). Use for numerics the sandbox cannot do (import pandas is impossible in-sandbox). cwd is the per-session work dir under the sandbox output root; relative savefig()/to_csv() outputs are surfaced as artifacts. print() inside the code to get data back.",
        "args": {
            "code": "Python source to execute on the host (required)",
            "timeout": "Seconds before the process tree is killed (default 60, max 600)",
        },
        "returns": "str — combined stdout+stderr, last ~8KB, '[truncated]' marker when capped, '[exit code: N]' on failure",
    },
    "nia_help": {
        "description": "Show this help text.",
    },
}


def _call_nia_help():
    """Return help text describing all available nia functions (a fresh copy)."""
    return json.loads(json.dumps(_NIA_HELP))


def _call_research_package(
    package, version=None, registry="npm", max_results=3, max_chars=600
):
    """All-in-one research function for building context blocks.

    Runs nia_search + nia_package + nia_package_grep + exa_search,
    filters noise, truncates, and returns a compact structured result.
    The agent uses this output to write the actual context block.

    Args:
        package: Package name (e.g. 'express', 'fastapi')
        version: Version string (e.g. '5.1.0') — used in search queries
        registry: 'npm', 'py_pi', 'crates_io', 'go_modules'
        max_results: Max results per search call (default 3)
        max_chars: Max chars per result content (default 600)

    Returns:
        dict with sections: nia_answer, official_docs, source_patterns,
        deprecations, guides, plus metadata (package, version_indexed, sources_used)
    """
    version_str = f" v{version}" if version else ""
    major = (
        version.split(".")[0] if version else ""
    )  # unused; kept so a non-str version still raises

    output = {
        "package": package,
        "version_requested": version,
        "version_indexed": None,
        "registry": registry,
        "sources_used": 0,
        "sections": {},
    }

    # 1. Structured docs via nia_search
    docs = _call_nia_search_sync(
        f"{package}{version_str} API breaking changes migration guide"
    )
    _add_section(output, "nia_answer", docs.get("answer", ""), count=1)
    official_docs = _official_docs(docs, package, max_results, max_chars)
    _add_section(output, "official_docs", official_docs)

    # 2. Source code patterns via nia_package
    pkg = _call_nia_package_sync(
        package, "API usage patterns middleware routing", registry=registry
    )
    output["version_indexed"] = pkg.get("version_used", "unknown")
    patterns = _source_patterns(pkg, max_results, max_chars)
    _add_section(output, "source_patterns", patterns)

    # 3. Deprecations via nia_package_grep (twice the hits of other sections)
    grep = _call_nia_package_grep_sync(package, "deprecat", registry=registry)
    _add_section(output, "deprecations", _deprecations(grep, max_results * 2))

    # 4. Broader coverage via exa_search
    exa = _call_exa_search_sync(
        f"{package}{version_str} migration guide best practices breaking changes",
        num_results=max_results,
    )
    _add_section(output, "guides", _guides(exa))

    return output


# nia_search sources kept even when the package name is not in display_name.
_TRUSTED_DOC_HOSTS = ("github.com", "npmjs.com", "pypi.org")


def _add_section(output, name, value, count=None):
    """Store a non-empty section; add `count` (default len(value)) to sources_used."""
    if value:
        output["sections"][name] = value
        output["sources_used"] += len(value) if count is None else count


def _official_docs(docs, package, max_results, max_chars):
    """nia_search hits from the package's own or a trusted source, content truncated."""
    official = []
    for r in docs.get("results", [])[:max_results]:
        src = r.get("source", {})
        display = src.get("display_name", "").lower()
        # Skip obvious noise (other packages, unrelated sites)
        if package.lower() not in display and display not in _TRUSTED_DOC_HOSTS:
            continue
        official.append(
            {
                "source": src.get("display_name", ""),
                "doc": src.get("document_name", ""),
                "content": r.get("content", "")[:max_chars],
            }
        )
    return official


def _source_patterns(pkg, max_results, max_chars):
    """Non-empty source documents from nia_package hits, truncated to max_chars."""
    return [
        code[:max_chars]
        for r in pkg.get("results", [])[:max_results]
        if (code := r.get("document", ""))
    ]


def _deprecations(grep, limit):
    """file/line/200-char content of the first `limit` grep hits that have content."""
    found = []
    for hit in grep.get("results", [])[:limit]:
        res = hit.get("result", hit)
        content = res.get("content", "")
        if content:
            found.append(
                {
                    "file": res.get("file_path", ""),
                    "line": res.get("start_line", ""),
                    "content": content[:200],
                }
            )
    return found


def _guides(exa):
    """Title and 300-char summary of every exa_search result."""
    return [
        {"title": r.get("title", ""), "summary": r.get("summary", "")[:300]}
        for r in exa.get("results", [])
    ]


# ---------------------------------------------------------------------------
# Security policy — controls what bridge functions can access
# ---------------------------------------------------------------------------

SECURITY_POLICY = {
    # Directories the sandbox can read from (resolved to absolute paths at runtime)
    "read_allow": [
        ".",  # current project (cwd; ignored when cwd is home or a drive root)
        "/tmp/ouros",  # ouros source
    ],
    # Secrets are never readable, even under an allowed root (checked after
    # resolving symlinks and '..'). Names match case-insensitively.
    "read_deny_names": [
        ".env",
        ".env.*",  # .env.example/.sample/.template stay readable
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "*.keystore",
        "id_rsa*",
        "id_dsa*",
        "id_ecdsa*",
        "id_ed25519*",
        ".credentials*",
        ".claude.json",
        ".netrc",
        "_netrc",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
    ],
    "read_deny_dirs": [  # relative to home
        ".ssh",
        ".aws",
        ".gnupg",
        ".azure",
        ".kube",
        ".docker",
        ".config/gh",
    ],
    # Directories the sandbox can write to
    "write_allow": [
        "/tmp/ouros-sandbox-output",
    ],
    # Commands the sandbox can run: argv-prefix match on the PARSED command,
    # executed with shell=False, so chaining (& | ; && ||), redirection and
    # substitution are never interpreted. claude/codex are not here — spawn
    # agents through agent_call, which bounds turns and permission mode.
    # cargo/npm/pytest/uv run project code by design (same trust as run_python).
    "command_allow": [
        ("tldr",),
        ("grep",),
        ("rg",),
        ("wc",),
        ("echo",),
        ("git", "log"),
        ("git", "diff"),
        ("git", "show"),
        ("git", "blame"),
        ("cargo", "build"),
        ("cargo", "test"),
        ("cargo", "clippy"),
        ("npm", "test"),
        ("npm", "run"),
        ("python", "-m", "pytest"),
        ("uv", "run", "python"),
    ],
    # Arguments that turn an allowed command into an exec or arbitrary write:
    # rg --pre runs a program per file; git --output writes anywhere;
    # --ext-diff/--textconv run configured external programs.
    "command_deny_args": [
        "--pre",
        "--output",
        "--ext-diff",
        "--textconv",
        "--open-in-pager",
        "--no-index",
        "--contents",
    ],
    # Patterns that are always blocked
    "command_deny": [
        "rm ",
        "rm\t",
        "rmdir",
        "chmod",
        "chown",
        "curl ",
        "wget ",
        "ssh ",
        "scp ",
        "sudo ",
        "kill ",
        "pkill",
        "> /dev/",
        ">> /dev/",
        "| sh",
        "| bash",
        "| zsh",
        "eval ",
        "exec ",
    ],
    # agent_call turn bound when the caller passes none (claude -p --max-turns).
    "agent_default_max_turns": 25,
    # run_python — the DS bridge. The ouros sandbox is a reimplemented Rust
    # interpreter (sys.version "3.14.0 (Ouros)", empty sys.path): importing
    # pandas/numpy in-sandbox is architecturally impossible. run_python
    # executes the given code on the HOST CPython (py -3.13) by design, so it
    # is NOT governed by the read/write/command allowlists above. This is not
    # an escalation beyond existing agent powers — the agent driving this
    # harness already has unrestricted Bash — but treat sandbox code that
    # calls run_python with the same trust as a shell command. Enforced
    # constraints: cwd pinned to the per-session work dir under the sandbox
    # output root (so relative savefig/to_csv land somewhere surfaced as
    # artifacts), at most one concurrent run, and timeout kills the whole
    # process tree (taskkill /T /F on Windows).
    "run_python": {
        "host_interpreter": "py -3.13 (fallback: the harness's own interpreter)",
        "max_concurrent": 1,
        "default_timeout_s": 60,
        "max_timeout_s": 600,
        "output_cap_bytes": 8192,
    },
}

# Root for sandbox writes and run_python work dirs. NOTE: "/tmp/..." is
# drive-relative on Windows — it resolves against the current drive, so with
# the harness running from C: this is C:\tmp\ouros-sandbox-output.
SANDBOX_OUTPUT_ROOT = Path("/tmp/ouros-sandbox-output").resolve()


def _apply_data_roots():
    """Extend read_allow with OUROS_DATA_ROOTS (env var or ~/.claude/.env).

    Value: os.pathsep-separated absolute directories. They are appended to
    read_allow only — read-only, never writable — and deny-by-default is
    preserved for every path not under an allowed root.
    """
    raw = os.environ.get("OUROS_DATA_ROOTS", "")
    for root in raw.split(os.pathsep):
        root = root.strip()
        if not root:
            continue
        if not os.path.isabs(root):
            print(
                f"Warning: OUROS_DATA_ROOTS entry ignored (not absolute): {root}",
                file=sys.stderr,
            )
            continue
        if root not in SECURITY_POLICY["read_allow"]:
            SECURITY_POLICY["read_allow"].append(root)


def _is_under(path, root):
    """True if resolved `path` is `root` or inside it (case-insensitive on Windows)."""
    p, r = os.path.normcase(str(path)), os.path.normcase(str(root))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives
        return False


def _effective_roots(allowlist):
    """Resolve allowlist entries. A root that is the home dir or a drive root
    (e.g. "." with cwd = home) grants too much: it is dropped, not widened."""
    home = os.path.normcase(str(Path.home().resolve()))
    roots = []
    for allowed in allowlist:
        r = Path(allowed).resolve()
        if os.path.normcase(str(r)) in (home, os.path.normcase(r.anchor)):
            continue
        roots.append(r)
    return roots


def _check_path_allowed(path, allowlist):
    """Check if a path (symlinks and '..' resolved) falls under an allowed directory."""
    resolved = Path(path).resolve()
    return any(_is_under(resolved, root) for root in _effective_roots(allowlist))


def _is_secret(path):
    """True if the resolved path is a credential file or under a credential dir."""
    import fnmatch

    resolved = Path(path).resolve()
    # NTFS alternate data streams: '.env:x' reads a stream of .env — match the base name
    name = resolved.name.lower().split(":", 1)[0]
    if name in (".env.example", ".env.sample", ".env.template"):
        return False
    if any(
        fnmatch.fnmatchcase(name, pat) for pat in SECURITY_POLICY["read_deny_names"]
    ):
        return True
    home = Path.home().resolve()
    return any(_is_under(resolved, home / d) for d in SECURITY_POLICY["read_deny_dirs"])


def _check_read_allowed(path):
    """(allowed, reason) for reading `path`: under read_allow and not a secret."""
    if _is_secret(path):
        return False, "a credential/secret file"
    if not _check_path_allowed(path, SECURITY_POLICY["read_allow"]):
        return False, "outside allowed directories"
    return True, ""


# Unquoted shell operators come out of shlex (punctuation_chars) as their own
# tokens; quoted ones stay inside an argument and are passed literally.
_SHELL_OPERATOR_CHARS = set("();<>|&")
# .cmd/.bat targets (e.g. npm) run under cmd.exe even with shell=False, which
# re-parses the command line, so no cmd metacharacter may reach them at all.
_CMD_META_CHARS = set('&|<>^%"()!')


def _parse_command(cmd):
    """Split a command (str or argv list) into argv without a shell.

    Backslash is NOT an escape character, so Windows paths and regexes like
    \\bfoo\\b survive; quotes group as usual. Returns (argv, error).
    """
    import shlex

    if isinstance(cmd, (list, tuple)):
        argv = [str(a) for a in cmd]
    else:
        if any(c in cmd for c in ("\n", "\r", "\x00")):
            return None, "newline/NUL in command"
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.escape = ""
        try:
            argv = list(lex)
        except ValueError as e:
            return None, f"unparseable command: {e}"
        for tok in argv:
            if tok and set(tok) <= _SHELL_OPERATOR_CHARS:
                return (
                    None,
                    f"shell operator '{tok}' not allowed (no chaining/redirection)",
                )
    if not argv:
        return None, "empty command"
    return argv, ""


def _exe_name(arg0):
    """Lower-case basename of argv[0] with .exe/.cmd/.bat stripped (in that order)."""
    exe = os.path.basename(arg0).lower()
    for ext in (".exe", ".cmd", ".bat"):
        if exe.endswith(ext):
            exe = exe[: -len(ext)]
    return exe


def _is_allowlisted(argv):
    """argv starts with one of the command_allow prefixes."""
    return any(
        tuple(argv[: len(allow)]) == allow for allow in SECURITY_POLICY["command_allow"]
    )


def _arg_refusal(arg):
    """Why one argument is refused ("" if it is fine).

    Allowed commands read whatever paths they are given (git diff --no-index,
    grep, git show rev:path): the read policy applies to path-like arguments.
    """
    if arg.split("=", 1)[0] in SECURITY_POLICY["command_deny_args"]:
        return f"argument '{arg}' not allowed"
    for cand in _path_candidates(arg):
        if _is_secret(cand):
            return f"argument '{arg}' names a credential/secret file"
        if os.path.exists(cand) and not _check_path_allowed(
            cand, SECURITY_POLICY["read_allow"]
        ):
            return f"argument '{arg}' is outside allowed directories"
    return ""


def _is_recursive_grep_flag(a):
    """-r/-R/--recursive/--dereference-recursive, or a short-flag cluster holding r/R."""
    return a in ("-r", "-R", "--recursive", "--dereference-recursive") or bool(
        a.startswith("-") and not a.startswith("--") and set(a[1:]) & {"r", "R"}
    )


def _with_secret_excludes(exe, argv):
    """Recursive search reads every file under a directory: keep secrets out of it."""
    if exe == "rg":
        return (
            argv[:1]
            + [f"--glob=!{p}" for p in SECURITY_POLICY["read_deny_names"]]
            + argv[1:]
        )
    if exe == "grep" and any(_is_recursive_grep_flag(a) for a in argv[1:]):
        return (
            argv[:1]
            + [f"--exclude={p}" for p in SECURITY_POLICY["read_deny_names"]]
            + [
                f"--exclude-dir={d.split('/')[-1]}"
                for d in SECURITY_POLICY["read_deny_dirs"]
            ]
            + argv[1:]
        )
    return argv


def _check_command_allowed(cmd):
    """Check a command against policy. Returns (allowed, reason, argv)."""
    raw = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
    raw_lower = raw.strip().lower()
    for deny in SECURITY_POLICY["command_deny"]:
        if deny in raw_lower:
            return False, f"blocked by deny rule: '{deny}'", None
    argv, err = _parse_command(cmd)
    if err:
        return False, err, None
    exe = _exe_name(argv[0])
    argv = [exe] + argv[1:]
    if not _is_allowlisted(argv):
        return False, "not in command allowlist", None
    for arg in argv[1:]:
        reason = _arg_refusal(arg)
        if reason:
            return False, reason, None
    return True, "", _with_secret_excludes(exe, argv)


def _path_candidates(arg):
    """Path-like readings of an argv token: the token itself (non-options), an
    option's '=value', and 'rev:path' / 'path:stream' segments (not drive letters)."""
    out = []
    if arg.startswith("-"):
        if "=" in arg:
            out.append(arg.split("=", 1)[1])
    else:
        out.append(arg)
    for c in list(out):
        body = (
            c[2:] if len(c) > 1 and c[1] == ":" else c
        )  # keep 'C:' drive prefix intact
        if ":" in body:
            out.extend(s for s in body.split(":") if s)
    return [c for c in out if c]


def _call_read_file(path):
    """Read a file from the host filesystem."""
    allowed, reason = _check_read_allowed(path)
    if not allowed:
        return {"error": f"read_file denied: '{path}' is {reason}"}
    try:
        return Path(path).read_text()
    except Exception as e:
        return {"error": f"read_file failed: {e}"}


def _call_write_file(path, content):
    """Write content to a file on the host filesystem.

    Binary-safe: accepts str (written as UTF-8) or bytes. The ouros bridge
    marshals sandbox bytes as {"$bytes": [ints]} — decoded back here so binary
    payloads (PNG, parquet, ...) round-trip intact.
    """
    if not _check_path_allowed(path, SECURITY_POLICY["write_allow"]):
        return {"error": f"write_file denied: '{path}' is outside allowed directories"}
    try:
        if isinstance(content, dict) and set(content.keys()) == {"$bytes"}:
            content = bytes(content["$bytes"])
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (bytes, bytearray)):
            p.write_bytes(bytes(content))
        else:
            p.write_text(str(content), encoding="utf-8")
        return {"ok": True, "path": str(p), "bytes": p.stat().st_size}
    except Exception as e:
        return {"error": f"write_file failed: {e}"}


def _call_glob_files(pattern, path="."):
    """Find files matching a glob pattern. The root and every match go through
    the read policy, so '..' patterns and secret files never come back."""
    import glob as g

    if not _check_path_allowed(path, SECURITY_POLICY["read_allow"]):
        return {"error": f"glob_files denied: '{path}' is outside allowed directories"}
    matches = sorted(g.glob(os.path.join(path, pattern), recursive=True))
    return [m for m in matches if _check_read_allowed(m)[0]][:500]


def _call_run_command(cmd, timeout=30):
    """Run an allowlisted command (str or argv list) WITHOUT a shell."""
    import shutil
    import subprocess

    allowed, reason, argv = _check_command_allowed(cmd)
    if not allowed:
        return {"error": f"run_command denied: {reason}"}
    if argv[0] == "echo":  # cmd builtin on Windows: answer in-process
        return {"stdout": " ".join(argv[1:]) + "\n", "stderr": "", "returncode": 0}
    exe = shutil.which(argv[0])
    if not exe:
        return {"error": f"run_command failed: '{argv[0]}' not found on PATH"}
    if exe.lower().endswith((".cmd", ".bat")) and any(
        c in _CMD_META_CHARS for a in argv[1:] for c in a
    ):
        return {
            "error": "run_command denied: cmd metacharacters in arguments to a .cmd/.bat target"
        }
    try:
        r = subprocess.run(
            [exe] + argv[1:],
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        result = {"stdout": r.stdout, "stderr": r.stderr, "returncode": r.returncode}
        return result
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"error": f"run_command failed: {e}"}


# --- run_python: host-CPython DS bridge -----------------------------------

_RUN_PYTHON_LOCK = threading.Lock()
# Per-session work dir for run_python, set by execute_in_sandbox each run.
_SESSION_WORK_DIR = None


def _host_python_cmd():
    """Interpreter for run_python: the Windows launcher pin 'py -3.13' when
    available (full DS stack), else the interpreter running this harness."""
    import shutil

    if shutil.which("py"):
        return ["py", "-3.13"]
    return [sys.executable]


def _kill_process_tree(proc):
    """Kill a subprocess and all its children."""
    import subprocess

    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        proc.kill()


def _call_run_python(code, timeout=60):
    """Execute Python on the HOST CPython and return combined output tail.

    The sandbox cannot import pandas/numpy (reimplemented interpreter, empty
    sys.path); run_python bridges to the real interpreter. See
    SECURITY_POLICY["run_python"] for the policy. cwd is the per-session work
    dir under SANDBOX_OUTPUT_ROOT so relative savefig()/to_csv() outputs are
    surfaced as artifacts. Output: stdout+stderr combined, last ~8KB with a
    '[truncated]' marker, exit code appended when nonzero.
    """
    if not isinstance(code, str) or not code.strip():
        return {"error": "run_python requires a non-empty code string"}
    timeout = _run_python_timeout(timeout)

    if not _RUN_PYTHON_LOCK.acquire(blocking=False):
        return {
            "error": "run_python denied: another run_python call is in "
            "progress (max_concurrent: 1)"
        }
    try:
        out, returncode, timed_out = _run_host_python(code, timeout)
        return _format_run_output(out, returncode, timed_out, timeout)
    except Exception as e:
        return {"error": f"run_python failed: {e}"}
    finally:
        _RUN_PYTHON_LOCK.release()


def _run_python_timeout(timeout):
    """Requested timeout as float, capped at max_timeout_s; the default if not numeric."""
    try:
        return min(float(timeout), SECURITY_POLICY["run_python"]["max_timeout_s"])
    except (TypeError, ValueError):
        return SECURITY_POLICY["run_python"]["default_timeout_s"]


def _run_host_python(code, timeout):
    """Run `code` on the host interpreter in the session work dir.

    Returns (output, returncode, timed_out); on timeout the process tree is
    killed and whatever it printed within a further 10s is kept.
    """
    import subprocess

    work_dir = _SESSION_WORK_DIR or (SANDBOX_OUTPUT_ROOT / "default")
    work_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        _host_python_cmd() + ["-c", code],
        cwd=str(work_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = ""
        return out or "", proc.returncode, True
    return out or "", proc.returncode, False


def _format_run_output(out, returncode, timed_out, timeout):
    """Cap output to its last output_cap_bytes chars, then append the timeout/exit marker."""
    cap = SECURITY_POLICY["run_python"]["output_cap_bytes"]
    if len(out) > cap:
        out = "[truncated]\n" + out[-cap:]
    if timed_out:
        out += f"\n[run_python timed out after {timeout:g}s — process tree killed]"
    elif returncode:
        out += f"\n[exit code: {returncode}]"
    return out


def _snapshot_output_dir():
    """Snapshot (path -> (mtime_ns, size)) of files under SANDBOX_OUTPUT_ROOT."""
    snap = {}
    if not SANDBOX_OUTPUT_ROOT.exists():
        return snap
    try:
        for i, p in enumerate(SANDBOX_OUTPUT_ROOT.rglob("*")):
            if i >= 5000:
                break
            if p.is_file():
                st = p.stat()
                snap[str(p)] = (st.st_mtime_ns, st.st_size)
    except OSError:
        pass
    return snap


def _diff_output_dir(before):
    """Absolute host paths of files new or changed since `before` snapshot."""
    after = _snapshot_output_dir()
    return sorted(path for path, sig in after.items() if before.get(path) != sig)


# Outbound endpoints llm_call may POST to: a fixed table, nothing is read from a
# file, argument or environment variable. On each request _post_json checks the
# table URL against the scheme/host/port allowlist below (https to the three
# provider hosts, plain http only to LM Studio on loopback:1234) and refuses any
# 30x reply instead of following it; the auth headers are also marked
# unredirected so urllib would never forward them.
LLM_ENDPOINTS = {
    "local": "http://localhost:1234/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
_LLM_HTTPS_HOSTS = frozenset({"api.anthropic.com", "api.openai.com", "openrouter.ai"})
_LLM_LOOPBACK_HTTP = frozenset({("localhost", 1234), ("127.0.0.1", 1234)})
_LLM_KEY_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


class UrlRefused(ValueError):
    """An outbound URL failed the scheme/host allowlist."""


def _check_url_allowed(url):
    """(allowed, reason) for an outbound llm_call URL: https to an allowlisted
    host on the default port, or http to LM Studio on loopback:1234. No userinfo."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        return False, f"unparseable URL ({e})"
    if parts.username is not None or parts.password is not None:
        return False, "credentials in URL"
    host = (parts.hostname or "").lower()
    check = _URL_SCHEME_CHECKS.get(parts.scheme)
    if check is None:
        return False, f"scheme '{parts.scheme}' not allowed (https only)"
    return check(host, port)


def _check_https_target(host, port):
    """(allowed, reason) for https: allowlisted host on the default port (443)."""
    if host not in _LLM_HTTPS_HOSTS:
        return False, f"host '{host}' not in allowlist"
    if port not in (None, 443):
        return False, f"port {port} not allowed"
    return True, ""


def _check_http_target(host, port):
    """(allowed, reason) for plain http: only LM Studio on loopback port 1234."""
    if (host, port) in _LLM_LOOPBACK_HTTP:
        return True, ""
    return False, "plain http is allowed only to localhost:1234"


# URL scheme -> (host, port) check; any other scheme is refused.
_URL_SCHEME_CHECKS = {"https": _check_https_target, "http": _check_http_target}


def _post_json(url, body, headers, timeout):
    """POST `body` as JSON to an allowlisted `url`; return the parsed reply.

    Raises UrlRefused before any request is built if the URL fails policy, and
    on any 30x reply (redirects are never followed)."""
    import urllib.request

    allowed, reason = _check_url_allowed(url)
    if not allowed:
        raise UrlRefused(f"llm_call refused URL {url!r}: {reason}")
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    for name, value in headers.items():
        # Unredirected headers are never copied to a redirected request.
        req.add_unredirected_header(name, value)
    return json.loads(_open_refusing_redirects(req, timeout))


def _open_refusing_redirects(req, timeout):
    """Body bytes of `req` via urllib.request.urlopen; any 30x raises UrlRefused.

    urlopen's opener follows 301/302/303 for POST. Its loop guard
    (HTTPRedirectHandler.http_error_302) raises HTTPError before following once
    req.redirect_dict holds max_redirections entries, so pre-filling it makes every
    redirect terminal without touching the process-wide opener. The loopback
    tests in test_ouros_policy.LlmRedirectTests pin this per CPython version."""
    import urllib.error
    import urllib.request

    limit = urllib.request.HTTPRedirectHandler.max_redirections
    req.redirect_dict = {f"redirects-refused:{i}": 0 for i in range(limit)}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if not 300 <= e.code < 400:
            raise
        target = e.headers.get("location") if e.headers else None
        e.close()
        raise UrlRefused(
            f"llm_call refused redirect {e.code} from {req.full_url!r} to {target!r}"
        ) from None


@dataclass(frozen=True)
class LlmCallOptions:
    """Sandbox llm_call() keywords after the prompt, in positional order."""

    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1000
    system: str | None = None
    temperature: float = 0.0
    backend: str = "anthropic"


def _call_llm(prompt, *args, **kwargs):
    """Call an LM as a sub-query. Returns the text response.

    Arguments after `prompt` (positional in this order, or keywords) are
    LlmCallOptions fields; an unknown keyword raises TypeError naming it.

    Args:
        prompt: The text prompt to send
        model: Model identifier. Anthropic: claude-haiku-4-5-20251001,
               claude-sonnet-4-6. LM Studio: whatever is loaded.
               OpenAI: gpt-4o-mini, gpt-5-nano, etc.
        max_tokens: Maximum response tokens (default 1000)
        system: Optional system prompt
        temperature: Sampling temperature (default 0.0 for deterministic)
        backend: Which provider to use:
                 'local' — LM Studio at localhost:1234
                 'anthropic' — Anthropic API (default)
                 'openai' — OpenAI API
                 'openrouter' — OpenRouter API

    Returns:
        str: The model's text response
    """
    o = LlmCallOptions(*args, **kwargs)
    backend = o.backend
    if backend not in LLM_ENDPOINTS:
        return {
            "error": f"Unknown backend: {backend}. Use 'local', 'anthropic', 'openai', or 'openrouter'."
        }
    headers, error = _llm_headers(backend)
    if error:
        return error

    body = {
        "model": o.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": o.max_tokens,
    }
    _apply_llm_sampling(body, backend, o.system, o.temperature)

    timeout = 60 if backend == "local" else 120
    try:
        resp = _post_json(LLM_ENDPOINTS[backend], body, headers, timeout)
    except UrlRefused as e:
        return {"error": str(e)}
    if backend == "anthropic":
        return resp["content"][0]["text"]
    return resp["choices"][0]["message"]["content"]


def _llm_headers(backend):
    """(auth headers, error) for `backend`; error is a dict when its API key is unset."""
    key_var = _LLM_KEY_VARS.get(backend)
    if not key_var:
        return {}, None
    api_key = os.environ.get(key_var)
    if not api_key:
        return {}, {"error": f"{key_var} not set"}
    if backend == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}, None
    return {"Authorization": f"Bearer {api_key}"}, None


def _apply_llm_sampling(body, backend, system, temperature):
    """Add system/temperature to `body`: Anthropic only when set/positive,
    other backends always send temperature and never a system prompt."""
    if backend != "anthropic":
        body["temperature"] = temperature
        return
    if system:
        body["system"] = system
    if temperature > 0:
        body["temperature"] = temperature


@dataclass(frozen=True)
class AgentCallOptions:
    """Sandbox agent_call() keywords after the prompt, in positional order."""

    agent: str = "claude-code"
    model: str | None = None
    max_turns: int | None = None
    timeout: float = 600
    cwd: str | None = None
    isolated: bool = False
    permission_mode: str = "default"


def _call_agent(prompt, *args, **kwargs):
    """Spawn a headless agent and return its output.

    Arguments after `prompt` (positional in this order, or keywords) are
    AgentCallOptions fields; an unknown keyword raises TypeError naming it.

    This is the RLM recursive call — a full agent with tool access runs
    autonomously and returns its final output. The agent can read files,
    edit code, run commands, and iterate.

    Supported agents:
    - claude-code: Claude Code CLI in headless mode (claude -p)
    - codex: OpenAI Codex CLI in exec mode (codex exec)

    Args:
        prompt: Task description for the agent
        agent: Which agent to spawn (default: claude-code)
        model: Model override. Claude Code: 'sonnet', 'opus', 'haiku'.
               Codex: 'o3', 'o4-mini', 'gpt-4.1', etc.
        max_turns: Max agent turns (default: None -> policy
                   agent_default_max_turns, 25). The agent exits with an
                   error at the limit; pass a larger value for long tasks.
        timeout: Max seconds to wait (default 600 = 10 min). This is the
                 real safety valve — if the agent hasn't finished, it's stuck.
        cwd: Working directory for the agent (default: current directory)
        isolated: If True, run in a git worktree (Claude Code only).
                  Safe for destructive tasks — if it fucks up, throw away
                  the worktree. No risk to your working tree.
        permission_mode: Permission mode for Claude Code (default: 'default').
                         'default' = agent can't do destructive ops without permission
                         'plan' = agent proposes changes, doesn't execute
                         'auto' = agent decides what needs permission
                         'bypassPermissions' = full autonomy (use with isolated=True)

    Returns:
        str: The agent's final text output
    """
    import subprocess

    o = AgentCallOptions(*args, **kwargs)
    agent, timeout = o.agent, o.timeout
    build = _AGENT_ARGV.get(agent)
    if build is None:
        return {"error": f"Unknown agent: {agent}. Supported: claude-code, codex"}
    cmd = build(prompt, o.model, o.max_turns, o.isolated, o.permission_mode)

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=o.cwd,
        )
    except subprocess.TimeoutExpired:
        return {
            "error": f"Agent timed out after {timeout}s. Task may be too complex or agent is stuck."
        }
    except FileNotFoundError:
        return {"error": f"Agent '{agent}' not found on PATH. Is it installed?"}
    return _agent_output(agent, r)


def _claude_argv(prompt, model, max_turns, isolated, permission_mode):
    """claude -p argv; --max-turns is always present (policy default when None)."""
    cmd = ["claude", "-p", prompt, "--output-format", "text"]
    if model:
        cmd.extend(["--model", model])
    if max_turns is None:
        max_turns = SECURITY_POLICY["agent_default_max_turns"]
    cmd.extend(["--max-turns", str(max_turns)])
    if isolated:
        cmd.append("--worktree")
    if permission_mode != "default":
        cmd.extend(["--permission-mode", permission_mode])
    return cmd


def _codex_argv(prompt, model, max_turns, isolated, permission_mode):
    """codex exec argv; max_turns/isolated/permission_mode do not apply to codex."""
    cmd = ["codex", "exec", prompt, "--json"]
    if model:
        cmd.extend(["-m", model])
    return cmd


# agent name -> argv builder(prompt, model, max_turns, isolated, permission_mode)
_AGENT_ARGV = {"claude-code": _claude_argv, "codex": _codex_argv}


def _codex_messages(output):
    """agent_message texts from Codex --json event lines (other lines skipped)."""
    messages = []
    for line in output.splitlines():
        try:
            item = json.loads(line).get("item", {})
            if item.get("type") == "agent_message":
                messages.append(item.get("text", ""))
        except (json.JSONDecodeError, AttributeError):
            continue
    return messages


def _agent_output(agent, r):
    """Final text of a finished agent run; stderr appended when it failed."""
    output = r.stdout.strip()
    # Parse Codex JSON output — extract agent_message text
    if agent == "codex" and output:
        messages = _codex_messages(output)
        if messages:
            output = "\n".join(messages)
    if r.returncode != 0 and r.stderr:
        output += f"\n[stderr]: {r.stderr.strip()}"
    return output


# function_name -> sync handler
EXTERNAL_FUNCTIONS = {
    "exa_search": _call_exa_search_sync,
    "nia_search": _call_nia_search_sync,
    "nia_universal": _call_nia_universal_sync,
    "nia_web": _call_nia_web_sync,
    "nia_package": _call_nia_package_sync,
    "nia_package_grep": _call_nia_package_grep_sync,
    "nia_help": _call_nia_help,
    "research_package": _call_research_package,
    "read_file": _call_read_file,
    "write_file": _call_write_file,
    "glob_files": _call_glob_files,
    "run_command": _call_run_command,
    "run_python": _call_run_python,
    "llm_call": _call_llm,
    "agent_call": _call_agent,
}


# ---------------------------------------------------------------------------
# Harness core
# ---------------------------------------------------------------------------


def _create_manager(storage_dir=None):
    """Create a SessionManager with optional persistence."""
    import ouros

    sm = ouros.SessionManager()
    if storage_dir:
        storage = Path(storage_dir)
        storage.mkdir(parents=True, exist_ok=True)
        sm.set_storage_dir(str(storage))
    return sm


def execute_in_sandbox(
    code, session_id=None, storage_dir=None, load_session=False, reset_session=False
):
    """Execute Python code in an ouros session with external function bridge.

    Uses Session API for state persistence. External function calls pause
    execution; the harness calls the real API and resumes. Variables persist
    across executions and can be saved/loaded from disk.

    Session semantics: a saved session with this name LOADS by default (as if
    --load); reset_session=True discards saved state and starts fresh (the old
    default). load_session=True still forces a load attempt.
    """
    import ouros

    global _SESSION_WORK_DIR
    sm = _create_manager(storage_dir)
    sid = session_id or "default"

    # Per-session work dir for run_python cwd and artifact surfacing
    _SESSION_WORK_DIR = SANDBOX_OUTPUT_ROOT / sid
    output_snapshot = _snapshot_output_dir()

    _open_session(sm, session_id, storage_dir, load_session, reset_session)
    session = ouros.Session(manager=sm, session_id=sid)
    result, stdout = _run_bridged(session, code)

    # Save session
    if session_id and storage_dir:
        try:
            sm.save_session(session_id=sid, name=session_id)
        except Exception:
            pass

    result = dict(result)
    result["stdout"] = stdout
    # New/changed files under the sandbox output root — absolute host paths
    result["artifacts"] = _diff_output_dir(output_snapshot)
    return result


def _has_saved_session(session_id, storage_dir):
    """True if {storage_dir}/{session_id}.bin exists (where saved sessions live)."""
    return bool(
        session_id
        and storage_dir
        and (Path(storage_dir) / f"{session_id}.bin").exists()
    )


def _open_session(sm, session_id, storage_dir, load_session, reset_session):
    """Load the saved session (default when one exists), else create or reset it."""
    ext_funcs = list(EXTERNAL_FUNCTIONS.keys())
    sid = session_id or "default"
    has_saved = _has_saved_session(session_id, storage_dir)
    if session_id and not reset_session and (load_session or has_saved):
        _load_saved_session(sm, session_id, ext_funcs)
    else:
        _fresh_session(sm, sid, ext_funcs)


def _load_saved_session(sm, session_id, ext_funcs):
    """Load `session_id` from storage; on failure warn on stderr and create it fresh."""
    try:
        sm.load_session(name=session_id, session_id=session_id)
        sm.register_external_functions(ext_funcs, session_id=session_id)
    except Exception as e:
        print(f"Warning: could not load session '{session_id}': {e}", file=sys.stderr)
        sm.create_session(session_id, external_functions=ext_funcs)


def _fresh_session(sm, sid, ext_funcs):
    """Create session `sid`, or reset it in place when the manager already has it."""
    existing = [s["id"] for s in sm.list_sessions()]
    if sid not in existing:
        sm.create_session(sid, external_functions=ext_funcs)
    else:
        sm.reset(session_id=sid, external_functions=ext_funcs)


def _run_bridged(session, code):
    """Execute `code`, serving each external-function pause; (final result, stdout).

    Each execute()/resume() returns only the stdout produced since the previous
    pause. Collect every segment; keeping only the last one silently drops
    anything printed before a later external-function call.
    """
    chunks = []

    def _collect(r):
        out = r.get("stdout")
        if out:
            chunks.append(out)
        return r

    # Execute — may pause at external function calls
    result = _collect(session.execute(code))

    # Pause/resume loop
    while not result.get("is_complete", True):
        progress = result.get("progress", {})
        if progress.get("status") != "function_call":
            break
        call_id = progress["call_id"]
        result = _collect(session.resume(call_id, _dispatch_external(progress)))
    return result, "".join(chunks)


def _dispatch_external(progress):
    """Call the real handler for a paused call; failures return {"error": ...}."""
    func_name = progress["function_name"]
    args = progress.get("args", [])
    kwargs = progress.get("kwargs", {})
    handler = EXTERNAL_FUNCTIONS.get(func_name)
    if not handler:
        return {"error": f"Unknown function: {func_name}"}
    try:
        return handler(*args, **kwargs)
    except Exception as e:
        return {"error": f"{func_name} failed: {e}"}


def list_variables(session_id, storage_dir):
    """List variables in a saved session."""
    import ouros

    sm = _create_manager(storage_dir)
    try:
        sm.load_session(name=session_id, session_id=session_id)
    except Exception as e:
        print(f"Error loading session '{session_id}': {e}", file=sys.stderr)
        return
    variables = sm.list_variables(session_id=session_id)
    for v in variables:
        print(f"  {v['name']}: {v['type_name']}")


def get_variable(session_id, var_name, storage_dir):
    """Get a variable from a saved session as JSON."""
    sm = _create_manager(storage_dir)
    try:
        sm.load_session(name=session_id, session_id=session_id)
    except Exception as e:
        print(f"Error loading session '{session_id}': {e}", file=sys.stderr)
        return
    val = sm.get_variable(var_name, session_id=session_id)
    print(json.dumps(val.get("json_value", val), indent=2))


def fork_session(source_id, new_id, storage_dir):
    """Fork a saved session into a new independent copy."""
    sm = _create_manager(storage_dir)
    try:
        sm.load_session(name=source_id, session_id=source_id)
    except Exception as e:
        print(f"Error loading session '{source_id}': {e}", file=sys.stderr)
        return
    sm.fork_session(source_id, new_id)
    if storage_dir:
        sm.save_session(session_id=new_id, name=new_id)
    print(f"Forked '{source_id}' -> '{new_id}'")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    """Parse the harness CLI: code source, session/storage, and session commands."""
    p = argparse.ArgumentParser(
        description="Ouros Harness — sandboxed code execution with external function bridge"
    )
    p.add_argument("--code", "-c", help="Python code to execute")
    p.add_argument("--file", "-f", help="Python file to execute")
    p.add_argument("--session", "-s", help="Session ID for persistence")
    p.add_argument(
        "--storage",
        default=None,
        help="Storage directory (default: thoughts/shared/dives)",
    )
    p.add_argument(
        "--load",
        action="store_true",
        help="Load a saved session before executing (now the default "
        "when a saved session exists; kept for compatibility)",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Discard saved session state and start fresh (the old "
        "default for an existing --session without --load)",
    )
    p.add_argument(
        "--list-vars", action="store_true", help="List variables in a session"
    )
    p.add_argument("--get-var", help="Get a variable as JSON")
    p.add_argument("--fork", help="Fork session into a new ID")
    return p.parse_args()


def main():
    """CLI entry: run a session command or execute code, print stdout and artifacts.

    Exits 1 on conflicting flags, a session command without --session, or no code.
    """
    _load_env()
    _apply_data_roots()
    args = parse_args()
    storage = args.storage or "thoughts/shared/dives"

    if args.reset and args.load:
        _fail("--reset and --load are mutually exclusive")

    if _run_session_command(args, storage):
        return

    result = execute_in_sandbox(
        code=_require_code(args),
        session_id=args.session,
        storage_dir=storage,
        load_session=args.load,
        reset_session=args.reset,
    )
    _print_result(result)


def _require_code(args):
    """Sandbox code from _read_code; exits 1 when no source provided any."""
    code = _read_code(args)
    if not code:
        _fail("provide code via --code, --file, or stdin")
    return code


def _print_result(result):
    """Print the run's stdout as-is, then new/changed artifact paths (absolute host paths)."""
    if result.get("stdout"):
        print(result["stdout"], end="")
    _print_artifacts(result.get("artifacts") or [])


def _fail(message):
    """Print 'Error: <message>' to stderr and exit with status 1."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _run_session_command(args, storage):
    """Run --list-vars, --get-var or --fork (first one given wins); True if one ran.

    Each requires --session; without it the CLI exits 1.
    """
    commands = (
        ("list_vars", "--list-vars", lambda: list_variables(args.session, storage)),
        (
            "get_var",
            "--get-var",
            lambda: get_variable(args.session, args.get_var, storage),
        ),
        ("fork", "--fork", lambda: fork_session(args.session, args.fork, storage)),
    )
    for attr, flag, run in commands:
        if getattr(args, attr):
            if not args.session:
                _fail(f"{flag} requires --session")
            run()
            return True
    return False


def _read_code(args):
    """Sandbox code from --code, else --file, else piped stdin; None if none given."""
    if args.code:
        return args.code
    if args.file:
        return Path(args.file).read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return None


def _print_artifacts(artifacts):
    """Print up to 10 artifact paths under an 'artifacts:' header, then a remainder count."""
    if not artifacts:
        return
    print("artifacts:")
    for a in artifacts[:10]:
        print(f"  {a}")
    if len(artifacts) > 10:
        print(f"  ... and {len(artifacts) - 10} more")


if __name__ == "__main__":
    main()
