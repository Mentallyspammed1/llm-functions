#!/usr/bin/env python3
# ==============================================================================
# sigoden_tool_creator.py — AIChat Tool Generator & Execution Engine v3.1.0
# argc/aichat compatible · Colorized UI · Safe Caching · Agent CWD Resolution
#
# @describe Enterprise tool generator for sigoden/aichat featuring strict schema export, path validation, and resilient JSONL routing.
# @meta require-tools aichat jq
#
# @option --target! <PATH>               Target file or directory path (required)
# @option --mode <MODE>                  Execution mode: summary/detailed (default: summary)
# @option --limit <NUM>                  Maximum items to process (default: 100)
# @flag   --use-cache                    Enable result caching
# @flag   --schema                       Print JSON Tool Schema for LLM registration and exit
# @flag   --json-only                    Output raw JSON/JSONL only
# @flag   --verbose                      Enable detailed debug log output
#
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# @env LLM_TOOL_MODE=summary             Default execution mode override
# ==============================================================================

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

__version__ = "3.1.0"
__all__ = ["ToolCache", "ToolError", "execute_tool", "generate_tool_schema", "main", "run"]

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_INVALID_INPUT = 127
EXIT_INTERRUPT = 130

NEON_CYAN = "\033[38;5;51m"
NEON_GREEN = "\033[38;5;46m"
NEON_RED = "\033[38;5;196m"
NEON_PURPLE = "\033[38;5;129m"
RESET = "\033[0m"
BOLD = "\033[1m"

VALID_MODES = {"summary", "detailed"}
SUMMARY_ITEM_PREVIEW = 5


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("LLM_NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


def colorize(text: str, color: str) -> str:
    if not _color_enabled():
        return text
    return f"{color}{text}{RESET}"


def configure_logging(verbose: bool, json_only: bool) -> None:
    if json_only and not verbose:
        logging.disable(logging.CRITICAL)
        return

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )


class ToolError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_ERROR, trace_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.trace_id = trace_id or str(uuid.uuid4())

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": self.message,
            "exit_code": self.exit_code,
            "trace_id": self.trace_id,
        }


class ToolJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, datetime):
            return obj.isoformat()
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        return super().default(obj)


class ToolCache:
    _CACHE_VERSION = "1"
    _CACHE_ENVELOPE_KEY = "__sigoden_tool_cache__"

    def __init__(self, cache_dir: Optional[str] = None) -> None:
        default_cache_dir = os.path.join(tempfile.gettempdir(), "aichat_tools")
        with contextlib.suppress(Exception):
            default_cache_dir = str(Path.home() / ".cache" / "aichat_tools")

        raw_cache_dir = cache_dir or os.environ.get("LLM_TOOL_CACHE_DIR") or default_cache_dir
        self.cache_dir = Path(raw_cache_dir).expanduser()
        self.ttl = self._parse_ttl(os.environ.get("LLM_TOOL_CACHE_TTL", ""))

        with contextlib.suppress(OSError):
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _parse_ttl(raw: str) -> Optional[int]:
        raw = str(raw or "").strip()
        if raw.isdigit():
            value = int(raw)
            if value > 0:
                return value
        return None

    def _hash_key(self, key: str) -> str:
        return hashlib.sha256(f"{self._CACHE_VERSION}:{key}".encode()).hexdigest()

    def get(self, key: str) -> Optional[dict[str, Any]]:
        file_path = self.cache_dir / f"{self._hash_key(key)}.json"

        try:
            if not file_path.exists():
                return None

            if self.ttl is not None:
                age = time.time() - file_path.stat().st_mtime
                if age > self.ttl:
                    return None

            with open(file_path, encoding="utf-8") as fp:
                payload = json.load(fp)

            if isinstance(payload, dict) and payload.get(self._CACHE_ENVELOPE_KEY) == self._CACHE_VERSION:
                data = payload.get("data")
                return data if isinstance(data, dict) else None

            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def set(self, key: str, value: Any) -> None:
        file_path = self.cache_dir / f"{self._hash_key(key)}.json"
        envelope = {
            self._CACHE_ENVELOPE_KEY: self._CACHE_VERSION,
            "created": _utc_timestamp(),
            "data": value,
        }

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.cache_dir),
                prefix=".tmp_",
                suffix=".json",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(envelope, fp, cls=ToolJSONEncoder, ensure_ascii=False)
            os.replace(tmp_path, file_path)
        except Exception:
            with contextlib.suppress(OSError):
                if tmp_path and Path(tmp_path).exists():
                    Path(tmp_path).unlink()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def generate_tool_schema() -> dict[str, Any]:
    return {
        "name": "sigoden_tool_creator",
        "description": "Enterprise tool generator and execution harness for sigoden/aichat.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Target filesystem path.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["summary", "detailed"],
                    "description": "Execution verbosity.",
                    "default": "summary",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum items to process.",
                    "minimum": 0,
                    "default": 100,
                },
                "use_cache": {
                    "type": "boolean",
                    "description": "Enable caching.",
                    "default": False,
                },
                "verbose": {
                    "type": "boolean",
                    "description": "Enable debug logging.",
                    "default": False,
                },
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    }


