#!/usr/bin/env python3
"""Wayback — archive external URLs with full styles and images.

NDJSON bidirectional protocol over stdin/stdout. Triggers:
  - "Archive URL": fetch a URL with `monolith`, save as
    static/archive/<category>/<slug>/index.html, plus a Hugo markdown stub at
    content/archive/<category>/<slug>.md.
  - "Manage Archives": list archives, prompt for items to delete, remove both
    the markdown stub and the static folder.
"""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse


# Force line-buffered stdout so each NDJSON message reaches the host
# immediately even if the plugin blocks afterwards on stdin.
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, OSError):
    pass


# ── NDJSON protocol helpers ──────────────────────────────────────────────

def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def send_progress(phase=None, current=None, total=None, message=None):
    msg = {"type": "progress"}
    if phase is not None: msg["phase"] = phase
    if current is not None: msg["current"] = float(current)
    if total is not None: msg["total"] = float(total)
    if message is not None: msg["message"] = message
    _send(msg)


def send_result(success, message=None, error=None, actions=None):
    msg = {"type": "result", "success": success}
    if message: msg["message"] = message
    if error: msg["error"] = error
    if actions: msg["actions"] = actions
    _send(msg)


def _read_line():
    return sys.stdin.readline()


def prompt_select(title, items, multiple=False, message=None):
    """Ask the user to pick from `items`. Returns the selected value(s), or
    None if the user cancelled."""
    pid = str(uuid.uuid4())
    msg = {
        "type": "prompt", "id": pid, "kind": "select",
        "title": title, "items": items, "multiple": multiple,
    }
    if message: msg["message"] = message
    _send(msg)
    line = _read_line()
    if not line:
        return None
    try:
        resp = json.loads(line)
    except Exception:
        return None
    if resp.get("type") != "prompt_response" or resp.get("id") != pid:
        return None
    return resp.get("value")


# ── Server config / paths ────────────────────────────────────────────────

def load_server_config():
    config_path = os.path.expanduser("~/.inn_server_config.json")
    with open(config_path, "r") as f:
        return json.load(f)


def get_base_path(data):
    ctx = data.get("context", {})
    base_path = ctx.get("base_path", "")
    if base_path:
        return base_path
    try:
        config = load_server_config()
        return config.get("cms_config", {}).get("hugo_config", {}).get("base_path", "")
    except Exception:
        return ""


def result_guide(title, body):
    return {
        "success": False,
        "error": title,
        "actions": [{
            "type": "show_result",
            "content": {"title": f"Wayback — {title}", "body": body},
        }],
    }


# ── Slug ─────────────────────────────────────────────────────────────────

MAX_SLUG_LEN = 20


def slugify(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[-\s]+", "-", text)
    return text.strip("-")


def truncate_slug(s, max_len=MAX_SLUG_LEN):
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    last_hyphen = cut.rfind("-")
    if last_hyphen >= max_len // 2:
        return cut[:last_hyphen]
    return cut.rstrip("-")


def make_slug(title, url):
    s = slugify(title)
    if len(s) >= 3:
        return truncate_slug(s)
    domain = (urlparse(url).netloc.replace(".", "-") or "archive")[:10]
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{domain}-{h}"


def unique_slug(slug, category_dir):
    """Avoid colliding with an existing `<slug>.md` in the same category."""
    candidate = slug
    i = 2
    while os.path.exists(os.path.join(category_dir, f"{candidate}.md")):
        candidate = f"{slug}-{i}"
        i += 1
    return candidate


# ── Title fetch + cleanup ────────────────────────────────────────────────

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_ENTITY_RE = re.compile(r"&(amp|lt|gt|quot|apos|#39);")
_ENTITY_MAP = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "#39": "'"}
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[·|·\-—–]\s*(GitHub|YouTube|Medium|Stack Overflow|DEV Community|Reddit|Twitter|X|LinkedIn)\s*$",
    re.IGNORECASE,
)
_TITLE_PREFIX_RE = re.compile(r"^GitHub\s*-\s*", re.IGNORECASE)


def clean_fetched_title(title):
    if not title:
        return ""
    prev = None
    while title != prev:
        prev = title
        title = _TITLE_SUFFIX_RE.sub("", title).strip()
    title = _TITLE_PREFIX_RE.sub("", title).strip()
    return title


