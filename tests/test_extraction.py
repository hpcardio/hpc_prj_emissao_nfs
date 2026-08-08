from __future__ import annotations

import unittest
from datetime import date

from nfs_fortaleza.extraction import (
    ExtractionConfigurationError,
    ExtractionPayload,
)
from nfs_fortaleza.periods import MonthPeriod


class ExtractionPayloadTests(unittest.TestCase):
    def test_scheduled_run_defaults_to_current_month(self) -> None:
        payload = ExtractionPayload.from_mapping(
            {},
            default_date=date(2026, 7, 22),
        )

        self.assertEqual(
            payload.periods,
            (MonthPeriod(2026, 7),),
        )

    def test_accepts_single_nfse_query(self) -> None:
        payload = ExtractionPayload.from_mapping(
            {
                "cnpj": "59.932.105/0001-21",
                "numero_nfse": "8",
            },
            default_date=date(2026, 7, 23),
        )

        self.assertEqual(payload.cnpj, "59932105000121")
        self.assertEqual(payload.numero_nfse, "8")
        self.assertEqual(payload.periods, ())

    def test_accepts_month_period(self) -> None:
        payload = ExtractionPayload.from_mapping(
            {"competencia": "07/2026"},
            default_date=date(2026, 7, 23),
        )

        self.assertEqual(payload.periods, (MonthPeriod(2026, 7),))

    def test_rejects_mixed_filters(self) -> None:
        with self.assertRaises(ExtractionConfigurationError):
            ExtractionPayload.from_mapping(
                {
                    "cnpj": "59932105000121",
                    "numero_nfse": "8",
                    "competencia": "07/2026",
                },
                default_date=date(2026, 7, 23),
            )


if __name__ == "__main__":
    unittest.main()
