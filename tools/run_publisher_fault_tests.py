#!/usr/bin/env python3
"""Fail-closed publisher fault tests (temp dirs + local bare remotes only).

Never touches production remotes or production JSON. Synthetic fixtures only.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS = 0
FAIL = 0
RESULTS: list[tuple[str, str, str]] = []

PENDING_TS = "2026-09-15T01:36:00Z"
REMOTE_TS = "2026-09-14T01:00:00Z"
NEWER_TS = "2026-09-16T08:00:00Z"


def record(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def run_git(cwd: Path, args: list[str], *, check: bool = True, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_AUTHOR_NAME"] = "Publisher Test"
    env["GIT_AUTHOR_EMAIL"] = "publisher-test@example.invalid"
    env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
    env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {args} rc={proc.returncode} stderr={(proc.stderr or '')[:600]}"
        )
    return proc


def git_bare(bare: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return run_git(bare, ["--git-dir", str(bare), *args], check=check)


def remote_head(bare: Path) -> str:
    proc = git_bare(bare, ["rev-parse", "refs/heads/main"])
    return (proc.stdout or "").strip()


def remote_show(bare: Path, relpath: str) -> str | None:
    proc = git_bare(bare, ["show", f"refs/heads/main:{relpath}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def write_hook(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def copy_project_tools_and_web(ws: Path) -> None:
    tools = ws / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    for src in (ROOT / "tools").iterdir():
        if src.suffix in {".py", ".sh"} and src.is_file():
            shutil.copy2(src, tools / src.name)
    web_src = ROOT / "web"
    web_dst = ws / "web"
    shutil.copytree(web_src, web_dst, dirs_exist_ok=True)
    for name in ("index.html", "app.js", "styles.css"):
        src = ROOT / name
        if src.is_file() and not (web_dst / name).is_file():
            shutil.copy2(src, web_dst / name)
    vendor_src = ROOT / "web" / "vendor"
    if vendor_src.is_dir():
        shutil.copytree(vendor_src, web_dst / "vendor", dirs_exist_ok=True)
    (ws / "data" / "generations").mkdir(parents=True, exist_ok=True)
    (ws / "data" / "incoming").mkdir(parents=True, exist_ok=True)


REQUIRED_WEB_JSON = (
    "meta.json",
    "companies.json",
    "valuation.json",
    "revisions.json",
    "eps_history.json",
    "earnings.json",
    "watchlist.json",
    "alerts.json",
    "dashboard.json",
)


def _min_meta(snapshot_utc: str, *, publishable: bool = True, extra: dict | None = None) -> dict:
    meta = {
        "lastSuccessfulCollection": snapshot_utc,
        "qualityGate": {
            "status": "ok" if publishable else "reject",
            "publishable": publishable,
            "reason": None if publishable else "fixture_reject",
        },
        "schemaVersion": "1",
        "dataVersion": "fixture-data-version",
        "refreshVersion": "fixture-refresh-version",
        "appVersion": "fixture-app-version",
        "releaseVersion": "fixture-release-version",
        "buildId": "fixture-data-version",
    }
    if extra:
        meta.update(extra)
    return meta


def seed_web_payload(ws: Path, snapshot_utc: str = PENDING_TS, *, publishable: bool = True) -> None:
    data = ws / "web" / "data"
    data.mkdir(parents=True, exist_ok=True)
    meta = _min_meta(snapshot_utc, publishable=publishable)
    # Preserve exported production-shaped JSON when present (synthetic copy in tmp).
    for name in REQUIRED_WEB_JSON:
        dest = data / name
        src = ROOT / "web" / "data" / name
        if src.is_file() and name != "meta.json":
            shutil.copy2(src, dest)
        elif name == "meta.json":
            dest.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        elif name == "dashboard.json":
            dest.write_text(json.dumps({"meta": dict(meta), "companies": {}}, indent=2) + "\n", encoding="utf-8")
        elif not dest.is_file():
            dest.write_text("{}\n", encoding="utf-8")
    live_meta_path = data / "meta.json"
    live = json.loads(live_meta_path.read_text(encoding="utf-8"))
    live.update(meta)
    live_meta_path.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")


def seed_current(
    ws: Path,
    *,
    run_id: str = "run-publish-1",
    snapshot_utc: str = PENDING_TS,
    publishable: bool = True,
) -> Path:
    seed_web_payload(ws, snapshot_utc, publishable=publishable)
    gen = ws / "data" / "generations" / run_id
    gen_web = gen / "web" / "data"
    gen_web.mkdir(parents=True, exist_ok=True)
    live = ws / "web" / "data"
    for name in REQUIRED_WEB_JSON:
        shutil.copy2(live / name, gen_web / name)
    (gen / "generation_meta.json").write_text(
        json.dumps(
            {
                "runId": run_id,
                "snapshot_utc": snapshot_utc,
                "schemaVersion": "1",
                "runStatus": "committed",
                "materializationStatus": "success",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (ws / "data" / "CURRENT.json").write_text(
        json.dumps(
            {
                "runId": run_id,
                "generation": str(gen),
                "snapshot_utc": snapshot_utc,
                "committedAt": snapshot_utc,
                "schemaVersion": "1",
                "crashAtomic": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (ws / "data" / ".materialized_run_id").write_text(run_id + "\n", encoding="utf-8")
    return gen


def init_identity(repo: Path) -> None:
    run_git(repo, ["config", "user.email", "publisher-test@example.invalid"])
    run_git(repo, ["config", "user.name", "Publisher Test"])
    run_git(repo, ["config", "commit.gpgsign", "false"])
    run_git(repo, ["config", "init.defaultBranch", "main"])


def make_bare_and_clone(
    tmp: Path,
    *,
    remote_collection: str = REMOTE_TS,
    extra_files: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    bare = tmp / "remote.git"
    run_git(tmp, ["init", "--bare", "-b", "main", str(bare)])
    git_bare(bare, ["config", "receive.denyNonFastForwards", "true"])
    seed = tmp / "seed"
    run_git(tmp, ["clone", str(bare), str(seed)])
    init_identity(seed)
    (seed / "data").mkdir(parents=True, exist_ok=True)
    (seed / "index.html").write_text("<html><body>seed</body></html>\n", encoding="utf-8")
    (seed / "app.js").write_text("/* seed */\n", encoding="utf-8")
    (seed / "styles.css").write_text("/* seed */\n", encoding="utf-8")
    meta = _min_meta(remote_collection)
    (seed / "data" / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (seed / "data" / "companies.json").write_text("{}\n", encoding="utf-8")
    (seed / ".data-version").write_text("seed-hash\n", encoding="utf-8")
    if extra_files:
        for rel, content in extra_files.items():
            p = seed / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    run_git(seed, ["add", "-A"])
    run_git(seed, ["commit", "-m", "seed pages"])
    run_git(seed, ["push", "-u", "origin", "main"])
    clone = tmp / "site-repo"
    run_git(tmp, ["clone", str(bare), str(clone)])
    init_identity(clone)
    return bare, clone


def make_workspace(tmp: Path, clone: Path) -> Path:
    ws = tmp / "ws"
    ws.mkdir(parents=True)
    copy_project_tools_and_web(ws)
    site = ws / "site-repo"
    if site.exists():
        shutil.rmtree(site)
    shutil.copytree(clone, site)
    return ws


def run_publisher(ws: Path, extra_env: dict | None = None, args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["AI_EPS_ROOT"] = str(ws)
    env["PIPELINE_LOCK_HELD"] = "1"
    env["SKIP_EXPORT"] = "1"
    env["PUBLISH_PREBUILT"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["SKIP_CANONICAL_GIT_PERSIST"] = "1"
    env["SKIP_CANONICAL_GIT_PUSH"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(ws / "tools" / "publish_github_pages.sh"), *(args or [])],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def combined(proc: subprocess.CompletedProcess) -> str:
    return f"{proc.stdout or ''}\n{proc.stderr or ''}"


def site_version(ws: Path) -> str:
    p = ws / "site-repo" / ".data-version"
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


def assert_failure_frozen(
    *,
    proc: subprocess.CompletedProcess,
    bare: Path,
    ws: Path,
    head_before: str,
    version_before: str,
    remote_meta_before: str | None,
    needle: str,
) -> tuple[bool, str]:
    out = combined(proc)
    ok = proc.returncode != 0
    ok = ok and needle.lower() in out.lower()
    ok = ok and remote_head(bare) == head_before
    ok = ok and site_version(ws) == version_before
    ok = ok and remote_show(bare, "data/meta.json") == remote_meta_before
    ok = ok and "PUSHED" not in (proc.stdout or "")
    ok = ok and "NO_CHANGES" not in (proc.stdout or "")
    detail = f"rc={proc.returncode} head_same={remote_head(bare)==head_before} ver={site_version(ws)!r} err={out[-300:]}"
    return ok, detail


def test_missing_current(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_web_payload(ws)
    # Intentionally no CURRENT.json
    head = remote_head(bare)
    meta = remote_show(bare, "data/meta.json")
    ver = site_version(ws)
    proc = run_publisher(ws)
    ok, detail = assert_failure_frozen(
        proc=proc,
        bare=bare,
        ws=ws,
        head_before=head,
        version_before=ver,
        remote_meta_before=meta,
        needle="CURRENT.json missing",
    )
    record("publisher_missing_current_fails_closed_test", ok, detail)


def test_corrupt_current(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_web_payload(ws)
    (ws / "data" / "CURRENT.json").write_text("{not-json", encoding="utf-8")
    head = remote_head(bare)
    meta = remote_show(bare, "data/meta.json")
    ver = site_version(ws)
    proc = run_publisher(ws)
    ok, detail = assert_failure_frozen(
        proc=proc,
        bare=bare,
        ws=ws,
        head_before=head,
        version_before=ver,
        remote_meta_before=meta,
        needle="corrupt",
    )
    record("publisher_corrupt_current_fails_closed_test", ok, detail)


def test_dangling_current(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_web_payload(ws)
    (ws / "data" / "CURRENT.json").write_text(
        json.dumps({"runId": "does-not-exist", "generation": str(ws / "data" / "generations" / "does-not-exist")})
        + "\n",
        encoding="utf-8",
    )
    head = remote_head(bare)
    meta = remote_show(bare, "data/meta.json")
    ver = site_version(ws)
    proc = run_publisher(ws)
    ok, detail = assert_failure_frozen(
        proc=proc,
        bare=bare,
        ws=ws,
        head_before=head,
        version_before=ver,
        remote_meta_before=meta,
        needle="dangling",
    )
    record("publisher_dangling_current_fails_closed_test", ok, detail)


def test_generation_missing_files(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    gen = seed_current(ws)
    (gen / "web" / "data" / "companies.json").unlink()
    head = remote_head(bare)
    meta = remote_show(bare, "data/meta.json")
    ver = site_version(ws)
    proc = run_publisher(ws)
    ok, detail = assert_failure_frozen(
        proc=proc,
        bare=bare,
        ws=ws,
        head_before=head,
        version_before=ver,
        remote_meta_before=meta,
        needle="missing required files",
    )
    record("publisher_generation_missing_files_fails_closed_test", ok, detail)


def test_rematerialize_failure(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_current(ws)
    head = remote_head(bare)
    meta = remote_show(bare, "data/meta.json")
    ver = site_version(ws)
    proc = run_publisher(ws, extra_env={"FAULT_INJECT_PUBLISH_REMATERIALIZE": "1"})
    ok, detail = assert_failure_frozen(
        proc=proc,
        bare=bare,
        ws=ws,
        head_before=head,
        version_before=ver,
        remote_meta_before=meta,
        needle="rematerialization",
    )
    record("publisher_rematerialization_failure_fails_closed_test", ok, detail)


def test_commit_hook_fail(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_current(ws)
    hook = ws / "site-repo" / ".git" / "hooks" / "pre-commit"
    write_hook(
        hook,
        "#!/bin/sh\necho 'ERROR: git commit hook forced fail' >&2\nexit 1\n",
    )
    head = remote_head(bare)
    meta = remote_show(bare, "data/meta.json")
    ver = site_version(ws)
    proc = run_publisher(ws)
    ok, detail = assert_failure_frozen(
        proc=proc,
        bare=bare,
        ws=ws,
        head_before=head,
        version_before=ver,
        remote_meta_before=meta,
        needle="commit",
    )
    ok = ok and site_version(ws) != "would-advance"
    record("publisher_commit_hook_fail_no_version_advance_test", ok, detail)


def test_push_failure(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_current(ws)
    write_hook(
        bare / "hooks" / "pre-receive",
        "#!/bin/sh\necho 'ERROR: git push rejected by pre-receive' >&2\nexit 1\n",
    )
    head = remote_head(bare)
    meta = remote_show(bare, "data/meta.json")
    ver = site_version(ws)
    proc = run_publisher(ws)
    ok, detail = assert_failure_frozen(
        proc=proc,
        bare=bare,
        ws=ws,
        head_before=head,
        version_before=ver,
        remote_meta_before=meta,
        needle="push",
    )
    # Safe to re-run: hook still rejects, but workspace is not marked published.
    proc2 = run_publisher(ws)
    ok = ok and proc2.returncode != 0
    ok = ok and remote_head(bare) == head
    ok = ok and site_version(ws) == ver
    record("publisher_push_failure_safe_rerun_test", ok, detail)


def test_canonical_clone_pushes_first(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    # Canonical clone advances origin/main while site clone stays behind.
    canonical = tmp / "canonical"
    run_git(tmp, ["clone", str(bare), str(canonical)])
    init_identity(canonical)
    (canonical / "canonical-history.txt").write_text("canonical-commit\n", encoding="utf-8")
    run_git(canonical, ["add", "canonical-history.txt"])
    run_git(canonical, ["commit", "-m", "canonical history persist"])
    run_git(canonical, ["push", "origin", "main"])
    canonical_sha = remote_head(bare)
    ws = make_workspace(tmp, clone)  # behind clone copied before fetch
    # Confirm site clone is behind
    behind_head = run_git(ws / "site-repo", ["rev-parse", "HEAD"]).stdout.strip()
    seed_current(ws)
    proc = run_publisher(ws)
    out = combined(proc)
    ok = proc.returncode == 0
    ok = ok and "PUSHED" in (proc.stdout or "")
    ok = ok and remote_head(bare) != behind_head
    ok = ok and remote_show(bare, "canonical-history.txt") == "canonical-commit\n"
    # Publish commit is descendant of the canonical push (no lost remote commits).
    contains = git_bare(bare, ["merge-base", "--is-ancestor", canonical_sha, "refs/heads/main"], check=False)
    ok = ok and contains.returncode == 0
    ok = ok and behind_head != canonical_sha
    record(
        "publisher_rebuilds_from_latest_remote_after_canonical_push_test",
        ok,
        f"rc={proc.returncode} ancestor={contains.returncode} out={out[-200:]}",
    )


def test_concurrent_remote_update(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_current(ws)
    origin_url = run_git(ws / "site-repo", ["remote", "get-url", "origin"]).stdout.strip()
    hook = r"""#!/bin/sh
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE GIT_PREFIX
MARKER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MARKER="$MARKER_DIR/concurrent-done"
if [ -f "$MARKER" ]; then
  exit 0