def fetch_title(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (wayback-plugin)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read(65536).decode("utf-8", errors="replace")
    except Exception:
        return ""
    m = _TITLE_RE.search(html)
    if not m:
        return ""
    title = re.sub(r"\s+", " ", m.group(1).strip())
    title = _ENTITY_RE.sub(lambda x: _ENTITY_MAP.get(x.group(1), x.group(0)), title)
    return clean_fetched_title(title)


# ── Tags / YAML ──────────────────────────────────────────────────────────

def parse_tags(raw):
    if not raw:
        return []
    seen, out = set(), []
    for part in raw.split(","):
        t = part.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def yaml_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── Monolith ─────────────────────────────────────────────────────────────

def find_monolith():
    r = subprocess.run(["which", "monolith"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    for candidate in (
        os.path.expanduser("~/.local/bin/monolith"),
        os.path.expanduser("~/.cargo/bin/monolith"),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def monolith_release_url():
    system = platform.system()
    machine = platform.machine().lower()
    base = "https://github.com/Y2Z/monolith/releases/latest/download"
    if system == "Linux":
        if machine in ("x86_64", "amd64"): return f"{base}/monolith-gnu-linux-x86_64"
        if machine in ("aarch64", "arm64"): return f"{base}/monolith-gnu-linux-aarch64"
    if system == "Darwin":
        return f"{base}/monolith-mac"
    return None


def install_guide_text(reason):
    return (
        f"{reason}\n\n"
        "Install manually on the server, then retry:\n\n"
        "  mkdir -p ~/.local/bin\n"
        "  curl -L https://github.com/Y2Z/monolith/releases/latest/download/monolith-gnu-linux-x86_64 \\\n"
        "    -o ~/.local/bin/monolith && chmod +x ~/.local/bin/monolith\n\n"
        "(replace the asset name with monolith-gnu-linux-aarch64 / monolith-mac for ARM64 / macOS)\n"
    )


def ensure_monolith():
    binary = find_monolith()
    if binary:
        return binary, ""

    url = monolith_release_url()
    if not url:
        return None, install_guide_text(
            f"No prebuilt monolith binary for {platform.system()}/{platform.machine()}."
        )

    send_progress(phase="install", message="Downloading monolith binary...")
    dest_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "monolith")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wayback-plugin"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.chmod(dest, 0o755)
    except Exception as e:
        return None, install_guide_text(f"Auto-download failed: {e}")
    return dest, ""


def run_monolith(binary, url, output_path):
    r = subprocess.run(
        [binary, "--no-audio", "--no-video", "-o", output_path, url],
        capture_output=True, text=True, timeout=300,
    )
    return r.returncode == 0, r.stderr.strip()


def format_size(b):
    if b < 1024: return f"{b} B"
    if b < 1024 * 1024: return f"{b/1024:.1f} KB"
    if b < 1024 * 1024 * 1024: return f"{b/(1024*1024):.1f} MB"
    return f"{b/(1024*1024*1024):.2f} GB"


# ── Archive URL ──────────────────────────────────────────────────────────

def archive_url(data):
    url = (data.get("url") or "").strip()
    if not url:
        return {"success": False, "error": "URL is required."}
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return {"success": False, "error": "URL must start with http:// or https://"}

    base_path = get_base_path(data)
    if not base_path:
        return result_guide(
            "Configuration Required",
            "base_path not configured. Set cms_config.hugo_config.base_path "
            "in ~/.inn_server_config.json."
        )
    if not os.path.isdir(base_path):
        return {"success": False, "error": f"Path not found: {base_path}"}

    send_progress(phase="setup", message="Checking monolith...")
    binary, msg = ensure_monolith()
    if not binary:
        return result_guide("monolith required", msg)

    title = (data.get("title") or "").strip()
    if not title:
        send_progress(phase="fetch_title", message="Fetching page title...")
        title = fetch_title(url)
    if not title:
        title = urlparse(url).netloc or "untitled"

    category = slugify((data.get("category") or "").strip()) or "tmp"
    tags = parse_tags(data.get("tags") or "")

    category_content_dir = os.path.join(base_path, "content", "archive", category)
    category_static_dir = os.path.join(base_path, "static", "snapshot", category)
    os.makedirs(category_content_dir, exist_ok=True)
    os.makedirs(category_static_dir, exist_ok=True)

    slug = unique_slug(make_slug(title, url), category_content_dir)
    slug_static_dir = os.path.join(category_static_dir, slug)
    os.makedirs(slug_static_dir, exist_ok=True)
    html_path = os.path.join(slug_static_dir, "index.html")

    send_progress(phase="download", message=f"Snapshotting {url} ...")
    ok, stderr = run_monolith(binary, url, html_path)
    if not ok:
        # cleanup empty folder
        try: os.rmdir(slug_static_dir)
        except OSError: pass
        return result_guide("monolith failed", stderr or "Unknown error")

    file_size = os.path.getsize(html_path) if os.path.exists(html_path) else 0

    send_progress(phase="write", message="Writing frontmatter...")
    now = datetime.now(timezone.utc).astimezone()
    date_str = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    date_str = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", date_str)
    domain = urlparse(url).netloc
    archive_url_path = f"/snapshot/{category}/{slug}/"

    md_path = os.path.join(category_content_dir, f"{slug}.md")
    tags_yaml = "[" + ", ".join(f'"{yaml_escape(t)}"' for t in tags) + "]"
    frontmatter = (
        "---\n"
        f'title: "{yaml_escape(title)}"\n'
        f"date: {date_str}\n"
        f'source_url: "{yaml_escape(url)}"\n'
        f'source_domain: "{yaml_escape(domain)}"\n'
        f'category: "{category}"\n'
        f"tags: {tags_yaml}\n"
        f'archive_path: "{archive_url_path}"\n'
        "---\n\n"
        f"[View original]({url}) · [Archived snapshot]({archive_url_path})\n"
    )
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(frontmatter)

    body = (
        f"Archived: {title}\n\n"
        f"  Source:    {url}\n"
        f"  Category:  {category}\n"
        f"  Slug:      {slug}\n"
        f"  Tags:      {', '.join(tags) if tags else '(none)'}\n"
        f"  HTML:      {html_path} ({format_size(file_size)})\n"
        f"  Markdown:  {md_path}\n"
    )
    return {
        "success": True,
        "message": f"Archived: {title}",
        "actions": [
            {"type": "show_result", "content": {"title": "Wayback", "body": body}},
            {"type": "refresh_tree"},
        ],
    }


# ── Manage Archives (list + prompt + delete) ─────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FM_FIELD_RE = re.compile(r'^([a-z_]+):\s*"?([^"\n]*)"?\s*$', re.MULTILINE)


def parse_frontmatter(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read(8192)
    except Exception:
        return {}
    m = _FRONTMATTER_RE.search(text)
    if not m:
        return {}
    return {fm.group(1): fm.group(2).strip() for fm in _FM_FIELD_RE.finditer(m.group(1))}


def scan_archives(base_path):
    """Walk content/archive/<category>/*.md → list of {category, slug, title, date, source_url}."""
    archive_dir = os.path.join(base_path, "content", "archive")
    if not os.path.isdir(archive_dir):
        return []
    items = []
    for category in sorted(os.listdir(archive_dir)):
        cat_dir = os.path.join(archive_dir, category)
        if not os.path.isdir(cat_dir):
            continue
        for fname in sorted(os.listdir(cat_dir)):
            if not fname.endswith(".md") or fname.startswith("_"):
                continue
            md_path = os.path.join(cat_dir, fname)
            fm = parse_frontmatter(md_path)
            slug = fname[:-3]
            items.append({
                "category": category,
                "slug": slug,
                "title": fm.get("title", slug),
                "date": fm.get("date", ""),
                "source_url": fm.get("source_url", ""),
            })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


def manage_archives(data):
    base_path = get_base_path(data)
    if not base_path:
        return {"success": False, "error": "base_path not configured."}

    items = scan_archives(base_path)
    if not items:
        return {"success": False, "error": "No archives yet. Run 'Archive URL' first."}

    # Build prompt items: value = "<category>/<slug>", label = title, desc = source_url
    prompt_items = []
    for it in items:
        prompt_items.append({
            "value": f"{it['category']}/{it['slug']}",
            "label": f"[{it['category']}] {it['title']}",
            "description": it["source_url"] or f"{it['date']}",
        })

    selected = prompt_select(
        title=f"Manage Archives ({len(items)})",
        message="Select items to delete. Both the markdown file and the static folder will be removed.",
        items=prompt_items,
        multiple=True,
    )
    if not selected:
        return {"success": True, "message": "Cancelled."}

    deleted, failed = [], []
    total = len(selected)
    for i, key in enumerate(selected, start=1):
        send_progress(phase="delete", current=i, total=total, message=f"Removing {key} ...")
        try:
            category, slug = key.split("/", 1)
        except ValueError:
            failed.append((key, "malformed key"))
            continue

        md_path = os.path.join(base_path, "content", "archive", category, f"{slug}.md")
        static_dir = os.path.join(base_path, "static", "snapshot", category, slug)

        try:
            if os.path.isfile(md_path): os.remove(md_path)
            if os.path.isdir(static_dir): shutil.rmtree(static_dir)
            deleted.append(key)
        except Exception as e:
            failed.append((key, str(e)))

    lines = [f"Deleted {len(deleted)} archive(s)."]
    if deleted:
        lines.append("")
        for k in deleted: lines.append(f"  ✓ {k}")
    if failed:
        lines.append("")
        lines.append(f"Failed: {len(failed)}")
        for k, err in failed: lines.append(f"  ✗ {k}: {err}")

    return {
        "success": True,
        "message": f"Deleted {len(deleted)} / {total}",
        "actions": [
            {"type": "show_result", "content": {"title": "Manage Archives", "body": "\n".join(lines)}},
            {"type": "refresh_tree"},
        ],
    }


# ── Entry ────────────────────────────────────────────────────────────────

def main():
    line = _read_line()
    if not line:
        send_result(False, error="No input received.")
        return
    try:
        data = json.loads(line)
    except Exception as e:
        send_result(False, error=f"Bad input JSON: {e}")
        return

    trigger = data.get("trigger", "manual")
    if trigger != "manual":
        send_result(False, error="Only manual triggers are supported.")
        return

    # "Archive URL" has the `url` field; "Manage Archives" has none.
    if "url" in data:
        result = archive_url(data)
    else:
        result = manage_archives(data)

    send_result(**result)


if __name__ == "__main__":
    main()
