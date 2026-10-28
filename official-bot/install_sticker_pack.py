#!/usr/bin/env python3
"""Install the bundled, licensed pack without network access or QQ credentials."""
import argparse
import json
import groups as G
from core.files import confined_path
from core.sticker_pack import install


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="zwift", help="registered alias/openid; zwift may stage a legacy library before registration")
    parser.add_argument("--embed-credit-footer", action="store_true", help="optionally paint attribution onto images; provenance files are always retained")
    args = parser.parse_args()
    try:
        group = G.resolve(args.group)
    except G.ConfigError:
        if args.group != "zwift" or G.all_groups():
            raise
        destination = confined_path(G.GROUPS, "zwift/stickers")
    else:
        if not G.plugin_enabled(group, "sticker"):
            raise PermissionError("Group sticker plugin is disabled")
        destination = confined_path(G.group_dir(group), "stickers")
    print(json.dumps(install(destination, embed_credit_footer=args.embed_credit_footer), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