fi
touch "$MARKER"
TMP=$(mktemp -d)
git clone "ORIGIN_URL" "$TMP/c" || exit 1
git -C "$TMP/c" config user.email concurrent@example.invalid
git -C "$TMP/c" config user.name "Concurrent Updater"
git -C "$TMP/c" config commit.gpgsign false
echo concurrent-update > "$TMP/c/concurrent.txt"
git -C "$TMP/c" add concurrent.txt
git -C "$TMP/c" commit -m concurrent-update >/dev/null
git -C "$TMP/c" push origin main >/dev/null
rm -rf "$TMP"
exit 0
"""
    write_hook(ws / "site-repo" / ".git" / "hooks" / "pre-push", hook.replace("ORIGIN_URL", origin_url))
    proc = run_publisher(ws, extra_env={"PUBLISH_RETRY_MAX": "3"})
    out = combined(proc)
    ok = proc.returncode == 0
    ok = ok and "PUSHED" in (proc.stdout or "")
    log = git_bare(bare, ["log", "--format=%s", "refs/heads/main"]).stdout
    ok = ok and "concurrent-update" in log
    ok = ok and remote_show(bare, "concurrent.txt") == "concurrent-update\n"
    count = int(git_bare(bare, ["rev-list", "--count", "refs/heads/main"]).stdout.strip() or "0")
    ok = ok and count >= 3
    record(
        "publisher_concurrent_remote_bounded_retry_test",
        ok,
        f"rc={proc.returncode} count={count} log={log!r} out={out[-240:]}",
    )


def test_older_pending_timestamp(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp, remote_collection=NEWER_TS)
    ws = make_workspace(tmp, clone)
    seed_current(ws, snapshot_utc=PENDING_TS)
    head = remote_head(bare)
    meta = remote_show(bare, "data/meta.json")
    ver = site_version(ws)
    proc = run_publisher(ws)
    ok, detail = assert_failure_frozen(
        proc=proc,
        bare=bare,
        ws=ws,
        head_before=head,
        version_before=ver,
        remote_meta_before=meta,
        needle="older",
    )
    record("publisher_older_collection_timestamp_blocks_test", ok, detail)


def test_malformed_timestamp(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_current(ws, snapshot_utc="not-a-timestamp")
    head = remote_head(bare)
    meta = remote_show(bare, "data/meta.json")
    ver = site_version(ws)
    proc = run_publisher(ws)
    ok, detail = assert_failure_frozen(
        proc=proc,
        bare=bare,
        ws=ws,
        head_before=head,
        version_before=ver,
        remote_meta_before=meta,
        needle="malformed timestamp",
    )
    record("publisher_malformed_timestamp_fails_closed_test", ok, detail)


def test_remote_verify_miss(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_current(ws)
    write_hook(
        bare / "hooks" / "post-receive",
        """#!/bin/sh
