#!/usr/bin/env python3
"""Blog backup — create compressed backup of the Hugo blog.

Manual trigger: backup with options (include .git, keep N).
Cron "Weekly backup": auto-backup every Sunday, .git excluded, keep 5.
"""

import json
import sys
import os
import subprocess
import glob
from datetime import datetime


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
        return config.get("hugo_config", {}).get("base_path", "")
    except Exception:
        return ""


def result_toast(message):
    return {"success": False, "error": message}


def result_guide(title, body):
    return {
        "success": False,
        "error": title,
        "actions": [{
            "type": "show_result",
            "content": {"title": f"Blog Backup — {title}", "body": body},
        }],
    }


def format_size(size_bytes):
    """Human-readable file size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def cleanup_old_backups(backup_dir, keep):
    """Remove old backups, keeping only the most recent N."""
    if keep <= 0:
        return []

    pattern = os.path.join(backup_dir, "blog_*.tar.gz")
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    removed = []
    for f in files[keep:]:
        try:
            os.remove(f)
            removed.append(os.path.basename(f))
        except Exception:
            pass
    return removed


def do_backup(base_path, include_git, include_themes, keep):
    """Create tar.gz backup of base_path."""
    # Ensure backup directory exists
    backup_dir = os.path.expanduser("~/inn_backups")
    os.makedirs(backup_dir, exist_ok=True)

    # Generate filename
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H%M%S")
    filename = f"blog_{timestamp}.tar.gz"
    backup_path = os.path.join(backup_dir, filename)

    # Build tar command
    parent_dir = os.path.dirname(base_path.rstrip("/"))
    site_name = os.path.basename(base_path.rstrip("/"))

    tar_cmd = ["tar", "-czf", backup_path, "-C", parent_dir]

    if not include_git:
        tar_cmd.extend(["--exclude", f"{site_name}/.git"])

    if not include_themes:
        tar_cmd.extend(["--exclude", f"{site_name}/themes"])

    tar_cmd.append(site_name)

    # Execute
    result = subprocess.run(
        tar_cmd,
        capture_output=True, text=True,
        timeout=300,
    )
    if result.returncode != 0:
        return result_toast(f"Backup failed: {result.stderr.strip()}")

    # Get file size
    file_size = os.path.getsize(backup_path)

    # Cleanup old backups
    removed = cleanup_old_backups(backup_dir, keep)

    # List current backups
    pattern = os.path.join(backup_dir, "blog_*.tar.gz")
    existing = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    # Build report
    lines = []
    lines.append(f"Backup created successfully.")
    lines.append(f"")
    lines.append(f"  File:  {backup_path}")
    lines.append(f"  Size:  {format_size(file_size)}")
    lines.append(f"  .git:    {'included' if include_git else 'excluded'}")
    lines.append(f"  themes:  {'included' if include_themes else 'excluded'}")
    lines.append(f"")

    if removed:
        lines.append(f"Removed {len(removed)} old backup(s):")
        for r in removed:
            lines.append(f"  - {r}")
        lines.append(f"")

    lines.append(f"Current backups ({len(existing)}):")
    for f in existing:
        size = format_size(os.path.getsize(f))
        name = os.path.basename(f)
        marker = " <- new" if f == backup_path else ""
        lines.append(f"  {name}  ({size}){marker}")

    body = "\n".join(lines)

    # Build download items from current backups
    download_items = []
    for f in existing:
        download_items.append({
            "path": f,
            "filename": os.path.basename(f),
            "size": format_size(os.path.getsize(f)),
        })

    return {
        "success": True,
        "message": f"Backup: {filename} ({format_size(file_size)})",
        "actions": [
            {
                "type": "show_result",
                "content": {
                    "title": "Blog Backup",
                    "body": body,
                },
            },
            {
                "type": "download_files",
                "content": {
                    "items": download_items,
                },
            },
        ],
    }


def main():
    data = {}
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read())
        except Exception:
            pass

    trigger = data.get("trigger", "cron")

    base_path = get_base_path(data)

    if not base_path:
        config_example = json.dumps({
            "hugo_config": {
                "base_path": "/home/user/my-blog",
                "content_path": "posts",
                "image_path": "static",
            }
        }, indent=2)
        print(json.dumps(result_guide(
            "Configuration Required",
            "base_path not configured.\n\n"
            "Set hugo_config.base_path in ~/.inn_server_config.json:\n\n"
            f"{config_example}\n",
        )))
        return

    if not os.path.isdir(base_path):
        print(json.dumps(result_toast(f"Path not found: {base_path}")))
        return

    # Determine options
    if trigger == "manual":
        include_git = data.get("include_git", True)
        include_themes = data.get("include_themes", True)
        try:
            keep = int(data.get("keep", "5"))
        except (ValueError, TypeError):
            keep = 5
    else:
        # Cron defaults: full backup
        include_git = True
        include_themes = True
        keep = 5

    result = do_backup(base_path, include_git, include_themes, keep)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
