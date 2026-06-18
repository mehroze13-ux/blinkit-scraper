"""
CLI entry point for the Blinkit scraper.

Examples:
    # Scrape a single product
    python main.py product "https://blinkit.com/prn/amul-butter/prid/3065"

    # Scrape a category (default 100 products, saved to DB)
    python main.py category "https://blinkit.com/cn/munchies/makhana-more/cid/1237/816"

    # Just collect links (dry run, no scraping)
    python main.py links "https://blinkit.com/cn/munchies/makhana-more/cid/1237/816"

    # Search
    python main.py search "makhana" --max 20

    # Show run history
    python main.py db

    # Show analytics report
    python main.py analytics "https://blinkit.com/cn/munchies/makhana-more/cid/1237/816"

    # Run with visible browser (good for debugging)
    python main.py category "..." --no-headless
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from scraper import BlinkitScraper
import config

app = typer.Typer(help="Blinkit product scraper + competitive intelligence", add_completion=False)
console = Console()


# ── Shared helpers ────────────────────────────────────────────────────────────

def _save(data: dict | list, filename: str) -> Path:
    out_dir = Path(config.OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return path


def _display_product(product: dict) -> None:
    table = Table(title=product.get("name", "Product"), show_lines=True, expand=False)
    table.add_column("Field", style="cyan", no_wrap=True, min_width=22)
    table.add_column("Value", style="white", overflow="fold")

    skip = {"image_urls", "variants", "rating", "nutrition"}
    for k, v in product.items():
        if k in skip or v is None or v == "" or v == []:
            continue
        table.add_row(k, str(v))

    if product.get("rating"):
        r = product["rating"]
        if r.get("score") is not None:
            table.add_row("rating_score", str(r["score"]))
        if r.get("count") is not None:
            table.add_row("rating_count", str(r["count"]))
        if r.get("review_count") is not None:
            table.add_row("review_count", str(r["review_count"]))

    if product.get("nutrition"):
        n = product["nutrition"]
        for nk, nv in n.items():
            if nv is not None and nk != "raw_nutrition_text":
                table.add_row(f"  {nk}", str(nv))

    if product.get("variants"):
        table.add_row("variants", ", ".join(v["label"] for v in product["variants"] if v["label"]))

    if product.get("image_urls"):
        table.add_row("images", f"{len(product['image_urls'])} image(s)")

    console.print(table)


def _summary_table(products) -> Table:
    """Rich summary table shown after a category scrape."""
    t = Table(title="Scraped Products", show_lines=True, expand=False)
    t.add_column("#", style="dim", width=4)
    t.add_column("Rank", style="dim", width=5)
    t.add_column("Name", style="bold white", max_width=34)
    t.add_column("Brand", style="cyan", max_width=14)
    t.add_column("Size", style="yellow", max_width=10)
    t.add_column("Price", style="green", width=8)
    t.add_column("MRP", style="dim", width=8)
    t.add_column("Rating", style="magenta", width=12)
    t.add_column("Inv", style="blue", width=5)
    t.add_column("MaxQ", style="blue", width=5)
    t.add_column("Stock", style="white", width=7)

    for i, p in enumerate(products, 1):
        rating_str = "—"
        if p.rating and p.rating.score is not None:
            rating_str = f"{p.rating.score}"
            if p.rating.count:
                rating_str += f"/{p.rating.count}"

        inv_str  = str(p.inventory)   if p.inventory   is not None else "—"
        maxq_str = str(p.max_cart_qty) if p.max_cart_qty is not None else "—"
        rank_str = str(p.rank)         if p.rank         is not None else "—"

        stock_str = "[green]Yes[/green]" if p.in_stock else "[red]No[/red]"
        if p.is_rationed:
            maxq_str = f"[red]{maxq_str}![/red]"   # rationing alert

        t.add_row(
            str(i),
            rank_str,
            (p.name or "")[:34],
            (p.brand or "—")[:14],
            (p.size or "—")[:10],
            f"₹{p.price}"  if p.price  else "—",
            f"₹{p.mrp}"    if p.mrp    else "—",
            rating_str,
            inv_str,
            maxq_str,
            stock_str,
        )
    return t


# ── Commands ──────────────────────────────────────────────────────────────────

@app.command()
def product(
    url: str = typer.Argument(..., help="Full Blinkit product URL"),
    lat: float = typer.Option(config.DEFAULT_LAT, help="Delivery latitude"),
    lng: float = typer.Option(config.DEFAULT_LNG, help="Delivery longitude"),
    pincode: str = typer.Option(config.DEFAULT_PINCODE, help="Delivery pincode"),
    headless: bool = typer.Option(config.HEADLESS),
    output: Optional[str] = typer.Option(None, help="Output filename in output/"),
    proxy: str = typer.Option(config.PROXY_URL),
) -> None:
    """Scrape a single product page."""

    async def run() -> None:
        scraper = BlinkitScraper(lat=lat, lng=lng, pincode=pincode, headless=headless, proxy_url=proxy)
        await scraper.start()
        try:
            console.print(Panel(f"[bold]Scraping product[/bold]\n{url}", expand=False))
            p = await scraper.scrape_product(url)
            if p:
                data = p.model_dump()
                _display_product(data)
                fname = output or f"product_{p.product_id or 'unknown'}.json"
                saved = _save(data, fname)
                console.print(f"\n[green]Saved →[/green] {saved}")
            else:
                console.print("[red]No product data extracted.[/red]")
        finally:
            await scraper.close()

    asyncio.run(run())


@app.command()
def links(
    url: str = typer.Argument(..., help="Blinkit category or search URL"),
    max: int = typer.Option(100, "--max", "-n", help="Max links to collect"),
    lat: float = typer.Option(config.DEFAULT_LAT),
    lng: float = typer.Option(config.DEFAULT_LNG),
    pincode: str = typer.Option(config.DEFAULT_PINCODE),
    headless: bool = typer.Option(config.HEADLESS),
    proxy: str = typer.Option(config.PROXY_URL),
    output: Optional[str] = typer.Option(None, help="Save link list to this filename"),
) -> None:
    """Phase 1 only — collect product links without scraping. Good for verifying URLs."""

    async def run() -> None:
        scraper = BlinkitScraper(lat=lat, lng=lng, pincode=pincode, headless=headless, proxy_url=proxy)
        await scraper.start()
        try:
            console.print(Panel(f"[bold]Collecting product links[/bold]\n{url}", expand=False))
            page = await scraper._new_page()
            try:
                await scraper._set_location(page)
                await page.goto(url, wait_until="load", timeout=config.BROWSER_TIMEOUT_MS)
                await asyncio.sleep(3)

                if await scraper._is_blocked(page):
                    console.print("[red]Bot detection triggered.[/red]")
                    return

                ranked_urls: list[tuple[str, int]] = await scraper.collect_all_links_from_page(page, max)
            finally:
                await page.close()

            console.print(f"\n[bold green]Collected {len(ranked_urls)} product link(s):[/bold green]\n")
            for product_url, rank in ranked_urls:
                console.print(f"  [dim]{rank:>3}.[/dim] {product_url}")

            if output:
                saved = _save([{"rank": rank, "url": u} for u, rank in ranked_urls], output)
                console.print(f"\n[green]Saved →[/green] {saved}")
        finally:
            await scraper.close()

    asyncio.run(run())


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    max: int = typer.Option(20, "--max", "-n", help="Max products to scrape"),
    lat: float = typer.Option(config.DEFAULT_LAT),
    lng: float = typer.Option(config.DEFAULT_LNG),
    pincode: str = typer.Option(config.DEFAULT_PINCODE),
    headless: bool = typer.Option(config.HEADLESS),
    output: Optional[str] = typer.Option(None),
    proxy: str = typer.Option(config.PROXY_URL),
    save_db: bool = typer.Option(False, "--save-db", help="Save results to SQLite database"),
) -> None:
    """Search Blinkit and scrape all results."""

    async def run() -> None:
        scraper = BlinkitScraper(lat=lat, lng=lng, pincode=pincode, headless=headless, proxy_url=proxy)
        await scraper.start()
        try:
            console.print(Panel(f"[bold]Search:[/bold] {query!r}   (max {max})", expand=False))

            search_url = f"https://blinkit.com/s/?q={query.replace(' ', '+')}"
            page = await scraper._new_page()
            try:
                await scraper._set_location(page)
                await page.goto(search_url, wait_until="load", timeout=config.BROWSER_TIMEOUT_MS)
                await asyncio.sleep(3)
                if await scraper._is_blocked(page):
                    console.print("[red]Bot detection triggered.[/red]")
                    return
                ranked_urls: list[tuple[str, int]] = await scraper.collect_all_links_from_page(page, max)
            finally:
                await page.close()

            console.print(f"\n[bold]Phase 1 — {len(ranked_urls)} link(s) found[/bold]")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Scraping…", total=len(ranked_urls))
                products = await scraper._scrape_url_list(ranked_urls, progress, task)

            console.print(f"\n[bold green]Done — {len(products)} product(s) scraped.[/bold green]\n")
            console.print(_summary_table(products))

            if save_db:
                from database import save_run
                run_id = save_run(search_url, products, pincode)
                console.print(f"[green]Saved to DB → run_id={run_id}[/green]")

            data = [p.model_dump() for p in products]
            fname = output or f"search_{query.replace(' ', '_')}.json"
            saved = _save(data, fname)
            console.print(f"[green]JSON saved →[/green] {saved}")
        finally:
            await scraper.close()

    asyncio.run(run())


@app.command()
def category(
    url: str = typer.Argument(..., help="Blinkit category page URL"),
    max: int = typer.Option(100, "--max", "-n", help="Max products to scrape"),
    lat: float = typer.Option(config.DEFAULT_LAT),
    lng: float = typer.Option(config.DEFAULT_LNG),
    pincode: str = typer.Option(config.DEFAULT_PINCODE),
    headless: bool = typer.Option(config.HEADLESS),
    output: Optional[str] = typer.Option(None),
    proxy: str = typer.Option(config.PROXY_URL),
    save_db: bool = typer.Option(True, "--save-db/--no-save-db", help="Save results to SQLite database"),
) -> None:
    """
    Scrape every product on a Blinkit category page.

    Phase 1: scroll the listing, collect all product links + rank positions.
    Phase 2: visit each product page and extract full details.
    Results are saved to SQLite (time-series) and JSON by default.
    """

    async def run() -> None:
        scraper = BlinkitScraper(lat=lat, lng=lng, pincode=pincode, headless=headless, proxy_url=proxy)
        await scraper.start()
        try:
            console.print(Panel(
                f"[bold]Category scrape[/bold]\n{url}\n\n"
                f"Location: {lat}, {lng}  |  Pincode: {pincode}  |  Max: {max}  |  Save DB: {save_db}",
                expand=False,
            ))

            # ── Phase 1 ───────────────────────────────────────────────────────
            console.print("\n[bold yellow]Phase 1:[/bold yellow] Collecting product links…\n")
            page = await scraper._new_page()
            try:
                await scraper._set_location(page)
                await page.goto(url, wait_until="load", timeout=config.BROWSER_TIMEOUT_MS)
                await asyncio.sleep(3)

                if await scraper._is_blocked(page):
                    console.print("[red]Bot detection triggered on category page.[/red]")
                    return

                ranked_urls: list[tuple[str, int]] = await scraper.collect_all_links_from_page(page, max)
            finally:
                await page.close()

            if not ranked_urls:
                console.print("[red]No product links found. Try --no-headless to debug.[/red]")
                return

            console.print(f"[bold green]{len(ranked_urls)} product link(s) found.[/bold green]")

            # ── Phase 2 ───────────────────────────────────────────────────────
            console.print(f"\n[bold yellow]Phase 2:[/bold yellow] Scraping {len(ranked_urls)} product(s)…\n")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[cyan]{task.completed}[/cyan]/[cyan]{task.total}[/cyan]"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Scraping products…", total=len(ranked_urls))
                products = await scraper._scrape_url_list(ranked_urls, progress, task)

            console.print(f"\n[bold green]Done — {len(products)} product(s) scraped.[/bold green]\n")
            console.print(_summary_table(products))

            # ── DB save ───────────────────────────────────────────────────────
            if save_db:
                from database import save_run
                run_id = save_run(url, products, pincode)
                console.print(f"\n[green]Saved to DB → run_id={run_id}[/green]")

            # ── JSON save ─────────────────────────────────────────────────────
            data = [p.model_dump() for p in products]
            fname = output or "category_products.json"
            saved = _save(data, fname)
            console.print(f"[green]JSON saved →[/green] {saved}")
        finally:
            await scraper.close()

    asyncio.run(run())


@app.command(name="db")
def show_db(
    limit: int = typer.Option(20, "--limit", "-n", help="Max runs to show"),
) -> None:
    """Show scrape run history from the SQLite database."""
    from database import get_runs, init_db
    init_db()
    runs = get_runs(limit=limit)

    if not runs:
        console.print("[yellow]No runs found. Run 'category' or 'search' with --save-db first.[/yellow]")
        return

    t = Table(title="Scrape Run History", show_lines=True)
    t.add_column("Run ID", style="dim", width=7)
    t.add_column("Scraped At", style="cyan", width=22)
    t.add_column("Category URL", style="white", overflow="fold")
    t.add_column("Products", style="green", width=9)
    t.add_column("Pincode", style="yellow", width=8)

    for r in runs:
        t.add_row(
            str(r["id"]),
            r["scraped_at"][:19],
            r["category_url"],
            str(r["product_count"]),
            r.get("pincode") or "—",
        )
    console.print(t)


@app.command()
def analytics(
    url: str = typer.Argument(..., help="Category URL to analyse"),
    days: int = typer.Option(7, "--days", "-d", help="Look-back period in days"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save JSON report to output/"),
    top: int = typer.Option(20, "--top", help="Show top N products in the table"),
) -> None:
    """
    Print a competitive intelligence report for a category.

    Shows: rank movers, sales estimates, brand market share, rationed products.
    Needs at least 2 scrape runs to produce meaningful deltas.
    """
    from analytics import category_report

    console.print(Panel(
        f"[bold]Analytics report[/bold]\n{url}\nPeriod: {days} day(s)",
        expand=False,
    ))

    report = category_report(category_url=url, days=days)

    if "error" in report:
        console.print(f"[red]{report['error']}[/red]")
        raise typer.Exit(1)

    ct = report["category_totals"]
    console.print(Panel(
        f"[bold]Category totals ({days}d)[/bold]\n"
        f"Products tracked:  {ct['total_products_tracked']}\n"
        f"Est. weekly units: {ct['est_total_units_weekly']:,}\n"
        f"Est. weekly GMV:   ₹{ct['est_total_gmv_weekly']:,.0f}\n"
        f"In-stock rate:     {ct['pct_in_stock']}%\n"
        f"Rationed products: {ct['rationed_products']}\n"
        f"\n[dim]{ct['note']}[/dim]",
        expand=False,
    ))

    # Brand market share
    bs = report["brand_summary"]
    if bs:
        bt = Table(title="Brand Market Share", show_lines=True)
        bt.add_column("Brand", style="cyan", max_width=22)
        bt.add_column("Products", style="white", width=9)
        bt.add_column("Est Units/wk", style="green", width=13)
        bt.add_column("Est GMV/wk", style="green", width=12)
        bt.add_column("Share %", style="yellow", width=9)
        bt.add_column("Avg Rating", style="magenta", width=11)

        for b in bs:
            bt.add_row(
                b["brand"],
                str(b["products"]),
                f"{b['est_units_weekly']:,}",
                f"₹{b['est_gmv_weekly']:,.0f}",
                f"{b['market_share_pct']}%",
                str(b.get("avg_rating") or "—"),
            )
        console.print(bt)

    # Per-product table (top N)
    products = report["products"][:top]
    if products:
        pt = Table(title=f"Top {len(products)} Products by Demand Score", show_lines=True)
        pt.add_column("Score", style="bold yellow", width=7)
        pt.add_column("Rank", style="dim", width=6)
        pt.add_column("Name", style="white", max_width=32)
        pt.add_column("Brand", style="cyan", max_width=14)
        pt.add_column("Price", style="green", width=8)
        pt.add_column("Rating", style="magenta", width=12)
        pt.add_column("Δ Ratings", style="blue", width=10)
        pt.add_column("Est/wk", style="green", width=8)
        pt.add_column("OOS %", style="red", width=7)
        pt.add_column("Rationed", style="red", width=9)
        pt.add_column("Rank Trend", style="white", width=14)

        for p in products:
            rank_str = str(p["rank_current"]) if p["rank_current"] is not None else "—"
            if p.get("rank_delta") is not None:
                rank_str += f" ({p['rank_trend'].split()[0]})"

            rating_str = "—"
            if p.get("rating_score") is not None:
                rating_str = str(p["rating_score"])
                if p.get("rating_count_now"):
                    rating_str += f"/{p['rating_count_now']}"

            pt.add_row(
                str(p["demand_score"]),
                str(p["rank_current"] or "—"),
                (p["name"] or "")[:32],
                (p["brand"] or "—")[:14],
                f"₹{p['price_current']}" if p.get("price_current") else "—",
                rating_str,
                f"+{p['rating_delta']}" if p["rating_delta"] else "—",
                f"{p['est_units_weekly']:,}" if p.get("est_units_weekly") else "—",
                f"{p['oos_rate_pct']}%",
                "[red]YES[/red]" if p["is_rationed"] else "no",
                p["rank_trend"],
            )
        console.print(pt)

    if output:
        saved = _save(report, output)
        console.print(f"\n[green]Report saved →[/green] {saved}")


if __name__ == "__main__":
    app()
