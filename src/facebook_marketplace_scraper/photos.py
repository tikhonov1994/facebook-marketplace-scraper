"""Collect listing gallery URLs without downloading or deduplicating images."""
import logging

logger = logging.getLogger(__name__)
GALLERY_SCRIPT = r"""() => {
  const urls = new Set();
  let galleryFound = false;
  const add = value => {
    if (Array.isArray(value)) return value.forEach(add);
    if (value && typeof value === 'object') return add(value.contentUrl || value.url);
    if (typeof value === 'string') {
      try { const u = new URL(value, location.href);
        if (u.protocol === 'https:' || u.protocol === 'http:') urls.add(u.href);
      } catch (_) {}
    }
  };
  const heading = document.querySelector('h1')?.textContent.trim();
  const listingId = location.pathname.match(/marketplace\/item\/(\d+)/)?.[1];
  const visit = value => {
    if (Array.isArray(value)) return value.forEach(visit);
    if (!value || typeof value !== 'object') return;
    const types = [].concat(value['@type'] || []);
    if (types.some(t => ['Product', 'Apartment', 'House', 'Residence',
        'SingleFamilyResidence', 'RealEstateListing'].includes(t)) && value.image
        && (value.name === heading || value.url === location.href)
        && Array.isArray(value.image)) {
      galleryFound = true; add(value.image);
    }
    // Property-component data is scoped by the current listing title.
    if (heading && value.title === heading && Array.isArray(value.images)) {
      galleryFound = true; add(value.images);
    }
    // Facebook's embedded listing object must match the URL, not a recommendation.
    if (listingId && String(value.id) === listingId) {
      const photos = value.listing_photos || value.all_listing_photos;
      if (Array.isArray(photos)) {
        galleryFound = true;
        photos.forEach(p => add(p.image?.uri || p.image?.url || p.uri));
      }
    }
    for (const child of Object.values(value)) {
      if (child && typeof child === 'object') visit(child);
    }
  };
  for (const script of document.querySelectorAll('script[type="application/ld+json"], script[type="application/json"]')) {
    try { visit(JSON.parse(script.textContent)); } catch (_) {}
  }
  // Next.js Flight chunks contain serialized props, including the complete gallery.
  for (const chunk of (window.__next_f || [])) {
    if (typeof chunk[1] !== 'string') continue;
    for (const line of chunk[1].split('\n')) {
      const colon = line.indexOf(':');
      if (colon < 0) continue;
      try { visit(JSON.parse(line.slice(colon + 1))); } catch (_) {}
    }
  }
  const selectors = '[class*="property-gallery"], [id*="property-gallery"], ' +
    '[class*="listing-gallery"], [class*="photo-gallery"], [class*="image-gallery"], ' +
    '[class*="property-slider"]';
  for (const gallery of document.querySelectorAll(selectors)) {
    galleryFound = true;
    for (const img of gallery.querySelectorAll('img')) {
      add(img.dataset.original || img.dataset.src || img.currentSrc || img.src);
    }
    for (const a of gallery.querySelectorAll('a[href]')) {
      if (/\.(jpe?g|png|webp)(\?|$)/i.test(a.href)) add(a.href);
    }
  }
  return {urls: [...urls], complete: galleryFound && urls.size > 0};
}"""


async def browser_photos(page) -> dict:
    try:
        gallery = await page.evaluate(GALLERY_SCRIPT)
        return {"photo_urls": gallery["urls"], "photos_complete": gallery["complete"]}
    except Exception:
        logger.warning("Could not inspect listing gallery", exc_info=True)
        return {"photo_urls": [], "photos_complete": False}
