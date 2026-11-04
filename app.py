"""Prelive-Vorschau – Builder
Domain eingeben -> Absender, Betreff und Preview-Text werden domain-spezifisch vorbefüllt, Avatare und das
Logo (5,5:1) werden auf der Website gesucht und in template.png eingesetzt. Manueller Upload (PNG, JPG, WEBP, SVG)
ist jederzeit möglich. Export: alle drei Mockups in EINEM PNG.

Benötigt: streamlit, pillow, requests, beautifulsoup4, cairosvg (SVG-Upload) – siehe requirements.txt / packages.txt.
template.png muss im selben Ordner wie diese Datei liegen.
"""
import base64, colorsys, html, io, ipaddress, json, math, os, re, socket, time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps
import streamlit as st

try:                                    # SVG -> PNG (braucht zusätzlich die Systembibliothek libcairo2)
    import cairosvg
except Exception:                       # ImportError oder OSError (libcairo fehlt)
    cairosvg = None

st.set_page_config(page_title='Prelive-Vorschau', page_icon='🖼️', layout='wide')

# ----------------------------------------------------------------------------
# Konstanten
# ----------------------------------------------------------------------------
AVATAR_DEFAULT = '#B8DDFD'     # Fallback-Avatar (Initialen)
AVATAR_BG_DEFAULT = '#FFFFFF'  # Hintergrund hinter Logo-Avataren (transparente SVG/PNG)
FETCH_CONNECT_TIMEOUT = 8
FETCH_READ_TIMEOUT = 15
PAGE_FETCH_BUDGET = 15         # Gesamtzeit für den Abruf der Startseite
IMAGE_FETCH_BUDGET = 10        # Gesamtzeit je Bild / Logo / Manifest
AVATAR_LIMIT = 8               # so viele Avatare stehen maximal zur Auswahl
LOGO_RATIO = 5.5               # Logo-Format 5,5 : 1
LOGO_EXPORT = (1100, 200)      # Export-Größe des 5,5:1-Logos
LOGO_FIT_MIN = 2.2             # ab diesem Seitenverhältnis (nach Zuschnitt) gilt ein Logo als „passend“ für 5,5:1
ALLOWED_TYPES = ['svg', 'png', 'jpg', 'jpeg', 'webp']
RENDER_SIDE = 1024             # SVGs/Bilder werden auf max. diese Kantenlänge gerendert
SUPERSAMPLE = 8                # Einsetzen der Grafiken in 8-facher Auflösung, dann herunterskalieren (glatte Kanten)

# ----------------------------------------------------------------------------
# Platzhalter in template.png (exakt vermessen; bbox = x0, y0, x1, y1 inklusive)
# ----------------------------------------------------------------------------
SLOTS = {
    'avatar_links': {'kind': 'circle', 'bbox': (83, 301, 127, 346), 'label': 'Mockup 1 – Avatar (GMX / WEB.DE / 1&1)'},
    'logo_mitte': {'kind': 'rect', 'bbox': (665, 290, 786, 311), 'label': 'Mockup 2 – Logo 5,5:1 (Telekom Mail / Freenet Mail)'},
    'avatar_rechts': {'kind': 'circle', 'bbox': (1219, 333, 1264, 377), 'label': 'Mockup 3 – Avatar (Darkmode / GMX Android)'},
}

# ----------------------------------------------------------------------------
# Textfelder in template.png (exakt vermessen)
#   slot     : Mockup 'links' (GMX/WEB.DE/1&1), 'mitte' (Telekom/Freenet Mail), 'rechts' (Darkmode/GMX Android)
#   field    : 'sender' | 'subject' | 'preheader'
#   x        : linke Textkante, baseline: Grundlinie, max_x: rechte Grenze (danach wird mit „…“ gekürzt)
#   clear    : Bereich mit dem Platzhaltertext, wird mit `bg` überdeckt
#   badge    : (optional) Verifiziert-Haken aus dem Template; wandert hinter den Absendernamen
# ----------------------------------------------------------------------------
TEXT_SLOTS = [
    # Mockup 1 – Avatar-Darstellung (GMX, WEB.DE, 1&1)
    {'slot': 'links', 'field': 'sender', 'x': 141, 'baseline': 327, 'max_x': 396, 'clear': (139, 310, 300, 331),
     'bg': '#FFFFFF', 'size': 17, 'bold': True, 'color': '#000000', 'badge': (249, 312, 267, 330), 'badge_gap': 5},
    {'slot': 'links', 'field': 'subject', 'x': 142, 'baseline': 354, 'max_x': 442, 'clear': (139, 337, 447, 357),
     'bg': '#FFFFFF', 'size': 18, 'bold': False, 'color': '#000000'},
    {'slot': 'links', 'field': 'preheader', 'x': 142, 'baseline': 382, 'max_x': 440, 'clear': (139, 365, 447, 386),
     'bg': '#FFFFFF', 'size': 17, 'bold': False, 'color': '#A6A6A6'},
    # Mockup 2 – Logo-Darstellung (Telekom Mail, Freenet Mail); der Absender ist hier das Logo
    {'slot': 'mitte', 'field': 'subject', 'x': 643, 'baseline': 335, 'max_x': 975, 'clear': (640, 318, 985, 339),
     'bg': '#FFFFFF', 'size': 15, 'bold': True, 'color': '#000000'},
    {'slot': 'mitte', 'field': 'preheader', 'x': 643, 'baseline': 361, 'max_x': 1024, 'clear': (640, 344, 1028, 366),
     'bg': '#FFFFFF', 'size': 16, 'bold': False, 'color': '#222222'},
    # Mockup 3 – Darkmode-Darstellung (GMX Android)
    {'slot': 'rechts', 'field': 'sender', 'x': 1281, 'baseline': 347, 'max_x': 1550, 'clear': (1278, 329, 1420, 350),
     'bg': '#121212', 'size': 19, 'bold': False, 'color': '#E8E8E8', 'badge': (1394, 330, 1413, 349), 'badge_gap': 5},
    {'slot': 'rechts', 'field': 'subject', 'x': 1281, 'baseline': 374, 'max_x': 1585, 'clear': (1278, 356, 1590, 377),
     'bg': '#121212', 'size': 16, 'bold': True, 'color': '#83B2FF'},
    {'slot': 'rechts', 'field': 'preheader', 'x': 1281, 'baseline': 400, 'max_x': 1558, 'clear': (1278, 384, 1562, 405),
     'bg': '#121212', 'size': 16, 'bold': False, 'color': '#959595'},
]


# ---------------------------------------------------------------------------
# Aus dem tD Preview Builder übernommen (Abruf, Marke, Avatar-Quellen)
# ---------------------------------------------------------------------------
def e(x):
    return html.escape(x or '', quote=True)


def normalize_domain(value):
    value = (value or '').strip()
    if value and not value.startswith(('http://', 'https://')):
        value = 'https://' + value
    return value


