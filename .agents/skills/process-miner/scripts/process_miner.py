#!/usr/bin/env python3
"""
Process Miner for Claude Code Execution Logs

Analyzes JSONL transcripts from Claude Code sessions to discover
repeatable workflow patterns and generate Trinity Process YAML definitions.

Usage:
    python process_miner.py --transcript PATH [--output YAML_PATH] [--min-frequency 3]
    python process_miner.py --project PATH [--output-dir DIR]
    python process_miner.py --list-projects
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


@dataclass
class ToolCall:
    """Represents a single tool invocation."""
    timestamp: str
    tool: str
    input: dict
    uuid: str
    parent_uuid: Optional[str] = None

    @property
    def normalized_tool(self) -> str:
        """Get normalized tool name for pattern matching."""
        return self.tool.lower()

    @property
    def input_summary(self) -> str:
        """Get a summary of the input for display."""
        if self.tool == "Read":
            return self.input.get("file_path", "")[:50]
        elif self.tool == "Write":
            return self.input.get("file_path", "")[:50]
        elif self.tool == "Edit":
            return self.input.get("file_path", "")[:50]
        elif self.tool == "Bash":
            cmd = self.input.get("command", "")[:50]
            return cmd
        elif self.tool == "Grep":
            return f"pattern: {self.input.get('pattern', '')[:30]}"
        elif self.tool == "Glob":
            return f"pattern: {self.input.get('pattern', '')[:30]}"
        return str(self.input)[:50]


@dataclass
class ToolSequence:
    """A sequence of tool calls representing a workflow pattern."""
    tools: list[str]
    frequency: int = 1
    examples: list[list[ToolCall]] = field(default_factory=list)

    @property
    def signature(self) -> str:
        """Get the signature string for this sequence."""
        return " -> ".join(self.tools)

    def __hash__(self):
        return hash(tuple(self.tools))

    def __eq__(self, other):
        return self.tools == other.tools


@dataclass
class UserIntent:
    """Represents a user message and the tool pattern it triggered."""
    message: str
    tool_sequence: list[str]
    timestamp: str


class TranscriptParser:
    """Parse Claude Code JSONL transcripts."""

    def __init__(self, jsonl_path: str):
        self.path = Path(jsonl_path)
        self.tool_calls: list[ToolCall] = []
        self.user_messages: list[dict] = []
        self.sessions: list[dict] = []

    def parse(self) -> "TranscriptParser":
        """Parse the transcript file."""
        with open(self.path) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    self._process_entry(entry)
                except json.JSONDecodeError:
                    continue
        return self

    def _process_entry(self, entry: dict):
        """Process a single JSONL entry."""
        entry_type = entry.get("type")

        if entry_type == "assistant" and "message" in entry:
            self._extract_tool_calls(entry)
        elif entry_type == "user" and "message" in entry:
            self._extract_user_message(entry)

    def _extract_tool_calls(self, entry: dict):
        """Extract tool_use entries from assistant message."""
        message = entry.get("message", {})
        content = message.get("content", [])

        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tool_call = ToolCall(
                    timestamp=entry.get("timestamp", ""),
                    tool=item.get("name", ""),
                    input=item.get("input", {}),
                    uuid=entry.get("uuid", ""),
                    parent_uuid=entry.get("parentUuid")
                )
                self.tool_calls.append(tool_call)

    def _extract_user_message(self, entry: dict):
        """Extract user message content."""
        message = entry.get("message", {})
        content = message.get("content", "")

        # Skip meta/command messages
        if "<command-name>" in content or "<local-command" in content:
            return

        self.user_messages.append({
            "timestamp": entry.get("timestamp", ""),
            "content": content,
            "uuid": entry.get("uuid", "")
        })


class PatternDiscoverer:
    """Discover patterns in tool sequences."""

    def __init__(self, tool_calls: list[ToolCall], min_frequency: int = 2):
        self.tool_calls = tool_calls
        self.min_frequency = min_frequency

    def discover_sequences(self, window_sizes: list[int] = [2, 3, 4, 5]) -> list[ToolSequence]:
        """Discover frequent tool sequences of various lengths."""
        all_sequences = []
        tool_names = [tc.tool for tc in self.tool_calls]

        for window_size in window_sizes:
            sequences = self._find_ngrams(tool_names, window_size)
            all_sequences.extend(sequences)

        # Sort by frequency and length
        all_sequences.sort(key=lambda s: (s.frequency, len(s.tools)), reverse=True)
        return all_sequences

    def _find_ngrams(self, tools: list[str], n: int) -> list[ToolSequence]:
        """Find n-grams in tool sequence."""
        if len(tools) < n:
            return []

        ngram_counts = Counter()
        for i in range(len(tools) - n + 1):
            ngram = tuple(tools[i:i+n])
            ngram_counts[ngram] += 1

        sequences = []
        for ngram, count in ngram_counts.items():
            if count >= self.min_frequency:
                seq = ToolSequence(tools=list(ngram), frequency=count)
                sequences.append(seq)

        return sequences

    def discover_tool_frequencies(self) -> Counter:
        """Get frequency of individual tools."""
        return Counter(tc.tool for tc in self.tool_calls)

    def discover_intent_patterns(self, user_messages: list[dict]) -> list[UserIntent]:
        """Try to associate user messages with subsequent tool patterns."""
        intents = []

        # Build timestamp index for tool calls
        tool_by_time = sorted(self.tool_calls, key=lambda tc: tc.timestamp)

        for msg in user_messages:
            msg_time = msg["timestamp"]
            # Find tool calls that came after this message
            subsequent_tools = [
                tc.tool for tc in tool_by_time
                if tc.timestamp > msg_time
            ][:10]  # Look at next 10 tools

            if subsequent_tools:
                intent = UserIntent(
                    message=msg["content"][:200],
                    tool_sequence=subsequent_tools,
                    timestamp=msg_time
                )
                intents.append(intent)

        return intents


class SemanticAnalyzer:
    """Analyze tool call semantics to understand intent."""

    @staticmethod
    def analyze_tool_call(tool: ToolCall) -> dict:
        """Extract semantic information from a tool call."""
        semantics = {
            "tool": tool.tool,
            "action": "unknown",
            "target": "",
            "description": ""
        }

        if tool.tool == "Read":
            path = tool.input.get("file_path", "")
            semantics["action"] = "read_file"
            semantics["target"] = Path(path).name if path else ""
            semantics["description"] = f"Read {semantics['target']}"

        elif tool.tool == "Write":
            path = tool.input.get("file_path", "")
            semantics["action"] = "write_file"
            semantics["target"] = Path(path).name if path else ""
            semantics["description"] = f"Write {semantics['target']}"

        elif tool.tool == "Edit":
            path = tool.input.get("file_path", "")
            semantics["action"] = "edit_file"
            semantics["target"] = Path(path).name if path else ""
            semantics["description"] = f"Edit {semantics['target']}"

        elif tool.tool == "Grep":
            pattern = tool.input.get("pattern", "")
            semantics["action"] = "search_pattern"
            semantics["target"] = pattern[:30]
            semantics["description"] = f"Search for '{pattern[:30]}'"

        elif tool.tool == "Glob":
            pattern = tool.input.get("pattern", "")
            semantics["action"] = "find_files"
            semantics["target"] = pattern
            semantics["description"] = f"Find files matching '{pattern}'"

        elif tool.tool == "Bash":
            cmd = tool.input.get("command", "")
            # Extract first command word
            first_word = cmd.split()[0] if cmd else ""
            semantics["action"] = f"execute_{first_word}"
            semantics["target"] = cmd[:40]
            semantics["description"] = f"Run: {cmd[:40]}"

        elif tool.tool == "Task":
            subagent = tool.input.get("subagent_type", "")
            semantics["action"] = "delegate"
            semantics["target"] = subagent
            semantics["description"] = f"Delegate to {subagent} agent"

        elif tool.tool == "WebFetch":
            url = tool.input.get("url", "")
            semantics["action"] = "fetch_url"
            semantics["target"] = url[:50]
            semantics["description"] = f"Fetch {url[:50]}"

        elif tool.tool == "WebSearch":
            query = tool.input.get("query", "")
            semantics["action"] = "web_search"
            semantics["target"] = query[:40]
            semantics["description"] = f"Search: {query[:40]}"

        return semantics

    @staticmethod
    def infer_workflow_intent(tool_calls: list[ToolCall]) -> str:
        """Infer the high-level intent from a sequence of tool calls."""
        actions = []
        targets = set()

        for tc in tool_calls:
            sem = SemanticAnalyzer.analyze_tool_call(tc)
            actions.append(sem["action"])
            if sem["target"]:
                targets.add(sem["target"])

        # Detect common workflow patterns
        if "edit_file" in actions or "write_file" in actions:
            if "read_file" in actions:
                return "read-modify workflow"
            return "file creation workflow"

        if "search_pattern" in actions and "read_file" in actions:
            return "search-and-review workflow"

        if "find_files" in actions:
            return "file discovery workflow"

        if "delegate" in actions:
            return "multi-agent delegation workflow"

        if "web_search" in actions or "fetch_url" in actions:
            return "research workflow"

        if all("execute_" in a for a in actions if a != "unknown"):
            return "command execution workflow"

        return "general workflow"


class ProcessYAMLGenerator:
    """Generate Trinity Process YAML from discovered patterns."""

    TOOL_TO_STEP_TYPE = {
        # Research/exploration tools -> agent_task with research focus
        "Read": "research",
        "Grep": "research",
        "Glob": "research",
        "WebFetch": "research",
        "WebSearch": "research",

        # Modification tools -> agent_task with edit focus
        "Edit": "modification",
        "Write": "modification",
        "NotebookEdit": "modification",

        # Execution tools -> agent_task with execution focus
        "Bash": "execution",
        "Task": "delegation",

        # Browser tools -> agent_task with browser focus
        "mcp__playwright": "browser",
    }

    def __init__(self, sequences: list[ToolSequence], source_path: str):
        self.sequences = sequences
        self.source_path = source_path

    def generate(self, pattern_index: int = 0) -> str:
        """Generate YAML for a discovered pattern."""
        if not self.sequences:
            return self._generate_empty_report()

        pattern = self.sequences[pattern_index]
        return self._pattern_to_yaml(pattern)

    def generate_all(self) -> str:
        """Generate YAML for all significant patterns."""
        yamls = []
        for i, pattern in enumerate(self.sequences[:5]):  # Top 5 patterns
            if pattern.frequency >= 3:
                yamls.append(self._pattern_to_yaml(pattern, index=i))

        if not yamls:
            return self._generate_empty_report()

        return "\n---\n\n".join(yamls)

    def _pattern_to_yaml(self, pattern: ToolSequence, index: int = 0) -> str:
        """Convert a tool sequence pattern to Trinity Process YAML."""
        # Infer pattern name and description
        pattern_name = self._infer_pattern_name(pattern.tools)
        description = self._infer_description(pattern.tools)

        steps_yaml = self._generate_steps(pattern.tools)

        return f'''# Auto-generated from execution log analysis
# Source: {self.source_path}
# Pattern frequency: {pattern.frequency} occurrences
# Confidence: {"high" if pattern.frequency >= 5 else "medium" if pattern.frequency >= 3 else "low"}

name: {pattern_name}
version: "1.0"
description: |
  {description}

  Discovered tool sequence: {pattern.signature}

triggers:
  - type: manual
    id: manual-start

steps:
{steps_yaml}

outputs:
  - name: result
    source: "{{{{steps.step-{len(pattern.tools)}.output.response}}}}"
'''

    def _infer_pattern_name(self, tools: list[str]) -> str:
        """Infer a descriptive name from the tool sequence."""
        tool_set = set(tools)

        if "Edit" in tool_set or "Write" in tool_set:
            if "Read" in tool_set:
                return "read-modify-pattern"
            return "file-modification-pattern"
        elif "Grep" in tool_set and "Read" in tool_set:
            return "search-and-review-pattern"
        elif "Glob" in tool_set:
            return "file-discovery-pattern"
        elif "Bash" in tool_set:
            return "command-execution-pattern"
        elif "Task" in tool_set:
            return "delegation-pattern"
        else:
            return f"discovered-pattern-{tools[0].lower()}"

    def _infer_description(self, tools: list[str]) -> str:
        """Infer a description from the tool sequence."""
        tool_set = set(tools)

        descriptions = []
        if "Read" in tool_set:
            descriptions.append("reads files to understand content")
        if "Grep" in tool_set:
            descriptions.append("searches for patterns in code")
        if "Glob" in tool_set:
            descriptions.append("discovers files by pattern")
        if "Edit" in tool_set:
            descriptions.append("modifies existing files")
        if "Write" in tool_set:
            descriptions.append("creates new files")
        if "Bash" in tool_set:
            descriptions.append("executes shell commands")
        if "Task" in tool_set:
            descriptions.append("delegates to specialized agents")

        if descriptions:
            return f"This process {', then '.join(descriptions)}."
        return "Auto-discovered workflow pattern from agent behavior."

    def _generate_steps(self, tools: list[str]) -> str:
        """Generate step definitions for the tool sequence."""
        steps = []
        for i, tool in enumerate(tools, 1):
            step_type = self._categorize_tool(tool)
            message = self._generate_step_message(tool, step_type)

            step = f'''  - id: step-{i}
    name: {step_type.title()} Step {i}
    type: agent_task
    agent: claude-code
    message: |
      {message}
    timeout: 5m'''

            if i > 1:
                step += f"\n    depends_on: [step-{i-1}]"

            steps.append(step)

        return "\n\n".join(steps)

    def _categorize_tool(self, tool: str) -> str:
        """Categorize a tool into a step type."""
        for prefix, category in self.TOOL_TO_STEP_TYPE.items():
            if tool.startswith(prefix):
                return category
        return "task"

    def _generate_step_message(self, tool: str, step_type: str) -> str:
        """Generate a message for a step based on tool type."""
        messages = {
            "research": f"Using {tool} to analyze and understand the relevant content.",
            "modification": f"Using {tool} to apply the necessary changes.",
            "execution": f"Using {tool} to execute the required operation.",
            "delegation": f"Using {tool} to delegate to a specialized agent.",
            "browser": f"Using {tool} to interact with web content.",
            "task": f"Performing task using {tool}.",
        }
        return messages.get(step_type, f"Execute using {tool}.")

    def _generate_empty_report(self) -> str:
        """Generate report when no patterns found."""
        return f'''# Process Mining Report
# Source: {self.source_path}
# Status: No significant patterns discovered

# Possible reasons:
# - Not enough tool calls in the transcript
# - Tool usage is too varied/random
# - Min frequency threshold not met

# Try:
# - Analyzing more transcripts together
# - Lowering the min_frequency threshold
# - Looking at a longer session
'''


class AnalysisReport:
    """Generate a comprehensive analysis report."""

    def __init__(
        self,
        parser: TranscriptParser,
        discoverer: PatternDiscoverer,
        sequences: list[ToolSequence]
    ):
        self.parser = parser
        self.discoverer = discoverer
        self.sequences = sequences

    def generate(self) -> str:
        """Generate the analysis report."""
        tool_freq = self.discoverer.discover_tool_frequencies()

        report = f'''# Process Mining Analysis Report

## Source
- **Transcript**: {self.parser.path}
- **Total Tool Calls**: {len(self.parser.tool_calls)}
- **User Messages**: {len(self.parser.user_messages)}

## Tool Usage Frequency

| Tool | Count | Percentage |
|------|-------|------------|
'''
        total = sum(tool_freq.values())
        for tool, count in tool_freq.most_common(15):
            pct = (count / total * 100) if total > 0 else 0
            report += f"| {tool} | {count} | {pct:.1f}% |\n"

        # Add semantic analysis section
        report += self._generate_semantic_section()

        report += f'''
## Discovered Patterns

Found **{len(self.sequences)}** repeating patterns:

'''
        for i, seq in enumerate(self.sequences[:10], 1):
            confidence = "HIGH" if seq.frequency >= 5 else "MEDIUM" if seq.frequency >= 3 else "LOW"
            workflow_type = SemanticAnalyzer.infer_workflow_intent(
                [tc for tc in self.parser.tool_calls if tc.tool in seq.tools][:5]
            )
            report += f'''{i}. **{seq.signature}**
   - Frequency: {seq.frequency} occurrences
   - Confidence: {confidence}
   - Workflow Type: {workflow_type}

'''

        report += '''## Recommendations

Based on the analysis:

'''
        if any(s.frequency >= 5 for s in self.sequences):
            report += "- **Strong patterns detected**: Consider formalizing the top patterns as Trinity Processes\n"
        if tool_freq.get("Task", 0) > 3:
            report += "- **Delegation detected**: This agent uses subagents; consider multi-step processes\n"
        if tool_freq.get("Edit", 0) > tool_freq.get("Write", 0):
            report += "- **Edit-heavy workflow**: Agent modifies existing code more than creating new files\n"
        if tool_freq.get("Grep", 0) > 10:
            report += "- **Search-heavy workflow**: Consider caching search results or creating an index\n"

        return report

    def _generate_semantic_section(self) -> str:
        """Generate semantic analysis section."""
        report = "\n## Semantic Analysis\n\n"

        # Analyze file targets
        files_read = set()
        files_written = set()
        search_patterns = []
        commands_run = []

        for tc in self.parser.tool_calls:
            sem = SemanticAnalyzer.analyze_tool_call(tc)

            if tc.tool == "Read" and sem["target"]:
                files_read.add(sem["target"])
            elif tc.tool in ("Write", "Edit") and sem["target"]:
                files_written.add(sem["target"])
            elif tc.tool == "Grep" and sem["target"]:
                search_patterns.append(sem["target"])
            elif tc.tool == "Bash" and sem["target"]:
                commands_run.append(sem["target"])

        if files_read:
            report += f"### Files Read ({len(files_read)})\n"
            for f in list(files_read)[:10]:
                report += f"- `{f}`\n"
            report += "\n"

        if files_written:
            report += f"### Files Modified ({len(files_written)})\n"
            for f in list(files_written)[:10]:
                report += f"- `{f}`\n"
            report += "\n"

        if search_patterns:
            report += f"### Search Patterns ({len(search_patterns)})\n"
            for p in search_patterns[:5]:
                report += f"- `{p}`\n"
            report += "\n"

        if commands_run:
            report += f"### Commands Run ({len(commands_run)})\n"
            for c in commands_run[:5]:
                report += f"- `{c}`\n"
            report += "\n"

        # Overall workflow type
        overall_intent = SemanticAnalyzer.infer_workflow_intent(self.parser.tool_calls)
        report += f"### Overall Workflow Type\n**{overall_intent}**\n\n"

        return report


def list_projects():
    """List all Claude Code projects with transcripts."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        print("No Claude Code projects found.")
        return

    print("Available Claude Code Projects:\n")
    for project in sorted(projects_dir.iterdir()):
        if project.is_dir():
            jsonl_files = list(project.glob("*.jsonl"))
            if jsonl_files:
                # Decode the project path
                decoded = project.name.replace("-", "/")
                print(f"  {decoded}")
                print(f"    Path: {project}")
                print(f"    Sessions: {len(jsonl_files)}")

                # Find most recent
                newest = max(jsonl_files, key=lambda p: p.stat().st_mtime)
                mtime = datetime.fromtimestamp(newest.stat().st_mtime)
                print(f"    Latest: {newest.name} ({mtime.strftime('%Y-%m-%d %H:%M')})")
                print()


