from backend.prompt_schema import ADVANCED_COMMANDS
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

class ConversationalRefiner:
    def __init__(self, api_key):
        self.api_key = api_key
        self.client = OpenAI(api_key=api_key)
        self.messages = []
        self.score = None
        self.conversation_context = {
            "current_file": None,
            "current_pass": None,
            "recent_changes": [],
            "user_preferences": {},
            "session_goals": []
        }
        # Reuse a small thread pool to enforce timeouts around blocking API calls
        self._executor = ThreadPoolExecutor(max_workers=2)
        
    def _score_hint(self):
        if self.score is None:
            return ""
        return f"(Current detection score: {self.score:.1f}%)"

    def is_schema_request(self, message: str) -> bool:
        m = message.strip().lower()
        if m in {"/schema", "schema", "/help", "help", "show schema"}:
            return True
        schema_terms = [
            "schema", "schema details", "advanced commands", "capabilities",
            "toggles", "flags", "what can you do", "show commands", "list commands",
            "who are you", "what are you", "hi", "hello"
        ]
        return any(term in m for term in schema_terms)

    def matches_strategy_request(self, message: str) -> bool:
        triggers = [
            "strategy", "strat", "gameplan", "plan", "playbook",
            "approach", "next move", "your move", "what are you doing",
            "how will you proceed", "what's your move", "what's the strat",
            "what comes next", "refinement direction", "what tactic"
        ]
        lowered = message.lower()
        return any(t in lowered for t in triggers)

    def extract_refiner_flags(self, message: str) -> dict:
        flags = {}
        msg = message.lower()
        for key in ADVANCED_COMMANDS:
            if key in msg or f"#{key}" in msg:
                flags[key] = True
        return flags

    def get_advanced_strategy_insight(self) -> str:
        lines = ["🎯 **Turbo Alan Strategy Modes:**\n"]
        for k, v in ADVANCED_COMMANDS.items():
            desc = v.get("description") if isinstance(v, dict) else str(v)
            lines.append(f"• `{k}` — {desc}")
        lines.append("\nType /schema to view these again, or ask about any control.")
        return "\n".join(lines)

    # --- Schema descriptions (rule-based, fast path) ---
    def _level_label(self, level: int | None) -> str:
        if level is None:
            return "(level: n/a)"
        return {
            0: "(off)",
            1: "(light)",
            2: "(moderate)",
            3: "(aggressive)",
        }.get(int(level), "(custom)")

    def describe_schema(self, schema_id: str, level: int | None = None) -> str:
        sid = str(schema_id or '').strip().lower()
        entry = ADVANCED_COMMANDS.get(sid)
        if not entry:
            return f"Unknown schema: {schema_id}"
        desc = entry.get('description') if isinstance(entry, dict) else str(entry)
        level_note = self._level_label(level)
        tips = {
            'microstructure_control': "Targets sentence length, passive voice, hedges, and cliché removal.",
            'macrostructure_analysis': "Looks for missing intro/conclusion, section headings, and redundant paragraphs.",
            'semantic_tone_tuning': "Tunes formality/friendliness/executive tone while preserving domain terms.",
            'anti_scanner_techniques': "Applies controlled variation (jitter), caps rare substitutions, removes overused scaffolds.",
            'entropy_management': "Shapes temperature/penalties to avoid repetitive n-grams and generic transitions.",
            'formatting_safeguards': "Protects headings/lists/code blocks and restores after refinement.",
            'refiner_control': "Governs pass aggressiveness and structure-change allowances.",
            'history_analysis': "Derives session profile from past passes to nudge strategy.",
            'annotation_mode': "Adds inline or sidecar notes explaining changes and rationale.",
            'humanize_academic': "Light humanization for academic tone with optional passive/synonym tweaks.",
        }.get(sid, '')
        more = f"\nHint: {tips}" if tips else ''
        return f"• `{sid}` {level_note} — {desc}{more}"

    def describe_all_schemas(self, schema_levels: dict | None) -> str:
        levels = {str(k): int(v) for k, v in (schema_levels or {}).items() if isinstance(v, (int, float))}
        lines = ["📊 **Current Schema Overview:**\n"]
        for sid in ADVANCED_COMMANDS.keys():
            lines.append(self.describe_schema(sid, levels.get(sid)))
        return "\n".join(lines)

    def summarize_active_strategy(self, flags: dict) -> str:
        if not flags:
            return "No schema flags are currently active. Enable some toggles or pass flags to activate strategy modes."

        lines = ["🧠 **Turbo Alan Active Strategy:**\n"]
        for key in flags:
            if key in ADVANCED_COMMANDS:
                desc = ADVANCED_COMMANDS[key]["description"]
                lines.append(f"• `{key}` — {desc}")
        return "\n".join(lines)

    def _safe_chat_completion(self, messages, model: str = "gpt-4", temperature: float = 0.7, timeout_seconds: int = 30) -> str:
        """Run OpenAI chat completion with a hard timeout and safe fallback."""
        def _call():
            return self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )

        try:
            response = self._executor.submit(_call).result(timeout=timeout_seconds)
            return response.choices[0].message.content
        except FuturesTimeoutError:
            return "The chat request timed out while contacting the model. Please try again."
        except Exception:
            return "The chat request failed while contacting the model. Please try again later."

    def chat(self, message, flags=None):
        # Extract context from the message
        self.extract_context_from_message(message)
        
        # Schema fast-paths (no model call)
        mlow = (message or '').strip().lower()
        if self.is_schema_request(mlow):
            return self.get_advanced_strategy_insight()
        # Explain single schema: "explain <schema>" or "explain <schema> to me"
        if mlow.startswith('explain') or 'explain' in mlow or 'what is' in mlow:
            # try to match any schema id within message
            for sid in ADVANCED_COMMANDS.keys():
                if sid in mlow:
                    return self.describe_schema(sid, getattr(self, 'schema_levels', {}).get(sid))
            if 'all' in mlow or 'current schema' in mlow or 'schemas' in mlow:
                return self.describe_all_schemas(getattr(self, 'schema_levels', {}))

        if flags is None:
            flags = self.extract_refiner_flags(message)
        if hasattr(self, 'last_flags'):
            flags.update(self.last_flags)
        if self.matches_strategy_request(message):
            return self.summarize_active_strategy(flags)

        # Build context-aware prompt
        context_summary = self.get_context_summary()
        context_prompt = f"Context: {context_summary}\n\n" if context_summary != "No specific context available." else ""
        
        prompt = f"{context_prompt}{message}"
        if self.score:
            prompt += "\n" + self._score_hint()

        context_gate = (
            "IMPORTANT: Do not rewrite anything unless the user explicitly asks you to rewrite, revise, or propose edits.\n"
            "If this is feedback or a question, only respond with insight or advice.\n"
            "Consider the conversation context when providing responses.\n"
            "If the user is referring to a specific file or pass, acknowledge it in your response."
        )
        self.messages.append({"role": "user", "content": context_gate + "\n" + prompt})

        reply = self._safe_chat_completion(self.messages, model="gpt-4", temperature=0.7, timeout_seconds=30)

        # Optional post-processing: humanize academic tone if requested
        if flags.get("humanize_academic"):
            try:
                # Lazy import to avoid heavy deps when not used
                from backend.academic_humanizer import AcademicTextHumanizer, download_nltk_resources
                download_nltk_resources()

                # Allow simple tuning via flags dict
                use_passive = bool(flags.get("use_passive", False))
                use_synonyms = bool(flags.get("use_synonyms", False))
                humanizer = AcademicTextHumanizer()
                reply = humanizer.humanize_text(reply, use_passive=use_passive, use_synonyms=use_synonyms)
            except Exception:
                # Fail open: if humanization fails, return the original reply
                pass
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def set_flags(self, flags: dict):
        self.last_flags = flags

    def set_score(self, score):
        self.score = score

    def update_context(self, **kwargs):
        """Update conversation context with new information"""
        for key, value in kwargs.items():
            if key in self.conversation_context:
                self.conversation_context[key] = value

    def get_context_summary(self):
        """Generate a context summary for the AI"""
        context_parts = []
        
        if self.conversation_context["current_file"]:
            context_parts.append(f"Currently working on: {self.conversation_context['current_file']}")
        
        if self.conversation_context["current_pass"]:
            context_parts.append(f"Current pass: {self.conversation_context['current_pass']}")
        
        if self.conversation_context["recent_changes"]:
            changes = self.conversation_context["recent_changes"][-3:]  # Last 3 changes
            context_parts.append(f"Recent changes: {', '.join(changes)}")
        
        if self.conversation_context["user_preferences"]:
            prefs = self.conversation_context["user_preferences"]
            context_parts.append(f"User preferences: {', '.join(f'{k}={v}' for k, v in prefs.items())}")
        
        if self.conversation_context["session_goals"]:
            goals = self.conversation_context["session_goals"]
            context_parts.append(f"Session goals: {', '.join(goals)}")
        
        return "\n".join(context_parts) if context_parts else "No specific context available."

    def extract_context_from_message(self, message):
        """Extract context information from user message"""
        message_lower = message.lower()
        
        # Extract file references
        if "file" in message_lower and ("current" in message_lower or "working" in message_lower):
            # Try to extract filename from message
            import re
            file_match = re.search(r'file[:\s]+([^\s,]+)', message)
            if file_match:
                self.conversation_context["current_file"] = file_match.group(1)
        
        # Extract pass references
        if "pass" in message_lower:
            import re
            pass_match = re.search(r'pass[:\s]+(\d+)', message)
            if pass_match:
                self.conversation_context["current_pass"] = int(pass_match.group(1))
        
        # Extract preferences
        if "prefer" in message_lower or "like" in message_lower:
            if "formal" in message_lower:
                self.conversation_context["user_preferences"]["tone"] = "formal"
            elif "casual" in message_lower:
                self.conversation_context["user_preferences"]["tone"] = "casual"
            elif "academic" in message_lower:
                self.conversation_context["user_preferences"]["tone"] = "academic"
        
        # Extract goals
        if "goal" in message_lower or "want" in message_lower or "need" in message_lower:
            if "reduce" in message_lower and "ai" in message_lower:
                self.conversation_context["session_goals"].append("reduce AI detection")
            if "improve" in message_lower and "readability" in message_lower:
                self.conversation_context["session_goals"].append("improve readability")
            if "maintain" in message_lower and "meaning" in message_lower:
                self.conversation_context["session_goals"].append("maintain meaning")