def public_url(value):
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return False
        for answer in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
            ip = ipaddress.ip_address(answer[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


# Header-Sätze in dieser Reihenfolge: erst schlicht, dann „vollwertiger Browser“. Hintergrund: Bot-Schutz-Systeme (z. B. Akamai
# bei zalando.de) gleichen den behaupteten Browser (User-Agent, Sec-Fetch-*) mit dem TLS-Fingerabdruck der Verbindung ab. Ein
# Python-Client, der sich als Chrome ausgibt, fällt dabei oft eher auf als ein schlichter Client. Umgekehrt verlangen andere
# Seiten (z. B. hm.com) gerade vollständige Browser-Header. Deshalb werden alle Varianten nacheinander probiert.
BROWSER_HEADERS = [
    {'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8'},
    {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
     'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
     'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8', 'Accept-Encoding': 'gzip, deflate',
     'Upgrade-Insecure-Requests': '1', 'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate',
     'Sec-Fetch-Site': 'none', 'Sec-Fetch-User': '?1'},
    {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
     'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
     'Accept-Language': 'de-DE,de;q=0.9', 'Accept-Encoding': 'gzip, deflate'},
    {'User-Agent': 'Mozilla/5.0 (compatible; trustedDialogPreviewBuilder/1.0)', 'Accept': 'text/html,*/*;q=0.8',
     'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8'},
]
BOT_BLOCK_STATUS = {401, 403, 406, 418, 429, 451, 503, 999}      # typische Antworten eines Bot-Schutzes


def alternate_urls(value):
    """Dieselbe Seite mit bzw. ohne „www.“ – manche Server antworten nur unter einer der beiden Schreibweisen
    (oder der eine Host ist deutlich langsamer als der andere)."""
    try:
        parts = urlparse(value)
        host = parts.hostname or ''
        labels = host.split('.')
        if host.startswith('www.'):
            alt = host[4:]
        elif len(labels) == 2 or (len(labels) == 3 and labels[-2] in SECOND_LEVEL):
            alt = 'www.' + host
        else:
            return []
        netloc = alt + (f':{parts.port}' if parts.port else '')
        return [parts._replace(netloc=netloc).geturl()]
    except Exception:
        return []


def friendly_fetch_error(error):
    """Verständliche Fehlermeldung statt „HTTPSConnectionPool(host=…): Read timed out“."""
    if isinstance(error, requests.Timeout):
        return str(error) if not str(error).startswith(('HTTPSConnectionPool', 'HTTPConnectionPool')) else \
            'Zeitüberschreitung: Die Website hat nicht rechtzeitig geantwortet'
    if isinstance(error, requests.exceptions.SSLError):
        return 'SSL-/Zertifikatsfehler beim Verbindungsaufbau'
    if isinstance(error, requests.ConnectionError):
        return 'Verbindung zur Website nicht möglich (Server nicht erreichbar oder Verbindung abgebrochen)'
    return str(error)


def safe_fetch(value, max_bytes=7000000, truncate=False, budget=IMAGE_FETCH_BUDGET, alternates=False):
    """Lädt eine URL. truncate=True: bei Überschreitung von max_bytes wird abgeschnitten statt abgebrochen
    (für HTML sinnvoll: Meta-Tags, Logos und Farben stehen fast immer am Anfang der Seite). Kommt eine HTML-Seite nur
    teilweise an (Verbindung bricht ab / Timeout mitten im Download), wird der bereits geladene Anfang verwendet.
    Wird eine Variante abgelehnt (HTTP-Fehler, Bot-Schutz, Timeout), wird die nächste Header-Variante probiert.
    alternates=True: bei Misserfolg zusätzlich die Schreibweise mit/ohne „www.“.
    budget: Gesamtzeit in Sekunden für alle Versuche zusammen."""
    if not public_url(value):
        raise ValueError('Die Domain ist nicht öffentlich erreichbar oder wurde aus Sicherheitsgründen blockiert.')
    urls = [value] + [u for u in (alternate_urls(value) if alternates else []) if public_url(u)]
    started = time.monotonic()
    deadline = started + budget
    last_error, last_status, timed_out = None, None, False
    for headers, url in [(h, u) for h in BROWSER_HEADERS for u in urls]:     # je Header-Variante erst alle Hosts
        remaining = deadline - time.monotonic()
        if remaining < 3:
            break
        timeout = (min(FETCH_CONNECT_TIMEOUT, remaining), min(FETCH_READ_TIMEOUT, remaining))
        try:
            with requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True) as response:
                if response.status_code >= 400:
                    last_status = response.status_code
                    last_error = requests.HTTPError(f'HTTP {response.status_code}')
                    continue                                          # nächste Variante
                if not public_url(response.url):
                    raise ValueError('Die Weiterleitung wurde blockiert.')
                content_type = response.headers.get('content-type', '')
                data = bytearray()
                try:
                    for chunk in response.iter_content(65536):
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            if truncate:
                                del data[max_bytes:]
                                break
                            raise ValueError('Die abgerufene Datei ist zu groß.')
                except requests.RequestException:
                    if truncate and len(data) >= 30000:               # Anfang der Seite genügt für die Analyse
                        return bytes(data), response.url, content_type
                    raise
                return bytes(data), response.url, content_type
        except requests.Timeout as error:
            timed_out, last_error = True, error
        except requests.RequestException as error:
            last_error = error
    if last_status in BOT_BLOCK_STATUS:
        raise requests.HTTPError(f'{last_status} – die Website blockiert automatische Abrufe (Bot-Schutz)')
    if last_status:
        raise requests.HTTPError(f'HTTP-Fehler {last_status} beim Abruf der Website')
    if timed_out:
        raise requests.Timeout(f'Zeitüberschreitung: Die Website hat auch nach {int(time.monotonic() - started)} Sekunden '
                               'nicht (vollständig) geantwortet')
    raise last_error or ValueError('Die Website konnte nicht geladen werden.')


def meta(soup, *keys):
    for key in keys:
        node = soup.find('meta', attrs={'property': key}) or soup.find('meta', attrs={'name': key})
        if node and node.get('content'):
            return node['content'].strip()
    return ''


def unique(values):
    out = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def short(value, limit=37):
    value = re.sub(r'\s+', ' ', value or '').strip()
    return value if len(value) <= limit else value[:limit - 3].rstrip() + '...'



LEGAL_SUFFIX_RE = re.compile(
    r'(?:,?\s+)(?:GmbH\s*&\s*Co\.?\s*(?:KGaA|KG|OHG)|GmbH|gGmbH|AG|SE|KGaA|KG|OHG|UG(?:\s*\(haftungsbeschränkt\))?|'
    r'e\.\s?K\.?|e\.\s?V\.?|Inc\.?|Ltd\.?|LLC|S\.A\.?|B\.V\.?|N\.V\.?|S\.r\.l\.?|Corp\.?|Co\.)\s*$', re.I)
SHOP_SUFFIX_RE = re.compile(
    r'(?:\s*[-–|:]\s*|\s+)(?:offizielle[rs]?\s+)?(?:online[- ]?shop|onlineshop|shop online|webshop|online[- ]?store|'
    r'official (?:online )?(?:store|site|website)|offizielle (?:website|seite|homepage))\s*$', re.I)
GENERIC_NAMES = {'home', 'startseite', 'homepage', 'start', 'shop', 'onlineshop', 'online shop', 'willkommen', 'welcome',
                 'index', 'website', 'offizielle website', 'official website', 'mode', 'news', 'login', 'anmelden'}
TITLE_SPLIT_RE = re.compile(r'\s+[|–—·•»:]\s+|\s+-\s+|\s*\|\s*')
SECOND_LEVEL = {'co', 'com', 'org', 'net', 'ac', 'gv', 'or', 'ne', 'gov', 'edu'}


def brand_key(value):
    value = (value or '').lower()
    for a, b in (('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'), ('ß', 'ss'), ('&', 'und')):
        value = value.replace(a, b)
    return re.sub(r'[^a-z0-9]', '', value)


def domain_label(host):
    parts = [p for p in (host or '').lower().split('.') if p and p != 'www']
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else '')


def clean_brand(value):
    value = re.sub(r'\s+', ' ', html.unescape(value or '')).strip(' -|–—·•:»«"\'®™©')
    value = re.sub(r'^(?:©|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*', '', value, flags=re.I)
    for _ in range(2):
        value = SHOP_SUFFIX_RE.sub('', value).strip(' -|–—·,')
        value = LEGAL_SUFFIX_RE.sub('', value).strip(' -|–—·,')
    if ' ' not in value:                                   # „dm.de“ / „home24.de“ -> Markenname ohne Endung
        value = re.sub(r'(?i)^(?:www\.)?([\w-]+)\.(?:de|com|at|ch|net|org|eu|shop|info|biz|io|co\.uk|nl|fr|it)$', r'\1', value)
    return value.strip(' ®™')


def json_ld_names(soup):
    """Liefert (Name, Gewicht) aus JSON-LD: Organization/Brand/Store > WebSite > publisher."""
    out = []

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        kind = node.get('@type', '')
        kind = ' '.join(kind) if isinstance(kind, list) else str(kind)
        name = node.get('name') if isinstance(node.get('name'), str) else ''
        if name:
            if re.search(r'Organization|Corporation|Brand|Store|LocalBusiness|NewsMediaOrganization', kind):
                out.append((name, 6))
            elif 'WebSite' in kind:
                out.append((name, 4))
        for key in ('publisher', 'brand', 'author', '@graph', 'mainEntity'):
            if key in node:
                walk(node[key], depth + 1)

    for tag in soup.find_all('script', attrs={'type': 'application/ld+json'})[:12]:
        try:
            walk(json.loads(tag.string or tag.get_text() or ''))
        except Exception:
            continue
    return out


