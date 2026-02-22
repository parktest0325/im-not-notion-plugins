#!/usr/bin/env python3
"""Deploy im-not-notion-theme + demo content to the Hugo site."""

import json
import sys
import os
import shutil


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


def result_toast(message, success=False):
    return {"success": success, "error": message} if not success else {"success": True, "message": message}


def copytree(src, dst):
    """Copy directory tree, overwriting existing files."""
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    for entry in os.scandir(src):
        s = entry.path
        d = os.path.join(dst, entry.name)
        if entry.is_dir(follow_symlinks=False):
            copytree(s, d)
        else:
            shutil.copy2(s, d)


def main():
    data = {}
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read())
        except Exception:
            pass

    base_path = get_base_path(data)
    if not base_path:
        print(json.dumps(result_toast("base_path not configured.")))
        return

    if not os.path.isdir(base_path):
        print(json.dumps(result_toast(f"Path not found: {base_path}")))
        return

    deploy_content = str(data.get("deploy_content", "true")).lower() == "true"

    # Plugin dir = where this script lives
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    theme_src = os.path.join(plugin_dir, "im-not-notion-theme")

    if not os.path.isdir(theme_src):
        print(json.dumps(result_toast(f"Theme not found: {theme_src}")))
        return

    actions_log = []

    # 1. Deploy theme to base_path/themes/im-not-notion-theme/
    theme_dst = os.path.join(base_path, "themes", "im-not-notion-theme")

    # Clean and overwrite theme directory
    if os.path.isdir(theme_dst):
        shutil.rmtree(theme_dst)
    os.makedirs(theme_dst, exist_ok=True)
    for entry in os.scandir(theme_src):
        if entry.name in ("exampleSite", ".git"):
            continue
        s = entry.path
        d = os.path.join(theme_dst, entry.name)
        if entry.is_dir(follow_symlinks=False):
            copytree(s, d)
        else:
            shutil.copy2(s, d)

    actions_log.append(f"Theme → {theme_dst}")

    # 2. Deploy demo content
    example_dir = os.path.join(theme_src, "exampleSite")
    if deploy_content and os.path.isdir(example_dir):
        content_src = os.path.join(example_dir, "content")
        content_dst = os.path.join(base_path, "content")

        # Rename old → copy new → delete old
        old_content = None
        if os.path.isdir(content_dst):
            old_content = content_dst + "_old"
            if os.path.exists(old_content):
                shutil.rmtree(old_content)
            os.rename(content_dst, old_content)

        shutil.copytree(content_src, content_dst)

        if old_content and os.path.isdir(old_content):
            shutil.rmtree(old_content)
        actions_log.append(f"Content → {content_dst}")

        # Copy hugo.toml (merge theme setting into existing, or copy if none)
        hugo_toml_src = os.path.join(example_dir, "hugo.toml")
        hugo_toml_dst = os.path.join(base_path, "hugo.toml")

        if os.path.isfile(hugo_toml_dst):
            # Existing config: patch theme line, keep everything else (baseURL, etc.)
            with open(hugo_toml_dst, "r") as f:
                lines = f.readlines()

            found = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("theme") and "=" in stripped:
                    lines[i] = 'theme = "im-not-notion-theme"\n'
                    found = True
                    break

            if not found:
                lines.insert(0, 'theme = "im-not-notion-theme"\n')

            # Also ensure menu entries exist for Blog/Projects
            has_menu = any("[menu]" in l or "[[menu." in l for l in lines)
            if not has_menu:
                lines.append("\n[menu]\n")
                lines.append('  [[menu.main]]\n    name = "Blog"\n    url = "/blog/"\n    weight = 1\n')
                lines.append('  [[menu.main]]\n    name = "Projects"\n    url = "/projects/"\n    weight = 2\n')

            with open(hugo_toml_dst, "w") as f:
                f.writelines(lines)
            actions_log.append("hugo.toml → theme + menu patched (baseURL preserved)")
        else:
            # No existing config: copy example but read baseURL from server config
            shutil.copy2(hugo_toml_src, hugo_toml_dst)
            try:
                config = load_server_config()
                domain = config.get("cms_config", {}).get("hugo_config", {}).get("base_url", "")
                if domain:
                    with open(hugo_toml_dst, "r") as f:
                        content = f.read()
                    import re
                    content = re.sub(
                        r'baseURL\s*=\s*"[^"]*"',
                        f'baseURL = "{domain}"',
                        content,
                    )
                    with open(hugo_toml_dst, "w") as f:
                        f.write(content)
                    actions_log.append(f"hugo.toml → new (baseURL = {domain})")
                else:
                    actions_log.append("hugo.toml → new (baseURL from example)")
            except Exception:
                actions_log.append("hugo.toml → new (baseURL from example)")

        # 3. Patch content_paths in server config to match demo sections
        demo_sections = []
        for entry in os.scandir(os.path.join(content_dst)):
            if entry.is_dir() and not entry.name.startswith("_"):
                demo_sections.append(entry.name)
        demo_sections.sort()

        if demo_sections:
            config_path = os.path.expanduser("~/.inn_server_config.json")
            try:
                config = load_server_config()
                hugo_cfg = config.get("cms_config", {}).get("hugo_config", {})
                old_paths = hugo_cfg.get("content_paths", [])
                hugo_cfg["content_paths"] = demo_sections
                with open(config_path, "w") as f:
                    json.dump(config, f, indent=2)
                actions_log.append(f"content_paths → {demo_sections} (was {old_paths})")
            except Exception as e:
                actions_log.append(f"content_paths patch failed: {e}")

    summary = "\n".join(f"  • {a}" for a in actions_log)
    print(json.dumps({
        "success": True,
        "message": "Theme deployed",
        "actions": [
            {"type": "toast", "content": {"message": "Theme deployed!", "toast_type": "success"}},
            {"type": "show_result", "content": {
                "title": "Deploy Theme — Complete",
                "body": f"Deployed to {base_path}\n\n{summary}",
            }},
            {"type": "refresh_tree"},
        ],
    }))


if __name__ == "__main__":
    main()
