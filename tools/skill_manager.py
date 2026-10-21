#!/usr/bin/env python3
from __future__ import annotations

# BEGIN HUMAN READABLE UI PATCH
import json, sys
def _print_human_readable_ui(res):
    if not isinstance(res, dict):
        return
    if res.get('success'):
        data = res.get('data', {})
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    print(f'>>> {k}:', file=sys.stderr)
                    for item in v:
                        if isinstance(item, dict):
                            summary = ' | '.join(
                                f'{ik}={iv}' for ik, iv in item.items()
                                if isinstance(iv, (str, int, float, bool))
                            )
                            print(f'    - {summary}', file=sys.stderr)
                        else:
                            print(f'    - {item}', file=sys.stderr)
                else:
                    print(f'>>> {k}: {v}', file=sys.stderr)
        for w in res.get('warnings') or []:
            print(f'~>> Warning: {w}', file=sys.stderr)
    else:
        err = res.get('error')
        if isinstance(err, dict):
            print(f'!!! Error: {err.get("message", err)}', file=sys.stderr)
        else:
            print(f'!!! Error: {err}', file=sys.stderr)
# END HUMAN READABLE UI PATCH

"""Manage agent skills in the open Agent Skills (SKILL.md) format.

A skill is a portable markdown file: YAML frontmatter (name, description,
category) plus a structured body (Overview, When to use, Core concepts,
Practical workflow, Common pitfalls). Skills are inert text — the agent reads
them to learn a workflow and acts only with user approval. Convention is
80-150 lines, lowercase-hyphen names, and descriptions with explicit trigger
phrases so the agent knows when to invoke the skill.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SKILL_FILENAME = "SKILL.md"
DEFAULT_SKILLS_DIR = os.path.expanduser("~/skills")
VALID_ACTIONS = ("create", "load", "list", "update", "delete", "validate", "search", "import")
LOOSE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
STRICT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REQUIRED_SECTIONS = ("Overview", "When to use", "Core concepts", "Practical workflow", "Common pitfalls")
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)


# ---------------------------------------------------------------------------
# Frontmatter helpers (minimal single-line YAML: name / description / category)
# ---------------------------------------------------------------------------

def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except Exception:
            return value[1:-1]
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _yaml_value(value: str) -> str:
    """Serialize a scalar, quoting when a plain YAML scalar would be invalid."""
    if re.search(r"[:#{}\[\]&*!|>%@`]", value) or value != value.strip() or not value:
        return json.dumps(value, ensure_ascii=False)
    return value


def _parse_frontmatter(text: str) -> Tuple[Optional[Dict[str, str]], str]:
    """Split SKILL.md text into (frontmatter dict, body). Frontmatter is optional."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    meta: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if line.strip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        if key:
            meta[key] = _unquote(raw)
    return meta, text[m.end():]