def detect_brand(soup, host):
    """Markenname des Absenders. Quellen werden gewichtet und gegen die Domain geprüft,
    damit nicht ein Seitentitel wie „Mode online kaufen“ als Absender landet."""
    label = domain_label(host)
    label_key = brand_key(label)
    candidates = []

    def add(value, weight):
        value = clean_brand(value)
        if 2 <= len(value) <= 40 and value.lower() not in GENERIC_NAMES:
            candidates.append((value, weight))

    add(meta(soup, 'og:site_name'), 6)
    add(meta(soup, 'application-name'), 5)
    add(meta(soup, 'apple-mobile-web-app-title'), 5)
    for name, weight in json_ld_names(soup):
        add(name, weight)
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    for source, weight in ((title, 3), (meta(soup, 'og:title'), 2)):
        parts = [p for p in TITLE_SPLIT_RE.split(source or '') if p.strip()]
        for index, part in enumerate(parts):
            add(part, weight + (1 if index in (0, len(parts) - 1) and len(parts) > 1 else 0))
    for tag in soup.find_all('img')[:80]:
        marker = (' '.join(tag.get('class', [])) + ' ' + (tag.get('id') or '') + ' ' + (tag.get('src') or '')).lower()
        if 'logo' in marker:
            add(re.sub(r'(?i)\s*(?:-\s*)?logo\b.*$', '', tag.get('alt') or tag.get('title') or ''), 3)
    twitter = meta(soup, 'twitter:site').lstrip('@')
    if twitter:
        add(twitter, 1)
    copyright_match = re.search(r'(?:©|&copy;|Copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*([A-ZÄÖÜ][\w&.\- ]{1,40}?)'
                                r'(?:\s*(?:GmbH|AG|SE|KG|Inc|Ltd)\b|[.,|]|\s{2}|$)', soup.get_text(' ', strip=True)[-6000:])
    if copyright_match:
        add(copyright_match.group(1), 2)

    best, best_score = '', -99.0
    for value, weight in candidates:
        key = brand_key(value)
        if not key:
            continue
        score = weight
        if label_key and key == label_key:
            score += 10
        elif label_key and len(label_key) >= 2 and (key.startswith(label_key) or label_key.startswith(key)):
            score += 7
        elif label_key and len(label_key) >= 3 and (label_key in key or key in label_key):
            score += 5
        elif not label_key or weight < 5:
            score -= 4                      # Titel-Fragment ohne Bezug zur Domain -> eher Slogan als Marke
        words = len(value.split())
        score -= max(0, words - 3) * 1.5 + len(value) * .04
        if value.isupper() and len(value) > 4 and words == 1 and label_key and key == label_key:
            score += .5                     # Schreibweise der Marke (z. B. „OTTO“) beibehalten
        if score > best_score:
            best, best_score = value, score
    if best and best_score >= 3:
        return best
    return label.replace('-', ' ').title() if label else host




HEX_RE = re.compile(r'#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])')
RGB_RE = re.compile(r'rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})')
CSS_VAR_RE = re.compile(
    r'--[\w-]*(?:primary|brand|accent|cta|highlight|key|main)[\w-]*\s*:\s*(#[0-9a-fA-F]{3,6}\b|rgba?\([^)]+\))', re.I)


def rgb_to_hex(rgb):
    return '#%02x%02x%02x' % tuple(int(max(0, min(255, v))) for v in rgb[:3])


def parse_color(raw):
    """CSS-Farbe (#abc, #aabbcc, rgb(...)) -> (r, g, b) oder None."""
    raw = (raw or '').strip()
    m = HEX_RE.fullmatch(raw)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = RGB_RE.match(raw)
    if m:
        return tuple(min(255, int(v)) for v in m.groups())
    return None


def safe_brand_rgb(value):
    try:
        rgb = ImageColor.getrgb(value)
        return tuple(rgb[:3])
    except Exception:
        return (19, 117, 215)


def rel_lum(rgb):
    def channel(v):
        v /= 255
        return v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4
    r, g, b = [channel(v) for v in rgb[:3]]
    return .2126 * r + .7152 * g + .0722 * b


def contrast(a, b):
    la, lb = rel_lum(a), rel_lum(b)
    return (max(la, lb) + .05) / (min(la, lb) + .05)


OFFER_PATTERNS = [r'\b\d{1,2}\s?%\s*(?:Rabatt|sparen|off)?\b',
                  r'\b\d+(?:[,.]\d{1,2})?\s?€\s*(?:Rabatt|Gutschein|sparen)?\b',
                  r'\b(?:Willkommensrabatt|Gratis Versand|kostenloser Versand|Sale|Gutschein|Rabatt)\b[^.!?]{0,65}']
GENERIC_ALT_RE = re.compile(r'^(?:image|img|bild|foto|photo|banner|teaser|hero|stage|slide|picture|grafik|platzhalter|'
                            r'placeholder|logo|kachel|\d+)$', re.I)


def find_offers(text):
    out = []
    for pattern in OFFER_PATTERNS:
        out.extend(re.findall(pattern, text or '', flags=re.I))
    return unique([re.sub(r'\s+', ' ', x).strip(' -|') for x in out])


def icon_size_hint(value):
    sizes = [int(a) * int(b) for a, b in re.findall(r'(\d+)\s*x\s*(\d+)', value or '', re.I)]
    return int(max(sizes) ** .5) if sizes else 0


SVG_CASE = ['viewBox', 'preserveAspectRatio', 'gradientUnits', 'gradientTransform', 'patternUnits', 'patternTransform',
            'clipPathUnits', 'maskUnits', 'maskContentUnits', 'stdDeviation', 'textLength', 'lengthAdjust', 'baseProfile',
            'clipPath', 'linearGradient', 'radialGradient', 'feGaussianBlur', 'feOffset', 'feFlood', 'feComposite',
            'feMerge', 'feMergeNode', 'feColorMatrix', 'feBlend', 'textPath', 'foreignObject', 'xlink:href']


def fix_svg_case(markup):
    """Der HTML-Parser schreibt Inline-SVG klein (viewbox, lineargradient) – SVG braucht die Originalschreibweise."""
    for name in SVG_CASE:
        markup = re.sub(r'(?<=[\s<"/])' + name.lower() + r'(?=[\s=>/])', name, markup)
    if 'xmlns=' not in markup[:300]:
        markup = markup.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    return markup


def collect_avatar_sources(soup, base_url):
    """Sammelt Avatar-Kandidaten mit Priorität. Rückgabe: Liste von (Priorität, Quelle, Bezeichnung);
    Quelle ist eine URL oder ein Inline-SVG (beginnt mit '<svg')."""
    found = []
    manifest = ''
    for link in soup.find_all('link', href=True):
        rel = ' '.join(link.get('rel', [])).lower()
        href = urljoin(base_url, link['href'])
        size = icon_size_hint(link.get('sizes'))
        if 'apple-touch-icon' in rel:
            found.append((10 + min(size, 512) / 256, href, f'App-Icon{f" {size}px" if size else ""}'))
        elif 'mask-icon' in rel:
            found.append((2, href, 'Masken-Icon (SVG)'))
        elif 'icon' in rel:
            svg = 'svg' in (link.get('type') or '') or href.lower().split('?')[0].endswith('.svg')
            found.append((7 + (2 if svg else 0) + min(size, 256) / 128, href, 'Favicon (SVG)' if svg else
                          f'Favicon{f" {size}px" if size else ""}'))
        elif 'manifest' in rel and not manifest:
            manifest = href
    tile = meta(soup, 'msapplication-TileImage')
    if tile:
        found.append((6, urljoin(base_url, tile), 'Kachel-Icon'))
    for tag in soup.find_all('script', attrs={'type': 'application/ld+json'})[:12]:
        for logo in re.findall(r'"logo"\s*:\s*(?:\{[^}]*?"url"\s*:\s*)?"([^"]+)"', tag.string or tag.get_text() or ''):
            found.append((8.5, urljoin(base_url, logo), 'Logo (strukturierte Daten)'))
    for tag in soup.find_all('img'):
        src = tag.get('src') or tag.get('data-src') or tag.get('data-lazy-src')
        marker = (' '.join(tag.get('class', [])) + ' ' + (tag.get('id') or '') + ' ' + (tag.get('alt') or '') + ' '
                  + (src or '')).lower()
        if src and 'logo' in marker and not src.startswith('data:image/gif'):
            found.append((5, urljoin(base_url, src), 'Logo-Grafik der Seite'))
    for svg in soup.find_all('svg')[:60]:
        parents = [p for p in svg.parents if getattr(p, 'name', None) in ('a', 'div', 'span', 'header')][:3]
        marker = (' '.join(svg.get('class', [])) + ' ' + (svg.get('id') or '') + ' ' + (svg.get('aria-label') or '') +
                  ' ' + ' '.join(' '.join(p.get('class', [])) + ' ' + (p.get('id') or '') for p in parents)).lower()
        if 'logo' in marker and len(str(svg)) < 150000 and not svg.find('use'):
            found.append((4, fix_svg_case(str(svg)), 'Logo (Inline-SVG)'))
    if manifest:
        try:
            raw, final, _ = safe_fetch(manifest, 300000)
            for icon in json.loads(raw.decode('utf-8', errors='ignore')).get('icons', [])[:8]:
                if icon.get('src'):
                    size = icon_size_hint(icon.get('sizes'))
                    found.append((9 + min(size, 512) / 256, urljoin(final, icon['src']),
                                  f'Web-App-Icon{f" {size}px" if size else ""}'))
        except Exception:
            pass
    found.append((1, urljoin(base_url, '/favicon.ico'), 'Favicon (Standard)'))
    found.sort(key=lambda item: item[0], reverse=True)
    out, seen = [], set()
    for priority, source, label in found:
        if source not in seen:
            seen.add(source)
            out.append((priority, source, label))
    return out[:16]




