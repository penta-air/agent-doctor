#!/usr/bin/env python3
"""agent-doctor scanner.

Read-only inventory of what AI coding agents (Claude Code, Codex) will load in
the current environment -- skills, commands, subagents, hooks, MCP servers and
instruction files -- plus deterministic checks for collisions and misfires.

Standard library only (Python 3.11+ for tomllib; TOML is skipped on older
versions with a note). Never writes anything. Secret-bearing fields of MCP
configs (env, headers) are reduced to key names before output.

Usage:
    python3 scan.py [--cwd DIR] [--home DIR] [--claude-home DIR]
                    [--codex-home DIR] [--managed-dir DIR]
                    [--format json|text] [--agents claude,codex]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import unicodedata
from itertools import combinations
from pathlib import Path

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

SEVERITY_ORDER = {"action": 0, "warning": 1, "info": 2}
SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__",
             "dist", "build", ".next", "target", ".cache"}
ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff\s]+")
# Variables that old blog posts used but hooks never receive (input is JSON on stdin).
BOGUS_HOOK_VARS = re.compile(
    r"\$\{?(CLAUDE_TOOL_INPUT\w*|CLAUDE_TOOL_OUTPUT\w*|CLAUDE_TOOL_NAME|CLAUDE_FILE_PATHS)\b")
CONTROL_EVENTS = {"PreToolUse", "PermissionRequest", "UserPromptSubmit", "Stop", "SubagentStop",
                  "PreCompact", "SessionStart"}
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"}
INTERPRETERS = {"bash", "sh", "zsh", "python", "python3", "node", "deno", "bun",
                "ruby", "perl", "pwsh", "powershell", "uv", "uvx", "npx"}


# --------------------------------------------------------------------------- utils

class Ctx:
    def __init__(self, a: argparse.Namespace):
        self.cwd = Path(a.cwd or os.getcwd()).resolve()
        self.home = Path(a.home or Path.home()).expanduser().resolve()
        self.claude_home = Path(a.claude_home or os.environ.get("CLAUDE_CONFIG_DIR")
                                or self.home / ".claude").expanduser().resolve()
        # ~/.claude.json sits next to ~/.claude unless CLAUDE_CONFIG_DIR is set.
        if a.claude_home or os.environ.get("CLAUDE_CONFIG_DIR"):
            self.claude_json = self.claude_home / ".claude.json"
        else:
            self.claude_json = self.home / ".claude.json"
        if not self.claude_json.is_file() and (self.home / ".claude.json").is_file():
            self.claude_json = self.home / ".claude.json"
        self.codex_home = Path(a.codex_home or os.environ.get("CODEX_HOME")
                               or self.home / ".codex").expanduser().resolve()
        self.managed_dir = Path(a.managed_dir).expanduser() if a.managed_dir else default_managed_dir()
        self.root = find_repo_root(self.cwd)
        self.agents = {x.strip() for x in a.agents.split(",") if x.strip()}
        self.format = a.format
        self.notes: list[str] = []
        self.inventory = {"skills": [], "commands": [], "agents": [], "hooks": [],
                          "mcp": [], "instructions": [], "plugins": []}
        self.findings: list[dict] = []
        self.team_plugins: set[str] = set()
        self.hook_pairs: list[tuple] = []
        self.similar_pairs: list[tuple] = []
        self.team_repo = False

    def disp(self, p: Path | str) -> str:
        """Display a path relative to home or repo for readability."""
        p = Path(p)
        for base, label in ((self.root, "<repo>"), (self.home, "~")):
            try:
                rel = p.relative_to(base).as_posix()
                return label if rel == "." else f"{label}/{rel}"
            except ValueError:
                continue
        return str(p)

    def add(self, sev: str, code: str, title: str, detail: str,
            locations: list[str] | None = None, fix: str | None = None):
        self.findings.append({"severity": sev, "code": code, "title": title,
                              "detail": detail, "locations": locations or [],
                              "suggestion": fix or ""})


def default_managed_dir() -> Path:
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if os.name == "nt":
        return Path(r"C:\Program Files\ClaudeCode")
    return Path("/etc/claude-code")


def find_repo_root(cwd: Path) -> Path:
    for d in (cwd, *cwd.parents):
        if (d / ".git").exists():
            return d
    return cwd


def dirs_root_to_cwd(root: Path, cwd: Path) -> list[Path]:
    out = [cwd]
    for d in cwd.parents:
        if root != cwd and (d == root or root in d.parents):
            out.append(d)
        if d == root:
            break
    if root not in out:
        out.append(root)
    return list(reversed(out))


def norm_name(s: str) -> str:
    return ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", s)).casefold()


def read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_json(ctx: Ctx, p: Path):
    txt = read_text(p)
    if txt is None:
        return None
    try:
        return json.loads(txt)
    except json.JSONDecodeError as e:
        ctx.add("action", "CONFIG_PARSE_ERROR", f"Invalid JSON: {ctx.disp(p)}",
                f"{e}. Everything defined in this file (hooks, MCP, permissions) is ignored.",
                [ctx.disp(p)], "Fix the JSON syntax.")
        return None


def read_toml(ctx: Ctx, p: Path):
    if not p.is_file():
        return None
    if tomllib is None:
        ctx.notes.append(f"Python < 3.11: skipped TOML file {ctx.disp(p)}")
        return None
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        ctx.add("action", "CONFIG_PARSE_ERROR", f"Invalid TOML: {ctx.disp(p)}", str(e),
                [ctx.disp(p)], "Fix the TOML syntax.")
        return None


def parse_frontmatter(text: str) -> tuple[dict, bool]:
    """Tiny YAML-frontmatter reader: top-level scalar keys and block scalars."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, False
    data: dict = {}
    key = None
    buf: list[str] = []

    def flush():
        if key is not None and buf:
            data[key] = " ".join(x.strip() for x in buf).strip()

    for line in lines[1:]:
        if line.strip() == "---":
            flush()
            return data, True
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            flush()
            key, val = m.group(1), m.group(2).strip()
            buf = []
            if val in ("|", ">", "|-", ">-", "|+", ">+", ""):
                data[key] = ""
            else:
                data[key] = val.strip("'\"")
                key = None
        elif key is not None:
            buf.append(line)
    return data, False


