import os
import re
import time
import json
import base64
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
from seleniumwire import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import WebDriverException

# --- Nastrojki ---
EXTS = ('.gltf', '.glb', '.obj', '.stl', '.ply', '.fbx')
DEFAULT_WAIT = 6
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'


def default_download_root() -> Path:
    """Podbor defolt. kataloga zagruzki dlya raznyh OS."""
    home = Path.home()
    for cand in (home / 'Downloads', home / 'Zagruzki'):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            return cand
        except Exception:
            continue
    return Path.cwd() / 'downloads'


DEFAULT_DOWNLOAD_FOLDER = str(default_download_root())

# ---------- VSPOMOGATEL'NYE FUNKCII ----------

def resolve_out_folder(path_like: str | None) -> str:
    """Normalizuet put' vyvoda (peremennye okruzheniya, ~, absolyutnost')."""
    if not path_like:
        path_like = DEFAULT_DOWNLOAD_FOLDER
    p = os.path.expandvars(os.path.expanduser(path_like))
    if not os.path.isabs(p):
        p = os.path.abspath(p)
    Path(p).mkdir(parents=True, exist_ok=True)
    return p


def ensure_folder(path: str):
    Path(path).mkdir(parents=True, exist_ok=True)


def unique_path(out_path: str) -> str:
    """Vozvrashchaet unikalnyj put' (file (1).ext, file (2).ext, ...)."""
    base, ext = os.path.splitext(out_path)
    cand = out_path
    i = 1
    while os.path.exists(cand):
        cand = f"{base} ({i}){ext}"
        i += 1
    return cand


def is_3d_url(u: str) -> bool:
    if not u:
        return False
    u = u.strip()
    if u.startswith('data:'):
        return True
    p = u.split('?')[0].split('#')[0].lower()
    return any(p.endswith(ext) for ext in EXTS)


# ---------- SOHRANENIE ARTEFAKTOV ----------

def save_page_artifacts(driver, out_folder: str, page_url: str) -> dict:
    ts = time.strftime('%Y%m%d_%H%M%S')
    artifacts: dict[str, str] = {}

    html_name = f'page_{ts}.html'
    html_path = unique_path(os.path.join(out_folder, html_name))
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(driver.page_source or '')
    artifacts['page_html'] = html_path

    urls_name = f'network_urls_{ts}.txt'
    urls_path = unique_path(os.path.join(out_folder, urls_name))
    with open(urls_path, 'w', encoding='utf-8') as f:
        for req in getattr(driver, 'requests', []):
            try:
                f.write((req.url or '').strip() + '\n')
            except Exception:
                continue
    artifacts['network_urls'] = urls_path

    manifest_name = f'manifest_{ts}.json'
    manifest_path = unique_path(os.path.join(out_folder, manifest_name))
    payload = {'timestamp': ts, 'page_url': page_url, 'artifacts': artifacts}
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    artifacts['manifest'] = manifest_path

    return artifacts


# ---------- SKACHIVANIE ----------

_CD_RE = re.compile(r'filename\*?=(?:UTF-8\'\'\')?"?([^";]+)"?', re.IGNORECASE)

def pick_filename_from_headers(url: str, resp: requests.Response) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    cd = resp.headers.get('Content-Disposition') or resp.headers.get('content-disposition')
    if cd:
        m = _CD_RE.search(cd)
        if m:
            name = os.path.basename(m.group(1))
    if not name:
        name = f'downloaded_{int(time.time())}'
        ctype = (resp.headers.get('Content-Type') or '').lower()
        for ext in EXTS:
            if ext.lstrip('.') in ctype:
                name += ext
                break
    return name or f'downloaded_{int(time.time())}.bin'


def save_data_url(data_url: str, out_folder: str) -> str | None:
    """Sokhranyaet vstroenny data:...;base64,... resurs."""
    try:
        header, b64 = data_url.split(',', 1)
    except ValueError:
        print('Invalid data URL')
        return None
    mime = ''
    if ':' in header:
        mime = header.split(':', 1)[1].split(';', 1)[0]
    ext = '.bin'
    if 'gltf' in mime: ext = '.gltf'
    elif 'glb' in mime: ext = '.glb'
    elif 'stl' in mime: ext = '.stl'
    elif 'obj' in mime: ext = '.obj'
    elif 'ply' in mime: ext = '.ply'
    elif 'fbx' in mime: ext = '.fbx'

    fname = f'embedded_{int(time.time()*1000)}{ext}'
    out_path = unique_path(os.path.join(out_folder, fname))
    try:
        with open(out_path, 'wb') as f:
            f.write(base64.b64decode(b64))
        print('Saved embedded ->', out_path)
        return out_path
    except Exception as e:
        print('Error saving data URL:', e)
        return None


def download_url(url: str, out_folder: str, session: requests.Session | None = None) -> str | None:
    ensure_folder(out_folder)
    if url.startswith('data:'):
        return save_data_url(url, out_folder)

    sess = session or requests.Session()
    headers = {'User-Agent': USER_AGENT}
    try:
        resp = sess.get(url, headers=headers, stream=True, timeout=30)
    except Exception as e:
        print('Request error for', url, e)
        return None
    if resp.status_code != 200:
        print('Skip', url, 'status', resp.status_code)
        return None

    filename = pick_filename_from_headers(url, resp)
    out_path = unique_path(os.path.join(out_folder, filename))
    total = int(resp.headers.get('content-length', '0') or 0)

    try:
        with open(out_path, 'wb') as f:
            if total:
                with tqdm(total=total, unit='B', unit_scale=True, desc=filename) as bar:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            bar.update(len(chunk))
            else:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        print('Saved ->', out_path)
        return out_path
    except Exception as e:
        print('Error writing file', out_path, e)
        return None