def is_svg_bytes(raw):
    head = raw[:2048].lstrip().lower()
    return head.startswith(b'<svg') or (head.startswith(b'<?xml') and b'<svg' in raw[:4096].lower()) or \
        (head.startswith(b'<!--') and b'<svg' in raw[:4096].lower())


def sized_svg(raw):
    """SVG ohne width/height erhält Maße aus der viewBox – sonst zeichnet der Browser es nicht zuverlässig in ein Canvas."""
    text = raw.decode('utf-8', errors='ignore')
    match = re.search(r'<svg\b[^>]*>', text, re.I)
    if not match:
        return raw
    tag = match.group(0)
    if 'xmlns=' not in tag:
        tag = tag[:-1].rstrip('/') + ' xmlns="http://www.w3.org/2000/svg">' if not tag.endswith('/>') else tag
    width = re.search(r'\swidth\s*=\s*["\']\s*([\d.]+)(px)?\s*["\']', tag)
    height = re.search(r'\sheight\s*=\s*["\']\s*([\d.]+)(px)?\s*["\']', tag)
    view = re.search(r'viewBox\s*=\s*["\']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', tag, re.I)
    if not (width and height):
        w, h = (float(view.group(1)), float(view.group(2))) if view else (256.0, 256.0)
        tag = re.sub(r'\s(width|height)\s*=\s*["\'][^"\']*["\']', '', tag)
        tag = tag.replace('<svg', f'<svg width="{w:g}" height="{h:g}"', 1)
    return (text[:match.start()] + tag + text[match.end():]).encode('utf-8')


AVATAR_DARK_BG = '#1F1F1F'      # Hintergrund für helle/weiße Logos, die auf Weiß nicht lesbar wären
AVATAR_IDEAL_PX = 132          # Avatar: 33 px Anzeige x 4 (PNG-Export 3-fach + Reserve) -> kleinste Größe ab hier ist ideal
NAMED_COLORS = {'white': (255, 255, 255), 'black': (0, 0, 0), 'red': (255, 0, 0), 'blue': (0, 0, 255),
                'green': (0, 128, 0), 'yellow': (255, 255, 0), 'orange': (255, 165, 0), 'gray': (128, 128, 128),
                'grey': (128, 128, 128), 'silver': (192, 192, 192), 'navy': (0, 0, 128)}


def initials_text_color(background):
    """Initialen immer gut lesbar: Weiß nur bei ausreichendem Kontrast (WCAG 4,5:1), sonst dunkles Blau bzw. Schwarz."""
    bg = safe_brand_rgb(background)
    for candidate in ('#FFFFFF', '#0B3C6E', '#000000'):
        if contrast(ImageColor.getrgb(candidate), bg) >= 4.5:
            return candidate
    return '#000000'


LOGO_MIN_CONTRAST = 2.5        # Mindestkontrast Logo/Hintergrund (klassisches Orange auf Weiß bleibt auf Weiß)


def readable_on(pixels, background, threshold=LOGO_MIN_CONTRAST):
    """Anteil der sichtbaren Logo-Pixel, die sich ausreichend vom Hintergrund abheben."""
    if not pixels:
        return 1.0
    return sum(1 for p in pixels if contrast(p, background) >= threshold) / len(pixels)


def logo_backdrop(image):
    """Wählt den Avatar-Hintergrund für eine Rastergrafik: Weiß (Standard) – außer das Logo ist hell/weiß und
    wäre auf Weiß nicht lesbar; dann dunkler Hintergrund. Vollflächige Icons (eigener Hintergrund) bleiben auf Weiß."""
    rgba = image.convert('RGBA')
    rgba.thumbnail((64, 64))
    data = list(rgba.getdata())
    visible = [p[:3] for p in data if p[3] > 160]
    if not visible or len(visible) / len(data) > .92:          # deckendes Icon: Hintergrund spielt keine Rolle
        return AVATAR_BG_DEFAULT
    white, dark = (255, 255, 255), ImageColor.getrgb(AVATAR_DARK_BG)
    if readable_on(visible, white) >= .3 or readable_on(visible, white) >= readable_on(visible, dark):
        return AVATAR_BG_DEFAULT
    return AVATAR_DARK_BG


def svg_backdrop(raw):
    """Wie logo_backdrop, für SVG (ohne Rendern): wertet die im SVG verwendeten Farben aus.
    Ohne Farbangabe zeichnet SVG schwarz -> Weiß passt."""
    text = raw.decode('utf-8', errors='ignore').lower()
    colors = []
    for value in re.findall(r'(?:fill|stroke|stop-color|color)\s*[:=]\s*["\']?\s*(#[0-9a-f]{3,6}\b|rgba?\([^)]*\)|[a-z]+)', text):
        rgb = parse_color(value) or NAMED_COLORS.get(value)
        if rgb:
            colors.append(rgb)
    if not colors:
        return AVATAR_BG_DEFAULT
    white, dark = (255, 255, 255), ImageColor.getrgb(AVATAR_DARK_BG)
    if readable_on(colors, white) >= .3 or readable_on(colors, white) >= readable_on(colors, dark):
        return AVATAR_BG_DEFAULT
    return AVATAR_DARK_BG


# ----------------------------------------------------------------------------
# Streamlit-Version-sichere Helfer
# ----------------------------------------------------------------------------
def stretch(fn, *args, **kwargs):
    """Volle Breite – neue Streamlit-Versionen: width='stretch', ältere: use_container_width=True."""
    try:
        return fn(*args, width='stretch', **kwargs)
    except Exception:
        return fn(*args, use_container_width=True, **kwargs)


# ----------------------------------------------------------------------------
# Website-Analyse (schlanke Variante aus dem tD Preview Builder)
# ----------------------------------------------------------------------------
def analyze_site(value, pasted=None):
    """pasted=None: Website wird abgerufen. pasted='...': eingefügter Quelltext/Text wird analysiert (Workaround bei
    Bot-Schutz). pasted='': nur die Marke wird aus der Domain abgeleitet."""
    if pasted is None:
        raw, final_url, _ = safe_fetch(normalize_domain(value), 3000000, truncate=True,
                                       budget=PAGE_FETCH_BUDGET, alternates=True)
    else:
        raw, final_url = pasted.encode('utf-8'), normalize_domain(value)
    soup = BeautifulSoup(raw, 'html.parser')
    host = (urlparse(final_url).hostname or '').replace('www.', '')
    brand = detect_brand(soup, host)
    description = meta(soup, 'og:description', 'description')
    offers = find_offers(' '.join(soup.stripped_strings))[:8]
    offer = offers[0] if offers else ''
    if offer:                                                   # gleiche Logik wie im tD Preview Builder
        subject = short(('Jetzt ' + offer + ' sichern') if len(offer) < 26 else offer)
        preheader = short('Angebot jetzt entdecken')
    else:
        subject = short('Neuigkeiten von ' + brand)
        preheader = short('Jetzt Vorteile entdecken')
    return {'url': final_url, 'brand': short(brand), 'description': description, 'offers': offers,
            'avatar_sources': collect_avatar_sources(soup, final_url), 'subject': subject, 'preheader': preheader}


