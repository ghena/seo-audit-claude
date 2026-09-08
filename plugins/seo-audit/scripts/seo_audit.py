import argparse
import csv
import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

LOCALE_FLAGS = {
    'it': '🇮🇹', 'en': '🇬🇧', 'fr': '🇫🇷', 'de': '🇩🇪', 'es': '🇪🇸', 'pt': '🇵🇹',
    'nl': '🇳🇱', 'ru': '🇷🇺', 'zh': '🇨🇳', 'ja': '🇯🇵', 'ar': '🇸🇦', 'pl': '🇵🇱',
}


def _collect_schema_types(data):
    """Estrae ricorsivamente i valori @type da un blocco JSON-LD (gestisce liste e @graph)."""
    types = []
    if isinstance(data, list):
        for item in data:
            types.extend(_collect_schema_types(item))
    elif isinstance(data, dict):
        if isinstance(data.get('@graph'), list):
            for item in data['@graph']:
                types.extend(_collect_schema_types(item))
        t = data.get('@type')
        if isinstance(t, list):
            types.extend(str(x) for x in t)
        elif isinstance(t, str):
            types.append(t)
    return types


def extract_schema_types(soup):
    """Ritorna (tipi_deduplicati, numero_blocchi_json_non_validi) dai tag ld+json della pagina."""
    types, errors = [], 0
    for script in soup.find_all('script', type='application/ld+json'):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            errors += 1
            continue
        types.extend(_collect_schema_types(data))
    seen = []
    for t in types:
        if t not in seen:
            seen.append(t)
    return seen, errors


def check_heading_hierarchy(soup):
    """Verifica la gerarchia H1-H6 secondo le best practice Google (nessun salto di livello in discesa)."""
    heading_tags = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    levels = [int(tag.name[1]) for tag in heading_tags]
    violations = []
    prev = 0
    for lvl in levels:
        if prev and lvl > prev + 1:
            violations.append(f'H{prev}→H{lvl}')
        prev = lvl
    sequence = ' > '.join(f'H{lvl}' for lvl in levels)
    return sequence, violations


class RateLimiter:
    """Token bucket semplice per rispettare max_requests_per_second."""
    def __init__(self, max_rps):
        self.max_rps = max_rps
        self.min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self.last_request = 0.0
        self.lock = __import__('threading').Lock()

    def wait(self):
        if self.max_rps <= 0:
            return
        with self.lock:
            now = time.time()
            elapsed = now - self.last_request
            if elapsed < self.min_interval:
                sleep = self.min_interval - elapsed
                time.sleep(sleep)
                self.last_request = time.time()
            else:
                self.last_request = now


def write_progress(path, phase, current, total, extra=None):
    """Scrive lo stato di avanzamento in un file JSON in modo atomico
    (write su file temporaneo + rename), cosi' un processo esterno che
    legge il file non trova mai un JSON a meta' scrittura."""
    if not path:
        return
    try:
        percent = round(100.0 * current / total, 1) if total else 0.0
        payload = {
            'phase': phase,
            'current': current,
            'total': total,
            'percent': percent,
            'updated_at': datetime.utcnow().isoformat() + 'Z',
        }
        if extra:
            payload.update(extra)
        tmp_path = f'{path}.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)
    except Exception:
        pass  # il progress-tracking non deve mai far fallire l'audit