def analyze_transcript(
    transcript_path: str,
    output_path: Optional[str] = None,
    min_frequency: int = 2,
    generate_yaml: bool = True
) -> tuple[str, str]:
    """Analyze a single transcript and generate outputs."""

    # Parse
    parser = TranscriptParser(transcript_path).parse()
    print(f"Parsed {len(parser.tool_calls)} tool calls from transcript")

    # Discover patterns
    discoverer = PatternDiscoverer(parser.tool_calls, min_frequency=min_frequency)
    sequences = discoverer.discover_sequences()
    print(f"Discovered {len(sequences)} repeating patterns")

    # Generate report
    report = AnalysisReport(parser, discoverer, sequences)
    report_text = report.generate()

    # Generate YAML if requested
    yaml_text = ""
    if generate_yaml and sequences:
        generator = ProcessYAMLGenerator(sequences, transcript_path)
        yaml_text = generator.generate_all()

    # Save if output path provided
    if output_path:
        output_dir = Path(output_path)
        output_dir.mkdir(parents=True, exist_ok=True)

        report_path = output_dir / "analysis-report.md"
        with open(report_path, "w") as f:
            f.write(report_text)
        print(f"Report saved to: {report_path}")

        if yaml_text:
            yaml_path = output_dir / "discovered-processes.yaml"
            with open(yaml_path, "w") as f:
                f.write(yaml_text)
            print(f"Process YAML saved to: {yaml_path}")

    return report_text, yaml_text


