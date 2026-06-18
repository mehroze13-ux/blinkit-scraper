"""
Analytics layer — derives business signals from the time-series SQLite database.

Key outputs:
  - Rating velocity  → estimated weekly unit sales per product
  - Rank movement    → who is gaining / losing shelf position
  - Price changes    → promo detection
  - Inventory signal → restock frequency, demand level
  - Rationing flag   → max_cart_qty < inventory = high-demand product
  - Category totals  → estimated category GMV and brand market share
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from database import get_all_products_between, get_latest_run_products, get_runs, DB_PATH
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

# Industry benchmark: ~2.5% of buyers leave a rating on quick-commerce platforms.
# Adjust this if you have a better estimate from your research.
REVIEW_RATE = 0.025


# ── Core analytics functions ──────────────────────────────────────────────────

def sales_estimate_from_rating_delta(delta_rating_count: int) -> int:
    """
    Convert a change in rating count over a period into an estimated unit sales figure.
    sales ≈ Δrating_count / REVIEW_RATE
    """
    if delta_rating_count <= 0:
        return 0
    return round(delta_rating_count / REVIEW_RATE)


def category_report(
    category_url: Optional[str] = None,
    days: int = 7,
    db_path: Path = DB_PATH,
) -> dict:
    """
    Build a full competitive intelligence report for a category.

    Returns a dict with:
      - products: per-product analytics rows
      - brand_summary: market share estimates per brand
      - category_totals: estimated GMV, units, etc.
    """
    now = datetime.now(timezone.utc)
    period_start = (now - timedelta(days=days)).isoformat()
    period_end   = now.isoformat()

    rows = get_all_products_between(period_start, period_end, category_url, db_path)
    if not rows:
        return {"error": "No data in the requested period. Run the scraper first."}

    # Group rows by product_id, sorted by scraped_at
    by_product: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("product_id"):
            by_product[row["product_id"]].append(row)

    product_analytics = []
    for pid, history in by_product.items():
        history.sort(key=lambda r: r["scraped_at"])
        first = history[0]
        last  = history[-1]

        # ── Rating velocity ───────────────────────────────────────────────────
        r_start = first.get("rating_count") or 0
        r_end   = last.get("rating_count")  or 0
        delta_ratings = max(0, r_end - r_start)
        est_units_period = sales_estimate_from_rating_delta(delta_ratings)

        # Weekly normalise
        actual_days = max(1, (
            datetime.fromisoformat(last["scraped_at"]) -
            datetime.fromisoformat(first["scraped_at"])
        ).days)
        est_units_weekly = round(est_units_period / actual_days * 7) if actual_days else 0

        # ── Rank movement ─────────────────────────────────────────────────────
        rank_first = first.get("rank")
        rank_last  = last.get("rank")
        rank_delta = None
        if rank_first is not None and rank_last is not None:
            rank_delta = rank_first - rank_last  # positive = moved up (better)

        rank_trend = "→ stable"
        if rank_delta is not None:
            if rank_delta > 2:
                rank_trend = f"↑ +{rank_delta} (gaining)"
            elif rank_delta < -2:
                rank_trend = f"↓ {rank_delta} (losing)"

        # ── Price changes ─────────────────────────────────────────────────────
        prices = [r["price"] for r in history if r.get("price")]
        price_min  = min(prices) if prices else None
        price_max  = max(prices) if prices else None
        price_now  = last.get("price")
        had_promo  = bool(price_min and price_max and price_max > price_min * 1.01)

        # ── OOS frequency ─────────────────────────────────────────────────────
        oos_count  = sum(1 for r in history if not r.get("in_stock"))
        oos_rate   = round(oos_count / len(history) * 100, 1) if history else 0

        # ── Inventory & rationing ─────────────────────────────────────────────
        inv_values = [r["inventory"] for r in history if r.get("inventory") is not None]
        inv_latest = last.get("inventory")
        max_cart   = last.get("max_cart_qty")
        is_rationed= bool(last.get("is_rationed"))

        # Inventory depletion signal: lowest recorded inventory vs latest
        inv_min = min(inv_values) if inv_values else None

        # Demand signal score (0–100)
        demand_score = _demand_score(
            oos_rate=oos_rate,
            is_rationed=is_rationed,
            rank=rank_last,
            est_units_weekly=est_units_weekly,
            delta_ratings=delta_ratings,
        )

        product_analytics.append({
            "product_id":         pid,
            "name":               last.get("name"),
            "brand":              last.get("brand"),
            "category":           last.get("category"),
            "sub_category":       last.get("sub_category"),

            # Listing
            "rank_current":       rank_last,
            "rank_start":         rank_first,
            "rank_delta":         rank_delta,
            "rank_trend":         rank_trend,

            # Pricing
            "price_current":      price_now,
            "price_min":          price_min,
            "price_max":          price_max,
            "had_promo":          had_promo,
            "discount_pct":       last.get("discount_pct"),
            "mrp":                last.get("mrp"),
            "size":               last.get("size"),

            # Ratings
            "rating_score":       last.get("rating_score"),
            "rating_count_start": r_start,
            "rating_count_now":   r_end,
            "rating_delta":       delta_ratings,

            # Sales estimates
            "est_units_in_period": est_units_period,
            "est_units_weekly":    est_units_weekly,
            "est_gmv_weekly":      round(est_units_weekly * (price_now or 0), 2),

            # Availability
            "in_stock_now":       bool(last.get("in_stock")),
            "oos_rate_pct":       oos_rate,
            "oos_count":          oos_count,

            # Inventory signals
            "inventory_now":      inv_latest,
            "inventory_min_seen": inv_min,
            "max_cart_qty":       max_cart,
            "is_rationed":        is_rationed,

            # Overall demand
            "demand_score":       demand_score,
            "data_points":        len(history),
            "period_days":        actual_days,
        })

    # Sort by demand_score descending
    product_analytics.sort(key=lambda x: x["demand_score"], reverse=True)

    # ── Brand summary ─────────────────────────────────────────────────────────
    brand_map: dict[str, dict] = defaultdict(lambda: {
        "products": 0, "est_units_weekly": 0, "est_gmv_weekly": 0.0,
        "avg_rating": [], "avg_discount": [],
    })
    for pa in product_analytics:
        b = pa.get("brand") or "Unknown"
        brand_map[b]["products"]         += 1
        brand_map[b]["est_units_weekly"] += pa["est_units_weekly"]
        brand_map[b]["est_gmv_weekly"]   += pa["est_gmv_weekly"]
        if pa.get("rating_score"):
            brand_map[b]["avg_rating"].append(pa["rating_score"])
        if pa.get("discount_pct"):
            brand_map[b]["avg_discount"].append(pa["discount_pct"])

    total_gmv = sum(b["est_gmv_weekly"] for b in brand_map.values()) or 1
    brand_summary = []
    for brand, data in sorted(brand_map.items(), key=lambda x: -x[1]["est_gmv_weekly"]):
        brand_summary.append({
            "brand":             brand,
            "products":          data["products"],
            "est_units_weekly":  data["est_units_weekly"],
            "est_gmv_weekly":    round(data["est_gmv_weekly"], 2),
            "market_share_pct":  round(data["est_gmv_weekly"] / total_gmv * 100, 1),
            "avg_rating":        round(sum(data["avg_rating"]) / len(data["avg_rating"]), 2)
                                 if data["avg_rating"] else None,
            "avg_discount_pct":  round(sum(data["avg_discount"]) / len(data["avg_discount"]), 1)
                                 if data["avg_discount"] else None,
        })

    # ── Category totals ───────────────────────────────────────────────────────
    category_totals = {
        "total_products_tracked":   len(product_analytics),
        "est_total_units_weekly":   sum(p["est_units_weekly"] for p in product_analytics),
        "est_total_gmv_weekly":     round(sum(p["est_gmv_weekly"] for p in product_analytics), 2),
        "avg_rating_score":         _avg([p["rating_score"] for p in product_analytics if p.get("rating_score")]),
        "pct_in_stock":             round(
            sum(1 for p in product_analytics if p["in_stock_now"]) / len(product_analytics) * 100, 1
        ) if product_analytics else 0,
        "rationed_products":        sum(1 for p in product_analytics if p["is_rationed"]),
        "period_days":              days,
        "note": (
            f"Sales estimates use rating-velocity method (review_rate={REVIEW_RATE*100:.1f}%). "
            "Accuracy improves with more data points over longer periods."
        ),
    }

    return {
        "generated_at":    now.isoformat(),
        "category_url":    category_url,
        "period_days":     days,
        "category_totals": category_totals,
        "brand_summary":   brand_summary,
        "products":        product_analytics,
    }


def rank_movers(
    category_url: Optional[str] = None,
    days: int = 7,
    db_path: Path = DB_PATH,
    top_n: int = 10,
) -> dict:
    """Return the biggest rank gainers and losers in the period."""
    report = category_report(category_url, days, db_path)
    if "error" in report:
        return report

    products = [p for p in report["products"] if p.get("rank_delta") is not None]
    gainers = sorted(products, key=lambda x: -(x["rank_delta"] or 0))[:top_n]
    losers  = sorted(products, key=lambda x:  (x["rank_delta"] or 0))[:top_n]

    return {
        "period_days": days,
        "gainers": gainers,
        "losers":  losers,
    }


def price_alerts(
    category_url: Optional[str] = None,
    days: int = 7,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """Return products whose price changed during the period."""
    report = category_report(category_url, days, db_path)
    if "error" in report:
        return []
    return [p for p in report["products"] if p.get("had_promo")]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _demand_score(
    oos_rate: float,
    is_rationed: bool,
    rank: Optional[int],
    est_units_weekly: int,
    delta_ratings: int,
) -> int:
    """
    Composite 0–100 demand score. Higher = more in-demand product.
    Weights:
      - OOS frequency: frequently out = high demand
      - Rationing: capped cart qty = very high demand
      - Rank: top positions = more visibility = more sales
      - Rating velocity: fast-growing count = high sales
    """
    score = 0.0

    # OOS rate (0–30 pts): being OOS often means strong demand
    score += min(30, oos_rate * 0.6)

    # Rationing (0–25 pts)
    if is_rationed:
        score += 25

    # Rank (0–25 pts): rank 1 = 25 pts, rank 10 = 15 pts, rank 50+ = 5 pts
    if rank:
        score += max(5, 25 - (rank - 1) * 0.5)

    # Rating velocity (0–20 pts)
    if delta_ratings > 0:
        score += min(20, delta_ratings * 2)

    return min(100, round(score))


def _avg(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None
