from __future__ import annotations

import os
import re
import time
from typing import Tuple, List, Optional, Dict

try:  # optional, used for token budgeting
    import tiktoken  # type: ignore
except Exception:  # pragma: no cover
    tiktoken = None

from .domain import PassState, StageState, PassMetrics, PassTexts, RunResult, StrategyPlan
from .settings import Settings
from .language_model import LanguageModel
from .storage import OutputSink, LocalSink
from .pipeline import stealth_prep_pipeline, post_pass_adjustments, protect_markdown_structures, restore_markdown_structures, validate_markdown_structures, generate_sidecar_annotations, inject_inline_annotations
from .utils import read_text_from_file, write_text_to_file, derive_history_profile
from .logger import log_event, log_exception, log_performance, log_metrics
from .core.file_versions import file_version_manager


class RefinementPipeline:
    def __init__(self, settings: Settings, model: LanguageModel):
        self.settings = settings
        self.model = model

        # Phase-0 configuration (can be toggled via env without code changes)
        self.max_input_tokens: int = int(os.getenv("REFINER_MAX_INPUT_TOKENS", "0") or 0)
        self.enable_domain_chunk: bool = os.getenv("REFINER_DOMAIN_CHUNK", "1") == "true" or os.getenv("REFINER_DOMAIN_CHUNK", "1") == "1"
        self.enable_placeholders: bool = os.getenv("REFINER_PLACEHOLDERS", "0") == "true" or os.getenv("REFINER_PLACEHOLDERS", "0") == "1"

    # ---- Phase-0 helpers ----
    def _get_model_name(self) -> Optional[str]:
        try:
            return getattr(self.model, 'model', None)  # OpenAIModel has .model
        except Exception:
            return None

    def _get_tokenizer(self, model_name: Optional[str]):
        if not tiktoken:
            return None
        try:
            return tiktoken.encoding_for_model(model_name) if model_name else tiktoken.get_encoding("cl100k_base")
        except Exception:
            try:
                return tiktoken.get_encoding("cl100k_base")
            except Exception:
                return None

    def _count_tokens(self, text: str, model_name: Optional[str]) -> int:
        enc = self._get_tokenizer(model_name)
        if enc is None:
            # heuristic fallback ~4 chars/token
            return (len(text) + 3) // 4
        return len(enc.encode(text))

    _DOMAIN_SPLITS = [
        r"\n##\s*(Findings|Impression|Assessment|Plan)\b",
        r"\b(?:ICD-10|ICD-9)\s*[:：]?\s*[A-Z0-9\.\-]+",
        r"\b(?:U\.S\.C\.|C\.F\.R\.|F\.Supp\.|F\.3d|N\.E\.2d|F\.\s?App’x)\b",
        r"\n(?:Facts|Issue|Holding|Reasoning)\s*[:：]",
    ]

    def _split_domain_sections(self, text: str) -> List[str]:
        if not text or not text.strip():
            return []
        pattern = "(" + "|".join(self._DOMAIN_SPLITS) + ")"
        parts = re.split(pattern, text, flags=re.IGNORECASE)
        chunks: List[str] = []
        buf: List[str] = []
        for seg in parts:
            if seg is None:
                continue
            if re.match("|".join(self._DOMAIN_SPLITS), seg, flags=re.IGNORECASE):
                if buf:
                    joined = "\n".join(p for p in buf if p)
                    if joined.strip():
                        chunks.append(joined)
                    buf = []
                buf.append(seg.strip())
            else:
                if seg.strip():
                    buf.append(seg.strip())
        if buf:
            joined = "\n".join(p for p in buf if p)
            if joined.strip():
                chunks.append(joined)
        return chunks if chunks else [text]

    def _pack_to_budget(self, sections: List[str], system: str, model_name: Optional[str], max_in_tokens: int) -> List[str]:
        if max_in_tokens <= 0:
            return ["\n\n".join(sections)]
        out: List[str] = []
        cur = ""
        for sec in sections:
            if not sec:
                continue
            cand = (cur + "\n\n" + sec) if cur else sec
            if self._count_tokens(system + "\n" + cand, model_name) <= max_in_tokens:
                cur = cand
            else:
                if cur:
                    out.append(cur)
                    cur = ""
                # if a single section still too big, split on sentences
                if self._count_tokens(system + "\n" + sec, model_name) > max_in_tokens:
                    sentences = re.split(r"(?<=[\.!?])\s+", sec)
                    buf = ""
                    for s in sentences:
                        t = (buf + " " + s).strip() if buf else s
                        if self._count_tokens(system + "\n" + t, model_name) <= max_in_tokens:
                            buf = t
                        else:
                            if buf:
                                out.append(buf)
                                buf = ""
                            if self._count_tokens(system + "\n" + s, model_name) <= max_in_tokens:
                                buf = s
                            else:
                                out.append(s)
                    if buf:
                        out.append(buf)
                else:
                    out.append(sec)
        if cur:
            out.append(cur)
        return out if out else ["\n\n".join(sections)]

    _PH_LONG_ID = re.compile(r"\b(ICD-10|ICD-9)\s*[:：]?\s*[A-Z0-9\.\-]+|\b\d+\s+U\.S\.C\.\s*§\s*\w[\w\-\.()]*", re.IGNORECASE)

    def _apply_placeholders(self, text: str) -> Tuple[str, Dict[str, str]]:
        mapping: Dict[str, str] = {}
        if not text:
            return text, mapping
        idx = 0
        def _sub(m):
            nonlocal idx
            idx += 1
            token = f"[P{idx}]"
            mapping[token] = m.group(0)
            return token
        return self._PH_LONG_ID.sub(_sub, text), mapping

    def _restore_placeholders(self, text: str, mapping: Dict[str, str]) -> str:
        if not mapping:
            return text
        out = text
        # Track per-pass token counts (preflight vs used) across phases/chunks
        pass_pre_tokens: int = 0
        pass_used_in_tokens: int = 0
        for k, v in mapping.items():
            out = out.replace(k, v)
        return out

    def run_pass(
        self,
        input_path: str,
        pass_index: int,
        prev_final_text: str | None,
        entropy_level: str,
        output_sink: OutputSink | None,
        drive_title_base: str | None = None,
        heuristics_overrides: dict | None = None,
        job_id: str | None = None,
    ) -> Tuple[PassState, RunResult, str]:
        print(f"PIPELINE: run_pass started for pass {pass_index}")
        
        # Track schema usage for analytics
        try:
            from backend.language_model import analytics_store
            if heuristics_overrides and 'schemaLevels' in heuristics_overrides:
                schema_levels = heuristics_overrides['schemaLevels']
                if isinstance(schema_levels, dict):
                    for schema_id, schema_level in schema_levels.items():
                        analytics_store.track_schema_usage(schema_id, int(schema_level))
        except Exception as e:
            print(f"PIPELINE: Failed to track schema usage: {e}")
        
        # Build effective heuristics (stateless per request)
        def _deep_merge(a: dict, b: dict) -> dict:
            out = dict(a or {})
            for k, v in (b or {}).items():
                if isinstance(v, dict) and isinstance(out.get(k), dict):
                    out[k] = _deep_merge(out[k], v)
                else:
                    out[k] = v
            return out
        try:
            from copy import deepcopy as _deepcopy
            base_heur = _deepcopy(self.settings.heuristics) if isinstance(self.settings.heuristics, dict) else {}
        except Exception:
            base_heur = dict(self.settings.heuristics or {}) if isinstance(self.settings.heuristics, dict) else {}
        heur = _deep_merge(base_heur, heuristics_overrides or {})
        ps = PassState(index=pass_index)
        for name in ["read","prep","refine","post","write","upload"]:
            ps.stages[name] = StageState(name=name)

        result = RunResult(file_path=input_path, pass_index=pass_index, success=False)

        # Read
        print(f"PIPELINE: Starting READ stage for pass {pass_index}")
        t0 = time.perf_counter()
        raw = read_text_from_file(input_path)
        ps.stages["read"].status = "ok"
        ps.stages["read"].duration_ms = (time.perf_counter() - t0) * 1000.0
        print(f"PIPELINE: READ stage completed ({ps.stages['read'].duration_ms:.2f}ms), read {len(raw)} chars")
        log_performance("READ_STAGE", ps.stages["read"].duration_ms, 
                       file=os.path.basename(input_path), pass_index=pass_index)

        # Prep
        t0 = time.perf_counter()
        # Map refiner_control strength (0..3 UI) -> knobs
        heur = heur or {}
        try:
            strength_level = float((heur.get('schema_flags', {}) or {}).get('refiner_control') or heur.get('refiner_control', 0))
        except Exception:
            strength_level = 0.0
        strength_norm = max(0.0, min(1.0, strength_level / 3.0))
        # Derive knobs
        entropy_from_strength = 'very_high' if strength_norm > 0.75 else 'high' if strength_norm > 0.5 else 'medium' if strength_norm > 0.25 else entropy_level
        allow_structure_change = strength_norm < 0.8  # stronger => keep more structure; can flip later
        dry_run = bool(heur.get('refiner_dry_run', False))

        # Prep with adjusted entropy
        print(f"PIPELINE: Starting PREP stage for pass {pass_index}")
        p1, _, _ = stealth_prep_pipeline(raw, [], heur, entropy_level=entropy_from_strength)
        ps.stages["prep"].status = "ok"
        ps.stages["prep"].duration_ms = (time.perf_counter() - t0) * 1000.0
        print(f"PIPELINE: PREP stage completed ({ps.stages['prep'].duration_ms:.2f}ms)")

        # Refine (LLM)
        print(f"PIPELINE: Starting REFINE stage for pass {pass_index}")
        ps.stages["refine"].status = "running"
        t0 = time.perf_counter()
        # Formatting safeguards: pre-protect structures when enabled
        mapping = None
        try:
            fs_cfg = (heur or {}).get('formatting_safeguards') or {}
            fs_mode = (fs_cfg.get('mode') if isinstance(fs_cfg, dict) else 'smart') or 'smart'  # 'strict'|'smart'
            if fs_cfg is True or (isinstance(fs_cfg, dict) and fs_cfg.get('enabled', True)):
                p1, mapping = protect_markdown_structures(p1, strict=(fs_mode == 'strict'))
        except Exception:
            mapping = None

        if dry_run:
            print(f"PIPELINE: DRY RUN mode for pass {pass_index}")
            # Emit plan + deltas only, no actual generation
            w, r, a, plan = self._analyze_strategy(p1, heur)
            from .logger import log_event as _log
            _log("DRY_RUN", f"weights={w} rationale={r[:160]} approach={a[:160]} plan={{'primary':'{plan.primary_strategy}','secondary':'{plan.secondary_strategy}','modulators':{plan.modulators}}}")
            refined = p1  # no change in dry run
            macro_results = {}  # No macro results in dry run
        else:
            # Set job_id for cost tracking
            if job_id:
                self._current_job_id = job_id
                self._pass_costs = []  # Reset costs for this pass
            
            # Phase-0: optional domain-aware chunking and placeholder compression will be applied
            print(f"PIPELINE: Calling _three_phase_refine for pass {pass_index}")
            refined, macro_results = self._three_phase_refine(p1, heur)
            print(f"PIPELINE: _three_phase_refine completed for pass {pass_index}")
        # Critic pass (regex/rule-based) on risky markers and clichés
        try:
            refined = self._critic_span_rewrite(refined)
        except Exception as e:
            log_exception("CRITIC_PASS_ERROR", e)
            # Continue with refined text as-is if critic pass fails
        # Restore structures after refine
        try:
            if mapping:
                refined = restore_markdown_structures(refined, mapping)
        except Exception as e:
            log_exception("STRUCTURE_RESTORE_ERROR", e)
            # Continue with refined text even if structure restore fails
        ps.stages["refine"].status = "ok"
        ps.stages["refine"].duration_ms = (time.perf_counter() - t0) * 1000.0
        log_performance("REFINE_STAGE", ps.stages["refine"].duration_ms,
                       file=os.path.basename(input_path), pass_index=pass_index,
                       dry_run=dry_run, entropy_level=entropy_from_strength)

        # Post
        t0 = time.perf_counter()
        final = post_pass_adjustments(refined, heur)
        
        # Apply macro analysis recommendations if available
        try:
            if macro_results:
                final = self._apply_macro_recommendations(final, macro_results)
                from .logger import log_event as _log
                _log("MACRO_APPLICATION", f"Applied macro recommendations: headings={len(macro_results.get('needs_headings', []))}, separators={len(macro_results.get('needs_separators', []))}, redundant={len(macro_results.get('redundant_pairs', []))}")
        except Exception as e:
            from .logger import log_event as _log
            _log("MACRO_APPLICATION_ERROR", f"Failed to apply macro recommendations: {str(e)}")
        # Annotation mode (inline or sidecar JSON)
        try:
            ann_cfg = (heur or {}).get('annotation_mode') or {}
            enabled = (ann_cfg is True) or (isinstance(ann_cfg, dict) and ann_cfg.get('enabled', False))
            if enabled:
                verbosity = (ann_cfg.get('verbosity') or 'low') if isinstance(ann_cfg, dict) else 'low'
                mode = (ann_cfg.get('mode') or 'inline') if isinstance(ann_cfg, dict) else 'inline'
                anns = generate_sidecar_annotations(ps.texts.prev or "", final, verbosity=verbosity)
                if mode == 'inline':
                    final = inject_inline_annotations(final, anns, verbosity=verbosity)
                else:
                    # sidecar: attach to result.extra
                    try:
                        result.extra['annotations'] = [
                            {'start': a.start, 'end': a.end, 'rationale': a.rationale, 'category': a.category}
                            for a in anns
                        ]
                    except Exception:
                        pass
        except Exception:
            pass
        # Post validators (warn-only): do not fail, but can be logged
        try:
            diags = validate_markdown_structures(final)
            if any(diags.values()):
                from .logger import log_event as _log
                _log("FORMAT_GUARD", f"open_fences={diags['open_fences']} header_like={diags['header_like']}")
        except Exception:
            pass
        ps.stages["post"].status = "ok"
        ps.stages["post"].duration_ms = (time.perf_counter() - t0) * 1000.0

        # Normalize paragraphs for DOCX
        final_norm = re.sub(r"\r\n?", "\n", final).strip() + "\n"

        # Guard: enforce minimum word ratio vs raw input
        try:
            raw_wc = len((raw or "").split())
            fin_wc = len((final_norm or "").split())
            ratio = (fin_wc / max(1, raw_wc))
            if ratio < float(self.settings.min_word_ratio):
                from .logger import log_event as _log
                _log("WORD_RATIO_GUARD", f"Refined text too short (ratio={ratio:.3f} < {self.settings.min_word_ratio}), applying fallback refinement")
                # Fallback: apply a lighter refinement pass instead of discarding everything
                try:
                    # Use a more conservative approach - just clean up the refined text
                    fallback_text = re.sub(r"\n{3,}", "\n\n", refined).strip() + "\n"
                    # If still too short, blend with original
                    fallback_wc = len(fallback_text.split())
                    fallback_ratio = (fallback_wc / max(1, raw_wc))
                    if fallback_ratio >= float(self.settings.min_word_ratio):
                        final_norm = fallback_text
                    else:
                        # Blend refined content with original structure
                        final_norm = self._blend_refined_with_original(refined, raw)
                except Exception as blend_error:
                    _log("FALLBACK_BLEND_ERROR", f"Fallback blending failed: {str(blend_error)}")
                    # Last resort: return lightly cleaned original
                    final_norm = re.sub(r"\n{3,}", "\n\n", raw).strip() + "\n"
        except Exception as e:
            from .logger import log_event as _log
            _log("WORD_RATIO_GUARD_ERROR", f"Word ratio guard failed: {str(e)}")

        # Write local temp
        t0 = time.perf_counter()
        base, ext = os.path.splitext(os.path.basename(input_path))
        local_out = write_text_to_file(
            os.getenv("TMPDIR", os.getenv("TEMP", "/tmp")),
            base,
            ext,
            final_norm,
            input_path,
            pass_index,
        )
        ps.stages["write"].status = "ok"
        ps.stages["write"].duration_ms = (time.perf_counter() - t0) * 1000.0
        result.local_path = local_out

        # Upload / move
        if output_sink is not None:
            t0 = time.perf_counter()
            dest_name = (drive_title_base or base)
            dest_name = re.sub(r"(?:_pass\d+)+$", "", dest_name, flags=re.IGNORECASE)
            dest_name = f"{dest_name}_pass{pass_index}"
            dest_ref = output_sink.write(local_out, dest_name)
            ps.stages["upload"].status = "ok"
            ps.stages["upload"].duration_ms = (time.perf_counter() - t0) * 1000.0
            # If writing to LocalSink, dest_ref is the filesystem path. Preserve it in local_path.
            try:
                if isinstance(output_sink, LocalSink):
                    result.local_path = dest_ref
                else:
                    result.doc_id = dest_ref
            except Exception:
                # Best effort fallback: if dest looks like a path, set local_path
                if isinstance(dest_ref, str) and os.path.sep in dest_ref:
                    result.local_path = dest_ref
                else:
                    result.doc_id = dest_ref
            log_event("UPLOAD", f"pass={pass_index} dest={dest_ref}")
        else:
            ps.stages["upload"].status = "skipped"

        # Metrics + texts
        ps.texts.prev = prev_final_text or raw
        ps.texts.final = final_norm
        ps.metrics.latency_ms_avg = self._avg_stage_latency(ps)
        result.success = True

        # Log core metrics for Pass Spark style analysis
        try:
            import difflib as _difflib
            import re as _re
            import statistics as _stats
            prev_text = ps.texts.prev or ""
            cur_text = final_norm or ""

            # Change/tension proxy
            ratio = _difflib.SequenceMatcher(None, prev_text, cur_text).ratio()
            change_pct = max(0.0, min(100.0, (1.0 - ratio) * 100.0))
            tension_pct = change_pct
            
            # Store in ps.metrics so downstream code can access it
            ps.metrics.change_pct = change_pct
            ps.metrics.tension_pct = tension_pct

            # Use structured logging for metrics
            log_metrics("PASS_METRICS", {
                "file": os.path.basename(input_path),
                "pass_index": pass_index,
                "change_pct": change_pct,
                "tension_pct": tension_pct,
                "avg_ms": ps.metrics.latency_ms_avg,
                "prev_word_count": len(prev_text.split()),
                "curr_word_count": len(cur_text.split())
            })

            # ----- Toggle-aligned metrics (mirror GUI for log analysis) -----
            _PUNCT_CHARS = r"""[.,;:!?'"“”‘’—–\-()]"""
            _WORD_RE = _re.compile(r"[A-Za-z0-9']+")
            _SENT_SPLIT_RE = _re.compile(r"(?<=[.!?])\s+")

            def _words(t: str):
                return _WORD_RE.findall((t or "").lower())

            def _sentences(t: str):
                t = (t or "").strip()
                return [s for s in _SENT_SPLIT_RE.split(t) if s]

            def _punct_density_per_100w(t: str) -> float:
                w = max(1, len(_words(t)))
                p = len(_re.findall(_PUNCT_CHARS, t or ""))
                return (p / w) * 100.0

            _TRANSITIONS = [
                "however","moreover","furthermore","therefore","hence","thus",
                "in addition","additionally","meanwhile","in contrast","by contrast",
                "on the other hand","for example","for instance","in other words",
                "as a result","consequently","nevertheless","nonetheless","similarly",
                "that said","even so",
            ]
            def _count_transitions(t: str) -> int:
                tl = (t or "").lower()
                c = 0
                for phrase in _TRANSITIONS:
                    c += len(_re.findall(rf"\b{_re.escape(phrase)}\b", tl))
                return c

            def _rhythm_cv(t: str) -> float:
                sents = _sentences(t)
                if not sents: return 0.0
                lens = [max(1, len(_words(s))) for s in sents]
                m = _stats.mean(lens)
                if m <= 0: return 0.0
                sd = _stats.pstdev(lens)
                return float(sd / m)

            def _grammar_issues(t: str) -> int:
                issues = 0
                issues += len(_re.findall(r"  +", t or ""))
                issues += len(_re.findall(r"\s+[,.!?;:]", t or ""))
                issues += len(_re.findall(r"[,.!?;:][A-Za-z]", t or ""))
                issues += len(_re.findall(r"\bi\b", (t or "").lower()))
                issues += len(_re.findall(r"[!?]{2,}", t or ""))
                return issues

            def _word_level_ops(a: str, b: str):
                wa = _words(a)
                wb = _words(b)
                sm = _difflib.SequenceMatcher(a=wa, b=wb)
                repl = ins = dele = 0
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag == "replace":
                        repl += max(i2 - i1, j2 - j1)
                    elif tag == "insert":
                        ins += (j2 - j1)
                    elif tag == "delete":
                        dele += (i2 - i1)
                total_a = max(1, len(wa))
                total_b = max(1, len(wb))
                return repl, ins, dele, total_a, total_b

            def _edit_noise_per_100w(a: str, b: str) -> float:
                repl, ins, dele, _, total_b = _word_level_ops(a, b)
                edits = repl + ins + dele
                return (edits / max(1, total_b)) * 100.0

            # Compute
            punct = _punct_density_per_100w(cur_text)
            punct_prev = _punct_density_per_100w(prev_text)
            sents = len(_sentences(cur_text))
            sents_prev = len(_sentences(prev_text))
            trans = _count_transitions(cur_text)
            trans_prev = _count_transitions(prev_text)
            rcv = _rhythm_cv(cur_text)
            rcv_prev = _rhythm_cv(prev_text)
            gi = _grammar_issues(cur_text)
            gi_prev = _grammar_issues(prev_text)
            edits_per_100 = _edit_noise_per_100w(prev_text, cur_text)

            # Keywords (from heuristics)
            heur_kw = (heur or {}).get('keywords', []) or []
            def _keyword_hits(t: str, kws):
                if not kws: return 0, {}
                tl = (t or "").lower()
                totals = {}
                for k in kws:
                    k2 = (k or "").strip().lower()
                    if not k2: continue
                    totals[k2] = len(_re.findall(rf"\b{_re.escape(k2)}\b", tl))
                return sum(totals.values()), totals
            k_total, k_break = _keyword_hits(cur_text, heur_kw)
            k_total_prev, _ = _keyword_hits(prev_text, heur_kw)

            # Word-level ops for explicit change in words
            repl, ins, dele, total_a, total_b = _word_level_ops(prev_text, cur_text)

            # Log a single structured line with key metrics
            _log(
                "PASS_TOGGLES",
                (
                    f"file={os.path.basename(input_path)} pass={pass_index} "
                    f"punct_per_100w={punct:.2f} punct_per_100w_prev={punct_prev:.2f} "
                    f"sentences={sents} sentences_prev={sents_prev} "
                    f"transitions={trans} transitions_prev={trans_prev} "
                    f"rhythm_cv={rcv:.3f} rhythm_cv_prev={rcv_prev:.3f} "
                    f"grammar_issues={gi} grammar_issues_prev={gi_prev} "
                    f"keywords_total={k_total} keywords_total_prev={k_total_prev} "
                    f"word_replace={repl} word_insert={ins} word_delete={dele} "
                    f"words_prev={total_a} words_curr={total_b} edits_per_100w={edits_per_100:.2f}"
                )
            )

            # Change gates and auto-rerun policy
            try:
                min_edits = float((heur or {}).get('gates', {}).get('min_edits_per_100w', 25))
                max_similarity = float((heur or {}).get('gates', {}).get('max_similarity', 0.85))
            except Exception:
                min_edits, max_similarity = 25.0, 0.85
            similarity = 1.0 - float(_difflib.SequenceMatcher(None, prev_text, cur_text).ratio())
            if edits_per_100 < min_edits or (1.0 - similarity) > max_similarity:
                from .logger import log_event as _log2
                _log2("GATE_RETRY", f"pass={pass_index} edits_per_100={edits_per_100:.2f} similarity={(1.0 - similarity):.2f} -> insufficient change detected")
                # Suggest heuristics for next pass instead of mutating shared settings
                try:
                    current_refiner_control = int(((heur or {}).get('schema_flags', {}).get('refiner_control') or 2))
                    current_jitter = float((heur or {}).get('anti_scanner', {}).get('jitter', 0.3))
                    
                    result.extra['retry_suggested'] = True
                    result.extra['suggested_heuristics'] = {
                        'schema_flags': {'refiner_control': min(3, current_refiner_control + 1)},
                        'anti_scanner': {'jitter': min(0.9, current_jitter + 0.1)},
                        'gates': {
                            'min_edits_per_100w': max(15, min_edits - 5),  # Lower threshold for retry
                            'max_similarity': min(0.95, max_similarity + 0.05)  # Higher similarity tolerance
                        }
                    }
                except Exception as e:
                    from .logger import log_event as _log3
                    _log3("GATE_RETRY_ERROR", f"Failed to generate retry suggestions: {str(e)}")
            
        except Exception:
            pass

        # Store file version for diff generation
        try:
            # Generate a unique file_id from input_path
            import hashlib as _hashlib
            file_id = _hashlib.md5(input_path.encode()).hexdigest()[:12]
            
            # Calculate metrics
            try:
                import difflib as _difflib
                scanner_risk = 0.0  # Will be calculated if available
                metrics = {
                    'changePercent': change_pct if 'change_pct' in locals() else 0.0,
                    'tensionPercent': tension_pct if 'tension_pct' in locals() else 0.0,
                    'scannerRisk': scanner_risk,
                    'success': result.success,
                    'localPath': result.local_path,
                    'docId': result.doc_id if hasattr(result, 'doc_id') else None,
                    'originalLength': len(raw),
                    'finalLength': len(final_norm),
                    'processingTime': sum(stage.duration_ms for stage in ps.stages.values())
                }
            except Exception:
                metrics = {}
            
            file_version_manager.store_version(
                file_id=file_id,
                pass_number=pass_index,
                content=final_norm,
                file_path=result.local_path,
                metrics=metrics,
                metadata={
                    "input_path": input_path,
                    "entropy_level": entropy_level,
                    "heuristics": heuristics_overrides,
                    "processing_time": sum(stage.duration_ms for stage in ps.stages.values())
                }
            )
        except Exception as e:
            log_exception("VERSION_STORAGE_ERROR", e)

        return ps, result, final_norm

    # --- Critics and gates ---
    def _critic_span_rewrite(self, text: str) -> str:
        import re as _re
        t = text or ""
        banned = [
            r"\bIn conclusion,\b", r"\bOverall,\b", r"\bTo summarize,\b", r"\bThis means that\b",
            r"\bIt is important to\b", r"\bIn other words,\b",
        ]
        for pat in banned:
            t = _re.sub(pat, "", t)
        # Enforce opener diversity by softening repeated sentence starts
        sents = [s for s in _re.split(r"(?<=[.!?])\s+", t.strip()) if s]
        fixed = []
        recent = []
        for s in sents:
            first = (s.split()[:1] or [""])[0].lower()
            if first in recent:
                s = _re.sub(r"^[A-Za-z]+", "Sometimes", s)
            recent.append(first)
            if len(recent) > 3:
                recent.pop(0)
            fixed.append(s)
        return " ".join(fixed)

    def _avg_stage_latency(self, ps: PassState) -> float:
        ms = [s.duration_ms for s in ps.stages.values() if s.status in ("ok","warn","fail")]
        return (sum(ms) / len(ms)) if ms else 0.0

    def _blend_refined_with_original(self, refined: str, original: str) -> str:
        """Blend refined content with original structure to meet word ratio requirements."""
        try:
            import re as _re
            # Split into paragraphs
            orig_paras = [p.strip() for p in original.split('\n\n') if p.strip()]
            refined_paras = [p.strip() for p in refined.split('\n\n') if p.strip()]
            
            if not orig_paras or not refined_paras:
                return refined + "\n"
            
            # Try to match refined paragraphs with original structure
            result_paras = []
            for i, orig_para in enumerate(orig_paras):
                if i < len(refined_paras):
                    # Use refined content but preserve original length if possible
                    refined_para = refined_paras[i]
                    orig_words = len(orig_para.split())
                    refined_words = len(refined_para.split())
                    
                    # If refined is significantly shorter, blend with original
                    if refined_words < orig_words * 0.7:
                        # Use robust sentence splitting
                        def robust_split(text):
                            abbrev_patterns = [
                                r'\b(?:Dr|Mr|Mrs|Ms|Prof|Sr|Jr|Inc|Ltd|Corp|Co|St|Mt|Fig|Vol|No|vs|p\.m|a\.m)\.',
                                r'\bU\.S\.(?:A\.)?', r'\bU\.K\.', r'\b(?:e\.g|i\.e|etc)\.', r'\b\d+\.'
                            ]
                            placeholder_map = {}
                            for i, pattern in enumerate(abbrev_patterns):
                                matches = _re.finditer(pattern, text, _re.IGNORECASE)
                                for match in matches:
                                    placeholder = f"__ABBREV_{i}_{len(placeholder_map)}__"
                                    placeholder_map[placeholder] = match.group(0)
                                    text = text.replace(match.group(0), placeholder, 1)
                            sentences = [s for s in _re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
                            for placeholder, original in placeholder_map.items():
                                for i, sentence in enumerate(sentences):
                                    sentences[i] = sentence.replace(placeholder, original)
                            return sentences
                        
                        refined_sentences = robust_split(refined_para)
                        orig_sentences = robust_split(orig_para)
                        
                        if len(refined_sentences) > 0 and len(orig_sentences) > 0:
                            # Better blending: take more of refined, pad with original if needed
                            take_refined = max(1, len(refined_sentences) // 2)
                            take_original = max(0, len(orig_sentences) - take_refined)
                            blended = ' '.join(refined_sentences[:take_refined] + orig_sentences[-take_original:])
                            result_paras.append(blended)
                        else:
                            result_paras.append(refined_para)
                    else:
                        result_paras.append(refined_para)
                else:
                    # Use original if no refined equivalent
                    result_paras.append(orig_para)
            
            return '\n\n'.join(result_paras) + '\n'
        except Exception:
            # If blending fails, return refined text as-is
            return refined + "\n"

    def _parse_strategy_weights(self, strategy_text: str) -> tuple[dict, str, str]:
        """Parse strategy output. Returns (weights, rationale, approach).
        We accept a line that starts with 'STRATEGY_WEIGHTS:' followed by
        comma-separated key=value pairs for clarity, persuasion, brevity, formality.
        """
        weights = {"clarity": 0.5, "persuasion": 0.3, "brevity": 0.3, "formality": 0.5}
        rationale = ""
        approach = ""
        try:
            import re as _re
            import json as _json
            text = strategy_text or ""
            lines = text.splitlines()

            # 1) Try explicit STRATEGY_WEIGHTS: k=v, k=v
            for ln in lines:
                if ln.strip().upper().startswith("STRATEGY_WEIGHTS"):
                    m = _re.search(r"(?i)strategy_weights\s*:\s*(.*)", ln)
                    if m:
                        body = m.group(1)
                        for kv in body.split(','):
                            if '=' in kv:
                                k, v = kv.split('=', 1)
                                k = k.strip().lower()
                                try:
                                    weights[k] = float(v.strip())
                                except Exception:
                                    pass

            # 2) If not populated, try to find JSON object with these keys
            if all(k in weights and isinstance(weights[k], float) for k in ("clarity","persuasion","brevity","formality")) is False:
                try:
                    obj = _json.loads(text)
                    cand = obj.get('strategy_weights') if isinstance(obj, dict) else None
                    if isinstance(cand, dict):
                        for k in ("clarity","persuasion","brevity","formality"):
                            if k in cand:
                                weights[k] = float(cand[k])
                except Exception:
                    pass

            # 3) Fallback: regex search for numbers near labels
            if all(isinstance(weights.get(k), float) for k in ("clarity","persuasion","brevity","formality")) is False:
                def find_val(label: str) -> float | None:
                    m = _re.search(label + r"\s*[:=]\s*([01](?:\.\d+)?)", text, flags=_re.IGNORECASE)
                    if m:
                        try:
                            return float(m.group(1))
                        except Exception:
                            return None
                    return None
                for k in ("clarity","persuasion","brevity","formality"):
                    v = find_val(k)
                    if v is not None:
                        weights[k] = v

            # RATIONALE and REFINEMENT_APPROACH (collect following lines until next header)
            def collect_after(prefix: str) -> str:
                out: list[str] = []
                capture = False
                for ln in lines:
                    up = ln.strip().upper()
                    if up.startswith(prefix):
                        capture = True
                        after = ln.split(':', 1)[-1].strip()
                        if after:
                            out.append(after)
                        continue
                    if capture and (up.startswith("STRATEGY_WEIGHTS") or up.startswith("RATIONALE") or up.startswith("REFINEMENT_APPROACH")):
                        break
                    if capture:
                        if ln.strip():
                            out.append(ln.strip())
                return " ".join(out).strip()
            if not rationale:
                rationale = collect_after("RATIONALE")
            if not approach:
                approach = collect_after("REFINEMENT_APPROACH")
        except Exception:
            pass
        # clamp 0..1 and normalize softly
        for k in list(weights.keys()):
            try:
                x = float(weights[k])
                weights[k] = max(0.0, min(1.0, x))
            except Exception:
                weights[k] = 0.5
        try:
            s = sum(float(weights[k]) for k in ("clarity","persuasion","brevity","formality"))
            if s > 0:
                # normalize lightly to keep proportions but sum to ~1 for downstream heuristics
                for k in ("clarity","persuasion","brevity","formality"):
                    weights[k] = float(weights[k]) / s
            else:
                # If all weights are 0, set reasonable defaults
                weights = {"clarity": 0.6, "persuasion": 0.3, "brevity": 0.3, "formality": 0.6}
        except Exception:
            # Fallback to defaults if any calculation fails
            weights = {"clarity": 0.6, "persuasion": 0.3, "brevity": 0.3, "formality": 0.6}
        return weights, rationale, approach

    def _analyze_strategy(self, text: str, heur: dict = None) -> tuple[dict, str, str, StrategyPlan]:
        """Call the model to produce strategy weights, rationale/approach, and slotting plan."""
        print(f"_analyze_strategy: Starting analysis (text length: {len(text)} chars)")
        # Lightweight document context (can be expanded later)
        word_count = len((text or "").split())
        # Detect simple keywords provided by heuristics if present
        if heur is None:
            heur = self.settings.heuristics or {}
        keywords = heur.get('keywords', []) or []
        detected = []
        try:
            low = (text or "").lower()
            for kw in keywords:
                k = (kw or "").strip()
                if not k:
                    continue
                if k.lower() in low:
                    detected.append(k)
        except Exception:
            pass
        doc_type = heur.get('doc_type') or "unknown"
        audience = heur.get('audience') or "general"
        goal = heur.get('goal') or heur.get('stated_goal') or "unspecified"

        # Heuristic override: allow static strategy weights from heuristics
        override = None
        try:
            override = heur.get('strategy_weights')
        except Exception:
            override = None

        # History analysis (MVP): adjust weights based on prior tendencies if enabled
        try:
            hist_cfg = (heur.get('history_analysis') or {}) if isinstance(heur, dict) else {}
            if hist_cfg is True or (isinstance(hist_cfg, dict) and hist_cfg.get('enabled', True)):
                history_path = hist_cfg.get('history_path', 'recent_history.json')
                
                # Validate history file exists and is readable
                import os
                if not os.path.exists(history_path):
                    from .logger import log_event as _log
                    _log("HISTORY_ANALYSIS", f"History file not found: {history_path}. Using defaults.")
                    prof = {"brevity_bias": 0.5, "formality_bias": 0.5, "structure_bias": 0.5}
                else:
                    try:
                        prof = derive_history_profile(history_path)
                        # Validate profile structure
                        required_keys = ['brevity_bias', 'formality_bias', 'structure_bias']
                        for key in required_keys:
                            if key not in prof or not isinstance(prof[key], (int, float)):
                                prof[key] = 0.5  # Default value
                                
                        # Clamp values to valid range
                        for key in required_keys:
                            prof[key] = max(0.0, min(1.0, float(prof[key])))
                            
                    except Exception as hist_error:
                        from .logger import log_event as _log
                        _log("HISTORY_ANALYSIS_ERROR", f"Failed to derive history profile: {str(hist_error)}")
                        prof = {"brevity_bias": 0.5, "formality_bias": 0.5, "structure_bias": 0.5}
                
                # map profile -> weights nudge
                # brevity -> increase brevity weight slightly; structure -> increase clarity/formality
                brevity_nudge = float(prof.get('brevity_bias', 0.5)) - 0.5
                structure_nudge = float(prof.get('structure_bias', 0.5)) - 0.5
                tone_nudge = float(prof.get('formality_bias', 0.5)) - 0.5
                
                # Clamp nudges to reasonable range (-0.2 to +0.2)
                brevity_nudge = max(-0.2, min(0.2, brevity_nudge))
                structure_nudge = max(-0.2, min(0.2, structure_nudge))
                tone_nudge = max(-0.2, min(0.2, tone_nudge))
                
                # Store as heuristic override for downstream phases (only once per pass)
                if not heur.get('_history_nudges_applied'):
                    heur.setdefault('history_profile', prof)
                    heur.setdefault('strategy_nudges', {})
                    heur['strategy_nudges'].update({
                        'brevity': brevity_nudge,
                        'clarity': structure_nudge * 0.4,
                        'formality': tone_nudge,
                    })
                    heur['_history_nudges_applied'] = True
                    
                    from .logger import log_event as _log
                    _log("HISTORY_ANALYSIS", f"Applied history nudges: brevity={brevity_nudge:.3f}, clarity={structure_nudge*0.4:.3f}, formality={tone_nudge:.3f}")
                
        except Exception as e:
            from .logger import log_event as _log
            _log("HISTORY_ANALYSIS_ERROR", f"History analysis failed: {str(e)}")

        system = (
            "You are Strategy Insight, a planning module that outputs a strategy vector for text refinement.\n"
            "Respond concisely in the specified format."
        )
        user = (
            f"STRATEGY ANALYSIS PHASE\n\n"
            f"Document Context:\n"
            f"- Type: {doc_type}\n"
            f"- Length: {word_count} words\n"
            f"- Audience: {audience}\n"
            f"- User Goal: {goal}\n"
            f"- Domain Keywords: {', '.join(detected) if detected else 'none'}\n\n"
            f"Generate a strategy vector:\n\n"
            f"1. CLARITY_WEIGHT (0.0-1.0)\n"
            f"2. PERSUASION_WEIGHT (0.0-1.0)\n"
            f"3. BREVITY_WEIGHT (0.0-1.0)\n"
            f"4. FORMALITY_WEIGHT (0.0-1.0)\n\n"
            f"Output format:\n"
            f"STRATEGY_WEIGHTS: clarity=0.7, persuasion=0.2, brevity=0.1, formality=0.6\n"
            f"RATIONALE: ...\n"
            f"REFINEMENT_APPROACH: ...\n"
        )
        if isinstance(override, dict) and override:
            # Build a synthetic strategy text to go through the same parser
            parts = [
                f"STRATEGY_WEIGHTS: " + ", ".join(f"{k}={override.get(k, 0.5)}" for k in ("clarity","persuasion","brevity","formality")),
                "RATIONALE: heuristics override",
                "REFINEMENT_APPROACH: heuristics override",
            ]
            strategy_text = "\n".join(parts)
            w, r, a = self._parse_strategy_weights(strategy_text)
            plan = self._build_strategy_plan(w)
            return w, r, a, plan

        # Strategy mode: rules vs model (MVP prefers rules if specified)
        mode = str((heur.get('strategy_mode') or os.getenv('STRATEGY_MODE', 'model')).lower()) if isinstance(heur, dict) else str(os.getenv('STRATEGY_MODE', 'model')).lower()
        
        # Validate mode - default to 'model' if invalid
        if mode not in ['rules', 'model']:
            mode = 'model'

        def _rule_based() -> tuple[dict, str, str]:
            """MVP: Rule-based routing using doc_type, audience, goal, length, keywords."""
            w = {"clarity": 0.6, "persuasion": 0.25, "brevity": 0.3, "formality": 0.55}
            rationale_bits = []
            # Length: long docs → more brevity
            if word_count > 1200:
                w["brevity"] += 0.15; rationale_bits.append("long_doc")
            elif word_count < 250:
                w["clarity"] += 0.05; rationale_bits.append("short_doc")
            # Doc type
            dt = (doc_type or "").lower()
            if dt in ("email","memo"):
                w["brevity"] += 0.1; w["formality"] -= 0.05; rationale_bits.append("email_memo")
            if dt in ("report","academic"):
                w["clarity"] += 0.1; w["formality"] += 0.1; rationale_bits.append("report_academic")
            # Audience
            au = (audience or "").lower()
            if au in ("executive","leadership"):
                w["brevity"] += 0.1; w["formality"] += 0.1; rationale_bits.append("executive")
            elif au in ("general","students"):
                w["clarity"] += 0.05; rationale_bits.append("general")
            # Goal
            gl = (goal or "").lower()
            if any(k in gl for k in ("convince","sell","persuad","motivate")):
                w["persuasion"] += 0.2; rationale_bits.append("persuasion_goal")
            if any(k in gl for k in ("summarize","reduce","condense")):
                w["brevity"] += 0.15; rationale_bits.append("summary_goal")
            if any(k in gl for k in ("explain","clarify","document")):
                w["clarity"] += 0.15; rationale_bits.append("clarity_goal")
            # Domain terms detected → clarity up (preserve technical accuracy)
            if detected:
                w["clarity"] += 0.05; rationale_bits.append("domain_terms")
            # Normalize and clamp 0..1 and sum ~1
            for k in list(w.keys()):
                try:
                    w[k] = max(0.0, min(1.0, float(w[k])))
                except Exception:
                    w[k] = 0.5
            s = sum(w.values()) or 1.0
            for k in w:
                w[k] = float(w[k]) / s
            rationale = ",".join(rationale_bits)
            approach = "rules_mvp"
            return w, rationale, approach

        if mode == 'rules':
            w, r, a = _rule_based()
            plan = self._build_strategy_plan(w)
            return w, r, a, plan

        try:
            user2 = user + "\nAlso provide a JSON object named STRATEGY_SLOTS with keys: primary_strategy, secondary_strategy, modulators (array)."
            print(f"_analyze_strategy: Calling LLM for strategy analysis")
            strategy_text = self.model.generate(system=system, user=user2, temperature=0.15, max_tokens=500)
            print(f"_analyze_strategy: LLM returned {len(strategy_text)} chars")
        except Exception as e:
            # Fall back to rules if model call fails
            print(f"_analyze_strategy: LLM call failed ({e}), using rule-based fallback")
            w, r, a = _rule_based()
            plan = self._build_strategy_plan(w)
            return w, r, a, plan
        w, r, a = self._parse_strategy_weights(strategy_text)
        # Apply nudges from history profile if present
        try:
            nudges = (self.settings.heuristics or {}).get('strategy_nudges', {})
            for k in ("clarity","persuasion","brevity","formality"):
                if k in nudges:
                    w[k] = max(0.0, min(1.0, float(w.get(k, 0.5)) + float(nudges.get(k, 0.0))))
        except Exception:
            pass
        plan = self._extract_strategy_slots(strategy_text, w)
        return w, r, a, plan

    def _build_strategy_plan(self, weights: dict) -> StrategyPlan:
        order = sorted([
            ("clarity", weights.get("clarity", 0.0)),
            ("persuasion", weights.get("persuasion", 0.0)),
            ("brevity", weights.get("brevity", 0.0)),
            ("formality", weights.get("formality", 0.0)),
        ], key=lambda t: t[1], reverse=True)
        primary = order[0][0]
        secondary = order[1][0] if len(order) > 1 else None
        modulators = [k for (k, _) in order[2:]] if len(order) > 2 else []
        return StrategyPlan(primary_strategy=primary, secondary_strategy=secondary, modulators=modulators)

    def _extract_strategy_slots(self, text: str, weights: dict) -> StrategyPlan:
        try:
            import json as _json
            import re as _re
            
            # Try to extract JSON from markdown-wrapped content
            json_match = _re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, _re.DOTALL)
            if json_match:
                json_text = json_match.group(1)
            else:
                # Look for JSON object in the text
                json_match = _re.search(r'\{[^{}]*"STRATEGY_SLOTS"[^{}]*\}', text, _re.DOTALL)
                if json_match:
                    json_text = json_match.group(0)
                else:
                    json_text = text
            
            obj = _json.loads(json_text)
            if isinstance(obj, dict) and isinstance(obj.get("STRATEGY_SLOTS"), dict):
                s = obj["STRATEGY_SLOTS"]
                primary = str(s.get("primary_strategy") or "").strip().lower() or None
                secondary = str(s.get("secondary_strategy") or "").strip().lower() or None
                mods = [str(x).strip().lower() for x in (s.get("modulators") or []) if str(x).strip()]
                allowed = {"clarity","persuasion","brevity","formality"}
                seq = [x for x in [primary, secondary] if x]
                seq += [m for m in mods if m]
                seq = [x for x in seq if x in allowed]
                if seq:
                    primary = seq[0]
                    secondary = seq[1] if len(seq) > 1 else None
                    modulators = [x for x in seq[2:]]
                    return StrategyPlan(primary_strategy=primary, secondary_strategy=secondary, modulators=modulators)
        except Exception as e:
            from .logger import log_event as _log
            _log("STRATEGY_SLOTS_PARSE_ERROR", f"Failed to parse strategy slots: {str(e)}")
        return self._build_strategy_plan(weights)

    def _three_phase_refine(self, text: str, heur: dict = None) -> tuple[str, dict]:
        # 1) Analyze strategy
        print(f"_three_phase_refine: Starting strategy analysis")
        weights, rationale, approach, plan = self._analyze_strategy(text, heur)
        print(f"_three_phase_refine: Strategy analysis completed, weights={weights}")
        try:
            from .logger import log_event as _log
            _log("STRATEGY", f"weights={weights} rationale={rationale[:160]} approach={approach[:160]} plan={{'primary': '{plan.primary_strategy}', 'secondary': '{plan.secondary_strategy}', 'modulators': {plan.modulators}}}")
        except Exception:
            pass
        
        # Store strategy in macro_results for API consumption
        self._last_strategy = {
            'weights': weights,
            'rationale': rationale,
            'approach': approach,
            'plan': {
                'primary': plan.primary_strategy,
                'secondary': plan.secondary_strategy,
                'modulators': plan.modulators
            }
        }
        clarity = float(weights.get('clarity', 0.6))
        persuasion = float(weights.get('persuasion', 0.3))
        brevity = float(weights.get('brevity', 0.3))
        formality = float(weights.get('formality', 0.6))
        print(f"_three_phase_refine: Extracted weights - clarity={clarity}, persuasion={persuasion}, brevity={brevity}, formality={formality}")

        # 2) Determine phases and temperatures influenced by global aggressiveness, weights, and entropy management
        print(f"_three_phase_refine: Determining phases and temperatures")
        aggr = (self.settings.aggressiveness or "Auto").lower()
        if aggr == "low":
            phases = [("Targeted Cleaner", 0.25)]
        elif aggr == "medium":
            phases = [("Pattern Scrubber", 0.3), ("Polisher", 0.35)]
        else:
            # Temperature shaping: persuasion ↑ temp, formality & brevity ↓ temp, clarity slight ↓ for stability
            base = 0.4
            base += 0.12 * (persuasion - 0.5)
            base -= 0.08 * max(0.0, formality - 0.6)
            base -= 0.05 * max(0.0, brevity - 0.5)
            base -= 0.03 * max(0.0, clarity - 0.6)
            # Entropy management signals (risk preference, section role)
            ent = (heur or {}).get('entropy', {}) or {}
            risk = float(ent.get('risk_preference', 0.5))  # 0 conservative .. 1 bold
            # Section-aware schedule (cleanup < refinement < polish)
            def clamp(x: float, lo: float=0.2, hi: float=0.9) -> float:
                return max(lo, min(hi, x))
            
            # Temperature scheduling rationale:
            # - Cleanup phase: Lower temp for precision, conservative approach
            # - Refinement phase: Higher temp for creativity, risk-dependent
            # - Polish phase: Lower temp for consistency, bring precision back
            cleanup_t = clamp(base - 0.05 - 0.15 * (1.0 - risk))  # Lower temp for low risk (conservative)
            refine_t  = clamp(base + 0.02 + 0.10 * risk)          # Higher temp for high risk (creative)
            polish_t  = clamp(base - 0.02 - 0.08 * (1.0 - risk))  # Lower temp for low risk (precise)
            phases = [
                ("Strategy-Guided Cleanup", cleanup_t),
                ("Strategy-Guided Refinement", refine_t),
                ("Strategy-Guided Polish", polish_t),
            ]

        # 3) Build dynamic guidance from weights + microstructure + tone + anti-scanner
        guidance_lines = [
            "REFINEMENT PHASE (Strategy-Guided):",
            f"Strategy Context: clarity={clarity:.2f}, persuasion={persuasion:.2f}, brevity={brevity:.2f}, formality={formality:.2f}",
            "",
        ]
        guidance_lines.append(f"Priority: primary={plan.primary_strategy}, secondary={plan.secondary_strategy}, modulators={', '.join(plan.modulators) if plan.modulators else 'none'}")
        # Thresholds (can be overridden via heuristics)
        t_clarity = float((heur or {}).get('thresholds', {}).get('clarity_high', 0.6)) if isinstance((heur or {}).get('thresholds', {}), dict) else 0.6
        t_persuasion = float((heur or {}).get('thresholds', {}).get('persuasion_high', 0.5)) if isinstance((heur or {}).get('thresholds', {}), dict) else 0.5
        t_brevity = float((heur or {}).get('thresholds', {}).get('brevity_high', 0.4)) if isinstance((heur or {}).get('thresholds', {}), dict) else 0.4
        t_formality = float((heur or {}).get('thresholds', {}).get('formality_high', 0.6)) if isinstance((heur or {}).get('thresholds', {}), dict) else 0.6

        if clarity > t_clarity:
            guidance_lines.append("HIGH CLARITY: Focus on sentence clarity, jargon reduction, logical flow.")
        if persuasion > t_persuasion:
            guidance_lines.append("HIGH PERSUASION: Emphasize action verbs, confident assertions, compelling evidence.")
        if brevity > t_brevity:
            guidance_lines.append("HIGH BREVITY: Prioritize conciseness, eliminate redundancy, tighten phrasing.")
        if formality > t_formality:
            guidance_lines.append("HIGH FORMALITY: Use professional language, avoid contractions, structured presentation.")
        # ---- Microstructure control (MVP) ----
        mtargets = (heur or {}).get('microstructure_targets', {}) or {}
        sl_min = float(mtargets.get('avg_sentence_len_min', 12))
        sl_max = float(mtargets.get('avg_sentence_len_max', 22))
        passive_max = float(mtargets.get('passive_rate_max', 0.20))
        hedge_max = float(mtargets.get('hedge_density_max', 0.15))
        banned = list(mtargets.get('banned_cliches', [])) or [
            'at the end of the day', 'needless to say', 'it goes without saying', 'the fact of the matter',
            "in today's world", 'paradigm shift', 'think outside the box'
        ]

        metrics = self._micro_metrics(text)
        try:
            from .logger import log_event as _log
            _log("PASS_MICRO_BASE", f"asl={metrics['avg_sentence_len']:.2f} passive_rate={metrics['passive_rate']:.3f} hedge_density={metrics['hedge_density']:.3f} fk_grade={metrics['fk_grade']:.2f} cliche_hits={metrics['cliche_hits']}")
        except Exception:
            pass

        need_len = (metrics['avg_sentence_len'] < sl_min) or (metrics['avg_sentence_len'] > sl_max)
        need_pass = metrics['passive_rate'] > passive_max
        need_hedge = metrics['hedge_density'] > hedge_max
        need_cliche = metrics['cliche_hits'] > 0

        if any([need_len, need_pass, need_hedge, need_cliche]):
            guidance_lines.append("")
            guidance_lines.append("Microstructure Control:")
            if need_len:
                guidance_lines.append(f"- Adjust average sentence length toward {int((sl_min+sl_max)/2)} by splitting/merging as needed.")
            if need_pass:
                guidance_lines.append("- Reduce passive voice; prefer active constructions where natural.")
            if need_hedge:
                guidance_lines.append("- Reduce hedges/qualifiers unless needed for precision.")
            if need_cliche:
                guidance_lines.append("- Replace banned clichés with fresh, concise alternatives.")

        # ---- Macrostructure analysis (MVP) ----
        macro = self._macro_analyze(text)
        try:
            from .logger import log_event as _log
            _log(
                "PASS_MACRO",
                (
                    f"has_intro={macro['coverage'].get('intro')} has_points={macro['coverage'].get('points')} "
                    f"has_conclusion={macro['coverage'].get('conclusion')} redundant_pairs={len(macro['redundant_pairs'])} "
                    f"needs_headings={len(macro['needs_headings'])} needs_separators={len(macro['needs_separators'])}"
                )
            )
        except Exception:
            pass
        if macro['needs_headings'] or macro['needs_separators'] or macro['redundant_pairs']:
            guidance_lines.append("")
            guidance_lines.append("Macrostructure Analysis:")
            cov = macro['coverage']
            missing = [n for n, ok in (('Intro', cov.get('intro')), ('Conclusion', cov.get('conclusion'))) if not ok]
            if missing:
                guidance_lines.append(f"- Coverage: add {' and '.join(missing)} section(s).")
            if macro['needs_headings']:
                head_idxs = ', '.join(str(i+1) for i in macro['needs_headings'][:5])
                guidance_lines.append(f"- Add headings before paragraphs: {head_idxs}{' ...' if len(macro['needs_headings'])>5 else ''}.")
            if macro['needs_separators']:
                sep_idxs = ', '.join(str(i+1) for i in macro['needs_separators'][:5])
                guidance_lines.append(f"- Insert separators between topic shifts near paragraphs: {sep_idxs}{' ...' if len(macro['needs_separators'])>5 else ''}.")
            if macro['redundant_pairs']:
                red = ', '.join(f"{a+1}-{b+1}" for a,b in macro['redundant_pairs'][:4])
                guidance_lines.append(f"- Merge or deduplicate overlapping paragraphs: {red}{' ...' if len(macro['redundant_pairs'])>4 else ''}.")
            
            # Store macro analysis results for post-processing enforcement
            heur['macro_analysis_results'] = macro

        # ---- Semantic tone tuning (MVP) ----
        tone_cfg = (heur or {}).get('tone', {}) or {}
        tone_target = str(tone_cfg.get('target') or '').strip().lower()  # formal|neutral|friendly|executive|academic
        tone_intensity = str(tone_cfg.get('intensity') or 'light').strip().lower()  # light|medium|strong
        safe_keywords = (heur or {}).get('keywords', []) or []
        if tone_target in ("formal","neutral","friendly","executive","academic"):
            guidance_lines.append("")
            guidance_lines.append("Semantic Tone Tuning:")
            guidance_lines.append(f"- Target tone: {tone_target} ({tone_intensity}).")
            guidance_lines.append("- Maintain content; adjust register markers (greetings, modals, intensifiers).")
            if safe_keywords:
                guidance_lines.append(f"- Do not alter domain terms: {', '.join(list(dict.fromkeys([str(k) for k in safe_keywords]))[:6])}{' ...' if len(safe_keywords)>6 else ''}.")
        # ---- Anti-scanner techniques (MVP) ----
        anti_cfg = (heur or {}).get('anti_scanner', {}) or {}
        jitter = float(anti_cfg.get('jitter', 0.3))
        rare_cap = float(anti_cfg.get('rare_per_100_max', 5))
        if jitter > 0.0:
            guidance_lines.append("")
            guidance_lines.append("Anti-Scanner Techniques:")
            guidance_lines.append(f"- Apply light variation (jitter={jitter:.2f}); keep clarity and truthfulness.")
            guidance_lines.append(f"- Cap rare/novel word substitutions to ≤{int(rare_cap)} per 100 tokens.")
            guidance_lines.append("- Avoid overused scaffolds (e.g., 'In conclusion,', generic transition macros).")
        guidance = "\n".join(guidance_lines)

        # 4) Optional schema directives (existing toggles)
        try:
            from .pipeline import schema_directives
            directives = schema_directives((heur or {}).get('schema_flags', {}))
        except Exception:
            directives = ""

        # 5) Run phases with combined system prompt
        out = text
        print(f"_three_phase_refine: Starting phase loop with {len(phases)} phases")
        for idx, (system_msg, temp) in enumerate(phases, 1):
            print(f"_three_phase_refine: Starting phase {idx}/{len(phases)}: {system_msg} (temp={temp})")
            # repetition/phrase penalties via guidance text (API-level penalties can be added later)
            ent_guidance = []
            try:
                ent_cfg = (heur or {}).get('entropy', {}) or {}
                if ent_cfg:
                    ent_guidance.append("Entropy Controls: Avoid repetitive n-grams and generic transitions; prefer varied phrase openers.")
                    if float(ent_cfg.get('repeat_penalty', 0.0)) > 0.0:
                        ent_guidance.append("Reduce repeated bigrams and trigrams; diversify connective phrases.")
                    if float(ent_cfg.get('phrase_penalty', 0.0)) > 0.0:
                        ent_guidance.append("Replace overused phrases with natural, domain-appropriate alternatives.")
            except Exception:
                pass
            sys_parts = [p for p in [directives, guidance, "\n".join(ent_guidance), system_msg] if p]
            sys_full = "\n".join(sys_parts)
            print(f"_three_phase_refine: Calling LLM for phase {idx} (input length: {len(out)} chars)")
            model_name = self._get_model_name()
            # Determine chunking
            if self.enable_domain_chunk and self.max_input_tokens > 0:
                sections = self._split_domain_sections(out)
                chunks = self._pack_to_budget(sections, sys_full, model_name, self.max_input_tokens)
            else:
                chunks = [out]

            generated_parts: List[str] = []
            for ch in chunks:
                payload = ch
                ph_map = {}
                if self.enable_placeholders:
                    payload, ph_map = self._apply_placeholders(payload)

                # Preflight token count for this chunk (system + payload)
                try:
                    pre_tokens = self._count_tokens((sys_full or "") + "\n" + (payload or ""), model_name)
                    pass_pre_tokens += int(pre_tokens)
                except Exception:
                    pass

                if hasattr(self, '_current_job_id') and self._current_job_id:
                    gen_txt, cost_info = self.model.generate(system=sys_full, user=payload, temperature=temp, max_tokens=2000, job_id=self._current_job_id)
                    if not hasattr(self, '_pass_costs'):
                        self._pass_costs = []
                    self._pass_costs.append(cost_info)
                    try:
                        pass_used_in_tokens += int(cost_info.get('tokens_in', 0) or 0)
                    except Exception:
                        pass
                else:
                    gen_txt = self.model.generate(system=sys_full, user=payload, temperature=temp, max_tokens=2000)

                if self.enable_placeholders and ph_map:
                    try:
                        gen_txt = self._restore_placeholders(gen_txt if isinstance(gen_txt, str) else gen_txt[0], ph_map)
                    except Exception:
                        pass

                # If generate returned tuple in non-job_id path, normalize
                if isinstance(gen_txt, tuple):
                    gen_txt = gen_txt[0]

                generated_parts.append(str(gen_txt))

            out = "\n\n".join([p for p in generated_parts if p])
            print(f"_three_phase_refine: LLM call completed for phase {idx} (output length: {len(out)} chars)")
        # Quick nudge passes with rollback capability: microstructure → tone → anti-scanner
        # Store initial state for potential rollback
        initial_out = out
        
        # Micro quick pass
        out = self._micro_quick_pass(out, {
            'avg_sentence_len_min': sl_min,
            'avg_sentence_len_max': sl_max,
            'banned_cliches': banned
        })
        
        # Validate after micro pass
        micro_targets = {
            'avg_sentence_len_min': sl_min,
            'avg_sentence_len_max': sl_max,
            'banned_cliches': banned,
            'passive_rate_max': passive_max,
            'hedge_density_max': hedge_max,
            'max_cliche_hits': 0
        }
        
        post_micro_validation = self._validate_microstructure_targets(out, micro_targets, heur)
        post_micro_score = post_micro_validation.get('validation_score', 0.0)
        
        # tone quick pass (lexicon swap with guardrails)
        tone_backup = out  # Backup before tone pass
        if tone_target:
            out, tone_stats = self._tone_quick_pass(out, tone_target, tone_intensity, safe_keywords)
            try:
                from .logger import log_event as _log
                _log("PASS_TONE", f"target={tone_target} intensity={tone_intensity} swaps={tone_stats.get('swaps',0)}")
                
                # Validate after tone pass and rollback if significantly worse
                post_tone_validation = self._validate_microstructure_targets(out, micro_targets, heur)
                post_tone_score = post_tone_validation.get('validation_score', 0.0)
                
                # Rollback if tone pass degraded quality significantly (both relative and absolute thresholds)
                degradation_pct = (post_micro_score - post_tone_score) / max(post_micro_score, 0.01)
                if degradation_pct > 0.2 or post_tone_score < 0.4:  # 20% degradation OR score below 0.4
                    _log("TONE_ROLLBACK", f"Rolling back tone changes due to degradation: {post_micro_score:.3f} → {post_tone_score:.3f} (degradation: {degradation_pct:.1%})")
                    out = tone_backup
                    # Re-validate after rollback
                    post_tone_validation = self._validate_microstructure_targets(out, micro_targets, heur)
                    post_tone_score = post_tone_validation.get('validation_score', 0.0)
                
            except Exception:
                pass
        
        # anti-scanner quick pass (controlled variation)
        anti_backup = out  # Backup before anti-scanner pass
        if jitter > 0.0:
            out, anti_stats = self._anti_scanner_quick_pass(out, jitter, rare_cap, safe_keywords)
            try:
                from .logger import log_event as _log
                _log("PASS_ANTI", f"jitter={jitter:.2f} rare_cap_per_100={rare_cap} synonyms={anti_stats.get('synonym_swaps',0)} idioms={anti_stats.get('idioms_added',0)} splits={anti_stats.get('splits',0)} joins={anti_stats.get('joins',0)} scaffolds_removed={anti_stats.get('scaffolds_removed',0)}")
                
                # Validate after anti-scanner pass and rollback if significantly worse
                post_anti_validation = self._validate_microstructure_targets(out, micro_targets, heur)
                post_anti_score = post_anti_validation.get('validation_score', 0.0)
                
                # Rollback if anti-scanner pass degraded quality significantly (both relative and absolute thresholds)
                degradation_pct = (post_tone_score - post_anti_score) / max(post_tone_score, 0.01)
                if degradation_pct > 0.2 or post_anti_score < 0.4:  # 20% degradation OR score below 0.4
                    _log("ANTI_ROLLBACK", f"Rolling back anti-scanner changes due to degradation: {post_tone_score:.3f} → {post_anti_score:.3f} (degradation: {degradation_pct:.1%})")
                    out = anti_backup
                    # Re-validate after rollback
                    post_anti_validation = self._validate_microstructure_targets(out, micro_targets, heur)
                    post_anti_score = post_anti_validation.get('validation_score', 0.0)
                
            except Exception:
                pass
        
        # Final validation after all quick passes with continuous scoring
        final_score = 1.0  # Default to high score if validation fails
        try:
            final_validation = self._validate_microstructure_targets(out, micro_targets, heur)
            final_score = final_validation.get('validation_score', 0.0)
            binary_score = final_validation.get('binary_score', 0.0)
            continuous_scores = final_validation.get('continuous_scores', {})
            
            from .logger import log_event as _log
            
            # Enhanced logging with continuous scoring details
            if final_score >= 0.8:
                _log("FINAL_MICRO_VALIDATION", f"Excellent results (continuous={final_score:.3f}, binary={binary_score:.2f})")
            elif final_score >= 0.6:
                _log("FINAL_MICRO_VALIDATION", f"Good results (continuous={final_score:.3f}, binary={binary_score:.2f})")
            elif final_score >= 0.4:
                _log("FINAL_MICRO_VALIDATION", f"Fair results (continuous={final_score:.3f}, binary={binary_score:.2f})")
            else:
                _log("FINAL_MICRO_VALIDATION", f"Poor results (continuous={final_score:.3f}, binary={binary_score:.2f}) - consider retry")
            
            # Log individual scores for detailed feedback
            score_details = []
            for metric, score in continuous_scores.items():
                status = "✓" if score >= 0.8 else "⚠" if score >= 0.5 else "✗"
                score_details.append(f"{status} {metric}={score:.2f}")
            
            if score_details:
                _log("FINAL_MICRO_DETAILS", f"Individual scores: {', '.join(score_details)}")
            
            # Log improvement suggestions if score is low
            if final_score < 0.6:
                suggestions = final_validation.get('improvement_suggestions', [])
                _log("FINAL_MICRO_VALIDATION", f"Improvement needed: {len(suggestions)} issues detected")
                for suggestion in suggestions[:3]:  # Log first 3 suggestions
                    _log("FINAL_MICRO_VALIDATION", f"  → {suggestion}")
                
                # Suggest retry with adjusted parameters if score is very low
                if final_score < 0.4:
                    _log("RETRY_SUGGESTED", f"Very poor results (score={final_score:.3f}) - suggesting retry with adjusted parameters")
                    
        except Exception:
            pass
        
        # Return refined text and macro analysis results
        macro_results = heur.get('macro_analysis_results', {}) if heur else {}
        
        # Add retry information if validation score is very low
        if final_score < 0.4:
            macro_results['retry_suggested'] = True
            macro_results['retry_reason'] = 'poor_microstructure_validation'
            macro_results['validation_score'] = final_score
            macro_results['suggested_adjustments'] = {
                'refiner_control': 'increase',  # Suggest higher refinement strength
                'microstructure_targets': 'relax',  # Suggest relaxing targets
                'tone_intensity': 'reduce'  # Suggest reducing tone changes
            }
        
        # Expose per-pass token counters for the caller (api layer can emit into metrics)
        try:
            self._last_pass_token_stats = {
                'preflightInTokens': int(pass_pre_tokens),
                'usedInTokens': int(pass_used_in_tokens),
            }
        except Exception:
            self._last_pass_token_stats = None
        return out, macro_results

    def _micro_metrics(self, text: str) -> dict:
        import re as _re
        def _sentences(t: str):
            # More robust sentence splitting that handles abbreviations
            text = (t or "").strip()
            if not text:
                return []
            
            # Handle common abbreviations that shouldn't end sentences
            abbrev_patterns = [
                r'\b(?:Dr|Mr|Mrs|Ms|Prof|Sr|Jr|Inc|Ltd|Corp|Co|St|Mt|Fig|Vol|No|vs|p\.m|a\.m)\.',
                r'\bU\.S\.(?:A\.)?',
                r'\bU\.K\.',
                r'\b(?:e\.g|i\.e|etc)\.',
                r'\b\d+\.',  # Numbers like "1." or "Fig. 3"
            ]
            
            # Temporarily replace abbreviations with placeholders
            placeholder_map = {}
            for i, pattern in enumerate(abbrev_patterns):
                matches = _re.finditer(pattern, text, _re.IGNORECASE)
                for match in matches:
                    placeholder = f"__ABBREV_{i}_{len(placeholder_map)}__"
                    placeholder_map[placeholder] = match.group(0)
                    text = text.replace(match.group(0), placeholder, 1)
            
            # Split on sentence endings
            sentences = [s for s in _re.split(r"(?<=[.!?])\s+", text) if s.strip()]
            
            # Restore abbreviations
            for placeholder, original in placeholder_map.items():
                for i, sentence in enumerate(sentences):
                    sentences[i] = sentence.replace(placeholder, original)
            
            return sentences
        def _words(t: str):
            return _re.findall(r"[A-Za-z0-9']+", (t or "").lower())
        sents = _sentences(text)
        words = _words(text)
        asl = (sum(len(_words(s)) for s in sents) / max(1, len(sents))) if sents else 0.0
        passive_hits = len(_re.findall(r"\b(am|is|are|was|were|be|been|being)\s+\w+ed\b", (text or ""), flags=_re.IGNORECASE))
        passive_rate = passive_hits / max(1, len(sents))
        hedges = [
            'seems','appears','likely','probably','perhaps','somewhat','kind of','sort of','rather','apparently','arguably','in my opinion'
        ]
        hedge_hits = 0
        low = (text or "").lower()
        for h in hedges:
            hedge_hits += len(_re.findall(rf"\b{_re.escape(h)}\b", low))
        hedge_density = hedge_hits / max(1, len(words))
        def _syllables(w: str) -> int:
            w = w.lower()
            w = _re.sub(r"e$", "", w)
            groups = _re.findall(r"[aeiouy]+", w)
            n = len(groups)
            return max(1, n)
        syllables = sum(_syllables(w) for w in words)
        sentences_n = max(1, len(sents))
        words_n = max(1, len(words))
        fk_grade = 0.39 * (words_n / sentences_n) + 11.8 * (syllables / words_n) - 15.59
        cliches_list = [
            'at the end of the day','needless to say','it goes without saying','the fact of the matter',
            "in today's world",'paradigm shift','think outside the box'
        ]
        c_hits = 0
        for c in cliches_list:
            c_hits += len(_re.findall(rf"\b{_re.escape(c)}\b", low))
        return {
            'avg_sentence_len': float(asl),
            'passive_rate': float(passive_rate),
            'hedge_density': float(hedge_density),
            'fk_grade': float(fk_grade),
            'cliche_hits': int(c_hits),
        }

    def _validate_microstructure_targets(self, text: str, targets: dict, heur: dict = None) -> dict:
        """Validate if microstructure targets were achieved and return validation results."""
        try:
            metrics = self._micro_metrics(text)
            validation_results = {
                'targets_met': {},
                'targets_missed': {},
                'improvement_suggestions': []
            }
            
            # Get target thresholds from heuristics
            mtargets = (heur or {}).get('microstructure_targets', {}) or {}
            sl_min = float(mtargets.get('avg_sentence_len_min', targets.get('avg_sentence_len_min', 12)))
            sl_max = float(mtargets.get('avg_sentence_len_max', targets.get('avg_sentence_len_max', 22)))
            passive_max = float(mtargets.get('passive_rate_max', targets.get('passive_rate_max', 0.20)))
            hedge_max = float(mtargets.get('hedge_density_max', targets.get('hedge_density_max', 0.15)))
            cliche_max = int(mtargets.get('max_cliche_hits', targets.get('max_cliche_hits', 0)))
            
            # Check sentence length with continuous scoring based on range width
            asl = metrics['avg_sentence_len']
            range_width = sl_max - sl_min
            if sl_min <= asl <= sl_max:
                validation_results['targets_met']['sentence_length'] = asl
                length_score = 1.0
            else:
                validation_results['targets_missed']['sentence_length'] = {
                    'current': asl, 'target_range': (sl_min, sl_max)
                }
                # Continuous scoring: distance relative to range width for symmetric scoring
                if asl < sl_min:
                    distance = sl_min - asl
                    length_score = max(0.0, 1.0 - (distance / max(range_width, 1.0)))
                    validation_results['improvement_suggestions'].append(
                        f"Sentence length too short ({asl:.1f} words, target: {sl_min}-{sl_max}). Consider combining sentences."
                    )
                else:  # asl > sl_max
                    distance = asl - sl_max
                    length_score = max(0.0, 1.0 - (distance / max(range_width, 1.0)))
                    validation_results['improvement_suggestions'].append(
                        f"Sentence length too long ({asl:.1f} words, target: {sl_min}-{sl_max}). Consider splitting complex sentences."
                    )
            
            validation_results['continuous_scores'] = validation_results.get('continuous_scores', {})
            validation_results['continuous_scores']['sentence_length'] = length_score
            
            # Check passive voice with continuous scoring
            passive_rate = metrics['passive_rate']
            if passive_rate <= passive_max:
                validation_results['targets_met']['passive_voice'] = passive_rate
                passive_score = 1.0
            else:
                validation_results['targets_missed']['passive_voice'] = {
                    'current': passive_rate, 'target_max': passive_max
                }
                # Continuous scoring: use excess ratio relative to target
                excess = passive_rate - passive_max
                # Calculate how many times over the target we are
                excess_ratio = excess / max(passive_max, 0.05)  # How many times over target
                passive_score = max(0.0, 1.0 - min(excess_ratio, 1.0))  # Cap penalty at 1.0
                validation_results['improvement_suggestions'].append(
                    f"Passive voice rate too high ({passive_rate:.3f}, target: ≤{passive_max}). Consider active voice alternatives."
                )
            
            validation_results['continuous_scores']['passive_voice'] = passive_score
            
            # Check hedge density with continuous scoring
            hedge_density = metrics['hedge_density']
            if hedge_density <= hedge_max:
                validation_results['targets_met']['hedge_density'] = hedge_density
                hedge_score = 1.0
            else:
                validation_results['targets_missed']['hedge_density'] = {
                    'current': hedge_density, 'target_max': hedge_max
                }
                # Continuous scoring: use excess ratio relative to target
                excess = hedge_density - hedge_max
                excess_ratio = excess / max(hedge_max, 0.05)  # How many times over target
                hedge_score = max(0.0, 1.0 - min(excess_ratio, 1.0))  # Cap penalty at 1.0
                validation_results['improvement_suggestions'].append(
                    f"Hedge density too high ({hedge_density:.3f}, target: ≤{hedge_max}). Reduce uncertain language."
                )
            
            validation_results['continuous_scores']['hedge_density'] = hedge_score
            
            # Check clichés with continuous scoring
            cliche_hits = metrics['cliche_hits']
            if cliche_hits <= cliche_max:
                validation_results['targets_met']['cliches'] = cliche_hits
                cliche_score = 1.0
            else:
                validation_results['targets_missed']['cliches'] = {
                    'current': cliche_hits, 'target_max': cliche_max
                }
                # Continuous scoring: explicit penalty per extra cliché
                excess = cliche_hits - cliche_max
                penalty_per_cliche = 0.2  # Lose 20% per extra cliché
                cliche_score = max(0.0, 1.0 - (excess * penalty_per_cliche))
                validation_results['improvement_suggestions'].append(
                    f"Too many clichés detected ({cliche_hits}, target: ≤{cliche_max}). Replace with fresh language."
                )
            
            validation_results['continuous_scores']['cliches'] = cliche_score
            
            # Overall validation score using continuous scoring
            continuous_scores = validation_results['continuous_scores']
            validation_results['validation_score'] = (
                continuous_scores.get('sentence_length', 0.0) +
                continuous_scores.get('passive_voice', 0.0) +
                continuous_scores.get('hedge_density', 0.0) +
                continuous_scores.get('cliches', 0.0)
            ) / 4.0
            
            # Also keep binary score for backward compatibility
            passed_checks = len(validation_results['targets_met'])
            validation_results['binary_score'] = passed_checks / 4.0
            
            return validation_results
            
        except Exception as e:
            from .logger import log_event as _log
            _log("MICRO_VALIDATION_ERROR", f"Microstructure validation failed: {str(e)}")
            return {
                'targets_met': {},
                'targets_missed': {},
                'improvement_suggestions': ['Validation failed - unable to check targets'],
                'validation_score': 0.0
            }

    def _micro_quick_pass(self, text: str, targets: dict) -> str:
        import re as _re
        t = text or ""
        for c in (targets.get('banned_cliches') or []):
            t = _re.sub(rf"\b{_re.escape(c)}\b", "", t, flags=_re.IGNORECASE)
        try:
            sl_min = float(targets.get('avg_sentence_len_min', 12))
            sl_max = float(targets.get('avg_sentence_len_max', 22))
        except Exception:
            sl_min, sl_max = 12.0, 22.0
        def _words(u: str):
            return _re.findall(r"[A-Za-z0-9']+", (u or ""))
        
        def _robust_sentence_split(text: str):
            # Handle abbreviations before splitting
            abbrev_patterns = [
                r'\b(?:Dr|Mr|Mrs|Ms|Prof|Sr|Jr|Inc|Ltd|Corp|Co|St|Mt|Fig|Vol|No|vs|p\.m|a\.m)\.',
                r'\bU\.S\.(?:A\.)?',
                r'\bU\.K\.',
                r'\b(?:e\.g|i\.e|etc)\.',
                r'\b\d+\.',  # Numbers like "1." or "Fig. 3"
            ]
            
            placeholder_map = {}
            for i, pattern in enumerate(abbrev_patterns):
                matches = _re.finditer(pattern, text, _re.IGNORECASE)
                for match in matches:
                    placeholder = f"__ABBREV_{i}_{len(placeholder_map)}__"
                    placeholder_map[placeholder] = match.group(0)
                    text = text.replace(match.group(0), placeholder, 1)
            
            sentences = [s for s in _re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
            
            # Restore abbreviations
            for placeholder, original in placeholder_map.items():
                for i, sentence in enumerate(sentences):
                    sentences[i] = sentence.replace(placeholder, original)
            
            return sentences
        
        sents = _robust_sentence_split(t)
        fixed = []
        for s in sents:
            if len(_words(s)) > sl_max + 6:
                parts = _re.split(r"([,;:])", s)
                if len(parts) > 3:
                    mid = len(parts)//2
                    a = ''.join(parts[:mid]).strip().rstrip(',;:') + '.'
                    b = ''.join(parts[mid:]).strip().lstrip(',;:')
                    if b and not _re.search(r"[.!?]$", b):
                        b += '.'
                    fixed.extend([a, b])
                    continue
            fixed.append(s)
        return ('\n'.join(fixed)).strip()

    def _tone_quick_pass(self, text: str, target: str, intensity: str, keywords: list) -> tuple[str, dict]:
        import re as _re
        t = text or ""
        low_target = (target or '').lower()
        level = {'light': 0.3, 'medium': 0.6, 'strong': 0.9}.get((intensity or 'light').lower(), 0.3)
        # Guardrail: don't change domain keywords
        safe = set([str(k).strip().lower() for k in (keywords or []) if str(k).strip()])

        # Lexicons
        contractions = {
            "I'm": "I am", "can't": "cannot", "won't": "will not", "don't": "do not", "doesn't": "does not",
            "isn't": "is not", "aren't": "are not", "it's": "it is", "we're": "we are", "they're": "they are",
        }
        greetings_friendly = ["Hi", "Hey", "Hello", "Thanks", "Cheers"]
        closers_formal = ["Sincerely", "Regards", "Best regards"]
        modals_exec_up = {"would": "will", "could": "can", "might": "will"}
        intensifiers_down = {"very": "", "really": "", "extremely": "", "highly": ""}

        swaps = 0
        def replace_word_boundary(src: str, dst: str) -> None:
            nonlocal t, swaps
            # Skip if src is a keyword
            if src.lower() in safe:
                return
            old = t
            
            # Case-preserving replacement function
            def case_preserving_replace(match):
                original = match.group(0)
                if original.isupper():
                    return dst.upper()
                elif original[0].isupper():
                    return dst.capitalize()
                else:
                    return dst.lower()
            
            # Use case-insensitive matching with case preservation
            t = _re.sub(rf"\b{_re.escape(src)}\b", case_preserving_replace, t, flags=_re.IGNORECASE)
            if t != old:
                swaps += 1

        if low_target in ("formal","academic","executive"):
            # Expand contractions based on intensity
            for k, v in contractions.items():
                if level >= 0.3:
                    replace_word_boundary(k, v)
            # Remove casual greetings
            for g in greetings_friendly:
                if level >= 0.6:
                    t = _re.sub(rf"^(\s*){_re.escape(g)}[,!]?\s+", r"\\1", t, flags=_re.IGNORECASE|_re.MULTILINE)
            # Executive modal uplift
            if low_target == 'executive' and level >= 0.6:
                for k, v in modals_exec_up.items():
                    replace_word_boundary(k, v)
            # Add formal closers when likely an email (heuristic):
            if level >= 0.6 and len(t.splitlines()) > 4 and not _re.search(r"Sincerely|Regards", t):
                t = t.rstrip() + "\n\n" + closers_formal[0]
        elif low_target == "friendly":
            # Introduce contractions lightly
            informal = {v: k for k, v in contractions.items()}
            for k, v in informal.items():
                if level >= 0.3:
                    replace_word_boundary(k, v)
            # Remove stiff intensifiers lightly (keep tone casual, avoid overstatement)
            for k, v in intensifiers_down.items():
                if level >= 0.3:
                    t = _re.sub(rf"\b{_re.escape(k)}\b\s+", v, t)
        else:
            # neutral: minimal
            if level >= 0.6:
                for k, v in intensifiers_down.items():
                    t = _re.sub(rf"\b{_re.escape(k)}\b\s+", v, t)

        return t, {"swaps": swaps}

    def _anti_scanner_quick_pass(self, text: str, jitter: float, rare_cap_per_100: float, safe_keywords: list = None) -> tuple[str, dict]:
        import re as _re
        import random as _rnd
        t = text or ""
        stats = {"synonym_swaps": 0, "idioms_added": 0, "splits": 0, "joins": 0, "scaffolds_removed": 0}
        
        # Guardrail: don't change domain keywords
        safe = set([str(k).strip().lower() for k in (safe_keywords or []) if str(k).strip()])

        # Helper tokenization
        def _words(u: str):
            return _re.findall(r"[A-Za-z0-9']+", (u or ""))
        def _sentences(u: str):
            return [s for s in _re.split(r"(?<=[.!?])\s+", (u or "").strip()) if s]

        # Remove overused scaffolds at sentence starts (but be selective)
        scaffolds_to_remove = ["In conclusion,", "To summarize,", "Overall,", "In summary,"]
        # Don't remove natural transition words that improve flow
        
        for sf in scaffolds_to_remove:
            before = t
            t = _re.sub(rf"(^|\n)(\s*){_re.escape(sf)}\s*", r"\\1\\2", t)
            if t != before:
                stats["scaffolds_removed"] += 1

        # Idiom sprinkling (light caps)
        idioms = ["to be fair", "for what it's worth", "oddly enough", "now and then"]
        if jitter > 0.05:
            sents = _sentences(t)
            out_s = []
            max_add = max(0, int(len(sents) * min(0.15, jitter)))
            added = 0
            for s in sents:
                if added < max_add and _rnd.random() < jitter * 0.15 and len(s.split()) > 6:
                    ins = _rnd.choice(idioms)
                    # avoid duplicating at start or anywhere in sentence
                    if (not s.lower().startswith(tuple(i.lower() for i in idioms)) and 
                        ins.lower() not in s.lower()):
                        s = f"{ins}, " + s[0].lower() + s[1:]
                        added += 1
                out_s.append(s)
            stats["idioms_added"] = added
            t = " ".join(out_s)

        # Sentence length variance: occasional split/join
        sents = _sentences(t)
        varied = []
        for s in sents:
            acted = False
            if len(_words(s)) > 28 and _rnd.random() < jitter * 0.2:
                parts = _re.split(r"([,;:])", s)
                if len(parts) > 3:
                    mid = len(parts)//2
                    a = ''.join(parts[:mid]).strip().rstrip(',;:') + '.'
                    b = ''.join(parts[mid:]).strip().lstrip(',;:')
                    if b and not _re.search(r"[.!?]$", b):
                        b += '.'
                    varied.extend([a, b])
                    stats["splits"] += 1
                    acted = True
            # Append sentence (joining logic removed as it was not implemented)
            varied.append(s)
        t = " ".join(varied)

        # Synonym diversity (bounded by rare_cap_per_100)
        vocab_map = {
            "use": ["employ","apply"],
            "help": ["assist","support"],
            "show": ["demonstrate","reveal"],
            "make": ["create","produce"],
            "get": ["obtain","receive"],
            "important": ["crucial","vital"],
            "good": ["solid","sound"],
        }
        tokens = max(1, len(_words(t)))
        cap = int(max(0.0, rare_cap_per_100) * (tokens / 100.0))
        swaps = 0
        if jitter > 0.05 and cap > 0:
            for src, alts in vocab_map.items():
                if swaps >= cap:
                    break
                # Skip protected keywords
                if src.lower() in safe:
                    continue
                if _rnd.random() < jitter * 0.3:
                    alt = _rnd.choice(alts)
                    before = t
                    t = _re.sub(rf"\b{_re.escape(src)}\b", alt, t)
                    if t != before:
                        swaps += 1
        stats["synonym_swaps"] = swaps

        return t, stats

    def _macro_analyze(self, text: str) -> dict:
        import re as _re
        import difflib as _dif
        paras = [p for p in (text or '').split('\n\n') if p.strip()]
        labels = []
        needs_headings = []
        needs_separators = []
        # Simple heuristics
        heading_re = _re.compile(r"^(#{1,3}\s|[A-Z][\w\s\-]{0,60}$)")
        bullet_re = _re.compile(r"^\s*([\-•*]|\d+\.)\s+")
        intro_re = _re.compile(r"\b(in this|here we|this (?:document|paper|memo)|overview)\b", _re.I)
        concl_re = _re.compile(r"\b(in conclusion|to conclude|to sum up|overall)\b", _re.I)
        point_re = _re.compile(r"\b(first|second|third|next|additionally|furthermore)\b", _re.I)
        for i, p in enumerate(paras):
            first_line = p.strip().splitlines()[0] if p.strip().splitlines() else ''
            if heading_re.match(first_line):
                labels.append('heading')
                continue
            if bullet_re.search(first_line):
                labels.append('bullet')
                continue
            low = p.lower()
            if intro_re.search(low) and i < 3:
                labels.append('intro')
            elif concl_re.search(low) and i >= max(0, len(paras)-3):
                labels.append('conclusion')
            elif point_re.search(low):
                labels.append('point')
            else:
                labels.append('body')
        # Coverage
        coverage = {
            'intro': any(l=='intro' for l in labels) or any(l=='heading' for l in labels[:2]),
            'points': any(l in ('point','bullet') for l in labels),
            'conclusion': any(l=='conclusion' for l in labels) or any(l=='heading' for l in labels[-2:]),
        }
        # Needs heading: long body paragraphs without nearby heading
        for i, (l, p) in enumerate(zip(labels, paras)):
            if l == 'body' and len(p.split()) > 80:
                prev_is_head = (i>0 and labels[i-1]=='heading')
                if not prev_is_head:
                    needs_headings.append(i)
        # Needs separators: detect abrupt topic shifts by low token overlap
        redundant_pairs = []
        for i in range(len(paras)-1):
            a, b = paras[i], paras[i+1]
            ratio = _dif.SequenceMatcher(None, a, b).ratio()
            if ratio > 0.92:
                redundant_pairs.append((i, i+1))
            if ratio < 0.35:
                needs_separators.append(i)
        return {
            'paragraphs': paras,
            'labels': labels,
            'coverage': coverage,
            'needs_headings': needs_headings,
            'needs_separators': needs_separators,
            'redundant_pairs': redundant_pairs,
        }

    def _apply_macro_recommendations(self, text: str, macro_results: dict) -> str:
        """Apply macro analysis recommendations programmatically."""
        try:
            import re as _re
            if not macro_results:
                return text
            
            # Normalize line endings before splitting
            normalized = _re.sub(r'\r\n?', '\n', text)  # Convert to Unix line endings
            normalized = _re.sub(r'\n{3,}', '\n\n', normalized)  # Collapse multiple newlines
            
            paragraphs = [p.strip() for p in normalized.split('\n\n') if p.strip()]
            if not paragraphs:
                return text
            
            # Track index adjustments from insertions
            index_shift = 0
            
            # Apply heading recommendations first (no index shifts)
            if macro_results.get('needs_headings'):
                for idx in sorted(macro_results['needs_headings'][:3]):  # Sort for predictable order
                    adjusted_idx = idx + index_shift
                    if 0 <= adjusted_idx < len(paragraphs):
                        para = paragraphs[adjusted_idx]
                        # Simple heuristic: if paragraph is long and doesn't start with #, add heading
                        if len(para.split()) > 50 and not para.startswith('#'):
                            # Extract first few words as heading and clean punctuation
                            words = para.split()[:5]  # Shorter heading
                            heading_text = ' '.join(words).rstrip('.,;:')  # Remove trailing punctuation
                            
                            # Remove heading words from paragraph to avoid duplication
                            para_words = para.split()[5:]  # Skip first 5 words
                            para_without_heading = ' '.join(para_words) if para_words else para
                            
                            paragraphs[adjusted_idx] = f"## {heading_text}\n\n{para_without_heading}"
            
            # Apply separator recommendations (causes index shifts)
            if macro_results.get('needs_separators'):
                # Add separators after paragraphs that need them (to separate from next paragraph)
                # Process in reverse order to maintain indices
                for idx in reversed(sorted(macro_results['needs_separators'][:3])):
                    adjusted_idx = idx + index_shift
                    if 0 <= adjusted_idx < len(paragraphs):
                        paragraphs.insert(adjusted_idx + 1, "---")
                        index_shift += 1  # Each insertion shifts subsequent indices
            
            # Apply redundancy removal
            if macro_results.get('redundant_pairs'):
                # Remove redundant paragraphs (keep the longer one)
                removed_indices = set()
                for a, b in macro_results['redundant_pairs'][:3]:  # Limit to first 3 pairs
                    # Only process if neither paragraph is already marked for removal
                    if a not in removed_indices and b not in removed_indices:
                        if 0 <= a < len(paragraphs) and 0 <= b < len(paragraphs):
                            # Keep the longer paragraph
                            if len(paragraphs[a]) > len(paragraphs[b]):
                                removed_indices.add(b)
                            else:
                                removed_indices.add(a)
                
                # Remove paragraphs in reverse order to maintain indices
                for idx in sorted(removed_indices, reverse=True):
                    if 0 <= idx < len(paragraphs):
                        del paragraphs[idx]
            
            result = '\n\n'.join(paragraphs)
            
            # Safety check: return original if result is empty
            if not result.strip():
                from .logger import log_event as _log
                _log("MACRO_APPLICATION_WARNING", "Macro application resulted in empty text, returning original")
                return text
            
            return result
            
        except Exception as e:
            from .logger import log_event as _log
            _log("MACRO_APPLICATION_ERROR", f"Failed to apply macro recommendations: {str(e)}")
            return text


