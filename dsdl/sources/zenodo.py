"""Zenodo records and Figshare articles (both pinned to the linked record/article)."""

from __future__ import annotations

from dsdl.http import HttpError, RemoteFile
from dsdl.resolve import Item
from dsdl.sources.base import Context, FileListSource, Listing, SourceError, safe_relpath


class Zenodo(FileListSource):
    name = "zenodo"

    def list(self, item: Item, ctx: Context) -> Listing:
        url = f"https://zenodo.org/api/records/{item.ident}"
        try:
            rec = ctx.http.get_json(url)
        except HttpError as e:
            if e.status in (404, 410):
                raise SourceError(f"Zenodo record {item.ident} not found") from e
            raise SourceError(f"Zenodo API error: {e}") from e
        entries = rec.get("files") or []
        if not entries:
            access = (rec.get("access") or {}).get("files") or rec.get("metadata", {}).get("access_right")
            raise SourceError(f"Zenodo record {item.ident} has no downloadable files (access: {access or 'unknown'})")
        files = []
        for f in entries:
            algo, _, digest = (f.get("checksum") or "").partition(":")
            link = (f.get("links") or {}).get("self") or (f.get("links") or {}).get("content")
            files.append(RemoteFile(safe_relpath(f["key"]), f.get("size"), link, algo or None, digest or None))
        version = (rec.get("metadata") or {}).get("version") or rec.get("revision")
        return Listing(str(version) if version is not None else None, files)


class Figshare(FileListSource):
    name = "figshare"

    def list(self, item: Item, ctx: Context) -> Listing:
        base = f"https://api.figshare.com/v2/articles/{item.ident}"
        try:
            art = ctx.http.get_json(base)
        except HttpError as e:
            if e.status in (403, 404):
                raise SourceError(f"Figshare article {item.ident} not found or private") from e
            raise SourceError(f"Figshare API error: {e}") from e
        entries, page = [], 1
        while True:
            batch = ctx.http.get_json(f"{base}/files", params={"page": page, "page_size": 1000})
            entries.extend(batch)
            if len(batch) < 1000:
                break
            page += 1
        files, warnings = [], []
        for f in entries:
            if f.get("is_link_only"):
                warnings.append(f"link-only file skipped: {f.get('name')} ({f.get('download_url')})")
                continue
            md5 = f.get("computed_md5") or f.get("supplied_md5") or None
            files.append(RemoteFile(safe_relpath(f["name"]), f.get("size"), f["download_url"], "md5" if md5 else None, md5))
        return Listing(str(art.get("version", "")) or None, files, warnings)
