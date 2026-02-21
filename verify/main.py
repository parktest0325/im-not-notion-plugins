#!/usr/bin/env python3
"""Verify — Image sync consistency verification plugin

Manual trigger: Full scan of all images and content(+hidden) files.

Detects:
1. Broken refs       — md references image that doesn't exist
2. Cross-references  — md references another file's image directory
3. Case mismatches   — ref path differs in case from actual file (Linux issue)
4. Orphan image dirs — image dir remains but corresponding md was deleted
5. Orphan images     — image files not referenced by any md
6. Duplicate images  — same filename exists in multiple directories
"""

import json
import sys
import os
import re


def load_server_config():
    config_path = os.path.expanduser("~/.inn_server_config.json")
    with open(config_path, "r") as f:
        return json.load(f)


def parse_image_refs(md_content):
    """Extract local image reference paths from markdown content."""
    pattern = r"!\[[^\]]*\]\(([^)]+)\)"
    refs = []
    for path in re.findall(pattern, md_content):
        if not path.startswith(("http://", "https://")):
            refs.append(path.strip())
    return refs


def find_md_files(directory):
    """Recursively find all .md files."""
    md_files = []
    if not os.path.isdir(directory):
        return md_files
    for dirpath, _, filenames in os.walk(directory):
        for f in filenames:
            if f.endswith(".md"):
                md_files.append(os.path.join(dirpath, f))
    return md_files


def find_image_files(directory):
    """Recursively find all files in directory."""
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


