"""
Blinkit product scraper.

Two authoritative techniques discovered by inspecting the live app:

LISTING PAGES  — Blinkit renders a virtualised product list inside #plpContainer.
                 Product cards are  div[role="button"][id]  where id = product_id.
                 We scroll the inner container (not window) and harvest card ids
                 before the virtual-scroll removes them from the DOM.

PRODUCT PAGES  — All product data is available in
                 window.grofers.PRELOADED_STATE.ui.pdp.bffPdp.bffData
                 before any network-interception tricks are needed.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

import config
from antibot import apply_stealth, set_realistic_headers, human_delay, simulate_mouse_movement
from models import BlinkitProduct, NutritionFacts, ProductRating, ProductVariant


BLINKIT_BASE = "https://blinkit.com"


# ── Scraper class ─────────────────────────────────────────────────────────────

class BlinkitScraper:
    def __init__(
        self,
        lat: float = config.DEFAULT_LAT,
        lng: float = config.DEFAULT_LNG,
        pincode: str = config.DEFAULT_PINCODE,
        headless: bool = config.HEADLESS,
        proxy_url: str = config.PROXY_URL,
    ) -> None:
        self.lat = lat
        self.lng = lng
        self.pincode = pincode
        self.headless = headless
        self.proxy_url = proxy_url

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._location_set = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        launch_kwargs: dict = {
            "headless": self.headless,
            "args": [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--lang=en-IN",
                "--window-size=900,1600",  # narrow viewport — shows more tabs on Blinkit
            ],
        }
        if self.proxy_url:
            launch_kwargs["proxy"] = {"server": self.proxy_url}

        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        ua = random.choice(config.USER_AGENTS)
        self._context = await self._browser.new_context(
            user_agent=ua,
            viewport={"width": 900, "height": 1600},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            geolocation={"latitude": self.lat, "longitude": self.lng},
            permissions=["geolocation"],
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        await apply_stealth(self._context)

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _new_page(self) -> Page:
        assert self._context, "Call start() first"
        page = await self._context.new_page()
        await set_realistic_headers(page)
        page.set_default_timeout(config.BROWSER_TIMEOUT_MS)
        return page

    async def _set_location(self, page: Page) -> None:
        if self._location_set:
            return
        await page.goto(BLINKIT_BASE, wait_until="load")
        await human_delay(2, 4)
        await page.evaluate("""([lat, lng, pincode]) => {
            const loc = { lat, lng, address: pincode };
            try { localStorage.setItem('gr_1', JSON.stringify(loc)); } catch(e){}
            try { localStorage.setItem('userLocation', JSON.stringify(loc)); } catch(e){}
            try { localStorage.setItem('bl_location', JSON.stringify(loc)); } catch(e){}
            const v = encodeURIComponent(JSON.stringify(loc));
            document.cookie = `gr_1=${v}; path=/; domain=.blinkit.com`;
        }""", [self.lat, self.lng, self.pincode])
        self._location_set = True

    async def _is_blocked(self, page: Page) -> bool:
        title = (await page.title()).lower()
        try:
            body_text = await page.evaluate("() => document.body?.innerText?.slice(0, 2000) || ''")
        except Exception:
            body_text = ""
        for signal in ("captcha", "blocked", "access denied", "unusual traffic", "verify you are human"):
            if signal in title or signal in body_text.lower():
                return True
        return False

    # ── Listing page — collect product links ──────────────────────────────────

    async def collect_all_links_from_page(self, page: Page, max_results: int) -> list[tuple[str, int]]:
        """
        Scroll #plpContainer and harvest every product card's id + rank position.
        Returns list of (url, rank) tuples in listing order.
        Includes OOS products — never filters by stock status.
        """
        from rich.console import Console
        _con = Console()

        # ordered dict preserves insertion order = rank order
        cards: dict[str, str] = {}   # {product_id: outer_html}

        try:
            await page.wait_for_selector("#plpContainer", timeout=15000)
        except Exception:
            _con.print("  [yellow]#plpContainer not found — falling back to <a> tag scan[/yellow]")
            links = await self._dom_product_links(page, max_results)
            return [(url, i + 1) for i, url in enumerate(links)]

        stagnation = 0
        bottom_plateau = 0

        for _ in range(60):
            new_cards = await self._harvest_cards(page)
            before = len(cards)
            # Only add new ids — preserve rank order of first appearance
            for pid, html in new_cards.items():
                if pid not in cards:
                    cards[pid] = html

            _con.print(f"  [dim]Found {len(cards)} product card(s) so far…[/dim]", end="\r")

            if len(cards) >= max_results:
                break

            await self._scroll_plp_container(page)
            await asyncio.sleep(1.5)

            if len(cards) == before:
                stagnation += 1
            else:
                stagnation = 0

            near_bottom = await self._near_bottom(page)
            bottom_plateau = (bottom_plateau + 1) if near_bottom else 0

            if stagnation >= 8 and bottom_plateau >= 2:
                break

        # Scroll to top — virtualised items re-render, pick up any missed
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1)
        for pid, html in (await self._harvest_cards(page)).items():
            if pid not in cards:
                cards[pid] = html

        _con.print("")
        return self._cards_to_ranked_urls(cards, max_results)

    async def _harvest_cards(self, page: Page) -> dict[str, str]:
        """Return {product_id: outer_html} for currently visible product cards."""
        try:
            payload = await page.evaluate("""() => {
                const container = document.querySelector('#plpContainer');
                if (!container) return [];
                return Array.from(
                    container.querySelectorAll('div[role="button"][id]')
                ).map(node => [node.id, node.outerHTML]);
            }""")
            return {card_id: html for card_id, html in (payload or []) if card_id}
        except Exception:
            return {}

    async def _scroll_plp_container(self, page: Page) -> None:
        await page.evaluate("""() => {
            const container = document.querySelector('#plpContainer');
            if (container) {
                container.scrollBy(0, container.clientHeight * 0.85);
            }
            window.scrollBy(0, window.innerHeight || 800);
        }""")

    async def _near_bottom(self, page: Page) -> bool:
        try:
            return bool(await page.evaluate("""() => {
                const c = document.querySelector('#plpContainer');
                if (!c) return true;
                return c.scrollTop + c.clientHeight >= c.scrollHeight - 24;
            }"""))
        except Exception:
            return False

    def _cards_to_ranked_urls(self, cards: dict[str, str], limit: int) -> list[tuple[str, int]]:
        """Build (url, rank) tuples from harvested card ids in listing order."""
        result: list[tuple[str, int]] = []
        for rank, (product_id, html) in enumerate(list(cards.items())[:limit], start=1):
            m = (re.search(r'aria-label="([^"]{4,})"', html)
                 or re.search(r'alt="([^"]{4,})"', html)
                 or re.search(r'title="([^"]{4,})"', html))
            name = m.group(1) if m else "product"
            if name.lower() in ("product", "image", "img", "blinkit"):
                name = "product"
            slug = _slugify(name)
            result.append((f"{BLINKIT_BASE}/prn/{slug}/prid/{product_id}", rank))
        return result

    async def _dom_product_links(self, page: Page, limit: int) -> list[str]:
        """Fallback: collect <a> tags with prid in href."""
        links: list[str] = []
        seen: set[str] = set()
        try:
            anchors = await page.query_selector_all("a")
            for anchor in anchors:
                href = await anchor.get_attribute("href") or ""
                full = href if href.startswith("http") else urljoin(BLINKIT_BASE, href)
                if "prid" in full and full not in seen:
                    seen.add(full)
                    links.append(full)
                if len(links) >= limit:
                    break
        except Exception:
            pass
        return links

    # ── Product page — full detail extraction ─────────────────────────────────

    async def scrape_product(self, url: str, rank: Optional[int] = None) -> Optional[BlinkitProduct]:
        pid = _id_from_url(url)
        page = await self._new_page()
        try:
            await self._set_location(page)
            await human_delay(0.5, 1.2)
            await page.goto(url, wait_until="load", timeout=config.BROWSER_TIMEOUT_MS)
            await simulate_mouse_movement(page)

            bff_data = await self._wait_for_preloaded_state(page)

            if await self._is_blocked(page):
                raise RuntimeError("Bot detection triggered")

            if bff_data:
                product = _parse_bff_data(bff_data, url, self.pincode)
                product.rank = rank
                return product

            product = await self._dom_product(page, url, pid)
            product.rank = rank
            return product

        finally:
            await page.close()

    async def _wait_for_preloaded_state(self, page: Page, timeout_ms: int = 15000) -> Optional[dict]:
        """
        Poll for window.grofers.PRELOADED_STATE and return the bffData sub-tree.
        Returns None if not available within timeout.
        """
        try:
            await page.wait_for_function(
                """() => {
                    try {
                        return !!(
                            window.grofers &&
                            window.grofers.PRELOADED_STATE &&
                            window.grofers.PRELOADED_STATE.ui &&
                            window.grofers.PRELOADED_STATE.ui.pdp &&
                            window.grofers.PRELOADED_STATE.ui.pdp.bffPdp &&
                            window.grofers.PRELOADED_STATE.ui.pdp.bffPdp.bffData &&
                            window.grofers.PRELOADED_STATE.ui.pdp.bffPdp.bffData.snippets
                        );
                    } catch(e) { return false; }
                }""",
                timeout=timeout_ms,
            )
            return await page.evaluate(
                "() => window.grofers.PRELOADED_STATE.ui.pdp.bffPdp.bffData"
            )
        except Exception:
            return None

    async def _dom_product(self, page: Page, url: str, pid: Optional[str]) -> BlinkitProduct:
        """Last-resort DOM scrape when PRELOADED_STATE is unavailable."""

        async def text(*selectors: str) -> Optional[str]:
            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        t = (await el.inner_text()).strip()
                        if t:
                            return t
                except Exception:
                    pass
            return None

        name = await text("h1", "[class*='product-name']", "[class*='ProductName']") or "Unknown"
        price_str = await text("[class*='selling-price']", "[class*='SellingPrice']", "[class*='sp']")
        mrp_str   = await text("[class*='mrp']", "[class*='strikethrough']", "s", "del")
        price = _to_float(_clean_price(price_str))
        mrp   = _to_float(_clean_price(mrp_str))
        discount_pct: Optional[float] = None
        if price and mrp and mrp > price:
            discount_pct = round((mrp - price) / mrp * 100, 1)

        return BlinkitProduct(
            product_id=pid,
            name=name,
            price=price,
            mrp=mrp,
            discount_pct=discount_pct,
            product_url=url,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            location_pincode=self.pincode,
        )

    # ── Batch helpers ─────────────────────────────────────────────────────────

    async def scrape_category(
        self,
        category_url: str,
        max_results: int = 100,
        _progress=None,
        _task_id=None,
    ) -> list[BlinkitProduct]:
        page = await self._new_page()
        try:
            await self._set_location(page)
            await human_delay()
            await page.goto(category_url, wait_until="load", timeout=config.BROWSER_TIMEOUT_MS)
            await simulate_mouse_movement(page)
            await asyncio.sleep(3)
            if await self._is_blocked(page):
                raise RuntimeError("Bot detection triggered on category page")
            ranked_urls = await self.collect_all_links_from_page(page, max_results)
        finally:
            await page.close()

        return await self._scrape_url_list(ranked_urls, _progress, _task_id)

    async def scrape_search(
        self,
        query: str,
        max_results: int = 100,
        _progress=None,
        _task_id=None,
    ) -> list[BlinkitProduct]:
        search_url = f"{BLINKIT_BASE}/s/?q={query.replace(' ', '+')}"
        page = await self._new_page()
        try:
            await self._set_location(page)
            await human_delay()
            await page.goto(search_url, wait_until="load", timeout=config.BROWSER_TIMEOUT_MS)
            await simulate_mouse_movement(page)
            await asyncio.sleep(3)
            if await self._is_blocked(page):
                raise RuntimeError("Bot detection triggered on search page")
            ranked_urls = await self.collect_all_links_from_page(page, max_results)
        finally:
            await page.close()

        return await self._scrape_url_list(ranked_urls, _progress, _task_id)

    async def _scrape_url_list(
        self,
        ranked_urls: list[tuple[str, int]],
        _progress=None,
        _task_id=None,
    ) -> list[BlinkitProduct]:
        products: list[BlinkitProduct] = []
        for url, rank in ranked_urls:
            try:
                p = await self.scrape_product(url, rank=rank)
                if p:
                    products.append(p)
                if _progress and _task_id is not None:
                    _progress.advance(_task_id)
                await human_delay(config.MIN_DELAY_S, config.MAX_DELAY_S)
            except Exception as e:
                print(f"[WARN] {url}: {e}")
        return products


# ── BFF data parser ───────────────────────────────────────────────────────────

def _parse_bff_data(bff: dict, url: str, pincode: str) -> BlinkitProduct:
    """
    Parse window.grofers.PRELOADED_STATE.ui.pdp.bffPdp.bffData into a BlinkitProduct.

    Main snippets  → name, price, size, brand, inventory, max_cart_qty, rating, images, category
    expand_attributes → ingredients, nutrition, FSSAI, allergens, shelf life, etc.
    """
    snippets: list[dict] = bff.get("snippets") or []
    expand_snippets: list[dict] = (
        bff.get("snippet_list_updater_data", {})
        .get("expand_attributes", {})
        .get("payload", {})
        .get("snippets_to_add") or []
    )

    # ── Pass 1: main snippets ─────────────────────────────────────────────────
    product_name: Optional[str] = None
    product_id: Optional[str] = None
    price: Optional[float] = None
    mrp: Optional[float] = None
    size: Optional[str] = None
    brand: Optional[str] = None
    in_stock: bool = True
    inventory: Optional[int] = None
    max_cart_qty: Optional[int] = None
    images: list[str] = []
    category: Optional[str] = None
    sub_category: Optional[str] = None
    rating_score: Optional[float] = None
    rating_count: Optional[int] = None
    product_type: Optional[str] = None

    for snippet in snippets:
        wtype = snippet.get("widget_type", "")
        data  = snippet.get("data") or {}

        if wtype == "text_right_icons_rating_snippet_type":
            product_name = _snip_text(data, "title") or product_name
            product_id   = str(data.get("identity", {}).get("id", "")) or product_id
            # Rating widget — score and count may be nested here
            for rk in ("rating", "ratings", "rating_data"):
                rating_raw = data.get(rk) or {}
                if rating_raw:
                    rating_score = _to_float(
                        rating_raw.get("average") or rating_raw.get("score") or
                        rating_raw.get("value")
                    ) or rating_score
                    rating_count = _to_int(
                        rating_raw.get("count") or rating_raw.get("total") or
                        rating_raw.get("rating_count")
                    ) or rating_count

        elif wtype == "ratings_and_reviews_snippet" or "rating" in wtype.lower():
            # Dedicated rating widget
            rating_score = _to_float(
                data.get("average_rating") or data.get("rating") or
                _snip_text(data, "rating")
            ) or rating_score
            rating_count = _to_int(
                data.get("rating_count") or data.get("count") or
                _snip_text(data, "count")
            ) or rating_count

        elif wtype == "product_atc_strip":
            product_id = str(data.get("identity", {}).get("id", "")) or product_id
            price_text = _snip_text(data, "normal_price") or ""
            price      = _to_float(_clean_price(price_text)) or price
            size       = _snip_text(data, "variant") or size
            in_stock   = not data.get("is_sold_out", False)
            inventory  = _to_int(data.get("inventory")) if data.get("inventory") is not None else inventory

            # max_cart_qty from stepper_data_v2.max_count
            stepper = data.get("stepper_data_v2") or {}
            max_cart_qty = _to_int(stepper.get("max_count")) or max_cart_qty

            # cart_item has definitive price, mrp, brand, image
            cart_item = (
                ((data.get("rfc_actions_v2") or {}).get("default") or [{}])[0] or {}
            ).get("remove_from_cart", {}).get("cart_item") or {}

            # Also check increment_actions for cart_item
            if not cart_item:
                cart_item = (
                    ((stepper.get("increment_actions") or {}).get("default") or [{}])[0] or {}
                ).get("add_to_cart", {}).get("cart_item") or {}

            if cart_item:
                price = _to_float(cart_item.get("price")) or price
                mrp   = _to_float(cart_item.get("mrp"))   or mrp
                brand = cart_item.get("brand")             or brand
                img_u = cart_item.get("image_url", "")
                if img_u and img_u not in images:
                    images.insert(0, img_u)
                inventory = _to_int(cart_item.get("inventory")) or inventory

        elif wtype == "horizontal_text_list_snippet":
            crumbs = [
                _snip_text(item, "title")
                for item in (data.get("horizontal_item_list") or [])
            ]
            crumbs = [c for c in crumbs if c and c not in ("›", ">", "/")]
            if len(crumbs) >= 2:
                category, sub_category = crumbs[0], crumbs[-1]
            elif crumbs:
                category = crumbs[0]

        elif wtype == "carousal_list_vr":
            for item in (data.get("itemList") or []):
                item_data = item.get("data") or {}
                mc = item_data.get("media_content") or {}
                if mc.get("media_type") == "image":
                    u = (mc.get("image") or {}).get("url", "")
                    if u and u not in images:
                        images.append(u)
                for asset in ((item_data.get("click_action") or {})
                              .get("show_gallery", {}).get("assets") or []):
                    u = asset.get("image_url", "")
                    if u and u not in images:
                        images.append(u)

        elif wtype == "b_image_text_snippet_type_3":
            title_txt = _snip_text(data, "title") or ""
            val_txt   = _snip_text(data, "subtitle") or ""
            if title_txt.lower() == "type":
                product_type = val_txt

    # ── Pass 2: expanded detail snippets (all key:value pairs) ───────────────
    detail_map: dict[str, str] = {}
    for snippet in expand_snippets:
        data = snippet.get("data") or {}
        title_txt = _snip_text(data, "title") or ""
        val_txt   = (_snip_text(data, "subtitle") or _snip_text(data, "description") or "")
        if title_txt and val_txt:
            detail_map[title_txt.lower().strip()] = val_txt.strip()

    def _d(*keys: str) -> Optional[str]:
        for key in keys:
            for k, v in detail_map.items():
                if key in k:
                    return v
        return None

    ingredients       = _d("ingredient")
    nutritional_info  = _d("nutrition information", "nutrition info")
    allergen_info     = _d("allergen")
    shelf_life        = _d("shelf life")
    storage           = _d("storage")
    country_of_origin = _d("country of origin")
    manufacturer      = _d("manufacturer")
    fssai             = _d("fssai license", "fssai")
    about             = _d("key features", "description", "about")
    diet_preference   = _d("diet preference")
    flavour           = _d("flavour")
    key_features      = _d("key features")

    # ── Structured nutrition ──────────────────────────────────────────────────
    def _nutr(key: str) -> Optional[str]:
        for k, v in detail_map.items():
            if key in k:
                return v
        return None

    nutrition = NutritionFacts(
        calories_per_100g   = _nutr("calorie") or _nutr("energy"),
        protein_per_100g    = _nutr("protein"),
        carbs_per_100g      = _nutr("carbohydrate") or _nutr("carb"),
        fat_per_100g        = _nutr("total fat"),
        saturated_fat_per_100g = _nutr("saturated fat"),
        fiber_per_100g      = _nutr("dietary fiber") or _nutr("fibre"),
        sugar_per_100g      = _nutr("total sugar") or _nutr("sugar"),
        sodium_per_100g     = _nutr("sodium"),
        calcium_per_100g    = _nutr("calcium"),
        serve_size          = _nutr("serve size"),
        raw_nutrition_text  = nutritional_info,
    )
    has_nutrition = any(v for v in nutrition.model_dump().values() if v)
    if not has_nutrition:
        nutrition = None  # type: ignore[assignment]

    # ── Derived fields ────────────────────────────────────────────────────────
    discount_pct: Optional[float] = None
    if price and mrp and mrp > price:
        discount_pct = round((mrp - price) / mrp * 100, 1)

    is_rationed = bool(
        inventory is not None and max_cart_qty is not None and max_cart_qty < inventory
    )

    rating: Optional[ProductRating] = None
    if rating_score is not None or rating_count is not None:
        rating = ProductRating(score=rating_score, count=rating_count)

    return BlinkitProduct(
        product_id        = product_id or _id_from_url(url),
        name              = product_name or "Unknown",
        brand             = brand,
        category          = category,
        sub_category      = sub_category or product_type,
        price             = price,
        mrp               = mrp,
        discount_pct      = discount_pct,
        size              = size,
        in_stock          = in_stock,
        inventory         = inventory,
        max_cart_qty      = max_cart_qty,
        is_rationed       = is_rationed,
        description       = about,
        about             = about,
        ingredients       = ingredients,
        nutrition         = nutrition,
        nutritional_info  = nutritional_info,
        allergen_info     = allergen_info,
        storage_instructions = shelf_life or storage,
        shelf_life        = shelf_life,
        country_of_origin = country_of_origin,
        manufacturer      = manufacturer,
        fssai_license     = fssai,
        diet_preference   = diet_preference,
        flavour           = flavour,
        product_type      = product_type,
        key_features      = key_features,
        rating            = rating,
        image_urls        = images,
        product_url       = url,
        scraped_at        = datetime.now(timezone.utc).isoformat(),
        location_pincode  = pincode,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _snip_text(data: dict, key: str) -> Optional[str]:
    """Extract .text from a snippet's title/subtitle/variant field."""
    v = data.get(key)
    if isinstance(v, dict):
        return (v.get("text") or "").strip() or None
    if isinstance(v, str):
        return v.strip() or None
    return None


def _id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/prid/(\d+)", url)
    return m.group(1) if m else None


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"['']", "", text)
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _clean_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return re.sub(r"[^\d.]", "", text.replace(",", ""))


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    s = re.sub(r"[^\d]", "", str(v))
    return int(s) if s else None
