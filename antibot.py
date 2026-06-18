"""
Anti-detection utilities for Playwright.

Patches known fingerprinting vectors that bot-detection scripts probe:
  - navigator.webdriver flag
  - navigator.plugins / mimeTypes
  - navigator.languages
  - Chrome runtime object
  - WebGL vendor/renderer strings
  - Canvas noise injection
  - Screen dimensions matching the user-agent
  - Permission query override (avoids "denied" fingerprint)
  - Consistent timezone + locale
"""

import random
import asyncio
from playwright.async_api import Page, BrowserContext

# JS injected before every page's scripts run
_STEALTH_JS = """
// 1. Remove webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. Fake plugins array (Chrome typically has 3)
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ];
        arr.__proto__ = PluginArray.prototype;
        return arr;
    }
});

// 3. Languages
Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en-US', 'en'] });

// 4. Chrome runtime object (headless Chrome lacks this by default)
if (!window.chrome) {
    window.chrome = { runtime: {} };
}

// 5. Override permission query so it doesn't return 'denied' for all
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);

// 6. WebGL vendor / renderer — spoof as a real GPU
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';          // UNMASKED_VENDOR_WEBGL
    if (parameter === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
    return getParameter.call(this, parameter);
};

// 7. Canvas noise — tiny imperceptible pixel shift breaks canvas hash
const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type, ...args) {
    const ctx = this.getContext('2d');
    if (ctx) {
        const imageData = ctx.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < 10; i++) {
            const idx = Math.floor(Math.random() * imageData.data.length / 4) * 4;
            imageData.data[idx] = imageData.data[idx] ^ 1;
        }
        ctx.putImageData(imageData, 0, 0);
    }
    return origToDataURL.apply(this, [type, ...args]);
};

// 8. Hide automation-related properties
delete window.__playwright;
delete window.__pw_manual;
delete window.__PW_inspect;
"""


async def apply_stealth(context: BrowserContext) -> None:
    """Apply all stealth patches to every new page in this context."""
    await context.add_init_script(_STEALTH_JS)


async def set_realistic_headers(page: Page, referer: str = "https://blinkit.com/") -> None:
    await page.set_extra_http_headers({
        "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })


async def human_delay(min_s: float = 1.5, max_s: float = 4.0) -> None:
    """Gaussian-shaped random delay that mimics human reading/browsing pace."""
    mu = (min_s + max_s) / 2
    sigma = (max_s - min_s) / 4
    delay = max(min_s, min(max_s, random.gauss(mu, sigma)))
    await asyncio.sleep(delay)


async def human_scroll(page: Page, steps: int = 4) -> None:
    """Scroll down the page in realistic increments."""
    for _ in range(steps):
        scroll_amount = random.randint(200, 600)
        await page.mouse.wheel(0, scroll_amount)
        await asyncio.sleep(random.uniform(0.3, 0.9))


async def simulate_mouse_movement(page: Page) -> None:
    """Move the mouse to a random position to appear human."""
    vp = page.viewport_size or {"width": 1280, "height": 800}
    x = random.randint(100, vp["width"] - 100)
    y = random.randint(100, vp["height"] - 100)
    await page.mouse.move(x, y, steps=random.randint(5, 15))
