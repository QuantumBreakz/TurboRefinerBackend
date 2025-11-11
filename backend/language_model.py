from __future__ import annotations

from typing import Protocol, List, Dict, Any
import time
import math
import os
from collections import deque, defaultdict
from datetime import datetime
try:  # optional dependency for token budgeting
    import tiktoken
except Exception:  # pragma: no cover
    tiktoken = None

# OpenAI Pricing (as of 2024)
OPENAI_PRICING = {
    'gpt-4': {
        'input': 0.03,  # $0.03 per 1K tokens
        'output': 0.06  # $0.06 per 1K tokens
    },
    'gpt-4-turbo': {
        'input': 0.01,  # $0.01 per 1K tokens
        'output': 0.03  # $0.03 per 1K tokens
    },
    'gpt-3.5-turbo': {
        'input': 0.0015,  # $0.0015 per 1K tokens
        'output': 0.002   # $0.002 per 1K tokens
    }
}

def calculate_cost(tokens_in: int, tokens_out: int, model: str = "gpt-4") -> dict:
    """Calculate cost for given token usage and model"""
    pricing = OPENAI_PRICING.get(model, OPENAI_PRICING['gpt-4'])
    
    input_cost = (tokens_in / 1000) * pricing['input']
    output_cost = (tokens_out / 1000) * pricing['output']
    total_cost = input_cost + output_cost
    
    return {
        'input_cost': input_cost,
        'output_cost': output_cost,
        'total_cost': total_cost,
        'tokens_in': tokens_in,
        'tokens_out': tokens_out,
        'model': model
    }

# Simple in-memory analytics store (exported for API)
class _Analytics:
    def __init__(self) -> None:
        self.total_requests = 0
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.total_cost = 0.0
        self.current_model = "gpt-4"  # Track current model
        # ring buffer of (ts_sec, in_tokens, out_tokens, model, cost)
        self.events: deque[tuple[int,int,int,str,float]] = deque(maxlen=10_000)
        # Job-level cost tracking
        self.job_costs: Dict[str, float] = {}  # job_id -> total_cost
        self.pass_costs: Dict[str, List[float]] = {}  # job_id -> [pass1_cost, pass2_cost, ...]
        # Schema usage tracking
        self.schema_usage: Dict[str, int] = {}  # schema_id -> usage_count
        self.schema_last_used: Dict[str, str] = {}  # schema_id -> last_used_timestamp

    def add(self, in_tokens: int, out_tokens: int, model: str = "gpt-4", job_id: str = None) -> dict:
        now = int(time.time())
        self.total_requests += 1
        self.total_tokens_in += max(0, in_tokens)
        self.total_tokens_out += max(0, out_tokens)
        self.current_model = model
        
        # Calculate cost for this request
        cost_info = calculate_cost(in_tokens, out_tokens, model)
        self.total_cost += cost_info['total_cost']
        
        # Track job-level costs
        if job_id:
            if job_id not in self.job_costs:
                self.job_costs[job_id] = 0.0
                self.pass_costs[job_id] = []
            self.job_costs[job_id] += cost_info['total_cost']
            self.pass_costs[job_id].append(cost_info['total_cost'])
        
        self.events.append((now, in_tokens, out_tokens, model, cost_info['total_cost']))
        return cost_info

    def summary_last_24h(self) -> Dict[str, Any]:
        now = int(time.time())
        cutoff = now - 24*3600
        per_hour = defaultdict(lambda: {"requests":0, "tokens_in":0, "tokens_out":0, "cost":0.0})
        reqs = 0; ti = 0; to = 0; cost_24h = 0.0
        for ts, tin, tout, model, cost in list(self.events):
            if ts < cutoff: continue
            hour_bucket = ts - (ts % 3600)
            b = per_hour[hour_bucket]
            b["requests"] += 1
            b["tokens_in"] += max(0,tin)
            b["tokens_out"] += max(0,tout)
            b["cost"] += cost
            reqs += 1; ti += max(0,tin); to += max(0,tout); cost_24h += cost
        series = [
            {"hour": k, **per_hour[k]}
            for k in sorted(per_hour.keys())
        ]
        return {"requests": reqs, "tokens_in": ti, "tokens_out": to, "cost": cost_24h, "series": series}
    
    def get_job_cost(self, job_id: str) -> dict:
        """Get cost information for a specific job"""
        total_cost = self.job_costs.get(job_id, 0.0)
        pass_costs = self.pass_costs.get(job_id, [])
        return {
            "job_id": job_id,
            "total_cost": total_cost,
            "pass_costs": pass_costs,
            "pass_count": len(pass_costs),
            "avg_cost_per_pass": total_cost / max(len(pass_costs), 1)
        }
    
    def track_schema_usage(self, schema_id: str, schema_level: int) -> None:
        """Track schema usage for analytics"""
        if schema_level > 0:  # Only track active schemas
            self.schema_usage[schema_id] = self.schema_usage.get(schema_id, 0) + 1
            self.schema_last_used[schema_id] = datetime.now().isoformat()
    
    def get_schema_usage_stats(self) -> dict:
        """Get schema usage statistics"""
        total_usages = sum(self.schema_usage.values())
        most_used = max(self.schema_usage.items(), key=lambda x: x[1]) if self.schema_usage else None
        least_used = min(self.schema_usage.items(), key=lambda x: x[1]) if self.schema_usage else None
        
        return {
            "total_usages": total_usages,
            "most_used_schema": most_used[0] if most_used else None,
            "most_used_count": most_used[1] if most_used else 0,
            "least_used_schema": least_used[0] if least_used else None,
            "least_used_count": least_used[1] if least_used else 0,
            "average_usage": total_usages / len(self.schema_usage) if self.schema_usage else 0,
            "schema_usage": dict(self.schema_usage),
            "schema_last_used": dict(self.schema_last_used),
        }