class SEOCrawler:
    def __init__(self, start_url, max_pages=500, concurrency=1, max_rps=1.0, respect_robots=True, user_agent=None, max_depth=None, progress_file=None):
        self.start_url = start_url.rstrip('/')
        self.domain = urlparse(self.start_url).netloc
        self.scheme = urlparse(self.start_url).scheme
        self.max_pages = max_pages
        self.concurrency = max(1, concurrency)
        self.max_rps = max_rps
        self.respect_robots = respect_robots
        self.max_depth = max_depth  # None = nessun limite di profondita'
        self.progress_file = progress_file
        self.visited = set()
        self.to_visit = [(self.start_url, 0)]
        self.url_depth = {self.start_url: 0}
        self.results = []
        self.session = requests.Session()
        ua = user_agent or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
        self.session.headers.update({
            'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'close',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        })
        self.rate_limiter = RateLimiter(max_rps)
        self.lock = __import__('threading').Lock()

        self.robots_parser = None
        self.robots_rules = []
        robots_url = f"{self.scheme}://{self.domain}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=15)
            if resp.status_code == 200:
                self.robots_parser = True  # marker che abbiamo letto robots.txt
                self.robots_rules = resp.text.splitlines()
        except requests.RequestException as e:
            print(f"[WARN] Impossibile leggere robots.txt: {e}")

    def normalize_url(self, url):
        url = url.strip()
        url, _ = urldefrag(url)
        parsed = urlparse(url)
        if not parsed.scheme:
            parsed = parsed._replace(scheme=self.scheme)
        if not parsed.netloc:
            parsed = parsed._replace(netloc=self.domain)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}{('?' + parsed.query) if parsed.query else ''}"

    def is_internal(self, url):
        parsed = urlparse(url)
        return parsed.netloc == self.domain or parsed.netloc == ''

    def can_fetch(self, url):
        if not self.respect_robots or not self.robots_parser:
            return True
        # Implementazione minimale delle regole robots.txt
        parsed_url = urlparse(url)
        path = parsed_url.path
        user_agent_matched = False
        for line in self.robots_rules:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if ':' not in line:
                continue
            key, _, value = line.partition(':')
            key = key.strip().lower()
            value = value.strip()
            if key == 'user-agent':
                user_agent_matched = value == '*'
            elif key == 'disallow' and user_agent_matched and value:
                if path.startswith(value):
                    return False
        return True

    def fetch(self, url):
        self.rate_limiter.wait()
        try:
            start = time.time()
            resp = self.session.get(url, timeout=20, allow_redirects=True)
            elapsed_total = round((time.time() - start) * 1000, 2)
            ttfb = getattr(resp, 'elapsed', None)
            ttfb_ms = round(ttfb.total_seconds() * 1000, 2) if ttfb else ''
            return resp, elapsed_total, ttfb_ms
        except requests.RequestException as e:
            return None, str(e), ''

    def extract_links(self, soup, base_url):
        internal, external = [], []
        for tag in soup.find_all('a', href=True):
            href = tag['href']
            full = urljoin(base_url, href)
            full = self.normalize_url(full)
            if self.is_internal(full):
                internal.append(full)
            else:
                external.append(full)
        return internal, external

    def analyze_page(self, url, resp, elapsed_total, ttfb_ms):
        base = {
            'url': url, 'status_code': 'ERROR', 'response_time_ms': '', 'ttfb_ms': '',
            'title': '', 'title_length': '', 'meta_description': '', 'meta_description_length': '',
            'canonical': '', 'canonical_self': False, 'canonical_external': False,
            'hreflang_count': 0, 'hreflang_has_x_default': False, 'hreflang_languages': '',
            'h1': '', 'h1_count': 0, 'meta_robots': '',
            'og_title': '', 'og_description': '', 'internal_links': 0, 'external_links': 0,
            'missing_alt': 0, 'has_schema': False, 'sitemap_declared': False, 'crawled': True,
            'locale': 'n-d', 'schema_types': '', 'schema_json_errors': 0,
            'heading_sequence': '', 'heading_hierarchy_ok': True, 'heading_violations': '',
        }
        if resp is None:
            base['response_time_ms'] = elapsed_total
            return base

        base['status_code'] = resp.status_code
        base['response_time_ms'] = elapsed_total
        base['ttfb_ms'] = ttfb_ms

        if resp.status_code != 200 or 'text/html' not in resp.headers.get('Content-Type', '').lower():
            return base

        soup = BeautifulSoup(resp.text, 'lxml')
        title_tag = soup.find('title')
        title = title_tag.get_text(strip=True) if title_tag else ''
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        description = meta_desc.get('content', '').strip() if meta_desc else ''
        canonical_tag = soup.find('link', attrs={'rel': 'canonical'})
        canonical = canonical_tag.get('href', '').strip() if canonical_tag else ''
        canonical_normalized = self.normalize_url(canonical) if canonical else ''
        current_url = self.normalize_url(resp.url)
        canonical_self = bool(canonical) and canonical_normalized == current_url
        canonical_external = bool(canonical) and not canonical_self

        hreflang_links = soup.find_all('link', attrs={'rel': 'alternate', 'hreflang': True})
        hreflang_langs = [tag.get('hreflang', '').lower() for tag in hreflang_links if tag.get('hreflang')]
        hreflang_has_x_default = 'x-default' in hreflang_langs
        internal, external = self.extract_links(soup, resp.url)
        h1_tags = soup.find_all('h1')
        h1_text = h1_tags[0].get_text(strip=True) if h1_tags else ''
        robots_tag = soup.find('meta', attrs={'name': 'robots'})
        robots = robots_tag.get('content', '').strip() if robots_tag else ''
        og_title = soup.find('meta', property='og:title')
        og_description = soup.find('meta', property='og:description')
        missing_alt = len([img for img in soup.find_all('img') if not img.get('alt')])
        has_schema = bool(soup.find('script', type='application/ld+json'))
        schema_types, schema_json_errors = extract_schema_types(soup)
        heading_sequence, heading_violations = check_heading_hierarchy(soup)

        raw_locale = ''
        if soup.html and soup.html.get('lang'):
            raw_locale = soup.html.get('lang').strip()
        if not raw_locale:
            og_locale_tag = soup.find('meta', property='og:locale')
            if og_locale_tag and og_locale_tag.get('content'):
                raw_locale = og_locale_tag.get('content').strip()
        locale = raw_locale.replace('_', '-').split('-')[0].lower() if raw_locale else 'altro'

        base.update({
            'url': resp.url, 'title': title, 'title_length': len(title),
            'meta_description': description, 'meta_description_length': len(description),
            'canonical': canonical, 'canonical_self': canonical_self, 'canonical_external': canonical_external,
            'hreflang_count': len(hreflang_links), 'hreflang_has_x_default': hreflang_has_x_default,
            'hreflang_languages': ', '.join(hreflang_langs),
            'h1': h1_text, 'h1_count': len(h1_tags),
            'meta_robots': robots,
            'og_title': og_title.get('content', '').strip() if og_title else '',
            'og_description': og_description.get('content', '').strip() if og_description else '',
            'internal_links': len(internal), 'external_links': len(external),
            'missing_alt': missing_alt, 'has_schema': has_schema, 'locale': locale,
            'schema_types': ', '.join(schema_types), 'schema_json_errors': schema_json_errors,
            'heading_sequence': heading_sequence, 'heading_hierarchy_ok': len(heading_violations) == 0,
            'heading_violations': '; '.join(heading_violations), '_links': internal,
        })
        return base

    def _process_url(self, url_depth):
        url, depth = url_depth
        url = self.normalize_url(url)
        if not self.can_fetch(url):
            return None, depth
        resp, elapsed_total, ttfb_ms = self.fetch(url)
        result = self.analyze_page(url, resp, elapsed_total, ttfb_ms)
        result['depth'] = depth
        return result, depth

    def crawl(self):
        results = []
        pbar = tqdm(total=self.max_pages, desc='Crawling')

        while self.to_visit and len(results) < self.max_pages:
            # Prepara un batch di URL da processare in parallelo
            batch = []
            while self.to_visit and len(batch) < self.concurrency and len(results) + len(batch) < self.max_pages:
                url, depth = self.to_visit.pop(0)
                url = self.normalize_url(url)
                if url in self.visited:
                    continue
                self.visited.add(url)
                batch.append((url, depth))

            if not batch:
                break

            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {executor.submit(self._process_url, item): item for item in batch}
                for future in as_completed(futures):
                    result, depth = future.result()
                    if result is None:
                        continue
                    results.append(result)
                    pbar.update(1)
                    write_progress(
                        self.progress_file, 'crawl', len(results), self.max_pages,
                        extra={'last_url': result.get('url'), 'depth': depth}
                    )
                    next_depth = depth + 1
                    if self.max_depth is not None and next_depth > self.max_depth:
                        continue  # non accodare link oltre la profondita' massima
                    for link in result.pop('_links', []):
                        if link not in self.visited and link not in self.url_depth:
                            self.url_depth[link] = next_depth
                            self.to_visit.append((link, next_depth))

            # Se il batch non ha riempito, i link aggiunti saranno processati al prossimo giro

        pbar.close()
        self.results = results
        return results


class SitemapAnalyzer:
    def __init__(self, domain, session=None, scheme='https'):
        self.domain = domain
        self.scheme = scheme
        self.session = session or requests.Session()
        self.session.headers.update({'User-Agent': 'SEOAuditorBot/1.0'})
        self.sitemap_urls = []
        self.sitemap_files = []

    def _fetch_text(self, url):
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.text
        except requests.RequestException:
            pass
        return None

    def discover_from_robots(self):
        """Legge Sitemap: dal robots.txt."""
        sitemaps = []
        try:
            resp = self.session.get(f'{self.scheme}://{self.domain}/robots.txt', timeout=15)
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line.lower().startswith('sitemap:'):
                        url = line.split(':', 1)[1].strip()
                        sitemaps.append(url)
        except requests.RequestException:
            pass
        return sitemaps

    def discover_sitemaps(self):
        candidates = set(self.discover_from_robots())
        candidates.update([
            f"{self.scheme}://{self.domain}/sitemap.xml",
            f"{self.scheme}://{self.domain}/sitemap_index.xml",
            f"{self.scheme}://{self.domain}/sitemapindex.xml",
        ])
        found = []
        self.sitemap_files = []
        for candidate in candidates:
            text = self._fetch_text(candidate)
            if text:
                found.append((candidate, text))
                self.sitemap_files.append(candidate)
        return found

    def parse_sitemap(self, text, source_url=None):
        urls = []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return urls
        nsmap = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

        def find_tag(element, tag_name):
            for candidate in [f'{{{nsmap["sm"]}}}{tag_name}', tag_name]:
                found = element.find(candidate)
                if found is not None:
                    return found
            return None

        for sitemap in root.findall(f'{{{nsmap["sm"]}}}sitemap') or root.findall('sitemap'):
            loc = find_tag(sitemap, 'loc')
            if loc is not None and loc.text:
                sub_url = loc.text.strip()
                if sub_url not in self.sitemap_files:
                    self.sitemap_files.append(sub_url)
                try:
                    sub_text = self._fetch_text(sub_url)
                    if sub_text:
                        urls.extend(self.parse_sitemap(sub_text, source_url=sub_url))
                except requests.RequestException:
                    continue
        for url in root.findall(f'{{{nsmap["sm"]}}}url') or root.findall('url'):
            loc = find_tag(url, 'loc')
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
        return urls

    def analyze(self):
        found = self.discover_sitemaps()
        all_urls = []
        for _, text in found:
            all_urls.extend(self.parse_sitemap(text))
        self.sitemap_urls = list(set(all_urls))
        return self.sitemap_urls


def find_lighthouse_binary(explicit_path=None):
    """Cerca il binario lighthouse nativo, evitando di raccogliere per errore
    un binario Windows esposto via interop di WSL (es. /mnt/c/.../lighthouse,
    che ha spazi nel path tipo "Program Files" e non e' eseguibile come
    processo Linux nativo).

    Ordine di ricerca:
    0. Path esplicito passato (--lighthouse-bin), tipicamente il node_modules
       locale del plugin (${CLAUDE_PLUGIN_ROOT}/node_modules/.bin/lighthouse)
       installato automaticamente da Claude Code come dipendenza npm
       dichiarata - ha sempre priorita' assoluta se valido.
    1. Variabile d'ambiente SEO_AUDIT_LIGHTHOUSE_BIN.
    2. Le directory di PATH che NON iniziano per /mnt/ (esclude i mount
       Windows di WSL).
    3. Fallback: shutil.which() su tutto il PATH, incluso /mnt/ (ultima
       risorsa, con avviso che potrebbe non funzionare sotto WSL).
    """
    if explicit_path and os.path.isfile(explicit_path) and os.access(explicit_path, os.X_OK):
        return explicit_path, None

    env_bin = os.environ.get('SEO_AUDIT_LIGHTHOUSE_BIN')
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin, None

    path_dirs = os.environ.get('PATH', '').split(os.pathsep)
    native_dirs = [d for d in path_dirs if not d.startswith('/mnt/')]
    for d in native_dirs:
        candidate = os.path.join(d, 'lighthouse')
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate, None

    fallback = shutil.which('lighthouse')
    if fallback:
        warning = None
        if fallback.startswith('/mnt/'):
            warning = (
                f"Trovato solo un binario lighthouse sotto un mount Windows "
                f"({fallback}), che su WSL spesso non funziona correttamente "
                f"con path contenenti spazi. Installane uno nativo Linux "
                f"(vedi setup del plugin)."
            )
        return fallback, warning

    return None, None


class LighthouseRunner:
    def __init__(self, sample_size=5, categories=None, progress_file=None, lighthouse_bin=None):
        self.sample_size = sample_size
        self.categories = categories or ['performance', 'accessibility', 'best-practices', 'seo']
        self.progress_file = progress_file
        self.lighthouse_bin = lighthouse_bin

    def sample_urls(self, urls):
        if not urls:
            return []
        return random.sample(urls, min(self.sample_size, len(urls)))

    def run(self, urls, form_factor='mobile'):
        if not urls:
            return []
        reports = []
        lighthouse_cmd, warning = find_lighthouse_binary(self.lighthouse_bin)
        if warning:
            print(f"[WARN] {warning}")
        if not lighthouse_cmd:
            return [{'url': u, 'error': 'lighthouse executable not found in PATH', 'form_factor': form_factor} for u in urls]
        total = len(urls)
        for i, url in enumerate(tqdm(urls, desc=f'Lighthouse {form_factor}')):
            write_progress(
                self.progress_file, f'lighthouse_{form_factor}', i, total,
                extra={'current_url': url}
            )
            cmd = [
                lighthouse_cmd, url,
                '--output=json',
                '--chrome-flags=--headless --no-sandbox --disable-gpu',
                f'--form-factor={form_factor}',
            ]
            if form_factor == 'desktop':
                cmd.append('--preset=desktop')
            for cat in self.categories:
                cmd.append(f'--only-categories={cat}')
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, shell=False)
                if proc.returncode != 0:
                    reports.append({'url': url, 'form_factor': form_factor, 'error': proc.stderr[:500]})
                    continue
                output = json.loads(proc.stdout)
                scores = {}
                for cat in self.categories:
                    scores[cat] = round(output['categories'][cat]['score'] * 100, 1)
                metrics = {
                    'lcp': output.get('audits', {}).get('largest-contentful-paint', {}).get('numericValue'),
                    'fcp': output.get('audits', {}).get('first-contentful-paint', {}).get('numericValue'),
                    'tbt': output.get('audits', {}).get('total-blocking-time', {}).get('numericValue'),
                    'cls': output.get('audits', {}).get('cumulative-layout-shift', {}).get('numericValue'),
                }
                reports.append({'url': url, 'form_factor': form_factor, 'scores': scores, 'metrics': metrics})
            except Exception as e:
                reports.append({'url': url, 'form_factor': form_factor, 'error': str(e)})
        write_progress(self.progress_file, f'lighthouse_{form_factor}', total, total)
        return reports


