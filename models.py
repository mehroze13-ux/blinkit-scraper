from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ProductRating(BaseModel):
    score: Optional[float] = None        # e.g. 4.3
    count: Optional[int] = None          # e.g. 1284 ratings
    review_count: Optional[int] = None


class ProductVariant(BaseModel):
    label: str
    price: Optional[float] = None
    mrp: Optional[float] = None
    in_stock: bool = True


class NutritionFacts(BaseModel):
    calories_per_100g: Optional[str] = None
    protein_per_100g: Optional[str] = None
    carbs_per_100g: Optional[str] = None
    fat_per_100g: Optional[str] = None
    saturated_fat_per_100g: Optional[str] = None
    fiber_per_100g: Optional[str] = None
    sugar_per_100g: Optional[str] = None
    sodium_per_100g: Optional[str] = None
    calcium_per_100g: Optional[str] = None
    serve_size: Optional[str] = None
    raw_nutrition_text: Optional[str] = None   # full text block as fallback


class BlinkitProduct(BaseModel):
    # ── Identity ──────────────────────────────────────────────────────────────
    product_id: Optional[str] = None
    name: str
    brand: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None

    # ── Listing signals ───────────────────────────────────────────────────────
    rank: Optional[int] = None            # position in category listing (1 = top)

    # ── Pricing ───────────────────────────────────────────────────────────────
    price: Optional[float] = None
    mrp: Optional[float] = None
    discount_pct: Optional[float] = None
    currency: str = "INR"

    # ── Size / quantity ───────────────────────────────────────────────────────
    size: Optional[str] = None

    # ── Stock & demand signals ────────────────────────────────────────────────
    in_stock: bool = True
    inventory: Optional[int] = None       # units available at this dark store
    max_cart_qty: Optional[int] = None    # per-customer cart cap (rationing signal)
    is_rationed: bool = False             # True when max_cart_qty < inventory

    # ── Details ───────────────────────────────────────────────────────────────
    description: Optional[str] = None
    about: Optional[str] = None
    ingredients: Optional[str] = None
    nutrition: Optional[NutritionFacts] = None
    nutritional_info: Optional[str] = None   # raw text fallback
    allergen_info: Optional[str] = None
    storage_instructions: Optional[str] = None
    country_of_origin: Optional[str] = None
    manufacturer: Optional[str] = None
    fssai_license: Optional[str] = None
    diet_preference: Optional[str] = None   # e.g. "Gluten-Free", "Vegan"
    flavour: Optional[str] = None
    product_type: Optional[str] = None      # e.g. "Roasted Snacks"
    shelf_life: Optional[str] = None
    key_features: Optional[str] = None

    # ── Ratings ───────────────────────────────────────────────────────────────
    rating: Optional[ProductRating] = None

    # ── Variants ─────────────────────────────────────────────────────────────
    variants: list[ProductVariant] = Field(default_factory=list)

    # ── Media & URLs ─────────────────────────────────────────────────────────
    image_urls: list[str] = Field(default_factory=list)
    product_url: Optional[str] = None

    # ── Meta ─────────────────────────────────────────────────────────────────
    scraped_at: Optional[str] = None
    location_pincode: Optional[str] = None
