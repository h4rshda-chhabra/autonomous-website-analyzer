from __future__ import annotations

from .schemas import (
    LighthouseRunnerInput, LighthouseRunnerOutput,
    AssetAnalyzerInput, AssetAnalyzerOutput,
)


async def run_lighthouse_runner(inp: LighthouseRunnerInput) -> LighthouseRunnerOutput:
    raise NotImplementedError(
        "LighthouseRunner is not implemented in Phase 0. "
        "Phase 1 implementation: invoke the Lighthouse CLI via asyncio.subprocess, "
        "parse the resulting JSON report for CWV metrics (LCP, CLS, TBT, FCP, SI), "
        "run inp.runs times and return the median scores."
    )


async def run_asset_analyzer(inp: AssetAnalyzerInput) -> AssetAnalyzerOutput:
    raise NotImplementedError(
        "AssetAnalyzer is not implemented in Phase 0. "
        "Phase 1 implementation: analyze inp.network_requests to identify "
        "render-blocking scripts, uncompressed responses, legacy image formats (JPEG>WebP), "
        "missing dimensions on images, and total page weight breakdown."
    )