# ----------------------------------------------------------------------------
# Bilder einlesen – SVG-Unterstützung (Pflicht) über cairosvg
# ----------------------------------------------------------------------------
def svg_to_image(raw, side=RENDER_SIDE):
    """SVG -> RGBA-Bild (transparent), längste Kante = side. Bringt SVGs ohne/mit prozentualen Maßen in Form."""
    if cairosvg is None:
        raise RuntimeError('SVG-Dateien benötigen das Python-Paket „cairosvg“ und die Systembibliothek libcairo2 '
                           '(requirements.txt: cairosvg, packages.txt: libcairo2).')
    text = raw.decode('utf-8-sig', errors='ignore')
    text = re.sub(r'<script\b.*?</script>', '', text, flags=re.I | re.S)
    match = re.search(r'<svg\b[^>]*>', text, re.I)
    if not match:
        raise ValueError('Die Datei ist keine gültige SVG-Grafik.')
    tag = match.group(0)
    view = re.search(r'viewBox\s*=\s*["\']\s*(-?[\d.]+)[\s,]+(-?[\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)', tag, re.I)

    def number(attr):
        found = re.search(r'\s' + attr + r'\s*=\s*["\']\s*([\d.]+)\s*(?:px)?\s*["\']', tag, re.I)
        return float(found.group(1)) if found else None

    width, height = number('width'), number('height')
    if not (width and height):
        width, height = (float(view.group(3)), float(view.group(4))) if view else (256.0, 256.0)
    if not width or not height:
        width, height = 256.0, 256.0
    scale = side / max(width, height)
    new_tag = re.sub(r'\s(?:width|height)\s*=\s*["\'][^"\']*["\']', '', tag, flags=re.I)
    extra = f' width="{width * scale:.2f}" height="{height * scale:.2f}"'
    if not view:
        extra += f' viewBox="0 0 {width:g} {height:g}"'
    if 'xmlns=' not in new_tag:
        extra += ' xmlns="http://www.w3.org/2000/svg"'
    if 'xlink:' in text and 'xmlns:xlink' not in new_tag:
        extra += ' xmlns:xlink="http://www.w3.org/1999/xlink"'
    new_tag = new_tag.replace('<svg', '<svg' + extra, 1) if not new_tag.startswith('<SVG') else new_tag.replace('<SVG', '<SVG' + extra, 1)
    png = cairosvg.svg2png(bytestring=(text[:match.start()] + new_tag + text[match.end():]).encode('utf-8'))
    return Image.open(io.BytesIO(png)).convert('RGBA')


def raw_to_image(raw, side=RENDER_SIDE):
    """Bilddaten (SVG, PNG, JPG, WEBP, ICO) -> RGBA-Bild mit max. `side` px Kantenlänge."""
    if is_svg_bytes(raw):
        return svg_to_image(raw, side)
    image = Image.open(io.BytesIO(raw))
    if getattr(image, 'format', '') == 'ICO':
        sizes = sorted(image.info.get('sizes', []) or [image.size])
        image.size = sizes[-1]
    image.load()
    image = image.convert('RGBA')
    if max(image.size) > side:
        image.thumbnail((side, side), Image.Resampling.LANCZOS)
    return image


def trim_logo(image):
    """Weißen Hintergrund (JPG/PNG ohne Alpha) freistellen und auf den sichtbaren Bereich zuschneiden."""
    logo = image.convert('RGBA')
    w, h = logo.size
    px = logo.load()
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    if all(c[3] > 240 and min(c[:3]) > 240 for c in corners):
        logo.putdata([(r, g, b, 0) if min(r, g, b) > 238 else (r, g, b, a) for r, g, b, a in logo.getdata()])
    bbox = logo.getchannel('A').point(lambda v: 255 if v > 20 else 0).getbbox()
    return logo.crop(bbox) if bbox else logo


def png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


def data_uri(image, box):
    small = image.copy()
    small.thumbnail(box, Image.Resampling.LANCZOS)
    return 'data:image/png;base64,' + base64.b64encode(png_bytes(small)).decode()


