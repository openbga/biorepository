"""Screenshots of every page, per role, size and theme, for before/after review.

Crawls the running development server from the navigation (and the record pages its lists link
to) for each demo role, opens every form that has a dialog from the button that opens it, and
writes PNGs to ``.local/shots/<side>/``. The "after" run replays the URLs the "before" run found,
so both sides show the same records. See README.md in this directory.

    uv run --group e2e python scripts/ui_compare/capture.py before
    uv run --group e2e python scripts/ui_compare/capture.py after
"""

import argparse
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SHOTS = REPO / ".local" / "shots"
BASE = os.environ.get("UI_COMPARE_BASE", "http://127.0.0.1:8020")
ROLES = ("demo_admin", "demo_manager", "demo_technician", "demo_viewer")  # synthetic demo accounts
WIDTHS = (1280, 1440)
NARROW = 768  # captured for the pages reached from the navigation only
HEIGHT = 900
THEMES = ("light", "dark")
MAX_PAGES = 400
BEFORE_TREE = REPO / ".local" / "ui-compare" / "before-tree"
BEFORE_PORT = 8027
SKIP = re.compile(
    r"^/(admin|static|media|accounts/logout|accounts/2fa/(?!$)|__debug__)"
    r"|/(export|download|labels?\.pdf)|\.(csv|pdf|png|jpe?g|zip|json|txt)$",
    re.I,
)
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ID_SEGMENT = re.compile(rf"^({UUID}|\d+)$", re.I)


# --------------------------------------------------------------------------------- helpers


def pattern(path: str) -> str:
    """The route shape of a path: record identifiers become <id>."""
    return "/".join("<id>" if ID_SEGMENT.match(part) else part for part in path.split("/"))


def page_key(url: str) -> str:
    """Stable file-name key for a URL: its route shape plus any query."""
    parts = urlsplit(url)
    key = pattern(parts.path).strip("/").replace("<id>", "id") or "home"
    if parts.query:
        key += "--" + parts.query
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", key).strip("_")


def settle(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightError:
        pass
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(250)


def set_theme(page: Page, theme: str) -> None:
    page.evaluate(
        """t => { localStorage.setItem('vr-theme', t);
                  document.documentElement.setAttribute('data-bs-theme', t);
                  document.documentElement.setAttribute('data-vr-theme-choice', t); }""",
        theme,
    )
    page.wait_for_timeout(120)


def login(browser, account: str):
    context = browser.new_context(
        base_url=BASE, viewport={"width": WIDTHS[-1], "height": HEIGHT}, reduced_motion="reduce"
    )
    page = context.new_page()
    page.goto("/accounts/login/")
    page.get_by_label(re.compile("^(Username or email|Login)")).fill(account)
    page.get_by_label("Password").fill(os.environ["DEMO_PASSWORD"])
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state()
    if "/accounts/login" in page.url:
        raise RuntimeError(f"sign-in failed for {account}")
    return context, page


def local_path(href: str, current: str) -> str | None:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    url = urldefrag(urljoin(current, href))[0]
    parts = urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}" != BASE or SKIP.search(parts.path):
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


# ----------------------------------------------------------------------------------- crawl