class HTMLReport:
    def __init__(self, results, sitemap_urls, sitemap_files, crawled_urls, internal_links_map, lighthouse_reports_mobile, lighthouse_reports_desktop, domain, concurrency, max_rps):
        self.results = results
        self.sitemap_urls = set(sitemap_urls)
        self.sitemap_files = sitemap_files
        self.crawled_urls = set(crawled_urls)
        self.internal_links_map = internal_links_map
        self.lighthouse_reports_mobile = lighthouse_reports_mobile
        self.lighthouse_reports_desktop = lighthouse_reports_desktop
        self.domain = domain
        self.concurrency = concurrency
        self.max_rps = max_rps

    def _count(self, predicate):
        return sum(1 for r in self.results if predicate(r))

    def _avg(self, key):
        vals = [r[key] for r in self.results if isinstance(r[key], (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else 0

    def _lighthouse_avg(self, reports, category):
        vals = [r['scores'][category] for r in reports if 'error' not in r and category in r.get('scores', {})]
        return round(sum(vals) / len(vals), 1) if vals else None

    def _issue_class(self, count):
        return 'good' if count == 0 else 'bad'

    def _issues(self, r):
        issues = []
        status_ok = r['status_code'] == 200
        if r['status_code'] != 200:
            issues.append(f"Status {r['status_code']}")
        # Per pagine non-200, on-page SEO non e' rilevante
        if status_ok:
            if not r['title']:
                issues.append('Title mancante')
            elif r['title_length'] < 30:
                issues.append('Title troppo corto')
            elif r['title_length'] > 60:
                issues.append('Title troppo lungo')
            if not r['meta_description']:
                issues.append('Meta description mancante')
            elif r['meta_description_length'] < 120:
                issues.append('Meta description troppo corta')
            elif r['meta_description_length'] > 160:
                issues.append('Meta description troppo lunga')
            if not r['h1']:
                issues.append('H1 mancante')
            if r['h1_count'] > 1:
                issues.append('Più di un H1')
            if r['missing_alt'] > 0:
                issues.append(f"{r['missing_alt']} img senza alt")
            # Canonical checks
            if not r['canonical']:
                issues.append('Canonical mancante')
            elif r['canonical_external']:
                issues.append(f"Canonical esterno: {r['canonical']}")
            # Hreflang checks
            if r['hreflang_count'] > 0 and not r['hreflang_has_x_default']:
                issues.append('Hreflang senza x-default')
            # Dati strutturati
            if not r['has_schema']:
                issues.append('Dati strutturati mancanti')
            elif r.get('schema_json_errors'):
                issues.append(f"{r['schema_json_errors']} blocco/i JSON-LD non validi")
            # Gerarchia heading
            if not r.get('heading_hierarchy_ok', True):
                issues.append(f"Gerarchia heading non rispettata ({r.get('heading_violations', '')})")
        if r['sitemap_declared'] is False:
            issues.append('Non in sitemap')
        return issues

    def _score_class(self, score):
        if score is None or score == '-' or score == '':
            return ''
        try:
            val = float(score)
        except (ValueError, TypeError):
            return ''
        if val < 50:
            return 'bad'
        if val < 90:
            return 'warn'
        return 'good'

    def _metric_class(self, key, value):
        if value is None or value == '-' or value == '':
            return ''
        try:
            val = float(value)
        except (ValueError, TypeError):
            return ''
        if key in ('lcp', 'fcp'):
            return 'bad' if val > 2500 else 'warn' if val > 1800 else 'good'
        if key == 'tbt':
            return 'bad' if val > 600 else 'warn' if val > 300 else 'good'
        if key == 'cls':
            return 'bad' if val > 0.25 else 'warn' if val > 0.1 else 'good'
        return ''

    def _render_lighthouse_table(self, reports):
        rows = []
        for rep in reports:
            if 'error' in rep:
                rows.append(f"<tr><td>{rep['url']}</td><td colspan='8' class='bad'>Errore: {rep['error']}</td></tr>")
                continue
            scores = rep['scores']
            m = rep['metrics']
            perf = scores.get('performance', '-')
            acc = scores.get('accessibility', '-')
            bp = scores.get('best-practices', '-')
            seo = scores.get('seo', '-')
            lcp = m.get('lcp', '-')
            fcp = m.get('fcp', '-')
            tbt = m.get('tbt', '-')
            cls = m.get('cls', '-')
            rows.append(f"""
                <tr>
                    <td><a href="{rep['url']}" target="_blank">{rep['url'][:60]}{'...' if len(rep['url'])>60 else ''}</a></td>
                    <td class="{self._score_class(perf)}">{perf}</td>
                    <td class="{self._score_class(acc)}">{acc}</td>
                    <td class="{self._score_class(bp)}">{bp}</td>
                    <td class="{self._score_class(seo)}">{seo}</td>
                    <td class="{self._metric_class('lcp', lcp)}">{lcp}</td>
                    <td class="{self._metric_class('fcp', fcp)}">{fcp}</td>
                    <td class="{self._metric_class('tbt', tbt)}">{tbt}</td>
                    <td class="{self._metric_class('cls', cls)}">{cls}</td>
                </tr>
            """)
        return ''.join(rows) if rows else '<tr><td colspan="9">Nessun report Lighthouse generato</td></tr>'

    def _render_schema_tab(self):
        type_counts = {}
        no_schema_count = 0
        rows = []
        for r in self.results:
            if r['status_code'] != 200:
                continue
            types = [t.strip() for t in r['schema_types'].split(',') if t.strip()]
            if not types:
                no_schema_count += 1
            for t in types:
                type_counts[t] = type_counts.get(t, 0) + 1
            badges = ''.join(f'<span class="schema-badge">{t}</span>' for t in types) if types else '<span class="warn">Nessuno</span>'
            error_cell = f'<span class="bad">{r["schema_json_errors"]} non validi</span>' if r.get('schema_json_errors') else ''
            rows.append(f"""
                <tr>
                    <td><a href="{r['url']}" target="_blank">{r['url'][:80]}{'...' if len(r['url'])>80 else ''}</a></td>
                    <td>{badges}</td>
                    <td>{error_cell}</td>
                </tr>
            """)
        type_cards = ''.join(
            f'<div class="card"><div class="value">{count}</div><div class="label">{t}</div></div>'
            for t, count in sorted(type_counts.items(), key=lambda kv: -kv[1])
        )
        type_cards += (
            f'<div class="card {self._issue_class(no_schema_count)}">'
            f'<div class="value">{no_schema_count}</div><div class="label">Senza dati strutturati</div></div>'
        )
        rows_html = ''.join(rows) if rows else '<tr><td colspan="3">Nessuna pagina 200 analizzata</td></tr>'
        return type_cards, rows_html

    def _render_sitemap_detail(self, missing_in_crawl, missing_in_sitemap):
        html_parts = []
        if self.sitemap_files:
            html_parts.append('<h3>📁 File sitemap trovati</h3>')
            html_parts.append('<ul>')
            for u in sorted(self.sitemap_files):
                html_parts.append(f'<li><a href="{u}" target="_blank">{u}</a></li>')
            html_parts.append('</ul>')
        if missing_in_crawl:
            html_parts.append('<h3>🔗 Orphan in sitemap (non linkati internamente)</h3>')
            html_parts.append('<ul>')
            for u in sorted(missing_in_crawl):
                html_parts.append(f'<li><a href="{u}" target="_blank">{u}</a></li>')
            html_parts.append('</ul>')
        if missing_in_sitemap:
            html_parts.append('<h3>🚫 URL crawllati non in sitemap</h3>')
            html_parts.append('<ul>')
            for u in sorted(missing_in_sitemap):
                html_parts.append(f'<li><a href="{u}" target="_blank">{u}</a></li>')
            html_parts.append('</ul>')
        return ''.join(html_parts) if html_parts else '<p>Nessun problema di sitemap rilevato.</p>'

    def _render_rows(self, results_subset, heading_only=False, show_canonical=True, show_sitemap=True, show_locale=True):
        rows = []
        for r in results_subset:
            if heading_only:
                issues = [] if r.get('heading_hierarchy_ok', True) else [f"Gerarchia heading non rispettata ({r.get('heading_violations', '')})"]
            else:
                issues = self._issues(r)
            issue_html = ', '.join(f'<span class="bad">{i}</span>' for i in issues) if issues else '<span class="ok">OK</span>'
            status_ok = r['status_code'] == 200
            if status_ok:
                canonical_title = (r['canonical'] or 'Mancante').replace('"', '&quot;')
                if r['canonical_self']:
                    canonical_cell = f'<span class="status-badge status-ok" title="{canonical_title}">✅ OK</span>'
                else:
                    canonical_cell = f'<span class="status-badge status-ko" title="{canonical_title}">❌ KO</span>'
                hreflang_cell = f"{r['hreflang_count']} ({r['hreflang_languages'][:40]})"
                if r['hreflang_count'] > 0 and not r['hreflang_has_x_default']:
                    hreflang_cell = f'<span class="bad">{hreflang_cell} — x-default mancante</span>'
                elif r['hreflang_count'] > 0:
                    hreflang_cell = f'<span class="good">{hreflang_cell}</span>'
                else:
                    hreflang_cell = '-'
                title_len = r['title_length']
                desc_len = r['meta_description_length']
                h1_count = r['h1_count']
                missing_alt = r['missing_alt']
            else:
                canonical_cell = '<span class="warn">N/D</span>'
                hreflang_cell = '<span class="warn">N/D</span>'
                title_len = '<span class="warn">N/D</span>'
                desc_len = '<span class="warn">N/D</span>'
                h1_count = '<span class="warn">N/D</span>'
                missing_alt = '<span class="warn">N/D</span>'
            status_badge = (
                '<span class="status-badge status-ok">✅ OK</span>' if status_ok
                else '<span class="status-badge status-ko">❌ KO</span>'
            )
            locale_code = r.get('locale') or 'altro'
            locale_flag = LOCALE_FLAGS.get(locale_code, '🌐')
            locale_label = locale_code.upper() if locale_code not in ('altro', 'n-d') else locale_code.capitalize()
            locale_badge = f'<span class="locale-badge">{locale_flag} {locale_label}</span>'
            row_class = ' row-issue' if not r.get('heading_hierarchy_ok', True) else ''
            cells = [
                f'<td>{status_badge}</td>',
                f'<td><a href="{r["url"]}" target="_blank">{r["url"][:80]}{"..." if len(r["url"])>80 else ""}</a></td>',
                f'<td>{r["status_code"]}</td>',
            ]
            if show_locale:
                cells.append(f'<td>{locale_badge}</td>')
            cells.append(f'<td>{r["response_time_ms"]}</td>')
            cells.append(f'<td>{r["ttfb_ms"]}</td>')
            cells.append(f'<td>{title_len}</td>')
            cells.append(f'<td>{desc_len}</td>')
            if show_canonical:
                cells.append(f'<td>{canonical_cell}</td>')
            cells.append(f'<td>{hreflang_cell}</td>')
            cells.append(f'<td>{h1_count}</td>')
            cells.append(f'<td>{missing_alt}</td>')
            if show_sitemap:
                cells.append(f'<td>{"Sì" if r["sitemap_declared"] else "No"}</td>')
            cells.append(f'<td>{issue_html}</td>')
            rows.append(f'<tr data-locale="{locale_code}" class="{row_class.strip()}">' + ''.join(cells) + '</tr>')
        return ''.join(rows)

    def render(self, output_path='seo_report.html'):
        total = len(self.results)
        ok_200 = self._count(lambda r: r['status_code'] == 200)
        status_404 = self._count(lambda r: r['status_code'] == 404)
        status_error = self._count(lambda r: r['status_code'] not in (200, 404))
        missing_title = self._count(lambda r: r['status_code'] == 200 and not r['title'])
        missing_desc = self._count(lambda r: r['status_code'] == 200 and not r['meta_description'])
        missing_h1 = self._count(lambda r: r['status_code'] == 200 and not r['h1'])
        multiple_h1 = self._count(lambda r: r['status_code'] == 200 and r['h1_count'] > 1)
        missing_alt = self._count(lambda r: r['status_code'] == 200 and r['missing_alt'] > 0)
        missing_schema = self._count(lambda r: r['status_code'] == 200 and not r['has_schema'])
        bad_heading = self._count(lambda r: r['status_code'] == 200 and not r.get('heading_hierarchy_ok', True))
        missing_sitemap = self._count(lambda r: not r['sitemap_declared'])
        avg_response = self._avg('response_time_ms')
        avg_ttfb = self._avg('ttfb_ms')
        missing_in_crawl = self.sitemap_urls - self.crawled_urls
        missing_in_sitemap = self.crawled_urls - self.sitemap_urls

        has_lighthouse = bool(self.lighthouse_reports_mobile) or bool(self.lighthouse_reports_desktop)
        lh_perf_mobile = self._lighthouse_avg(self.lighthouse_reports_mobile, 'performance')
        lh_perf_desktop = self._lighthouse_avg(self.lighthouse_reports_desktop, 'performance')
        lh_acc_mobile = self._lighthouse_avg(self.lighthouse_reports_mobile, 'accessibility')
        lh_bp_mobile = self._lighthouse_avg(self.lighthouse_reports_mobile, 'best-practices')
        lh_seo_mobile = self._lighthouse_avg(self.lighthouse_reports_mobile, 'seo')

        def lh_card(label, value):
            cls = self._score_class(value)
            display = value if value is not None else '-'
            return f'<div class="card {cls}"><div class="value">{display}</div><div class="label">{label}</div></div>'

        lighthouse_summary_html = ''
        if has_lighthouse:
            lighthouse_summary_html = f"""
<div class="summary-group">
<h3 class="summary-group-title">⚡ Performance (Lighthouse)</h3>
<div class="summary">
    {lh_card('Performance Mobile', lh_perf_mobile)}
    {lh_card('Performance Desktop', lh_perf_desktop)}
    {lh_card('Accessibility', lh_acc_mobile)}
    {lh_card('Best Practices', lh_bp_mobile)}
    {lh_card('SEO Score', lh_seo_mobile)}
</div>
</div>
"""

        results_200 = [r for r in self.results if r['status_code'] == 200]
        results_404 = [r for r in self.results if r['status_code'] == 404]
        results_other = [r for r in self.results if r['status_code'] not in (200, 404)]

        locale_counts = {}
        for r in self.results:
            loc = r.get('locale') or 'altro'
            locale_counts[loc] = locale_counts.get(loc, 0) + 1
        locale_order = sorted(locale_counts, key=lambda l: (l in ('altro', 'n-d'), l))
        locale_buttons = ''.join(
            f'<button onclick="filterLocale(\'{loc}\', this)">'
            f'{LOCALE_FLAGS.get(loc, "🌐")} {loc.upper() if loc not in ("altro", "n-d") else loc.capitalize()} ({locale_counts[loc]})</button>'
            for loc in locale_order
        )

        rows_all = self._render_rows(self.results, show_sitemap=False, show_locale=False)
        rows_200 = self._render_rows(results_200, show_sitemap=False, show_locale=False)
        rows_404 = self._render_rows(results_404, show_sitemap=False, show_locale=False)
        rows_other = self._render_rows(results_other, show_sitemap=False, show_locale=False)

        status_tab_defs = [('tab-all', 'Tutti', total, rows_all)]
        if ok_200 > 0:
            status_tab_defs.append(('tab-200', '200 OK', ok_200, rows_200))
        if status_404 > 0:
            status_tab_defs.append(('tab-404', '404', status_404, rows_404))
        if status_error > 0:
            status_tab_defs.append(('tab-other', 'Altri', status_error, rows_other))

        status_tab_buttons = ''.join(
            f'<button class="{"active" if i == 0 else ""}" onclick="showTab(\'{tab_id}\', this)">{label} ({count})</button>'
            for i, (tab_id, label, count, _) in enumerate(status_tab_defs)
        )
        url_table_header = (
            '<tr><th>Stato</th><th>URL</th><th>Status</th>'
            '<th title="Tempo di risposta totale in millisecondi">Resp (ms)</th>'
            '<th title="Time To First Byte in millisecondi">TTFB (ms)</th>'
            '<th title="Lunghezza del tag title in caratteri">Title</th>'
            '<th title="Lunghezza della meta description in caratteri">Meta desc</th>'
            '<th title="Presenza e correttezza del tag canonical">Canonical</th>'
            '<th>Hreflang</th>'
            '<th title="Numero di tag H1 nella pagina">H1</th>'
            '<th title="Numero di immagini senza attributo alt">Alt</th>'
            '<th>Problemi</th></tr>'
        )
        tag_table_header = (
            '<tr><th>Stato</th><th>URL</th><th>Status</th><th>Response (ms)</th>'
            '<th>TTFB (ms)</th><th>Title len</th><th>Meta desc len</th><th>Hreflang</th>'
            '<th>H1 count</th><th>Alt mancanti</th><th>In sitemap</th><th>Problemi</th></tr>'
        )
        status_tab_panels = ''.join(f"""
<div id="{tab_id}" class="tab{' active' if i == 0 else ''}">
<div class="table-scroll">
<table class="url-table">
<thead>
{url_table_header}
</thead>
<tbody>
{rows}
</tbody>
</table>
</div>
</div>
""" for i, (tab_id, label, count, rows) in enumerate(status_tab_defs))

        heading_issue_results = [r for r in self.results if not r.get('heading_hierarchy_ok', True)]
        rows_heading_issues = self._render_rows(heading_issue_results, heading_only=True, show_canonical=False, show_locale=False)
        rows_all_heading = self._render_rows(self.results, heading_only=True, show_canonical=False, show_locale=False)
        tag_tab_defs = []
        if heading_issue_results:
            tag_tab_defs.append(('tag-issues', '⚠️ Con problemi', len(heading_issue_results), rows_heading_issues))
        tag_tab_defs.append(('tag-all', 'Tutti', total, rows_all_heading))
        tag_tab_buttons = ''.join(
            f'<button class="{"active" if i == 0 else ""}" onclick="showTab(\'{tab_id}\', this)">{label} ({count})</button>'
            for i, (tab_id, label, count, _) in enumerate(tag_tab_defs)
        )
        tag_tab_panels = ''.join(f"""
<div id="{tab_id}" class="tab{' active' if i == 0 else ''}">
<div class="table-scroll">
<table class="url-table">
<thead>
{tag_table_header}
</thead>
<tbody>
{rows}
</tbody>
</table>
</div>
</div>
""" for i, (tab_id, label, count, rows) in enumerate(tag_tab_defs))

        lighthouse_table = self._render_lighthouse_table(self.lighthouse_reports_mobile)
        lighthouse_table_desktop = self._render_lighthouse_table(self.lighthouse_reports_desktop)

        schema_type_cards, schema_rows = self._render_schema_tab()

        html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<title>SEO Audit - {self.domain}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2rem; background:#0f172a; color:#e2e8f0; }}
h1, h2 {{ color:#38bdf8; }}
.summary {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(160px,220px)); gap:1rem; margin:1.5rem 0; }}
.card {{ background:#1e293b; padding:1rem; border-radius:8px; text-align:center; border:1px solid transparent; }}
.card .value {{ font-size:2rem; font-weight:bold; color:#38bdf8; }}
.card .label {{ font-size:.85rem; color:#94a3b8; text-transform:uppercase; }}
.card.good {{ background:rgba(74,222,128,.1); border-color:rgba(74,222,128,.3); }}
.card.good .value {{ color:#4ade80; }}
.card.warn {{ background:rgba(251,191,36,.1); border-color:rgba(251,191,36,.3); }}
.card.warn .value {{ color:#fbbf24; }}
.card.bad {{ background:rgba(248,113,113,.1); border-color:rgba(248,113,113,.3); }}
.card.bad .value {{ color:#f87171; }}
.summary-group {{ margin:1.5rem 0; }}
.summary-group-title {{ color:#94a3b8; font-size:.9rem; text-transform:uppercase; letter-spacing:.05em; margin:0 0 .75rem; }}
.summary-group .summary {{ margin:0; }}
table {{ width:100%; border-collapse:collapse; margin:1rem 0; font-size:.9rem; background:#1e293b; border-radius:8px; overflow:hidden; }}
th, td {{ padding:.65rem .8rem; text-align:left; border-bottom:1px solid #334155; }}
th {{ background:#0f172a; color:#38bdf8; }}
tr:hover {{ background:#334155; }}
.ok {{ color:#4ade80; }}
.issue {{ color:#f87171; }}
.bad {{ color:#f87171; font-weight:bold; }}
.warn {{ color:#fbbf24; }}
.good {{ color:#4ade80; }}
a {{ color:#38bdf8; }}
.nd {{ color:#94a3b8; font-style:italic; }}

.tab-buttons {{ display:flex; gap:.25rem; margin:1rem 0 0; border-bottom:2px solid #334155; }}
.tab-buttons button {{ position:relative; top:2px; background:transparent; color:#94a3b8; border:1px solid transparent; border-bottom:none; padding:.55rem 1.1rem; border-radius:8px 8px 0 0; cursor:pointer; font-size:.9rem; }}
.tab-buttons button:hover {{ color:#e2e8f0; }}
.tab-buttons button.active {{ background:#1e293b; color:#38bdf8; font-weight:bold; border-color:#334155; }}
.tab {{ display: none; }}
.tab.active {{ display: block; background:#1e293b; border:1px solid #334155; border-top:none; border-radius:0 10px 10px 10px; padding:1.25rem; margin:0 0 1.5rem; }}

.macro-tab-buttons {{ display:flex; gap:.25rem; margin:2rem 0 0; border-bottom:2px solid #334155; }}
.macro-tab-buttons button {{ position:relative; top:2px; background:transparent; color:#94a3b8; border:1px solid transparent; border-bottom:none; padding:.75rem 1.5rem; border-radius:10px 10px 0 0; cursor:pointer; font-size:1rem; font-weight:600; }}
.macro-tab-buttons button:hover {{ color:#e2e8f0; }}
.macro-tab-buttons button.active {{ background:#1e293b; color:#38bdf8; border-color:#334155; }}
.macro-tab {{ display: none; }}
.macro-tab.active {{ display: block; background:#1e293b; border:1px solid #334155; border-top:none; border-radius:0 12px 12px 12px; padding:1.75rem; }}
.macro-tab.active > h2:first-child, .macro-tab.active > .section > h2:first-child, .macro-tab.active > .summary-group:first-child {{ margin-top:0; }}
.status-badge {{ display:inline-block; padding:.2rem .6rem; border-radius:999px; font-size:.8rem; font-weight:bold; white-space:nowrap; }}
.status-badge.status-ok {{ background:rgba(74,222,128,.15); color:#4ade80; }}
.status-badge.status-ko {{ background:rgba(248,113,113,.15); color:#f87171; }}
.table-scroll {{ overflow-x:auto; border-radius:8px; }}
.url-table {{ width:auto; min-width:100%; }}
.url-table th:nth-child(1), .url-table td:nth-child(1) {{ position:sticky; left:0; width:90px; min-width:90px; }}
.url-table th:nth-child(2), .url-table td:nth-child(2) {{ position:sticky; left:90px; width:320px; min-width:320px; max-width:320px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.url-table th:nth-child(3), .url-table td:nth-child(3) {{ position:sticky; left:410px; width:80px; min-width:80px; box-shadow:4px 0 6px -4px rgba(0,0,0,.5); }}
.url-table th:nth-child(1), .url-table th:nth-child(2), .url-table th:nth-child(3) {{ background:#0f172a; z-index:3; }}
.url-table td:nth-child(1), .url-table td:nth-child(2), .url-table td:nth-child(3) {{ background:#1e293b; z-index:1; }}
.url-table tbody tr:hover td:nth-child(1), .url-table tbody tr:hover td:nth-child(2), .url-table tbody tr:hover td:nth-child(3) {{ background:#334155; }}
.url-table tbody tr.row-issue td {{ background:rgba(251,191,36,.08); }}
.url-table tbody tr.row-issue td:nth-child(1), .url-table tbody tr.row-issue td:nth-child(2), .url-table tbody tr.row-issue td:nth-child(3) {{ background:rgba(251,191,36,.16); }}
.url-table tbody tr.row-issue:hover td {{ background:#334155; }}
.locale-badge {{ display:inline-block; padding:.2rem .6rem; border-radius:999px; font-size:.8rem; font-weight:bold; white-space:nowrap; background:rgba(148,163,184,.15); color:#e2e8f0; }}
.locale-buttons {{ display:flex; flex-wrap:wrap; gap:.5rem; margin:0 0 1rem; }}
.locale-buttons button {{ background:#1e293b; color:#94a3b8; border:1px solid #334155; padding:.35rem .8rem; border-radius:999px; cursor:pointer; font-size:.85rem; }}
.locale-buttons button.active {{ background:#38bdf8; color:#0f172a; font-weight:bold; border-color:#38bdf8; }}
.schema-badge {{ display:inline-block; padding:.15rem .5rem; margin:.1rem; border-radius:6px; font-size:.78rem; font-weight:600; background:rgba(56,189,248,.15); color:#38bdf8; }}
</style>
</head>
<body>
<h1>🔍 SEO Audit Report</h1>
<p>Dominio: <strong>{self.domain}</strong> — Generato il {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<p>Configurazione: concorrenza <strong>{self.concurrency}</strong>, max richieste/sec <strong>{self.max_rps}</strong></p>

<div class="macro-tab-buttons">
    <button class="active" onclick="showMacroTab('macro-dashboard')">📊 Dashboard</button>
    <button onclick="showMacroTab('macro-url')">📄 URL analizzati</button>
    <button onclick="showMacroTab('macro-sitemap')">🗺️ Sitemap</button>
    <button onclick="showMacroTab('macro-schema')">🏷️ Dati Strutturati</button>
    <button onclick="showMacroTab('macro-heading')">🏗️ Gerarchia Tag</button>
    <button onclick="showMacroTab('macro-lighthouse')">⚡ Lighthouse Benchmark</button>
</div>

<div id="macro-dashboard" class="macro-tab active">
<div class="summary-group">
<h3 class="summary-group-title">🌐 Crawl &amp; Stato HTTP</h3>
<div class="summary">
    <div class="card"><div class="value">{total}</div><div class="label">URL crawllati</div></div>
    <div class="card good"><div class="value">{ok_200}</div><div class="label">Status 200</div></div>
    <div class="card {self._issue_class(status_404)}"><div class="value">{status_404}</div><div class="label">Status 404</div></div>
    <div class="card {self._issue_class(status_error)}"><div class="value">{status_error}</div><div class="label">Altri errori</div></div>
</div>
</div>

<div class="summary-group">
<h3 class="summary-group-title">⚡ Performance (Crawl)</h3>
<div class="summary">
    <div class="card"><div class="value">{avg_response}</div><div class="label">Tempo risposta medio (ms)</div></div>
    <div class="card"><div class="value">{avg_ttfb}</div><div class="label">TTFB medio (ms)</div></div>
</div>
</div>
{lighthouse_summary_html}
<div class="summary-group">
<h3 class="summary-group-title">📝 Contenuti On-Page</h3>
<div class="summary">
    <div class="card {self._issue_class(missing_title)}"><div class="value">{missing_title}</div><div class="label">Title mancanti</div></div>
    <div class="card {self._issue_class(missing_desc)}"><div class="value">{missing_desc}</div><div class="label">Meta desc mancanti</div></div>
    <div class="card {self._issue_class(missing_h1)}"><div class="value">{missing_h1}</div><div class="label">H1 mancanti</div></div>
    <div class="card {self._issue_class(multiple_h1)}"><div class="value">{multiple_h1}</div><div class="label">Più H1</div></div>
    <div class="card {self._issue_class(missing_alt)}"><div class="value">{missing_alt}</div><div class="label">URL con img senza alt</div></div>
    <div class="card {self._issue_class(missing_schema)}"><div class="value">{missing_schema}</div><div class="label">Dati strutturati mancanti</div></div>
    <div class="card {self._issue_class(bad_heading)}"><div class="value">{bad_heading}</div><div class="label">Gerarchia heading errata</div></div>
</div>
</div>

<div class="summary-group">
<h3 class="summary-group-title">🗺️ Indicizzazione</h3>
<div class="summary">
    <div class="card {self._issue_class(missing_sitemap)}"><div class="value">{missing_sitemap}</div><div class="label">URL non in sitemap</div></div>
</div>
</div>
</div>

<div id="macro-url" class="macro-tab">
<div class="section">
<h2>📄 URL analizzati</h2>
<div class="tab-buttons">
    {status_tab_buttons}
</div>

<div class="locale-buttons">
    <button class="active" onclick="filterLocale('all', this)">🌐 Tutti i locale ({total})</button>
    {locale_buttons}
</div>
{status_tab_panels}
</div>
</div>

<div id="macro-sitemap" class="macro-tab">
<div class="section">
<h2>🗺️ Sitemap</h2>
<p>File sitemap trovati: <strong>{', '.join(self.sitemap_files) if self.sitemap_files else 'Nessuno'}</strong></p>
<p>URL in sitemap ma non crawllati (orphan): <strong>{len(missing_in_crawl)}</strong></p>
<p>URL crawllati ma non in sitemap: <strong>{len(missing_in_sitemap)}</strong></p>
{self._render_sitemap_detail(missing_in_crawl, missing_in_sitemap)}
</div>
</div>

<div id="macro-schema" class="macro-tab">
<div class="section">
<h2>🏷️ Dati Strutturati (Schema.org)</h2>
<div class="summary">
{schema_type_cards}
</div>
<div class="table-scroll">
<table>
<thead>
<tr><th>URL</th><th>Tipi di schema rilevati</th><th>Errori JSON-LD</th></tr>
</thead>
<tbody>
{schema_rows}
</tbody>
</table>
</div>
</div>
</div>

<div id="macro-heading" class="macro-tab">
<div class="section">
<h2>🏗️ Gerarchia Tag (H1-H6)</h2>
<p>Verifica il rispetto della gerarchia di intestazioni secondo le best practice Google: nessun salto di livello in discesa (es. H2 seguito direttamente da H4).</p>
<div class="tab-buttons">
    {tag_tab_buttons}
</div>
{tag_tab_panels}
</div>
</div>

<div id="macro-lighthouse" class="macro-tab">
<div class="section">
<h2>⚡ Lighthouse Benchmark</h2>
<div class="tab-buttons">
    <button class="active" onclick="showTab('lh-mobile', this)">📱 Mobile</button>
    <button onclick="showTab('lh-desktop', this)">💻 Desktop</button>
</div>
<div id="lh-mobile" class="tab active">
<table>
<thead>
<tr><th>URL</th><th>Performance</th><th>Accessibility</th><th>Best Practices</th><th>SEO</th><th>LCP</th><th>FCP</th><th>TBT</th><th>CLS</th></tr>
</thead>
<tbody>
{lighthouse_table}
</tbody>
</table>
</div>
<div id="lh-desktop" class="tab">
<table>
<thead>
<tr><th>URL</th><th>Performance</th><th>Accessibility</th><th>Best Practices</th><th>SEO</th><th>LCP</th><th>FCP</th><th>TBT</th><th>CLS</th></tr>
</thead>
<tbody>
{lighthouse_table_desktop}
</tbody>
</table>
</div>
</div>
</div>

<script>
function showTab(id, btn) {{
    const scope = btn.closest('.macro-tab') || document;
    scope.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    scope.querySelectorAll('.tab-buttons button').forEach(el => el.classList.remove('active'));
    btn.classList.add('active');
}}
function showMacroTab(id) {{
    document.querySelectorAll('.macro-tab').forEach(el => el.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    document.querySelectorAll('.macro-tab-buttons button').forEach(el => el.classList.remove('active'));
    event.target.classList.add('active');
}}
function filterLocale(locale, btn) {{
    document.querySelectorAll('.url-table tbody tr[data-locale]').forEach(tr => {{
        tr.style.display = (locale === 'all' || tr.dataset.locale === locale) ? '' : 'none';
    }});
    document.querySelectorAll('.locale-buttons button').forEach(el => el.classList.remove('active'));
    btn.classList.add('active');
}}
</script>
</body>
</html>"""
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
        return output_path


def main():
    parser = argparse.ArgumentParser(description='SEO Audit Crawler + Sitemap + HTML Report + Lighthouse')
    parser.add_argument('url', help='URL di partenza, es. https://example.com')
    parser.add_argument('--max-pages', type=int, default=500, help='Max pagine da crawllare')
    parser.add_argument('--max-depth', type=int, default=None, help='Profondita massima di crawling rispetto alla home (default: nessun limite)')
    parser.add_argument('--concurrency', type=int, default=1, help='Numero di worker concorrenti (default: 1)')
    parser.add_argument('--max-rps', type=float, default=1.0, help='Max richieste al secondo (default: 1.0)')
    parser.add_argument('--respect-robots', action='store_true', default=True, help='Rispetta robots.txt')
    parser.add_argument('--output', default='seo_report.csv', help='File CSV di output')
    parser.add_argument('--sitemap-output', default='sitemap_report.csv', help='File CSV di output per il confronto sitemap')
    parser.add_argument('--html', default='seo_report.html', help='File HTML di output')
    parser.add_argument('--no-sitemap', action='store_true', help='Disabilita analisi sitemap')
    parser.add_argument('--lighthouse-sample', type=int, default=5, help='Numero di URL random per Lighthouse (0=disabilita)')
    parser.add_argument('--lighthouse-form-factor', default='mobile', choices=['mobile', 'desktop'], help='DEPRECATED: ora esegue sempre mobile e desktop')
    parser.add_argument('--user-agent', default=None, help='User-Agent custom (default: Chrome Windows)')
    parser.add_argument('--progress-file', default=None, help='Percorso di un file JSON aggiornato periodicamente con lo stato di avanzamento (fase, pagine correnti/totali, percentuale)')
    parser.add_argument('--lighthouse-bin', default=None, help='Percorso esplicito del binario lighthouse (bypassa la ricerca in PATH, utile su WSL per evitare di raccogliere un binario Windows)')
    args = parser.parse_args()

    print(f"[INFO] Inizio crawl di {args.url}")
    print(f"[INFO] Concorrenza: {args.concurrency}, max richieste/sec: {args.max_rps}, profondita max: {args.max_depth if args.max_depth is not None else 'illimitata'}")
    write_progress(args.progress_file, 'crawl', 0, args.max_pages)
    crawler = SEOCrawler(
        args.url,
        max_pages=args.max_pages,
        concurrency=args.concurrency,
        max_rps=args.max_rps,
        respect_robots=args.respect_robots,
        user_agent=args.user_agent,
        max_depth=args.max_depth,
        progress_file=args.progress_file,
    )
    results = crawler.crawl()
    print(f"[INFO] Crawl completato: {len(results)} URL analizzati")
    # Sitemap
    sitemap_urls = []
    sitemap_files = []
    if not args.no_sitemap:
        write_progress(args.progress_file, 'sitemap', 0, 1)
        domain = urlparse(args.url).netloc
        scheme = urlparse(args.url).scheme
        sitemap = SitemapAnalyzer(domain, session=crawler.session, scheme=scheme)
        sitemap_urls = sitemap.analyze()
        sitemap_files = sitemap.sitemap_files
        print(f"[INFO] URL trovati in sitemap: {len(sitemap_urls)}")
        write_progress(args.progress_file, 'sitemap', 1, 1)

    # Build internal link map for orphan detection
    internal_links_map = {}
    for r in results:
        internal_links_map[r['url']] = r.get('internal_links', 0)

    crawled_urls = {r['url'] for r in results}
    sitemap_set = set(sitemap_urls)
    for r in results:
        r['sitemap_declared'] = r['url'] in sitemap_set

    # CSV
    fieldnames = [
        'url', 'status_code', 'response_time_ms', 'ttfb_ms', 'title', 'title_length',
        'meta_description', 'meta_description_length', 'canonical', 'canonical_self', 'canonical_external',
        'hreflang_count', 'hreflang_has_x_default', 'hreflang_languages',
        'h1', 'h1_count', 'meta_robots', 'og_title', 'og_description', 'internal_links', 'external_links',
        'missing_alt', 'has_schema', 'schema_types', 'schema_json_errors',
        'heading_sequence', 'heading_hierarchy_ok', 'heading_violations',
        'locale', 'sitemap_declared', 'crawled', 'depth'
    ]
    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"[INFO] Report CSV salvato in {args.output}")

    # Sitemap report
    if not args.no_sitemap:
        with open(args.sitemap_output, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['type', 'url'])
            for u in sitemap_set - crawled_urls:
                writer.writerow(['missing_in_crawl', u])
            for u in crawled_urls - sitemap_set:
                writer.writerow(['missing_in_sitemap', u])
        print(f"[INFO] Report sitemap salvato in {args.sitemap_output}")

    # Lighthouse
    lighthouse_reports_mobile = []
    lighthouse_reports_desktop = []
    if args.lighthouse_sample > 0:
        ok_urls = [r['url'] for r in results if r['status_code'] == 200]
        runner = LighthouseRunner(sample_size=args.lighthouse_sample, progress_file=args.progress_file, lighthouse_bin=args.lighthouse_bin)
        sampled = runner.sample_urls(ok_urls)
        if sampled:
            lighthouse_reports_mobile = runner.run(sampled, form_factor='mobile')
            lighthouse_reports_desktop = runner.run(sampled, form_factor='desktop')

    # HTML
    write_progress(args.progress_file, 'report', 0, 1)
    domain = urlparse(args.url).netloc
    html_path = HTMLReport(
        results, sitemap_urls, sitemap_files, crawled_urls, internal_links_map,
        lighthouse_reports_mobile, lighthouse_reports_desktop,
        domain, args.concurrency, args.max_rps
    ).render(args.html)
    print(f"[INFO] Report HTML salvato in {html_path}")
    write_progress(args.progress_file, 'done', 1, 1)


if __name__ == '__main__':
    main()
