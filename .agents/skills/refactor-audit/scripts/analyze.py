#!/usr/bin/env python3
"""
Refactor Audit Analysis Script

Analyzes Python, Vue, and TypeScript files for refactoring candidates.
Outputs JSON with categorized findings.

Usage:
    python analyze.py [scope] [--quick] [--json]

Examples:
    python analyze.py src/backend/
    python analyze.py src/frontend/src/views/
    python analyze.py --quick
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# Thresholds
THRESHOLDS = {
    "python": {
        "file_lines": {"low": 300, "medium": 500, "high": 800, "critical": 1000},
        "function_lines": {"low": 30, "medium": 50, "high": 100, "critical": 200},
        "complexity": {"low": 10, "medium": 15, "high": 20, "critical": 30},
        "parameters": {"low": 5, "medium": 7, "high": 10, "critical": 15},
        "nesting": {"low": 3, "medium": 4, "high": 5, "critical": 6},
    },
    "vue": {
        "file_lines": {"low": 200, "medium": 300, "high": 400, "critical": 600},
        "script_lines": {"low": 80, "medium": 150, "high": 250, "critical": 400},
    },
    "typescript": {
        "file_lines": {"low": 200, "medium": 400, "high": 600, "critical": 800},
        "function_lines": {"low": 30, "medium": 50, "high": 100, "critical": 150},
    },
}


@dataclass
class Issue:
    file: str
    line: int
    issue_type: str
    metric_name: str
    metric_value: int
    threshold: int
    severity: str  # critical, high, medium, low
    recommendation: str

    def to_dict(self):
        return asdict(self)


@dataclass
class AnalysisResult:
    scope: str
    timestamp: str
    issues: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    hotspots: list = field(default_factory=list)

    def to_dict(self):
        return {
            "scope": self.scope,
            "timestamp": self.timestamp,
            "issues": [i.to_dict() for i in self.issues],
            "summary": self.summary,
            "hotspots": self.hotspots,
        }


def get_severity(value: int, thresholds: dict) -> Optional[str]:
    """Determine severity based on threshold dict."""
    if value >= thresholds.get("critical", float("inf")):
        return "critical"
    elif value >= thresholds.get("high", float("inf")):
        return "high"
    elif value >= thresholds.get("medium", float("inf")):
        return "medium"
    elif value >= thresholds.get("low", float("inf")):
        return "low"
    return None


def count_lines(filepath: Path) -> int:
    """Count non-empty, non-comment lines."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return len([l for l in lines if l.strip() and not l.strip().startswith("#")])
    except Exception:
        return 0


