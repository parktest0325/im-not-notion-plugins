#!/usr/bin/env python3
"""fix-image-link — Fix image links and clean orphans (two-pass)

Manual trigger: Scan all md files and fix image references.

Pass 1: Per-file link fixes
  - External refs (other file's images) → copy to my folder + update link
  - External URLs (http(s)://)          → download + save locally + update link
  - Handles both ![](…) and <img src="…"> patterns

Pass 2: Global orphan cleanup
  - Orphan images: not referenced by any md file → delete
  - Orphan dirs: image dir with no corresponding md → delete
"""

import json
import sys
import os
import re
import shutil
import hashlib

# Optional: urllib for URL download (stdlib, no pip needed)
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


def to_bool(val, default=False):
    """Safely parse boolean from JSON input (handles str/bool/None)."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return default


def load_server_config():
    config_path = os.path.expanduser("~/.inn_server_config.json")
    with open(config_path, "r") as f:
        return json.load(f)


def get_content_paths(hugo):
    paths = hugo.get("content_paths")
    if paths and isinstance(paths, list):
        return [p for p in paths if p]
    path = hugo.get("content_path", "")
    if path:
        return [path]
    return []


def find_md_files(directory):
    md_files = []
    if not os.path.isdir(directory):
        return md_files
    for dirpath, _, filenames in os.walk(directory):
        for f in filenames:
            if f.endswith(".md"):
                md_files.append(os.path.join(dirpath, f))
    return md_files


def find_image_files(directory):
    images = []
    if not os.path.isdir(directory):
        return images
    for dirpath, _, filenames in os.walk(directory):
        for f in filenames:
            images.append(os.path.join(dirpath, f))
    return images


def get_relative_path(abs_path, base_dir):
    base = base_dir.rstrip("/") + "/"
    if abs_path.startswith(base):
        return abs_path[len(base):]
    return None


def strip_fragment(path):
    """Strip #fragment from image path. e.g. 'img.png#center-w60' -> 'img.png'"""
    idx = path.find("#")
    return path[:idx] if idx >= 0 else path


def generate_url_filename(url):
    """Generate filename from URL: sha256 hash (12 chars) + extension."""
    path_part = url.split("?")[0].split("#")[0]
    ext = ""
    if "." in os.path.basename(path_part):
        ext = os.path.splitext(path_part)[1]
        if len(ext) > 6:
            ext = ""
    hash_str = hashlib.sha256(url.encode()).hexdigest()[:12]
    return f"{hash_str}{ext}"


def download_image(url, dest_path):
    """Download image from URL using stdlib urllib. Returns True on success."""
    try:
        req = Request(url, headers={"User-Agent": "inn-fix-image-link/2.0"})
        resp = urlopen(req, timeout=15)
        data = resp.read()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except (URLError, HTTPError, OSError):
        return False


