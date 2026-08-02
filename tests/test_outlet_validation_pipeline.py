from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cfdpipe.outlet_validation_pipeline import _history_summary


class OutletValidationPipelineTests(unittest.TestCase):
    def test_su2_spaced_quoted_residual_headers_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.csv"
            history.write_text(
                '"Time_Iter","Outer_Iter","Inner_Iter",    "rms[Rho]"    ,    "rms[RhoU]"   \n'
                "0,0,0,-1.0,2.0\n"
                "0,0,1,-2.0,1.0\n",
                encoding="utf-8",
            )

            summary = _history_summary(history)

        self.assertEqual(summary["row_count"], 2)
        self.assertEqual(set(summary["residuals"]), {"rms[Rho]", "rms[RhoU]"})
        self.assertEqual(summary["residuals"]["rms[Rho]"]["initial"], -1.0)
        self.assertEqual(summary["residuals"]["rms[Rho]"]["final"], -2.0)
        self.assertFalse(summary["convergence_claimed"])


if __name__ == "__main__":
    unittest.main()
