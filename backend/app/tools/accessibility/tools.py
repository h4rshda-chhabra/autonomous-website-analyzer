from __future__ import annotations

from .schemas import (
    AxeCoreScannerInput, AxeCoreScannerOutput,
    ContrastCheckerInput, ContrastCheckerOutput,
)


async def run_axe_core_scanner(inp: AxeCoreScannerInput) -> AxeCoreScannerOutput:
    raise NotImplementedError(
        "AxeCoreScanner is not implemented in Phase 0. "
        "Phase 1 implementation: inject axe-core into a Playwright page via "
        "page.evaluate(), run axe.run() with configured rules, "
        "return violations grouped by rule_id with affected element selectors."
    )


async def run_contrast_checker(inp: ContrastCheckerInput) -> ContrastCheckerOutput:
    raise NotImplementedError(
        "ContrastChecker is not implemented in Phase 0. "
        "Phase 1 implementation: use Playwright to compute getComputedStyle() for "
        "all text elements, calculate WCAG contrast ratios, identify failures "
        "against AA (4.5:1 normal, 3:1 large text) thresholds."
    )