def walk_files(base: Path, name: str, max_depth: int):
    """Yield files called `name` under base (following symlinks, bounded depth)."""
    if not base.is_dir():
        return
    seen: set[str] = set()
    stack = [(base, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            real = os.path.realpath(d)
            if real in seen:
                continue
            seen.add(real)
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.name == name and e.is_file():
                yield e
            elif e.is_dir() and depth < max_depth and e.name not in SKIP_DIRS:
                stack.append((e, depth + 1))


# --------------------------------------------------------------------------- skills

def folder_hash(d: Path) -> str:
    """Hash of every file in a skill folder (relative path + bytes), to tell copies from variants."""
    h = hashlib.sha256()
    try:
        files = sorted(f for f in d.rglob("*") if f.is_file() and "__pycache__" not in f.parts)
    except OSError:
        return ""
    for f in files:
        h.update(f.relative_to(d).as_posix().encode() + b"\0")
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
        h.update(b"\0")
    return h.hexdigest()


TEAM_SCOPES = {"project", "project-nested", "repo", "repo-subdir"}
PERSONAL_SCOPES = {"personal", "account-synced", "user", "local"}


def owner_of(scope: str, team_plugins: set[str] | None = None) -> str:
    """team = shared through the repository; personal = this machine/account only; org = managed."""
    if scope.startswith("plugin:") or scope == "plugin":
        return "team" if team_plugins is not None and scope.split(":", 1)[-1] in team_plugins else "personal"
    if scope in TEAM_SCOPES:
        return "team"
    if scope in ("managed", "system"):
        return "org"
    return "personal"


def add_skill(ctx: Ctx, agent: str, scope: str, skill_md: Path, origin: str | None = None,
              enabled: bool | None = True):
    text = read_text(skill_md) or ""
    fm, ok = parse_frontmatter(text)
    folder = skill_md.parent.name
    entry = {
        "agent": agent, "scope": scope, "origin": origin,
        "name": fm.get("name") or folder, "folder": folder,
        "description": fm.get("description", ""),
        "path": ctx.disp(skill_md), "realpath": os.path.realpath(skill_md.parent),
        "enabled": enabled,
        "user_only": fm.get("disable-model-invocation", "").lower() == "true",
        "has_hooks": "hooks" in fm,
        "content_hash": folder_hash(skill_md.parent)[:16],
    }
    ctx.inventory["skills"].append(entry)
    loc = [ctx.disp(skill_md)]
    if not ok:
        ctx.add("action", "SKILL_NO_FRONTMATTER", f"Skill without valid frontmatter: {folder}",
                "SKILL.md must start with a '---' YAML block; the skill may not load.", loc,
                "Add frontmatter with name and description.")
    elif not fm.get("description"):
        ctx.add("warning", "SKILL_NO_DESCRIPTION", f"Skill without description: {entry['name']}",
                "Without a description the model has nothing to decide when to use it.", loc,
                "Add a description that says what it does and when to use it.")
    if ok and fm.get("name") and norm_name(fm["name"]) != norm_name(folder):
        ctx.add("info", "SKILL_NAME_FOLDER_MISMATCH",
                f"Skill name '{fm['name']}' differs from folder '{folder}'",
                "Some agents use the folder name, others the frontmatter name, so the "
                "invocation name can differ between agents.", loc,
                "Make the folder name and the name field identical.")


def scan_markdown_dir(ctx: Ctx, kind: str, agent: str, scope: str, d: Path,
                      origin: str | None = None, enabled: bool | None = True):
    if not d.is_dir():
        return
    for f in sorted(d.rglob("*.md")):
        fm, _ = parse_frontmatter(read_text(f) or "")
        rel = f.relative_to(d).with_suffix("")
        ctx.inventory[kind].append({
            "agent": agent, "scope": scope, "origin": origin, "enabled": enabled,
            "name": fm.get("name") or rel.as_posix().replace("/", ":"),
            "description": fm.get("description", ""), "path": ctx.disp(f),
            "has_hooks": "hooks" in fm,
        })


def scan_claude_skills(ctx: Ctx):
    ch = ctx.claude_home
    for d in (ctx.managed_dir / ".claude" / "skills", ctx.managed_dir / "skills"):
        for f in walk_files(d, "SKILL.md", 3):
            add_skill(ctx, "claude", "managed", f)
    for f in walk_files(ch / "skills", "SKILL.md", 4):
        scope = "account-synced" if "synced" in f.relative_to(ch / "skills").parts[:1] else "personal"
        add_skill(ctx, "claude", scope, f)
    # project: every .claude/skills from repo root to cwd, plus nested ones below cwd
    seen = set()
    for d in dirs_root_to_cwd(ctx.root, ctx.cwd):
        p = d / ".claude" / "skills"
        seen.add(p)
        for f in walk_files(p, "SKILL.md", 3):
            add_skill(ctx, "claude", "project", f)
    for f in walk_files(ctx.cwd, "SKILL.md", 7):
        parts = f.parts
        if ".claude" in parts:
            i = len(parts) - 1 - parts[::-1].index(".claude")
            base = Path(*parts[: i + 1]) / "skills"
            if base not in seen and base.parent.parent != ctx.home and parts[i + 1:i + 2] == ("skills",):
                add_skill(ctx, "claude", "project-nested", f)
    scan_markdown_dir(ctx, "commands", "claude", "personal", ch / "commands")
    scan_markdown_dir(ctx, "commands", "claude", "project", ctx.root / ".claude" / "commands")
    scan_markdown_dir(ctx, "agents", "claude", "personal", ch / "agents")
    scan_markdown_dir(ctx, "agents", "claude", "project", ctx.root / ".claude" / "agents")


def scan_codex_skills(ctx: Ctx):
    for d in reversed(dirs_root_to_cwd(ctx.root, ctx.cwd)):
        for f in walk_files(d / ".agents" / "skills", "SKILL.md", 3):
            add_skill(ctx, "codex", "repo" if d == ctx.root else "repo-subdir", f)
    for f in walk_files(ctx.home / ".agents" / "skills", "SKILL.md", 3):
        add_skill(ctx, "codex", "user", f)
    for f in walk_files(ctx.codex_home / "skills", "SKILL.md", 3):
        add_skill(ctx, "codex", "user", f)
    for f in walk_files(Path("/etc/codex/skills"), "SKILL.md", 3):
        add_skill(ctx, "codex", "system", f)


# --------------------------------------------------------------------------- settings / plugins

def claude_settings_files(ctx: Ctx) -> list[tuple[str, Path]]:
    return [
        ("managed", ctx.managed_dir / "managed-settings.json"),
        ("user", ctx.claude_home / "settings.json"),
        ("project", ctx.root / ".claude" / "settings.json"),
        ("local", ctx.root / ".claude" / "settings.local.json"),
    ]


def load_claude_settings(ctx: Ctx) -> list[tuple[str, Path, dict]]:
    out = []
    for scope, p in claude_settings_files(ctx):
        if p.is_file():
            data = read_json(ctx, p)
            if isinstance(data, dict):
                out.append((scope, p, data))
    return out


def plugin_enabled_map(settings) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for scope in ("user", "project", "local", "managed"):  # later wins
        for s, _, data in settings:
            if s == scope and isinstance(data.get("enabledPlugins"), dict):
                for k, v in data["enabledPlugins"].items():
                    result[k] = bool(v)
    return result


def discover_plugins(ctx: Ctx, settings) -> list[dict]:
    enabled = plugin_enabled_map(settings)
    ctx.team_plugins = set()
    for scope, _, data in settings:
        if scope == "project" and isinstance(data.get("enabledPlugins"), dict):
            ctx.team_plugins |= {k.split("@")[0] for k, v in data["enabledPlugins"].items() if v}
    plugins: list[dict] = []
    seen: set[str] = set()
    inst = ctx.claude_home / "plugins" / "installed_plugins.json"
    data = read_json(ctx, inst) if inst.is_file() else None
    if isinstance(data, dict) and isinstance(data.get("plugins"), dict):
        for key, recs in data["plugins"].items():
            for rec in (recs if isinstance(recs, list) else [recs]):
                if isinstance(rec, dict) and rec.get("installPath"):
                    path = Path(rec["installPath"]).expanduser()
                    if str(path) not in seen:
                        seen.add(str(path))
                        plugins.append({"key": key, "path": path})
    cache = ctx.claude_home / "plugins" / "cache"
    if cache.is_dir():
        for pj in cache.glob("*/*/*/.claude-plugin/plugin.json"):
            path = pj.parent.parent
            if str(path) not in seen:
                seen.add(str(path))
                plugins.append({"key": f"{path.parent.name}@{path.parent.parent.name}", "path": path})
    for p in plugins:
        manifest = read_json(ctx, p["path"] / ".claude-plugin" / "plugin.json") or {}
        p["manifest"] = manifest if isinstance(manifest, dict) else {}
        p["name"] = p["manifest"].get("name") or p["key"].split("@")[0]
        p["enabled"] = enabled.get(p["key"])  # None = unknown
        p["owner"] = "team" if p["name"] in ctx.team_plugins or p["key"].split("@")[0] in ctx.team_plugins else "personal"
        ctx.inventory["plugins"].append({"key": p["key"], "name": p["name"], "owner": p["owner"],
                                         "path": ctx.disp(p["path"]), "enabled": p["enabled"]})
    return plugins


def as_paths(base: Path, value) -> list[Path]:
    if isinstance(value, str):
        return [base / value]
    if isinstance(value, list):
        return [base / v for v in value if isinstance(v, str)]
    return []


def scan_plugin_components(ctx: Ctx, plugins: list[dict]) -> list[tuple]:
    hook_sources = []
    for p in plugins:
        base, m, name, en = p["path"], p["manifest"], p["name"], p["enabled"]
        if en is False:
            continue
        skill_dirs = [base / "skills"] + as_paths(base, m.get("skills"))
        for d in skill_dirs:
            for f in walk_files(d, "SKILL.md", 3):
                add_skill(ctx, "claude", "plugin", f, origin=name, enabled=en)
        for d in [base / "commands"] + as_paths(base, m.get("commands")):
            scan_markdown_dir(ctx, "commands", "claude", "plugin", d, origin=name, enabled=en)
        for d in [base / "agents"] + as_paths(base, m.get("agents")):
            scan_markdown_dir(ctx, "agents", "claude", "plugin", d, origin=name, enabled=en)
        hooks_files = [base / "hooks" / "hooks.json"] + as_paths(base, m.get("hooks"))
        for hf in hooks_files:
            if hf.is_file():
                d = read_json(ctx, hf)
                if isinstance(d, dict):
                    hook_sources.append(("claude", f"plugin:{name}", hf, d.get("hooks", d), base))
        if isinstance(m.get("hooks"), dict):
            hook_sources.append(("claude", f"plugin:{name}", base / ".claude-plugin" / "plugin.json",
                                 m["hooks"].get("hooks", m["hooks"]), base))
        mcp_file = base / ".mcp.json"
        if mcp_file.is_file():
            d = read_json(ctx, mcp_file)
            if isinstance(d, dict):
                add_mcp_servers(ctx, "claude", f"plugin:{name}", mcp_file, d.get("mcpServers", d))
        if isinstance(m.get("mcpServers"), dict):
            add_mcp_servers(ctx, "claude", f"plugin:{name}", base / ".claude-plugin" / "plugin.json",
                            m["mcpServers"])
    return hook_sources


# --------------------------------------------------------------------------- hooks

def add_hooks(ctx: Ctx, agent: str, source: str, path: Path, hooks_obj, plugin_root: Path | None):
    if not isinstance(hooks_obj, dict):
        return
    for event, groups in hooks_obj.items():
        if not isinstance(groups, list):
            continue
        for g in groups:
            if not isinstance(g, dict):
                continue
            matcher = g.get("matcher")
            if isinstance(matcher, list):
                ctx.add("action", "HOOK_MATCHER_ARRAY", f"{event} matcher is an array ({source})",
                        "Matchers must be a single string; an array makes the entry invalid "
                        "(for PreToolUse it can disable the whole file's hooks).",
                        [ctx.disp(path)], 'Use one string such as "Edit|Write".')
                matcher = "|".join(map(str, matcher))
                bad_matcher = True
            else:
                bad_matcher = False
            for h in g.get("hooks", []) if isinstance(g.get("hooks"), list) else []:
                if not isinstance(h, dict):
                    continue
                ctx.inventory["hooks"].append({
                    "agent": agent, "source": source, "file": ctx.disp(path), "event": event,
                    "matcher": matcher if matcher is not None else "",
                    "type": h.get("type", "command"),
                    "command": h.get("command") or h.get("url") or h.get("prompt") or "",
                    "owner": owner_of(source, ctx.team_plugins),
                    "_abs": str(path), "_issues": ["matcher が配列（無効な設定）"] if bad_matcher else [],
                    "_plugin_root": str(plugin_root) if plugin_root else None,
                })


def scan_hooks(ctx: Ctx, settings, plugin_hook_sources):
    if "claude" in ctx.agents:
        for scope, p, data in settings:
            if data.get("disableAllHooks"):
                ctx.add("info", "HOOKS_DISABLED", f"disableAllHooks is set in {scope} settings",
                        "No hooks will run while this is set.", [ctx.disp(p)])
            add_hooks(ctx, "claude", scope, p, data.get("hooks"), None)
        for agent, src, p, obj, root in plugin_hook_sources:
            add_hooks(ctx, agent, src, p, obj, root)
    if "codex" in ctx.agents:
        for scope, base in (("user", ctx.codex_home), ("repo", ctx.root / ".codex")):
            hf = base / "hooks.json"
            if hf.is_file():
                d = read_json(ctx, hf)
                if isinstance(d, dict):
                    add_hooks(ctx, "codex", scope, hf, d.get("hooks", d), None)
            cfg = read_toml(ctx, base / "config.toml")
            if isinstance(cfg, dict) and isinstance(cfg.get("hooks"), dict):
                add_hooks(ctx, "codex", scope, base / "config.toml", cfg["hooks"], None)


def matcher_tokens(m: str):
    m = (m or "").strip()
    if m in ("", "*", ".*"):
        return None  # matches everything
    return {t.strip() for t in re.split(r"[|,]", m) if t.strip()}


def matchers_overlap(a: str, b: str) -> bool:
    ta, tb = matcher_tokens(a), matcher_tokens(b)
    if ta is None or tb is None:
        return True
    if ta & tb:
        return True
    rx = re.compile(r"[.^$\[\]()*+?\\]")
    for x, ys in ((ta, tb), (tb, ta)):
        for t in x:
            if rx.search(t):
                for y in ys:
                    try:
                        if re.fullmatch(t, y):
                            return True
                    except re.error:
                        pass
    return False


def resolve_script(ctx: Ctx, cmd: str, plugin_root: str | None):
    """Return (path, invoked_directly) of the script a hook command runs, if any."""
    expanded = cmd
    for var, val in (("CLAUDE_PLUGIN_ROOT", plugin_root), ("CLAUDE_PROJECT_DIR", str(ctx.root)),
                     ("HOME", str(ctx.home))):
        if val:
            expanded = re.sub(r"\$\{?" + var + r"\}?", val, expanded)
    expanded = re.sub(r"^~(?=/)", str(ctx.home), expanded)
    try:
        tokens = shlex.split(expanded)
    except ValueError:
        return None, False
    if not tokens or "$" in tokens[0]:
        return None, False
    direct = True
    first = tokens[0]
    if Path(first).name in INTERPRETERS and len(tokens) > 1:
        rest = [t for t in tokens[1:] if not t.startswith("-")]
        if not rest or first.endswith(("npx", "uvx")) or tokens[1] in ("-c", "run"):
            return None, False
        first, direct = rest[0], False
    if "/" not in first or "$" in first:
        return None, False
    p = Path(first)
    if not p.is_absolute():
        p = ctx.root / p
    return p, direct


def check_hooks(ctx: Ctx):
    hooks = ctx.inventory["hooks"]
    for h in hooks:
        loc = [h["file"]]
        cmd = h["command"]
        if h["type"] == "command":
            stripped = "\n".join(l for l in cmd.splitlines() if not l.strip().startswith("#")).strip()
            if not stripped:
                ctx.add("warning", "HOOK_EMPTY_COMMAND",
                        f"{h['event']} hook does nothing ({h['source']})",
                        "The command is empty or only comments, so the hook runs a no-op on every match.",
                        loc, "Fill in the command or remove the entry.")
                h["_issues"].append("コマンドが空（コメントのみ）")
                continue
            m = BOGUS_HOOK_VARS.search(cmd)
            if m:
                ctx.add("action", "HOOK_UNDEFINED_INPUT_VAR",
                        f"{h['event']} hook reads ${m.group(1)}, which is never set ({h['source']})",
                        "Hooks receive the tool input as JSON on stdin; this variable is empty, so the "
                        "hook probably never sees the command/file it is meant to check (a silent dead guard).",
                        loc, "Read stdin, e.g. `jq -r '.tool_input.command'`.")
                h["_issues"].append(f"設定されない変数 ${m.group(1)} を読んでいる（入力は stdin の JSON）")
            if h["agent"] == "claude" and h["source"] == "user" and "CLAUDE_PROJECT_DIR" in cmd:
                ctx.add("warning", "HOOK_USER_PROJECT_PATH",
                        f"User-level {h['event']} hook runs a script inside each project ({h['source']})",
                        "It is active in every repository but the script only exists in some, so it errors "
                        "(or runs a different script) elsewhere.", loc,
                        "Move the hook to that project's .claude/settings.json, or put the script under ~/.claude.")
            if h["event"] == "PreToolUse" and re.search(r'decision\W{0,6}:\W{0,6}(block|approve)', cmd):
                ctx.add("info", "HOOK_LEGACY_DECISION",
                        f"PreToolUse hook uses the legacy decision:block/approve output ({h['source']})",
                        "The current form is hookSpecificOutput.permissionDecision (allow/deny/ask) or exit code 2.",
                        loc, "Switch to permissionDecision or exit 2 with a reason on stderr.")
            script, direct = resolve_script(ctx, cmd, h["_plugin_root"])
            if script is not None:
                if not script.exists():
                    ctx.add("action", "HOOK_SCRIPT_MISSING",
                            f"{h['event']} hook script not found: {ctx.disp(script)} ({h['source']})",
                            "The hook will fail every time it fires.", loc,
                            "Fix the path or install the script.")
                    h["_issues"].append(f"スクリプトが存在しない（{ctx.disp(script)}）")
                elif direct and not os.access(script, os.X_OK):
                    ctx.add("warning", "HOOK_SCRIPT_NOT_EXECUTABLE",
                            f"{h['event']} hook script is not executable: {ctx.disp(script)}",
                            "It is invoked directly, so it fails without the execute bit.", loc,
                            "chmod +x the script, or call it through an interpreter.")
                    h["_issues"].append("スクリプトに実行権限が無い")
        if h["event"] in TOOL_EVENTS:
            mt = matcher_tokens(h["matcher"])
            if mt is None:
                ctx.add("info", "HOOK_BROAD_MATCHER",
                        f"{h['event']} hook matches every tool ({h['source']})",
                        "It runs on every tool call, which costs time and may affect unrelated tools.",
                        loc, "Narrow the matcher if it only concerns specific tools.")
            else:
                low = [t for t in mt if re.fullmatch(r"[a-z]+", t) and t not in ("mcp",)]
                if low:
                    ctx.add("warning", "HOOK_MATCHER_CASE",
                            f"{h['event']} matcher uses lowercase tool name(s) {low} ({h['source']})",
                            "Tool matching is case-sensitive (Bash, Edit, Write ...), so this never matches.",
                            loc, "Capitalise the tool names.")
                    h["_issues"].append(f"matcher のツール名が小文字（{', '.join(low)}）で一致しない")
    # overlaps across different sources of the same agent
    reported = set()
    for a, b in combinations(hooks, 2):
        if a["agent"] != b["agent"] or a["event"] != b["event"] or a["file"] == b["file"]:
            continue
        if not matchers_overlap(a["matcher"], b["matcher"]):
            continue
        same = " ".join(a["command"].split()) == " ".join(b["command"].split())
        key = (a["agent"], a["event"], tuple(sorted((a["file"], b["file"]))), same)
        if key in reported:
            continue
        reported.add(key)
        ctx.hook_pairs.append((a, b, same))
        if same:
            ctx.add("action", "HOOK_DUPLICATE",
                    f"Same {a['event']} hook registered twice ({a['source']} and {b['source']})",
                    "Identical commands from two sources both run, so the action fires twice.",
                    [a["file"], b["file"]], "Keep only one of them (often a copy of a plugin hook).")
        else:
            ctx.add("warning", "HOOK_MULTI_SOURCE",
                    f"{a['event']} hooks from {a['source']} ({a['owner']}) and {b['source']} ({b['owner']}) "
                    "fire on the same trigger",
                    f"Matchers '{a['matcher'] or '*'}' and '{b['matcher'] or '*'}' overlap; both run for "
                    "the same event, and either one can block or alter the result.",
                    [a["file"], b["file"]], "Confirm both are intended; remove or narrow one if not.")
    # personal hooks running inside a repository that has its own shared agent setup
    team_repo = any((ctx.root / d).exists() for d in (".claude", ".agents", ".codex", "CLAUDE.md", "AGENTS.md"))
    ctx.team_repo = team_repo
    if team_repo:
        by_src: dict[tuple, list] = {}
        for h in hooks:
            if h["owner"] == "personal":
                by_src.setdefault((h["agent"], h["source"], h["file"]), []).append(h)
        for (agent, src, file), hs in by_src.items():
            events = sorted({f"{h['event']}" + (f"({h['matcher']})" if h["matcher"] else "") for h in hs})
            controlling = sorted({h["event"] for h in hs} & CONTROL_EVENTS)
            ctx.add("warning", "PERSONAL_HOOKS_IN_TEAM_REPO",
                    f"{agent}: {len(hs)} personal hook(s) from {src} also run in this repository",
                    "These come from this machine's own settings, not from the repository, so teammates do not "
                    f"have them: {', '.join(events)}."
                    + (f" {', '.join(controlling)} can block or redirect the agent, so results here may differ "
                       "from the team's." if controlling else ""),
                    [file] + [f"{h['event']}: {h['command'][:120]}" for h in hs][:6],
                    "If they are not meant for this repository, move them to the projects that need them, "
                    "or check that they do not conflict with the team's conventions.")


# --------------------------------------------------------------------------- MCP

def server_identity(cfg: dict) -> str | None:
    if not isinstance(cfg, dict):
        return None
    if cfg.get("url"):
        return "url:" + str(cfg["url"]).rstrip("/")
    if cfg.get("command"):
        args = cfg.get("args") or []
        return "cmd:" + " ".join([str(cfg["command"])] + [str(x) for x in args])
    return None


SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|AUTH|CREDENTIAL|PRIVATE)", re.I)


def literal_secret_keys(cfg: dict) -> list[str]:
    keys = []
    for field in ("env", "headers"):
        d = cfg.get(field)
        if isinstance(d, dict):
            for k, v in d.items():
                if (isinstance(v, str) and SECRET_KEY.search(k) and len(v) >= 8
                        and "$" not in v and not v.startswith("<")):
                    keys.append(f"{field}.{k}")
    return keys


def add_mcp_servers(ctx: Ctx, agent: str, scope: str, path: Path, servers):
    if not isinstance(servers, dict):
        return
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        leaked = literal_secret_keys(cfg)
        if leaked:
            ctx.add("warning", "MCP_PLAINTEXT_SECRET",
                    f"MCP server '{name}' ({scope}) has a secret written in plain text: {leaked}",
                    "The value is stored in the config file itself (value not shown here). It is easy to "
                    "leak through backups, screen sharing or a committed file.",
                    [ctx.disp(path)], "Reference an environment variable instead (e.g. \"${GITHUB_TOKEN}\") "
                    "and rotate the token if the file was ever shared.")
        ctx.inventory["mcp"].append({
            "agent": agent, "scope": scope, "name": name, "file": ctx.disp(path),
            "transport": cfg.get("type") or ("http" if cfg.get("url") else "stdio"),
            "identity": server_identity(cfg),
            "env_keys": sorted((cfg.get("env") or {}).keys()) if isinstance(cfg.get("env"), dict) else [],
            "header_keys": sorted((cfg.get("headers") or {}).keys()) if isinstance(cfg.get("headers"), dict) else [],
            "disabled": bool(cfg.get("disabled") or cfg.get("enabled") is False),
            "owner": owner_of(scope, ctx.team_plugins), "_abs": str(path),
        })


def scan_mcp(ctx: Ctx):
    if "claude" in ctx.agents:
        mm = ctx.managed_dir / "managed-mcp.json"
        if mm.is_file():
            d = read_json(ctx, mm)
            if isinstance(d, dict):
                add_mcp_servers(ctx, "claude", "managed", mm, d.get("mcpServers", d))
                ctx.add("info", "MCP_MANAGED", "Organisation-managed MCP config is present",
                        "managed-mcp.json may restrict which other MCP servers can be used.", [ctx.disp(mm)])
        cj = read_json(ctx, ctx.claude_json) if ctx.claude_json.is_file() else None
        if isinstance(cj, dict):
            add_mcp_servers(ctx, "claude", "user", ctx.claude_json, cj.get("mcpServers"))
            projects = cj.get("projects") if isinstance(cj.get("projects"), dict) else {}
            for key in {str(ctx.root), str(ctx.cwd)}:
                proj = projects.get(key)
                if isinstance(proj, dict):
                    add_mcp_servers(ctx, "claude", "local", ctx.claude_json, proj.get("mcpServers"))
        pm = ctx.root / ".mcp.json"
        if pm.is_file():
            d = read_json(ctx, pm)
            if isinstance(d, dict):
                add_mcp_servers(ctx, "claude", "project", pm, d.get("mcpServers"))
                if "servers" in d and "mcpServers" not in d:
                    ctx.add("action", "MCP_WRONG_KEY", ".mcp.json uses 'servers' instead of 'mcpServers'",
                            "Claude Code ignores this file's servers.", [ctx.disp(pm)],
                            "Rename the key to mcpServers.")
        if (ctx.root / ".claude" / ".mcp.json").is_file():
            ctx.add("warning", "MCP_WRONG_LOCATION", ".mcp.json is inside .claude/",
                    "Project MCP config is only read from the repository root.",
                    [ctx.disp(ctx.root / ".claude" / ".mcp.json")], "Move it to the repo root.")
    if "codex" in ctx.agents:
        for scope, p in (("system", Path("/etc/codex/config.toml")),
                         ("user", ctx.codex_home / "config.toml"),
                         *[("repo", d / ".codex" / "config.toml")
                           for d in dirs_root_to_cwd(ctx.root, ctx.cwd)]):
            cfg = read_toml(ctx, p)
            if isinstance(cfg, dict):
                add_mcp_servers(ctx, "codex", scope, p, cfg.get("mcp_servers"))


MCP_PRECEDENCE = {"claude": ["local", "project", "user", "managed"],
                  "codex": ["repo", "user", "system"]}


def check_mcp(ctx: Ctx):
    servers = [s for s in ctx.inventory["mcp"] if not s["disabled"]]
    for agent in ("claude", "codex"):
        mine = [s for s in servers if s["agent"] == agent]
        by_name: dict[str, list] = {}
        for s in mine:
            if not s["scope"].startswith("plugin:"):  # plugin servers are namespaced
                by_name.setdefault(s["name"], []).append(s)
        for name, group in by_name.items():
            scopes = {s["scope"] for s in group}
            if len(scopes) > 1:
                order = MCP_PRECEDENCE[agent]
                win = min(group, key=lambda s: order.index(s["scope"]) if s["scope"] in order else 99)
                same = len({s["identity"] for s in group}) == 1
                ctx.add("info" if same else "warning", "MCP_NAME_SHADOWED",
                        f"{agent} MCP server '{name}' is defined in {sorted(scopes)}",
                        f"The {win['scope']} definition wins; the others are ignored"
                        + (" (they are identical)." if same else " and differ from it."),
                        sorted({s["file"] for s in group}),
                        "Keep one definition, or rename if both are wanted.")
        by_id: dict[str, list] = {}
        for s in mine:
            if s["identity"]:
                by_id.setdefault(s["identity"], []).append(s)
        for ident, group in by_id.items():
            names = {s["name"] for s in group}
            if len(names) > 1:
                ctx.add("warning", "MCP_DUPLICATE_SERVER",
                        f"{agent}: the same MCP server is registered as {sorted(names)}",
                        "It starts twice and exposes duplicate tools, which wastes context and can make "
                        "the model pick either copy.",
                        sorted({f"{s['file']} ({s['scope']})" for s in group}),
                        "Remove one registration (often a plugin plus a manual copy).")
    if {"claude", "codex"} <= ctx.agents:
        cn = {s["identity"] for s in servers if s["agent"] == "claude" and s["identity"]}
        shared = {s["name"] for s in servers if s["agent"] == "codex" and s["identity"] in cn}
        if shared:
            ctx.add("info", "MCP_SHARED_ACROSS_AGENTS", f"MCP servers configured for both agents: {sorted(shared)}",
                    "Fine if intended; keep their settings in sync.")


# --------------------------------------------------------------------------- instructions

def add_instr(ctx: Ctx, agent: str, scope: str, p: Path):
    txt = read_text(p)
    if txt is None:
        return
    ctx.inventory["instructions"].append({
        "agent": agent, "scope": scope, "path": ctx.disp(p), "bytes": len(txt.encode()),
        "symlink_to": ctx.disp(Path(os.path.realpath(p))) if p.is_symlink() else None,
        "imports": re.findall(r"(?m)^\s*@([^\s]+)", txt)[:20],
        "empty": not txt.strip(),
    })


def scan_instructions(ctx: Ctx):
    if "claude" in ctx.agents:
        add_instr(ctx, "claude", "managed", ctx.managed_dir / "CLAUDE.md")
        add_instr(ctx, "claude", "user", ctx.claude_home / "CLAUDE.md")
        for f in sorted((ctx.claude_home / "rules").rglob("*.md")) if (ctx.claude_home / "rules").is_dir() else []:
            add_instr(ctx, "claude", "user-rules", f)
        chain = [d for d in reversed([ctx.cwd, *ctx.cwd.parents]) if d != ctx.home and d != Path("/")]
        for d in chain:
            for name in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"):
                add_instr(ctx, "claude", "project" if d == ctx.root else "ancestor"
                          if d in ctx.root.parents else "subdir", d / name)
        rules = ctx.root / ".claude" / "rules"
        for f in sorted(rules.rglob("*.md")) if rules.is_dir() else []:
            add_instr(ctx, "claude", "project-rules", f)
    if "codex" in ctx.agents:
        for base, scope in [(ctx.codex_home, "user")] + [(d, "repo") for d in dirs_root_to_cwd(ctx.root, ctx.cwd)]:
            ov, md = base / "AGENTS.override.md", base / "AGENTS.md"
            ov_ok = ov.is_file() and (read_text(ov) or "").strip()
            if ov_ok:
                add_instr(ctx, "codex", scope + "-override", ov)
                if md.is_file():
                    ctx.add("warning", "AGENTS_OVERRIDDEN", f"{ctx.disp(md)} is ignored",
                            "AGENTS.override.md in the same directory replaces it for Codex.",
                            [ctx.disp(ov), ctx.disp(md)], "Merge them or delete the override if stale.")
            elif md.is_file():
                add_instr(ctx, "codex", scope, md)
                if md.stat().st_size > 32 * 1024:
                    ctx.add("warning", "AGENTS_TOO_LARGE", f"{ctx.disp(md)} exceeds 32 KiB",
                            "Codex truncates project docs at project_doc_max_bytes (32 KiB by default).",
                            [ctx.disp(md)], "Shorten it or split it per directory.")
    # CLAUDE.md and AGENTS.md maintained separately in the same repo?
    for d in dirs_root_to_cwd(ctx.root, ctx.cwd):
        c, a = d / "CLAUDE.md", d / "AGENTS.md"
        if c.is_file() and a.is_file():
            linked = os.path.realpath(c) == os.path.realpath(a) or "@AGENTS.md" in (read_text(c) or "")
            if not linked:
                ctx.add("info", "INSTRUCTIONS_DUAL", f"CLAUDE.md and AGENTS.md both exist in {ctx.disp(d)}",
                        "Claude reads CLAUDE.md and Codex reads AGENTS.md; they can drift apart. "
                        "Compare them for contradictions.", [ctx.disp(c), ctx.disp(a)],
                        "Make one import the other (e.g. CLAUDE.md containing @AGENTS.md) or a symlink.")


# --------------------------------------------------------------------------- skill checks

CLAUDE_PRECEDENCE = ["managed", "personal", "project", "project-nested", "account-synced"]
CODEX_PRECEDENCE = ["repo-subdir", "repo", "user", "system"]


def bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s).casefold())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def skill_group(s: dict) -> str | None:
    if s["scope"] == "plugin":
        return "plugin:" + str(s["origin"])
    if s["scope"] == "account-synced":
        return "synced:" + os.path.dirname(s["realpath"])
    return "solo:" + s["realpath"]


