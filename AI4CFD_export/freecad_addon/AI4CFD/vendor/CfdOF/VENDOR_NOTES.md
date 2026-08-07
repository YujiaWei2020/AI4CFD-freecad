# Vendored copy of CfdOF

This directory is a trimmed, vendored copy of the [CfdOF](https://github.com/jaheyns/CfdOF)
FreeCAD workbench, bundled directly into the AI4CFD addon so users don't need
to separately install CfdOF via FreeCAD's Addon Manager.

- **Upstream repository:** https://github.com/jaheyns/CfdOF
- **Vendored commit:** `2047ae3` (2026-05-16, branch `master`)
- **License:** LGPL-3.0-or-later — see [LICENSE](LICENSE) (kept verbatim,
  unmodified).

## What was trimmed

Only what's needed at runtime was kept: `CfdOF/` (the Python package),
`Gui/`, `Data/`, `Translations/`, `Init.py`, `InitGui.py`, `LICENSE`,
`package.xml`. Dropped: `Demos/` (example `.FCStd` files), `Doc/`, and
repo-meta files (`.github/`, lint/pre-commit configs, `CONTRIBUTING.md`,
`ROADMAP.md`, `AM_INSTALLATION_DIGEST.txt`).

`TestCfdOF.py` was initially trimmed too, then restored: `CfdOF/CfdTestCommands.py`
does an unconditional `import TestCfdOF` at module scope, and that module is
itself unconditionally imported by `InitGui.py`'s `Initialize()`. Without it,
the whole CfdOF `Initialize()` call throws and none of CfdOF's commands
(mesh, physics, solver, BCs...) get registered — not just the dev/test menu.
Keep `TestCfdOF.py` in place unless `CfdTestCommands.py` is patched to import
it lazily.

## How it's loaded

AI4CFD's [InitGui.py](../../InitGui.py) puts this directory on `sys.path` and
`exec()`s the vendored `Init.py`/`InitGui.py` in a namespace seeded with
`FreeCAD`/`FreeCADGui`/`Workbench`, mirroring how FreeCAD's own Mod-folder
loader runs them. The vendored files themselves are left untouched so they
stay diffable against upstream.

## Modifying this code

Per the LGPL (via GPLv3 §5(a), incorporated by reference), any file edited
here going forward should carry a notice stating that it was changed and
when. Add a short comment near the top of the file, e.g.:

```
# Modified by AI4CFD_core, <date>: <what changed>
```

## Updating from upstream

To pull newer CfdOF changes, diff this directory against a fresh checkout of
`jaheyns/CfdOF` at the desired commit, re-apply the trimming above, and
re-apply any local modifications noted per-file as above.
