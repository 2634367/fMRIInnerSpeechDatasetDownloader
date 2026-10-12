"""Source registry: item.source -> handler."""

from dsdl.sources.base import Context, FileListSource, GitSource, Listing, SourceError
from dsdl.sources.gitrepo import Gin, GitHub
from dsdl.sources.huggingface import HuggingFace
from dsdl.sources.openneuro import OpenNeuro
from dsdl.sources.osf import OSF
from dsdl.sources.zenodo import Figshare, Zenodo

SOURCES: dict[str, FileListSource | GitSource] = {
    s.name: s for s in (OpenNeuro(), OSF(), Zenodo(), Figshare(), HuggingFace(), Gin(), GitHub())
}

__all__ = ["SOURCES", "Context", "FileListSource", "GitSource", "Listing", "SourceError"]