def contain(image, box):
    """Proportional in die Box einpassen (auch vergrößern) – wie CSS object-fit: contain."""
    scale = min(box[0] / image.width, box[1] / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


# ----------------------------------------------------------------------------
# Kandidaten (Avatare / Logos) der Website
# ----------------------------------------------------------------------------
def build_candidate(raw, label, priority, url=''):
    """Bilddaten -> Kandidat (Avatar-Ansicht + zugeschnittenes Logo) oder None, wenn unbrauchbar."""
    svg = is_svg_bytes(raw)
    image = raw_to_image(raw)
    if max(image.size) < 24:
        return None
    side = 9999 if svg else min(image.size)
    trimmed = trim_logo(image)
    if trimmed.width < 4 or trimmed.height < 4:
        return None
    flat = Image.new('RGBA', image.size, (255, 255, 255, 255))
    flat.alpha_composite(image)
    flat = flat.convert('RGB')
    ink = ImageOps.invert(flat.convert('L')).point(lambda v: 255 if v > 24 else 0).getbbox()
    core = flat.crop(ink) if ink else flat
    sig = list(core.resize((16, 16), Image.Resampling.BOX).getdata())
    avatar = image.copy()
    avatar.thumbnail((512, 512), Image.Resampling.LANCZOS)
    return {'png': png_bytes(avatar), 'logo_png': png_bytes(trimmed), 'svg': svg, 'side': side,
            'oratio': image.width / image.height, 'ratio': trimmed.width / trimmed.height,
            'bg': logo_backdrop(image), 'sig': sig, 'label': label, 'priority': priority, 'url': url,
            'detail': 'SVG' if svg else f'{image.width}×{image.height}px',
            'thumb': data_uri(avatar, (96, 96)), 'logo_thumb': data_uri(trimmed, (240, 60)), 'hidden': 0}


def same_image(a, b):
    """Optisch gleiche Grafik (nur andere Pixelgröße)?"""
    if abs(a['oratio'] - b['oratio']) > .12:
        return False
    diff = sum(abs(x - y) for p, q in zip(a['sig'], b['sig']) for x, y in zip(p, q)) / (16 * 16 * 3)
    return diff < (40 if min(a['side'], b['side']) < 48 else 22)


def ideal_rank(c):
    """Kleinere Zahl = bessere Auflösung: SVG zuerst, dann kleinste Variante ab AVATAR_IDEAL_PX, sonst die größte."""
    if c['svg']:
        return (-1, 0)
    return (0, c['side']) if c['side'] >= AVATAR_IDEAL_PX else (1, -c['side'])


def fetch_candidate(item):
    priority, source, label = item
    try:
        inline = source.lstrip().startswith('<svg')
        raw = source.encode('utf-8') if inline else safe_fetch(source, 2500000, budget=IMAGE_FETCH_BUDGET)[0]
        return build_candidate(raw, label, priority, '' if inline else source)
    except Exception:
        return None


def load_pool(sources):
    """Lädt alle Kandidaten parallel; jede Grafik wird nur EINMAL angeboten (Favicon 32/96/192 px = ein Vorschlag)."""
    with ThreadPoolExecutor(max_workers=6) as executor:
        found = list(executor.map(fetch_candidate, sources))
    groups = []
    for cand in found:
        if not cand:
            continue
        group = next((g for g in groups if same_image(g[0], cand)), None)
        if group is None:
            groups.append([cand])
        else:
            group.append(cand)
    pool = []
    for group in groups:
        best = dict(min(group, key=ideal_rank))
        best['hidden'] = len(group) - 1
        best['priority'] = max(c['priority'] for c in group)
        pool.append(best)
    return pool


def avatar_score(c):
    score = c['priority'] + (2 if c['svg'] or c['side'] >= 120 else -3 if c['side'] < 48 else 0)
    if not .6 <= c['oratio'] <= 1.6:
        score -= 2                                      # breite Logos passen schlechter in den runden Avatar
    return score


def logo_score(c):
    return c['priority'] + (1.5 if c['svg'] else 0) - min(abs(math.log(c['ratio'] / LOGO_RATIO)), 2) * 1.5


def split_pool(pool):
    """Avatar-Auswahl (max. 8) und Logo-Auswahl (nur breite Grafiken) aus dem Kandidaten-Pool."""
    avatars = sorted(pool, key=avatar_score, reverse=True)[:AVATAR_LIMIT]
    logos = sorted([c for c in pool if c['ratio'] >= 1.8], key=logo_score, reverse=True)[:6]
    return avatars, logos


def cand_image(cand, key='png'):
    return Image.open(io.BytesIO(cand[key])).convert('RGBA')


# ----------------------------------------------------------------------------
# Schriften (für Initialen und Textfelder)
# ----------------------------------------------------------------------------
FONT_REGULAR = ['liberationsans-regular.ttf', 'arimo-regular.ttf', 'arial.ttf', 'roboto-regular.ttf', 'inter-regular.ttf',
                'opensans-regular.ttf', 'notosans-regular.ttf', 'dejavusans.ttf']
FONT_BOLD = ['liberationsans-bold.ttf', 'arimo-bold.ttf', 'arialbd.ttf', 'roboto-bold.ttf', 'inter-bold.ttf', 'opensans-bold.ttf',
             'notosans-bold.ttf', 'dejavusans-bold.ttf']


@lru_cache(maxsize=1)
def font_index():
    here = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
    roots = [os.path.join(here, 'fonts'), '/usr/share/fonts', '/usr/local/share/fonts', os.path.expanduser('~/.fonts'),
             '/Library/Fonts', '/System/Library/Fonts/Supplemental', 'C:/Windows/Fonts']
    found = {}
    for root in roots:
        for directory, _, files in os.walk(root):
            for name in files:
                if name.lower().endswith(('.ttf', '.otf')):
                    found.setdefault(name.lower(), os.path.join(directory, name))
    return found


@lru_cache(maxsize=64)
def get_font(size, bold=False):
    size = max(6, int(size))
    index = font_index()
    for name in (FONT_BOLD if bold else FONT_REGULAR):
        if name in index:
            try:
                return ImageFont.truetype(index[name], size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


# ----------------------------------------------------------------------------
# template.png: Platzhalter ersetzen
# ----------------------------------------------------------------------------
def script_dir():
    return Path(__file__).resolve().parent


@st.cache_resource(show_spinner=False)
def load_template():
    path = script_dir() / 'template.png'
    if not path.exists():
        return None
    return Image.open(path).convert('RGBA')


def bbox_size(bbox):
    return (bbox[2] - bbox[0] + 1, bbox[3] - bbox[1] + 1)


def ring_color(template, bbox, gap=3):
    """Hintergrundfarbe rund um den Platzhalter (Median eines schmalen Rahmens)."""
    x0, y0, x1, y1 = bbox
    width, height = template.size
    px = template.load()
    points = []
    for x in range(x0, x1 + 1, 2):
        for y in (y0 - gap, y1 + gap):
            if 0 <= x < width and 0 <= y < height:
                points.append(px[x, y][:3])
    for y in range(y0, y1 + 1, 2):
        for x in (x0 - gap, x1 + gap):
            if 0 <= x < width and 0 <= y < height:
                points.append(px[x, y][:3])
    if not points:
        return (255, 255, 255)
    return tuple(sorted(p[i] for p in points)[len(points) // 2] for i in range(3))


def slot_pink(template, bbox):
    """Farbe des Platzhalters (häufigste Farbe innerhalb der bbox)."""
    crop = template.crop((bbox[0], bbox[1], bbox[2] + 1, bbox[3] + 1)).convert('RGB')
    colors = crop.getcolors(maxcolors=crop.width * crop.height + 1) or []
    return max(colors)[1] if colors else (255, 0, 255)


def clear_slot(image, bbox, pink, fill, tolerance=130, margin=3):
    """Pinke Platzhalter-Pixel (auch Kantenpixel) durch die Umgebungsfarbe ersetzen."""
    px = image.load()
    width, height = image.size
    for y in range(max(0, bbox[1] - margin), min(height, bbox[3] + margin + 1)):
        for x in range(max(0, bbox[0] - margin), min(width, bbox[2] + margin + 1)):
            p = px[x, y]
            fringe = min(p[0], p[2]) - p[1] > 10 and abs(p[0] - p[2]) < 60        # Misch-Pixel pink/Hintergrund an der Kante
            if fringe or sum(abs(p[i] - pink[i]) for i in range(3)) <= tolerance:
                px[x, y] = fill + (255,)


def render_avatar(image, size, bg, fit='contain', pad=0.0, initials=''):
    """Runder Avatar in 8-facher Auflösung, dann heruntergerechnet. image=None: Initialen auf Fallback-Farbe."""
    ss = SUPERSAMPLE
    big = (size[0] * ss, size[1] * ss)
    layer = Image.new('RGBA', big, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((0, 0, big[0] - 1, big[1] - 1), fill=ImageColor.getrgb(bg) + (255,))
    if image is not None:
        inner = (max(1, round(big[0] * (1 - 2 * pad))), max(1, round(big[1] * (1 - 2 * pad))))
        fitted = ImageOps.fit(image, inner, Image.Resampling.LANCZOS) if fit == 'cover' else contain(image, inner)
        layer.alpha_composite(fitted, ((big[0] - fitted.width) // 2, (big[1] - fitted.height) // 2))
    else:
        ImageDraw.Draw(layer).text((big[0] / 2, big[1] / 2), initials or 'M', font=get_font(big[1] * .40, True),
                                   fill=initials_text_color(bg), anchor='mm')
    mask = Image.new('L', big, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big[0] - 1, big[1] - 1), fill=255)
    layer.putalpha(Image.composite(layer.getchannel('A'), Image.new('L', big, 0), mask))
    return layer.resize(size, Image.Resampling.LANCZOS)


def render_logo(image, size, pad=0, backdrop='Transparent', ss=SUPERSAMPLE):
    """Logo proportional in ein Rechteck (z. B. 5,5:1) einpassen. pad < 0 vergrößert, pad > 0 erzeugt Innenabstand."""
    big = (size[0] * ss, size[1] * ss)
    layer = Image.new('RGBA', big, (0, 0, 0, 0))
    if backdrop != 'Transparent':
        layer.paste(Image.new('RGBA', big, ImageColor.getrgb('#FFFFFF' if backdrop == 'Weiß' else AVATAR_DARK_BG) + (255,)))
    if image is not None:
        inner = (max(1, big[0] - 2 * round(pad * ss)), max(1, big[1] - 2 * round(pad * ss)))
        fitted = contain(image, inner)
        temp = Image.new('RGBA', big, (0, 0, 0, 0))
        temp.paste(fitted, ((big[0] - fitted.width) // 2, (big[1] - fitted.height) // 2))   # negative Offsets werden beschnitten
        layer.alpha_composite(temp)
    return layer.resize(size, Image.Resampling.LANCZOS)


def fit_text(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text.rstrip() + '…', font=font) > max_width:
        text = text[:-1]
    return text.rstrip() + '…'


def build_result(template, avatars, logo, opts, texts):
    """avatars: {'links': (Bild|None, bg), 'rechts': (Bild|None, bg)}; logo: Bild|None; texts: {'sender','subject','preheader'}"""
    result = template.copy()
    initials = ''.join(x[0] for x in (texts.get('sender') or '').split()[:2]).upper() or 'M'
    for key, slot_key in (('links', 'avatar_links'), ('rechts', 'avatar_rechts')):
        bbox = SLOTS[slot_key]['bbox']
        clear_slot(result, bbox, slot_pink(template, bbox), ring_color(template, bbox))
        image, bg = avatars[key]
        overlay = render_avatar(image, bbox_size(bbox), bg if image is not None else AVATAR_DEFAULT,
                                opts['fit'], opts['pad'], initials)
        result.alpha_composite(overlay, (bbox[0], bbox[1]))
    bbox = SLOTS['logo_mitte']['bbox']
    clear_slot(result, bbox, slot_pink(template, bbox), ring_color(template, bbox))
    if logo is not None:
        result.alpha_composite(render_logo(logo, bbox_size(bbox), opts['logo_pad'], opts['logo_bg']), (bbox[0], bbox[1]))
    badges = {id(slot): template.crop(slot['badge']) for slot in TEXT_SLOTS if slot.get('badge')}   # vor dem Überdecken sichern
    draw = ImageDraw.Draw(result)
    for slot in TEXT_SLOTS:                                    # Texte: Platzhaltertext überdecken, neuen Text setzen
        draw.rectangle(slot['clear'], fill=ImageColor.getrgb(slot['bg']) + (255,))
        value = re.sub(r'\s+', ' ', texts.get(slot['field']) or '').strip()
        font = get_font(slot['size'], slot['bold'])
        badge = badges.get(id(slot))
        reserve = (badge.width + slot.get('badge_gap', 5)) if badge else 0
        shown = fit_text(draw, value, font, slot['max_x'] - slot['x'] - reserve)
        draw.text((slot['x'], slot['baseline']), shown, font=font, fill=slot['color'], anchor='ls')
        if badge is not None and shown:
            end_x = slot['x'] + draw.textlength(shown, font=font)
            result.paste(badge, (round(end_x + slot.get('badge_gap', 5)) - 1, slot['badge'][1]))
    return result


def debug_overlay(template):
    preview = template.copy()
    draw = ImageDraw.Draw(preview)
    for slot in SLOTS.values():
        shape = draw.ellipse if slot['kind'] == 'circle' else draw.rectangle
        shape(slot['bbox'], outline=(0, 200, 255, 255) if slot['kind'] == 'circle' else (255, 200, 0, 255), width=2)
    for slot in TEXT_SLOTS:
        draw.rectangle(slot['clear'], outline=(0, 255, 0, 255), width=1)
    return preview


# ----------------------------------------------------------------------------
# Auswahl-Kacheln
# ----------------------------------------------------------------------------
def css_url(uri):
    return 'url("' + uri.replace('"', '%22') + '")'


def render_tiles(prefix, tiles, selected, per_row, on_pick, kind):
    """Anklickbare Mini-Vorschau. Jede Kachel ist ein Streamlit-Button; das Bild liegt per CSS im Button.
    kind: 'avatar' (Kreis) oder 'logo' (Rechteck 5,5:1)."""
    base = f'div[class*="st-key-{prefix}_"] button'
    if kind == 'logo':
        css = [f'{base}{{position:relative;width:100%;height:auto;min-height:0;padding:44px 4px 4px;border-radius:8px}}',
               f'{base}::before{{content:var(--txt,"");position:absolute;left:8px;right:8px;top:6px;height:32px;'
               f'border-radius:4px;box-shadow:0 0 0 1px rgba(0,0,0,.14);background:var(--bg,#fff) var(--thumb) center/contain no-repeat;'
               f'background-origin:content-box;color:#777;font:600 12px/32px "Source Sans Pro",Arial,sans-serif;text-align:center}}']
    else:
        css = [f'{base}{{position:relative;width:100%;height:auto;min-height:0;padding:52px 2px 4px;border-radius:10px}}',
               f'{base}::before{{content:var(--txt,"");position:absolute;left:50%;top:6px;width:40px;height:40px;'
               f'margin-left:-20px;border-radius:50%;box-shadow:0 0 0 1px rgba(0,0,0,.14);'
               f'background:var(--bg,#fff) var(--thumb) center/contain no-repeat;color:var(--fg,#000);'
               f'font:700 14px/40px "Source Sans Pro",Arial,sans-serif;text-align:center}}']
    css.append(f'{base} p{{font-size:12px;line-height:1.25;white-space:normal;word-break:break-word}}')
    for index, tile in enumerate(tiles):
        props = [f'--thumb:{css_url(tile["image"]) if tile.get("image") else "none"}',
                 f'--bg:{tile.get("bg") or AVATAR_BG_DEFAULT}']
        if tile.get('text'):
            props += [f'--txt:"{tile["text"][:3]}"', f'--fg:{initials_text_color(tile.get("bg") or AVATAR_DEFAULT)}']
        if index == selected:
            props += ['border:2px solid #1375d7!important', 'background-color:#eef5fd!important']
        css.append(f'.st-key-{prefix}_{index} button{{{";".join(props)}}}')
    st.markdown('<style>' + '\n'.join(css) + '</style>', unsafe_allow_html=True)
    for start in range(0, len(tiles), per_row):
        columns = st.columns(per_row, gap='small')
        for offset, tile in enumerate(tiles[start:start + per_row]):
            index = start + offset
            with columns[offset]:
                stretch(st.button, tile['caption'], key=f'{prefix}_{index}', help=tile.get('help'),
                        on_click=on_pick, args=(index,))


def pick_avatar(index):
    st.session_state.avatar_choice = -1 if index >= len(st.session_state.avatar_options) else index


def pick_logo(index):
    st.session_state.logo_choice = -1 if index >= len(st.session_state.logo_options) else index


# ====================================================================== UI ===
STICKY_CSS = (
    '<style>'
    '[data-testid="stColumn"]:has(#pl-sticky-anchor),[data-testid="column"]:has(#pl-sticky-anchor)'
    '{position:sticky;top:4rem;align-self:flex-start;z-index:5}'
    '[data-testid="stElementContainer"]:has(#pl-sticky-anchor),.element-container:has(#pl-sticky-anchor){display:none}'
    '[data-testid="stMainBlockContainer"],.block-container{padding-top:4.25rem!important}'
    '</style><div id="pl-sticky-anchor"></div>')

for key, value in {'domain': '', 'sender': 'Marke', 'subject': 'Betreff', 'preheader': 'Preview-Text',
                   'analysis': None, 'pool': [], 'avatar_options': [], 'avatar_choice': -1,
                   'logo_options': [], 'logo_choice': -1, 'logo_hint': '', 'fit': 'Eingepasst', 'pad': 0,
                   'logo_pad': 0, 'logo_bg': 'Transparent', 'fetch_failed': False, 'fetch_error': '',
                   'paste_html': ''}.items():
    st.session_state.setdefault(key, value)

template = load_template()
left, right = st.columns([1, 2], gap='large')          # links bearbeiten, rechts die sticky Vorschau

with left:
    st.title('Prelive-Vorschau')
    st.caption('Domain eingeben, Vorschläge automatisch erstellen lassen, bei Bedarf anpassen – '
               'am Ende alle drei Mockups als ein PNG herunterladen.')
    if template is None:
        st.error('template.png wurde nicht gefunden. Bitte im selben Ordner wie app.py ablegen.')
        st.stop()

    # ------------------------------------------------------------------ 1. Domain
    st.subheader('1. Unternehmen analysieren')
    with st.form('domain_form', border=False):        # Enter im Eingabefeld = Klick auf „Vorschläge erstellen“
        d1, d2 = st.columns([3, 2], vertical_alignment='bottom')
        with d1:
            st.text_input('Für welche Domain soll die Prelive-Vorschau erstellt werden?', key='domain',
                          placeholder='z. B. www.beispiel.de')
        with d2:
            analyze_clicked = stretch(st.form_submit_button, 'Vorschläge erstellen', type='primary')
    paste_clicked = manual_clicked = False
    if st.session_state.fetch_failed:
        st.warning('Die Website konnte nicht automatisch gelesen werden (' + st.session_state.fetch_error + '). '
                   'Ein erneuter Klick auf „Vorschläge erstellen“ hilft bei langsamen Servern manchmal. Sonst: '
                   '**Quelltext** der Startseite im Browser öffnen (Strg+U bzw. Cmd+Option+U), alles markieren, kopieren '
                   'und hier einfügen – dann werden auch Logos und Icons gefunden. Alternativ die Grafiken unten hochladen.')
        st.text_area('Quelltext oder Seitentext der Website (optional)', key='paste_html', height=150)
        f1, f2 = st.columns(2)
        with f1:
            paste_clicked = stretch(st.button, 'Aus eingefügtem Text analysieren',
                                    disabled=not st.session_state.paste_html.strip())
        with f2:
            manual_clicked = stretch(st.button, 'Ohne Analyse weiter',
                                     help='Nur die Marke wird aus der Domain abgeleitet; alles Weitere trägst du selbst ein.')
    fetch_failed_now = False
    if analyze_clicked or paste_clicked or manual_clicked:
        pasted = st.session_state.paste_html if paste_clicked else ('' if manual_clicked else None)
        if not st.session_state.domain.strip():
            st.warning('Bitte zuerst eine Domain eingeben.')
        else:
            try:
                with st.spinner('Website wird analysiert, Avatare und Logo werden gesucht …'):
                    result = analyze_site(st.session_state.domain, pasted)
                    st.session_state.analysis = result
                    st.session_state.sender = result['brand']
                    st.session_state.subject = result['subject']
                    st.session_state.preheader = result['preheader']
                    pool = load_pool(result['avatar_sources'])
                    avatars, logos = split_pool(pool)
                    st.session_state.pool = pool
                    st.session_state.avatar_options = avatars
                    st.session_state.avatar_choice = 0 if avatars else -1
                    st.session_state.logo_options = logos
                    fitting = [i for i, c in enumerate(logos) if c['ratio'] >= LOGO_FIT_MIN]
                    st.session_state.logo_choice = fitting[0] if fitting else -1
                    st.session_state.logo_hint = '' if fitting else (
                        'Es wurde kein breites Logo gefunden, das sich sinnvoll auf 5,5 : 1 bringen lässt – '
                        'bei Bedarf unten ein eigenes Logo hochladen.')
                st.session_state.fetch_failed = False
                if pasted is None or pasted:
                    st.success('Vorschläge wurden erstellt. Alle Inhalte können unten überschrieben werden.')
                else:
                    st.info('Es wurde nur die Marke aus der Domain abgeleitet. Texte und Grafiken bitte selbst eintragen bzw. hochladen.')
            except Exception as error:
                if pasted is None:                         # Abruf gescheitert -> Workaround anbieten
                    st.session_state.fetch_failed = True
                    st.session_state.fetch_error = friendly_fetch_error(error)
                    fetch_failed_now = True
                else:
                    st.error(f'Die Eingabe konnte nicht analysiert werden: {error}')
    if fetch_failed_now:
        st.rerun()
    analysis = st.session_state.analysis
    if analysis:
        with st.expander('Erkannte Website-Informationen', expanded=False):
            st.write(f"**Marke:** {analysis['brand']}")
            if analysis['offers']:
                st.write('**Gefundene Angebots-Hinweise:** ' + ' · '.join(analysis['offers'][:5]))
            if analysis['description']:
                st.write('**Beschreibung:** ' + analysis['description'][:350])
            st.caption('Bitte prüfen Sie die vorgeschlagenen Inhalte und stellen Sie sicher, dass die erforderlichen '
                       'Bild- und Markenrechte vorliegen.')

    # ------------------------------------------------------------------ 2. Texte
    st.subheader('2. Absender & Texte')
    with st.container(border=True):
        st.text_input('Absender / Marke', key='sender', max_chars=37)
        st.text_input('Betreff', key='subject', max_chars=37)
        st.text_input('Preview-Text', key='preheader', max_chars=37)
        if not TEXT_SLOTS:
            st.caption('Hinweis: Die Textfelder in template.png sind noch nicht eingemessen (TEXT_SLOTS in app.py) – '
                       'die Texte werden daher noch nicht in die Mockups geschrieben.')

    # ------------------------------------------------------------------ 3. Avatar
    st.subheader('3. Avatar (Mockup 1 und 3)')
    errors = []
    with st.container(border=True):
        options = st.session_state.avatar_options
        letters = ''.join(x[0] for x in (st.session_state.sender or '').split()[:2]).upper() or 'M'
        if options:
            st.caption('Grafik anklicken, um sie als Avatar zu verwenden.')
            tiles = [{'caption': str(i + 1), 'image': o['thumb'], 'bg': o['bg'], 'help': f"{o['label']} ({o['detail']})"}
                     for i, o in enumerate(options)]
            tiles.append({'caption': 'Initialen', 'image': '', 'bg': AVATAR_DEFAULT, 'text': letters,
                          'help': 'Keine Grafik – Initialen auf der Fallback-Farbe'})
            choice = st.session_state.avatar_choice
            render_tiles('apick', tiles, len(options) if choice == -1 else choice, 6, pick_avatar, 'avatar')
            if 0 <= choice < len(options):
                o = options[choice]
                hidden = f" · {o['hidden']} weitere Größe(n) derselben Grafik ausgeblendet" if o['hidden'] else ''
                st.caption(f"Gewählt: {choice + 1} · {o['label']} ({o['detail']}){hidden}")
        else:
            st.caption('Keine Avatare gefunden – bitte unten eine Grafik hochladen.' if analysis
                       else 'Nach der Website-Analyse stehen hier die gefundenen Avatare zur Auswahl.')
        up_avatar = st.file_uploader('Eigenen Avatar hochladen (überschreibt die Auswahl)', type=ALLOWED_TYPES,
                                     key='up_avatar', help='PNG, JPG, WEBP oder SVG. Transparente Bereiche erhalten '
                                     'automatisch einen gut lesbaren Hintergrund.')
        with st.expander('Pro Mockup einzeln hochladen (optional)'):
            up_left = st.file_uploader('Avatar Mockup 1 (GMX / WEB.DE / 1&1)', type=ALLOWED_TYPES, key='up_left')
            up_right = st.file_uploader('Avatar Mockup 3 (Darkmode / GMX Android)', type=ALLOWED_TYPES, key='up_right')
        st.radio('Darstellung im Kreis', ['Eingepasst', 'Füllend'], key='fit', horizontal=True,
                 help='Eingepasst: ganzes Logo sichtbar. Füllend: Kreis wird komplett ausgefüllt (Ränder werden beschnitten).')
        st.slider('Innenabstand Avatar (%)', 0, 30, key='pad', step=2)

    # ------------------------------------------------------------------ 4. Logo 5,5:1
    st.subheader('4. Logo 5,5 : 1 (Mockup 2)')
    with st.container(border=True):
        logos = st.session_state.logo_options
        if logos:
            st.caption('Breite Logos der Website, passend zugeschnitten. Anklicken zum Übernehmen.')
            tiles = [{'caption': f"{i + 1} · " + ('passt' if c['ratio'] >= LOGO_FIT_MIN else 'bedingt'),
                      'image': c['logo_thumb'], 'bg': logo_backdrop(cand_image(c, 'logo_png')),
                      'help': f"{c['label']} ({c['detail']}, Seitenverhältnis {c['ratio']:.1f} : 1)"}
                     for i, c in enumerate(logos)]
            tiles.append({'caption': 'Kein Logo', 'image': '', 'bg': '#FFFFFF', 'text': '–',
                          'help': 'Mockup 2 ohne Logo'})
            choice = st.session_state.logo_choice
            render_tiles('lpick', tiles, len(logos) if choice == -1 else choice, 4, pick_logo, 'logo')
            if 0 <= choice < len(logos):
                c = logos[choice]
                st.caption(f"Gewählt: {choice + 1} · {c['label']} ({c['detail']}) · Seitenverhältnis {c['ratio']:.1f} : 1"
                           + ('' if c['ratio'] >= LOGO_FIT_MIN else ' – eher kompakt, wirkt in 5,5 : 1 klein'))
        elif analysis:
            st.caption('Kein breites Logo gefunden – bitte unten ein eigenes Logo hochladen.')
        else:
            st.caption('Nach der Website-Analyse stehen hier die gefundenen Logos zur Auswahl.')
        if st.session_state.logo_hint and analysis:
            st.info(st.session_state.logo_hint)
        up_logo = st.file_uploader('Eigenes Logo hochladen (überschreibt die Auswahl)', type=ALLOWED_TYPES,
                                   key='up_logo', help='PNG, JPG, WEBP oder SVG – wird automatisch zugeschnitten '
                                   'und proportional in 5,5 : 1 eingepasst.')
        l1, l2 = st.columns(2)
        with l1:
            st.selectbox('Logo-Hintergrund', ['Transparent', 'Weiß', 'Dunkelgrau'], key='logo_bg',
                         help='Nur nötig, wenn ein helles/dunkles Logo auf dem Hintergrund des Mockups schlecht lesbar ist.')
        with l2:
            st.slider('Innenabstand Logo (px)', -5, 10, key='logo_pad',
                      help='Negative Werte vergrößern das Logo leicht über die Standardfläche hinaus.')

    with st.expander('Erweitert'):
        show_debug = st.checkbox('Platzhalter-Konturen anzeigen', value=False)


# ------------------------------------------------------------------ Grafiken zusammenführen
def uploaded_image(file, label):
    """Hochgeladene Datei -> Bild (SVG über cairosvg). Fehler werden gemeldet, nicht geworfen."""
    if file is None:
        return None
    try:
        return raw_to_image(file.getvalue())
    except Exception as error:
        errors.append(f'{label}: {error}')
        return None


def avatar_for(file):
    """(Bild, Hintergrund) für einen Avatar-Platz: einzelner Upload > Avatar-Upload > gewählter Vorschlag > Initialen."""
    image = uploaded_image(file, 'Avatar') if file is not None else None
    if image is None and file is None:
        image = uploaded_image(up_avatar, 'Avatar')
    if image is not None:
        return image, logo_backdrop(image)
    choice = st.session_state.avatar_choice
    options = st.session_state.avatar_options
    if 0 <= choice < len(options):
        return cand_image(options[choice]), options[choice]['bg']
    return None, AVATAR_DEFAULT


logo_image = None
uploaded_logo = uploaded_image(up_logo, 'Logo')
if uploaded_logo is not None:
    logo_image = trim_logo(uploaded_logo)
elif 0 <= st.session_state.logo_choice < len(st.session_state.logo_options):
    logo_image = cand_image(st.session_state.logo_options[st.session_state.logo_choice], 'logo_png')

avatars = {'links': avatar_for(up_left), 'rechts': avatar_for(up_right)}
opts = {'fit': 'cover' if st.session_state.fit == 'Füllend' else 'contain', 'pad': st.session_state.pad / 100,
        'logo_pad': st.session_state.logo_pad, 'logo_bg': st.session_state.logo_bg}
texts = {'sender': st.session_state.sender, 'subject': st.session_state.subject, 'preheader': st.session_state.preheader}
result = build_result(template, avatars, logo_image, opts, texts)

with left:
    for message in errors:
        st.error(message)

with right:
    st.markdown(STICKY_CSS, unsafe_allow_html=True)
    stretch(st.image, debug_overlay(result) if show_debug else result)
    brand_slug = re.sub(r'[^a-z0-9]+', '-', brand_key(st.session_state.sender) or 'prelive').strip('-') or 'prelive'
    b1, b2 = st.columns([3, 2])
    with b1:
        stretch(st.download_button, 'Alle 3 Mockups als PNG herunterladen', data=png_bytes(result),
                file_name=f'prelive-vorschau_{brand_slug}.png', mime='image/png', type='primary')
    with b2:
        if logo_image is not None:
            stretch(st.download_button, 'Logo 5,5 : 1 (PNG)',
                    data=png_bytes(render_logo(logo_image, LOGO_EXPORT, st.session_state.logo_pad * 8,
                                               st.session_state.logo_bg, ss=2)),
                    file_name=f'logo-5-5-zu-1_{brand_slug}.png', mime='image/png')
    st.caption('Avatare und Logo stammen aus der Website-Analyse – SVG, PNG, JPG und WEBP können jederzeit selbst hochgeladen werden. '
               f'Export: eine Grafik mit allen 3 Mockups ({template.width} × {template.height} px).')