def _normalize_mode(mode: Optional[str]) -> str:
    candidate = str(mode or os.environ.get("LLM_TOOL_MODE") or "summary").strip().lower()
    if candidate not in VALID_MODES:
        raise ToolError(
            f"Invalid mode '{mode}'. Expected one of: summary, detailed.",
            exit_code=EXIT_INVALID_INPUT,
        )
    return candidate


def _normalize_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except Exception as exc:
        raise ToolError(
            f"Invalid limit value: {limit!r}. Expected an integer.",
            exit_code=EXIT_INVALID_INPUT,
        ) from exc

    if value < 0:
        raise ToolError(
            f"Invalid limit value: {value}. Limit must be >= 0.",
            exit_code=EXIT_INVALID_INPUT,
        )

    return value


def _resolve_target(target: Any, trace_id: str) -> Path:
    if target is None:
        raise ToolError(
            "Missing required argument: target",
            exit_code=EXIT_INVALID_INPUT,
            trace_id=trace_id,
        )

    raw_target = str(target).strip()
    if not raw_target:
        raise ToolError(
            "Target path cannot be empty",
            exit_code=EXIT_INVALID_INPUT,
            trace_id=trace_id,
        )

    try:
        target_path = Path(raw_target).expanduser().resolve(strict=False)
    except OSError as exc:
        raise ToolError(
            f"Unable to resolve target path: {raw_target} ({exc})",
            exit_code=EXIT_INVALID_INPUT,
            trace_id=trace_id,
        ) from exc

    if not target_path.exists():
        raise ToolError(
            f"Target path not found: {raw_target}",
            exit_code=EXIT_ERROR,
            trace_id=trace_id,
        )

    return target_path


def _collect_items(target_path: Path, limit: int, trace_id: str) -> list[str]:
    if limit <= 0:
        return []

    if target_path.is_dir():
        items: list[str] = []

        try:
            with os.scandir(target_path) as entries:
                for entry in entries:
                    if len(items) >= limit:
                        break
                    items.append(str(Path(entry.path)))
        except PermissionError as exc:
            raise ToolError(
                f"Permission denied while reading directory: {target_path}",
                exit_code=EXIT_ERROR,
                trace_id=trace_id,
            ) from exc
        except OSError as exc:
            raise ToolError(
                f"Unable to read directory: {target_path} ({exc})",
                exit_code=EXIT_ERROR,
                trace_id=trace_id,
            ) from exc

        items.sort()
        return items

    return [str(target_path)]