analytics_store = _Analytics()


class LanguageModel(Protocol):
    def generate(self, system: str, user: str, temperature: float = 0.4, max_tokens: int = 2000) -> str: ...


class OpenAIModel:
    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def generate(self, system: str, user: str, temperature: float = 0.4, max_tokens: int = 2000, job_id: str = None) -> tuple[str, dict]:
        t0 = time.perf_counter()
        
        def _estimate_max_chars_for_model(model: str, max_input_tokens: int = 7000) -> int:
            # Heuristic: ~4 chars/token. Leave room for system + output tokens.
            return max(2000, max_input_tokens * 4)

        def _truncate_text(text: str, max_chars: int) -> str:
            if len(text) <= max_chars:
                return text
            # Prefer keeping the beginning (context/structure) for refinement
            return text[:max_chars]

        # Best-effort: attempt the call, and on context errors, progressively truncate
        attempt_user = user
        max_chars = _estimate_max_chars_for_model(self.model)
        if len(attempt_user) > max_chars:
            attempt_user = _truncate_text(attempt_user, max_chars)

        # Optional token budget pre-check using tiktoken if enabled
        max_in = int(os.environ.get("REFINER_MAX_INPUT_TOKENS", "0") or 0)
        if max_in > 0 and tiktoken is not None:
            try:
                enc = tiktoken.encoding_for_model(self.model)
            except Exception:
                enc = tiktoken.get_encoding("cl100k_base")
            pre_tokens = len(enc.encode(system)) + len(enc.encode(attempted_user if 'attempted_user' in locals() else attempt_user))
            if pre_tokens > max_in:
                raise ValueError(f"TOKEN_BEDGET_EXCEEDED: {pre_tokens}>{max_in}")

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": attempt_user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            # Handle context length exceeded by retrying with more aggressive truncation
            err_str = str(e)
            if "context length" in err_str or "maximum context length" in err_str or "context_length_exceeded" in err_str:
                # Retry with 50% size, then 25%, etc.
                for factor in (0.5, 0.25, 0.125):
                    try:
                        reduced = _truncate_text(user, int(max_chars * factor))
                        resp = self.client.chat.completions.create(
                            model=self.model,
                            messages=[
                                {"role": "system", "content": system},
                                {"role": "user", "content": reduced},
                            ],
                            temperature=temperature,
                            max_tokens=max_tokens,
                        )
                        break
                    except Exception as inner_e:
                        if factor == 0.125:
                            raise inner_e
                        continue
            else:
                raise

        content = resp.choices[0].message.content or ""
        # Token usage (OpenAI provides usage fields)
        try:
            used_in = int(getattr(resp, "usage").prompt_tokens or 0)
            used_out = int(getattr(resp, "usage").completion_tokens or 0)
        except Exception:
            used_in = 0; used_out = 0
        
        # Track cost and return cost information
        cost_info = analytics_store.add(used_in, used_out, self.model, job_id)
        return content, cost_info


