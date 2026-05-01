# ETABS API — primitive ML dataset & export helpers

Python tools to **sample building primitives**, **build multi-story ETABS frame models** (COM API), and optionally run a **post-build pipeline** (slab loads, ASCE 7-16 / UBC97 auto seismic, mass source, analysis, CSV export).

**Requirements:** Windows, **ETABS** installed (CSI COM API), Python ≥ 3.9, `numpy`, `comtypes`, `pandas`.  
CSI documentation: [CSI API ETABS](https://www.csiamerica.com/products/etabs).

## Quick start

```bash
git clone https://github.com/jimmy-cook/ETABS_API.git
cd ETABS_API
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

### Generate models (primitive batch)

```bash
# Installed entry point (recommended)
etabs-primitive-gen --config configs/primitive_ml_dataset.default.json --out ml_primitive_output --limit 5

# Or without install (adds src/ automatically)
python scripts/run_primitive_ml_dataset.py --config configs/primitive_ml_dataset.default.json --out ml_primitive_output --limit 5
```

Configs live under **`configs/`** (e.g. US/FPS preset: `configs/primitive_ml_dataset.default.us_fps.json`).

### Other CLIs (after `pip install -e .`)

| Command | Description |
|--------|-------------|
| `etabs-primitive-gen` | Primitive ML batch (`run_primitive_ml_dataset`) |
| `etabs-frame-datagen` | Alternate frame datagen driver |
| `etabs-parametric` | Cartesian parametric grid batch |
| `etabs-structured-export` | Open `.edb` / attach and export analysis tables |

### Smoke scripts (ETABS required)

From repo root (scripts add `src/` to `sys.path`):

```bash
python scripts/export_asce716_test_edb.py
python scripts/export_ubc97_test_edb.py
python scripts/export_asce716_full_workflow_edb.py
```

Outputs default to **`models/`** next to the repo root.

### Tests (no ETABS)

```bash
pytest -q
```

## Layout

| Path | Role |
|------|------|
| `src/etabs_ml/` | Installable package |
| `configs/` | JSON dataset / unit presets |
| `scripts/` | Runnable helpers without `pip install` |
| `tests/` | `pytest` |

## License

MIT — see [LICENSE](LICENSE).