def crawl(account: str) -> dict:
    """Discover pages and dialog openers for one role.

    Pages: everything reachable from the navigation; query-string variants only when the
    navigation or home page links them; one record page per route shape.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        _context, page = login(browser, account)
        queue = [("/", 0)]
        seen_shapes, pages, dialogs, errors = set(), [], {}, []
        while queue and len(pages) < MAX_PAGES:
            path, depth = queue.pop(0)
            query = urlsplit(path).query
            shape = pattern(urlsplit(path).path) + (f"?{query}" if query else "")
            if shape in seen_shapes:
                continue
            seen_shapes.add(shape)
            try:
                response = page.goto(path)
            except PlaywrightError as exc:
                errors.append(f"{account} {path}: {str(exc).splitlines()[0]}")
                continue
            if response is None or "text/html" not in response.headers.get("content-type", ""):
                continue
            if response.status >= 400:
                errors.append(f"{account} {path}: HTTP {response.status}")
                continue
            if urlsplit(page.url).path != urlsplit(path).path:
                continue  # redirected (permission or flow): the target is crawled on its own
            routes = page.evaluate(
                "() => { const r = document.getElementById('vr-form-routes');"
                " return r ? JSON.parse(r.textContent).map(x => x.pattern) : []; }"
            )
            pages.append({"key": page_key(path), "path": path, "nav": depth <= 1})
            hrefs = page.eval_on_selector_all(
                "a[href]:not([download])",
                "els => els.map(e => [e.getAttribute('href'),"
                " !!e.closest('#vr-nav, body > header'), !!e.closest('main'),"
                " e.offsetParent !== null])",
            )
            for href, in_nav, in_main, visible in hrefs:
                target = local_path(href, page.url)
                if not target:
                    continue
                parts = urlsplit(target)
                if any(re.search(r, parts.path) for r in routes):
                    if in_main and visible and pattern(parts.path) not in dialogs:
                        dialogs[pattern(parts.path)] = {
                            "key": "dialog--" + page_key(target),
                            "from": path,
                            "href": href,
                            "path": target,
                        }
                    continue
                if parts.query and not (in_nav or depth == 0):
                    continue
                queue.append((target, 1 if in_nav else depth + 1))
        browser.close()
    return {"account": account, "pages": pages, "dialogs": list(dialogs.values()), "errors": errors}


# --------------------------------------------------------------------------------- capture


def shoot(page: Page, out: Path, key: str, widths, full: bool) -> int:
    count = 0
    for theme in THEMES:
        set_theme(page, theme)
        for width in widths:
            page.set_viewport_size({"width": width, "height": HEIGHT})
            page.wait_for_timeout(120)
            page.screenshot(path=out / f"{key}--{width}-{theme}.png", full_page=full)
            count += 1
    return count


def capture(job: tuple) -> dict:
    side, account, pages, dialogs = job
    out = SHOTS / side / account
    out.mkdir(parents=True, exist_ok=True)
    shots, errors = 0, []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        _context, page = login(browser, account)
        # Server errors seen while a page or dialog loads: such a view is recorded as URL and
        # status only and never screenshotted (a DEBUG error page shows the environment).
        failed = []
        page.on(
            "response",
            lambda r: (
                failed.append(f"HTTP {r.status} {urlsplit(r.url).path}")
                if r.status >= 500
                else None
            ),
        )
        for item in pages:
            try:
                failed.clear()
                response = page.goto(item["path"])
                if response is not None and response.status >= 400:
                    errors.append(f"{account} {item['path']}: HTTP {response.status}")
                    continue
                settle(page)
                if failed:  # a fragment or asset failed on the server: record it, never show it
                    errors.append(f"{account} {item['path']}: {', '.join(failed)}")
                    continue
                widths = WIDTHS + ((NARROW,) if item["nav"] else ())
                shots += shoot(page, out, item["key"], widths, full=True)
            except PlaywrightError as exc:
                errors.append(f"{account} {item['path']}: {str(exc).splitlines()[0]}")
        for item in dialogs:
            try:
                for theme in THEMES:
                    for width in WIDTHS:
                        page.set_viewport_size({"width": width, "height": HEIGHT})
                        page.goto(item["from"])
                        set_theme(page, theme)
                        page.reload()
                        settle(page)
                        failed.clear()
                        link = page.locator(f'main a[href="{item["href"]}"]:visible').first
                        link.click(timeout=5000)
                        page.locator("#vr-form-dialog.show").wait_for(timeout=10000)
                        settle(page)
                        if failed:
                            raise PlaywrightError(", ".join(failed))
                        page.screenshot(path=out / f"{item['key']}--{width}-{theme}.png")
                        shots += 1
            except PlaywrightError as exc:
                errors.append(
                    f"{account} dialog {item['path']} from {item['from']}: "
                    f"{str(exc).splitlines()[0]}"
                )
        browser.close()
    return {"shots": shots, "errors": errors}


# ------------------------------------------------------------------------- before snapshot

# Never handed to a spawned server: a DEBUG error page would print the process environment.
SECRET_NAMES = {"DEMO_PASSWORD", "DEMO_ADMIN_PASSWORD"}


def server_env() -> dict:
    """A scrubbed environment for a spawned server (like `env -i`): PATH and HOME for uv, plus
    the repository's .env settings minus the demo passwords. Nothing else of this shell leaks."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR", "USER") if k in os.environ}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if key not in SECRET_NAMES:
            env[key] = value.strip().strip('"').strip("'")
    return env


