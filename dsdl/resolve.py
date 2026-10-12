"""Turn sheet rows into downloadable items (source + id) and a list of manual downloads."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

from dsdl.sheet import Row, unwrap_google_redirect

DATASETS, CODE = "datasets", "code"

OSF_PROVIDERS = {
    "osfstorage", "github", "gitlab", "bitbucket", "googledrive", "dropbox", "box",
    "figshare", "owncloud", "s3", "onedrive", "dataverse", "boa",
}
GITHUB_RESERVED = {
    "orgs", "users", "topics", "settings", "sponsors", "marketplace", "collections",
    "features", "search", "explore", "about", "login", "enterprise",
}
MANUAL_HOSTS = {
    "search.kg.ebrains.eu": "HCP via EBRAINS — requires registration / data use terms",
    "www.humanconnectome.org": "HCP — requires ConnectomeDB registration",
    "www.cneuromod.ca": "CNeuroMod — registration required (or datalad from github.com/courtois-neuromod)",
    "www.synapse.org": "Synapse — requires a Synapse account and data use agreement",
    "purr.purdue.edu": "Purdue PURR — download from the publication page",
    "smallworldofwords.org": "SWOW — request access via the research page",
    "arxiv.org": "paper only — no code/data link in the sheet",
    "icml.cc": "paper only — no code/data link in the sheet",
    "naturalscenesdataset.org": "NSD — requires data access agreement",
}


@dataclass
class Ref:
    tab: str
    row: int
    title: str
    column: str
    categories: str = ""
    notes: str = ""


@dataclass
class Target:
    source: str
    ident: str
    url: str
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ident}"


@dataclass
class Item:
    key: str
    task: str
    source: str
    ident: str
    url: str
    extra: dict = field(default_factory=dict)
    refs: list[Ref] = field(default_factory=list)

    @property
    def rel_dir(self) -> str:
        return f"{self.task}/{SOURCE_DIRS.get(self.source, self.source)}/{self.ident}"

    @property
    def title(self) -> str:
        return self.refs[0].title if self.refs else self.key


@dataclass
class Manual:
    task: str
    title: str
    url: str
    reason: str
    tab: str
    row: int


SOURCE_DIRS = {"hf": "huggingface"}


@dataclass
class Resolution:
    items: list[Item]
    manual: list[Manual]

    def for_task(self, task: str) -> list[Item]:
        return [i for i in self.items if i.task == task]


_DS = re.compile(r"^ds\d{6}$")
_OSF_GUID = re.compile(r"^[a-z0-9]{5}$")
_NUM = re.compile(r"^(\d{5,9})(?:\.0)?$")
_GH_TOKEN = re.compile(r"^([A-Za-z0-9][\w.-]*)/([\w.-]+)$")


def classify_link(url: str) -> Target | str:
    """Return a Target, or a string explaining why it must be downloaded manually."""
    url = unwrap_google_redirect(url.strip())
    p = urlparse(url)
    host = (p.netloc or "").lower()
    parts = [unquote(s) for s in p.path.split("/") if s]

    if host in ("htmlpreview.github.io",) and p.query:
        return classify_link(p.query.split("&")[0])
    if _DS.match(host):  # broken link like https://ds003745
        return Target("openneuro", host, f"https://openneuro.org/datasets/{host}")
    if host.endswith("openneuro.org") and len(parts) >= 2 and parts[0] == "datasets" and _DS.match(parts[1]):
        extra = {}
        if len(parts) >= 4 and parts[2] == "versions":
            extra["version"] = parts[3]
        return Target("openneuro", parts[1], f"https://openneuro.org/datasets/{parts[1]}", extra)
    if host in ("osf.io", "www.osf.io") and parts and _OSF_GUID.match(parts[0]):
        guid = parts[0]
        if len(parts) >= 3 and parts[1] == "files" and _OSF_GUID.match(parts[2]) and parts[2] not in OSF_PROVIDERS:
            guid = parts[2]
        extra = {}
        vo = parse_qs(p.query).get("view_only")
        if vo:
            extra["view_only"] = vo[0]
        return Target("osf", guid, f"https://osf.io/{guid}/", extra)
    if host in ("zenodo.org", "www.zenodo.org") and len(parts) >= 2 and parts[0] in ("records", "record") and parts[1].isdigit():
        return Target("zenodo", parts[1], f"https://zenodo.org/records/{parts[1]}")
    if "figshare.com" in host and parts and parts[0] == "articles":
        for seg in parts[1:]:
            if seg.isdigit() and len(seg) >= 6:
                return Target("figshare", seg, f"https://figshare.com/articles/{seg}")
    if host in ("doi.org", "dx.doi.org"):
        doi = "/".join(parts).lower()
        m = re.match(r"10\.5281/zenodo\.(\d+)$", doi)
        if m:
            return Target("zenodo", m[1], f"https://zenodo.org/records/{m[1]}")
        m = re.match(r"10\.6084/m9\.figshare\.(\d+)", doi)
        if m:
            return Target("figshare", m[1], f"https://figshare.com/articles/{m[1]}")
        return f"DOI landing page ({doi}) — no supported API for this host"
    if host == "huggingface.co" and parts:
        if parts[0] == "datasets" and len(parts) >= 3:
            kind, owner, name, rest = "datasets", parts[1], parts[2], parts[3:]
        elif parts[0] == "spaces":
            return "Hugging Face Space — not a downloadable repo"
        elif len(parts) >= 2:
            kind, owner, name, rest = "models", parts[0], parts[1], parts[2:]
        else:
            return "Hugging Face user page — no repo in link"
        extra = {"kind": kind}
        if len(rest) >= 2 and rest[0] == "tree" and rest[1] != "main":
            extra["revision"] = rest[1]
        prefix = "datasets/" if kind == "datasets" else ""
        return Target("hf", f"{kind}/{owner}/{name}", f"https://huggingface.co/{prefix}{owner}/{name}", extra)
    if host == "gin.g-node.org" and len(parts) >= 2:
        return Target("gin", f"{parts[0]}/{parts[1]}", f"https://gin.g-node.org/{parts[0]}/{parts[1]}")
    if host in ("github.com", "www.github.com"):
        if len(parts) >= 2 and parts[0].lower() not in GITHUB_RESERVED:
            repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
            return Target("github", f"{parts[0]}/{repo}", f"https://github.com/{parts[0]}/{repo}")
        return "GitHub organisation/user page — pick the repos to clone manually"
    if host in MANUAL_HOSTS:
        return MANUAL_HOSTS[host]
    if "ezproxy" in host:
        return "library proxy link (paper) — no code/data link in the sheet"
    return f"unsupported host ({host or url}) — download manually"


def classify_token(tok: str) -> Target | None:
    tok = tok.strip().strip(",;")
    if _DS.match(tok):
        return Target("openneuro", tok, f"https://openneuro.org/datasets/{tok}")
    m = _NUM.match(tok)
    if m:
        return Target("zenodo", m[1], f"https://zenodo.org/records/{m[1]}")
    if _OSF_GUID.match(tok):
        return Target("osf", tok, f"https://osf.io/{tok}/")
    m = _GH_TOKEN.match(tok)
    if m:
        return Target("github", f"{m[1]}/{m[2]}", f"https://github.com/{m[1]}/{m[2]}")
    return None


def _task_for(target: Target, default: str) -> str:
    return CODE if target.source == "github" else default


class _Collector:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], Item] = {}
        self.manual: list[Manual] = []
        self._manual_seen: set[tuple[str, str]] = set()

    def add(self, target: Target, task: str, ref: Ref) -> None:
        k = (task, target.key.lower())
        it = self.items.get(k)
        if it is None:
            it = self.items[k] = Item(target.key, task, target.source, target.ident, target.url, dict(target.extra))
        else:
            for ek, ev in target.extra.items():
                it.extra.setdefault(ek, ev)
        if not any(r.tab == ref.tab and r.row == ref.row for r in it.refs):
            it.refs.append(ref)

    def add_manual(self, task: str, title: str, url: str, reason: str, row: Row) -> None:
        seen_key = (task, url or f"{row.tab}:{row.number}")
        if seen_key in self._manual_seen:
            return
        self._manual_seen.add(seen_key)
        self.manual.append(Manual(task, title, url, reason, row.tab, row.number))


def _row_targets(row: Row, link_cols: list[str], token_col: str | None) -> tuple[list[Target], list[tuple[str, str]]]:
    targets, manual = [], []
    for col in link_cols:
        for link in row.links(col):
            t = classify_link(link)
            if isinstance(t, Target):
                targets.append(t)
            else:
                manual.append((link, t))
    if token_col:
        all_links = " ".join(link for c in row.cells for link in row.links(c)).lower()
        for tok in re.split(r"[\s|]+", row.text(token_col)):
            t = classify_token(tok)
            if t is None or t.ident.lower() in all_links:
                continue
            if any(x.key.lower() == t.key.lower() for x in targets):
                continue
            targets.append(t)
    return targets, manual


def resolve(tabs: dict[str, list[Row]], datasets_tab: str, code_tab: str, include_dataset_code_column: bool = True) -> Resolution:
    col = _Collector()
    ds_rows = _find_tab(tabs, datasets_tab)
    code_rows = _find_tab(tabs, code_tab)

    for row in ds_rows:
        title = row.text("title") or row.text("id") or f"row {row.number}"
        ref = lambda c: Ref(row.tab, row.number, title, c, row.text("categories"), row.text("notes"))  # noqa: E731
        targets, manual = _row_targets(row, ["id"], "id")
        for t in targets:
            col.add(t, _task_for(t, DATASETS), ref("id"))
        for url, why in manual:
            col.add_manual(DATASETS, title, url, why, row)
        if not targets and not manual and row.text("id"):
            col.add_manual(DATASETS, title, "", f"no link for ID '{row.text('id')}'", row)
        if include_dataset_code_column:
            targets, manual = _row_targets(row, ["code"], None)
            for t in targets:
                col.add(t, CODE, ref("code"))
            for url, why in manual:
                col.add_manual(CODE, title, url, why, row)

    for row in code_rows:
        title = row.text("name") or f"row {row.number}"
        targets, manual = _row_targets(row, ["name"], None)
        for t in targets:
            col.add(t, CODE, Ref(row.tab, row.number, title, "name", row.text("type"), row.text("notes")))
        for url, why in manual:
            col.add_manual(CODE, title, url, why, row)
        if not targets and not manual:
            col.add_manual(CODE, title, "", "no link in the sheet", row)

    # an item that is already downloaded as a dataset isn't downloaded again as code
    dataset_keys = {k for (task, k) in col.items if task == DATASETS}
    items = [it for (task, k), it in col.items.items() if not (task == CODE and k in dataset_keys)]
    return Resolution(items, col.manual)


def _find_tab(tabs: dict[str, list[Row]], name: str) -> list[Row]:
    for tab, rows in tabs.items():
        if tab.strip().lower() == name.strip().lower():
            return rows
    raise KeyError(f"tab '{name}' not found in sheet (tabs: {', '.join(tabs)})")


def match_keys(items: list[Item], patterns: list[str]) -> list[Item]:
    """Case-insensitive match of item keys; a pattern may also be a bare id or end with '*'."""
    out = []
    pats = [p.lower() for p in patterns]
    for it in items:
        k = it.key.lower()
        for p in pats:
            if k == p or it.ident.lower() == p or (p.endswith("*") and k.startswith(p[:-1])):
                out.append(it)
                break
    return out
