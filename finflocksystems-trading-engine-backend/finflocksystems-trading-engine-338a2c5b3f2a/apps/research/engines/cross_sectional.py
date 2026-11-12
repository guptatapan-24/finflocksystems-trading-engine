from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .base import ResearchProtocolContext


@dataclass(frozen=True)
class CrossSectionalBacktestResult:
    returns: np.ndarray
    ranked: tuple[dict, ...]
    selected: tuple[dict, ...]
    diagnostics: dict


class CrossSectionalBacktestEngine:
    """Ranks a point-in-time panel and evaluates only its later holdout returns."""

    def run(self, selector, panel, parameters, context: ResearchProtocolContext):
        ranked = tuple(selector.rank(panel, parameters, context))
        quantile = max(0.01, min(1.0, float(parameters.get("selection_quantile", .20))))
        selected_count = max(1, int(len(ranked) * quantile)) if ranked else 0
        previous_ids = {
            row["instrument_id"] for row in panel
            if row.get("previously_selected") or row.get("current_weight", 0) > 0
        }
        retention_quantile = max(quantile, min(1.0, float(parameters.get("retention_quantile", quantile))))
        retention_count = max(selected_count, int(len(ranked) * retention_quantile))
        retained = [row for row in ranked[:retention_count] if row.get("instrument_id") in previous_ids]
        selected = retained[:selected_count]
        selected_ids = {row.get("instrument_id") for row in selected}
        for row in ranked:
            if len(selected) >= selected_count:
                break
            if row.get("instrument_id") not in selected_ids:
                selected.append(row); selected_ids.add(row.get("instrument_id"))
        if previous_ids and "maximum_turnover" in parameters:
            maximum_replacements = max(0, int(float(parameters["maximum_turnover"]) * selected_count))
            new_rows = [row for row in selected if row.get("instrument_id") not in previous_ids]
            if len(new_rows) > maximum_replacements:
                keep_new = {row.get("instrument_id") for row in new_rows[:maximum_replacements]}
                eligible_previous = [row for row in ranked if row.get("instrument_id") in previous_ids]
                selected = [row for row in selected if row.get("instrument_id") in previous_ids or row.get("instrument_id") in keep_new]
                present = {row.get("instrument_id") for row in selected}
                for row in eligible_previous:
                    if len(selected) >= selected_count:
                        break
                    if row.get("instrument_id") not in present:
                        selected.append(row); present.add(row.get("instrument_id"))
        selected = tuple(selected)
        by_id = {row["instrument_id"]: row for row in panel}
        selected_rows = [by_id[row["instrument_id"]] for row in selected if row.get("instrument_id") in by_id]
        if not selected_rows:
            returns = np.asarray([], dtype=float)
        else:
            count = min(len(row["forward_returns"]) for row in selected_rows)
            matrix = np.asarray([row["forward_returns"][-count:] for row in selected_rows], dtype=float).T
            returns = np.mean(matrix, axis=1)
        return CrossSectionalBacktestResult(
            returns=returns, ranked=ranked, selected=tuple(selected),
            diagnostics={
                "ranked_count": len(ranked), "selected_count": len(selected),
                "retained_count": len({row.get("instrument_id") for row in selected} & previous_ids),
                "turnover_names": len({row.get("instrument_id") for row in selected} - previous_ids) if previous_ids else len(selected),
                "selected_liquidity": min((float(row.get("liquidity", 0)) for row in selected_rows), default=0),
                "point_in_time": all(row.get("feature_available_at") and row.get("decision_date") for row in selected_rows),
            },
        )
