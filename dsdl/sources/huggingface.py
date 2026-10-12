"""Hugging Face Hub datasets/models: tree listing pinned to a commit, files via /resolve/."""

from __future__ import annotations

from urllib.parse import quote

from dsdl.http import HttpError, RemoteFile
from dsdl.resolve import Item
from dsdl.sources.base import Context, FileListSource, Listing, SourceError, safe_relpath

HF = "https://huggingface.co"


def _split(item: Item) -> tuple[str, str]:
    kind, repo = item.ident.split("/", 1)
    return kind, repo


class HuggingFace(FileListSource):
    name = "hf"

    def _explain(self, item: Item, e: HttpError) -> SourceError:
        kind, repo = _split(item)
        if e.status in (401, 403):
            hint = "set hf_token and accept the terms at" if not item.extra.get("_has_token") else "accept the terms at"
            return SourceError(f"gated or private — {hint} {item.url}")
        if e.status == 404:
            return SourceError(f"{kind[:-1]} {repo} not found on Hugging Face")
        return SourceError(f"Hugging Face API error: {e}")

    def current_version(self, item: Item, ctx: Context) -> str | None:
        kind, repo = _split(item)
        rev = item.extra.get("revision", "main")
        item.extra["_has_token"] = bool(ctx.cfg.token("hf"))
        try:
            info = ctx.http.get_json(f"{HF}/api/{kind}/{repo}/revision/{quote(rev, safe='')}")
        except HttpError as e:
            raise self._explain(item, e) from e
        return info["sha"]

    def list(self, item: Item, ctx: Context) -> Listing:
        kind, repo = _split(item)
        sha = self.current_version(item, ctx)
        prefix = "datasets/" if kind == "datasets" else ""
        url = f"{HF}/api/{kind}/{repo}/tree/{sha}"
        params: dict | None = {"recursive": "true", "expand": "false"}
        files = []
        while url:
            r = ctx.http.request("GET", url, params=params)
            if r.status_code >= 400:
                raise self._explain(item, HttpError(r.status_code, url, r.text[:200]))
            for e in r.json():
                if e.get("type") != "file":
                    continue
                path = e["path"]
                lfs = e.get("lfs")
                if lfs:
                    size, algo, digest = lfs.get("size", e.get("size")), "sha256", lfs.get("oid")
                else:
                    size, algo, digest = e.get("size"), "gitsha1", e.get("oid")
                dl = f"{HF}/{prefix}{repo}/resolve/{sha}/{quote(path)}"
                files.append(RemoteFile(safe_relpath(path), size, dl, algo if digest else None, digest))
            url = (r.links.get("next") or {}).get("url")
            params = None
        return Listing(sha, files)