def _build_frontmatter(meta: Dict[str, str]) -> str:
    ordered = ("name", "description", "category")
    lines = ["---"]
    for key in ordered:
        if meta.get(key):
            lines.append(f"{key}: {_yaml_value(str(meta[key]))}")
    for key, value in meta.items():
        if key not in ordered and value:
            lines.append(f"{key}: {_yaml_value(str(value))}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _assemble(meta: Dict[str, str], body: str) -> str:
    """Rebuild a full SKILL.md from frontmatter dict + markdown body."""
    body = body.strip("\n")
    text = _build_frontmatter(meta) + "\n"
    if body:
        text += body + "\n"
    return text


# ---------------------------------------------------------------------------
# Skill discovery / metadata
# ---------------------------------------------------------------------------

def _resolve_skill(skills_dir: Path, name: str) -> Optional[Path]:
    """Find a skill's file: skills_dir/<name>/SKILL.md, or flat skills_dir/<name>.md."""
    if not name:
        return None
    nested = skills_dir / name / SKILL_FILENAME
    if nested.is_file():
        return nested
    flat = skills_dir / f"{name}.md"
    if flat.is_file():
        return flat
    return None


def _iter_skill_files(skills_dir: Path) -> List[Path]:
    found: List[Path] = []
    if not skills_dir.is_dir():
        return found
    for item in sorted(skills_dir.iterdir()):
        if item.is_dir():
            md = item / SKILL_FILENAME
            if md.is_file():
                found.append(md)
        elif item.is_file() and item.suffix == ".md":
            found.append(item)
    return found


def _skill_meta(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    meta, _ = _parse_frontmatter(text)
    fallback = path.parent.name if path.name == SKILL_FILENAME else path.stem
    stat = path.stat()
    return {
        "name": (meta or {}).get("name") or fallback,
        "description": (meta or {}).get("description", ""),
        "category": (meta or {}).get("category", ""),
        "path": str(path.absolute()),
        "lines": len(text.splitlines()),
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def _short(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Format validation (Agent Skills conventions)
# ---------------------------------------------------------------------------

def _validate_skill_text(name: str, text: str) -> List[Dict[str, str]]:
    """Check a skill against the Agent Skills format. Returns issue dicts with
    severity: 'error' (invalid), 'warning' (convention violation),
    'suggestion' (recommended design pattern)."""
    issues: List[Dict[str, str]] = []
    meta, body = _parse_frontmatter(text)
    lines = text.splitlines()

    if meta is None:
        issues.append({
            "severity": "error",
            "check": "frontmatter",
            "message": "Missing YAML frontmatter — skills must start with a '---' block containing name and description.",
        })
        meta = {}
    else:
        fm_name = meta.get("name", "")
        if not fm_name:
            issues.append({"severity": "error", "check": "name", "message": "Frontmatter is missing the 'name' field."})
        else:
            if name and fm_name != name:
                issues.append({"severity": "warning", "check": "name", "message": f"Frontmatter name '{fm_name}' does not match skill name '{name}'."})
            if not STRICT_NAME_RE.match(fm_name):
                issues.append({"severity": "warning", "check": "name", "message": f"Name '{fm_name}' — prefer lowercase letters, digits, and hyphens (e.g. 'marketplace-appraiser')."})

        desc = meta.get("description", "")
        if not desc:
            issues.append({"severity": "error", "check": "description", "message": "Frontmatter is missing the 'description' field — without it the agent cannot decide when to trigger the skill."})
        else:
            if len(desc) < 40:
                issues.append({"severity": "warning", "check": "description", "message": f"Description is thin ({len(desc)} chars) — spell out what the skill does and when to use it."})
            if '"' not in desc:
                issues.append({"severity": "suggestion", "check": "description", "message": "Description has no quoted trigger phrases — explicit triggers (e.g. \"is this a good deal?\") help the agent invoke the skill reliably."})

        if not meta.get("category"):
            issues.append({"severity": "warning", "check": "category", "message": "Frontmatter is missing the 'category' field."})

    headings = []
    for line in body.splitlines():
        m = re.match(r"^#{1,3}\s+(.+?)\s*$", line)
        if m:
            headings.append(m.group(1).lower().rstrip(":"))
    for section in REQUIRED_SECTIONS:
        if section.lower() not in headings:
            issues.append({"severity": "warning", "check": "sections", "message": f"Missing recommended section: '{section}'."})

    n = len(lines)
    if n < 20:
        issues.append({"severity": "warning", "check": "length", "message": f"Only {n} lines — too thin to be actionable."})
    elif n > 200:
        issues.append({"severity": "warning", "check": "length", "message": f"{n} lines — convention is 80-150; consider splitting into focused skills."})

    lowered = text.lower()
    if "won't do" not in lowered and "won’t do" not in lowered:
        issues.append({"severity": "suggestion", "check": "refusals", "message": "No 'won't do' refusal lines found — explicit refusal boundaries are a recommended pattern."})

    return issues


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def validate(action: str, name: str, description: str, category: str, content: str,
             file: str, query: str, skills_dir: str) -> None:
    """Validate inputs before execution."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid action: {action}. Must be one of: {', '.join(VALID_ACTIONS)}")

    needs_name = action in {"load", "update", "delete"} or (action == "validate" and not file)
    if needs_name and not name:
        raise ValueError(f"Name is required for action '{action}'")

    if action == "create":
        if not content and not file:
            raise ValueError("Content is required when creating a skill (pass content or a file path)")
        if not file:
            meta, _ = _parse_frontmatter(content or "")
            if meta is None and not description:
                raise ValueError("Description is required when creating a skill (unless content carries YAML frontmatter)")
            if meta is not None and not name and not meta.get("name"):
                raise ValueError("Name is required: content has frontmatter but no 'name' field")

    if action == "search" and not query:
        raise ValueError("Query is required for action 'search'")

    if action == "import" and not file:
        raise ValueError("A source file path is required for action 'import'")

    if name and not LOOSE_NAME_RE.match(name):
        raise ValueError("Skill name can only contain alphanumeric characters, hyphens, and underscores")

    if not skills_dir:
        raise ValueError("skills_dir path cannot be empty")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _ok(data: Dict[str, Any], warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"success": True, "data": data, "warnings": warnings or [], "error": None}


def _fail(message: str) -> Dict[str, Any]:
    return {"success": False, "data": {}, "warnings": [], "error": message}


def _read_file_arg(file: str) -> str:
    src = Path(os.path.expanduser(file))
    if not src.is_file():
        raise ValueError(f"File not found: {src}")
    return src.read_text(encoding="utf-8")


def execute(action: str, name: str, description: str, category: str, content: str,
            file: str, query: str, skills_dir: str, force: bool) -> Dict[str, Any]:
    """Execute the core logic of the skill manager."""
    target_dir = Path(os.path.expanduser(skills_dir))

    if action == "create":
        raw = _read_file_arg(file) if file else content
        incoming_meta, incoming_body = _parse_frontmatter(raw)
        if incoming_meta is not None:
            # Content already carries frontmatter — merge, never double-wrap.
            meta = dict(incoming_meta)
            if name:
                meta["name"] = name
            if description:
                meta["description"] = description
            if category:
                meta["category"] = category
            body = incoming_body
        else:
            if not name:
                raise ValueError("Name is required when creating a skill")
            meta = {"name": name, "description": description}
            if category:
                meta["category"] = category
            body = raw
        effective_name = meta.get("name", "")
        if not effective_name:
            raise ValueError("Could not determine skill name")
        skill_dir = target_dir / effective_name
        skill_file = skill_dir / SKILL_FILENAME
        if skill_file.exists() and not force:
            return _fail(f"Skill '{effective_name}' already exists at {skill_file} — pass force to overwrite")
        skill_dir.mkdir(parents=True, exist_ok=True)
        full_text = _assemble(meta, body)
        try:
            skill_file.write_text(full_text, encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"Failed to write skill file: {e}")
        issues = _validate_skill_text(effective_name, full_text)
        return _ok(
            {
                "action": "create",
                "name": effective_name,
                "path": str(skill_file.absolute()),
                "lines": len(full_text.splitlines()),
                "size_bytes": len(full_text.encode("utf-8")),
                "message": f"Skill '{effective_name}' created successfully.",
            },
            warnings=[i["message"] for i in issues if i["severity"] in ("warning", "suggestion")],
        )

    if action == "load":
        skill_file = _resolve_skill(target_dir, name)
        if skill_file is None:
            return _fail(f"Skill '{name}' not found in {target_dir} (looked for {name}/SKILL.md and {name}.md)")
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"Failed to read skill file: {e}")
        meta, _ = _parse_frontmatter(text)
        return _ok({
            "action": "load",
            "name": name,
            "path": str(skill_file.absolute()),
            "description": (meta or {}).get("description", ""),
            "category": (meta or {}).get("category", ""),
            "lines": len(text.splitlines()),
            "content": text,
        })

    if action == "list":
        skills = []
        for md in _iter_skill_files(target_dir):
            m = _skill_meta(md)
            m["description"] = _short(m["description"])
            skills.append(m)
        return _ok({
            "action": "list",
            "skills_dir": str(target_dir.absolute()),
            "count": len(skills),
            "skills": sorted(skills, key=lambda s: s["name"]),
        })

    if action == "update":
        skill_file = _resolve_skill(target_dir, name)
        if skill_file is None:
            return _fail(f"Skill '{name}' not found in {target_dir}")
        text = skill_file.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        meta = dict(meta or {})
        changed: List[str] = []
        if file or content:
            raw = _read_file_arg(file) if file else content
            new_meta, new_body = _parse_frontmatter(raw)
            if new_meta is not None:
                meta.update(new_meta)
                body = new_body
            else:
                body = raw
            changed.append("content")
        if description:
            meta["description"] = description
            changed.append("description")
        if category:
            meta["category"] = category
            changed.append("category")
        if not changed:
            return _fail("Nothing to update — provide content, a file, description, or category")
        meta.setdefault("name", name)
        full_text = _assemble(meta, body)
        try:
            skill_file.write_text(full_text, encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"Failed to write skill file: {e}")
        issues = _validate_skill_text(meta.get("name", name), full_text)
        return _ok(
            {
                "action": "update",
                "name": name,
                "path": str(skill_file.absolute()),
                "changed": changed,
                "lines": len(full_text.splitlines()),
                "message": f"Skill '{name}' updated ({', '.join(changed)}).",
            },
            warnings=[i["message"] for i in issues if i["severity"] in ("warning", "suggestion")],
        )

    if action == "delete":
        skill_file = _resolve_skill(target_dir, name)
        if skill_file is None:
            return _fail(f"Skill '{name}' not found in {target_dir}")
        if skill_file.name == SKILL_FILENAME:
            skill_dir = skill_file.parent
            extras = [p.name for p in skill_dir.iterdir() if p.name != SKILL_FILENAME]
            if extras and not force:
                return _fail(f"Skill directory also contains {len(extras)} other file(s) ({', '.join(extras[:5])}) — pass force to delete anyway")
            shutil.rmtree(skill_dir)
            removed = str(skill_dir.absolute())
        else:
            skill_file.unlink()
            removed = str(skill_file.absolute())
        return _ok({"action": "delete", "name": name, "removed": removed, "message": f"Skill '{name}' deleted."})

    if action == "validate":
        if file:
            skill_file = Path(os.path.expanduser(file))
            if not skill_file.is_file():
                return _fail(f"File not found: {skill_file}")
        else:
            skill_file = _resolve_skill(target_dir, name)
            if skill_file is None:
                return _fail(f"Skill '{name}' not found in {target_dir} (or pass a file path)")
        text = skill_file.read_text(encoding="utf-8")
        issues = _validate_skill_text(name, text)
        errors = [i for i in issues if i["severity"] == "error"]
        warns = [i for i in issues if i["severity"] == "warning"]
        suggests = [i for i in issues if i["severity"] == "suggestion"]
        result: Dict[str, Any] = {
            "action": "validate",
            "path": str(skill_file.absolute()),
            "valid": not errors,
            "lines": len(text.splitlines()),
            "error_count": len(errors),
            "warning_count": len(warns),
            "suggestion_count": len(suggests),
            "issues": issues,
        }
        if errors:
            result["errors"] = [e["message"] for e in errors]
            return {
                "success": False,
                "data": result,
                "warnings": [i["message"] for i in warns + suggests],
                "error": f"{len(errors)} format error(s): " + "; ".join(e["message"] for e in errors),
            }
        return _ok(result, warnings=[i["message"] for i in warns + suggests])

    if action == "search":
        q = query.lower()
        matches = []
        for md in _iter_skill_files(target_dir):
            text = md.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(text)
            skill_name = ((meta or {}).get("name") or md.stem).lower()
            desc = (meta or {}).get("description", "").lower()
            cat = (meta or {}).get("category", "").lower()
            where = []
            if q in skill_name:
                where.append("name")
            if q in desc:
                where.append("description")
            if q in cat:
                where.append("category")
            if q in text.lower():
                where.append("content")
            if where:
                m = _skill_meta(md)
                m["description"] = _short(m["description"])
                m["matched_in"] = where
                matches.append(m)
        return _ok({"action": "search", "query": query, "count": len(matches), "skills": matches})

    if action == "import":
        raw = _read_file_arg(file)
        src_meta, src_body = _parse_frontmatter(raw)
        if src_meta is None:
            return _fail("Source file has no YAML frontmatter — not a valid SKILL.md")
        meta = dict(src_meta)
        effective_name = name or meta.get("name") or Path(os.path.expanduser(file)).stem
        meta["name"] = effective_name
        if description:
            meta["description"] = description
        if category:
            meta["category"] = category
        skill_dir = target_dir / effective_name
        skill_file = skill_dir / SKILL_FILENAME
        if skill_file.exists() and not force:
            return _fail(f"Skill '{effective_name}' already exists at {skill_file} — pass force to overwrite")
        skill_dir.mkdir(parents=True, exist_ok=True)
        full_text = _assemble(meta, src_body)
        try:
            skill_file.write_text(full_text, encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"Failed to write skill file: {e}")
        issues = _validate_skill_text(effective_name, full_text)
        return _ok(
            {
                "action": "import",
                "name": effective_name,
                "source": str(Path(os.path.expanduser(file)).absolute()),
                "path": str(skill_file.absolute()),
                "message": f"Skill '{effective_name}' imported.",
            },
            warnings=[i["message"] for i in issues if i["severity"] in ("warning", "suggestion")],
        )

    return _fail(f"Unknown execution path for action '{action}'")


def run(
    action: str,
    name: str = "",
    description: str = "",
    category: str = "",
    content: str = "",
    file: str = "",
    query: str = "",
    skills_dir: str = "",
    force: bool = False,
) -> Dict[str, Any]:
    """Create, load, list, update, delete, validate, search, or import agent skills.

    Skills follow the Agent Skills format: a SKILL.md markdown file with YAML
    frontmatter (name, description, category) and a body with the sections
    Overview, When to use, Core concepts, Practical workflow, Common pitfalls.

    Args:
        action: The action to perform. 'create' writes a new skill; 'load' returns a skill's full content; 'list' lists all skills with metadata; 'update' modifies an existing skill; 'delete' removes one; 'validate' checks a skill against the Agent Skills format; 'search' finds skills matching a keyword; 'import' registers an external SKILL.md file.
        name: Skill name, lowercase-hyphens preferred (e.g. 'marketplace-appraiser'). Required for load/update/delete; for create, required unless content carries frontmatter with a name; for import, overrides the frontmatter name.
        description: Frontmatter description with explicit trigger phrases (what the skill does and when to use it); used on create/update.
        category: Frontmatter category (e.g. 'money', 'navigation', 'productivity'); used on create/update.
        content: Markdown for create/update — either the body only, or a full SKILL.md including frontmatter (fields are merged, never double-wrapped).
        file: Path to a SKILL.md or body markdown file — alternative to content for create/update; the source file for import; the file to check for validate.
        query: Keyword or phrase for action 'search' (matched against name, description, category, and body).
        skills_dir: Directory where skills live (default '~/skills'). Each skill is skills_dir/<name>/SKILL.md; flat <name>.md files are also supported.
        force: Overwrite an existing skill on create/import; on delete, remove a skill directory that contains extra files.
    """
    try:
        skills_dir = skills_dir or DEFAULT_SKILLS_DIR
        validate(action, name, description, category, content, file, query, skills_dir)
        return execute(action, name, description, category, content, file, query, skills_dir, force)
    except Exception as e:
        return {
            "success": False,
            "data": {},
            "warnings": [],
            "error": str(e)
        }


def _cli() -> int:
    p = argparse.ArgumentParser(description="Manage agent skills (Agent Skills / SKILL.md format).")
    p.add_argument("--action", required=True, choices=list(VALID_ACTIONS), help="Action to perform")
    p.add_argument("--name", default="", help="Name of the skill")
    p.add_argument("--description", default="", help="Description of the skill (frontmatter; for create/update)")
    p.add_argument("--category", default="", help="Category of the skill (frontmatter; e.g. money, navigation)")
    p.add_argument("--content", default="", help="Markdown content of the skill (for create/update)")
    p.add_argument("--file", default="", help="Path to a SKILL.md or body markdown file (create/update/import/validate)")
    p.add_argument("--query", default="", help="Search keyword (for search)")
    p.add_argument("--skills-dir", default=DEFAULT_SKILLS_DIR, help="Directory where skills are stored")
    p.add_argument("--force", action="store_true", help="Overwrite existing skill, or delete a skill dir holding extra files")

    args = p.parse_args()

    result = run(
        action=args.action,
        name=args.name,
        description=args.description,
        category=args.category,
        content=args.content,
        file=args.file,
        query=args.query,
        skills_dir=args.skills_dir,
        force=args.force,
    )

    _print_human_readable_ui(result)

    # Write machine result to LLM_OUTPUT if available, else stdout
    output_target = os.environ.get("LLM_OUTPUT", "")
    if output_target and output_target != "/dev/stdout":
        try:
            with open(output_target, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
        except OSError as e:
            print(f"Failed to write to LLM_OUTPUT: {e}", file=sys.stderr)
            print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result, indent=2))

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(_cli())
