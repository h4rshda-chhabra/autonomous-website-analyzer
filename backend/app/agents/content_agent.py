from __future__ import annotations

from typing import List

from app.models import (
    AgentType,
    FindingCategory,
    ImplementationEffort,
    Severity,
    TraceEventType,
)
from app.tools.content.schemas import ContentExtractorInput
from .base_agent import BaseAgent


class ContentAgent(BaseAgent):
    """
    Evaluates content quality and goal alignment.
    Combines deterministic extraction (ContentExtractor) with AI scoring (ClaudeContentAnalyzer).
    Deterministic findings have confidence=0.95; AI findings carry Claude's reported confidence.
    """

    def agent_type(self) -> AgentType:
        return AgentType.CONTENT

    def allowed_tools(self) -> List[str]:
        return ["ContentExtractor", "ClaudeContentAnalyzer"]

    async def execute(self) -> None:
        site_profile = await self.get_site_profile()
        audit_plan = await self.get_audit_plan()
        agent_config = audit_plan.get_config(AgentType.CONTENT)

        playwright_output = await self.get_recon_artifact("playwright_output")
        rendered_html = getattr(playwright_output, "rendered_html", "") if playwright_output else ""
        url = site_profile.final_url or site_profile.url

        await self.emit_trace(
            TraceEventType.REASONING,
            reasoning=f"Site category: {site_profile.category.value}. "
                      f"Primary goal: {site_profile.primary_goals[0].goal if site_profile.primary_goals else 'unknown'}.",
        )

        # ── Tool 1: ContentExtractor (deterministic) ──────────────────────────
        extract_result = await self.run_tool(
            "ContentExtractor",
            ContentExtractorInput(
                html=rendered_html,
                url=url,
                site_category=site_profile.category.value,
            ),
            action_summary="Extracting main content, reading level, and CTA inventory",
        )

        extracted_content = None

        if not extract_result.success:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"ContentExtractor failed: {extract_result.error.message if extract_result.error else 'unknown'}. "
                            "Cannot run ClaudeContentAnalyzer without extracted content.",
            )
            await self.create_finding(
                category=FindingCategory.CONTENT_QUALITY,
                title="Content extraction failed — quality analysis incomplete",
                description="The ContentExtractor could not parse the page's main content. "
                            "This may indicate heavy JavaScript rendering, a gated content wall, "
                            "or an unsupported page structure.",
                severity=Severity.INFO,
                business_impact="Without content extraction, quality and readability issues may be missed.",
                impact_score=1,
                effort=ImplementationEffort.EASY,
                effort_hours_min=0,
                effort_hours_max=1,
                fix_description="Verify the page is publicly accessible and renders visible text content.",
                tool_name="ContentExtractor",
                evidence_raw_data={"error": extract_result.error.message if extract_result.error else "unknown"},
                confidence=0.80,
            )
            await self.complete()
            return

        extracted_content = extract_result.data
        word_count = getattr(extracted_content, "word_count", 0)
        reading_grade = getattr(extracted_content, "reading_grade", None)
        cta_count = getattr(extracted_content, "cta_count", 0)
        primary_cta_above_fold = getattr(extracted_content, "primary_cta_above_fold", True)
        passive_voice_pct = getattr(extracted_content, "passive_voice_percentage", 0.0)

        await self.emit_trace(
            TraceEventType.OBSERVATION,
            observation=f"Extracted: {word_count} words, reading grade {reading_grade}, "
                        f"{cta_count} CTA(s), primary CTA above fold: {primary_cta_above_fold}.",
        )

        # Deterministic findings (confidence=0.95)
        if word_count < 150:
            await self.create_finding(
                category=FindingCategory.CONTENT_QUALITY,
                title=f"Page content is thin — only {word_count} words detected",
                description=f"The main content area contains only {word_count} words. "
                            "Google considers pages with very little content as 'thin' and may rank them lower.",
                severity=Severity.HIGH,
                business_impact="Thin content pages rank poorly in search results and provide low value to visitors. "
                                "Google's quality guidelines explicitly penalise pages with insufficient content.",
                impact_score=7,
                effort=ImplementationEffort.MEDIUM,
                effort_hours_min=2,
                effort_hours_max=8,
                fix_description="Expand the page content to at least 300–500 words "
                                "that directly answer the user's likely query.",
                tool_name="ContentExtractor",
                evidence_raw_data={"word_count": word_count},
                confidence=0.95,
                metric_value=f"{word_count} words",
                metric_threshold="> 150 words",
            )

        if reading_grade is not None and reading_grade > 12:
            await self.create_finding(
                category=FindingCategory.READABILITY,
                title=f"Reading level is college-grade ({reading_grade:.0f}) — content may be too complex",
                description=f"Flesch-Kincaid grade level: {reading_grade:.0f}. "
                            "Most web content is most effective at grades 7–9 for broad audiences.",
                severity=Severity.MEDIUM,
                business_impact="Content written above grade 10 loses a significant portion of potential readers, "
                                "increasing bounce rate and reducing conversion.",
                impact_score=4,
                effort=ImplementationEffort.MEDIUM,
                effort_hours_min=2,
                effort_hours_max=8,
                fix_description="Simplify sentences, replace jargon with plain language, "
                                "and break up long paragraphs. Target grade 7–9.",
                tool_name="ContentExtractor",
                evidence_raw_data={"reading_grade": reading_grade},
                confidence=0.88,
                metric_value=f"Grade {reading_grade:.0f}",
                metric_threshold="Grade 7–9",
            )

        if cta_count == 0:
            await self.create_finding(
                category=FindingCategory.CTA,
                title="No calls-to-action detected on the page",
                description="The page contains no detectable CTA buttons or links. "
                            "Without a clear next step, visitors have no guided path to conversion.",
                severity=Severity.HIGH,
                business_impact="Pages without CTAs have significantly lower conversion rates. "
                                "Visitors who don't know what to do next tend to leave.",
                impact_score=8,
                effort=ImplementationEffort.EASY,
                effort_hours_min=1,
                effort_hours_max=3,
                fix_description="Add at least one prominent CTA above the fold that aligns with the page's goal "
                                f"(e.g. '{site_profile.primary_goals[0].goal if site_profile.primary_goals else 'primary goal'}').",
                tool_name="ContentExtractor",
                evidence_raw_data={"cta_count": 0},
                confidence=0.90,
                tags=["conversion", "quick-win"],
            )
        elif not primary_cta_above_fold:
            await self.create_finding(
                category=FindingCategory.CTA,
                title="Primary call-to-action is not visible above the fold",
                description="No CTA button or link is visible in the initial viewport without scrolling. "
                            "Users who don't scroll will miss the primary conversion opportunity.",
                severity=Severity.MEDIUM,
                business_impact="Above-fold CTAs convert significantly better than below-fold CTAs. "
                                "Moving the primary CTA above the fold is one of the highest-ROI content changes.",
                impact_score=6,
                effort=ImplementationEffort.EASY,
                effort_hours_min=1,
                effort_hours_max=3,
                fix_description="Move or duplicate the primary CTA to be visible in the first viewport. "
                                "The hero section should always contain a CTA.",
                tool_name="ContentExtractor",
                evidence_raw_data={"primary_cta_above_fold": False},
                confidence=0.88,
                tags=["conversion"],
            )

        if passive_voice_pct > 0.30:
            await self.create_finding(
                category=FindingCategory.READABILITY,
                title=f"High passive voice usage ({passive_voice_pct:.0%}) weakens content clarity",
                description=f"Over 30% of sentences use passive voice. "
                            "Active voice is clearer, more direct, and better for conversion copy.",
                severity=Severity.LOW,
                business_impact="Passive voice reduces clarity and authority in marketing copy, "
                                "lowering reader engagement and conversion intent.",
                impact_score=2,
                effort=ImplementationEffort.MEDIUM,
                effort_hours_min=1,
                effort_hours_max=4,
                fix_description="Rewrite passive sentences to active voice. "
                                "E.g. 'Results are delivered' → 'We deliver results'.",
                tool_name="ContentExtractor",
                evidence_raw_data={"passive_voice_pct": passive_voice_pct},
                confidence=0.80,
                metric_value=f"{passive_voice_pct:.0%}",
                metric_threshold="< 30%",
            )

        # ── AI content analysis (via LLMClient — provider-agnostic) ─────────────
        main_text = getattr(extracted_content, "main_content_text", "")
        above_fold_text = getattr(extracted_content, "above_fold_text", "")
        cta_texts = getattr(extracted_content, "cta_texts", [])

        from app.infrastructure.settings import settings
        truncated = len(main_text) > settings.max_html_chars_for_ai
        main_text_input = main_text[:settings.max_html_chars_for_ai]

        primary_goal = site_profile.primary_goals[0].goal if site_profile.primary_goals else "unknown"

        if self._llm and self._llm.is_available:
            ai_data = await self._run_content_llm(
                main_text=main_text_input,
                above_fold_text=above_fold_text,
                cta_texts=cta_texts,
                site_category=site_profile.category.value,
                primary_goal=primary_goal,
                word_count=word_count,
                truncated=truncated,
                max_tokens=settings.openrouter_content_max_tokens,
            )
        else:
            ai_data = None

        if ai_data:
            quality_score = ai_data.get("quality_score")
            vp_score = ai_data.get("value_proposition_clarity_score")
            ai_confidence = float(ai_data.get("confidence", 0.70))
            if truncated:
                ai_confidence = min(ai_confidence, 0.75)
            if word_count < 200:
                ai_confidence = min(ai_confidence, 0.65)

            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation=f"AI scored: quality={quality_score}/10, "
                            f"value_proposition={vp_score}/10. "
                            f"Confidence: {ai_confidence:.0%}.",
            )

            if ai_confidence >= 0.65:
                if quality_score is not None and quality_score < 5:
                    sev = Severity.HIGH if quality_score < 4 else Severity.MEDIUM
                    await self.create_finding(
                        category=FindingCategory.CONTENT_QUALITY,
                        title=f"Content quality score is low ({quality_score}/10)",
                        description=(
                            f"AI scored the page content {quality_score}/10 for overall quality. "
                            f"Key issues: {ai_data.get('top_issue', 'see AI analysis')}."
                        ),
                        severity=sev,
                        business_impact=(
                            "Low-quality content reduces time on page, increases bounce rate, "
                            "and signals poor authority to search engines."
                        ),
                        impact_score=7 if sev == Severity.HIGH else 5,
                        effort=ImplementationEffort.MEDIUM,
                        effort_hours_min=4,
                        effort_hours_max=16,
                        fix_description=(
                            f"Rewrite focus areas identified by AI: "
                            f"{ai_data.get('rewrite_suggestions', 'improve clarity and depth')}."
                        ),
                        tool_name="LLMContentAnalyzer",
                        evidence_raw_data={"quality_score": quality_score, "ai_confidence": ai_confidence},
                        confidence=ai_confidence,
                    )

                if vp_score is not None and vp_score < 5:
                    await self.create_finding(
                        category=FindingCategory.VALUE_PROPOSITION,
                        title=f"Value proposition is unclear (AI score: {vp_score}/10)",
                        description=(
                            f"AI assessed value proposition clarity as {vp_score}/10. "
                            "Visitors cannot quickly understand what differentiates this offering."
                        ),
                        severity=Severity.HIGH,
                        business_impact=(
                            "An unclear value proposition is the leading cause of high bounce rates. "
                            "Visitors who don't understand the offering within 5 seconds leave."
                        ),
                        impact_score=8,
                        effort=ImplementationEffort.MEDIUM,
                        effort_hours_min=2,
                        effort_hours_max=8,
                        fix_description=(
                            "Add a clear headline that states: who this is for, what problem it solves, "
                            "and what makes it better than alternatives. Test with 5-second test."
                        ),
                        tool_name="LLMContentAnalyzer",
                        evidence_raw_data={"vp_score": vp_score},
                        confidence=ai_confidence,
                    )
        else:
            await self.emit_trace(
                TraceEventType.OBSERVATION,
                observation="AI content analysis skipped (LLM unavailable). Deterministic findings only.",
            )

        await self.emit_trace(
            TraceEventType.REASONING,
            reasoning=f"Content analysis complete. {self._findings_written} finding(s) written.",
        )
        await self.complete()

    async def _run_content_llm(
        self,
        *,
        main_text: str,
        above_fold_text: str,
        cta_texts: list,
        site_category: str,
        primary_goal: str,
        word_count: int,
        truncated: bool,
        max_tokens: int,
    ):
        """Calls the injected LLMClient for AI content scoring. Returns parsed dict or None."""
        import json as _json
        from app.llm.base import LLMMessage

        system_prompt = (
            "You are a website conversion and content quality expert. "
            "Analyse the provided website content and return ONLY a valid JSON object. "
            "No explanation, no markdown — just the JSON."
        )
        user_prompt = (
            f"Site category: {site_category}\n"
            f"Primary goal: {primary_goal}\n"
            f"Word count: {word_count}{' (truncated)' if truncated else ''}\n"
            f"Above-fold text: {above_fold_text[:500] or '(not extracted)'}\n"
            f"CTA texts: {', '.join(cta_texts[:5]) or '(none detected)'}\n\n"
            f"Main content:\n{main_text[:4000]}\n\n"
            "Return a JSON object with these keys:\n"
            '  "quality_score": integer 1-10 (overall content quality),\n'
            '  "value_proposition_clarity_score": integer 1-10,\n'
            '  "goal_alignment_score": integer 1-10,\n'
            '  "top_issue": string (most important problem in one sentence),\n'
            '  "rewrite_suggestions": string (2-3 specific improvement suggestions),\n'
            '  "confidence": float 0.0-1.0 (your confidence in this assessment)\n'
        )

        resp = await self._llm.complete(
            [LLMMessage(role="system", content=system_prompt),
             LLMMessage(role="user", content=user_prompt)],
            max_tokens=max_tokens,
            temperature=0.1,
            json_mode=True,
        )

        if not resp.success:
            await self.add_ai_warning(
                f"Content Agent: AI analysis failed — {resp.error}. Deterministic findings only."
            )
            return None

        try:
            raw = resp.content.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return _json.loads(raw)
        except Exception as exc:
            await self.add_ai_warning(
                f"Content Agent: AI response parse error — {exc}. Deterministic findings only."
            )
            return None
