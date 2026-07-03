#!/usr/bin/env python3
"""
Circuit-Synth New Project Setup Tool

Creates a complete circuit-synth project with:
- Claude AI agents registration (.claude/ directory)
- Example circuits (main.py + simple examples)
- Project README with usage guide
- KiCad installation verification
- Optional KiCad library setup
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.text import Text

# Import circuit-synth modules
from circuit_synth.ai_integration.claude import register_circuit_agents
from circuit_synth.core.kicad_validator import validate_kicad_installation

# Import new interactive CLI modules
from .interactive_cli import parse_cli_flags, run_interactive_setup
from .project_config import get_default_config
from .template_manager import CLAUDEMDGenerator, READMEGenerator, TemplateManager

console = Console()


def create_claude_directory_from_templates(
    project_path: Path, developer_mode: bool = False
) -> None:
    """Create a complete .claude directory structure using templates and agent registry

    Args:
        project_path: Target project directory
        developer_mode: If True, includes contributor agents and dev commands
    """
    dest_claude_dir = project_path / ".claude"
    dest_claude_dir.mkdir(exist_ok=True)

    console.print(
        "🤖 Setting up Claude Code integration from templates...", style="blue"
    )

    try:
        # Find the template directory
        circuit_synth_dir = Path(
            __file__
        ).parent.parent.parent  # Get to circuit_synth directory
        template_claude_dir = (
            circuit_synth_dir / "data" / "templates" / "example_project" / ".claude"
        )

        if template_claude_dir.exists():
            console.print(
                f"📋 Copying templates from {template_claude_dir}", style="blue"
            )

            # Copy the entire template .claude directory structure
            if dest_claude_dir.exists():
                shutil.rmtree(dest_claude_dir)
            shutil.copytree(template_claude_dir, dest_claude_dir)

            # Handle developer mode filtering
            commands_dir = dest_claude_dir / "commands"
            agents_dir = dest_claude_dir / "agents"

            if not developer_mode:
                # Remove dev commands (not needed for end users)
                dev_commands_to_remove = [
                    "development/dev-release-pypi.md",
                    "development/dev-review-branch.md",
                    "development/dev-review-repo.md",
                    "development/dev-run-tests.md",
                    "development/dev-update-and-commit.md",
                ]
                # Remove setup commands directory entirely for end users
                setup_dir = commands_dir / "setup"
                if setup_dir.exists():
                    shutil.rmtree(setup_dir)

                # Remove development commands directory for end users
                dev_commands_dir = commands_dir / "development"
                if dev_commands_dir.exists():
                    shutil.rmtree(dev_commands_dir)

                for cmd_file in dev_commands_to_remove:
                    cmd_path = commands_dir / cmd_file
                    if cmd_path.exists():
                        cmd_path.unlink()

                # Remove development agents (not needed for end users)
                dev_agents_to_remove = [
                    "development/contributor.md",
                    "development/first_setup_agent.md",
                    "development/circuit_generation_agent.md",
                ]
                for agent_file in dev_agents_to_remove:
                    agent_path = agents_dir / agent_file
                    if agent_path.exists():
                        agent_path.unlink()

                # Remove development agents directory if empty
                dev_agents_dir = agents_dir / "development"
                if dev_agents_dir.exists() and not any(dev_agents_dir.iterdir()):
                    dev_agents_dir.rmdir()

            console.print("✅ Copied complete template structure", style="green")

        else:
            console.print(
                "⚠️  Template directory not found, using basic setup", style="yellow"
            )
            # Fallback: just register agents
            register_circuit_agents()

        # Also register agents to update with any newer agent definitions
        register_circuit_agents()

        # Hooks removed - they caused more problems than they solved
        console.print("✅ Clean environment setup (no hooks)", style="green")

        # Remove mcp_settings.json as it's not needed for user projects
        mcp_settings_file = dest_claude_dir / "mcp_settings.json"
        if mcp_settings_file.exists():
            mcp_settings_file.unlink()

        # Count what was created
        agents_count = len(list((dest_claude_dir / "agents").rglob("*.md")))
        commands_count = len(list((dest_claude_dir / "commands").rglob("*.md")))

        console.print(f"📁 Agents available: {agents_count}", style="green")
        console.print(f"🔧 Commands available: {commands_count}", style="green")

        console.print(
            "✅ Created Claude directory structure with templates", style="green"
        )
        console.print(
            f"📁 Created project-local .claude in {dest_claude_dir}", style="blue"
        )

    except Exception as e:
        console.print(
            f"⚠️  Could not create complete Claude setup: {str(e)}", style="yellow"
        )
        # Fall back to basic agent registration
        register_circuit_agents()


def copy_complete_claude_setup(
    project_path: Path, developer_mode: bool = False
) -> None:
    """Copy the complete .claude directory from circuit-synth to new project

    Args:
        project_path: Target project directory
        developer_mode: If True, includes contributor agents and dev commands
    """

    # Find the circuit-synth root directory (where we have the complete .claude setup)
    circuit_synth_root = Path(__file__).parent.parent.parent.parent
    source_claude_dir = circuit_synth_root / ".claude"

    if not source_claude_dir.exists():
        console.print(
            "⚠️  Source .claude directory not found - using template-based setup",
            style="yellow",
        )
        # Use template-based approach to create complete .claude directory
        create_claude_directory_from_templates(project_path, developer_mode)
        return

    # Destination .claude directory in the new project
    dest_claude_dir = project_path / ".claude"

    console.print(f"📋 Copying Claude setup from {source_claude_dir}", style="blue")
    if developer_mode:
        console.print(
            "🔧 Developer mode: Including contributor agents and dev tools",
            style="cyan",
        )

    try:
        # Copy the entire .claude directory structure
        if dest_claude_dir.exists():
            shutil.rmtree(dest_claude_dir)
        shutil.copytree(source_claude_dir, dest_claude_dir)

        # Remove mcp_settings.json as it's not needed for user projects
        mcp_settings_file = dest_claude_dir / "mcp_settings.json"
        if mcp_settings_file.exists():
            mcp_settings_file.unlink()

        # Handle commands and agents based on mode
        commands_dir = dest_claude_dir / "commands"
        agents_dir = dest_claude_dir / "agents"

        if not developer_mode:
            # Remove dev commands (not needed for end users)
            dev_commands_to_remove = [
                "dev-release-pypi.md",
                "dev-review-branch.md",
                "dev-review-repo.md",
                "dev-run-tests.md",
                "dev-update-and-commit.md",
            ]
            # Remove setup commands directory entirely for end users
            setup_dir = commands_dir / "setup"
            if setup_dir.exists():
                shutil.rmtree(setup_dir)

            for cmd_file in dev_commands_to_remove:
                cmd_path = commands_dir / cmd_file
                if cmd_path.exists():
                    cmd_path.unlink()

            # Remove development agents (not needed for end users)
            dev_agents_to_remove = [
                "development/contributor.md",
                "development/first_setup_agent.md",
                "development/circuit_generation_agent.md",
            ]
            for agent_file in dev_agents_to_remove:
                agent_path = agents_dir / agent_file
                if agent_path.exists():
                    agent_path.unlink()

        else:
            console.print("✅ Keeping all developer tools and agents", style="green")

        console.print("✅ Copied all agents and commands", style="green")

        # Hooks removed - they caused more problems than they solved
        console.print("✅ Clean environment setup (no hooks)", style="green")

        # Count what was copied (now includes subdirectories)
        agents_count = len(list((dest_claude_dir / "agents").rglob("*.md")))
        commands_count = len(list((dest_claude_dir / "commands").rglob("*.md")))

        console.print(f"📁 Agents available: {agents_count}", style="green")
        console.print(f"🔧 Commands available: {commands_count}", style="green")

        # List key agents by category
        circuit_agents = []
        manufacturing_agents = []
        development_agents = []
        quality_agents = []

        for agent_file in (dest_claude_dir / "agents").rglob("*.md"):
            agent_name = agent_file.stem
            if "circuit" in agent_file.parent.name:
                circuit_agents.append(agent_name)
            elif "manufacturing" in agent_file.parent.name:
                manufacturing_agents.append(agent_name)
            elif "development" in agent_file.parent.name:
                development_agents.append(agent_name)
            elif "quality" in agent_file.parent.name:
                quality_agents.append(agent_name)

        if circuit_agents:
            console.print(
                f"🔌 Circuit agents: {', '.join(circuit_agents)}", style="cyan"
            )
        if manufacturing_agents:
            console.print(
                f"🏭 Manufacturing agents: {', '.join(manufacturing_agents)}",
                style="cyan",
            )
        if quality_agents:
            console.print(
                f"✅ Quality agents: {', '.join(quality_agents)}", style="cyan"
            )
        if development_agents and developer_mode:
            console.print(
                f"🔧 Development agents: {', '.join(development_agents)}", style="cyan"
            )

        # List some key commands
        key_commands = ["find-symbol", "find-footprint", "jlc-search"]
        if developer_mode:
            key_commands.extend(["dev-run-tests", "dev-review-branch"])

        available_commands = [
            f.stem for f in (dest_claude_dir / "commands").rglob("*.md")
        ]
        found_key_commands = [cmd for cmd in key_commands if cmd in available_commands]

        if found_key_commands:
            console.print(
                f"⚡ Key commands: /{', /'.join(found_key_commands)}", style="cyan"
            )

    except Exception as e:
        console.print(f"⚠️  Could not copy .claude directory: {e}", style="yellow")
        console.print("🔄 Falling back to basic agent registration", style="yellow")
        register_circuit_agents()


def check_kicad_installation() -> Dict[str, Any]:
    """Check KiCad installation and return path info (cross-platform)"""
    console.print("🔍 Checking KiCad installation...", style="yellow")

    try:
        result = validate_kicad_installation()

        # Check if KiCad CLI is available (main requirement)
        if result.get("cli_available", False):
            console.print("✅ KiCad found!", style="green")
            console.print(f"   🔧 CLI Path: {result.get('cli_path', 'Unknown')}")
            console.print(f"   📦 Version: {result.get('cli_version', 'Unknown')}")

            # Check libraries
            if result.get("libraries_available", False):
                console.print(
                    f"   📚 Symbol libraries: {result.get('symbol_path', 'Not found')}"
                )
                console.print(
                    f"   👟 Footprint libraries: {result.get('footprint_path', 'Not found')}"
                )
            else:
                console.print(
                    "   ⚠️  Libraries not found but CLI available", style="yellow"
                )

            result["kicad_installed"] = True
            return result
        else:
            console.print("❌ KiCad not found", style="red")
            console.print("📥 Install options:", style="cyan")

            # Cross-platform installation suggestions
            if sys.platform == "darwin":  # macOS
                console.print("   • Download: https://www.kicad.org/download/macos/")
                console.print("   • Homebrew: brew install kicad")
            elif sys.platform == "win32":  # Windows
                console.print("   • Download: https://www.kicad.org/download/windows/")
                console.print("   • Chocolatey: choco install kicad")
                console.print("   • Winget: winget install KiCad.KiCad")
            else:  # Linux
                console.print("   • Download: https://www.kicad.org/download/linux/")
                console.print("   • Ubuntu/Debian: sudo apt install kicad")
                console.print("   • Fedora: sudo dnf install kicad")
                console.print("   • Arch: sudo pacman -S kicad")

            result["kicad_installed"] = False
            return result

    except Exception as e:
        console.print(f"⚠️  Could not verify KiCad installation: {e}", style="yellow")
        return {"kicad_installed": False, "error": str(e)}


def copy_example_project_template(project_path: Path) -> bool:
    """Copy the entire example_project template to the target project directory

    Returns:
        bool: True if template was successfully copied, False otherwise
    """

    # Find the project template in the package data directory
    circuit_synth_dir = Path(
        __file__
    ).parent.parent.parent  # Get to circuit_synth directory
    template_dir = circuit_synth_dir / "data" / "templates" / "example_project"

    # Fallback: check for example_project in repo root (for development)
    if not template_dir.exists():
        circuit_synth_root = Path(__file__).parent.parent.parent.parent
        fallback_template = circuit_synth_root / "example_project"
        if fallback_template.exists():
            template_dir = fallback_template

    if not template_dir.exists():
        console.print(
            f"⚠️  Project template not found at {template_dir}", style="yellow"
        )
        console.print("🔄 Falling back to basic project creation", style="yellow")
        return False

    console.print(
        f"📋 Copying complete project template from {template_dir}", style="blue"
    )

    try:
        # Copy all files and directories from template to project_path
        for item in template_dir.iterdir():
            if item.is_file():
                # Copy individual files
                dest_file = project_path / item.name
                shutil.copy2(item, dest_file)
                console.print(f"   ✅ Copied {item.name}", style="green")
            elif item.is_dir():
                # Copy entire directories
                dest_dir = project_path / item.name
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.copytree(item, dest_dir)
                console.print(f"   ✅ Copied {item.name}/ directory", style="green")

        console.print("✅ Complete project template copied successfully", style="green")
        console.print(
            "   🎯 Ready-to-use ESP32-C6 development board example included!",
            style="cyan",
        )
        console.print(
            "   🤖 Claude Code agents and commands included from template!",
            style="cyan",
        )
        return True

    except Exception as e:
        console.print(f"⚠️  Could not copy project template: {e}", style="yellow")
        console.print(
            "🔄 Project setup will continue without template files", style="yellow"
        )
        return False


@click.command()
@click.option("--skip-kicad-check", is_flag=True, help="Skip KiCad installation check")
@click.option("--quick", is_flag=True, help="Quick start with defaults (no prompts)")
@click.option(
    "--circuits", type=str, help="Comma-separated circuits: resistor,led,esp32,usb"
)
@click.option("--no-agents", is_flag=True, help="Skip Claude AI agents setup")
@click.option("--developer", is_flag=True, help="Include developer tools")
def main(
    skip_kicad_check: bool,
    quick: bool,
    circuits: Optional[str],
    no_agents: bool,
    developer: bool,
):
    """Setup circuit-synth in the current uv project directory

    Run this command from within your uv project directory after:
    1. uv init my-project
    2. cd my-project
    3. uv add circuit-synth
    4. uv run cs-new-project

    Examples:
        # Interactive mode (default) - shows menu to select circuits
        uv run cs-new-project

        # Quick start with defaults (resistor divider)
        uv run cs-new-project --quick

        # Select specific circuits via flags
        uv run cs-new-project --circuits resistor,led,esp32

        # Minimal project without Claude agents
        uv run cs-new-project --circuits minimal --no-agents
    """

    # Use current directory as project path
    project_path = Path.cwd()

    # Remove default main.py created by uv init (we don't need it)
    default_main = project_path / "main.py"
    if default_main.exists():
        default_main.unlink()

    # Step 1: Check KiCad installation (unless skipped)
    kicad_installed = False
    if not skip_kicad_check:
        kicad_info = check_kicad_installation()
        kicad_installed = kicad_info.get("kicad_installed", False)
        if not kicad_installed:
            if not Confirm.ask(
                "Continue without KiCad? (You'll need it later for opening projects)"
            ):
                console.print("❌ Aborted - Please install KiCad first", style="red")
                sys.exit(1)

    # Step 2: Determine project configuration
    config = None

    if quick:
        # Quick mode: use defaults, no prompts
        console.print("[bold cyan]⚡ Quick Start Mode[/bold cyan]")
        config = get_default_config()
        if developer:
            config.developer_mode = True
        console.print(
            f"✅ Creating project with: [green]{', '.join([c.display_name for c in config.circuits])}[/green]"
        )
        console.print()

    elif circuits or no_agents:
        # Flag-based mode: parse flags into configuration
        config = parse_cli_flags(circuits, no_agents, developer)
        if config is None:
            sys.exit(1)  # parse_cli_flags already printed error

    else:
        # Interactive mode: run interactive CLI
        config = run_interactive_setup(project_path, developer_mode=developer)
        if config is None:
            console.print("[yellow]Setup cancelled by user[/yellow]")
            sys.exit(0)

    # Step 3: Initialize template manager and generators
    template_mgr = TemplateManager()
    readme_gen = READMEGenerator()
    claude_md_gen = CLAUDEMDGenerator()

    # Step 4: Create circuit-synth directory and copy all selected circuits
    console.print("\n[bold cyan]📝 Creating Project Files...[/bold cyan]")

    if config.has_circuits():
        for idx, circuit in enumerate(config.circuits):
            try:
                # First circuit becomes main.py, others use their own names
                is_first = idx == 0
                template_mgr.copy_circuit_to_project(
                    circuit, project_path, is_first=is_first
                )

                if is_first:
                    console.print(
                        f"✅ Created circuit-synth/main.py ({circuit.display_name})",
                        style="green",
                    )
                else:
                    console.print(
                        f"✅ Created circuit-synth/{circuit.value}.py ({circuit.display_name})",
                        style="green",
                    )

            except FileNotFoundError as e:
                console.print(
                    f"[yellow]⚠️  Could not add {circuit.display_name}: {e}[/yellow]"
                )
    else:
        console.print(
            "[yellow]⚠️  No circuits selected. Creating empty project.[/yellow]"
        )

    # Step 6: Setup Claude AI agents if requested
    if config.include_agents:
        console.print("\n[cyan]🤖 Setting up Claude Code integration...[/cyan]")
        try:
            copy_complete_claude_setup(
                project_path, developer_mode=config.developer_mode
            )
            agents_count = len(
                list((project_path / ".claude" / "agents").rglob("*.md"))
            )
            commands_count = len(
                list((project_path / ".claude" / "commands").rglob("*.md"))
            )
            console.print(
                f"✅ Claude agents setup complete ({agents_count} agents, {commands_count} commands)",
                style="green",
            )
        except Exception as e:
            console.print(f"[yellow]⚠️  Could not setup Claude agents: {e}[/yellow]")
    else:
        console.print("\n[dim]⏭️  Skipped Claude agents setup[/dim]")

    # Step 7: Generate README.md and CLAUDE.md
    console.print("\n[cyan]📚 Generating documentation...[/cyan]")

    readme_content = readme_gen.generate(config, project_path)
    readme_path = project_path / "README.md"
    readme_path.write_text(readme_content, encoding="utf-8")
    console.print("✅ Created README.md", style="green")

    claude_md_content = claude_md_gen.generate(config)
    claude_md_path = project_path / "CLAUDE.md"
    claude_md_path.write_text(claude_md_content, encoding="utf-8")
    console.print("✅ Created CLAUDE.md", style="green")

    # Step 8: KiCad plugins note (if KiCad is installed)
    if kicad_installed:
        console.print("\n[cyan]🔌 KiCad plugins available separately[/cyan]")
        console.print(
            "[dim]   Run 'uv run cs-setup-kicad-plugins' to install AI integration plugins[/dim]"
        )

    # Success message
    console.print()
    success_text = Text(
        f"✅ Circuit-synth project setup complete!", style="bold green"
    ) + Text(f"\n\n📁 Location: {project_path}")

    if config.has_circuits():
        circuits_names = ", ".join([c.display_name for c in config.circuits])
        success_text += Text(
            f"\n🎛️  Circuits ({len(config.circuits)}): {circuits_names}"
        )

    success_text += Text(
        f"\n\n🚀 Get started: [cyan]uv run python circuit-synth/main.py[/cyan]"
    )
    success_text += Text(f"\n📖 Documentation: See README.md")

    if config.has_circuits():
        success_text += Text(
            f"\n📦 Manufacturing: Templates auto-generate BOM and PDF"
        )

    if config.include_agents:
        agents_count = len(list((project_path / ".claude" / "agents").rglob("*.md")))
        commands_count = len(
            list((project_path / ".claude" / "commands").rglob("*.md"))
        )
        success_text += Text(
            f"\n🤖 AI Agents: {agents_count} agents, {commands_count} commands available"
        )

    console.print(Panel.fit(success_text, title="🎉 Success!", style="green"))


if __name__ == "__main__":
    main()
