from openai import OpenAI
import time
import random
import os
from backend.utils import load_heuristics

class RefinementMemory:
    def __init__(self):
        self.history = []

    def log_pass(self, original: str, refined: str, score: float = None, notes: list = None):
        self.history.append({
            "input": original,
            "output": refined,
            "score": score,
            "notes": notes or []
        })

    def last_output(self) -> str:
        return self.history[-1]['output'] if self.history else None

    def last_score(self) -> float:
        return self.history[-1]['score'] if self.history else None


def gpt4_multi_pass_refine(api_key: str, text: str, heuristics: dict) -> str:
    client = OpenAI(api_key=api_key, timeout=240.0)

    system = {
        "role": "system",
        "content": (
            "You are Turbo Alan, an AI content refiner. "
            "Your goal is to revise the user's content to reduce AI detection scores below 10%, "
            "while preserving clarity, structure, and tone exactly as provided."
        )
    }

    user = {"role": "user", "content": text}

    # Retry with exponential backoff on transient errors and rate limits
    max_retries = int(os.getenv('API_MAX_RETRIES', '6'))
    backoff = float(os.getenv('API_BACKOFF_START', '1.0'))
    backoff_cap = float(os.getenv('API_BACKOFF_CAP', '8.0'))
    last_err = None
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model="gpt-4",
                messages=[system, user],
                temperature=0.45,
                top_p=0.9,
                max_tokens=2048
            )
            refined = resp.choices[0].message.content
            return refined
        except Exception as e:
            last_err = e
            # crude check for rate limit or server error; SDK has specific types but keep generic here
            msg = str(e).lower()
            if any(tok in msg for tok in ["rate limit", "429", "timeout", "temporar", "503", "500"]):
                time.sleep(backoff + random.uniform(0, 0.25))
                backoff = min(backoff_cap, backoff * 2.0)
                continue
            break
    raise last_err if last_err else RuntimeError("OpenAI request failed")


def refine_with_feedback(api_key: str, text: str, heuristics: dict, memory: RefinementMemory, flags: dict = None) -> str:
    if flags:
        print("⏱️  Applying refinement flags:")
        for key in flags:
            # Example: adjust future prompt behavior here based on flags
            # e.g., if key == 'anti_scanner_techniques': modify system message, etc.
            print(f"  - {key}")

    refined = gpt4_multi_pass_refine(api_key, text, heuristics)
    memory.log_pass(text, refined)
    return refined