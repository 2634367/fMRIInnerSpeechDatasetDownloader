"""OpenNeuro: GraphQL snapshot listing → versioned S3 URLs."""

from __future__ import annotations

from urllib.parse import quote, unquote, urlparse

from dsdl.http import HttpError, RemoteFile
from dsdl.resolve import Item
from dsdl.sources.base import Context, FileListSource, Listing, SourceError, safe_relpath

GQL = "https://openneuro.org/crn/graphql"


class OpenNeuro(FileListSource):
    name = "openneuro"

    def _query(self, ctx: Context, query: str, variables: dict, timeout: float | None = None) -> dict:
        try:
            res = ctx.http.post_json(GQL, {"query": query, "variables": variables}, timeout=timeout)
        except HttpError as e:
            raise SourceError(f"OpenNeuro API error: {e}") from e
        if res.get("errors"):
            raise SourceError("OpenNeuro: " + "; ".join(e.get("message", "?") for e in res["errors"])[:300])
        return res["data"]

    def listing_fingerprint(self, ctx: Context) -> str:
        return f"version={ctx.cfg.openneuro_version};derivatives={ctx.cfg.openneuro_derivatives}"

    def current_version(self, item: Item, ctx: Context) -> str | None:
        if ctx.cfg.openneuro_version == "sheet" and item.extra.get("version"):
            return item.extra["version"]
        data = self._query(ctx, "query($id: ID!){ dataset(id:$id){ latestSnapshot{ tag } } }", {"id": item.ident})
        ds = data.get("dataset")
        if not ds or not ds.get("latestSnapshot"):
            raise SourceError(f"{item.ident} not found on OpenNeuro (or has no public snapshot)")
        return ds["latestSnapshot"]["tag"]

    def list(self, item: Item, ctx: Context) -> Listing:
        tag = self.current_version(item, ctx)
        data = self._query(
            ctx,
            "query($id: ID!, $tag: String!){ snapshot(datasetId:$id, tag:$tag){ files(recursive:true){ filename size urls } } }",
            {"id": item.ident, "tag": tag},
            timeout=600,
        )
        snap = data.get("snapshot")
        if not snap:
            raise SourceError(f"{item.ident} snapshot {tag} not found")
        entries = snap["files"] or []
        # The recursive listing is sometimes empty or truncated (ds001499 returned 12 of 5994
        # files). The snapshot summary counts raw files (no derivatives); if the listing has
        # fewer, walk the directory tree folder by folder instead.
        expected = self._total_files(ctx, item.ident, tag)
        raw = sum(1 for f in entries if not f["filename"].startswith("derivatives/"))
        if raw < expected * 0.95:
            entries = self._walk_tree(ctx, item.ident, tag)
            raw = sum(1 for f in entries if not f["filename"].startswith("derivatives/"))
            if raw < expected * 0.95:
                raise RuntimeError(f"OpenNeuro listed only {raw} of {expected} files for {item.ident} {tag} — retried next run")
        files = []
        for f in entries:
            name = f["filename"]
            if not ctx.cfg.openneuro_derivatives and name.startswith("derivatives/"):
                continue
            urls = f.get("urls") or []
            url = urls[0] if urls else (
                f"https://openneuro.org/crn/datasets/{item.ident}/snapshots/{tag}/files/{quote(name.replace('/', ':'))}"
            )
            # `filename` occasionally drops a directory level (seen in ds005874 derivatives);
            # the versioned S3 key is authoritative, so prefer it when present.
            key = unquote(urlparse(url).path)
            marker = f"/{item.ident}/"
            if urls and "amazonaws.com" in url and marker in key:
                name = key.split(marker, 1)[1]
            files.append(RemoteFile(safe_relpath(name), f.get("size"), url))
        return Listing(tag, files)

    def _walk_tree(self, ctx: Context, ds: str, tag: str) -> list[dict]:
        q = ("query($id: ID!, $tag: String!, $tree: String){ snapshot(datasetId:$id, tag:$tag){ "
             "files(tree:$tree){ id filename size directory urls } } }")
        out, stack = [], [(None, "")]
        while stack:
            tree, prefix = stack.pop()
            if tree is not None and not ctx.cfg.openneuro_derivatives and prefix == "derivatives/":
                continue
            data = self._query(ctx, q, {"id": ds, "tag": tag, "tree": tree}, timeout=300)
            for f in (data.get("snapshot") or {}).get("files") or []:
                path = prefix + f["filename"]
                if f.get("directory"):
                    stack.append((f["id"], path + "/"))
                else:
                    out.append({**f, "filename": path})
        return out

    def _total_files(self, ctx: Context, ds: str, tag: str) -> int:
        data = self._query(ctx, "query($id: ID!, $tag: String!){ snapshot(datasetId:$id, tag:$tag){ summary{ totalFiles } } }",
                           {"id": ds, "tag": tag})
        return ((data.get("snapshot") or {}).get("summary") or {}).get("totalFiles") or 0
