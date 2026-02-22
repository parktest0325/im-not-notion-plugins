#!/usr/bin/env python3
"""verify-image-link — Image link consistency verification plugin

Manual trigger: Full scan of all images and content(+hidden) files.

Detects (5 categories):
1. External Refs    [X] — refs to other file's images or external URLs
2. Broken Links     [X] — link exists but image file is missing
3. Case Mismatches  [!] — ref path differs in case from actual file
4. Orphans          [!] — unreferenced images or dirs without corresponding md
5. Duplicates       [i] — same filename in multiple directories
"""

import json
import sys
import os
import re


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


def parse_all_image_refs(md_content):
    """Extract all image references (local + external URLs) from markdown.

    Returns:
        local_refs: list of local image paths (stripped of leading /)
        external_urls: list of http(s) URLs
    """
    local_refs = []
    external_urls = []

    # Markdown: ![...](path)
    for path in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md_content):
        path = path.strip()
        if path.startswith(("http://", "https://")):
            external_urls.append(path)
        else:
            local_refs.append(path.lstrip("/"))

    # HTML: <img src="path"> or <img src='path'>
    for src in re.findall(r"""<img\s[^>]*src\s*=\s*["']([^"']+)["']""", md_content):
        src = src.strip()
        if src.startswith(("http://", "https://")):
            external_urls.append(src)
        else:
            local_refs.append(src.lstrip("/"))

    return local_refs, external_urls