def handle_manual(data, base_path, image_path, content_path, hidden_path):
    verbose = data.get("verbose", False)

    image_root = os.path.join(base_path, image_path)
    content_dir = os.path.join(base_path, "content", content_path)
    hidden_dir = None
    if hidden_path:
        hidden_dir = os.path.join(base_path, "content", hidden_path, content_path)

    # ── 1. Collect all image files (relative to image_root) ──
    all_images = set()
    all_images_lower = {}  # lowercase -> set of actual paths
    for abs_path in find_image_files(image_root):
        rel = get_relative_path(abs_path, image_root)
        if rel:
            all_images.add(rel)
            all_images_lower.setdefault(rel.lower(), set()).add(rel)

    # ── 2. Collect all md files ──
    # Each entry: (display_path, full_rel, content)
    # full_rel: image directory prefix = md_rel (content_path is NOT included in image refs)
    md_entries = []
    for md_abs in find_md_files(content_dir):
        md_rel = get_relative_path(md_abs, content_dir)
        if not md_rel:
            continue
        full_rel = md_rel
        with open(md_abs, "r", encoding="utf-8") as f:
            content = f.read()
        md_entries.append((md_rel, full_rel, content))

    if hidden_dir and os.path.isdir(hidden_dir):
        for md_abs in find_md_files(hidden_dir):
            md_rel = get_relative_path(md_abs, hidden_dir)
            if not md_rel:
                continue
            full_rel = md_rel
            with open(md_abs, "r", encoding="utf-8") as f:
                content = f.read()
            md_entries.append((f"[H] {md_rel}", full_rel, content))

    # ── 3. Analyze references ──
    referenced_images = set()
    md_full_rels = set()

    broken_refs = []       # (display_path, ref)
    cross_refs = []        # (display_path, ref)
    case_mismatches = []   # (display_path, ref, actual_path)

    for display_path, full_rel, content in md_entries:
        md_full_rels.add(full_rel)
        expected_prefix = f"{full_rel}/"

        refs = parse_image_refs(content)
        for ref in refs:
            ref_clean = ref.lstrip("/")
            referenced_images.add(ref_clean)

            # Cross-reference: ref doesn't belong to this md's image dir
            if not ref_clean.startswith(expected_prefix):
                cross_refs.append((display_path, ref))

            # Broken ref / case mismatch
            if ref_clean not in all_images:
                lower_matches = all_images_lower.get(ref_clean.lower(), set())
                if lower_matches:
                    for actual in lower_matches:
                        case_mismatches.append((display_path, ref, actual))
                else:
                    broken_refs.append((display_path, ref))

    # ── 4. Orphan image dirs ──
    # Image dirs that have files but no corresponding md file
    image_dirs = set()
    for img in all_images:
        parent = os.path.dirname(img)
        if parent:
            image_dirs.add(parent)
    orphan_dirs = sorted(d for d in image_dirs if d not in md_full_rels)

    # Count files per orphan dir
    orphan_dir_counts = {}
    for d in orphan_dirs:
        count = sum(1 for img in all_images if os.path.dirname(img) == d)
        orphan_dir_counts[d] = count

    # ── 5. Orphan images ──
    # Not referenced by any md, excluding those already in orphan dirs
    orphan_dirs_set = set(orphan_dirs)
    orphan_images = []
    orphan_in_dirs = []
    for img in sorted(all_images - referenced_images):
        parent = os.path.dirname(img)
        if parent in orphan_dirs_set:
            orphan_in_dirs.append(img)
        else:
            orphan_images.append(img)

    # ── 6. Duplicate filenames ──
    filename_map = {}
    for img in all_images:
        name = os.path.basename(img)
        filename_map.setdefault(name, []).append(img)
    duplicates = {n: sorted(p) for n, p in filename_map.items() if len(p) > 1}

    # ── Build report ──
    issue_count = (
        len(broken_refs)
        + len(cross_refs)
        + len(case_mismatches)
        + len(orphan_dirs)
        + len(orphan_images)
        + len(duplicates)
    )

    lines = []
    lines.append(f"=== Image Sync Verification ===")
    lines.append(f"Images: {len(all_images)}  |  MD files: {len(md_entries)}  |  Issues: {issue_count}")
    lines.append("")

    # verbose: list all files
    if verbose:
        lines.append(f"--- All Images ({len(all_images)}) ---")
        for img in sorted(all_images):
            lines.append(f"  {img}")
        lines.append("")
        lines.append(f"--- All MD Files ({len(md_entries)}) ---")
        for display_path, full_rel, _ in md_entries:
            lines.append(f"  {display_path}  (images: {full_rel}/)")
        lines.append("")

    if issue_count == 0:
        lines.append("All images are properly synced!")
    else:
        # Critical issues first
        if broken_refs:
            lines.append(f"[X] Broken Refs ({len(broken_refs)})")
            lines.append(f"    Image referenced in md but file does not exist")
            for md, ref in broken_refs:
                lines.append(f"    {md}  ->  {ref}")
            lines.append("")

        if cross_refs:
            lines.append(f"[X] Cross-References ({len(cross_refs)})")
            lines.append(f"    MD file references another file's image directory")
            for md, ref in cross_refs:
                lines.append(f"    {md}  ->  {ref}")
            lines.append("")

        if case_mismatches:
            lines.append(f"[!] Case Mismatches ({len(case_mismatches)})")
            lines.append(f"    Ref path differs in case from actual file")
            for md, ref, actual in case_mismatches:
                lines.append(f"    {md}  ->  {ref}")
                lines.append(f"      actual: {actual}")
            lines.append("")

        if orphan_dirs:
            lines.append(f"[!] Orphan Image Dirs ({len(orphan_dirs)})")
            lines.append(f"    Image directory exists but corresponding md file was deleted")
            for d in orphan_dirs:
                lines.append(f"    {d}/  ({orphan_dir_counts[d]} files)")
            lines.append("")

        if orphan_images:
            lines.append(f"[i] Orphan Images ({len(orphan_images)})")
            lines.append(f"    Image exists but not referenced by its md file")
            for img in orphan_images:
                lines.append(f"    {img}")
            lines.append("")

        if duplicates:
            lines.append(f"[i] Duplicate Filenames ({len(duplicates)})")
            lines.append(f"    Same filename in multiple directories (possible copy leftover)")
            for name, paths in sorted(duplicates.items()):
                lines.append(f"    {name}:")
                for p in paths:
                    lines.append(f"      {p}")
            lines.append("")

    body = "\n".join(lines)

    return {
        "success": True,
        "message": f"{len(all_images)} images, {len(md_entries)} files, {issue_count} issues",
        "actions": [
            {
                "type": "show_result",
                "content": {
                    "title": "Image Sync Verification",
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
        hugo = config.get("hugo_config", {})
        base_path = hugo.get("base_path", ctx.get("base_path", ""))
        image_path = hugo.get("image_path", ctx.get("image_path", ""))
        content_path = hugo.get("content_path", ctx.get("content_path", ""))
        hidden_path = hugo.get("hidden_path", ctx.get("hidden_path", ""))
    except Exception:
        base_path = ctx.get("base_path", "")
        image_path = ctx.get("image_path", "")
        content_path = ctx.get("content_path", "")
        hidden_path = ctx.get("hidden_path", "")

    if not base_path or not image_path:
        print(json.dumps({"success": True, "message": "Skipped: missing config"}))
        return

    if trigger == "manual":
        result = handle_manual(data, base_path, image_path, content_path, hidden_path)
    else:
        result = {"success": True, "message": "Manual only plugin"}

    print(json.dumps(result))


if __name__ == "__main__":
    main()
