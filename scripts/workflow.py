#!/usr/bin/env python3
"""Daily Ruijie question-bank workflow helper.

This script intentionally handles only mechanical file-state decisions. The
agent still performs script repair, FAIL triage, and PASS final review.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


STATE_FILE = ".workflow_state.json"
SKILL_FILE = Path(__file__).resolve().parent.parent / "SKILL.md"
SKILL_RECEIVE_DIR_RE = re.compile(r"(?m)^-\s*\*\*日志接收目录\*\*:\s*`([^`]*)`\s*$")
PASS_ANOMALY_PATTERNS = [
    (r"%\s*Invalid input", "% Invalid input"),
    (r"%\s*Unknown command\.?", "% Unknown command."),
    (r"%\s*Unknowm command\.?", "% Unknowm command."),
    (r"incomplete command", "incomplete command"),
    (r"ambiguous command", "ambiguous command"),
    (r"bad parameter", "Bad parameter"),
    (r"does not exist", "does not exist"),
    (r"can't find", "can't find"),
    (r"Traceback", "Traceback"),
    (r"AttributeError", "AttributeError"),
    (r"TypeError", "TypeError"),
]
FRAMEWORK_LOG_MARKERS = [
    "设备系统无异常",
    "设备无coredump",
    "配置对比通过",
    "内存显示",
    "show pbi count error",
    "show cpu core",
    "show memory low-watermark",
    "当前配置与初始配置",
]
ALLOWED_SCRIPT_FUNCTIONS = {
    "setup_class",
    "teardown_class",
    "setup_method",
    "teardown_method",
    "test_process",
    "_on_error",
}
FORBIDDEN_COMMAND_PATTERNS = [
    (r"\bterminal\s+monitor\b", "terminal monitor"),
    (r"\blogging\s+on\b", "logging on"),
    (r"\blogging\s+monitor\b", "logging monitor"),
    (r"\blogging\s+console\b", "logging console"),
]
ABBREVIATED_COMMAND_PATTERNS = [
    (r"^\s*sh\s+", "sh"),
    (r"^\s*conf(?:\s+t|\s+terminal)?\s*$", "conf"),
    (r"^\s*int\s+", "int"),
]
HARDCODED_INTERFACE_RE = re.compile(
    r"\b(?:TFGigabitEthernet|TenGigabitEthernet|FortyGigabitEthernet|HundredGigabitEthernet|"
    r"GigabitEthernet|FastEthernet|Ethernet)\s*\d+(?:/\d+)+(?:\.\d+)?\b",
    re.I,
)


def file_time_ns(path: Path) -> int:
    stat = path.stat()
    birth = getattr(stat, "st_birthtime", None)
    if birth is not None:
        return int(birth * 1_000_000_000)
    return stat.st_ctime_ns


def mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


def ensure_dirs(date_dir: Path) -> None:
    for name in ("bak", "py", "log", "done", "check", "ignore"):
        (date_dir / name).mkdir(parents=True, exist_ok=True)


def load_state(date_dir: Path) -> dict[str, Any]:
    path = date_dir / STATE_FILE
    if not path.exists():
        return {"completed": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"completed": []}


def save_state(date_dir: Path, state: dict[str, Any]) -> None:
    path = date_dir / STATE_FILE
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_receive_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise SystemExit(f"日志接收目录不存在: {resolved}")
    if not resolved.is_dir():
        raise SystemExit(f"日志接收路径不是目录: {resolved}")
    return resolved


def read_skill_receive_dir(skill_file: Path = SKILL_FILE) -> Path | None:
    if not skill_file.exists():
        return None
    text = skill_file.read_text(encoding="utf-8", errors="ignore")
    match = SKILL_RECEIVE_DIR_RE.search(text)
    if not match:
        return None
    configured = match.group(1).strip()
    if not configured or configured in {"未设置", "待设置"}:
        return None
    return validate_receive_dir(Path(configured))


def save_receive_dir(date_dir: Path, receive_dir: Path) -> Path:
    resolved = validate_receive_dir(receive_dir)
    state = load_state(date_dir)
    state["log_receive_dir"] = str(resolved)
    save_state(date_dir, state)
    return resolved


def resolve_receive_dir(date_dir: Path, override: Path | None = None) -> Path:
    if override is not None:
        return save_receive_dir(date_dir, override)

    skill_receive_dir = read_skill_receive_dir()
    if skill_receive_dir is not None:
        return skill_receive_dir

    configured = load_state(date_dir).get("log_receive_dir")
    if not isinstance(configured, str) or not configured.strip():
        raise SystemExit(
            "尚未配置日志接收目录，请填写 SKILL.md 中的“日志接收目录”；"
            "也可兼容使用 init --receive-dir、set-receive-dir 或 receive-log --receive-dir。"
        )
    return validate_receive_dir(Path(configured))


def py_files(path: Path) -> list[Path]:
    return sorted(path.glob("*.py"), key=lambda p: p.name)


def current_py(date_dir: Path) -> Path | None:
    files = py_files(date_dir / "py")
    if len(files) > 1:
        raise SystemExit(f"py/ 中存在多个脚本，请先整理为单文件工作台: {[p.name for p in files]}")
    return files[0] if files else None


def detect_log_status(path: Path) -> str:
    name = path.name.upper()
    if "PASS" in name:
        return "PASS"
    if "FAIL" in name:
        return "FAIL"

    text = path.read_text(encoding="utf-8", errors="ignore")
    normalized = re.sub(r"\s+", "", text.upper())
    if "TCRESULT:PASS" in normalized or "测试结果：PASS" in text or "测试结果:PASS" in normalized:
        return "PASS"
    if "TCRESULT:FAIL" in normalized or "测试结果：FAIL" in text or "测试结果:FAIL" in normalized:
        return "FAIL"
    return "UNKNOWN"


def latest_matching_log(date_dir: Path, py_path: Path) -> Path | None:
    prefix = py_path.stem
    logs = sorted(
        (date_dir / "log").glob(f"{prefix}*.txt"),
        key=lambda path: (mtime_ns(path), file_time_ns(path), path.name),
    )
    return logs[-1] if logs else None


def latest_received_log(receive_dir: Path, py_path: Path) -> Path | None:
    prefix = py_path.stem
    logs = [
        path for path in receive_dir.rglob("*")
        if path.is_file() and path.suffix.lower() == ".txt" and path.name.startswith(prefix)
    ]
    return max(
        logs,
        key=lambda path: (mtime_ns(path), file_time_ns(path), str(path.relative_to(receive_dir))),
        default=None,
    )


def clear_log_dir(date_dir: Path, keep_path: Path) -> list[str]:
    log_dir = (date_dir / "log").resolve()
    resolved_date_dir = date_dir.resolve()
    if log_dir.parent != resolved_date_dir or log_dir.name.lower() != "log":
        raise SystemExit(f"拒绝清理未通过范围校验的日志目录: {log_dir}")

    keep_resolved = keep_path.resolve()
    removed: list[str] = []
    for path in log_dir.iterdir():
        if path.resolve() == keep_resolved:
            continue
        removed.append(str(path))
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    return removed


def clear_check_dir(date_dir: Path) -> list[str]:
    check_dir = (date_dir / "check").resolve()
    resolved_date_dir = date_dir.resolve()
    if check_dir.parent != resolved_date_dir or check_dir.name.lower() != "check":
        raise SystemExit(f"拒绝清理未通过范围校验的 EXE Check 目录: {check_dir}")

    removed: list[str] = []
    for path in check_dir.iterdir():
        removed.append(str(path))
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    return removed


def latest_done_log(done_py_path: Path) -> Path | None:
    log_dir = done_py_path.parent / "Log"
    if not log_dir.exists():
        return None
    logs = sorted(log_dir.glob(f"{done_py_path.stem}*.txt"), key=file_time_ns)
    return logs[-1] if logs else None


def ignore_case_dir(date_dir: Path, py_name: str) -> Path:
    return date_dir / "ignore" / Path(py_name).stem


def write_error_json(path: Path, py_name: str, fail_reason: str) -> None:
    payload = {
        "pyName": py_name,
        "failReason": fail_reason,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")


def prepare_ignore_case_dir(date_dir: Path, py_name: str, overwrite: bool = False) -> Path:
    target_dir = ignore_case_dir(date_dir, py_name)
    if target_dir.exists() and not overwrite:
        raise SystemExit(f"ignore 归档目录已存在，避免覆盖: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "Log").mkdir(parents=True, exist_ok=True)
    return target_dir


def find_done_py(date_dir: Path, py_name: str) -> Path:
    matches = sorted((date_dir / "done").rglob(py_name), key=lambda p: str(p))
    if not matches:
        raise SystemExit(f"done/ 中找不到退回脚本: {py_name}")
    if len(matches) > 1:
        raise SystemExit(f"done/ 中存在多个同名脚本，请人工确认: {py_name} -> {[str(p) for p in matches]}")
    return matches[0]


def load_reject_items(args: argparse.Namespace) -> list[dict[str, str]]:
    if args.items_file and args.items_json:
        raise SystemExit("reject-done 只能使用 --items-file 或 --items-json 其中一种。")
    if args.items_file:
        raw = args.items_file.read_text(encoding="utf-8")
    elif args.items_json:
        raw = args.items_json
    else:
        raise SystemExit("reject-done 需要 --items-file 或 --items-json。")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"退回列表不是有效 JSON: {exc}") from exc
    if not isinstance(data, list):
        raise SystemExit("退回列表必须是 JSON 数组。")

    items: list[dict[str, str]] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"第 {index} 项必须是 JSON 对象。")
        py_name = item.get("pyName")
        fail_reason = item.get("failReason")
        if not isinstance(py_name, str) or not py_name.endswith(".py"):
            raise SystemExit(f"第 {index} 项 pyName 必须是 .py 文件名。")
        if not isinstance(fail_reason, str):
            raise SystemExit(f"第 {index} 项 failReason 必须是字符串。")
        items.append({"pyName": Path(py_name).name, "failReason": fail_reason})
    return items


def next_bak(date_dir: Path) -> Path | None:
    state = load_state(date_dir)
    completed = set(state.get("completed", []))
    done_names = {p.name for p in (date_dir / "done").rglob("*.py")}
    ignore_names = {p.name for p in (date_dir / "ignore").rglob("*.py")}
    active_names = {p.name for p in py_files(date_dir / "py")}
    for path in py_files(date_dir / "bak"):
        if path.name in completed or path.name in done_names or path.name in ignore_names or path.name in active_names:
            continue
        return path
    return None


def parse_srs_fields(py_path: Path) -> dict[str, str]:
    text = py_path.read_text(encoding="utf-8", errors="ignore")
    fields: dict[str, str] = {}
    patterns = {
        "level1": r"@srs一级规格\s*:\s*(.+)",
        "level2": r"@srs二级规格\s*:\s*(.+)",
        "level3": r"@srs三级规格\s*:\s*(.+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if not match:
            raise SystemExit(f"无法从 {py_path} 解析 {pattern}")
        fields[key] = match.group(1).strip()
    return fields


def read_final_script_text(py_path: Path) -> str:
    text = py_path.read_text(encoding="utf-8", errors="ignore")
    return re.sub(r"(?m)^\s*@具体UI命令[^:]*:.*\n", "", text)


def extract_case_level(text: str) -> str | None:
    match = re.search(r"@用例等级\s*:\s*(L[0-3])\b", text, re.I)
    return match.group(1).upper() if match else None


def extract_runtime_minutes(log_path: Path | None, log_text: str = "") -> int | None:
    candidates = []
    if log_path is not None:
        candidates.append(log_path.name)
    if log_text:
        candidates.append(log_text)

    patterns = [
        r"用时\((\d+)\)",
        r"用时[:：]?\s*(\d+)\s*(?:分钟|min|minute)",
        r"耗时[:：]?\s*(\d+)\s*(?:分钟|min|minute)",
    ]
    for text in candidates:
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return int(match.group(1))
    return None


def recommended_case_level(runtime_minutes: int | None, current_level: str | None) -> str | None:
    if runtime_minutes is None:
        return current_level
    current = current_level.upper() if current_level else None
    if runtime_minutes > 10 and current in {"L0", "L1"}:
        return "L2"
    if runtime_minutes > 10 and current is None:
        return "L2"
    return current


def update_case_level(text: str, level: str | None) -> str:
    if not level:
        return text
    if re.search(r"@用例等级\s*:", text):
        return re.sub(r"(@用例等级\s*:\s*)L[0-3]\b", rf"\g<1>{level}", text, count=1, flags=re.I)
    return text


def build_case_level_report(py_path: Path | None, log_path: Path | None) -> dict[str, Any]:
    script_text = py_path.read_text(encoding="utf-8", errors="ignore") if py_path else ""
    log_text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path and log_path.exists() else ""
    script_level = extract_case_level(script_text)
    log_level = extract_case_level(log_text)
    runtime_minutes = extract_runtime_minutes(log_path, log_text)
    recommended = recommended_case_level(runtime_minutes, script_level)
    report: dict[str, Any] = {
        "script_case_level": script_level,
        "log_case_level": log_level,
        "runtime_minutes": runtime_minutes,
        "recommended_case_level": recommended,
        "needs_script_level_update": bool(script_level and recommended and script_level != recommended),
        "notes": [],
    }
    if runtime_minutes is not None and runtime_minutes > 10:
        report["notes"].append("日志用时超过 10 分钟，L0/L1 脚本需要提升为 L2。")
    if runtime_minutes is not None and runtime_minutes > 30:
        report["notes"].append("日志用时超过 30 分钟，超出 L2/L3 运行时间要求，请人工关注。")
    if script_level and log_level and script_level != log_level:
        report["notes"].append("脚本和日志中的 @用例等级 不一致，终验时需要核对。")
    return report


def decide_next_action(status: dict[str, Any]) -> str:
    if not status.get("current_py"):
        return "ADVANCE_FROM_BAK" if status.get("next_bak") else "QUEUE_DONE"
    if status.get("log_status") == "NO_LOG":
        return "WAIT_EXECUTOR_LOG"
    if status.get("log_status") == "FAIL":
        return "ANALYZE_FAIL_LOG"
    if status.get("log_status") == "PASS":
        return "FINAL_VERIFY_PASS"
    return "INSPECT_LOG_STATUS"


def build_status(date_dir: Path) -> dict[str, Any]:
    ensure_dirs(date_dir)
    py_path = current_py(date_dir)
    ignored_paths = sorted((date_dir / "ignore").rglob("*.py"), key=lambda p: p.name)
    result: dict[str, Any] = {
        "date_dir": str(date_dir),
        "current_py": None,
        "latest_log": None,
        "log_status": "NO_PY",
        "log_is_fresh": False,
        "next_bak": None,
        "next_action": None,
        "ignore_enabled": (date_dir / "ignore").exists(),
        "ignore_count": len(ignored_paths),
        "ignored_names_sample": [p.name for p in ignored_paths[:20]],
    }
    nxt = next_bak(date_dir)
    result["next_bak"] = nxt.name if nxt else None
    if py_path is None:
        result["log_status"] = "WAITING_ADVANCE" if nxt else "DONE"
        result["next_action"] = decide_next_action(result)
        return result

    result["current_py"] = py_path.name
    log_path = latest_matching_log(date_dir, py_path)
    if log_path is None:
        result["log_status"] = "NO_LOG"
        result["next_action"] = decide_next_action(result)
        return result

    log_created_time = file_time_ns(log_path)
    log_modified_time = mtime_ns(log_path)
    py_time = mtime_ns(py_path)
    result.update(
        {
            "latest_log": log_path.name,
            "latest_log_created_ns": log_created_time,
            "latest_log_modified_ns": log_modified_time,
            "current_py_modified_ns": py_time,
            "log_status": detect_log_status(log_path),
            "log_is_fresh": log_modified_time >= py_time,
        }
    )
    result["next_action"] = decide_next_action(result)
    return result


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{}")
        return "".join(parts)
    return None


def list_literal_strings(node: ast.AST) -> list[str]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    values: list[str] = []
    for item in node.elts:
        value = literal_string(item)
        if value is not None:
            values.append(value)
    return values


def has_enable_before_config(commands: list[str]) -> bool:
    lowered = [cmd.strip().lower() for cmd in commands]
    if contains_config_command(commands):
        return bool(lowered and lowered[0] in {"enable", "en"})
    return True


def contains_config_command(commands: list[str]) -> bool:
    config_patterns = [
        r"^configure terminal$",
        r"^interface\b",
        r"^default interface\b",
        r"^router\b",
        r"^address-family\b",
        r"^ip route\b",
        r"^ipv6 route\b",
        r"^mpls\b",
        r"^no\b",
        r"^vlan\b",
        r"^vrf\b",
        r"^rd\b",
        r"^route-target\b",
        r"^neighbor\b",
        r"^network\b",
        r"^redistribute\b",
        r"^shutdown$",
        r"^no shutdown$",
    ]
    for command in commands:
        normalized = command.strip().lower()
        if any(re.search(pattern, normalized) for pattern in config_patterns):
            return True
    return False


def is_show_command(command: str) -> bool:
    normalized = command.strip().lower()
    return normalized.startswith(("show ", "display ", "more "))


def target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for item in target.elts:
            names.extend(target_names(item))
        return names
    return []


def assignment_target_text(target: ast.AST) -> str:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        base = assignment_target_text(target.value)
        return f"{base}.{target.attr}" if base else target.attr
    if isinstance(target, (ast.Tuple, ast.List)):
        return ",".join(assignment_target_text(item) for item in target.elts)
    return ""


def assigned_command_list_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        names.extend(target_names(target))
    return names


def call_uses_star_name(node: ast.Call, name: str) -> bool:
    for arg in node.args:
        if isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name) and arg.value.id == name:
            return True
    return False


def nearest_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return None


def assert_mentions_fail(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and "FAIL" in child.value.upper():
            return True
    return False


def assert_mentions_pass(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and "PASS" in child.value.upper():
            return True
    return False


def raw_output_assert_reason(node: ast.Assert) -> str | None:
    text = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
    lowered = text.lower()
    if "pass" in lowered or "fail" in lowered:
        return None
    if isinstance(node.test, ast.Call) and dotted_name(node.test.func).startswith("re."):
        return "禁止直接用原生 assert 校验 re 匹配结果，应使用公共库判断函数后断言 PASS。"
    for child in ast.walk(node.test):
        if isinstance(child, ast.Call) and dotted_name(child.func).startswith("re."):
            return "禁止在 assert 中直接使用 re.search/re.match 校验设备回显，应使用公共库判断函数。"
        if isinstance(child, ast.Call) and dotted_name(child.func) == "len":
            return "禁止用 assert len(...) 作为回显验证，应使用公共库判断函数后断言 PASS。"
    if isinstance(node.test, (ast.Compare, ast.BoolOp)):
        if re.search(r"\b(?:output|show_info|result_str|data|ret|response|info)\b", lowered):
            return "禁止直接对设备回显做原生 assert 文本/列表判断，应使用公共库判断函数后断言 PASS。"
    return None


def extract_doc_value(text: str, label: str) -> str | None:
    match = re.search(rf"{re.escape(label)}\s*[:：]\s*(.+)", text)
    return match.group(1).strip() if match else None


def extract_class_setup_topology(tree: ast.AST) -> tuple[int, str] | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if dotted_name(node.func).endswith("class_setup"):
            for arg in node.args:
                value = literal_string(arg)
                if value:
                    return node.lineno, value
    return None


def is_class_setup_assignment(node: ast.Assign | ast.AnnAssign) -> bool:
    if not isinstance(node.value, ast.Call) or not dotted_name(node.value.func).endswith("class_setup"):
        return False
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        if isinstance(target, (ast.Tuple, ast.List)):
            names.extend(assignment_target_text(item) for item in target.elts)
        else:
            names.append(assignment_target_text(target))
    return names == ["self.tb", "self.test_case"]


def check_setup_class_shape(tree: ast.AST) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "setup_class":
            continue
        has_class_setup = False
        for stmt in node.body:
            if isinstance(stmt, (ast.Expr, ast.Pass)):
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                    continue
                if isinstance(stmt, ast.Pass):
                    continue
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and is_class_setup_assignment(stmt):
                has_class_setup = True
                continue
            issues.append({
                "code": "SETUP_CLASS_EXTRA_LOGIC",
                "message": "setup_class 只应包含 self.tb, self.test_case = class_setup_deardown.class_setup(...)，禁止多余赋值或调用。",
                "line": getattr(stmt, "lineno", node.lineno),
            })
        if not has_class_setup:
            issues.append({
                "code": "SETUP_CLASS_MISSING_CLASS_SETUP",
                "message": "setup_class 缺少 class_setup_deardown.class_setup(...) 主线调用。",
                "line": node.lineno,
            })
    return issues


def value_uses_self_tb(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and dotted_name(node).startswith("self.tb."):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(value_uses_self_tb(item) for item in node.elts)
    return False


def is_step_expect_with(node: ast.With) -> bool:
    for item in node.items:
        expr = item.context_expr
        if isinstance(expr, ast.Call) and dotted_name(expr.func).endswith(".expect"):
            return True
    return False


def is_casestep_with(node: ast.With) -> bool:
    for item in node.items:
        expr = item.context_expr
        if isinstance(expr, ast.Call) and dotted_name(expr.func).endswith(".casestep"):
            return True
    return False


def local_string_bindings(func: ast.AST) -> dict[str, str]:
    """Collect simple local name -> string constant assignments in a function."""
    bindings: dict[str, str] = {}
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return bindings
    for node in func.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = literal_string(node.value)
            if value is not None:
                bindings[node.targets[0].id] = value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            value = literal_string(node.value)
            if value is not None:
                bindings[node.target.id] = value
    return bindings


def resolve_string_expr(node: ast.AST, bindings: dict[str, str] | None = None) -> str | None:
    value = literal_string(node)
    if value is not None:
        return value
    if bindings and isinstance(node, ast.Name):
        return bindings.get(node.id)
    return None


def call_first_string_arg(call: ast.Call, bindings: dict[str, str] | None = None) -> str | None:
    if call.args:
        return resolve_string_expr(call.args[0], bindings)
    for keyword in call.keywords:
        if keyword.arg in {None, "msg", "step_msg", "expect", "text"}:
            value = resolve_string_expr(keyword.value, bindings)
            if value is not None:
                return value
    return None


def step_context_text(node: ast.With, suffix: str, bindings: dict[str, str] | None = None) -> str | None:
    for item in node.items:
        expr = item.context_expr
        if isinstance(expr, ast.Call) and dotted_name(expr.func).endswith(suffix):
            return call_first_string_arg(expr, bindings)
    return None


def has_cleanup_command_call(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = dotted_name(child.func)
        if name.endswith(".command") or name.endswith(".check_write_cmd"):
            return True
    return False


TEARDOWN_CASESTEP_TEXTS = {
    "配置清空",
    "配置清除",
    "清理配置",
}
TEARDOWN_EXPECT_TEXTS = {
    "配置清除成功",
    "配置清空成功",
    "清理成功",
}


def is_teardown_cleanup_text(text: str | None, *, kind: str) -> bool:
    if not text:
        return False
    normalized = text.strip()
    if kind == "casestep":
        return (
            normalized in TEARDOWN_CASESTEP_TEXTS
            or "配置清空" in normalized
            or "配置清除" in normalized
            or "清理" in normalized
        )
    return (
        normalized in TEARDOWN_EXPECT_TEXTS
        or "配置清除" in normalized
        or "配置清空" in normalized
        or "清理成功" in normalized
    )


def is_teardown_cleanup_expect(node: ast.With, parents: dict[ast.AST, ast.AST]) -> bool:
    if nearest_function(node, parents) != "teardown_method":
        return False
    # Resolve step_msg = "配置清除成功" style locals in the enclosing function.
    current = parents.get(node)
    bindings: dict[str, str] = {}
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings = local_string_bindings(current)
            break
        current = parents.get(current)
    expect_text = step_context_text(node, ".expect", bindings)
    return is_teardown_cleanup_text(expect_text, kind="expect")


def check_teardown_method_structure(tree: ast.AST) -> list[dict[str, Any]]:
    """Enforce teardown_method shell: casestep + expect + cleanup action."""
    issues: list[dict[str, Any]] = []
    teardown_nodes = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "teardown_method"
    ]
    if not teardown_nodes:
        issues.append({
            "code": "TEARDOWN_METHOD_MISSING",
            "message": "缺少 teardown_method；应使用 casestep('配置清空') + expect('配置清除成功') 做逆序清理。",
            "line": 1,
        })
        return issues

    for teardown in teardown_nodes:
        has_casestep = False
        has_cleanup_expect = False
        has_cleanup_cmd = has_cleanup_command_call(teardown)
        bindings = local_string_bindings(teardown)

        for node in ast.walk(teardown):
            if not isinstance(node, ast.With):
                continue
            if is_casestep_with(node):
                step_text = (step_context_text(node, ".casestep", bindings) or "").strip()
                if is_teardown_cleanup_text(step_text, kind="casestep"):
                    has_casestep = True
            if is_step_expect_with(node):
                expect_text = (step_context_text(node, ".expect", bindings) or "").strip()
                if is_teardown_cleanup_text(expect_text, kind="expect"):
                    has_cleanup_expect = True

        if not has_casestep or not has_cleanup_expect:
            issues.append({
                "code": "TEARDOWN_SHELL_MISSING",
                "message": (
                    "teardown_method 必须使用 casestep('配置清空') + expect('配置清除成功') 步骤壳；"
                    "禁止为消 lint 删除该结构后只保留裸清理命令。"
                ),
                "line": teardown.lineno,
            })
        elif not has_cleanup_cmd:
            # 允许仅查询类 teardown（如告警查询）在 expect 内用公共库 assert；
            # 配置恢复可只下发 command，不要求校验或断言。
            asserts = [child for child in ast.walk(teardown) if isinstance(child, ast.Assert)]
            valid_asserts = [child for child in asserts if not is_constant_true(child.test)]
            if not valid_asserts:
                issues.append({
                    "code": "TEARDOWN_EXPECT_EMPTY",
                    "message": (
                        "teardown 的 step.expect 无有效 assert 时，块内必须包含 command/check_write_cmd 清理动作；"
                        "不可为空壳。"
                    ),
                    "line": teardown.lineno,
                })
    return issues


def is_constant_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def extract_srs_levels(text: str) -> dict[str, str | None]:
    names = {
        "level1": "@srs一级规格",
        "level2": "@srs二级规格",
        "level3": "@srs三级规格",
        "level4": "@srs四级规格",
        "level5": "@srs五级规格",
        "level6": "@srs六级规格",
    }
    result: dict[str, str | None] = {}
    for key, label in names.items():
        match = re.search(rf"{re.escape(label)}\s*:\s*(.+)", text)
        result[key] = match.group(1).strip() if match else None
    return result


def build_lint(date_dir: Path) -> dict[str, Any]:
    ensure_dirs(date_dir)
    py_path = current_py(date_dir)
    result: dict[str, Any] = {
        "date_dir": str(date_dir),
        "current_py": py_path.name if py_path else None,
        "ok": False,
        "issues": [],
        "warnings": [],
        "srs_levels": {},
        "expect_count": 0,
        "assert_count": 0,
    }
    issues: list[dict[str, Any]] = result["issues"]
    warnings: list[dict[str, Any]] = result["warnings"]

    if py_path is None:
        issues.append({"code": "NO_PY", "message": "py/ 为空，无法做静态检查。"})
        return result

    text = py_path.read_text(encoding="utf-8", errors="ignore")
    result["srs_levels"] = extract_srs_levels(text)
    missing_srs = [key for key, value in result["srs_levels"].items() if not value]
    if missing_srs:
        issues.append({"code": "MISSING_SRS_LEVEL", "message": f"缺少 SRS 字段: {missing_srs}"})

    if re.search(r"\bexec\s*\(", text):
        issues.append({"code": "EXEC_FORBIDDEN", "message": "脚本中不允许使用 exec 函数。"})
    if re.search(r"@pytest\.mark\.usefixtures\s*\(", text):
        issues.append({"code": "USEFIXTURES_FORBIDDEN", "message": "必须删除 @pytest.mark.usefixtures(...) 装饰器。"})
    if re.search(r"\b(?:pytest|python)\.skip\s*\(", text):
        issues.append({"code": "SKIP_FORBIDDEN", "message": "脚本中不允许使用 pytest.skip/python.skip。"})
    if re.search(r"\bcmd_list\s*=\s*\[\s*\]", text):
        issues.append({"code": "EMPTY_CMD_LIST", "message": "脚本中存在空 cmd_list = []。"})
    if re.search(r"@具体UI命令[^:]*:", text):
        warnings.append({"code": "FINAL_DOCSTRING_MARKER", "message": "封包前需要删除 @具体UI命令 行。"})
    for pattern, label in FORBIDDEN_COMMAND_PATTERNS:
        if re.search(pattern, text, re.I):
            issues.append({"code": "FORBIDDEN_COMMAND", "message": f"脚本中禁止使用命令: {label}"})

    level_blob = " ".join(value or "" for value in result["srs_levels"].values())
    if re.search(r"\bbgp\b", text, re.I) and "instance" in text.lower() and "多实例" not in level_blob:
        issues.append({"code": "BGP_INSTANCE_WITHOUT_SPEC", "message": "BGP 规格未体现多实例时禁止使用 instance 命令。"})

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        issues.append({"code": "SYNTAX_ERROR", "message": exc.msg, "line": exc.lineno})
        return result

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    issues.extend(check_setup_class_shape(tree))
    result["assert_count"] = sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assert))
    assigned_command_lists: list[tuple[int, list[str], list[str], str | None]] = []
    command_calls: list[tuple[int, list[str]]] = []
    command_list_call_names: dict[str, list[str]] = {}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "exec" in node.name.lower():
                issues.append({
                    "code": "EXEC_NAME_FORBIDDEN",
                    "message": "方法名禁止包含 exec 关键词。",
                    "line": node.lineno,
                    "name": node.name,
                })
            if node.name not in ALLOWED_SCRIPT_FUNCTIONS:
                issues.append({
                    "code": "CUSTOM_FUNCTION_FORBIDDEN",
                    "message": "除框架方法外禁止定义自定义函数，逻辑应直接写入 test_process。",
                    "line": node.lineno,
                    "name": node.name,
                })
            for arg in node.args.args + node.args.kwonlyargs:
                if "exec" in arg.arg.lower():
                    issues.append({
                        "code": "EXEC_NAME_FORBIDDEN",
                        "message": "参数名禁止包含 exec 关键词。",
                        "line": arg.lineno,
                        "name": arg.arg,
                    })
        elif isinstance(node, ast.Try):
            function_name = nearest_function(node, parents)
            if function_name != "setup_method":
                issues.append({
                    "code": "TRY_EXCEPT_FORBIDDEN",
                    "message": "除 setup_method 外禁止使用 try...except，错误应交由测试框架接管。",
                    "line": node.lineno,
                    "function": function_name,
                })
        elif isinstance(node, ast.Call):
            name = dotted_name(node.func)
            if name == "exec":
                issues.append({"code": "EXEC_FORBIDDEN", "message": "脚本中不允许使用 exec 函数。", "line": node.lineno})
            if name in {"pytest.skip", "python.skip"}:
                issues.append({"code": "SKIP_FORBIDDEN", "message": "脚本中不允许使用 pytest.skip/python.skip。", "line": node.lineno})
            if name.endswith(".command"):
                commands = [value for value in (literal_string(arg) for arg in node.args) if value is not None]
                if commands:
                    command_calls.append((node.lineno, commands))
                    if (
                        contains_config_command(commands)
                        and nearest_function(node, parents) != "teardown_method"
                    ):
                        issues.append({
                            "code": "CONFIG_NOT_CHECK_WRITE_CMD",
                            "message": "非 teardown 配置命令必须通过 cmgr.check_write_cmd(*cmd_list) 下发，不能用 cmgr.command 逐条或批量下发。",
                            "line": node.lineno,
                        })
            for arg in node.args:
                value = literal_string(arg)
                if value and HARDCODED_INTERFACE_RE.search(value):
                    issues.append({
                        "code": "HARDCODED_INTERFACE",
                        "message": "禁止硬编码物理接口名称，应使用 self.dut*_port* 变量。",
                        "line": node.lineno,
                        "text": value[:160],
                    })
                if value:
                    for pattern, label in ABBREVIATED_COMMAND_PATTERNS:
                        if re.search(pattern, value, re.I):
                            warnings.append({
                                "code": "ABBREVIATED_COMMAND",
                                "message": f"命令建议使用完整写法，避免缩写: {label}",
                                "line": node.lineno,
                                "text": value[:160],
                            })
            called_names = [
                arg.value.id for arg in node.args
                if isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name)
            ]
            for called_name in called_names:
                command_list_call_names.setdefault(called_name, []).append(name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            values = list_literal_strings(node.value)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for target_name in target_names(target):
                    if "exec" in target_name.lower():
                        issues.append({
                            "code": "EXEC_NAME_FORBIDDEN",
                            "message": "变量名/字段名禁止包含 exec 关键词。",
                            "line": node.lineno,
                            "name": target_name,
                        })
            if value_uses_self_tb(node.value):
                issues.append({
                    "code": "SELF_TB_ASSIGN_FORBIDDEN",
                    "message": "禁止从 self.tb 手动提取设备或端口属性，class_setup 已自动注入。",
                    "line": node.lineno,
                })
            if values:
                names = assigned_command_list_names(node)
                assigned_command_lists.append((
                    node.lineno,
                    values,
                    names,
                    nearest_function(node, parents),
                ))
                for command in values:
                    if HARDCODED_INTERFACE_RE.search(command):
                        issues.append({
                            "code": "HARDCODED_INTERFACE",
                            "message": "禁止硬编码物理接口名称，应使用 self.dut*_port* 变量。",
                            "line": node.lineno,
                            "text": command[:160],
                        })
                    for pattern, label in ABBREVIATED_COMMAND_PATTERNS:
                        if re.search(pattern, command, re.I):
                            warnings.append({
                                "code": "ABBREVIATED_COMMAND",
                                "message": f"命令建议使用完整写法，避免缩写: {label}",
                                "line": node.lineno,
                                "text": command[:160],
                            })
        elif isinstance(node, ast.Assert):
            if is_constant_true(node.test):
                issues.append({"code": "ASSERT_TRUE", "message": "禁止使用 assert True 这类虚假断言。", "line": node.lineno})
            if assert_mentions_fail(node.test):
                issues.append({
                    "code": "FAIL_ASSERT_FORBIDDEN",
                    "message": "禁止以 FAIL 作为最终通过条件，所有断言最终应判断 PASS。",
                    "line": node.lineno,
                })
            if not assert_mentions_pass(node.test):
                reason = raw_output_assert_reason(node)
                if reason:
                    issues.append({"code": "RAW_OUTPUT_ASSERT_FORBIDDEN", "message": reason, "line": node.lineno})
        elif isinstance(node, ast.With) and is_step_expect_with(node):
            result["expect_count"] += 1
            asserts = [child for child in ast.walk(node) if isinstance(child, ast.Assert)]
            valid_asserts = [child for child in asserts if not is_constant_true(child.test)]
            if not valid_asserts:
                # teardown 配置恢复只负责下发命令，结果由框架配置对比检查。
                if is_teardown_cleanup_expect(node, parents):
                    if has_cleanup_command_call(node):
                        warnings.append({
                            "code": "TEARDOWN_EXPECT_NO_ASSERT_OK",
                            "message": (
                                "teardown 清理 expect 无 assert 但已包含实际清理命令，"
                                "按清理步骤规范放行。"
                            ),
                            "line": node.lineno,
                        })
                    else:
                        issues.append({
                            "code": "TEARDOWN_EXPECT_EMPTY",
                            "message": (
                                "teardown 的 step.expect 无 assert 时，块内必须包含 "
                                "command/check_write_cmd 清理动作。"
                            ),
                            "line": node.lineno,
                        })
                else:
                    issues.append({
                        "code": "EXPECT_WITHOUT_ASSERT",
                        "message": "step.expect 块内缺少有效 assert。",
                        "line": node.lineno,
                    })

    topology_doc = extract_doc_value(text, "@测试拓扑")
    setup_topology = extract_class_setup_topology(tree)
    if topology_doc and setup_topology and topology_doc != setup_topology[1]:
        issues.append({
            "code": "TOPOLOGY_MISMATCH",
            "message": "@测试拓扑 与 class_setup(...) 拓扑名称必须完全一致。",
            "line": setup_topology[0],
            "doc_topology": topology_doc,
            "class_setup_topology": setup_topology[1],
        })

    for line, commands in command_calls + [
        (line, commands) for line, commands, _, _ in assigned_command_lists
    ]:
        if not has_enable_before_config(commands):
            issues.append({"code": "CONFIG_WITHOUT_ENABLE", "message": "包含配置/清理命令的 cmd_list 首条必须是 enable/en。", "line": line})

    for line, commands, names, function_name in assigned_command_lists:
        if not contains_config_command(commands):
            continue
        if function_name == "teardown_method":
            continue
        for name in names:
            callees = command_list_call_names.get(name, [])
            if callees and not any(callee.endswith(".check_write_cmd") for callee in callees):
                issues.append({
                    "code": "CONFIG_NOT_CHECK_WRITE_CMD",
                    "message": "非 teardown 配置 cmd_list 必须通过 cmgr.check_write_cmd(*cmd_list) 下发。",
                    "line": line,
                    "name": name,
                })

    # 方案B：强制 teardown 步骤壳，防止为消 lint 拆掉 casestep/expect。
    issues.extend(check_teardown_method_structure(tree))

    if result["expect_count"] == 0:
        warnings.append({"code": "NO_EXPECT_BLOCK", "message": "未发现 step.expect 块。"})

    result["ok"] = not issues
    return result


def find_error_excerpt(lines: list[str], status: str, context_before: int = 20, context_after: int = 80) -> dict[str, Any] | None:
    patterns = [
        r"Traceback \(most recent call last\)",
        r"AssertionError",
        r"\bFAIL(?:ED)?\b",
        r"\bERROR\b",
        r"Exception",
        r"Invalid input",
        r"%\s*(?:Error|Invalid|Incomplete)",
    ]
    if status != "FAIL":
        patterns = patterns[:2]
    for index, line in enumerate(lines):
        if any(re.search(pattern, line, re.I) for pattern in patterns):
            start = max(0, index - context_before)
            end = min(len(lines), index + context_after + 1)
            return {
                "matched_line": index + 1,
                "start_line": start + 1,
                "end_line": end,
                "lines": lines[start:end],
            }
    return None


def infer_error_reason(excerpt: dict[str, Any] | None) -> str | None:
    if not excerpt:
        return None
    text = "\n".join(excerpt.get("lines", []))
    checks = [
        (r"AssertionError[:：]?\s*(.+)", "断言失败"),
        (r"SyntaxError[:：]?\s*(.+)", "语法错误"),
        (r"NameError[:：]?\s*(.+)", "变量或名称未定义"),
        (r"AttributeError[:：]?\s*(.+)", "对象属性不存在"),
        (r"Timeout|timed out|超时", "等待或命令执行超时"),
        (r"Invalid input|%\s*Invalid", "设备提示 Invalid input"),
        (r"Incomplete command|%\s*Incomplete", "设备提示 Incomplete command"),
        (r"Unknown command|Unrecognized command", "设备提示未知命令"),
    ]
    for pattern, label in checks:
        match = re.search(pattern, text, re.I)
        if match and match.lastindex:
            return f"{label}: {match.group(1).strip()[:240]}"
        if match:
            return label
    first = next((line.strip() for line in excerpt.get("lines", []) if line.strip()), None)
    return f"未识别具体类型，请查看错误片段: {first[:240]}" if first else None


def summarize_log_lines(lines: list[str], limit: int = 80) -> list[dict[str, Any]]:
    step_patterns = [
        r"case\s*step",
        r"casestep",
        r"step_msg",
        r"步骤",
        r"预期",
        r"expect",
    ]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if any(re.search(pattern, stripped, re.I) for pattern in step_patterns):
            key = stripped[:160]
            if key in seen:
                continue
            seen.add(key)
            items.append({"line": index + 1, "text": key})
            if len(items) >= limit:
                break
    return items


def build_log_summary(date_dir: Path) -> dict[str, Any]:
    status = build_status(date_dir)
    result: dict[str, Any] = {
        "date_dir": str(date_dir),
        "current_py": status.get("current_py"),
        "latest_log": status.get("latest_log"),
        "log_status": status.get("log_status"),
        "log_is_fresh": status.get("log_is_fresh"),
        "next_action": status.get("next_action"),
        "case_level": None,
        "summary": None,
        "error_excerpt": None,
        "error_reason": None,
        "pass_anomaly_check": None,
    }
    if not status.get("current_py") or not status.get("latest_log"):
        return result

    log_path = date_dir / "log" / status["latest_log"]
    py_path = date_dir / "py" / status["current_py"]
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    result["case_level"] = build_case_level_report(py_path, log_path)
    result["summary"] = {
        "path": str(log_path),
        "bytes": log_path.stat().st_size,
        "line_count": len(lines),
        "step_like_lines": summarize_log_lines(lines),
    }
    result["error_excerpt"] = find_error_excerpt(lines, status.get("log_status", "UNKNOWN"))
    if status.get("log_status") == "FAIL":
        result["error_reason"] = infer_error_reason(result["error_excerpt"])
    if status.get("log_status") == "PASS":
        result["pass_anomaly_check"] = build_pass_anomaly_check(py_path, log_path)
    return result


def extract_doc_lines(text: str, start_label: str, end_label: str) -> list[str]:
    start = text.find(start_label)
    end = text.find(end_label, start + len(start_label)) if start >= 0 else -1
    if start < 0 or end < 0:
        return []
    return [line.strip() for line in text[start + len(start_label):end].splitlines() if line.strip()]


def extract_code_step_lines(text: str) -> dict[str, list[dict[str, Any]]]:
    casesteps: list[dict[str, Any]] = []
    expects: list[dict[str, Any]] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {"casesteps": casesteps, "expects": expects}

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = literal_string(node.value)
            if value is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "step_msg":
                    casesteps.append({"line": node.lineno, "text": value})
        elif isinstance(node, ast.Call):
            name = dotted_name(node.func)
            if not node.args:
                continue
            value = literal_string(node.args[0])
            if value is None:
                continue
            if name.endswith(".casestep"):
                casesteps.append({"line": node.lineno, "text": value})
            elif name.endswith(".expect"):
                expects.append({"line": node.lineno, "text": value})
    return {"casesteps": casesteps, "expects": expects}


def split_log_step_blocks(lines: list[str]) -> list[dict[str, Any]]:
    starts = [
        index for index, line in enumerate(lines)
        if re.match(r"^\s*Step\s*:\s*\d+\s*$", line, re.I) or line.strip() == "恢复配置"
    ]
    blocks: list[dict[str, Any]] = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        for index in range(start + 1, end):
            if any(marker in lines[index] for marker in FRAMEWORK_LOG_MARKERS):
                end = index
                break
        description = None
        for line in lines[start:min(end, start + 12)]:
            match = re.search(r"测试描述\s*[:：]\s*(.+)", line)
            if match:
                description = match.group(1).strip()
                break
        blocks.append(
            {
                "start_line": start + 1,
                "end_line": end,
                "description": description,
                "lines": lines[start:end],
            }
        )
    return blocks


def pattern_expected_in_block(pattern_label: str, block_text: str) -> bool:
    normalized_label = pattern_label.lower().replace("%", "").replace(".", "").strip()
    if normalized_label == "unknowm command":
        normalized_label = "unknown command"
    normalized_text = block_text.lower().replace("%", "").replace(".", "")
    expected_lines = [
        line for line in normalized_text.splitlines()
        if "预期结果" in line or "@预期结果" in line or "expect" in line
    ]
    return any(normalized_label in line for line in expected_lines)


def find_pass_anomalies_in_lines(lines: list[str], absolute_start_line: int) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    block_text = "\n".join(lines)
    for offset, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if "预期结果" in stripped or "@预期结果" in stripped:
            continue
        for pattern, label in PASS_ANOMALY_PATTERNS:
            if pattern_expected_in_block(label, block_text):
                continue
            if re.search(pattern, stripped, re.I):
                matches.append(
                    {
                        "line": absolute_start_line + offset,
                        "keyword": label,
                        "text": stripped[:240],
                    }
                )
    return matches


def build_pass_anomaly_check(py_path: Path, log_path: Path) -> dict[str, Any]:
    py_text = py_path.read_text(encoding="utf-8", errors="ignore")
    code_steps = extract_code_step_lines(py_text)
    script_step_texts = {item["text"] for item in code_steps["casesteps"]}
    script_expect_texts = {item["text"] for item in code_steps["expects"]}

    log_lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    blocks = split_log_step_blocks(log_lines)
    checked_blocks: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []

    for block in blocks:
        block_text = "\n".join(block["lines"])
        description = block.get("description")
        is_script_block = bool(description and description in script_step_texts)
        if not is_script_block:
            is_script_block = any(expect in block_text for expect in script_expect_texts)
        if not is_script_block:
            continue
        block_matches = find_pass_anomalies_in_lines(block["lines"], block["start_line"])
        checked_blocks.append(
            {
                "start_line": block["start_line"],
                "end_line": block["end_line"],
                "description": description,
                "match_count": len(block_matches),
            }
        )
        matches.extend(block_matches)

    warnings = []
    if not checked_blocks:
        warnings.append("未能在 PASS 日志中定位脚本真实步骤块，无法完成假 PASS 关键字检查。")

    return {
        "ok": bool(checked_blocks) and not matches,
        "checked_block_count": len(checked_blocks),
        "checked_blocks": checked_blocks,
        "matches": matches,
        "warnings": warnings,
    }


def build_final_check(date_dir: Path) -> dict[str, Any]:
    status = build_status(date_dir)
    lint = build_lint(date_dir)
    result: dict[str, Any] = {
        "date_dir": str(date_dir),
        "status": status,
        "lint_ok": lint.get("ok"),
        "lint_issues": lint.get("issues"),
        "ready_for_package_review": False,
        "doc_steps": [],
        "doc_expectations": [],
        "code_steps": {},
        "log_step_like_lines": [],
        "case_level": None,
        "pass_anomaly_check": None,
        "notes": [],
    }
    if status.get("log_status") != "PASS":
        result["notes"].append("最新同前缀日志不是 PASS，不能终验。")
    if status.get("log_status") == "PASS" and not status.get("log_is_fresh"):
        result["notes"].append("最新 PASS 日志早于脚本修改时间；按当前策略仅提示，不阻止终验和封包。")
    if not status.get("current_py"):
        result["notes"].append("py/ 中没有当前脚本。")
        return result

    py_path = date_dir / "py" / status["current_py"]
    text = py_path.read_text(encoding="utf-8", errors="ignore")
    log_path = date_dir / "log" / status["latest_log"] if status.get("latest_log") else None
    result["case_level"] = build_case_level_report(py_path, log_path)
    result["doc_steps"] = extract_doc_lines(text, "@测试步骤:", "@预期结果")
    result["doc_expectations"] = extract_doc_lines(text, "@预期结果：", "@用例等级")
    if not result["doc_expectations"]:
        result["doc_expectations"] = extract_doc_lines(text, "@预期结果:", "@用例等级")
    result["code_steps"] = extract_code_step_lines(text)

    log_summary = build_log_summary(date_dir)
    summary = log_summary.get("summary") or {}
    result["log_step_like_lines"] = summary.get("step_like_lines", [])
    result["pass_anomaly_check"] = log_summary.get("pass_anomaly_check")
    if status.get("log_status") == "PASS":
        anomaly = result["pass_anomaly_check"]
        if not anomaly or not anomaly.get("ok"):
            result["notes"].append("PASS 日志真实步骤异常关键字检查未通过，禁止封包。")
    result["ready_for_package_review"] = (
        status.get("log_status") == "PASS"
        and bool(lint.get("ok"))
        and bool(result["pass_anomaly_check"] and result["pass_anomaly_check"].get("ok"))
    )
    return result


def print_status(status: dict[str, Any]) -> None:
    print(json.dumps(status, ensure_ascii=False, indent=2))
    current = status.get("current_py")
    if not current:
        if status["log_status"] == "DONE":
            print("状态: 当天 bak 队列已完成。")
        else:
            print(f"状态: py/ 为空，下一个待推进脚本: {status.get('next_bak')}")
        return

    latest = status.get("latest_log")
    if not latest:
        print(f"状态: {current} 等待执行机日志回流。")
        return

    if status.get("log_is_fresh"):
        fresh = "新鲜"
    elif status.get("log_status") == "PASS":
        fresh = "早于脚本（按当前策略允许封包）"
    else:
        fresh = "过旧"
    print(f"状态: {current} 最新同前缀日志为 {latest}，结果 {status['log_status']}，日志{fresh}。")


def cmd_init(args: argparse.Namespace) -> None:
    ensure_dirs(args.date_dir)
    print(f"已初始化目录: {args.date_dir}")
    if args.receive_dir is not None:
        receive_dir = save_receive_dir(args.date_dir, args.receive_dir)
        print(f"已设置日志接收目录: {receive_dir}")


def cmd_status(args: argparse.Namespace) -> None:
    print_status(build_status(args.date_dir))


def cmd_lint(args: argparse.Namespace) -> None:
    print(json.dumps(build_lint(args.date_dir), ensure_ascii=False, indent=2))


def cmd_log_summary(args: argparse.Namespace) -> None:
    print(json.dumps(build_log_summary(args.date_dir), ensure_ascii=False, indent=2))


def cmd_final_check(args: argparse.Namespace) -> None:
    print(json.dumps(build_final_check(args.date_dir), ensure_ascii=False, indent=2))


def cmd_advance(args: argparse.Namespace) -> None:
    ensure_dirs(args.date_dir)
    if current_py(args.date_dir) is not None:
        print_status(build_status(args.date_dir))
        raise SystemExit("py/ 已有当前脚本，不能推进下一个。")

    src = next_bak(args.date_dir)
    if src is None:
        print_status(build_status(args.date_dir))
        return

    dst = args.date_dir / "py" / src.name
    shutil.copy2(src, dst)
    print(f"已从 bak/ 复制下一个脚本到 py/: {src.name}")
    print_status(build_status(args.date_dir))


def cmd_package(args: argparse.Namespace) -> None:
    ensure_dirs(args.date_dir)
    py_path = current_py(args.date_dir)
    if py_path is None:
        raise SystemExit("py/ 为空，无法封包。")

    status = build_status(args.date_dir)
    if status.get("log_status") != "PASS":
        raise SystemExit(f"最新同前缀日志不是 PASS，禁止封包: {status.get('latest_log')} {status.get('log_status')}")
    if not status.get("log_is_fresh"):
        print(f"警告: 最新 PASS 日志早于脚本修改时间，按当前策略继续封包: {status.get('latest_log')}")

    log_path = args.date_dir / "log" / status["latest_log"]
    anomaly = build_pass_anomaly_check(py_path, log_path)
    if not anomaly.get("ok"):
        matches = anomaly.get("matches") or []
        if matches:
            first = matches[0]
            raise SystemExit(
                "PASS 日志真实步骤中存在疑似命令异常，禁止封包: "
                f"line {first.get('line')} {first.get('keyword')} {first.get('text')}"
            )
        raise SystemExit(
            "PASS 日志未能定位脚本真实步骤块，无法完成假 PASS 检查，禁止封包: "
            + "; ".join(anomaly.get("warnings") or [])
        )

    fields = parse_srs_fields(py_path)
    case_level = build_case_level_report(py_path, log_path)
    package_root = args.package_root or (args.date_dir / "done")
    target_dir = package_root / fields["level1"] / fields["level2"] / fields["level3"]
    target_log_dir = target_dir / "Log"
    target_log_dir.mkdir(parents=True, exist_ok=True)

    target_py_path = target_dir / py_path.name
    old_target_logs = sorted(target_log_dir.glob(f"{py_path.stem}*.txt"), key=lambda p: p.name)
    final_text = read_final_script_text(py_path)
    final_text = update_case_level(final_text, case_level.get("recommended_case_level"))
    target_py_path.write_text(final_text, encoding="utf-8")
    for old_log in old_target_logs:
        old_log.unlink()
    shutil.copy2(log_path, target_log_dir / log_path.name)
    py_path.unlink()

    state = load_state(args.date_dir)
    completed = set(state.get("completed", []))
    completed.add(py_path.name)
    state["completed"] = sorted(completed)
    state["last_packaged"] = {
        "py": py_path.name,
        "log": log_path.name,
        "target_dir": str(target_dir),
        "target_log_dir": str(target_log_dir),
        "replaced_old_logs": [str(path) for path in old_target_logs],
        "case_level": case_level,
    }
    save_state(args.date_dir, state)

    print(f"已封包脚本: {target_py_path}")
    print(f"已封包日志: {target_log_dir / log_path.name}")
    if old_target_logs:
        print(f"已清理旧 PASS 日志: {[path.name for path in old_target_logs]}")
    if case_level.get("needs_script_level_update"):
        print(
            "已按日志用时调整归档脚本用例等级: "
            f"{case_level.get('script_case_level')} -> {case_level.get('recommended_case_level')}"
        )
    nxt = next_bak(args.date_dir)
    if nxt is not None:
        dst = args.date_dir / "py" / nxt.name
        shutil.copy2(nxt, dst)
        print(f"已自动推进下一个脚本到 py/: {nxt.name}")
    else:
        print("当天 bak 队列已完成。")
    print_status(build_status(args.date_dir))


def cmd_exe_check(args: argparse.Namespace) -> None:
    ensure_dirs(args.date_dir)
    done_dir = args.date_dir / "done"
    check_dir = args.date_dir / "check"
    source_files = sorted(
        (
            path for path in done_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".py", ".txt"}
        ),
        key=lambda path: str(path.relative_to(done_dir)),
    )

    paths_by_name: dict[str, list[Path]] = {}
    for source_path in source_files:
        paths_by_name.setdefault(source_path.name.casefold(), []).append(source_path)
    duplicate_names = {
        paths[0].name: [str(path) for path in paths]
        for paths in paths_by_name.values()
        if len(paths) > 1
    }
    if duplicate_names:
        raise SystemExit(
            "done/ 中存在扁平复制后会重名的文件，未清理 check/，请先人工处理: "
            + json.dumps(duplicate_names, ensure_ascii=False)
        )

    staging_dir = Path(tempfile.mkdtemp(prefix=".exe-check-", dir=args.date_dir))
    copied: list[dict[str, str]] = []
    removed: list[str] = []
    try:
        for source_path in source_files:
            shutil.copy2(source_path, staging_dir / source_path.name)

        removed = clear_check_dir(args.date_dir)
        for source_path in source_files:
            target_path = check_dir / source_path.name
            shutil.move(str(staging_dir / source_path.name), target_path)
            copied.append({"source": str(source_path), "target": str(target_path)})
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

    print(
        json.dumps(
            {
                "done_dir": str(done_dir),
                "check_dir": str(check_dir),
                "copied_count": len(copied),
                "copied": copied,
                "removed_from_check": removed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_set_receive_dir(args: argparse.Namespace) -> None:
    ensure_dirs(args.date_dir)
    receive_dir = save_receive_dir(args.date_dir, args.receive_dir)
    print(
        json.dumps(
            {
                "date_dir": str(args.date_dir),
                "receive_dir": str(receive_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_receive_log(args: argparse.Namespace) -> None:
    ensure_dirs(args.date_dir)
    receive_dir = resolve_receive_dir(args.date_dir, args.receive_dir)
    py_path = current_py(args.date_dir)
    if py_path is None:
        raise SystemExit("py/ 为空，无法接收当前脚本日志。")

    source_path = latest_received_log(receive_dir, py_path)
    if source_path is None:
        raise SystemExit(
            f"日志接收目录中找不到与当前脚本同前缀的 txt 日志: "
            f"{receive_dir} -> {py_path.stem}*.txt"
        )

    log_dir = args.date_dir / "log"
    file_descriptor, staging_name = tempfile.mkstemp(
        prefix=".receive-log-",
        suffix=".tmp",
        dir=log_dir,
    )
    os.close(file_descriptor)
    staging_path = Path(staging_name)
    target_path = log_dir / source_path.name
    removed: list[str] = []
    try:
        shutil.copy2(source_path, staging_path)
        removed = clear_log_dir(args.date_dir, staging_path)
        staging_path.replace(target_path)
        source_path.unlink()
    except Exception:
        if staging_path.exists():
            staging_path.unlink()
        raise

    print(
        json.dumps(
            {
                "current_py": py_path.name,
                "receive_dir": str(receive_dir),
                "selected_source": str(source_path),
                "selected_modified_ns": mtime_ns(target_path),
                "moved_to": str(target_path),
                "removed_from_log": removed,
                "status": build_status(args.date_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_ignore_current(args: argparse.Namespace) -> None:
    ensure_dirs(args.date_dir)
    py_path = current_py(args.date_dir)
    if py_path is None:
        raise SystemExit("py/ 为空，无法归档当前无法解决脚本。")

    status = build_status(args.date_dir)
    if status.get("log_status") != "FAIL":
        raise SystemExit(f"最新同前缀日志不是 FAIL，禁止归档到 ignore: {status.get('latest_log')} {status.get('log_status')}")
    if not status.get("log_is_fresh"):
        raise SystemExit(f"最新 FAIL 日志早于脚本修改时间，禁止归档到 ignore: {status.get('latest_log')}")

    log_path = args.date_dir / "log" / status["latest_log"]
    target_dir = prepare_ignore_case_dir(args.date_dir, py_path.name, args.overwrite)
    target_log_dir = target_dir / "Log"
    shutil.copy2(py_path, target_dir / py_path.name)
    shutil.copy2(log_path, target_log_dir / log_path.name)
    write_error_json(target_dir / "error.json", py_path.name, args.reason or "")

    py_path.unlink()
    state = load_state(args.date_dir)
    ignored = set(state.get("ignored", []))
    ignored.add(py_path.name)
    state["ignored"] = sorted(ignored)
    state["last_ignored"] = {
        "source": "current_fail",
        "py": py_path.name,
        "log": log_path.name,
        "target_dir": str(target_dir),
        "target_log_dir": str(target_log_dir),
        "fail_reason": args.reason or "",
    }
    save_state(args.date_dir, state)

    print(f"已归档无法解决脚本到 ignore: {target_dir / py_path.name}")
    print(f"已归档 FAIL 日志: {target_log_dir / log_path.name}")
    print(f"已创建错误说明: {target_dir / 'error.json'}")
    nxt = next_bak(args.date_dir)
    if nxt is not None:
        dst = args.date_dir / "py" / nxt.name
        shutil.copy2(nxt, dst)
        print(f"已自动推进下一个脚本到 py/: {nxt.name}")
    else:
        print("当天 bak 队列已完成。")
    print_status(build_status(args.date_dir))


def cmd_reject_done(args: argparse.Namespace) -> None:
    ensure_dirs(args.date_dir)
    items = load_reject_items(args)
    archived: list[dict[str, str]] = []

    for item in items:
        py_name = item["pyName"]
        fail_reason = item["failReason"]
        done_py_path = find_done_py(args.date_dir, py_name)
        log_path = latest_done_log(done_py_path)
        if log_path is None:
            raise SystemExit(f"done/ 中找不到同前缀 PASS 日志: {py_name}")
        if detect_log_status(log_path) != "PASS":
            raise SystemExit(f"done/ 中最新同前缀日志不是 PASS，禁止作为人工退回归档: {log_path}")

        target_dir = prepare_ignore_case_dir(args.date_dir, py_name, args.overwrite)
        target_log_dir = target_dir / "Log"
        shutil.copy2(done_py_path, target_dir / py_name)
        shutil.copy2(log_path, target_log_dir / log_path.name)
        write_error_json(target_dir / "error.json", py_name, fail_reason)
        archived.append(
            {
                "py": py_name,
                "done_py": str(done_py_path),
                "log": str(log_path),
                "target_dir": str(target_dir),
                "fail_reason": fail_reason,
            }
        )

    state = load_state(args.date_dir)
    rejected = set(state.get("rejected_done", []))
    rejected.update(item["pyName"] for item in items)
    state["rejected_done"] = sorted(rejected)
    state["last_rejected_done"] = archived
    save_state(args.date_dir, state)

    print(json.dumps({"archived": archived}, ensure_ascii=False, indent=2))
    print_status(build_status(args.date_dir))


def main() -> int:
    parser = argparse.ArgumentParser(description="Ruijie daily question workflow helper")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, func in (
        ("init", cmd_init),
        ("status", cmd_status),
        ("lint", cmd_lint),
        ("log-summary", cmd_log_summary),
        ("final-check", cmd_final_check),
        ("advance", cmd_advance),
        ("package", cmd_package),
        ("exe-check", cmd_exe_check),
        ("set-receive-dir", cmd_set_receive_dir),
        ("receive-log", cmd_receive_log),
        ("ignore-current", cmd_ignore_current),
        ("reject-done", cmd_reject_done),
    ):
        p = sub.add_parser(name)
        p.add_argument("date_dir", type=Path)
        if name in {"init", "receive-log"}:
            p.add_argument("--receive-dir", type=Path, default=None, help="用户指定的外部日志接收目录；设置后保存到工作流状态。")
        if name == "set-receive-dir":
            p.add_argument("receive_dir", type=Path, help="用户指定的外部日志接收目录。")
        if name == "package":
            p.add_argument("--package-root", type=Path, default=None)
        if name == "ignore-current":
            p.add_argument("--reason", default="", help="可选：写入 error.json 的无法解决原因。默认留空供用户手动填写。")
            p.add_argument("--overwrite", action="store_true", help="允许覆盖已有 ignore/<脚本stem>/ 目录中的同名归档。")
        if name == "reject-done":
            p.add_argument("--items-file", type=Path, default=None, help="锐捷人工退回 JSON 数组文件。")
            p.add_argument("--items-json", default=None, help="锐捷人工退回 JSON 数组字符串。")
            p.add_argument("--overwrite", action="store_true", help="允许覆盖已有 ignore/<脚本stem>/ 目录中的同名归档。")
        p.set_defaults(func=func)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