def handle_manual(data, base_path, image_path, content_paths, hidden_path):
    verbose = to_bool(data.get("verbose"), True)
    image_root = os.path.join(base_path, image_path)

    # ── 1. Collect all image files ──
    all_images = set()
    all_images_lower = {}  # lowercase -> set of actual paths
    section_images = set()  # images inside content_paths sections
    non_section_images = set()  # images outside content_paths sections
    section_prefixes = tuple(f"{s}/" for s in content_paths)

    for abs_path in find_image_files(image_root):
        rel = get_relative_path(abs_path, image_root)
        if rel:
            all_images.add(rel)
            all_images_lower.setdefault(rel.lower(), set()).add(rel)
            if rel.startswith(section_prefixes):
                section_images.add(rel)
            else:
                non_section_images.add(rel)

    # ── 2. Collect all md files ──
    md_entries = []  # (display_path, full_rel, local_refs, external_urls)

    for section in content_paths:
        content_dir = os.path.join(base_path, "content", section)
        for md_abs in find_md_files(content_dir):
            md_rel = get_relative_path(md_abs, content_dir)
            if not md_rel:
                continue
            full_rel = f"{section}/{md_rel}" if section else md_rel
            with open(md_abs, "r", encoding="utf-8") as f:
                content = f.read()
            local_refs, ext_urls = parse_all_image_refs(content)
            md_entries.append((full_rel, full_rel, local_refs, ext_urls))

        # Hidden files
        if hidden_path:
            hidden_dir = os.path.join(base_path, "content", hidden_path, section)
            if os.path.isdir(hidden_dir):
                for md_abs in find_md_files(hidden_dir):
                    md_rel = get_relative_path(md_abs, hidden_dir)
                    if not md_rel:
                        continue
                    full_rel = f"{section}/{md_rel}" if section else md_rel
                    with open(md_abs, "r", encoding="utf-8") as f:
                        content = f.read()
                    local_refs, ext_urls = parse_all_image_refs(content)
                    md_entries.append((f"[H] {full_rel}", full_rel, local_refs, ext_urls))

    # ── 3. Analyze ──
    referenced_images = set()
    md_full_rels = set()

    external_refs = []     # (display, ref_or_url, type)  type: "cross" | "url"
    broken_links = []      # (display, ref)
    case_mismatches = []   # (display, ref, actual)

    for display_path, full_rel, local_refs, ext_urls in md_entries:
        md_full_rels.add(full_rel)
        my_prefix = f"{full_rel}/"

        # External URLs → category 1
        for url in ext_urls:
            external_refs.append((display_path, url, "url"))

        # Local refs
        for ref_clean in local_refs:
            referenced_images.add(ref_clean)

            # Is it mine?
            if not ref_clean.startswith(my_prefix):
                external_refs.append((display_path, ref_clean, "cross"))

            # Broken or case mismatch?
            if ref_clean not in all_images:
                lower_matches = all_images_lower.get(ref_clean.lower(), set())
                if lower_matches:
                    for actual in lower_matches:
                        case_mismatches.append((display_path, ref_clean, actual))
                else:
                    broken_links.append((display_path, ref_clean))

    # ── 4. Orphans (section only — fix target) ──
    orphan_images = sorted(section_images - referenced_images)

    image_dirs = set()
    for img in section_images:
        parent = os.path.dirname(img)
        if parent:
            image_dirs.add(parent)

    orphan_dirs = []
    for d in sorted(image_dirs):
        if d not in md_full_rels:
            count = sum(1 for img in section_images if os.path.dirname(img) == d)
            orphan_dirs.append((d, count))

    # ── 5. Duplicates ──
    filename_map = {}
    for img in all_images:
        name = os.path.basename(img)
        filename_map.setdefault(name, []).append(img)
    duplicates = {n: sorted(p) for n, p in filename_map.items() if len(p) > 1}

    # ── Build report ──
    issue_count = (
        len(external_refs)
        + len(broken_links)
        + len(case_mismatches)
        + len(orphan_images) + len(orphan_dirs)
        + len(duplicates)
    )

    lines = []
    lines.append("=== Image Link Verification ===")
    lines.append(f"Images: {len(section_images)} (section) + {len(non_section_images)} (other)  |  "
                 f"MD files: {len(md_entries)}  |  Issues: {issue_count}")
    lines.append(f"Sections: {', '.join(content_paths)}")
    lines.append("")

    if verbose:
        # 접이식 블록: 섹션 이미지 목록
        detail = "\n".join(f"  {img}" for img in sorted(section_images))
        lines.append(f"{{{{copy:Section Images ({len(section_images)})}}}}")
        lines.append(detail)
        lines.append("{{/copy}}")
        lines.append("")

        # 접이식 블록: 전체 MD 파일 목록
        detail = "\n".join(f"  {dp}  (images: {fr}/)" for dp, fr, *_ in md_entries)
        lines.append(f"{{{{copy:All MD Files ({len(md_entries)})}}}}")
        lines.append(detail)
        lines.append("{{/copy}}")
        lines.append("")

    if non_section_images:
        lines.append(f"[i] Non-Section Files — {len(non_section_images)} (not managed by fix)")
        if verbose:
            detail = "\n".join(f"  {img}" for img in sorted(non_section_images))
            lines.append("{{copy:[i] Non-Section Files Details}}")
            lines.append(detail)
            lines.append("{{/copy}}")
        lines.append("")

    if issue_count == 0:
        lines.append("All image links are consistent!")
    else:
        if external_refs:
            cross = [(d, r) for d, r, t in external_refs if t == "cross"]
            urls = [(d, r) for d, r, t in external_refs if t == "url"]
            lines.append(f"[X] External Refs — {len(cross)} cross, {len(urls)} URLs")
            if verbose:
                detail_lines = []
                if cross:
                    detail_lines.append(f"Cross-references ({len(cross)}):")
                    for md, ref in cross:
                        detail_lines.append(f"  {md}  ->  {ref}")
                if urls:
                    detail_lines.append(f"External URLs ({len(urls)}):")
                    for md, url in urls:
                        detail_lines.append(f"  {md}  ->  {url}")
                lines.append(f"{{{{copy:[X] External Refs Details}}}}")
                lines.append("\n".join(detail_lines))
                lines.append("{{/copy}}")
            lines.append("")

        if broken_links:
            lines.append(f"[X] Broken Links — {len(broken_links)}")
            if verbose:
                detail = "\n".join(f"  {md}  ->  {ref}" for md, ref in broken_links)
                lines.append(f"{{{{copy:[X] Broken Links Details}}}}")
                lines.append(detail)
                lines.append("{{/copy}}")
            lines.append("")

        if case_mismatches:
            lines.append(f"[!] Case Mismatches — {len(case_mismatches)}")
            if verbose:
                detail_lines = []
                for md, ref, actual in case_mismatches:
                    detail_lines.append(f"  {md}  ->  {ref}")
                    detail_lines.append(f"    actual: {actual}")
                lines.append(f"{{{{copy:[!] Case Mismatches Details}}}}")
                lines.append("\n".join(detail_lines))
                lines.append("{{/copy}}")
            lines.append("")

        if orphan_images or orphan_dirs:
            lines.append(f"[!] Orphans — {len(orphan_dirs)} dirs, {len(orphan_images)} images")
            if verbose:
                detail_lines = []
                if orphan_dirs:
                    for d, count in orphan_dirs:
                        detail_lines.append(f"  dir: {d}/  ({count} files)")
                if orphan_images:
                    for img in orphan_images:
                        detail_lines.append(f"  img: {img}")
                lines.append(f"{{{{copy:[!] Orphans Details}}}}")
                lines.append("\n".join(detail_lines))
                lines.append("{{/copy}}")
            lines.append("")

        if duplicates:
            lines.append(f"[i] Duplicates — {len(duplicates)} filenames")
            if verbose:
                detail_lines = []
                for name, paths in sorted(duplicates.items()):
                    detail_lines.append(f"  {name}:")
                    for p in paths:
                        detail_lines.append(f"    {p}")
                lines.append(f"{{{{copy:[i] Duplicates Details}}}}")
                lines.append("\n".join(detail_lines))
                lines.append("{{/copy}}")
            lines.append("")

    body = "\n".join(lines)

    return {
        "success": True,
        "message": f"{len(all_images)} images, {len(md_entries)} files, {issue_count} issues",
        "actions": [
            {
                "type": "show_result",
                "content": {
                    "title": "Image Link Verification",
                    "body": body,
                },
            }
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
