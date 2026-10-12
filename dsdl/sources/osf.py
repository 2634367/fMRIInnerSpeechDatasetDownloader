"""OSF: projects/components/registrations (all storage providers + child components) or single files."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dsdl.http import HttpError, RemoteFile
from dsdl.resolve import Item
from dsdl.sources.base import Context, FileListSource, Listing, SourceError, safe_relpath

API = "https://api.osf.io/v2"


def _with_params(url: str, params: dict) -> str:
    if not params:
        return url
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    q.update(params)
    return urlunparse(p._replace(query=urlencode(q)))


class OSF(FileListSource):
    name = "osf"

    def list(self, item: Item, ctx: Context) -> Listing:
        view_only = ctx.cfg.override(item.key).get("view_only") or item.extra.get("view_only")
        return _Walker(ctx, {"view_only": view_only} if view_only else {}).list(item)


class _Walker:
    """One listing run (holds per-item state so listings can run in parallel)."""

    def __init__(self, ctx: Context, view_only: dict) -> None:
        self._ctx = ctx
        self._vo = view_only

    def list(self, item: Item) -> Listing:
        files: list[RemoteFile] = []
        warnings: list[str] = []
        try:
            guid = self._get(f"{API}/guids/{item.ident}/", {"resolve": "false"})
        except HttpError as e:
            raise self._explain(item, e) from e
        ref = guid["data"]["relationships"]["referent"]["data"]
        rtype = ref["type"]
        if rtype in ("nodes", "registrations"):
            self._walk_node(rtype, item.ident, "", files, warnings, top=True)
        elif rtype == "files":
            data = self._get(f"{API}/files/{item.ident}/")["data"]
            a = data["attributes"]
            if a["kind"] == "folder":
                self._walk_folder(data["relationships"]["files"]["links"]["related"]["href"], "", files)
            else:
                files.append(self._file(data, a["name"]))
        elif rtype == "preprints":
            rel = self._get(f"{API}/preprints/{item.ident}/")["data"]["relationships"]
            primary = (((rel.get("primary_file") or {}).get("links") or {}).get("related") or {}).get("href")
            if primary:
                data = self._get(primary)["data"]
                files.append(self._file(data, "preprint/" + data["attributes"]["name"]))
            node = (rel.get("node") or {}).get("data") or {}
            if node.get("id"):  # the preprint's supplemental project
                self._walk_node("nodes", node["id"], f"project [{node['id']}]/", files, warnings)
        else:
            raise SourceError(f"OSF {item.ident} is a '{rtype}', which isn't supported — download manually")
        return Listing(None, files, warnings)

    # -- helpers --------------------------------------------------------------------
    def _get(self, url: str, params: dict | None = None) -> dict:
        return self._ctx.http.get_json(url, params={**(params or {}), **self._vo})

    def _paged(self, url: str):
        params = {"page[size]": 100}
        seen = set()
        while url and url not in seen:
            seen.add(url)
            page = self._get(url, params)
            yield from page.get("data", [])
            url = (page.get("links") or {}).get("next")
            params = {}  # the next link already carries the page params

    def _explain(self, item: Item, e: HttpError) -> SourceError:
        if e.status in (401, 403):
            return SourceError(f"OSF {item.ident} is private — set osf_token or a view_only key in [overrides]")
        if e.status in (404, 410):
            return SourceError(f"OSF {item.ident} not found (deleted, or private without a view-only key)")
        return SourceError(f"OSF API error: {e}")

    def _walk_node(self, ntype: str, nid: str, prefix: str, files, warnings, top: bool = False) -> None:
        try:
            providers = list(self._paged(f"{API}/{ntype}/{nid}/files/"))
        except HttpError as e:
            if top:
                raise self._explain(Item(f"osf:{nid}", "", "osf", nid, ""), e) from e
            warnings.append(f"component {nid}: {e}")
            return
        for p in providers:
            prov = p["attributes"].get("provider") or p["attributes"].get("name") or "storage"
            href = p["relationships"]["files"]["links"]["related"]["href"]
            sub = prefix if prov == "osfstorage" else f"{prefix}_{prov}/"
            try:
                self._walk_folder(href, sub, files)
            except HttpError as e:
                if prov == "osfstorage":
                    raise
                warnings.append(f"{prov} add-on storage of {nid} skipped: {e}")
        for child in self._paged(f"{API}/{ntype}/{nid}/children/"):
            title = child["attributes"].get("title") or child["id"]
            self._walk_node(child["type"], child["id"], f"{prefix}{safe_relpath(title)} [{child['id']}]/", files, warnings)

    def _walk_folder(self, href: str, prefix: str, files) -> None:
        for e in self._paged(href):
            a = e["attributes"]
            if a["kind"] == "folder":
                self._walk_folder(e["relationships"]["files"]["links"]["related"]["href"], prefix, files)
            else:
                rel = (a.get("materialized_path") or a["name"]).lstrip("/")
                files.append(self._file(e, prefix + rel))

    def _file(self, data: dict, rel: str) -> RemoteFile:
        a = data["attributes"]
        hashes = (a.get("extra") or {}).get("hashes") or {}
        algo = "sha256" if hashes.get("sha256") else ("md5" if hashes.get("md5") else None)
        url = _with_params(data["links"]["download"], self._vo)
        return RemoteFile(safe_relpath(rel), a.get("size"), url, algo, hashes.get(algo) if algo else None)