def execute_tool(
    target: str,
    mode: str = "summary",
    limit: int = 100,
    use_cache: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    start_time = time.monotonic()
    req_id = f"req_{uuid.uuid4().hex[:8]}"

    try:
        normalized_mode = _normalize_mode(mode)
        normalized_limit = _normalize_limit(limit)
        target_path = _resolve_target(target, req_id)

        logging.debug(
            "tool=sigoden_tool_creator request_id=%s target=%s mode=%s limit=%s cache=%s verbose=%s",
            req_id,
            target_path,
            normalized_mode,
            normalized_limit,
            use_cache,
            verbose,
        )

        cache = ToolCache()
        cache_key = f"{target_path}|{normalized_mode}|{normalized_limit}|{__version__}"

        if use_cache:
            cached = cache.get(cache_key)
            if isinstance(cached, dict):
                cached["cached"] = True
                cached["request_id"] = req_id
                cached["duration_ms"] = round((time.monotonic() - start_time) * 1000, 2)
                logging.debug("cache hit request_id=%s key=%s", req_id, cache_key)
                return cached

        items = _collect_items(target_path, normalized_limit, req_id)

        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        result = {
            "success": True,
            "request_id": req_id,
            "timestamp": _utc_timestamp(),
            "target": str(target_path),
            "mode": normalized_mode,
            "count": len(items),
            "items": items if normalized_mode == "detailed" else items[:SUMMARY_ITEM_PREVIEW],
            "cached": False,
            "duration_ms": duration_ms,
            "exit_code": EXIT_SUCCESS,
        }

        if use_cache:
            cache.set(cache_key, result)
            logging.debug("cache set request_id=%s key=%s", req_id, cache_key)

        return result

    except ToolError as exc:
        exc.trace_id = req_id
        logging.error("%s", exc.message)
        return exc.to_dict()

    except Exception as exc:
        logging.exception("Unexpected tool failure")
        return ToolError(
            f"Unexpected error: {exc}",
            exit_code=EXIT_ERROR,
            trace_id=req_id,
        ).to_dict()


run = execute_tool


def write_llm_output(data: dict[str, Any]) -> None:
    try:
        payload = json.dumps(data, cls=ToolJSONEncoder, ensure_ascii=False)
    except Exception:
        payload = json.dumps(
            {
                "success": False,
                "error": "Unserializable tool result",
                "raw": str(data),
            },
            ensure_ascii=False,
        )

    out = str(os.environ.get("LLM_OUTPUT") or "/dev/stdout").strip() or "/dev/stdout"

    try:
        if out in {"/dev/stdout", "/dev/fd/1", "-", "stdout"}:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()
            return

        if out in {"/dev/stderr", "/dev/fd/2", "stderr"}:
            sys.stderr.write(payload + "\n")
            sys.stderr.flush()
            return

        out_path = Path(out).expanduser()

        with contextlib.suppress(OSError):
            out_path.parent.mkdir(parents=True, exist_ok=True)

        with open(out_path, "a", encoding="utf-8") as fp:
            fp.write(payload + "\n")

        return

    except Exception:
        try:
            sys.stderr.write(payload + "\n")
        except Exception:
            pass


class ToolArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        err = ToolError(
            f"Argument error: {message}",
            exit_code=EXIT_INVALID_INPUT,
        ).to_dict()

        write_llm_output(err)
        self.print_usage(sys.stderr)
        self.exit(EXIT_INVALID_INPUT, f"{self.prog}: error: {message}\n")


def main() -> int:
    default_mode = str(os.environ.get("LLM_TOOL_MODE") or "summary").strip().lower()
    if default_mode not in VALID_MODES:
        default_mode = "summary"

    parser = ToolArgumentParser(
        prog="sigoden_tool_creator",
        description="AIChat Tool Creator & Engine",
    )
    parser.add_argument("--target", "-t", help="Target path")
    parser.add_argument("--mode", choices=["summary", "detailed"], default=default_mode)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--use-cache", action="store_true", default=False)
    parser.add_argument("--schema", action="store_true", default=False)
    parser.add_argument("--json-only", action="store_true", default=False)
    parser.add_argument("--verbose", "-v", action="store_true", default=False)

    try:
        args = parser.parse_args()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return EXIT_SUCCESS
        if isinstance(code, int):
            return code
        return EXIT_INVALID_INPUT

    configure_logging(args.verbose, args.json_only)

    if args.schema:
        try:
            sys.stdout.write(
                json.dumps(
                    generate_tool_schema(),
                    indent=2,
                    cls=ToolJSONEncoder,
                    ensure_ascii=False,
                )
                + "\n"
            )
            sys.stdout.flush()
        except BrokenPipeError:
            return EXIT_SUCCESS
        return EXIT_SUCCESS

    if not args.target:
        err = ToolError(
            "Missing required argument: --target",
            exit_code=EXIT_INVALID_INPUT,
        ).to_dict()

        if not args.json_only:
            sys.stderr.write(
                colorize("✖ [AIChat Tool Error]", NEON_RED)
                + f" {err.get('error')}\n"
            )

        write_llm_output(err)
        return EXIT_INVALID_INPUT

    try:
        res = execute_tool(
            target=args.target,
            mode=args.mode,
            limit=args.limit,
            use_cache=args.use_cache,
            verbose=args.verbose,
        )
    except KeyboardInterrupt:
        err = ToolError(
            "Interrupted by user",
            exit_code=EXIT_INTERRUPT,
        ).to_dict()

        if not args.json_only:
            sys.stderr.write(colorize("✖ [AIChat Tool Interrupt]", NEON_RED) + "\n")

        write_llm_output(err)
        return EXIT_INTERRUPT

    except Exception as exc:
        err = ToolError(
            f"Fatal execution error: {exc}",
            exit_code=EXIT_ERROR,
        ).to_dict()

        if not args.json_only:
            sys.stderr.write(
                colorize("✖ [AIChat Tool Error]", NEON_RED)
                + f" {err.get('error')}\n"
            )

        write_llm_output(err)
        return EXIT_ERROR

    if not args.json_only:
        if res.get("success"):
            sys.stderr.write(
                colorize("⚡ [AIChat Tool Success]", NEON_PURPLE)
                + f" Target: {res.get('target')}"
                + f" | items: {res.get('count')}"
                + f" | {res.get('duration_ms')}ms\n"
            )
        else:
            sys.stderr.write(
                colorize("✖ [AIChat Tool Error]", NEON_RED)
                + f" {res.get('error')}\n"
            )

    write_llm_output(res)

    exit_code = res.get("exit_code", EXIT_SUCCESS)
    try:
        return int(exit_code)
    except Exception:
        return EXIT_ERROR


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPT)
