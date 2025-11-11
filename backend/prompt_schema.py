
# prompt_schema.py

ADVANCED_COMMANDS = {
    "strategy_insight": {
        "description": "Detailed explanation of what the next refinement pass will target and why.",
        "keys": [
            "targeted_patterns", "structural_mods", "entropy_mode", "pass_type", "diff_estimate"
        ]
    },
    "microstructure_control": {
        "description": "Detailed breakdown of clause shaping, sentence rhythm, and repetition mitigation.",
        "keys": [
            "starter_variety", "clause_symmetry_break", "length_distribution", "conjunction_usage"
        ]
    },
    "macrostructure_analysis": {
        "description": "Global adjustments across paragraphs or sections including rhythm maps and paragraph pacing.",
        "keys": [
            "cadence_map", "para_length_profile", "logic_flow_disruption", "topic_recurrence"
        ]
    },
    "semantic_tone_tuning": {
        "description": "Manipulates tone and ambiguity to mimic natural human inconsistency.",
        "keys": [
            "ambivalence_level", "tentative_phrasings", "interruption_tokens", "opinion_drift"
        ]
    },
    "anti_scanner_techniques": {
        "description": "Core stealth tactics designed to reduce detection probability by introducing controlled imperfection.",
        "keys": [
            "punctuation_variance", "rhetorical_periods", "lowercase_insert", "fragment_insertion"
        ]
    },
    "entropy_management": {
        "description": "Control over entropy sampling, suppression or expansion of token predictability.",
        "keys": [
            "sampling_profile", "logit_bias_flags", "token_rarity_target", "forced_novelty_rate"
        ]
    },
    "history_analysis": {
        "description": "Summarize prior runs and mine logs/history to seed strategy (brevity/structure/tone tendencies).",
        "keys": [
            "diff_ratio", "token_overlap", "structural_drift", "pattern_eliminations", "session_profile"
        ]
    },
    "formatting_safeguards": {
        "description": "Respect and preserve formatting constraints and logical anchors.",
        "keys": [
            "h1_h2_h3_count", "paragraph_spacing", "style_markers_preserved", "lock_tokens"
        ]
    },
    "refiner_control": {
        "description": "User-level directives for pass management or instruction injection.",
        "keys": [
            "expert_mode", "terse_mode", "next_pass_stack", "fork_preview", "rollback_options"
        ]
    },
    "annotation_mode": {
        "description": "User can ask questions about specific phrases or flags.",
        "keys": [
            "why_flagged", "show_rhythm_map", "explain_entropy", "highlight_trigger_tokens"
        ]
    },
    "humanize_academic": {
        "description": "Apply light humanization with academic transitions and optional synonym/passive tweaks.",
        "keys": [
            "use_passive", "use_synonyms", "intensity"
        ]
    }
}