while read oldrev newrev refname; do
  if [ "$refname" = "refs/heads/main" ]; then
    git update-ref refs/heads/main "$oldrev"
  fi
done
""",
    )
    head = remote_head(bare)
    meta = remote_show(bare, "data/meta.json")
    ver = site_version(ws)
    proc = run_publisher(ws)
    out = combined(proc)
    ok = proc.returncode != 0
    ok = ok and ("not found on remote" in out.lower() or "verification failed" in out.lower())
    ok = ok and "PUSHED" not in (proc.stdout or "")
    ok = ok and remote_head(bare) == head
    ok = ok and site_version(ws) == ver
    ok = ok and remote_show(bare, "data/meta.json") == meta
    record(
        "publisher_remote_target_commit_missing_no_success_test",
        ok,
        f"rc={proc.returncode} head_same={remote_head(bare)==head} out={out[-300:]}",
    )


def test_idempotent_second_run(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_current(ws)
    proc1 = run_publisher(ws)
    head1 = remote_head(bare)
    ver1 = site_version(ws)
    ok = proc1.returncode == 0 and "PUSHED" in (proc1.stdout or "")
    proc2 = run_publisher(ws)
    out2 = combined(proc2)
    ok = ok and proc2.returncode == 0
    ok = ok and "NO_CHANGES" in (proc2.stdout or "")
    ok = ok and remote_head(bare) == head1
    ok = ok and site_version(ws) == ver1
    record(
        "publisher_second_run_idempotent_no_changes_test",
        ok,
        f"rc1={proc1.returncode} rc2={proc2.returncode} out2={out2[-200:]}",
    )


def test_fresh_clone_preflight(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    src = make_workspace(tmp, clone)
    seed_current(src)
    # Track only tools/web (no leftover site-repo/.data-version accidents).
    proj = tmp / "tracked"
    shutil.copytree(src / "tools", proj / "tools")
    shutil.copytree(src / "web", proj / "web")
    (proj / "data" / "generations").mkdir(parents=True)
    shutil.copytree(src / "data" / "generations", proj / "data" / "generations", dirs_exist_ok=True)
    shutil.copy2(src / "data" / "CURRENT.json", proj / "data" / "CURRENT.json")
    (proj / "data" / ".materialized_run_id").write_text(
        (src / "data" / ".materialized_run_id").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    run_git(proj, ["init", "-b", "main"])
    init_identity(proj)
    run_git(proj, ["add", "tools", "web"])
    run_git(proj, ["commit", "-m", "tracked source"])
    fresh = tmp / "fresh-clone"
    run_git(tmp, ["clone", str(proj), str(fresh)])
    # Pipeline workspace state (CURRENT generation) is explicit, not a leftover site-repo.
    shutil.copytree(proj / "data", fresh / "data", dirs_exist_ok=True)
    if (fresh / "site-repo").exists():
        shutil.rmtree(fresh / "site-repo")
    run_git(fresh, ["remote", "add", "pages", str(bare)], check=False)
    # Attach the pages remote as origin for publish by cloning site-repo from bare.
    run_git(fresh, ["clone", str(bare), str(fresh / "site-repo")])
    init_identity(fresh / "site-repo")
    # Preflight must pass without leftover .data-version in the source clone.
    assert not (fresh / ".data-version").exists()
    proc = run_publisher(fresh, args=["--preflight"])
    out = combined(proc)
    ok = proc.returncode == 0
    ok = ok and "PREFLIGHT_OK" in (proc.stdout or "")
    ok = ok and not (fresh / ".data-version").exists()
    porcelain = run_git(fresh, ["status", "--porcelain"]).stdout
    # CURRENT/generations are untracked by design; no site-repo accidents required.
    ok = ok and ".data-version" not in porcelain
    record(
        "publisher_fresh_clone_preflight_test",
        ok,
        f"rc={proc.returncode} porcelain={porcelain!r} out={out[-200:]}",
    )


def test_worktree_clean_after_run(tmp: Path) -> None:
    bare, clone = make_bare_and_clone(tmp)
    ws = make_workspace(tmp, clone)
    seed_current(ws)
    before_wts = run_git(ws / "site-repo", ["worktree", "list", "--porcelain"]).stdout
    proc = run_publisher(ws)
    after_wts = run_git(ws / "site-repo", ["worktree", "list", "--porcelain"]).stdout
    leftover = list(Path("/tmp").glob("ai-eps-publish-wt-*"))
    ok = proc.returncode == 0
    # Only the primary worktree remains.
    wt_paths = [ln for ln in after_wts.splitlines() if ln.startswith("worktree ")]
    ok = ok and len(wt_paths) == 1
    ok = ok and "ai-eps-publish-wt-" not in after_wts
    ok = ok and not leftover
    record(
        "publisher_git_worktree_clean_after_run_test",
        ok,
        f"rc={proc.returncode} wts={len(wt_paths)} leftover={leftover} before={before_wts!r}",
    )


def main() -> int:
    os.environ.setdefault("SKIP_CANONICAL_GIT_PERSIST", "1")
    print("=== ai-eps-monitor publisher fail-closed fault tests ===")
    tests = [
        test_missing_current,
        test_corrupt_current,
        test_dangling_current,
        test_generation_missing_files,
        test_rematerialize_failure,
        test_commit_hook_fail,
        test_push_failure,
        test_canonical_clone_pushes_first,
        test_concurrent_remote_update,
        test_older_pending_timestamp,
        test_malformed_timestamp,
        test_remote_verify_miss,
        test_idempotent_second_run,
        test_fresh_clone_preflight,
        test_worktree_clean_after_run,
    ]
    for fn in tests:
        with tempfile.TemporaryDirectory(prefix=f"ai_eps_pub_{fn.__name__}_") as td:
            try:
                fn(Path(td))
            except Exception as exc:  # noqa: BLE001
                record(fn.__name__ + "_test" if not fn.__name__.endswith("_test") else fn.__name__, False, f"EXC {type(exc).__name__}: {exc}")
                print(f"EXC in {fn.__name__}: {exc}")
    print()
    print(f"Passed: {PASS}  Failed: {FAIL}  count={PASS + FAIL}")
    for name, status, detail in RESULTS:
        print(f"  {status}: {name}" + (f" ({detail})" if detail else ""))
    print(f"SUITE_TIMING suite=publisher_fault pass={PASS} fail={FAIL} count={PASS + FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
