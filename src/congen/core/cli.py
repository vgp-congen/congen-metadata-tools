"""The ``congen`` dispatcher."""

from __future__ import annotations

import click

from congen.core.tools import discover_tools


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="congen-metadata-tools")
def main() -> None:
    """Tools for VGP Conservation Genomics metadata."""


@main.command("list-tools")
def list_tools() -> None:
    """Show the registered tools and whether they write metadata."""
    tools = discover_tools()
    if not tools:
        click.echo("no tools registered")
        return
    width = max(len(t.name) for t in tools)
    for tool in tools:
        flags = []
        if tool.writes_metadata:
            flags.append("writes metadata")
        if tool.needs_network:
            flags.append("network")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        click.echo(f"{tool.name:{width}}  {tool.summary}{suffix}")


for _tool in discover_tools():
    if _tool.command is not None:
        main.add_command(_tool.command, name=_tool.name)
