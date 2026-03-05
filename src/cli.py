from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from src.pipeline import Pipeline
from src.utils.file_utils import load_json

console = Console()


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
def main():
    """IIPS – Intelligent Invoice Processing System"""
    pass


@main.command()
@click.argument("bundle_path", type=click.Path(exists=True))
@click.option("--output", "-o", default="runs", help="Output directory for run artifacts")
@click.option("--policy", "-p", default=None, help="Path to policy YAML file")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
def process(bundle_path: str, output: str, policy: str | None, verbose: bool):
    """Process an invoice bundle through the full pipeline."""
    setup_logging(verbose)

    console.print(Panel.fit(
        "[bold blue]IIPS – Intelligent Invoice Processing System[/bold blue]\n"
        f"Bundle: {bundle_path}",
        border_style="blue",
    ))

    try:
        pipeline = Pipeline(
            bundle_path=bundle_path,
            output_dir=output,
            policy_path=policy,
        )
        context = pipeline.run()

        # Display results
        _display_results(context, pipeline.run_dir)

    except Exception as e:
        console.print(f"[red bold]Pipeline failed:[/red bold] {e}")
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


@main.command()
@click.argument("run_dir", type=click.Path(exists=True))
def inspect(run_dir: str):
    """Inspect artifacts from a previous run."""
    run_path = Path(run_dir)
    console.print(f"\n[bold]Inspecting run: {run_path.name}[/bold]\n")

    artifacts = sorted(run_path.glob("*"))
    if not artifacts:
        console.print("[yellow]No artifacts found.[/yellow]")
        return

    tree = Tree(f" {run_path.name}")
    for f in artifacts:
        size = f.stat().st_size
        tree.add(f" {f.name} ({size:,} bytes)")

    console.print(tree)

    # Show final decision if available
    decision_path = run_path / "final_decision.json"
    if decision_path.exists():
        data = load_json(decision_path)
        console.print("\n[bold]Final Decision:[/bold]")
        table = Table()
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Decision", data.get("decision", "N/A"))
        table.add_row("Reason", data.get("reason", "N/A"))
        table.add_row("Risk Score", str(data.get("risk_score", "N/A")))
        table.add_row("Invoice", data.get("invoice_number", "N/A"))
        table.add_row("Vendor", data.get("vendor_name", "N/A"))
        table.add_row("Amount", f"{data.get('currency', '')} {data.get('total_amount', 'N/A')}")
        console.print(table)


@main.command(name="list")
@click.option("--output", "-o", default="runs", help="Output directory")
def list_runs(output: str):
    """List all pipeline runs."""
    runs_dir = Path(output)
    if not runs_dir.exists():
        console.print("[yellow]No runs directory found.[/yellow]")
        return

    runs = sorted(runs_dir.iterdir())
    if not runs:
        console.print("[yellow]No runs found.[/yellow]")
        return

    table = Table(title="Pipeline Runs")
    table.add_column("Run ID", style="cyan")
    table.add_column("Decision", style="green")
    table.add_column("Artifacts", style="yellow")

    for run_path in runs:
        if not run_path.is_dir():
            continue
        artifacts = len(list(run_path.glob("*")))
        decision = "N/A"
        decision_path = run_path / "final_decision.json"
        if decision_path.exists():
            data = load_json(decision_path)
            decision = data.get("decision", "N/A")
        table.add_row(run_path.name, decision, str(artifacts))

    console.print(table)


def _display_results(context: dict, run_dir: Path) -> None:
    """Display pipeline results in a rich format."""
    decision = context.get("final_decision")
    if not decision:
        console.print("[yellow]No final decision produced.[/yellow]")
        return

    # Decision panel
    color = {
        "auto_post": "green",
        "approve_and_post": "blue",
        "route_for_approval": "yellow",
        "hold": "red",
        "reject": "red",
        "manual_review": "yellow",
    }.get(decision.decision.value, "white")

    console.print()
    console.print(Panel.fit(
        f"[bold {color}]{decision.decision.value.upper()}[/bold {color}]\n\n"
        f"{decision.reason}",
        title="Final Decision",
        border_style=color,
    ))

    # Summary table
    table = Table(title="Processing Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Invoice", decision.invoice_number or "N/A")
    table.add_row("Vendor", decision.vendor_name or "N/A")
    table.add_row("Amount", f"{decision.currency} {decision.total_amount or 'N/A'}")
    table.add_row("Risk Score", f"{decision.risk_score}/10")
    table.add_row("Confidence", f"{decision.confidence:.0%}")
    table.add_row("Total Findings", str(len(decision.all_findings)))
    table.add_row("Critical", str(len(decision.critical_findings)))

    console.print(table)

    # Findings summary
    if decision.all_findings:
        console.print("\n[bold]Findings:[/bold]")
        for f in decision.all_findings[:10]:
            icon = {"critical": "-", "error": "-", "warning": "-", "info": "-"}.get(f.severity.value, "-")
            console.print(f"  {icon} [{f.severity.value}] {f.title}")

        if len(decision.all_findings) > 10:
            console.print(f"  ... and {len(decision.all_findings) - 10} more")

    # Artifacts
    console.print(f"\n[bold]Artifacts saved to:[/bold] {run_dir}")
    for artifact in sorted(run_dir.glob("*")):
        console.print(f"   {artifact.name}")

    console.print()


if __name__ == "__main__":
    main()