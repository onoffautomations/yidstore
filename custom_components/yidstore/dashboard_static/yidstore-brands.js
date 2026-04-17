/**
 * YidStore Brands Patcher
 * - Overrides Home Assistant brand icon/logo fetches to prefer local overrides:
 *     /config/custom_components/<domain>/brand/...
 *     /config/custom_components/<domain>/...
 * - Falls back to official brands site if no local file exists.
 */
(function() {
  'use strict';

  const BRANDS_LIST_API = '/api/yidstore/brands';
  let localBrands = {}; // domain -> { filename -> local url }
  let patchApplied = false;

  async function loadLocalBrands() {
    try {
      const res = await fetch(BRANDS_LIST_API, { cache: 'no-store' });
      if (res.ok) {
        localBrands = await res.json();
        console.debug('[YidStore] Local brands loaded:', Object.keys(localBrands).length);
      }
    } catch (e) {
      console.debug('[YidStore] Failed to load local brands list:', e);
    }
  }

  function matchBrandUrl(u) {
    if (typeof u !== 'string') return null;
    if (!u.includes('brands.home-assistant.io')) return null;

    // Supports:
    //  - https://brands.home-assistant.io/<domain>/icon.png
    //  - https://brands.home-assistant.io/_/<domain>/icon.png
    //  - .../logo.png, dark_icon.png, dark_logo.png, @2x and svg
    const m = u.match(/brands\.home-assistant\.io\/(?:_\/)?([^/]+)\/([^/?#]+)$/);
    if (!m) return null;
    return { domain: decodeURIComponent(m[1] || ''), filename: (m[2] || '').split('?')[0] };
  }

  function getLocalUrl(domain, filename) {
    const d = (domain || '').toLowerCase();
    const files = localBrands[d];
    if (!files) return null;
    if (files[filename]) return files[filename];

    // Fallback order: if a specific file isn't present, fall back between dark/light, and logo->icon.
    const alt = {
      'dark_icon.png': 'icon.png',
      'icon.png': 'dark_icon.png',
      'dark_logo.png': 'logo.png',
      'logo.png': 'dark_logo.png',
      'dark_icon@2x.png': 'icon@2x.png',
      'icon@2x.png': 'dark_icon@2x.png',
      'dark_logo@2x.png': 'logo@2x.png',
      'logo@2x.png': 'dark_logo@2x.png',
    };
    const a = alt[filename];
    if (a && files[a]) return files[a];

    // If logo missing, try icon
    if (filename.startsWith('logo') && files['icon.png']) return files['icon.png'];
    if (filename.startsWith('dark_logo') && files['dark_icon.png']) return files['dark_icon.png'];

    return null;
  }

  function patch() {
    if (patchApplied) return;

    // Patch fetch
    const origFetch = window.fetch;
    window.fetch = async function(url, options) {
      const hit = matchBrandUrl(url);
      if (hit) {
        const local = getLocalUrl(hit.domain, hit.filename);
        if (local) return origFetch.call(this, local, options);
      }
      return origFetch.call(this, url, options);
    };

    // Patch <img src=...>
    const desc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
    Object.defineProperty(HTMLImageElement.prototype, 'src', {
      get() { return desc.get.call(this); },
      set(v) {
        const hit = matchBrandUrl(v);
        if (hit) {
          const local = getLocalUrl(hit.domain, hit.filename);
          if (local) return desc.set.call(this, local);
        }
        return desc.set.call(this, v);
      }
    });

    // Catch dynamically inserted images
    const obs = new MutationObserver((mutations) => {
      for (const m of mutations) {
        for (const node of m.addedNodes || []) {
          if (!node || node.nodeType !== 1) continue;
          const imgs = node.tagName === 'IMG' ? [node] : (node.querySelectorAll ? node.querySelectorAll('img') : []);
          imgs.forEach(img => {
            const src = img.getAttribute('src') || '';
            const hit = matchBrandUrl(src);
            if (!hit) return;
            const local = getLocalUrl(hit.domain, hit.filename);
            if (local) img.src = local;
          });
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });

    patchApplied = true;
    console.debug('[YidStore] Brands patcher active');
  }

  async function init() {
    await loadLocalBrands();
    patch();
    setInterval(loadLocalBrands, 60000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(); 