def main():
    parser = argparse.ArgumentParser(
        description="Process Miner for Claude Code Execution Logs"
    )

    parser.add_argument(
        "--transcript", "-t",
        help="Path to a specific JSONL transcript file"
    )
    parser.add_argument(
        "--project", "-p",
        help="Path to a Claude Code project directory"
    )
    parser.add_argument(
        "--list-projects", "-l",
        action="store_true",
        help="List available Claude Code projects"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output directory for generated files"
    )
    parser.add_argument(
        "--min-frequency", "-f",
        type=int,
        default=2,
        help="Minimum frequency for pattern detection (default: 2)"
    )
    parser.add_argument(
        "--report-only", "-r",
        action="store_true",
        help="Generate report only, no YAML"
    )

    args = parser.parse_args()

    if args.list_projects:
        list_projects()
        return

    if args.transcript:
        report, yaml = analyze_transcript(
            args.transcript,
            args.output,
            args.min_frequency,
            not args.report_only
        )

        if not args.output:
            print("\n" + "="*60)
            print("ANALYSIS REPORT")
            print("="*60)
            print(report)

            if yaml:
                print("\n" + "="*60)
                print("DISCOVERED PROCESS YAML")
                print("="*60)
                print(yaml)

    elif args.project:
        project_path = Path(args.project)
        jsonl_files = list(project_path.glob("*.jsonl"))

        if not jsonl_files:
            print(f"No transcript files found in {project_path}")
            return

        # Analyze most recent transcript
        newest = max(jsonl_files, key=lambda p: p.stat().st_mtime)
        print(f"Analyzing most recent transcript: {newest.name}")

        analyze_transcript(
            str(newest),
            args.output,
            args.min_frequency,
            not args.report_only
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