def analyze_python_file(filepath: Path, issues: list) -> None:
    """Analyze a Python file for complexity and size issues."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
        lines = content.split("\n")
        line_count = len([l for l in lines if l.strip() and not l.strip().startswith("#")])

        # Check file length
        thresholds = THRESHOLDS["python"]["file_lines"]
        severity = get_severity(line_count, thresholds)
        if severity:
            issues.append(Issue(
                file=str(filepath),
                line=1,
                issue_type="large_file",
                metric_name="lines",
                metric_value=line_count,
                threshold=thresholds[severity],
                severity=severity,
                recommendation=f"Consider splitting into multiple modules. Target <{thresholds['medium']} lines."
            ))

        # Parse AST for function analysis
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Function length
                if hasattr(node, "end_lineno") and node.end_lineno:
                    func_lines = node.end_lineno - node.lineno
                    thresholds = THRESHOLDS["python"]["function_lines"]
                    severity = get_severity(func_lines, thresholds)
                    if severity:
                        issues.append(Issue(
                            file=str(filepath),
                            line=node.lineno,
                            issue_type="long_function",
                            metric_name="lines",
                            metric_value=func_lines,
                            threshold=thresholds[severity],
                            severity=severity,
                            recommendation=f"Extract logic into smaller functions. Function '{node.name}' is {func_lines} lines."
                        ))

                # Parameter count
                param_count = len(node.args.args) + len(node.args.kwonlyargs)
                if node.args.vararg:
                    param_count += 1
                if node.args.kwarg:
                    param_count += 1

                thresholds = THRESHOLDS["python"]["parameters"]
                severity = get_severity(param_count, thresholds)
                if severity:
                    issues.append(Issue(
                        file=str(filepath),
                        line=node.lineno,
                        issue_type="too_many_parameters",
                        metric_name="parameters",
                        metric_value=param_count,
                        threshold=thresholds[severity],
                        severity=severity,
                        recommendation=f"Use a config object or dataclass. Function '{node.name}' has {param_count} parameters."
                    ))

    except Exception as e:
        print(f"Error analyzing {filepath}: {e}", file=sys.stderr)


def analyze_vue_file(filepath: Path, issues: list) -> None:
    """Analyze a Vue file for size issues."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
        lines = content.split("\n")
        line_count = len([l for l in lines if l.strip()])

        # Check file length
        thresholds = THRESHOLDS["vue"]["file_lines"]
        severity = get_severity(line_count, thresholds)
        if severity:
            issues.append(Issue(
                file=str(filepath),
                line=1,
                issue_type="large_component",
                metric_name="lines",
                metric_value=line_count,
                threshold=thresholds[severity],
                severity=severity,
                recommendation=f"Extract child components or composables. Target <{thresholds['medium']} lines."
            ))

        # Check script section length
        script_match = re.search(r"<script[^>]*>(.*?)</script>", content, re.DOTALL)
        if script_match:
            script_content = script_match.group(1)
            script_lines = len([l for l in script_content.split("\n") if l.strip()])
            thresholds = THRESHOLDS["vue"]["script_lines"]
            severity = get_severity(script_lines, thresholds)
            if severity:
                issues.append(Issue(
                    file=str(filepath),
                    line=1,
                    issue_type="large_script_section",
                    metric_name="script_lines",
                    metric_value=script_lines,
                    threshold=thresholds[severity],
                    severity=severity,
                    recommendation=f"Extract logic to composables (use*.js). Script section is {script_lines} lines."
                ))

    except Exception as e:
        print(f"Error analyzing {filepath}: {e}", file=sys.stderr)


