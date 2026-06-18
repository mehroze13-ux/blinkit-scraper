"""
Blinkit Competitive Intelligence Dashboard
Reads from the SQLite time-series database built by the scraper.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Blinkit Intelligence",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = Path(__file__).parent / "output" / "blinkit.db"

# ── DB helpers ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_runs() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql("SELECT * FROM runs ORDER BY scraped_at DESC", conn)


@st.cache_data(ttl=300)
def load_products(category_url: str, days: int) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql("""
            SELECT p.*, r.category_url
            FROM products p
            JOIN runs r ON p.run_id = r.id
            WHERE r.category_url = ? AND p.scraped_at >= ?
            ORDER BY p.scraped_at ASC
        """, conn, params=(category_url, cutoff))


def no_data_msg():
    st.warning(
        "No data yet. The scraper hasn't run or the database hasn't been committed to the repo. "
        "Check the **Actions** tab on GitHub."
    )
    st.stop()


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🛒 Blinkit Intel")

    runs_df = load_runs()
    if runs_df.empty:
        no_data_msg()

    if runs_df.empty or "category_url" not in runs_df.columns:
        no_data_msg()
    category_options = runs_df["category_url"].unique().tolist()
    selected_url = st.selectbox("Category", category_options)

    days = st.slider("Look-back period (days)", 1, 30, 7)

    st.divider()
    last_run = runs_df[runs_df["category_url"] == selected_url].iloc[0]
    st.caption(f"Last scraped: {last_run['scraped_at'][:16]} UTC")
    st.caption(f"Total runs: {len(runs_df[runs_df['category_url'] == selected_url])}")
    st.caption(f"DB: `output/blinkit.db`")

# ── Load data ─────────────────────────────────────────────────────────────────

df = load_products(selected_url, days)
if df.empty:
    no_data_msg()

df["scraped_at"] = pd.to_datetime(df["scraped_at"])
df["price"] = pd.to_numeric(df["price"], errors="coerce")
df["mrp"] = pd.to_numeric(df["mrp"], errors="coerce")
df["rating_score"] = pd.to_numeric(df["rating_score"], errors="coerce")
df["rating_count"] = pd.to_numeric(df["rating_count"], errors="coerce")
df["inventory"] = pd.to_numeric(df["inventory"], errors="coerce")
df["rank"] = pd.to_numeric(df["rank"], errors="coerce")

# Latest snapshot per product
latest = (
    df.sort_values("scraped_at")
    .groupby("product_id")
    .last()
    .reset_index()
)

# Earliest snapshot per product (for deltas)
earliest = (
    df.sort_values("scraped_at")
    .groupby("product_id")
    .first()
    .reset_index()
    [["product_id", "rating_count", "rank", "price"]]
    .rename(columns={"rating_count": "rating_count_start", "rank": "rank_start", "price": "price_start"})
)

merged = latest.merge(earliest, on="product_id", how="left")
merged["rating_delta"] = (merged["rating_count"] - merged["rating_count_start"]).clip(lower=0)
merged["est_units_weekly"] = (merged["rating_delta"] / 0.025).round().astype("Int64")
merged["est_gmv_weekly"] = (merged["est_units_weekly"] * merged["price"]).round(2)
merged["rank_delta"] = merged["rank_start"] - merged["rank"]  # positive = moved up
merged["is_rationed"] = merged["is_rationed"].fillna(0).astype(bool)
merged["in_stock"] = merged["in_stock"].fillna(1).astype(bool)

# OOS frequency
oos_rate = (
    df.groupby("product_id")
    .apply(lambda g: (g["in_stock"] == 0).mean() * 100)
    .reset_index()
    .rename(columns={0: "oos_rate_pct"})
)
merged = merged.merge(oos_rate, on="product_id", how="left")

# ── KPI row ───────────────────────────────────────────────────────────────────

st.title("Blinkit Category Intelligence")
st.caption(f"Category: `{selected_url}`  |  Period: {days} day(s)")

total_gmv = merged["est_gmv_weekly"].sum()
total_units = merged["est_units_weekly"].sum()
in_stock_pct = merged["in_stock"].mean() * 100
rationed_count = merged["is_rationed"].sum()
total_products = len(merged)

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Products Tracked", total_products)
k2.metric("Est. Weekly Units", f"{int(total_units):,}")
k3.metric("Est. Weekly GMV", f"₹{total_gmv:,.0f}")
k4.metric("In-Stock Rate", f"{in_stock_pct:.0f}%")
k5.metric("Rationed Products 🔴", int(rationed_count))

st.divider()

# ── Brand market share ────────────────────────────────────────────────────────

col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("Brand Market Share")
    brand_df = (
        merged.groupby("brand")
        .agg(
            products=("product_id", "count"),
            est_units_weekly=("est_units_weekly", "sum"),
            est_gmv_weekly=("est_gmv_weekly", "sum"),
            avg_rating=("rating_score", "mean"),
        )
        .reset_index()
        .sort_values("est_gmv_weekly", ascending=False)
    )
    brand_df["market_share_pct"] = (brand_df["est_gmv_weekly"] / brand_df["est_gmv_weekly"].sum() * 100).round(1)
    brand_df["avg_rating"] = brand_df["avg_rating"].round(2)

    fig_pie = px.pie(
        brand_df.head(10),
        values="est_gmv_weekly",
        names="brand",
        hole=0.4,
        color_discrete_sequence=px.colors.qualitative.Set3,
    )
    fig_pie.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=320)
    st.plotly_chart(fig_pie, use_container_width=True)

with col2:
    st.subheader("Brand Table")
    display_brand = brand_df[["brand", "products", "est_units_weekly", "est_gmv_weekly", "market_share_pct", "avg_rating"]].copy()
    display_brand.columns = ["Brand", "Products", "Units/wk", "GMV/wk (₹)", "Share %", "Avg Rating"]
    display_brand["GMV/wk (₹)"] = display_brand["GMV/wk (₹)"].map(lambda x: f"₹{x:,.0f}")
    display_brand["Units/wk"] = display_brand["Units/wk"].map(lambda x: f"{int(x):,}")
    st.dataframe(display_brand, hide_index=True, use_container_width=True, height=320)

st.divider()

# ── Rank movement ─────────────────────────────────────────────────────────────

col3, col4 = st.columns([1, 1])

with col3:
    st.subheader("🚀 Biggest Rank Gainers")
    gainers = (
        merged[merged["rank_delta"].notna() & (merged["rank_delta"] > 0)]
        .nlargest(10, "rank_delta")
        [["name", "brand", "rank", "rank_start", "rank_delta"]]
    )
    gainers.columns = ["Product", "Brand", "Rank Now", "Rank Before", "Positions Gained"]
    st.dataframe(gainers, hide_index=True, use_container_width=True)

with col4:
    st.subheader("📉 Biggest Rank Losers")
    losers = (
        merged[merged["rank_delta"].notna() & (merged["rank_delta"] < 0)]
        .nsmallest(10, "rank_delta")
        [["name", "brand", "rank", "rank_start", "rank_delta"]]
    )
    losers["rank_delta"] = losers["rank_delta"].abs()
    losers.columns = ["Product", "Brand", "Rank Now", "Rank Before", "Positions Lost"]
    st.dataframe(losers, hide_index=True, use_container_width=True)

st.divider()

# ── Rating velocity over time (top 10 products) ───────────────────────────────

st.subheader("📈 Rating Velocity Over Time (Top 10 by sales estimate)")

top_pids = merged.nlargest(10, "est_units_weekly")["product_id"].tolist()
velocity_df = df[df["product_id"].isin(top_pids)][["scraped_at", "product_id", "name", "rating_count"]].copy()
velocity_df = velocity_df.merge(
    df[["product_id", "name"]].drop_duplicates("product_id"),
    on="product_id", how="left", suffixes=("", "_y")
)
velocity_df["label"] = velocity_df["name"].str[:30]

fig_vel = px.line(
    velocity_df,
    x="scraped_at",
    y="rating_count",
    color="label",
    markers=True,
    labels={"scraped_at": "Time", "rating_count": "Total Ratings", "label": "Product"},
)
fig_vel.update_layout(height=380, margin=dict(t=10))
st.plotly_chart(fig_vel, use_container_width=True)

st.divider()

# ── OOS tracker ───────────────────────────────────────────────────────────────

col5, col6 = st.columns([1, 1])

with col5:
    st.subheader("⚠️ Out-of-Stock Frequency")
    oos_display = (
        merged[merged["oos_rate_pct"] > 0]
        .nlargest(15, "oos_rate_pct")
        [["name", "brand", "oos_rate_pct", "in_stock"]]
    )
    if oos_display.empty:
        st.success("All products were in stock during this period.")
    else:
        fig_oos = px.bar(
            oos_display,
            x="oos_rate_pct",
            y="name",
            orientation="h",
            color="oos_rate_pct",
            color_continuous_scale="Reds",
            labels={"oos_rate_pct": "OOS %", "name": ""},
        )
        fig_oos.update_layout(height=380, margin=dict(t=10), coloraxis_showscale=False)
        st.plotly_chart(fig_oos, use_container_width=True)

with col6:
    st.subheader("🔴 Rationed Products")
    rationed = merged[merged["is_rationed"]][["name", "brand", "price", "inventory", "max_cart_qty", "rating_score"]]
    if rationed.empty:
        st.success("No rationed products right now.")
    else:
        rationed.columns = ["Product", "Brand", "Price (₹)", "Stock", "Max Cart", "Rating"]
        st.dataframe(rationed, hide_index=True, use_container_width=True)
    st.caption("Rationed = Blinkit capped max cart qty below available inventory. Strong demand signal.")

st.divider()

# ── Full product leaderboard ──────────────────────────────────────────────────

st.subheader("Full Product Leaderboard")

leaderboard = merged[[
    "rank", "name", "brand", "size", "price", "mrp",
    "rating_score", "rating_count", "rating_delta",
    "est_units_weekly", "est_gmv_weekly",
    "in_stock", "oos_rate_pct", "is_rationed", "inventory", "max_cart_qty",
]].copy()

leaderboard = leaderboard.sort_values("rank", na_position="last")
leaderboard.columns = [
    "Rank", "Product", "Brand", "Size", "Price ₹", "MRP ₹",
    "Rating", "# Ratings", "Δ Ratings",
    "Est Units/wk", "Est GMV/wk ₹",
    "In Stock", "OOS %", "Rationed", "Inventory", "Max Cart",
]

st.dataframe(
    leaderboard,
    hide_index=True,
    use_container_width=True,
    height=500,
    column_config={
        "Rank": st.column_config.NumberColumn(width="small"),
        "Price ₹": st.column_config.NumberColumn(format="₹%.0f"),
        "MRP ₹": st.column_config.NumberColumn(format="₹%.0f"),
        "Est GMV/wk ₹": st.column_config.NumberColumn(format="₹%.0f"),
        "In Stock": st.column_config.CheckboxColumn(),
        "Rationed": st.column_config.CheckboxColumn(),
        "OOS %": st.column_config.NumberColumn(format="%.1f%%"),
    },
)

st.caption(
    "Sales estimates use rating velocity method: Δrating_count ÷ 2.5% assumed review rate. "
    "Accuracy improves with more data points over longer periods."
)
