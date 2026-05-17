#!/usr/bin/env python3
"""
Repository audit helper for the /tidy skill.

Usage:
    python audit.py                    # Full audit
    python audit.py --scope docs       # Audit only docs/
    python audit.py --scope root       # Audit only root folder
    python audit.py --scope tests      # Audit only tests/
    python audit.py --scope config     # Audit only config/
    python audit.py --safe-clean       # Only clean safe artifacts
    python audit.py --json             # Output as JSON

This script does NOT make changes - it only reports findings.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

# Directories to always exclude from audit
EXCLUDE_DIRS = {
    '.git', 'node_modules', '.venv', 'venv', '__pycache__',
    '.pytest_cache', 'htmlcov', '.mypy_cache', '.ruff_cache',
    'dist', 'build', '.next', '.nuxt', 'archive',
    '.playwright-mcp', 'backups', 'trinity-data', 'repositories',
}

# Files that are safe to delete without approval
SAFE_DELETE_PATTERNS = [
    '**/__pycache__/**',
    '**/*.pyc',
    '**/*.pyo',
    '**/.DS_Store',
    '**/Thumbs.db',
    'tests/reports/raw-test-output-*.txt',
    '**/*.log',
    '**/*.tmp',
    '**/*.bak',
    '.coverage',
    'coverage.xml',
]

# Files that belong in root
ROOT_ALLOWED = {
    'README.md', 'CONTRIBUTING.md', 'CODE_OF_CONDUCT.md', 'SECURITY.md',
    'LICENSE', 'CHANGELOG.md', 'VERSION',
    'CLAUDE.md', 'CLAUDE.local.md', 'CLAUDE.local.md.example',
    '.gitignore', '.gitattributes',
    '.env.example', 'deploy.config.example', 'deploy.config',
    'package.json', 'package-lock.json', 'pyproject.toml', 'poetry.lock',
    'Makefile', 'requirements.txt', '.env', '.python-version',
    '.nvmrc', '.node-version', 'tsconfig.json', '.prettierrc',
    '.eslintrc.js', '.eslintrc.json', 'vite.config.js', 'vite.config.ts',
    'install.sh', 'template.yaml',
}

# Extensions that indicate code (don't touch)
CODE_EXTENSIONS = {'.py', '.ts', '.js', '.tsx', '.jsx', '.vue', '.go', '.rs', '.rb'}


class Issue(NamedTuple):
    path: str
    issue_type: str
    severity: str  # HIGH, MEDIUM, LOW
    confidence: str  # HIGH, MEDIUM, LOW
    recommendation: str
    details: str = ""


class AuditReport:
    def __init__(self):
        self.safe_deletes: list[Path] = []
        self.issues: list[Issue] = []
        self.stats = defaultdict(int)

    def add_safe_delete(self, path: Path):
        self.safe_deletes.append(path)
        self.stats['safe_delete_count'] += 1
        self.stats['safe_delete_bytes'] += path.stat().st_size if path.exists() else 0

    def add_issue(self, issue: Issue):
        self.issues.append(issue)
        self.stats[f'{issue.severity.lower()}_issues'] += 1

    def to_dict(self) -> dict:
        return {
            'timestamp': datetime.now().isoformat(),
            'safe_deletes': [str(p) for p in self.safe_deletes],
            'safe_delete_bytes': self.stats['safe_delete_bytes'],
            'issues': [i._asdict() for i in self.issues],
            'stats': dict(self.stats),
        }

    def to_markdown(self) -> str:
        lines = [
            f"## Tidy Audit Report - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
        ]

        # Safe deletes section
        if self.safe_deletes:
            bytes_str = format_bytes(self.stats['safe_delete_bytes'])
            lines.extend([
                "### Safe Cleanup Candidates",
                f"Found {len(self.safe_deletes)} files ({bytes_str}) safe to auto-delete:",
                "",
                "| File | Size |",
                "|------|------|",
            ])
            for p in self.safe_deletes[:20]:  # Limit display
                size = format_bytes(p.stat().st_size) if p.exists() else "?"
                lines.append(f"| `{p}` | {size} |")
            if len(self.safe_deletes) > 20:
                lines.append(f"| ... and {len(self.safe_deletes) - 20} more | |")
            lines.append("")

        # Issues by severity
        for severity in ['HIGH', 'MEDIUM', 'LOW']:
            severity_issues = [i for i in self.issues if i.severity == severity]
            if severity_issues:
                lines.extend([
                    f"### {severity} Priority Issues",
                    "",
                    "| File | Issue | Recommendation | Confidence |",
                    "|------|-------|----------------|------------|",
                ])
                for issue in severity_issues:
                    lines.append(
                        f"| `{issue.path}` | {issue.issue_type} | {issue.recommendation} | {issue.confidence} |"
                    )
                lines.append("")

        # Summary
        lines.extend([
            "### Summary",
            "",
            f"- Safe deletes: {len(self.safe_deletes)} files ({format_bytes(self.stats['safe_delete_bytes'])})",
            f"- HIGH priority issues: {self.stats.get('high_issues', 0)}",
            f"- MEDIUM priority issues: {self.stats.get('medium_issues', 0)}",
            f"- LOW priority issues: {self.stats.get('low_issues', 0)}",
        ])

        return '\n'.join(lines)


def format_bytes(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024 * 1024):.1f} MB"


def should_exclude(path: Path) -> bool:
    """Check if path should be excluded from audit."""
    parts = path.parts
    return any(part in EXCLUDE_DIRS for part in parts)


def is_code_file(path: Path) -> bool:
    """Check if file is source code (should not be touched)."""
    return path.suffix.lower() in CODE_EXTENSIONS


def find_safe_deletes(root: Path) -> list[Path]:
    """Find files that are safe to delete without approval."""
    safe = []

    # Find __pycache__ directories
    for p in root.rglob('__pycache__'):
        if p.is_dir() and not should_exclude(p.parent):
            for f in p.rglob('*'):
                if f.is_file():
                    safe.append(f)

    # Find .pyc/.pyo files
    for pattern in ['**/*.pyc', '**/*.pyo']:
        for p in root.glob(pattern):
            if not should_exclude(p):
                safe.append(p)

    # Find .DS_Store
    for p in root.rglob('.DS_Store'):
        if not should_exclude(p):
            safe.append(p)

    # Find test output files (all gitignored per .gitignore)
    tests_dir = root / 'tests'
    if tests_dir.exists():
        for p in tests_dir.rglob('raw-test-output-*.txt'):
            safe.append(p)
        for p in tests_dir.rglob('*.log'):
            safe.append(p)
        reports_dir = tests_dir / 'reports'
        if reports_dir.exists():
            patterns = [
                'test-report-*.md', 'test-report-*.html',
                'test-summary-*.json',
            ]
            for pattern in patterns:
                for p in reports_dir.glob(pattern):
                    if p.is_file():
                        safe.append(p)
            coverage_dir = reports_dir / 'coverage'
            if coverage_dir.exists():
                for p in coverage_dir.rglob('*'):
                    if p.is_file():
                        safe.append(p)

    return safe


def audit_root(root: Path, report: AuditReport):
    """Audit root folder for misplaced files."""
    for item in root.iterdir():
        if item.is_dir():
            continue
        if item.name.startswith('.'):
            continue  # Skip hidden files

        name = item.name

        # Check if allowed
        if name in ROOT_ALLOWED:
            continue

        # Check patterns
        if name.endswith('.sh'):
            report.add_issue(Issue(
                path=str(item),
                issue_type="Script in root",
                severity="MEDIUM",
                confidence="HIGH",
                recommendation="Move to scripts/",
            ))
        elif name.endswith('.md') and name not in ROOT_ALLOWED:
            report.add_issue(Issue(
                path=str(item),
                issue_type="Markdown in root",
                severity="LOW",
                confidence="MEDIUM",
                recommendation="Move to docs/ or delete if obsolete",
            ))
        elif name.endswith(('.yaml', '.yml')) and not name.startswith('docker-compose'):
            # docker-compose*.yml variants are allowed (e.g., docker-compose.gitea.yml)
            report.add_issue(Issue(
                path=str(item),
                issue_type="Config in root",
                severity="LOW",
                confidence="MEDIUM",
                recommendation="Move to config/",
            ))
        elif name.endswith(('.tmp', '.bak', '.old')):
            report.add_issue(Issue(
                path=str(item),
                issue_type="Temporary file",
                severity="HIGH",
                confidence="HIGH",
                recommendation="Delete",
            ))


def audit_docs(root: Path, report: AuditReport):
    """Audit docs folder for outdated/orphan files."""
    docs_dir = root / 'docs'
    if not docs_dir.exists():
        return

    # Look for patterns indicating outdated docs
    for md_file in docs_dir.rglob('*.md'):
        if should_exclude(md_file):
            continue

        name = md_file.name.lower()
        rel_path = md_file.relative_to(root)

        # Check for old/draft prefixes
        if name.startswith('old-') or name.startswith('old_'):
            report.add_issue(Issue(
                path=str(rel_path),
                issue_type="Old prefix",
                severity="HIGH",
                confidence="HIGH",
                recommendation="Archive",
            ))
        elif name.startswith('draft-') or name.startswith('draft_'):
            # Check age
            mtime = datetime.fromtimestamp(md_file.stat().st_mtime)
            age_days = (datetime.now() - mtime).days
            if age_days > 90:
                report.add_issue(Issue(
                    path=str(rel_path),
                    issue_type=f"Stale draft ({age_days} days old)",
                    severity="MEDIUM",
                    confidence="MEDIUM",
                    recommendation="Archive or finalize",
                ))
        elif name.startswith('v1-') or name.startswith('v1_'):
            report.add_issue(Issue(
                path=str(rel_path),
                issue_type="Versioned doc (v1)",
                severity="MEDIUM",
                confidence="MEDIUM",
                recommendation="Check if superseded, then archive",
            ))

        # Check for WIP/TODO in filename
        if 'wip' in name or 'todo' in name:
            report.add_issue(Issue(
                path=str(rel_path),
                issue_type="WIP/TODO in name",
                severity="LOW",
                confidence="LOW",
                recommendation="Review and finalize or archive",
            ))


def audit_tests(root: Path, report: AuditReport):
    """Audit tests folder for artifacts."""
    tests_dir = root / 'tests'
    if not tests_dir.exists():
        return

    # Find report files that aren't in gitignore
    reports_dir = tests_dir / 'reports'
    if reports_dir.exists():
        for f in reports_dir.iterdir():
            if f.is_file() and f.suffix in ['.txt', '.log', '.html']:
                # Check if it looks like a generated report
                if 'raw-test-output' in f.name or f.name.endswith('.log'):
                    # Already handled by safe_deletes
                    continue
                report.add_issue(Issue(
                    path=str(f.relative_to(root)),
                    issue_type="Test report artifact",
                    severity="LOW",
                    confidence="MEDIUM",
                    recommendation="Review if needed, then delete",
                ))


def audit_config(root: Path, report: AuditReport):
    """Audit config folder for unused templates."""
    config_dir = root / 'config'
    if not config_dir.exists():
        return

    templates_dir = config_dir / 'agent-templates'
    if templates_dir.exists():
        # List templates - would need to cross-reference with code
        # For now, just report empty or suspicious ones
        for template_dir in templates_dir.iterdir():
            if not template_dir.is_dir():
                continue

            # Check for empty templates
            files = list(template_dir.rglob('*'))
            if len(files) == 0:
                report.add_issue(Issue(
                    path=str(template_dir.relative_to(root)),
                    issue_type="Empty template directory",
                    severity="HIGH",
                    confidence="HIGH",
                    recommendation="Delete",
                ))


def run_audit(root: Path, scope: str = 'all') -> AuditReport:
    """Run audit and return report."""
    report = AuditReport()

    # Always find safe deletes
    safe_deletes = find_safe_deletes(root)
    for p in safe_deletes:
        report.add_safe_delete(p)

    # Run scoped audits
    if scope in ['all', 'root']:
        audit_root(root, report)
    if scope in ['all', 'docs']:
        audit_docs(root, report)
    if scope in ['all', 'tests']:
        audit_tests(root, report)
    if scope in ['all', 'config']:
        audit_config(root, report)

    return report


def main():
    parser = argparse.ArgumentParser(description='Repository audit helper')
    parser.add_argument('--scope', choices=['all', 'root', 'docs', 'tests', 'config'],
                        default='all', help='Scope of audit')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--safe-clean', action='store_true',
                        help='Only list safe-to-clean files')
    parser.add_argument('--root', type=Path, default=Path('.'),
                        help='Repository root')

    args = parser.parse_args()

    root = args.root.resolve()
    report = run_audit(root, args.scope)

    if args.safe_clean:
        # Just list safe deletes
        for p in report.safe_deletes:
            print(p)
        return

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_markdown())


if __name__ == '__main__':
    main()
