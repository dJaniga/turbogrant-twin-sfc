"""Turn calibration runs into a ready-to-paste ``calibration:`` config block.

    uv run scripts/calibrate.py --point logs/ice_bath.csv 0.05 \
                                --point logs/warm_bath.csv 45.31

Workflow on the rig:

1. Put every sensor and the reference thermometer at the same temperature and let
   them settle (a stirred bath is ideal; a closed box is usually good enough).
2. In the app, press **Record CSV** for a minute or two, then press it again.
3. Note what the reference read, and repeat at a second temperature if you want
   gain as well as offset.
4. Run this script with one ``--point <session.csv> <reference °C>`` per run.

It reports each sensor's mean, its spread during the run, and the residual after
correction, then prints the YAML. Nothing is written unless ``--write`` is given.

Points are fitted per sensor:

* one point   → ``offset``   (``reference - mean``)
* two+ points → ``linear``   (least squares) or ``table`` with ``--type table``

Sessions recorded *while a calibration was already active* are handled correctly:
the ``<id>_raw`` columns are used when present, so re-calibrating never stacks a
correction on top of an existing one.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from config.loader import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from config.schema import ConfigError  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="calibrate",
        description="Fit per-sensor corrections from recorded calibration runs.",
    )
    parser.add_argument(
        "--point", nargs=2, action="append", metavar=("SESSION_CSV", "REFERENCE_C"),
        required=True,
        help="a recorded session and what the reference thermometer read; repeatable",
    )
    parser.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--type", choices=["auto", "offset", "linear", "table"],
                        default="auto", help="correction type (default: auto)")
    parser.add_argument("--reference", help="note describing the reference instrument")
    parser.add_argument("--trim", type=float, default=0.0, metavar="SECONDS",
                        help="ignore the first SECONDS of each run while it settles")
    parser.add_argument("--write", action="store_true",
                        help="write the block into the config file instead of printing it")
    return parser.parse_args(argv)


def load_run(path: Path, sensor_ids: list[str], trim_s: float) -> pd.DataFrame:
    """Read one session, preferring `<id>_raw` columns when the app wrote them."""
    table = pd.read_csv(path)
    columns = {}
    for sensor_id in sensor_ids:
        raw_column = f"{sensor_id}_raw"
        if raw_column in table.columns:
            columns[sensor_id] = pd.to_numeric(table[raw_column], errors="coerce")
        elif sensor_id in table.columns:
            columns[sensor_id] = pd.to_numeric(table[sensor_id], errors="coerce")
        else:
            raise SystemExit(f"{path}: no column for {sensor_id}")

    frame = pd.DataFrame(columns).dropna()
    if trim_s > 0 and "timestamp" in table.columns:
        stamps = pd.to_datetime(table["timestamp"], errors="coerce")
        keep = stamps >= stamps.min() + pd.Timedelta(seconds=trim_s)
        frame = frame[keep.reindex(frame.index, fill_value=True)]
    if frame.empty:
        raise SystemExit(f"{path}: no usable rows (after trimming {trim_s:g}s)")
    return frame


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    # Disabled sensors are not recorded, so only the enabled ones can be fitted.
    sensor_ids = list(config.active_sensor_ids)
    runs: list[tuple[float, pd.DataFrame]] = []
    print(f"Calibration runs ({len(sensor_ids)} enabled sensors)")
    for session, reference in args.point:
        frame = load_run(Path(session), sensor_ids, args.trim)
        runs.append((float(reference), frame))
        spread = frame.mean().max() - frame.mean().min()
        print(f"  {Path(session).name:32s} reference {float(reference):8.3f} °C  "
              f"{len(frame):5d} rows  sensor spread {spread:5.3f} °C")

    kind = args.type
    if kind == "auto":
        kind = "offset" if len(runs) == 1 else "linear"
    if kind != "offset" and len(runs) < 2:
        print(f"\n{kind!r} needs at least two --point runs", file=sys.stderr)
        return 2

    print(f"\nFitted corrections ({kind})")
    print(f"  {'sensor':6s} {'mean(s)':>26s} {'correction':>34s} {'residual':>9s}")
    entries: dict[str, dict] = {}
    for sensor_id in sensor_ids:
        means = [float(frame[sensor_id].mean()) for _, frame in runs]
        noise = max(float(frame[sensor_id].std(ddof=0)) for _, frame in runs)
        references = [reference for reference, _ in runs]

        if kind == "offset":
            offset = references[0] - means[0]
            entry = {"type": "offset", "offset": round(offset, 4)}
            residual = 0.0
            summary = f"offset {offset:+.3f}"
        elif kind == "linear":
            gain, intercept = np.polyfit(means, references, 1)
            entry = {"type": "linear", "gain": round(float(gain), 6),
                     "offset": round(float(intercept), 4)}
            residual = float(np.max(np.abs(np.array(means) * gain + intercept - references)))
            summary = f"gain {gain:.5f}, offset {intercept:+.3f}"
        else:
            points = [[round(mean, 3), round(reference, 3)]
                      for mean, reference in zip(means, references)]
            entry = {"type": "table", "points": points}
            residual = 0.0
            summary = f"table, {len(points)} points"

        if args.reference:
            entry["note"] = args.reference
        entries[sensor_id] = entry
        print(f"  {sensor_id:6s} {', '.join(f'{m:7.3f}' for m in means):>26s} "
              f"{summary:>34s} {residual:8.3f}  (noise ±{noise:.3f})")

    # Pasting the block replaces calibration.sensors wholesale, so carry over
    # what disabled sensors already have rather than silently dropping it.
    kept = [sensor_id for sensor_id in config.sensor_ids
            if sensor_id not in entries and sensor_id in config.calibration.sensors]
    for sensor_id in kept:
        entries[sensor_id] = config.calibration.sensors[sensor_id].model_dump(
            mode="json", exclude_defaults=True
        )
    if kept:
        print(f"  kept existing entries for disabled sensor(s): {', '.join(kept)}")

    block = {"calibration": {"enabled": True,
                             "reference": args.reference,
                             "sensors": entries}}
    rendered = yaml.safe_dump(block, sort_keys=False, default_flow_style=None)

    if args.write:
        # Deliberately not an automatic edit: config.yaml carries hand-written
        # comments that a dump would destroy.
        out = args.config.with_suffix(".calibration.yaml")
        out.write_text(rendered, encoding="utf-8")
        print(f"\nWrote {out} — paste its `calibration:` section into {args.config.name}")
    else:
        print("\nPaste into config/config.yaml:\n")
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
