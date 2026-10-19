#!/usr/bin/env python3
"""Per-group knowledge base helper for MaxShenBi.

  kb.py --group zwift search "query" [--max 20] [--context 1]
  kb.py --group zwift sync            # git clone/pull repos from config.json into knowledge/repos/
  kb.py --group zwift list            # list docs + repos
  kb.py --group zwift add-doc PATH    # copy a md/txt/pdf into knowledge/

Only ever reads the given group's own folder — never another group's knowledge.
PDFs are converted with pdftotext into knowledge/.pdftext/ (cached by mtime) and searched as text.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))
import groups as G  # noqa: E402


def kdir(goid: str, cfg: dict) -> Path:
    gd = G.group_dir(goid)
    d = (gd / ((cfg.get("knowledge") or {}).get("docs_dir") or "knowledge/")).resolve()
    if not str(d).startswith(str(gd.resolve())):
        raise SystemExit("docs_dir must stay inside the group folder")
    d.mkdir(parents=True, exist_ok=True)
    return d


def pdf_cache(kd: Path) -> None:
    out = kd / ".pdftext"
    for pdf in kd.rglob("*.pdf"):
        if ".pdftext" in pdf.parts:
            continue
        txt = out / (str(pdf.relative_to(kd)) + ".txt")
        if txt.exists() and txt.stat().st_mtime >= pdf.stat().st_mtime:
            continue
        txt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=False)


def cmd_search(goid, cfg, args) -> int:
    kd = kdir(goid, cfg)
    pdf_cache(kd)
    terms = [args.query] + [t for t in args.query.split() if len(t) > 1 and t != args.query]
    rg = [
        "rg", "--json", "-i", "--max-count", "5", "-C", str(args.context),
        "--hidden", "-g", "!.git", "-g", "!*.png", "-g", "!*.jpg", "-g", "!*.gif", "-g", "!*.mp4",
        "-g", "!*.pdf", "-g", "!node_modules",
    ] + sum((["-e", t] for t in terms), []) + [str(kd)]
    p = subprocess.run(rg, capture_output=True, text=True)
    hits, shown = [], 0
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") not in ("match", "context"):
            continue
        d = ev["data"]
        path = Path(d["path"]["text"])
        rel = str(path.relative_to(kd))
        if rel.startswith(".pdftext/"):
            rel = rel[len(".pdftext/"):-4] + " (pdf)"
        hits.append({"file": rel, "line": d.get("line_number"), "match": ev["type"] == "match",
                     "text": (d.get("lines", {}).get("text") or "").rstrip()[:300]})
    out = []
    for h in hits:
        if h["match"]:
            shown += 1
            if shown > args.max:
                break
        out.append(h)
    if args.json:
        print(json.dumps({"group": cfg.get("name"), "query": args.query, "hits": out}, ensure_ascii=False, indent=1))
    else:
        print(f"# {cfg.get('name')} 知识库搜索: {args.query}  ({min(shown, args.max)} 条匹配)")
        last = None
        for h in out:
            if h["file"] != last:
                print(f"\n## {h['file']}")
                last = h["file"]
            print(f"{h['line']}{':' if h['match'] else '-'} {h['text']}")
    return 0 if shown else 1


def cmd_sync(goid, cfg, args) -> int:
    gd = G.group_dir(goid)
    rc = 0
    for r in (cfg.get("knowledge") or {}).get("repos") or []:
        url, br = r.get("url"), r.get("branch") or ""
        lp = (gd / (r.get("local_path") or f"knowledge/repos/{Path(url).stem}")).resolve()
        if not str(lp).startswith(str(gd.resolve())):
            print(f"skip {url}: local_path outside group folder"); rc = 1; continue
        if (lp / ".git").exists():
            cmd = ["git", "-C", str(lp), "pull", "--ff-only", "-q"]
        else:
            lp.parent.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "clone", "-q", "--depth", "1"] + (["-b", br] if br else []) + [url, str(lp)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        head = subprocess.run(["git", "-C", str(lp), "log", "-1", "--format=%h %cd %s", "--date=iso"], capture_output=True, text=True).stdout.strip()
        print(f"{'OK ' if p.returncode == 0 else 'ERR'} {url} -> {lp.relative_to(gd)}  {head} {p.stderr.strip()[:200]}")
        rc |= p.returncode
    return rc


def cmd_list(goid, cfg, args) -> int:
    kd = kdir(goid, cfg)
    print(f"# {cfg.get('name')} ({goid})  knowledge: {kd}")
    for f in sorted(kd.rglob("*")):
        if f.is_file() and ".git" not in f.parts and ".pdftext" not in f.parts and "repos" not in f.relative_to(kd).parts[:1]:
            print(f"doc  {f.relative_to(kd)}  {f.stat().st_size}B")
    for r in (cfg.get("knowledge") or {}).get("repos") or []:
        print(f"repo {r.get('url')} -> {r.get('local_path')}")
    return 0


def cmd_add_doc(goid, cfg, args) -> int:
    kd = kdir(goid, cfg)
    src = Path(args.path)
    dst = kd / src.name
    shutil.copy2(src, dst)
    print(f"added {dst}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", help="alias / name / QQ number / group_openid (default Zwift)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--max", type=int, default=20)
    s.add_argument("--context", type=int, default=1); s.add_argument("--json", action="store_true")
    sub.add_parser("sync"); sub.add_parser("list")
    a = sub.add_parser("add-doc"); a.add_argument("path")
    args = ap.parse_args()
    goid = G.resolve(args.group)
    cfg = G.load_config(goid)
    if cfg is None:
        raise SystemExit(f"group {goid} has no config.json")
    if not (cfg.get("features") or {}).get("knowledge", True):
        raise SystemExit(f"knowledge feature is off for {cfg.get('name')}")
    fn = {"search": cmd_search, "sync": cmd_sync, "list": cmd_list, "add-doc": cmd_add_doc}[args.cmd]
    sys.exit(fn(goid, cfg, args))


if __name__ == "__main__":
    main()