def check_skills(ctx: Ctx):
    skills = [s for s in ctx.inventory["skills"] if s["enabled"] is not False]
    # same directory reached twice (symlinks)
    by_real: dict[tuple, list] = {}
    for s in skills:
        by_real.setdefault((s["agent"], s["realpath"]), []).append(s)
    for (agent, _), group in by_real.items():
        if len({g["path"] for g in group}) > 1:
            ctx.add("warning", "SKILL_LOADED_TWICE",
                    f"{agent}: skill '{group[0]['name']}' is reachable from {len(group)} locations",
                    "The same folder is found through a symlink or overlapping path and may be registered "
                    "twice (shows up as a duplicate or a load error).", [g["path"] for g in group],
                    "Point only one discovery path at the folder.")
    # name collisions per agent
    for agent, order in (("claude", CLAUDE_PRECEDENCE), ("codex", CODEX_PRECEDENCE)):
        local = [s for s in skills if s["agent"] == agent and s["scope"] != "plugin"]
        by_name: dict[str, list] = {}
        for s in local:
            by_name.setdefault(norm_name(s["name"]), []).append(s)
        if agent == "claude":
            for c in ctx.inventory["commands"]:
                if c["scope"] != "plugin":
                    by_name.setdefault(norm_name(c["name"]), []).append({**c, "scope": c["scope"] + "-command"})
        for key, group in by_name.items():
            reals = {g.get("realpath", g["path"]) for g in group}
            if len(group) < 2 or len(reals) < 2:
                continue
            def rank(g):
                sc = g["scope"]
                if sc.endswith("-command"):
                    return 50
                return order.index(sc) if sc in order else 40
            win = min(group, key=rank)
            losers = [g for g in group if g is not win]
            hashes = {g.get("content_hash") for g in group}
            if len(hashes) == 1 and None not in hashes and "" not in hashes:
                ctx.add("info", "SKILL_DUPLICATE_IDENTICAL",
                        f"{agent}: '{win['name']}' exists in {len(group)} places with identical content",
                        "No behaviour difference today, but if the shared copy is updated the "
                        f"{win['scope']} copy will keep winning with the old content.",
                        [g["path"] for g in group],
                        "Delete the extra copy and rely on the shared one.")
                continue
            synced_lost = any(g["scope"] == "account-synced" for g in losers)
            extra = (" The claude.ai account skill keeps running only as /anthropic-skills:<name>."
                     if synced_lost else "")
            guess = "" if agent == "claude" else " (Codex precedence inferred: closer scope first.)"
            owners = {owner_of(g["scope"].replace("-command", "")) for g in group}
            team_lost = any(owner_of(g["scope"].replace("-command", "")) == "team" for g in losers)
            team_note = (" The repository's shared (team) version is overridden by a personal one, so this "
                         "machine runs different instructions from the rest of the team." if team_lost else "")
            ctx.add("action", "SKILL_NAME_COLLISION",
                    f"{agent}: {len(group)} skills/commands named '{win['name']}' with different content"
                    + (" (team version overridden)" if team_lost else ""),
                    team_note.strip() + (" " if team_note else "") +
                    f"The {win['scope']} one ({win['path']}) is used; "
                    + ", ".join(f"{g['scope']} ({g['path']})" for g in losers)
                    + " is hidden." + extra + guess,
                    [g["path"] for g in group],
                    "Rename or remove the one you do not want; if they are copies, keep a single source.")
        # plugin skill with the same short name as a local one: both load, but confusable
        plug = [s for s in skills if s["agent"] == agent and s["scope"] == "plugin"]
        for p in plug:
            twins = [s for s in local if norm_name(s["name"]) == norm_name(p["name"])]
            if twins:
                ctx.add("warning", "SKILL_PLUGIN_SHADOW",
                        f"Plugin skill {p['origin']}:{p['name']} has the same name as a {twins[0]['scope']} skill",
                        "Both load (plugin skills are namespaced), but /" + p["name"] + " runs the local one and "
                        "the model sees two near-identical entries when choosing automatically.",
                        [p["path"]] + [t["path"] for t in twins],
                        "Rename one, or disable whichever is redundant.")
    # similar descriptions within an agent (candidates for the model to judge)
    pairs = []
    for agent in ("claude", "codex"):
        cand = [s for s in skills if s["agent"] == agent and s["description"] and not s["user_only"]]
        for a, b in combinations(cand, 2):
            if norm_name(a["name"]) == norm_name(b["name"]) or a["realpath"] == b["realpath"]:
                continue
            if skill_group(a) == skill_group(b):
                continue  # shipped together by one author (same plugin / account bundle)
            ba, bb = bigrams(a["description"]), bigrams(b["description"])
            if not ba or not bb:
                continue
            j = len(ba & bb) / len(ba | bb)
            if j >= 0.5:
                pairs.append((j, agent, a, b))
    seen_pairs = set()
    uniq = []
    for item in sorted(pairs, key=lambda x: -x[0]):
        k = (item[1],) + tuple(sorted((norm_name(item[2]["name"]), norm_name(item[3]["name"]))))
        if k not in seen_pairs:
            seen_pairs.add(k)
            uniq.append(item)
    ctx.similar_pairs = uniq[:15]
    for j, agent, a, b in uniq[:15]:
        ctx.add("warning", "SKILL_SIMILAR_DESCRIPTION",
                f"{agent}: '{a['name']}' and '{b['name']}' have similar descriptions ({j:.2f})",
                "Candidate only: the model may pick the wrong one. Judge whether their triggers really overlap.",
                [a["path"], b["path"]],
                "Make the descriptions say when NOT to use each, or drop one.")
    # subagent names
    by_agent_name: dict[str, list] = {}
    for g in ctx.inventory["agents"]:
        if g["scope"] != "plugin":
            by_agent_name.setdefault(norm_name(g["name"]), []).append(g)
    for group in by_agent_name.values():
        if len(group) > 1:
            win = min(group, key=lambda g: ["project", "personal"].index(g["scope"])
                      if g["scope"] in ("project", "personal") else 9)
            ctx.add("warning", "SUBAGENT_NAME_COLLISION", f"Subagent '{win['name']}' is defined {len(group)} times",
                    f"The {win['scope']} definition is used; the others are hidden.",
                    [g["path"] for g in group], "Rename or remove the duplicate.")
    # skills/agents that carry their own hooks
    for s in ctx.inventory["skills"] + ctx.inventory["agents"]:
        if s.get("has_hooks"):
            ctx.add("info", "EMBEDDED_HOOKS", f"'{s['name']}' defines hooks in its frontmatter",
                    "These hooks run only while it is active, and do not appear in settings files.", [s["path"]])


