"""Helpers for loading overlay factories without cross-asset module leakage."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from placement_contract import import_path_matches_overlay


def reset_gin_state() -> bool:
    """Best-effort reset of gin's global registry before reloading overlays."""
    try:
        gin = importlib.import_module("gin")
    except ModuleNotFoundError:
        return False

    clear_config = getattr(gin, "clear_config", None)
    if callable(clear_config):
        try:
            clear_config(clear_registered=True)
        except TypeError:
            clear_config()

    enter_interactive_mode = getattr(gin, "enter_interactive_mode", None)
    if callable(enter_interactive_mode):
        enter_interactive_mode()
    return True


def purge_infinigen_modules() -> int:
    """Drop any cached ``infinigen`` modules so the next overlay loads fresh."""
    removed = 0
    for name in list(sys.modules):
        if name == "infinigen" or name.startswith("infinigen."):
            sys.modules.pop(name, None)
            removed += 1
    return removed


def _looks_like_overlay_root(path: Path) -> bool:
    try:
        return (path / "factory.txt").is_file() and (path / "infinigen").is_dir()
    except OSError:
        return False


def _prune_overlay_roots_from_sys_path(current_root: Path) -> None:
    """Keep only the current overlay root; remove older staged overlay roots.

    ``verify_overlay_not_stale`` falls back to ``sys.path`` when a staged root
    has no recorded ``base_pkg`` sidecar.  If we leave older overlay roots in
    ``sys.path``, that fallback can compare the current asset against the
    previous asset's overlay instead of the real runtime checkout.
    """

    current_root = current_root.resolve()
    kept = []
    for entry in sys.path:
        if not entry:
            kept.append(entry)
            continue
        try:
            resolved = Path(entry).resolve()
        except (OSError, RuntimeError, ValueError):
            kept.append(entry)
            continue
        if resolved == current_root:
            continue
        if _looks_like_overlay_root(resolved):
            continue
        kept.append(entry)
    sys.path[:] = kept


def _read_factory_meta(path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta


def load_overlay_factory(code_dir, expect_name):
    """Load a factory class from one overlay root, isolated from prior loads."""
    code_dir = Path(code_dir).resolve()
    purge_infinigen_modules()
    reset_gin_state()
    _prune_overlay_roots_from_sys_path(code_dir)

    code_dir_str = str(code_dir)
    while code_dir_str in sys.path:
        sys.path.remove(code_dir_str)
    sys.path.insert(0, code_dir_str)
    importlib.invalidate_caches()

    ftxt = code_dir / "factory.txt"
    specific_ftxt = code_dir / "factories" / f"{expect_name}.txt"
    if ftxt.is_file():
        root_meta = _read_factory_meta(ftxt)
        root_fqn = root_meta.get("fqn", "")
        root_cls = root_fqn.rsplit(".", 1)[-1] if "." in root_fqn else ""
        if (
            root_meta.get("factory") != expect_name
            and root_cls != expect_name
            and specific_ftxt.is_file()
        ):
            ftxt = specific_ftxt
    elif specific_ftxt.is_file():
        ftxt = specific_ftxt
    if not ftxt.is_file():
        raise FileNotFoundError(
            f"{code_dir}/factory.txt 不存在 —— placer 假設 factory.txt 在 overlay root")

    meta = _read_factory_meta(ftxt)
    fqn = meta["fqn"]
    mod_fqn, cls_name = fqn.rsplit(".", 1)

    assert cls_name == expect_name, (
        f"★overlay 類名 {cls_name!r} 與 box 的 {expect_name!r} 不符★ —— "
        f"populate_all 用類名配對，不一致就會完全找不到 box")

    mod = importlib.import_module(mod_fqn)
    assert import_path_matches_overlay(mod.__file__, code_dir, sys.path), (
        f"★import 到官方版不是 overlay★: {mod.__file__}")
    return getattr(mod, cls_name), fqn
