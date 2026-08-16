"""Scattered-data interpolation of the sensor field onto a regular grid.

Pure numpy/scipy, no Qt (CLAUDE.md §3.3). The evaluation grid is built once in
the constructor and reused for every frame — rebuilding it per frame is the
single easiest way to make this too slow for a Pi 5 (CLAUDE.md §9.3).
"""

from __future__ import annotations

import logging

import numpy as np
from scipy.interpolate import RBFInterpolator, griddata

from config.schema import InterpolationConfig, InterpolationMethod, ModelExtent

_log = logging.getLogger(__name__)

__all__ = ["FieldInterpolator"]


class FieldInterpolator:
    """Turns ``n`` sensor values at fixed (x, y) into a 2-D temperature field."""

    def __init__(
        self,
        points: np.ndarray,
        extent: ModelExtent,
        method: InterpolationMethod = InterpolationMethod.RBF,
        resolution: int = 100,
    ) -> None:
        self._points = np.asarray(points, dtype=float).reshape(-1, 2)
        if self._points.shape[0] < 1:
            raise ValueError("At least one sensor position is required")
        self._extent = extent
        self._method = method
        self._resolution = int(resolution)

        self._xs = np.linspace(extent.x_min, extent.x_max, self._resolution)
        self._ys = np.linspace(extent.y_min, extent.y_max, self._resolution)
        # 'ij' indexing keeps the result x-major, which is what pyqtgraph's
        # ImageItem expects (image[x, y]).
        grid_x, grid_y = np.meshgrid(self._xs, self._ys, indexing="ij")
        self._grid_points = np.column_stack((grid_x.ravel(), grid_y.ravel()))
        self._grid_shape = grid_x.shape
        self._warned_fallback = False
        self._operator: np.ndarray | None = None

    @classmethod
    def from_config(
        cls, points: np.ndarray, extent: ModelExtent, config: InterpolationConfig
    ) -> "FieldInterpolator":
        return cls(
            points=points,
            extent=extent,
            method=config.method,
            resolution=config.grid_resolution,
        )

    @property
    def extent(self) -> ModelExtent:
        return self._extent

    @property
    def shape(self) -> tuple[int, int]:
        return self._grid_shape

    def interpolate(self, values: np.ndarray) -> np.ndarray:
        """Interpolate one frame. Returns a ``(resolution, resolution)`` array, x-major.

        The common case — every sensor reporting — is a single matrix-vector
        product against a precomputed operator (see :meth:`_operator_matrix`),
        which is what makes this cheap enough to redraw continuously.

        Sensors reporting NaN are excluded from the fit rather than poisoning the
        whole field; that case rebuilds the interpolation the slow way.
        """
        values = np.asarray(values, dtype=float).ravel()
        if values.shape[0] != self._points.shape[0]:
            raise ValueError(
                f"Expected {self._points.shape[0]} values, got {values.shape[0]}"
            )

        finite = np.isfinite(values)
        if finite.all():
            return (self._operator_matrix() @ values).reshape(self._grid_shape)
        if not finite.any():
            return np.full(self._grid_shape, np.nan)
        return self._scatter(self._points[finite], values[finite]).reshape(self._grid_shape)

    def _operator_matrix(self) -> np.ndarray:
        """The interpolation as a fixed ``(grid_points, n_sensors)`` matrix.

        Every method here (RBF, linear, cubic, nearest) is a *linear* map from
        sensor values to grid values: the sensor positions and the grid are fixed,
        so only the coefficients depend on the readings. Interpolating a unit
        reading at sensor *i* therefore gives column *i* of that map, and any
        frame is one ``operator @ values`` product — no refit per frame.

        Built once on first use: ``n_sensors`` fits up front, then ~0.1 ms per
        frame instead of a few milliseconds. Costs
        ``resolution² × n_sensors × 8`` bytes (0.8 MB at 100×100 with 10 sensors).
        """
        if self._operator is None:
            columns = []
            for index in range(self._points.shape[0]):
                unit = np.zeros(self._points.shape[0])
                unit[index] = 1.0
                columns.append(self._scatter(self._points, unit))
            self._operator = np.column_stack(columns)
        return self._operator

    def _scatter(self, points: np.ndarray, good: np.ndarray) -> np.ndarray:
        """Interpolate scattered `points`/`good` onto the grid, flattened."""
        if np.unique(points, axis=0).shape[0] < 3:
            # One or two sensors: nothing to fit.
            return self._nearest(points, good).ravel()

        try:
            if self._method is InterpolationMethod.RBF:
                field = RBFInterpolator(points, good, kernel="thin_plate_spline")(
                    self._grid_points
                )
            elif self._method is InterpolationMethod.NEAREST:
                return self._nearest(points, good).ravel()
            else:
                field = griddata(
                    points, good, self._grid_points, method=self._method.value
                )
                # griddata leaves NaN outside the convex hull; fill from nearest
                # so the model outline is fully coloured.
                holes = ~np.isfinite(field)
                if holes.any():
                    field[holes] = griddata(
                        points, good, self._grid_points[holes], method="nearest"
                    )
        except (ValueError, np.linalg.LinAlgError) as exc:
            if not self._warned_fallback:
                self._warned_fallback = True
                _log.warning(
                    "%s interpolation failed (%s); falling back to nearest-neighbour",
                    self._method.value,
                    exc,
                )
            return self._nearest(points, good).ravel()

        return field

    def _nearest(self, points: np.ndarray, values: np.ndarray) -> np.ndarray:
        if points.shape[0] == 1:
            return np.full(self._grid_shape, float(values[0]))
        field = griddata(points, values, self._grid_points, method="nearest")
        return field.reshape(self._grid_shape)