# --------------------------------------------------------------------------- report (markdown)

def layer_label(item: dict, ctx: Ctx) -> str:
    sc = item["scope"].replace("-command", "")
    agent = "Codex・" if item.get("agent") == "codex" else ""
    if sc == "plugin" or sc.startswith("plugin:"):
        name = item.get("origin") or sc.split(":", 1)[-1]
        who = "チーム" if name in ctx.team_plugins else "個人"
        return f"{agent}{who}・プラグイン {name}"
    if sc == "account-synced":
        return f"{agent}アカウント同期"
    return agent + {"team": "チーム", "personal": "個人", "org": "組織"}[owner_of(sc)]


def find_line(path: str | None, needle: str) -> int | None:
    if not path or not needle:
        return None
    text = read_text(Path(path)) or ""
    key = needle.strip().splitlines()[0][:40]
    for variant in (key, json.dumps(key)[1:-1]):
        for i, line in enumerate(text.splitlines(), 1):
            if variant and variant in line:
                return i
    return None


def hook_target(h: dict, ctx: Ctx) -> str:
    line = find_line(h.get("_abs"), h["command"])
    cmd = " ".join(h["command"].split())
    cmd = cmd if len(cmd) <= 60 else cmd[:57] + "..."
    m = f"({h['matcher']})" if h["matcher"] else ""
    return f"`{h['file']}{':' + str(line) if line else ''}`（{layer_label({**h, 'scope': h['source']}, ctx)}）{h['event']}{m}: `{cmd}`"


