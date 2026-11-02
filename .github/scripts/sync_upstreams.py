#!/usr/bin/env python3
"""Keep the pinned native components in step with their upstream releases.

``buildScript/builtins/versions.json`` is the only source of truth for what is
bundled in the APK and for the independently updatable component channels.  A
new upstream release therefore needs three things: refreshed pins, a bumped
channel revision (published revisions are immutable), and a rebuilt set of
signed packages.  This script performs the first two; the workflow builds and
publishes the third.

Examples::

    sync_upstreams.py --check --summary report.md --github-output "$GITHUB_OUTPUT"
    sync_upstreams.py --apply --components xray,snell
    sync_upstreams.py --self-test

Only components with a self-contained upstream artefact are updated
automatically.  The sing-box cores and the sing-snell adapter stay advisory:
they need Android patch rebases and adapter code review that a nightly job
cannot judge.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import http.client
import json
import os
import re
import socket
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "buildScript/builtins/versions.json"
ENV = ROOT / "buildScript/lib/core/get_source_env.sh"
PROTOCOL_DOC = ROOT / "docs/built-in-protocols.md"
XRAY_DOC = ROOT / "docs/vless-sudoku.md"
MIERU_DOC = ROOT / "docs/mieru-import.md"
CACHE = ROOT / "core-build/sync-cache"
API = "https://api.github.com"
ABIS = ("arm64-v8a", "armeabi-v7a", "x86", "x86_64")
MIHOMO_ARCH = {"arm64-v8a": "arm64-v8", "armeabi-v7a": "armv7", "x86": "386", "x86_64": "amd64"}
USER_AGENT = "Vbox-upstream-sync/1"


class ProbeError(RuntimeError):
    """Upstream answered, but not in a shape this project can pin."""


class NotFound(ProbeError):
    """The requested release, tag or file does not exist."""


# --------------------------------------------------------------------------
# upstream access
# --------------------------------------------------------------------------
class GitHub:
    def __init__(self, token: str | None = None, cache: Path = CACHE, attempts: int = 3):
        self.token = token or ""
        self.cache = cache
        self.attempts = attempts

    # -- low level ------------------------------------------------------
    def _open(self, url: str, auth: bool = True, accept: str = "application/vnd.github+json"):
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if accept.startswith("application/vnd.github"):
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    raise NotFound(f"404 {url}") from error
                if error.code in (403, 429):
                    remaining = error.headers.get("X-RateLimit-Remaining")
                    if remaining == "0":
                        reset = error.headers.get("X-RateLimit-Reset", "")
                        raise ProbeError(
                            f"GitHub API rate limit exhausted (resets {reset or 'later'}); "
                            "set GH_TOKEN for 5000 requests/hour"
                        ) from error
                    if attempt + 1 < self.attempts:
                        time.sleep(5 * (attempt + 1))
                        continue
                    raise ProbeError(f"GitHub refused {url}") from error
                if 500 <= error.code < 600 and attempt + 1 < self.attempts:
                    last = error
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ProbeError(f"HTTP {error.code} for {url}") from error
            except (urllib.error.URLError, TimeoutError, socket.timeout,
                    ConnectionError, http.client.HTTPException) as error:
                last = error
                if attempt + 1 < self.attempts:
                    time.sleep(2 * (attempt + 1))
        raise ProbeError(f"cannot reach {url}: {last}")

    def json(self, path: str) -> Any:
        url = path if path.startswith("https://") else API + path
        with self._open(url) as response:
            return json.loads(response.read())

    def text(self, url: str) -> str:
        with self._open(url, auth=False, accept="text/plain") as response:
            return response.read().decode("utf-8", "replace")

    def file_text(self, repo: str, path: str, ref: str) -> str:
        """Read one repository file through the API, with raw.githubusercontent as fallback."""
        try:
            data = self.json(f"/repos/{repo}/contents/{path}?ref={ref}")
            if data.get("encoding") == "base64" and data.get("content"):
                return base64.b64decode(data["content"]).decode("utf-8", "replace")
            raise ProbeError(f"{repo}/{path}@{ref}: unexpected contents response")
        except ProbeError as error:
            try:
                return self.text(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}")
            except ProbeError:
                raise error

    # -- GitHub helpers -------------------------------------------------
    def release(self, repo: str, prerelease: bool = False) -> dict:
        if not prerelease:
            return self.json(f"/repos/{repo}/releases/latest")
        releases = self.json(f"/repos/{repo}/releases?per_page=20")
        for item in releases:
            if item.get("prerelease"):
                return item
        raise NotFound(f"{repo}: no prerelease")

    def tag_commit(self, repo: str, tag: str) -> str:
        ref = self.json(f"/repos/{repo}/git/ref/tags/{urllib.parse.quote(tag, safe='')}")
        target = ref.get("object", {})
        # An annotated tag points at a tag object that must be dereferenced.
        if target.get("type") == "tag":
            target = self.json(f"/repos/{repo}/git/tags/{target['sha']}").get("object", {})
        sha = target.get("sha", "")
        if target.get("type") != "commit" or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ProbeError(f"{repo}@{tag}: tag does not resolve to a commit")
        return sha

    def head_commit(self, repo: str) -> dict:
        commits = self.json(f"/repos/{repo}/commits?per_page=1")
        if not commits:
            raise ProbeError(f"{repo}: empty repository")
        return commits[0]

    def digest(self, url: str) -> str:
        """SHA-256 of a public download, memoised because release assets are immutable."""
        key = hashlib.sha256(url.encode()).hexdigest()
        cached = self.cache / key
        if cached.is_file():
            value = cached.read_text().strip()
            if re.fullmatch(r"[0-9a-f]{64}", value):
                return value
        digest = hashlib.sha256()
        with self._open(url, auth=False, accept="application/octet-stream") as response:
            for chunk in iter(lambda: response.read(1 << 20), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(value + "\n")
        return value

    def asset_digest(self, asset: dict) -> str:
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            value = digest.split(":", 1)[1]
            if re.fullmatch(r"[0-9a-f]{64}", value):
                return value
        url = asset.get("browser_download_url")
        if not url:
            raise ProbeError(f"asset {asset.get('name')} has no download URL")
        return self.digest(url)


# --------------------------------------------------------------------------
# small pure helpers (covered by --self-test)
# --------------------------------------------------------------------------
def strip_v(tag: str) -> str:
    return tag[1:] if re.match(r"^v\d", tag) else tag


def version_key(text: str) -> tuple:
    """Comparable key for tags such as ``v1.19.33``, ``v154.0.8037.49-5`` or ``v1.15.0-alpha.13``."""
    value = strip_v(text.strip())
    main, _, rest = value.partition("+")[0].partition("-")
    numbers = tuple(int(part) if part.isdigit() else 0 for part in main.split("."))
    if not rest:
        return (numbers, 1, ())
    pre = []
    for part in rest.split("."):
        pre.append((0, int(part)) if part.isdigit() else (1, part))
    return (numbers, 0, tuple(pre))


def age_hours(published_at: str | None) -> float | None:
    if not published_at:
        return None
    try:
        then = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def assets_for_abis(release: dict, naming: Callable[[str], str]) -> dict:
    """Map every supported ABI to its release asset; fail loudly on a rename."""
    named = {asset["name"]: asset for asset in release.get("assets", [])}
    selected = {}
    missing = []
    for abi in ABIS:
        name = naming(abi)
        if name not in named:
            missing.append(name)
            continue
        selected[abi] = named[name]
    if missing:
        raise ProbeError("missing release assets: " + ", ".join(missing))
    return selected


def pin_assets(api: "GitHub", release: dict, naming: Callable[[str], str]) -> dict:
    assets = assets_for_abis(release, naming)
    return {abi: {"url": asset["browser_download_url"], "sha256": api.asset_digest(asset)}
            for abi, asset in assets.items()}


def sort_assets(pinned: dict) -> dict:
    return {abi: pinned[abi] for abi in ABIS if abi in pinned}


def differs(entry: dict, pin: dict) -> bool:
    return any(entry.get(key) != value for key, value in pin.items())


def go_directive_minor(text: str) -> tuple[int, int] | None:
    match = re.search(r"^\s*toolchain\s+go(\d+)\.(\d+)(?:\.(\d+))?\s*$", text, re.M)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"^\s*go\s+(\d+)\.(\d+)(?:\.(\d+))?\s*$", text, re.M)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def toolchain_for(go_mod: str, pinned: str) -> str:
    """Keep the proven toolchain unless the module has moved to a newer Go minor."""
    explicit = re.search(r"^\s*toolchain\s+(go\d+\.\d+(?:\.\d+)?)\s*$", go_mod, re.M)
    if explicit:
        return explicit.group(1)
    needed = go_directive_minor(go_mod)
    current = re.fullmatch(r"go(\d+)\.(\d+)(?:\.(\d+))?", pinned or "")
    if not needed or not current:
        return pinned
    if needed > (int(current.group(1)), int(current.group(2))):
        return f"go{needed[0]}.{needed[1]}.0"
    return pinned


# --------------------------------------------------------------------------
# component descriptions and probes
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    id: str
    title: str
    revision: str
    automatic: bool
    lock_key: str
    reason: str = ""


SPECS = {
    spec.id: spec
    for spec in (
        Spec("naive", "NaïveProxy", "naive:stable", True, "NaiveProxy"),
        Spec("snell", "Snell（mihomo）", "snell:stable", True, "Snell (mihomo)"),
        Spec("trojan-go", "Trojan-Go", "trojan-go:stable", True, "Trojan-Go"),
        Spec("mieru", "Mieru", "mieru:stable", True, "mieru"),
        Spec("xray", "Xray", "xray:stable", True, "xray"),
        Spec(
            "snell-preview",
            "Snell（sing-snell 测试渠道）",
            "snell:preview",
            False,
            "snell_preview",
            "构建字节来自 buildScript/snell-client/go.mod，需要同时更新 Go 依赖和适配器",
        ),
    )
}


def lock_entry(lock: dict, spec: Spec) -> dict:
    if spec.lock_key in lock and isinstance(lock[spec.lock_key], dict):
        return lock[spec.lock_key]
    for entry in lock.get("components", []):
        if entry.get("name") == spec.lock_key:
            return entry
    raise ProbeError(f"versions.json has no entry for {spec.title}")


@dataclass
class Probe:
    spec: Spec
    current: str
    latest: str
    pin: dict = field(default_factory=dict)
    changed: bool = False
    status: str = "current"
    detail: str = ""
    published_at: str | None = None
    error: str | None = None


def probe_naive(api: GitHub, lock: dict, min_age: float) -> Probe:
    release = api.release("klzgrad/naiveproxy")
    tag = release["tag_name"]
    pin = {
        "version": tag,
        "source": f"https://github.com/klzgrad/naiveproxy/releases/tag/{tag}",
        "assets": pin_assets(api, release, lambda abi: f"naiveproxy-plugin-{tag}-{abi}.apk"),
    }
    return finish(SPECS["naive"], lock, pin, release.get("published_at"), min_age)


def probe_mihomo(api: GitHub, lock: dict, min_age: float) -> Probe:
    release = api.release("MetaCubeX/mihomo")
    tag = release["tag_name"]
    pin = {
        "version": tag,
        "source": f"https://github.com/MetaCubeX/mihomo/releases/tag/{tag}",
        "assets": pin_assets(
            api, release, lambda abi: f"mihomo-android-{MIHOMO_ARCH[abi]}-{tag}.gz"
        ),
    }
    return finish(SPECS["snell"], lock, pin, release.get("published_at"), min_age)


def probe_trojan_go(api: GitHub, lock: dict, min_age: float) -> Probe:
    refs = api.json("/repos/SagerNet/SagerNet/git/matching-refs/tags/trojan-go-plugin-")
    tags = []
    for ref in refs:
        tag = ref.get("ref", "").rsplit("/", 1)[-1]
        if re.fullmatch(r"trojan-go-plugin-\d+\.\d+\.\d+", tag):
            tags.append(tag)
    if not tags:
        raise ProbeError("SagerNet/SagerNet publishes no trojan-go-plugin tag")
    tag = max(tags, key=version_key)
    version = tag[len("trojan-go-plugin-"):]
    release = api.json(f"/repos/SagerNet/SagerNet/releases/tags/{tag}")
    try:
        commit = api.tag_commit("p4gefau1t/trojan-go", f"v{version}")
        source = f"https://github.com/p4gefau1t/trojan-go/tree/{commit}"
    except ProbeError as error:
        # New SagerNet packaging without a resolvable upstream tag: the binaries
        # can still ship, but the provenance link must stay on the last good pin.
        print(f"warning: trojan-go {version} source unresolved: {error}", file=sys.stderr)
        source = lock_entry(lock, SPECS["trojan-go"]).get("source", "")
    pin = {
        "version": version,
        "source": source,
        "assets": pin_assets(api, release, lambda abi: f"{tag}-{abi}.apk"),
    }
    return finish(SPECS["trojan-go"], lock, pin, release.get("published_at"), min_age)


def probe_mieru(api: GitHub, lock: dict, min_age: float) -> Probe:
    release = api.release("enfein/mieru")
    tag = release["tag_name"]
    version = strip_v(tag)
    commit = api.tag_commit("enfein/mieru", tag)
    entry = lock_entry(lock, SPECS["mieru"])
    if (entry.get("version") == version and entry.get("commit") == commit
            and entry.get("url") and entry.get("sha256")):
        # Same tag and commit: the archive cannot have changed, so skip the download.
        pin = {key: entry[key] for key in ("version", "source", "commit", "url", "sha256") if key in entry}
    else:
        url = f"https://github.com/enfein/mieru/archive/{commit}.tar.gz"
        pin = {
            "version": version,
            "source": f"https://github.com/enfein/mieru/tree/{commit}",
            "commit": commit,
            "url": url,
            "sha256": api.digest(url),
        }
    return finish(SPECS["mieru"], lock, pin, release.get("published_at"), min_age)


def probe_xray(api: GitHub, lock: dict, min_age: float) -> Probe:
    release = api.release("XTLS/Xray-core")
    tag = release["tag_name"]
    version = strip_v(tag)
    commit = api.tag_commit("XTLS/Xray-core", tag)
    entry = lock_entry(lock, SPECS["xray"])
    if (entry.get("version") == version and entry.get("commit") == commit
            and entry.get("url") and entry.get("sha256") and entry.get("toolchain")):
        pin = {key: entry[key] for key in ("version", "source", "commit", "url", "sha256", "toolchain")
               if key in entry}
    else:
        url = f"https://codeload.github.com/XTLS/Xray-core/tar.gz/{commit}"
        pinned_toolchain = entry.get("toolchain", "")
        try:
            go_mod = api.file_text("XTLS/Xray-core", "go.mod", commit)
            toolchain = toolchain_for(go_mod, pinned_toolchain)
        except ProbeError as error:
            print(f"warning: xray go.mod unresolved, keeping {pinned_toolchain}: {error}", file=sys.stderr)
            toolchain = pinned_toolchain
        pin = {
            "version": version,
            "source": f"https://github.com/XTLS/Xray-core/tree/{commit}",
            "commit": commit,
            "url": url,
            "sha256": api.digest(url),
            "toolchain": toolchain,
        }
    return finish(SPECS["xray"], lock, pin, release.get("published_at"), min_age)


def probe_snell_preview(api: GitHub, lock: dict, min_age: float) -> Probe:
    entry = lock_entry(lock, SPECS["snell-preview"])
    head = api.head_commit("SagerNet/sing-snell")
    sha = head["sha"]
    go_mod = (ROOT / "buildScript/snell-client/go.mod").read_text()
    match = re.search(r"github\.com/sagernet/sing-snell\s+v\S+-([0-9a-f]{12})", go_mod) or re.search(
        r"github\.com/sagernet/sing-snell\s+v\S{0,40}[.-]([0-9a-f]{12})", go_mod
    )
    built = match.group(1) if match else ""
    changed = not built or not sha.startswith(built)
    detail = f"go.mod 固定 {built or '未知'}，上游 HEAD {sha[:12]}"
    if changed:
        detail += "；需更新 go.mod 并复核 Snell 适配器"
    return Probe(
        spec=SPECS["snell-preview"],
        current=entry.get("version", ""),
        latest=f"sing-snell {sha[:12]} / adapter 1",
        pin={},
        changed=changed,
        detail=detail,
        published_at=head.get("commit", {}).get("committer", {}).get("date"),
    )


PROBES: dict[str, Callable[[GitHub, dict, float], Probe]] = {
    "naive": probe_naive,
    "snell": probe_mihomo,
    "trojan-go": probe_trojan_go,
    "mieru": probe_mieru,
    "xray": probe_xray,
    "snell-preview": probe_snell_preview,
}


FIELD_LABELS = {
    "version": "版本",
    "source": "来源链接",
    "assets": "下载地址/哈希",
    "url": "源码包地址",
    "sha256": "源码包哈希",
    "commit": "源码提交",
    "toolchain": "Go 工具链",
}


def finish(spec: Spec, lock: dict, pin: dict, published_at: str | None, min_age: float) -> Probe:
    entry = lock_entry(lock, spec)
    changed = differs(entry, pin)
    current = entry.get("version", "")
    detail = ""
    if changed:
        fields = [FIELD_LABELS.get(key, key) for key in pin if entry.get(key) != pin[key]]
        detail = "变更：" + "、".join(fields)
    return Probe(
        spec=spec,
        current=current,
        latest=pin.get("version", current),
        pin=pin,
        changed=changed,
        detail=detail,
        published_at=published_at,
    )


def classify(probe: Probe, min_age: float) -> Probe:
    if probe.error:
        probe.status = "error"
    elif not probe.changed:
        probe.status = "current"
    elif not probe.spec.automatic:
        probe.status = "manual"
        if not probe.detail:
            probe.detail = probe.spec.reason
    else:
        age = age_hours(probe.published_at)
        if min_age > 0 and age is not None and age < min_age:
            probe.status = "soaking"
            probe.detail = f"上游发布 {age:.1f}h，观察期 {min_age:g}h 后自动同步；" + probe.detail
        else:
            probe.status = "update"
    return probe


# --------------------------------------------------------------------------
# writing the lock and the human documentation
# --------------------------------------------------------------------------
def apply_pin(lock: dict, spec: Spec, pin: dict) -> None:
    entry = lock_entry(lock, spec)
    for key, value in pin.items():
        entry[key] = copy.deepcopy(value)
    if "assets" in pin:
        entry["assets"] = sort_assets(entry["assets"])
    key = spec.revision
    lock.setdefault("revisions", {})[key] = int(lock["revisions"].get(key, 0)) + 1


def dump_lock(lock: dict) -> str:
    return json.dumps(lock, indent=2, ensure_ascii=False) + "\n"


def doc_row(lock: dict, component: str) -> str:
    if component == "trojan-go":
        trojan = lock_entry(lock, SPECS["trojan-go"])
        return (
            f"| Trojan-Go | {trojan['version']} | [源码提交]({trojan['source']})，GPL-3.0；"
            f"[SagerNet 打包源码](https://github.com/SagerNet/SagerNet/tree/trojan-go-plugin-"
            f"{trojan['version']}/plugin/trojan-go) |"
        )
    if component == "naive":
        naive = lock_entry(lock, SPECS["naive"])
        return (
            f"| NaïveProxy | {strip_v(naive['version'])} | "
            f"[源码与构建工作流](https://github.com/klzgrad/naiveproxy/tree/{naive['version']})，"
            "BSD-3-Clause 与 Chromium 第三方许可 |"
        )
    if component == "mieru":
        mieru = lock_entry(lock, SPECS["mieru"])
        return f"| Mieru | {mieru['version']} | [源码提交]({mieru['source']})，GPL-3.0 |"
    if component == "snell":
        mihomo = lock_entry(lock, SPECS["snell"])
        return (
            f"| mihomo（Snell） | {strip_v(mihomo['version'])} | "
            f"[源码与构建工作流](https://github.com/MetaCubeX/mihomo/tree/{mihomo['version']})，GPL-3.0 |"
        )
    if component == "snell-preview":
        snell = lock_entry(lock, SPECS["snell-preview"])
        version = snell["version"].removeprefix("sing-snell ")
        return (
            f"| sing-snell（Snell 测试渠道） | {version} | "
            f"[锁定的源码提交]({snell['source']})，GPL-3.0-or-later；"
            "本项目适配器位于 `buildScript/snell-client` |"
        )
    raise ProbeError(f"no documentation row for {component}")


def rewrite_doc(text: str, lock: dict, changed: Iterable[str]) -> tuple[str, bool]:
    changed = set(changed)
    patterns = {
        "trojan-go": r"(?m)^\| Trojan-Go \|.*$",
        "naive": r"(?m)^\| NaïveProxy \|.*$",
        "mieru": r"(?m)^\| Mieru \|.*$",
        "snell": r"(?m)^\| mihomo（Snell） \|.*$",
        "snell-preview": r"(?m)^\| sing-snell（Snell 测试渠道） \|.*$",
    }
    before = text
    for component in changed:
        if component in patterns:
            text = re.sub(patterns[component], lambda _match, c=component: doc_row(lock, c), text)
    return text, text != before


def rewrite_xray_doc(text: str, entry: dict) -> str:
    text = re.sub(r"(bundled Xray )\d[^\s]*", lambda m: m.group(1) + entry["version"], text)
    toolchain = entry.get("toolchain", "")
    if toolchain.startswith("go"):
        text = re.sub(r"(using Go )\d[^\s]*", lambda m: m.group(1) + toolchain[2:], text)
    return text


def rewrite_mieru_doc(text: str, entry: dict) -> str:
    text = re.sub(r"(pinned Mieru )\d[^\s]*", lambda m: m.group(1) + entry["version"], text)
    text = re.sub(r"(mieru/blob/v)[^/]+/", lambda m: m.group(1) + entry["version"] + "/", text)
    return text


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def write_if_changed(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)
    return True


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
LABELS = {
    "update": "可更新",
    "soaking": "观察期",
    "manual": "需人工",
    "current": "已是最新",
    "error": "检查失败",
}


def summary_markdown(probes: list[Probe], advisories: list[str], applied: bool) -> str:
    lines = [
        "### 上游组件同步" if not applied else "### 已同步的上游组件",
        "",
        "| 组件 | 锁定版本 | 上游版本 | 结果 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for probe in probes:
        detail = (probe.error or probe.detail or "").replace("|", "\\|")
        lines.append(
            f"| {probe.spec.title} | `{probe.current}` | `{probe.latest}` | "
            f"{LABELS[probe.status]} | {detail} |"
        )
    manual = [p for p in probes if p.status == "manual"]
    if manual:
        lines += ["", "需人工处理的组件："]
        lines += [f"- **{p.spec.title}**：{p.detail}" for p in manual]
    if advisories:
        lines += ["", "内核提示（需按 `docs/core-updates.md` 重新适配补丁后发布）："]
        lines += [f"- {item}" for item in advisories]
    return "\n".join(lines) + "\n"


def text_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def pad(text: str, size: int) -> str:
    return text + " " * max(0, size - text_width(text))


def print_table(probes: list[Probe]) -> None:
    widths = [
        max([text_width(p.spec.title) for p in probes] + [len("component")]),
        max([text_width(p.current) for p in probes] + [len("current")]),
        max([text_width(p.latest) for p in probes] + [len("upstream")]),
    ]
    print(f"{pad('component', widths[0])}  {pad('current', widths[1])}  {pad('upstream', widths[2])}  status")
    for probe in probes:
        print(
            f"{pad(probe.spec.title, widths[0])}  {pad(probe.current, widths[1])}  "
            f"{pad(probe.latest, widths[2])}  {LABELS[probe.status]}"
        )
        if probe.error:
            print(f"{'':{widths[0]}}  ! {probe.error}")
        elif probe.detail:
            print(f"{'':{widths[0]}}  - {probe.detail}")


# --------------------------------------------------------------------------
# core advisories (never auto-applied)
# --------------------------------------------------------------------------
def core_pins(text: str) -> dict[str, str]:
    """Read one CORE_VERSION per channel; a plain findall would mix the two blocks."""
    versions = re.findall(r'export CORE_VERSION="([^"]+)"', text)
    if len(versions) != 2:
        raise ProbeError("get_source_env.sh must pin exactly two CORE_VERSION values")
    return {"stable": versions[0], "preview": versions[1]}


def core_advisories(api: GitHub) -> list[str]:
    lines: list[str] = []
    try:
        pinned = core_pins(ENV.read_text(encoding="utf-8"))
    except ProbeError as error:
        return [f"内核固定版本无法读取：{error}"]

    try:
        latest_stable = strip_v(api.release("SagerNet/sing-box")["tag_name"])
        if version_key(latest_stable) > version_key(pinned["stable"]):
            lines.append(
                f"sing-box 稳定版上游 {latest_stable} 可用，项目锁定 {pinned['stable']}"
                "（MatsuriDayo Android 分支，需要先同步补丁再发布）"
            )
    except ProbeError as error:
        lines.append(f"sing-box 稳定版检查失败：{error}")

    try:
        newest = max(
            (item["tag_name"] for item in api.json("/repos/SagerNet/sing-box/releases?per_page=30")
             if item.get("prerelease")),
            key=version_key,
            default=None,
        )
        if newest and version_key(newest) > version_key(pinned["preview"]):
            lines.append(f"sing-box 测试版上游 {strip_v(newest)} 可用，项目锁定 {pinned['preview']}")
    except ProbeError as error:
        lines.append(f"sing-box 测试版检查失败：{error}")
    return lines


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def selected_ids(value: str | None, automatic_only: bool = False) -> list[str]:
    requested = [item.strip() for item in (value or "").split(",") if item.strip()]
    if not requested:
        return [cid for cid, spec in SPECS.items() if spec.automatic or not automatic_only]
    unknown = [item for item in requested if item not in SPECS]
    if unknown:
        raise SystemExit(f"unknown component(s): {', '.join(unknown)}")
    return requested


def collect(api: GitHub, lock: dict, ids: list[str], min_age: float) -> list[Probe]:
    probes = []
    for cid in ids:
        try:
            probes.append(classify(PROBES[cid](api, lock, min_age), min_age))
        except ProbeError as error:
            spec = SPECS[cid]
            probes.append(
                Probe(
                    spec=spec,
                    current=lock_entry(lock, spec).get("version", ""),
                    latest="—",
                    status="error",
                    error=str(error),
                )
            )
    return probes


def write_outputs(args, probes: list[Probe]) -> None:
    changed = [p.spec.id for p in probes if p.status == "update"]
    errors = [p.spec.id for p in probes if p.status == "error"]
    # Snell has two channels sharing one published component name, so the
    # workflow needs the revision key (component:channel) rather than the id.
    publish = [p.spec.revision for p in probes if p.status == "update"]
    destination = getattr(args, "github_output", None)
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(f"changed={','.join(changed)}\n")
            handle.write(f"errors={','.join(errors)}\n")
            handle.write(f"publish={','.join(publish)}\n")


def command_check(args) -> int:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    api = GitHub(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    ids = selected_ids(args.components)
    probes = collect(api, lock, ids, args.min_age_hours)
    advisories = core_advisories(api)
    print_table(probes)
    if advisories:
        print("\ncore advisories:")
        for line in advisories:
            print(f"  - {line}")
    summary = summary_markdown(probes, advisories, applied=False)
    if args.summary:
        write_if_changed(Path(args.summary), summary)
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "changed": [p.spec.id for p in probes if p.status == "update"],
                    "errors": {p.spec.id: p.error for p in probes if p.status == "error"},
                    "probes": [
                        {
                            "id": p.spec.id,
                            "title": p.spec.title,
                            "current": p.current,
                            "latest": p.latest,
                            "status": p.status,
                            "detail": p.detail,
                        }
                        for p in probes
                    ],
                    "advisories": advisories,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    write_outputs(args, probes)
    if args.strict and any(p.status == "error" for p in probes):
        return 1
    return 0


def command_apply(args) -> int:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    before = dump_lock(lock)
    api = GitHub(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    ids = selected_ids(args.components, automatic_only=True)
    probes = collect(api, lock, ids, args.min_age_hours)
    failed = [p for p in probes if p.status == "error"]
    if failed:
        print_table(probes)
        raise SystemExit("refusing to rewrite pins while upstream checks fail")
    ready = [p for p in probes if p.status == "update" and p.pin]
    if not ready:
        print("no component needs a pin update")
        return 0

    for probe in ready:
        apply_pin(lock, probe.spec, probe.pin)
    written = [display_path(LOCK)] if write_if_changed(LOCK, dump_lock(lock)) else []

    changed_ids = [p.spec.id for p in ready]
    doc = PROTOCOL_DOC.read_text(encoding="utf-8")
    doc, doc_changed = rewrite_doc(doc, lock, changed_ids)
    if doc_changed:
        write_if_changed(PROTOCOL_DOC, doc)
        written.append(display_path(PROTOCOL_DOC))
    if "xray" in changed_ids:
        text = XRAY_DOC.read_text(encoding="utf-8")
        updated = rewrite_xray_doc(text, lock_entry(lock, SPECS["xray"]))
        if write_if_changed(XRAY_DOC, updated):
            written.append(display_path(XRAY_DOC))
    if "mieru" in changed_ids:
        text = MIERU_DOC.read_text(encoding="utf-8")
        updated = rewrite_mieru_doc(text, lock_entry(lock, SPECS["mieru"]))
        if write_if_changed(MIERU_DOC, updated):
            written.append(display_path(MIERU_DOC))

    summary = summary_markdown(probes, [], applied=True)
    if args.summary:
        write_if_changed(Path(args.summary), summary)
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "applied": [
                        {
                            "id": p.spec.id,
                            "from": p.current,
                            "to": p.latest,
                            "revision": lock["revisions"][p.spec.revision],
                        }
                        for p in ready
                    ],
                    "files": written,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    print_table(probes)
    print("\nupdated: " + ", ".join(f"{p.spec.id} {p.current} -> {p.latest}" for p in ready))
    print("files: " + ", ".join(written))
    # Hand the exact channels that were rewritten to the publisher; anything
    # upstream released while this job was running is simply left for the next run.
    write_outputs(
        argparse.Namespace(github_output=getattr(args, "github_output", None)),
        [
            Probe(spec=p.spec, current=p.current, latest=p.latest, pin=p.pin,
                  changed=True, status="update")
            for p in ready
        ],
    )
    assert dump_lock(lock) != before
    return 0


def command_self_test(_args) -> int:
    global LOCK, PROTOCOL_DOC, XRAY_DOC, MIERU_DOC
    assert version_key("v1.19.33") > version_key("v1.19.32")
    assert version_key("v1.15.0-alpha.13") > version_key("v1.15.0-alpha.2")
    assert version_key("v1.15.0") > version_key("v1.15.0-alpha.13")
    assert version_key("v154.0.8037.49-5") > version_key("v154.0.8037.49-4")
    assert version_key("v154.0.8037.49-5") > version_key("v154.0.8037.48-9")
    assert version_key("v26.10.31") > version_key("v26.9.30")
    assert strip_v("v3.39.0") == "3.39.0" and strip_v("0.10.6") == "0.10.6"

    release = {
        "assets": [
            {
                "name": f"mihomo-android-{arch}-v9.9.9.gz",
                "browser_download_url": f"https://x/{arch}",
                "digest": "sha256:" + "a" * 64,
            }
            for arch in MIHOMO_ARCH.values()
        ]
    }
    assets = assets_for_abis(release, lambda abi: f"mihomo-android-{MIHOMO_ARCH[abi]}-v9.9.9.gz")
    assert list(assets) == list(ABIS)
    try:
        assets_for_abis(release, lambda abi: f"mihomo-android-{abi}-v9.9.9.gz")
    except ProbeError as error:
        assert "missing release assets" in str(error)
    else:
        raise AssertionError("unexpected ABI name should fail")
    assert list(sort_assets({abi: {} for abi in reversed(ABIS)})) == list(ABIS)

    class StubApi:
        def asset_digest(self, asset):
            return asset["digest"].split(":", 1)[1]

    pinned = pin_assets(StubApi(), release, lambda abi: f"mihomo-android-{MIHOMO_ARCH[abi]}-v9.9.9.gz")
    assert pinned["arm64-v8a"] == {"url": "https://x/arm64-v8", "sha256": "a" * 64}

    fresh = Probe(spec=SPECS["xray"], current="1", latest="2", changed=True,
                  published_at=datetime.now(timezone.utc).isoformat())
    assert classify(Probe(**{**fresh.__dict__}), 24).status == "soaking"
    assert classify(Probe(**{**fresh.__dict__}), 0).status == "update"

    doc = "These nodes use a bundled Xray 1.0 executable, using Go 1.25.0 and the NDK."
    updated = rewrite_xray_doc(doc, {"version": "26.10.31", "toolchain": "go1.27.3"})
    assert "bundled Xray 26.10.31" in updated and "using Go 1.27.3" in updated
    mieru = "pinned Mieru 1.0.0 core\nhttps://github.com/enfein/mieru/blob/v1.0.0/pkg/appctl/url.go"
    updated = rewrite_mieru_doc(mieru, {"version": "3.39.0"})
    assert "pinned Mieru 3.39.0" in updated and "blob/v3.39.0/" in updated

    assert toolchain_for("go 1.27\n", "go1.27.3") == "go1.27.3"
    assert toolchain_for("go 1.28\n", "go1.27.3") == "go1.28.0"
    assert toolchain_for("go 1.25.0\ntoolchain go1.26.1\n", "go1.25") == "go1.26.1"

    lock = {
        "components": [{"name": "NaiveProxy", "version": "1"}],
        "xray": {"version": "1", "assets": {}},
        "revisions": {"xray:stable": 4},
    }
    pin = {"version": "2", "source": "s", "commit": "c", "url": "u", "sha256": "h", "toolchain": "go1.27.3"}
    entry = lock_entry(lock, SPECS["xray"])
    assert differs(entry, pin)
    apply_pin(lock, SPECS["xray"], pin)
    assert entry["version"] == "2" and lock["revisions"]["xray:stable"] == 5
    assert not differs(entry, pin)
    first = dump_lock(lock)
    apply_pin(lock, SPECS["xray"], pin)
    assert lock["revisions"]["xray:stable"] == 6 and dump_lock(lock) != first

    binary_lock = {
        "components": [{"name": "NaiveProxy", "version": "v1", "assets": {"x86": {"url": "old", "sha256": "old"}}}],
        "revisions": {"naive:stable": 1},
    }
    binary_pin = {
        "version": "v2",
        "source": "https://example/v2",
        "assets": {abi: {"url": f"https://example/{abi}", "sha256": abi * 2} for abi in reversed(ABIS)},
    }
    binary_entry = lock_entry(binary_lock, SPECS["naive"])
    assert differs(binary_entry, binary_pin)
    apply_pin(binary_lock, SPECS["naive"], binary_pin)
    assert list(binary_entry["assets"]) == list(ABIS)
    assert binary_lock["revisions"]["naive:stable"] == 2
    binary_pin["assets"]["x86"]["sha256"] = "0" * 12
    assert differs(binary_entry, binary_pin)

    doc = "| Mieru | 1.0.0 | old |\n| Xray | 1 | old |\n"
    updated, changed = rewrite_doc(doc, {"mieru": {"version": "3.39.0", "source": "https://s"}}, ["mieru"])
    assert changed and "| Mieru | 3.39.0 | [源码提交](https://s)，GPL-3.0 |" in updated
    assert "| Xray | 1 | old |" in updated

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "doc.md"
        path.write_text("a", encoding="utf-8")
        assert write_if_changed(path, "b") and path.read_text(encoding="utf-8") == "b"
        assert not write_if_changed(path, "b")

    # Unchanged source components must not re-download their archives.
    real_lock = json.loads(LOCK.read_text(encoding="utf-8"))

    class QuietApi:
        def __init__(self, repo, tag, commit):
            self.repo, self.tag, self.commit = repo, tag, commit

        def release(self, repo, prerelease=False):
            return {"tag_name": self.tag, "published_at": None}

        def tag_commit(self, repo, tag):
            return self.commit

        def digest(self, url):
            raise AssertionError(f"unexpected download of {url}")

        def file_text(self, repo, path, ref):
            raise AssertionError("unexpected go.mod read")

    assert not probe_mieru(QuietApi("enfein/mieru", "v" + real_lock["mieru"]["version"],
                                     real_lock["mieru"]["commit"]), real_lock, 24).changed
    assert not probe_xray(QuietApi("XTLS/Xray-core", "v" + real_lock["xray"]["version"],
                                   real_lock["xray"]["commit"]), real_lock, 24).changed

    # The publish step must address Snell's two channels by their release names
    # (component:channel), not by the component ids used in the lock.
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "github-output.txt"
        write_outputs(
            argparse.Namespace(github_output=str(output)),
            [
                Probe(spec=SPECS["snell-preview"], current="1", latest="2", changed=True, status="update"),
                Probe(spec=SPECS["snell"], current="1", latest="1", changed=False, status="current"),
                Probe(spec=SPECS["xray"], current="1", latest="2", changed=True, status="update"),
                Probe(spec=SPECS["mieru"], current="1", latest="—", status="error", error="boom"),
            ],
        )
        written = output.read_text(encoding="utf-8").splitlines()
        assert written == [
            "changed=snell-preview,xray",
            "errors=mieru",
            "publish=snell:preview,xray:stable",
        ], written

    # End-to-end apply against copies of the real lock and docs, with the network
    # replaced by a probe that reports one Xray update.
    saved = (LOCK, PROTOCOL_DOC, XRAY_DOC, MIERU_DOC, PROBES["xray"])
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, source in (("versions.json", saved[0]), ("protocols.md", saved[1]),
                             ("xray.md", saved[2]), ("mieru.md", saved[3])):
            (root / name).write_bytes(Path(source).read_bytes())
        LOCK, PROTOCOL_DOC, XRAY_DOC, MIERU_DOC = (
            root / "versions.json", root / "protocols.md", root / "xray.md", root / "mieru.md"
        )
        calls = {"count": 0}

        def fake_probe(_api, lock, _age):
            entry = lock_entry(lock, SPECS["xray"])
            calls["count"] += 1
            pin = {"version": "99.0.0", "source": "https://example/99", "commit": "c" * 40,
                   "url": "https://example/99.tar.gz", "sha256": "d" * 64, "toolchain": "go1.27.3"}
            return Probe(spec=SPECS["xray"], current=entry["version"], latest=pin["version"],
                         pin=pin, changed=differs(entry, pin))

        PROBES["xray"] = fake_probe
        try:
            before = json.loads(LOCK.read_text())["revisions"]["xray:stable"]
            github_output = root / "apply-output.txt"
            command_apply(argparse.Namespace(components="xray", min_age_hours=0, summary=None,
                                             json=None, github_output=str(github_output)))
            assert github_output.read_text(encoding="utf-8").splitlines() == [
                "changed=xray", "errors=", "publish=xray:stable"
            ]
            applied = json.loads(LOCK.read_text())
            assert applied["xray"]["version"] == "99.0.0"
            assert applied["revisions"]["xray:stable"] == before + 1
            assert "bundled Xray 99.0.0" in XRAY_DOC.read_text()
            assert "using Go 1.27.3" in XRAY_DOC.read_text()
            command_apply(argparse.Namespace(components="xray", min_age_hours=0, summary=None, json=None))
            assert json.loads(LOCK.read_text())["revisions"]["xray:stable"] == before + 1
            assert github_output.read_text(encoding="utf-8").splitlines()[-1] == "publish=xray:stable"
        finally:
            LOCK, PROTOCOL_DOC, XRAY_DOC, MIERU_DOC, PROBES["xray"] = saved

    print("self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="probe upstream without writing")
    mode.add_argument("--apply", action="store_true", help="rewrite pins for components that need it")
    mode.add_argument("--self-test", action="store_true", help="run the offline unit checks")
    parser.add_argument("--components", default="", help="comma-separated component ids (blank means all)")
    parser.add_argument("--min-age-hours", type=float, default=24,
                        help="soak time before an automatic update is applied (default 24)")
    parser.add_argument("--json", help="write a machine-readable report")
    parser.add_argument("--summary", help="write a Markdown report for the job summary")
    parser.add_argument("--github-output", help="append changed/errors/publish to this file")
    parser.add_argument("--strict", action="store_true", help="fail when any upstream check errors")
    args = parser.parse_args(argv)
    if args.self_test:
        return command_self_test(args)
    if args.check:
        return command_check(args)
    return command_apply(args)


if __name__ == "__main__":
    sys.exit(main())