def files_are_identical(path_a, path_b):
    """Compare two files by SHA-256 hash."""
    def sha256(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    return sha256(path_a) == sha256(path_b)


def parse_all_image_refs(content):
    """Parse all image references from markdown.

    Returns list of dicts with keys:
        full_match, path, is_external_url, type ("markdown"|"img_tag"),
        alt (for markdown), prefix/quote (for img_tag)
    """
    refs = []

    for m in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", content):
        path = m.group(2).strip()
        refs.append({
            "full_match": m.group(0),
            "alt": m.group(1),
            "path": path,
            "is_external_url": path.startswith(("http://", "https://")),
            "type": "markdown",
        })

    for m in re.finditer(r"""(<img\s[^>]*src\s*=\s*)(["'])([^"']+)\2""", content):
        path = m.group(3).strip()
        refs.append({
            "full_match": m.group(0),
            "prefix": m.group(1),
            "quote": m.group(2),
            "path": path,
            "is_external_url": path.startswith(("http://", "https://")),
            "type": "img_tag",
        })

    return refs


def replace_ref_in_content(content, ref, new_path):
    """Replace one image reference in content, ensuring / prefix."""
    new_path_with_slash = f"/{new_path}" if not new_path.startswith("/") else new_path
    old_str = ref["full_match"]
    if ref["type"] == "markdown":
        new_str = f"![{ref['alt']}]({new_path_with_slash})"
    else:
        new_str = f"{ref['prefix']}{ref['quote']}{new_path_with_slash}{ref['quote']}"
    return content.replace(old_str, new_str, 1)


def handle_manual(data, base_path, image_path, content_paths, hidden_path):
    dry_run = to_bool(data.get("dry_run"), True)
    download_urls = to_bool(data.get("download_urls"), True)
    clean_orphans = to_bool(data.get("clean_orphans"), True)

    image_root = os.path.join(base_path, image_path)

    # Collect all md files: (display_path, full_rel, md_abs)
    md_files = []
    for section in content_paths:
        content_dir = os.path.join(base_path, "content", section)
        for md_abs in find_md_files(content_dir):
            md_rel = get_relative_path(md_abs, content_dir)
            if not md_rel:
                continue
            full_rel = f"{section}/{md_rel}" if section else md_rel
            md_files.append((full_rel, full_rel, md_abs))

        if hidden_path:
            hidden_dir = os.path.join(base_path, "content", hidden_path, section)
            if os.path.isdir(hidden_dir):
                for md_abs in find_md_files(hidden_dir):
                    md_rel = get_relative_path(md_abs, hidden_dir)
                    if not md_rel:
                        continue
                    full_rel = f"{section}/{md_rel}" if section else md_rel
                    md_files.append((f"[H] {full_rel}", full_rel, md_abs))

    # ══════════════════════════════════════════
    # Pass 1: Fix links per file
    # ══════════════════════════════════════════
    global_referenced = set()  # all referenced image paths (after fixes)
    file_reports = []
    total_refs_copied = 0
    total_urls_downloaded = 0
    total_urls_failed = 0

    for display_path, full_rel, md_abs in md_files:
        my_prefix = f"{full_rel}/"
        my_image_dir = os.path.join(image_root, full_rel)

        with open(md_abs, "r", encoding="utf-8") as f:
            content = f.read()
        original_content = content

        refs = parse_all_image_refs(content)
        details = []
        refs_copied = 0
        urls_downloaded = 0
        urls_failed = 0

        for ref in refs:
            path = ref["path"]

            if ref["is_external_url"]:
                if not download_urls:
                    continue

                filename = generate_url_filename(path)
                new_rel = f"{full_rel}/{filename}"
                new_abs = os.path.join(image_root, new_rel)

                if dry_run:
                    urls_downloaded += 1
                    details.append(f"  [DRY] Download: {path} -> {new_rel}")
                    global_referenced.add(new_rel)
                else:
                    if download_image(path, new_abs):
                        content = replace_ref_in_content(content, ref, new_rel)
                        urls_downloaded += 1
                        details.append(f"  Downloaded: {path} -> {new_rel}")
                        global_referenced.add(new_rel)
                    else:
                        urls_failed += 1
                        details.append(f"  FAILED download: {path}")
            else:
                raw_path = path.lstrip("/")
                ref_clean = strip_fragment(raw_path)
                fragment = raw_path[len(ref_clean):]

                if ref_clean.startswith(my_prefix):
                    # Already mine
                    global_referenced.add(ref_clean)
                    continue

                # External ref → copy to my folder
                src_abs = os.path.join(image_root, ref_clean)
                if not os.path.isfile(src_abs):
                    # Broken ref — skip (verify reports this)
                    global_referenced.add(ref_clean)
                    continue

                filename = os.path.basename(ref_clean)
                new_rel = f"{full_rel}/{filename}"
                new_abs = os.path.join(image_root, new_rel)

                if os.path.isfile(new_abs):
                    if files_are_identical(src_abs, new_abs):
                        # Same file exists → just update link
                        if dry_run:
                            details.append(f"  [DRY] Relink (same): {ref_clean} -> {new_rel}")
                        else:
                            content = replace_ref_in_content(content, ref, new_rel + fragment)
                            details.append(f"  Relinked (same): {ref_clean} -> {new_rel}")
                        refs_copied += 1
                        global_referenced.add(new_rel)
                    else:
                        # Different file → copy with new name
                        rand_hex = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
                        new_filename = f"{rand_hex}_{filename}"
                        new_rel = f"{full_rel}/{new_filename}"
                        new_abs = os.path.join(image_root, new_rel)
                        if dry_run:
                            details.append(f"  [DRY] Copy (renamed): {ref_clean} -> {new_rel}")
                        else:
                            os.makedirs(os.path.dirname(new_abs), exist_ok=True)
                            shutil.copy2(src_abs, new_abs)
                            content = replace_ref_in_content(content, ref, new_rel + fragment)
                            details.append(f"  Copied (renamed): {ref_clean} -> {new_rel}")
                        refs_copied += 1
                        global_referenced.add(new_rel)
                else:
                    # No conflict → simple copy
                    if dry_run:
                        details.append(f"  [DRY] Copy: {ref_clean} -> {new_rel}")
                    else:
                        os.makedirs(os.path.dirname(new_abs), exist_ok=True)
                        shutil.copy2(src_abs, new_abs)
                        content = replace_ref_in_content(content, ref, new_rel + fragment)
                        details.append(f"  Copied: {ref_clean} -> {new_rel}")
                    refs_copied += 1
                    global_referenced.add(new_rel)

        # Save modified content
        if not dry_run and content != original_content:
            with open(md_abs, "w", encoding="utf-8") as f:
                f.write(content)
            # Re-parse to update global_referenced with final paths
            final_refs = parse_all_image_refs(content)
            for ref in final_refs:
                if not ref["is_external_url"]:
                    global_referenced.add(strip_fragment(ref["path"].lstrip("/")))
        elif dry_run:
            # In dry run, also collect current refs that weren't changed
            for ref in refs:
                if not ref["is_external_url"]:
                    global_referenced.add(strip_fragment(ref["path"].lstrip("/")))

        file_total = refs_copied + urls_downloaded
        if file_total > 0 or urls_failed > 0:
            parts = []
            if refs_copied:
                parts.append(f"{refs_copied} refs fixed")
            if urls_downloaded:
                parts.append(f"{urls_downloaded} URLs downloaded")
            if urls_failed:
                parts.append(f"{urls_failed} URL failures")
            file_reports.append((display_path, ", ".join(parts), details))

        total_refs_copied += refs_copied
        total_urls_downloaded += urls_downloaded
        total_urls_failed += urls_failed

    # ══════════════════════════════════════════
    # Pass 2: Global orphan cleanup
    # ══════════════════════════════════════════
    total_orphan_images = 0
    total_orphan_dirs = 0
    orphan_details = []

    if clean_orphans:
        # Only scan inside content_paths sections to avoid deleting
        # Hugo site resources (favicons, logos, etc.) at the image_path root.
        all_images = set()
        for section in content_paths:
            section_dir = os.path.join(image_root, section)
            for abs_path in find_image_files(section_dir):
                rel = get_relative_path(abs_path, image_root)
                if rel:
                    all_images.add(rel)

        md_full_rels = set(full_rel for _, full_rel, _ in md_files)

        # Orphan images: not referenced by any md
        orphan_images = sorted(all_images - global_referenced)
        for img in orphan_images:
            img_abs = os.path.join(image_root, img)
            if dry_run:
                total_orphan_images += 1
                orphan_details.append(f"  [DRY] Delete image: {img}")
            else:
                try:
                    os.remove(img_abs)
                    total_orphan_images += 1
                    orphan_details.append(f"  Deleted image: {img}")
                except OSError:
                    orphan_details.append(f"  FAILED delete: {img}")

        # Orphan dirs: image dir with no corresponding md
        image_dirs = set()
        remaining_images = all_images - set(orphan_images) if not dry_run else all_images
        for img in remaining_images:
            parent = os.path.dirname(img)
            if parent:
                image_dirs.add(parent)

        for d in sorted(image_dirs):
            if d not in md_full_rels:
                dir_abs = os.path.join(image_root, d)
                if os.path.isdir(dir_abs):
                    if dry_run:
                        count = sum(1 for img in all_images if os.path.dirname(img) == d)
                        total_orphan_dirs += 1
                        orphan_details.append(f"  [DRY] Delete dir: {d}/ ({count} files)")
                    else:
                        try:
                            shutil.rmtree(dir_abs)
                            total_orphan_dirs += 1
                            orphan_details.append(f"  Deleted dir: {d}/")
                        except OSError:
                            orphan_details.append(f"  FAILED delete dir: {d}/")

    # ══════════════════════════════════════════
    # Build report
    # ══════════════════════════════════════════
    mode = "DRY RUN" if dry_run else "APPLIED"
    lines = []
    lines.append(f"=== Fix Image Links ({mode}) ===")
    lines.append(f"Scanned: {len(md_files)} files  |  Sections: {', '.join(content_paths)}")
    lines.append(f"Options: dry_run={dry_run} (raw={data.get('dry_run', 'N/A')!r}), "
                 f"download_urls={download_urls}, clean_orphans={clean_orphans}")
    lines.append("")

    has_fixes = file_reports or orphan_details
    if not has_fixes:
        lines.append("No issues found — all image links are correct!")
    else:
        if file_reports:
            lines.append("── Pass 1: Link Fixes ──")
            for display, summary, details in file_reports:
                lines.append(f"  {display} — {summary}")
                for detail in details:
                    lines.append(detail)
            lines.append("")

        if orphan_details:
            lines.append("── Pass 2: Orphan Cleanup ──")
            for detail in orphan_details:
                lines.append(detail)
            lines.append("")

        parts = []
        if total_refs_copied:
            parts.append(f"{total_refs_copied} refs fixed")
        if total_urls_downloaded:
            parts.append(f"{total_urls_downloaded} downloaded")
        if total_urls_failed:
            parts.append(f"{total_urls_failed} failed")
        if total_orphan_images:
            parts.append(f"{total_orphan_images} orphan images")
        if total_orphan_dirs:
            parts.append(f"{total_orphan_dirs} orphan dirs")
        lines.append(f"Summary: {', '.join(parts)}")

    if dry_run:
        lines.append("")
        lines.append("This was a DRY RUN. No files were modified.")
        lines.append("Run again with 'Dry run' unchecked to apply changes.")

    body = "\n".join(lines)

    return {
        "success": True,
        "message": f"{mode}: {total_refs_copied} fixed, {total_urls_downloaded} downloaded, "
                   f"{total_orphan_images} orphan imgs, {total_orphan_dirs} orphan dirs",
        "actions": [
            {"type": "show_result", "content": {"title": f"Fix Image Links ({mode})", "body": body}},
        ],
    }


def main():
    data = json.loads(sys.stdin.read())
    trigger = data.get("trigger", "")
    ctx = data.get("context", {})

    try:
        config = load_server_config()
        hugo = config.get("cms_config", {}).get("hugo_config", {})
        base_path = hugo.get("base_path", ctx.get("base_path", ""))
        image_path = hugo.get("image_path", ctx.get("image_path", ""))
        content_paths = get_content_paths(hugo) or ctx.get("content_paths", [ctx.get("content_path", "")])
        hidden_path = hugo.get("hidden_path", ctx.get("hidden_path", ""))
    except Exception:
        base_path = ctx.get("base_path", "")
        image_path = ctx.get("image_path", "")
        content_paths = ctx.get("content_paths", [ctx.get("content_path", "")])
        hidden_path = ctx.get("hidden_path", "")

    content_paths = [p for p in content_paths if p]

    if not base_path or not image_path or not content_paths:
        print(json.dumps({"success": False, "error": "Missing config: base_path, image_path, or content_paths"}))
        return

    if trigger == "manual":
        result = handle_manual(data, base_path, image_path, content_paths, hidden_path)
    else:
        result = {"success": True, "message": "Manual only plugin"}

    print(json.dumps(result))


if __name__ == "__main__":
    main()