def md_cell(lines: list[str]) -> str:
    return "<br>".join(f"・{x}".replace("|", "\\|") for x in lines)


def build_report_md(ctx: Ctx) -> str:
    skills = [s for s in ctx.inventory["skills"] if s["enabled"] is not False]
    rows_s: list[tuple] = []
    flagged: set[str] = set()
    # name duplicates with different content (identical copies are not a conflict)
    for agent in ("claude", "codex"):
        groups: dict[str, list] = {}
        for sk in skills:
            if sk["agent"] == agent:
                groups.setdefault(norm_name(sk["name"]), []).append(sk)
        if agent == "claude":
            for c in ctx.inventory["commands"]:
                if c["enabled"] is not False:
                    groups.setdefault(norm_name(c["name"]), []).append({**c, "scope": c["scope"] + "-command"})
        for g in groups.values():
            reals = {x.get("realpath", x["path"]) for x in g}
            hashes = {x.get("content_hash") for x in g}
            if len(g) < 2 or len(reals) < 2 or (len(hashes) == 1 and None not in hashes):
                continue
            rows_s.append(("nameの重複", [f"`{x['path']}`（{layer_label(x, ctx)}）" for x in g],
                           f"`{g[0]['name']}` が{len(g)}か所にあり、内容が異なる"))
            flagged |= {x["path"] for x in g}
    for j, agent, a, b in ctx.similar_pairs:
        rows_s.append(("descriptionの重複", [f"`{x['path']}`（{layer_label(x, ctx)}）" for x in (a, b)],
                       f"`{a['name']}` と `{b['name']}` の説明文が類似（類似度 {j:.2f}）"))
        flagged |= {a["path"], b["path"]}
    if ctx.team_repo:
        only: dict[str, list] = {}
        shared = {(x["agent"], norm_name(x["name"]), x.get("content_hash")) for x in skills
                  if "チーム" in layer_label(x, ctx)}
        for sk in skills:
            lab = layer_label(sk, ctx)
            if (sk["agent"], norm_name(sk["name"]), sk.get("content_hash")) in shared:
                continue  # identical copy of a team skill: same behaviour as the team
            if "チーム" not in lab and "組織" not in lab and sk["path"] not in flagged:
                only.setdefault(lab, []).append(sk)
        for lab, lst in only.items():
            rows_s.append(("この環境でのみ動作", [f"`{x['path']}`" for x in lst],
                           f"出どころ: {lab}（{len(lst)}件）。リポジトリには含まれない"))
            flagged |= {x["path"] for x in lst}
    # hooks
    hooks = ctx.inventory["hooks"]
    rows_h: list[tuple] = []
    hflag: set[int] = set()
    for a, b, same in ctx.hook_pairs:
        rows_h.append(("重複発火", [hook_target(a, ctx), hook_target(b, ctx)],
                       "同じコマンドが2か所に登録されている" if same else
                       f"{a['event']} で条件が重なり、両方が発火する"))
        hflag |= {id(a), id(b)}
    if ctx.team_repo:
        by_src: dict[str, list] = {}
        for h in hooks:
            if h["owner"] == "personal":
                by_src.setdefault(h["file"], []).append(h)
        for file, hs in by_src.items():
            rows_h.append(("この環境でのみ発火", [hook_target(h, ctx) for h in hs],
                           f"出どころ: {layer_label({**hs[0], 'scope': hs[0]['source']}, ctx)}（{len(hs)}件）。"
                           "リポジトリには含まれない"))
            hflag |= {id(h) for h in hs}
    for h in hooks:
        if h.get("_issues"):
            rows_h.append(("動作しない恐れ", [hook_target(h, ctx)], "／".join(h["_issues"])))
            hflag.add(id(h))
    # MCP
    servers = [x for x in ctx.inventory["mcp"] if not x["disabled"]]
    rows_m: list[tuple] = []
    mflag: set[int] = set()

    def mcp_target(x: dict) -> str:
        line = find_line(x.get("_abs"), f'"{x["name"]}"') or find_line(x.get("_abs"), f"mcp_servers.{x['name']}")
        lab = layer_label({**x, "scope": x["scope"]}, ctx)
        where = (x["identity"] or "").split(":", 1)[-1]
        where = where if len(where) <= 60 else where[:57] + "..."
        return f"`{x['file']}{':' + str(line) if line else ''}`（{lab}）`{x['name']}`: `{where}`"

    for agent in ("claude", "codex"):
        mine = [x for x in servers if x["agent"] == agent]
        by_name: dict[str, list] = {}
        for x in mine:
            if not x["scope"].startswith("plugin:"):
                by_name.setdefault(x["name"], []).append(x)
        for name, g in by_name.items():
            if len(g) > 1 and len({x["identity"] for x in g}) > 1:
                rows_m.append(("nameの重複", [mcp_target(x) for x in g], f"`{name}` が{len(g)}か所にあり、接続先が異なる"))
                mflag |= {id(x) for x in g}
        by_id: dict[str, list] = {}
        for x in mine:
            if x["identity"]:
                by_id.setdefault(x["identity"], []).append(x)
        for ident, g in by_id.items():
            if len({x["name"] for x in g}) > 1:
                rows_m.append(("接続先の重複", [mcp_target(x) for x in g],
                               "同じサーバーが別名で登録され、二重に起動する"))
                mflag |= {id(x) for x in g}
    if ctx.team_repo:
        team_ids = {(x["agent"], x["identity"]) for x in servers if x["owner"] == "team"}
        by_file: dict[str, list] = {}
        for x in servers:
            if x["owner"] == "personal" and id(x) not in mflag and (x["agent"], x["identity"]) not in team_ids:
                by_file.setdefault(x["file"] + x["scope"], []).append(x)
        for g in by_file.values():
            rows_m.append(("この環境でのみ接続", [mcp_target(x) for x in g],
                           f"出どころ: {layer_label(g[0], ctx)}（{len(g)}件）。リポジトリには含まれない"))
            mflag |= {id(x) for x in g}
    broken = [f for f in ctx.findings if f["code"] in ("MCP_WRONG_KEY", "MCP_WRONG_LOCATION")]
    for f in broken:
        rows_m.append(("動作しない恐れ", [f"`{l}`" for l in f["locations"]],
                       "キーが `servers` になっていて読み込まれない" if f["code"] == "MCP_WRONG_KEY"
                       else "`.claude/` 内にあり読み込まれない（リポジトリ直下に置く必要がある）"))
    # CLAUDE.md / AGENTS.md (persistent instructions)
    instr = [x for x in ctx.inventory["instructions"] if not x["empty"]]
    rows_i: list[tuple] = []
    personal_scopes = {"user", "user-rules", "ancestor", "user-override"}
    only_i = [x for x in instr if x["scope"] in personal_scopes] if ctx.team_repo else []
    by_lab: dict[str, list] = {}
    for x in only_i:
        lab = ("Codex・" if x["agent"] == "codex" else "") + ("個人（リポジトリの外の上位フォルダ）" if x["scope"] == "ancestor" else "個人")
        by_lab.setdefault(lab, []).append(x)
    for lab, g in by_lab.items():
        rows_i.append(("この環境でのみ読み込み", [f"`{x['path']}`" for x in g],
                       f"出どころ: {lab}（{len(g)}件）。リポジトリには含まれない"))
    out = []
    for title, total, rows, n in (("skills", len(skills), rows_s, len(flagged)),
                                   ("hooks", len(hooks), rows_h, len(hflag)),
                                   ("MCP", len(servers), rows_m, len(mflag) + len(broken)),
                                   ("CLAUDE.md / AGENTS.md", len(instr), rows_i, len(only_i))):
        out += [f"## {title}", "", f"- 読み取り: {total}件", f"- 競合の可能性あり: {n}件", ""]
        if rows:
            out += ["| 分類 | 対象 | 状況 |", "|---|---|---|"]
            out += [f"| {c} | {md_cell(t)} | {st} |" for c, t, st in rows]
            out.append("")
    if ctx.notes:
        out += [f"※ {n}" for n in ctx.notes]
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- output