def analyze_typescript_file(filepath: Path, issues: list) -> None:
    """Analyze a TypeScript file for size issues."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
        lines = content.split("\n")
        line_count = len([l for l in lines if l.strip() and not l.strip().startswith("//")])

        # Check file length
        thresholds = THRESHOLDS["typescript"]["file_lines"]
        severity = get_severity(line_count, thresholds)
        if severity:
            issues.append(Issue(
                file=str(filepath),
                line=1,
                issue_type="large_file",
                metric_name="lines",
                metric_value=line_count,
                threshold=thresholds[severity],
                severity=severity,
                recommendation=f"Consider splitting into multiple modules. Target <{thresholds['medium']} lines."
            ))

    except Exception as e:
        print(f"Error analyzing {filepath}: {e}", file=sys.stderr)


def run_radon(scope: Path) -> list:
    """Run radon for cyclomatic complexity on Python files."""
    issues = []
    try:
        result = subprocess.run(
            ["radon", "cc", "-s", "-j", str(scope)],
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            for filepath, functions in data.items():
                for func in functions:
                    complexity = func.get("complexity", 0)
                    thresholds = THRESHOLDS["python"]["complexity"]
                    severity = get_severity(complexity, thresholds)
                    if severity:
                        issues.append(Issue(
                            file=filepath,
                            line=func.get("lineno", 1),
                            issue_type="high_complexity",
                            metric_name="cyclomatic_complexity",
                            metric_value=complexity,
                            threshold=thresholds[severity],
                            severity=severity,
                            recommendation=f"Reduce complexity of '{func.get('name', 'unknown')}'. Extract conditionals or use early returns."
                        ))
    except FileNotFoundError:
        print("radon not installed. Run: pip install radon", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("radon timed out", file=sys.stderr)
    except Exception as e:
        print(f"radon error: {e}", file=sys.stderr)
    return issues


def run_vulture(scope: Path) -> list:
    """Run vulture for dead code detection."""
    issues = []
    try:
        result = subprocess.run(
            ["vulture", str(scope), "--min-confidence", "80"],
            capture_output=True,
            text=True,
            timeout=120
        )
        # vulture outputs to stdout with format: filepath:line: message (confidence%)
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            match = re.match(r"(.+):(\d+): (.+) \((\d+)% confidence\)", line)
            if match:
                filepath, lineno, message, confidence = match.groups()
                issues.append(Issue(
                    file=filepath,
                    line=int(lineno),
                    issue_type="dead_code",
                    metric_name="confidence",
                    metric_value=int(confidence),
                    threshold=80,
                    severity="low",
                    recommendation=f"Unused code: {message}. Remove if truly unused."
                ))
    except FileNotFoundError:
        print("vulture not installed. Run: pip install vulture", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("vulture timed out", file=sys.stderr)
    except Exception as e:
        print(f"vulture error: {e}", file=sys.stderr)
    return issues


def find_files(scope: Path, extensions: list) -> list:
    """Find all files with given extensions, excluding common ignores."""
    ignores = {
        "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
        ".git", ".pytest_cache", ".mypy_cache", "htmlcov", "archive"
    }
    files = []
    for ext in extensions:
        for filepath in scope.rglob(f"*{ext}"):
            if not any(ignore in filepath.parts for ignore in ignores):
                files.append(filepath)
    return files


def calculate_hotspots(issues: list) -> list:
    """Find files with multiple issues."""
    file_issues = defaultdict(list)
    for issue in issues:
        file_issues[issue.file].append(issue)

    severity_scores = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    hotspots = []
    for filepath, file_issue_list in file_issues.items():
        if len(file_issue_list) >= 2:
            score = sum(severity_scores.get(i.severity, 0) for i in file_issue_list)
            hotspots.append({
                "file": filepath,
                "issue_count": len(file_issue_list),
                "severity_score": score,
                "issues": [i.issue_type for i in file_issue_list]
            })

    return sorted(hotspots, key=lambda x: -x["severity_score"])[:10]


def main():
    parser = argparse.ArgumentParser(description="Analyze code for refactoring candidates")
    parser.add_argument("scope", nargs="?", default="src/", help="Directory or file to analyze")
    parser.add_argument("--quick", action="store_true", help="Show only top 10 issues")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    # Resolve scope
    scope = Path(args.scope)
    if not scope.exists():
        # Try common mappings
        mappings = {
            "backend": "src/backend/",
            "frontend": "src/frontend/",
            "mcp": "src/mcp-server/",
            "mcp-server": "src/mcp-server/",
            "agent": "docker/base-image/agent_server/",
        }
        if args.scope in mappings:
            scope = Path(mappings[args.scope])

    if not scope.exists():
        print(f"Error: Scope not found: {scope}", file=sys.stderr)
        sys.exit(1)

    issues = []

    # Analyze Python files
    python_files = find_files(scope, [".py"])
    for filepath in python_files:
        analyze_python_file(filepath, issues)

    # Run radon for complexity
    if python_files:
        issues.extend(run_radon(scope))

    # Run vulture for dead code (only if not --quick for speed)
    if not args.quick and python_files:
        issues.extend(run_vulture(scope))

    # Analyze Vue files
    vue_files = find_files(scope, [".vue"])
    for filepath in vue_files:
        analyze_vue_file(filepath, issues)

    # Analyze TypeScript files
    ts_files = find_files(scope, [".ts", ".tsx"])
    for filepath in ts_files:
        analyze_typescript_file(filepath, issues)

    # Remove duplicates (same file+line+issue_type)
    seen = set()
    unique_issues = []
    for issue in issues:
        key = (issue.file, issue.line, issue.issue_type)
        if key not in seen:
            seen.add(key)
            unique_issues.append(issue)
    issues = unique_issues

    # Sort by severity
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    issues.sort(key=lambda x: (severity_order.get(x.severity, 99), x.file, x.line))

    # Limit if --quick
    if args.quick:
        issues = issues[:10]

    # Calculate summary
    summary = {
        "critical": len([i for i in issues if i.severity == "critical"]),
        "high": len([i for i in issues if i.severity == "high"]),
        "medium": len([i for i in issues if i.severity == "medium"]),
        "low": len([i for i in issues if i.severity == "low"]),
        "total": len(issues),
        "files_analyzed": len(python_files) + len(vue_files) + len(ts_files),
    }

    # Calculate hotspots
    hotspots = calculate_hotspots(issues)

    # Build result
    result = AnalysisResult(
        scope=str(scope),
        timestamp=datetime.now().isoformat(),
        issues=issues,
        summary=summary,
        hotspots=hotspots,
    )

    # Output
    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    main()