# ---------- POISK 3D-SSYLOK V HTML ----------

def find_3d_urls_from_html(page_url: str, html: str) -> set[str]:
    soup = BeautifulSoup(html, 'html.parser')
    found: set[str] = set()

    for tag in soup.find_all(['a', 'link'], href=True):
        full = urljoin(page_url, tag.get('href') or '')
        if is_3d_url(full):
            found.add(full)

    for tag in soup.find_all(['img', 'source', 'script'], src=True):
        full = urljoin(page_url, tag.get('src') or '')
        if is_3d_url(full):
            found.add(full)

    for t in soup.find_all(True):
        for _, val in t.attrs.items():
            if isinstance(val, str) and is_3d_url(val):
                found.add(urljoin(page_url, val))

    for m in re.finditer(r'["\']([^"\']+\.(?:gltf|glb|obj|stl|ply|fbx)(?:\?[^"\']*)?)["\']', html, re.IGNORECASE):
        found.add(urljoin(page_url, m.group(1)))

    for m in re.finditer(r'(data:[^,]+;base64,[A-Za-z0-9+/=]+)', html):
        found.add(m.group(1))

    return found


# ---------- OSNOVNAYA LOGIKA ----------

def parse_dynamic_page(page_url: str,
                       out_folder: str = DEFAULT_DOWNLOAD_FOLDER,
                       wait: int = DEFAULT_WAIT,
                       save_artifacts: bool = True,
                       marker_on_empty: bool = True) -> list[str]:
    out_folder = resolve_out_folder(out_folder)
    ensure_folder(out_folder)

    chrome_opts = Options()
    chrome_opts.add_argument('--headless=new')
    chrome_opts.add_argument('--disable-gpu')
    chrome_opts.add_argument('--no-sandbox')
    chrome_opts.add_argument('--disable-dev-shm-usage')
    chrome_opts.add_argument('--disable-blink-features=AutomationControlled')
    chrome_opts.add_argument(f'--user-agent={USER_AGENT}')

    try:
        driver = webdriver.Chrome(options=chrome_opts)
    except WebDriverException as e:
        print('Error starting Chrome driver:', e)
        print('Ubedites, chto Chrome/ChromeDriver ustanovleny i sovmestimy.')
        return []

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})

    try:
        print('Opening page:', page_url)
        driver.scopes = ['.*']
        driver.get(page_url)
        print(f'Waiting {wait} seconds for dynamic content to load...')
        time.sleep(wait)

        results: list[str] = []

        if save_artifacts:
            artifacts = save_page_artifacts(driver, out_folder, page_url)
            results.extend(artifacts.values())

        print('Scanning network requests...')
        found = set()
        for req in driver.requests:
            try:
                if is_3d_url(req.url):
                    found.add(req.url)
            except Exception:
                continue

        print('Scanning page HTML...')
        html = driver.page_source
        found.update(find_3d_urls_from_html(page_url, html))
        print('Found', len(found), 'candidate 3D URLs')

        for url in sorted(found):
            saved = download_url(url, out_folder, session=session)
            if saved:
                results.append(saved)

        if not any(Path(p).suffix.lower() in EXTS for p in results) and marker_on_empty:
            marker = unique_path(os.path.join(out_folder, 'NO_3D_FOUND.txt'))
            with open(marker, 'w', encoding='utf-8') as f:
                f.write(f'URL: {page_url}\n3D-extensions: {chr(44).join(EXTS)}\nNo 3D resources found.')
            results.append(marker)
            print('Created marker ->', marker)

        return results
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ---------- CLI/INTERAKTIV ----------

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Ishchet 3D-resursy na stranitse, sohranyaet modeli i artefakty.')
    parser.add_argument('url', nargs='?', help='Page URL, naprimer: https://example.com/')
    parser.add_argument('--out', default=DEFAULT_DOWNLOAD_FOLDER,
                        help='Katalog vyvoda')
    parser.add_argument('--wait', type=int, default=DEFAULT_WAIT,
                        help='Pauza ozhidaniya dinamicheskogo kontenta, sek')
    parser.add_argument('--no-artifacts', action='store_true',
                        help='Ne sohranyat HTML/spisok setevyh URL/manifest')
    parser.add_argument('--no-empty-marker', action='store_true',
                        help='Ne sozdavat NO_3D_FOUND.txt pri otsutstvii 3D')

    args = parser.parse_args()

    url = args.url
    if not url:
        url = input('Page URL: ').strip()

    if not url:
        print('URL ne ukazan -- rabota prekrashchena.')
    else:
        files = parse_dynamic_page(
            url,
            out_folder=args.out,
            wait=args.wait,
            save_artifacts=not args.no_artifacts,
            marker_on_empty=not args.no_empty_marker,
        )
        print('\nGotovo. Sokhraneno fajlov:', len(files))
        for p in files:
            print(' -', p)