def to_text(report: dict) -> str:
    inv = report["inventory"]
    out = [f"agent-doctor scan  cwd={report['environment']['cwd']}  repo={report['environment']['repo_root']}",
           "Inventory: " + ", ".join(f"{k}={len(v)}" for k, v in inv.items()), ""]
    labels = {"action": "ACTION", "warning": "WARNING", "info": "INFO"}
    for f in report["findings"]:
        out.append(f"[{labels[f['severity']]}] {f['code']}: {f['title']}")
        out.append(f"    {f['detail']}")
        for loc in f["locations"]:
            out.append(f"    - {loc}")
        if f["suggestion"]:
            out.append(f"    fix: {f['suggestion']}")
    if not report["findings"]:
        out.append("No problems found.")
    for n in report["notes"]:
        out.append(f"note: {n}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd")
    ap.add_argument("--home")
    ap.add_argument("--claude-home")
    ap.add_argument("--codex-home")
    ap.add_argument("--managed-dir")
    ap.add_argument("--agents", default="claude,codex")
    ap.add_argument("--format", choices=["report", "json", "text"], default="report")
    ctx = Ctx(ap.parse_args(argv))

    settings = load_claude_settings(ctx) if "claude" in ctx.agents else []
    plugin_hooks = []
    if "claude" in ctx.agents:
        scan_claude_skills(ctx)
        plugin_hooks = scan_plugin_components(ctx, discover_plugins(ctx, settings))
    if "codex" in ctx.agents:
        scan_codex_skills(ctx)
    scan_hooks(ctx, settings, plugin_hooks)
    scan_mcp(ctx)
    scan_instructions(ctx)
    check_skills(ctx)
    check_hooks(ctx)
    check_mcp(ctx)
    if ctx.format == "report":
        print(build_report_md(ctx), end="")
        return 0
    for s in ctx.inventory["skills"]:
        s.pop("realpath", None)
    for h in ctx.inventory["hooks"]:
        for k in ("_plugin_root", "_abs", "_issues"):
            h.pop(k, None)
    for m in ctx.inventory["mcp"]:
        m.pop("_abs", None)

    present = {"claude": ctx.claude_home.is_dir(), "codex": ctx.codex_home.is_dir()
               or any(i["agent"] == "codex" for i in ctx.inventory["skills"] + ctx.inventory["instructions"])}
    ctx.findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["code"]))
    report = {
        "environment": {"cwd": str(ctx.cwd), "repo_root": str(ctx.root), "home": str(ctx.home),
                        "claude_home": str(ctx.claude_home), "codex_home": str(ctx.codex_home),
                        "managed_dir": str(ctx.managed_dir), "agents_scanned": sorted(ctx.agents),
                        "agents_detected": sorted(k for k, v in present.items() if v),
                        "remote_session": bool(os.environ.get("CLAUDE_CODE_REMOTE"))},
        "summary": {s: sum(1 for f in ctx.findings if f["severity"] == s) for s in SEVERITY_ORDER},
        "findings": ctx.findings,
        "inventory": ctx.inventory,
        "notes": ctx.notes,
    }
    print(to_text(report) if ctx.format == "text" else json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
