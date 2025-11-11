from __future__ import annotations

import os
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from .utils import load_heuristics


@dataclass
class Settings:
    openai_api_key: str
    openai_model: str = "gpt-4.1"
    aggressiveness: str = "Auto"  # Auto | Low | Medium | High | Very High
    random_seed: Optional[int] = None
    batch_pace_delay_s: float = 0.5
    heuristics: Dict[str, Any] = None  # type: ignore
    min_word_ratio: float = 0.8  # output must be at least 80% of input words
    target_scanner_risk: float = 25.0  # desired max risk to stop

    @staticmethod
    def load() -> "Settings":
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY", "")
        aggr = os.getenv("AGGRESSIVENESS", "Auto")
        model_name = os.getenv("OPENAI_MODEL", "gpt-4.1")
        seed_env = os.getenv("RANDOM_SEED", "")
        seed = int(seed_env) if seed_env.strip().isdigit() else None
        try:
            delay = float(os.getenv("BATCH_PACE_DELAY", "0.5"))
        except Exception:
            delay = 0.5
        heur = load_heuristics()
        try:
            min_ratio = float(os.getenv("MIN_WORD_RATIO", "0.8"))
        except Exception:
            min_ratio = 0.8
        try:
            target_risk = float(os.getenv("TARGET_SCANNER_RISK", "25"))
        except Exception:
            target_risk = 25.0
        return Settings(
            openai_api_key=api_key,
            aggressiveness=aggr,
            openai_model=model_name,
            random_seed=seed,
            batch_pace_delay_s=delay,
            heuristics=heur,
            min_word_ratio=min_ratio,
            target_scanner_risk=target_risk,
        )


