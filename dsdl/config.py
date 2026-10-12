"""Load and validate config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TOKEN_ENV = {
    "github": "GITHUB_TOKEN",
    "hf": "HF_TOKEN",
    "osf": "OSF_TOKEN",
    "zenodo": "ZENODO_TOKEN",
    "figshare": "FIGSHARE_TOKEN",
}


class ConfigError(Exception):
    pass


@dataclass
class Config:
    path: Path
    output_dir: Path
    state_dir: Path
    prevent_sleep: bool = True
    sheet_id: str = ""
    datasets_tab: str = "Datasets"
    code_tab: str = "Code Examples"
    include_dataset_code_column: bool = True
    parallel_items: int = 2
    parallel_files: int = 8
    order: str = "smallest_first"
    retries: int = 8
    timeout: float = 60.0
    verify_checksums: bool = True
    min_free_space_gb: float = 50.0
    exclude: list[str] = field(default_factory=list)
    only: list[str] = field(default_factory=list)
    openneuro_version: str = "latest"
    openneuro_derivatives: bool = True
    git_depth: int = 1
    git_lfs: bool = True
    git_parallel: int = 4
    git_update_on_rerun: bool = False
    tokens: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def token(self, name: str) -> str | None:
        return self.tokens.get(name) or None

    def override(self, key: str) -> dict[str, Any]:
        low = key.lower()
        for k, v in self.overrides.items():
            if k.lower() == low:
                return v
        return {}


def find_config(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p
    env = os.environ.get("DSDL_CONFIG")
    if env:
        return find_config(env)
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent):
        for name in ("config.yaml", "config.yml"):
            if (base / name).is_file():
                return base / name
    raise ConfigError(
        "no config.yaml found — copy config.example.yaml to config.yaml and set output_dir "
        "(or pass --config PATH / set DSDL_CONFIG)"
    )


def _section(raw: dict, name: str) -> dict:
    sec = raw.get(name) or {}
    if not isinstance(sec, dict):
        raise ConfigError(f"'{name}:' must be a mapping of settings")
    return sec


def _get(sec: dict, secname: str, key: str, typ: type | tuple, default: Any) -> Any:
    if key not in sec:
        return default
    val = sec[key]
    if val is None:
        return default
    if typ is str and isinstance(val, (int, float)) and not isinstance(val, bool):
        val = str(val)  # YAML reads unquoted ids/numbers as numbers
    if typ is float and isinstance(val, int) and not isinstance(val, bool):
        val = float(val)
    if not isinstance(val, typ) or (typ is int and isinstance(val, bool)):
        name = typ.__name__ if isinstance(typ, type) else "/".join(t.__name__ for t in typ)
        raise ConfigError(f"{secname}.{key} must be {name}, got {val!r}")
    return val


def load_config(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    gen = _section(raw, "general")
    out = _get(gen, "general", "output_dir", str, "")
    if not out:
        raise ConfigError("general.output_dir is required")
    output_dir = Path(out).expanduser()
    if not output_dir.is_absolute():
        output_dir = (path.parent / output_dir).resolve()
    state = _get(gen, "general", "state_dir", str, "")
    state_dir = Path(state).expanduser() if state else output_dir / ".dsdl"

    sh = _section(raw, "sheet")
    dl = _section(raw, "download")
    on = _section(raw, "openneuro")
    git = _section(raw, "git")
    auth = _section(raw, "auth")

    tokens = {}
    for name, env in TOKEN_ENV.items():
        val = _get(auth, "auth", f"{name}_token", str, "").strip()
        tokens[name] = val or os.environ.get(env, "").strip()

    cfg = Config(
        path=path,
        output_dir=output_dir,
        state_dir=state_dir,
        prevent_sleep=_get(gen, "general", "prevent_sleep", bool, True),
        sheet_id=_get(sh, "sheet", "id", str, ""),
        datasets_tab=_get(sh, "sheet", "datasets_tab", str, "Datasets"),
        code_tab=_get(sh, "sheet", "code_tab", str, "Code Examples"),
        include_dataset_code_column=_get(sh, "sheet", "include_dataset_code_column", bool, True),
        parallel_items=_get(dl, "download", "parallel_items", int, 2),
        parallel_files=_get(dl, "download", "parallel_files", int, 8),
        order=_get(dl, "download", "order", str, "smallest_first"),
        retries=_get(dl, "download", "retries", int, 8),
        timeout=_get(dl, "download", "timeout_s", float, 60.0),
        verify_checksums=_get(dl, "download", "verify_checksums", bool, True),
        min_free_space_gb=_get(dl, "download", "min_free_space_gb", float, 50.0),
        exclude=[str(x) for x in _get(dl, "download", "exclude", list, [])],
        only=[str(x) for x in _get(dl, "download", "only", list, [])],
        openneuro_version=_get(on, "openneuro", "version", str, "latest"),
        openneuro_derivatives=_get(on, "openneuro", "include_derivatives", bool, True),
        git_depth=_get(git, "git", "depth", int, 1),
        git_lfs=_get(git, "git", "lfs", bool, True),
        git_parallel=_get(git, "git", "parallel", int, 4),
        git_update_on_rerun=_get(git, "git", "update_on_rerun", bool, False),
        tokens=tokens,
        overrides={str(k): {kk: str(vv) for kk, vv in (v or {}).items()} for k, v in _section(raw, "overrides").items()},
    )
    if not cfg.sheet_id:
        raise ConfigError("sheet.id is required")
    if cfg.order not in ("smallest_first", "sheet"):
        raise ConfigError('download.order must be "smallest_first" or "sheet"')
    if cfg.openneuro_version not in ("latest", "sheet"):
        raise ConfigError('openneuro.version must be "latest" or "sheet"')
    for n in ("parallel_items", "parallel_files", "git_parallel", "retries"):
        if getattr(cfg, n) < 1:
            raise ConfigError(f"{n} must be >= 1")
    return cfg


def ensure_output_dir(path: Path) -> None:
    """Create output_dir only if its drive/parent exists — never silently write to the internal disk."""
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "Volumes" and not Path("/Volumes", parts[2]).is_dir():
        raise ConfigError(f"drive /Volumes/{parts[2]} is not mounted (output_dir = {path})")
    if path.is_dir():
        return
    if path.exists():
        raise ConfigError(f"output_dir exists but is not a directory: {path}")
    if not path.parent.is_dir():
        raise ConfigError(f"parent of output_dir does not exist: {path.parent} — create it or fix output_dir")
    path.mkdir()