def serve_ref(ref: str) -> subprocess.Popen:
    """Export REF with git archive (no change to the repository) and serve it on BEFORE_PORT."""
    shutil.rmtree(BEFORE_TREE, ignore_errors=True)
    BEFORE_TREE.mkdir(parents=True)
    archive = subprocess.run(["git", "archive", ref], cwd=REPO, check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(BEFORE_TREE)], input=archive.stdout, check=True)
    env = server_env()
    quiet = {"cwd": BEFORE_TREE, "env": env, "check": True, "stdout": subprocess.DEVNULL}
    subprocess.run(["uv", "sync", "-q"], **quiet)
    manage = ["uv", "run", "python", "manage.py"]
    subprocess.run([*manage, "collectstatic", "--noinput", "-v0"], **quiet)
    server = subprocess.Popen(
        [*manage, "runserver", f"127.0.0.1:{BEFORE_PORT}", "--noreload"],
        cwd=BEFORE_TREE,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{BEFORE_PORT}/accounts/login/"
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=2)
            return server
        except OSError:
            if server.poll() is not None:
                break
            time.sleep(1)
    server.terminate()
    sys.exit(f"the {ref} snapshot did not start on port {BEFORE_PORT} (is the port free?)")


# ------------------------------------------------------------------------------------ main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("side", choices=("before", "after"))
    parser.add_argument("--roles", default=",".join(ROLES), help="comma-separated demo accounts")
    parser.add_argument("--workers", type=int, default=2, help="processes per role")
    parser.add_argument("--ref", help="before: serve this git ref from a snapshot on :8027")
    parser.add_argument("--recrawl", action="store_true", help="after: crawl again, not replay")
    args = parser.parse_args()
    if "DEMO_PASSWORD" not in os.environ:
        sys.exit("DEMO_PASSWORD is not set: run `set -a; . ./.env; set +a` first.")
    roles = [r for r in args.roles.split(",") if r]
    server = None
    if args.ref:
        if args.side != "before":
            sys.exit("--ref is for the before side")
        server = serve_ref(args.ref)
        os.environ["UI_COMPARE_BASE"] = f"http://127.0.0.1:{BEFORE_PORT}"  # read by the workers
    try:
        return run(args, roles)
    finally:
        if server:
            server.terminate()
            server.wait()


def run(args, roles) -> int:
    base = os.environ.get("UI_COMPARE_BASE", BASE)
    started = time.time()
    side_dir = SHOTS / args.side
    side_dir.mkdir(parents=True, exist_ok=True)

    before_manifest = SHOTS / "before" / "manifest.json"
    if args.side == "after" and before_manifest.exists() and not args.recrawl:
        replay = json.loads(before_manifest.read_text())["roles"]
        found = {r["account"]: r for r in replay if r["account"] in roles}
    else:
        with multiprocessing.Pool(len(roles)) as pool:
            found = {r["account"]: r for r in pool.map(crawl, roles)}
    print(
        "found:",
        "; ".join(
            f"{a} {len(r['pages'])} pages + {len(r['dialogs'])} dialogs" for a, r in found.items()
        ),
        flush=True,
    )

    jobs = []
    for account, result in found.items():
        n = max(1, args.workers)
        for i in range(n):
            jobs.append((args.side, account, result["pages"][i::n], result["dialogs"][i::n]))
    with multiprocessing.Pool(len(jobs)) as pool:
        results = pool.map(capture, jobs)

    errors = [e for r in [*found.values(), *results] for e in r["errors"]]
    manifest = {
        "side": args.side,
        "base": base,
        "ref": args.ref,
        "captured": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": round(time.time() - started),
        "widths": list(WIDTHS),
        "narrow": NARROW,
        "themes": list(THEMES),
        "shots": sum(r["shots"] for r in results),
        "roles": list(found.values()),
        "errors": sorted(errors),
    }
    (side_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"{manifest['shots']} screenshots in {manifest['seconds']} s → {side_dir}")
    for error in manifest["errors"]:
        print("  gap:", error)
    subprocess_build()
    return 1 if errors else 0


def subprocess_build() -> None:
    try:
        from build import main as build  # noqa: PLC0415 (sibling module, run as a script)
    except ImportError:
        return
    build()


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
