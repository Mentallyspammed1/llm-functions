#!/usr/bin/env python3
"""Demo script to create a comprehensive cheat sheet for execute_command tool."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cheat_sheet_gen import CheatSheetGenerator
import argparse

# Create a mock args object
class MockArgs:
    def __init__(self):
        self.title = "Execute Command Tool - Complete Reference"
        self.category = "system"
        self.format = "md"
        self.output = None
        self.template = None
        self.author = "aichat/argc Tool Suite"
        self.version = "1.0.0"
        self.columns = 2
        self.max_width = 80
        self.include_toc = True
        self.number_sections = True
        self.highlight_syntax = True
        self.compact = False
        self.verbose = True
        self.no_color = False

def main():
    args = MockArgs()
    generator = CheatSheetGenerator(args)
    
    # Add sections with useful information
    generator.add_section(
        "Overview",
        "The execute_command tool allows you to run arbitrary shell commands and capture their output with complete runtime metadata. It's designed for secure shell command execution in Termux environments with native caching capabilities."
    )
    
    generator.add_section(
        "Basic Usage",
        "```bash\n# Simple command execution\nexecute_command.py --command \"ls -la\"\n\n# With timeout\nexecute_command.py --command \"sleep 5\" --timeout 10s\n\n# Working directory\nexecute_command.py --command \"pwd\" --working-dir /tmp\n\n# Environment variables\nexecute_command.py --command \"echo $MY_VAR\" --env MY_VAR=hello\n```"
    )
    
    generator.add_section(
        "Advanced Options",
        "```bash\n# Shell selection\nexecute_command.py --command \"echo $SHELL\" --shell zsh\n\n# Caching\nexecute_command.py --command \"date\" --use-cache\n\n# ANSI handling\nexecute_command.py --command \"echo -e '\\033[31mRed text'\"; --no-color\n# Strip ANSI codes\nexecute_command.py --command \"some-colorful-command\" --strip-ansi\n\n# Verbose output\nexecute_command.py --command \"ls\" --verbose\n```"
    )
    
    generator.add_section(
        "Return Values",
        "The tool returns a JSON object with:\n- success: boolean indicating if command succeeded\n- output: stdout/stderr combined output\n- exit_code: process exit code\n- duration_ms: execution time in milliseconds\n- lines_count: number of output lines\n- bytes_count: output size in bytes\n- Plus runtime metadata (shell, cwd, timestamps, etc.)"
    )
    
    generator.add_section(
        "Examples",
        "```bash\n# Get system information\nexecute_command.py --command \"uname -a\"\n\n# Check network connectivity\nexecute_command.py --command \"ping -c 3 8.8.8.8\" --timeout 15s\n\n# File operations\nexecute_command.py --command \"find . -name '*.py' -type f\" --working-dir /data/data/com.termux/files/home\n\n# Process management\nexecute_command.py --command \"ps aux | head -10\"\n\n# Package management\nexecute_command.py --command \"pkg list-installed\" --timeout 30s\n```"
    )
    
    generator.add_section(
        "Tips & Best Practices",
        "• Always validate user input before passing to --command\n• Use --timeout to prevent hanging commands\n• Leverage --use-cache for repetitive commands\n• Combine with --strip-ansi when processing output programmatically\n• Use --working-dir for context-specific operations\n• Environment variables are isolated per execution\n• The tool is safe for automated scripts and CI/CD pipelines"
    )
    
    # Generate the cheat sheet
    output_file = generator.generate()
    print(f"\n✅ Comprehensive cheat sheet generated: {output_file}")
    
    # Show a preview
    print("\n📄 Preview:")
    print("=" * 50)
    try:
        preview = open(output_file).read()
        lines = preview.split('\n')
        for i, line in enumerate(lines[:20]):  # Show first 20 lines
            print(f"{i+1:2}: {line}")
        if len(lines) > 20:
            print("   ...")
            print(f"   ... and {len(lines) - 20} more lines")
    except Exception as e:
        print(f"Could not preview file: {e}")

if __name__ == "__main__":
    main()
