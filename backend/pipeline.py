from __future__ import annotations


def schema_directives(flags):
    directives = []
    if flags.get("entropy_management"):
        directives.append("Apply entropy suppression. Use rare or less predictable phrasing.")
    if flags.get("macrostructure_analysis"):
        directives.append("Disrupt predictable paragraph structures. Break cadence intentionally.")
    if flags.get("semantic_tone_tuning"):
        directives.append("Introduce ambiguous or wavering tone. Mimic human inconsistency.")
    if flags.get("anti_scanner_techniques"):
        directives.append("Introduce low-confidence structures. Use rhetorical fragments, informal punctuation.")
    if flags.get("formatting_safeguards"):
        directives.append("Preserve all original H1, H2, H3 formatting, spacing, and style markers exactly.")
    return "\n".join(directives)


import re
import random
import difflib
import logging
from typing import Any, Callable, Dict, Iterable, List, Tuple
from .domain import AnnotationSpan
from .logger import get_logger, log_exception, log_performance

# Set up logging
logger = get_logger(__name__)

# Create module-level Random instance for deterministic behavior
_rng = random.Random(42)

def set_random_seed(seed: int) -> None:
    """Set the random seed for reproducible transformations.
    
    Args:
        seed: Random seed value for deterministic behavior
    """
    global _rng
    _rng.seed(seed)  # Re-seed existing instance instead of creating new one

try:
    from nltk.tokenize import sent_tokenize  # type: ignore
    from nltk.corpus import wordnet  # type: ignore
except Exception:
    def sent_tokenize(text: str) -> List[str]:
        return re.split(r"(?<=[.!?])\s+", text.strip()) if text.strip() else []

def _sentences(text: str) -> List[str]:
    """Robust sentence tokenization that handles abbreviations."""
    if not text:
        return []
    
    # Handle common abbreviations that shouldn't end sentences
    # Order matters: longer patterns first to avoid partial matches
    abbrev_patterns = [
        r'\b(?:Ph\.D|M\.D|B\.A|M\.S|U\.S\.A|U\.K)\.',  # Academic titles and countries first
        r'\b(?:Dr|Mr|Mrs|Ms|Prof|Sr|Jr|Inc|Ltd|Corp|Co|St|Mt|Fig|Vol|No|vs|p\.m|a\.m|Ave|Blvd|Dept)\.',  # General abbreviations
        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.',  # Months
        r'\b(?:e\.g|i\.e|etc|et al|ibid)\.',  # Academic terms
        r'\b\d+\.',  # Numbers like "1." or "Fig. 3" (last to avoid conflicts)
    ]
    
    # Process text with position-aware replacement
    processed_text = text
    placeholder_map = {}
    placeholder_counter = 0
    
    # Collect all matches first to avoid position invalidation
    all_matches = []
    for pattern in abbrev_patterns:
        for match in re.finditer(pattern, processed_text, re.IGNORECASE):
            all_matches.append((match.start(), match.end(), match.group(0)))
    
    # Remove overlapping matches (keep first/longest)
    filtered_matches = []
    for start, end, text in sorted(all_matches, key=lambda x: (x[0], -(x[1]-x[0]))):
        # Check if this overlaps with any already-added match
        if not any(m_start <= start < m_end or m_start < end <= m_end 
                   for m_start, m_end, _ in filtered_matches):
            filtered_matches.append((start, end, text))
    
    # Sort matches by position (reverse order) to replace from end to start
    # This prevents position invalidation from earlier replacements
    for start, end, matched_text in sorted(filtered_matches, key=lambda x: x[0], reverse=True):
        placeholder = f"__SENT_ABBREV_{placeholder_counter}__"
        placeholder_map[placeholder] = matched_text
        processed_text = processed_text[:start] + placeholder + processed_text[end:]
        placeholder_counter += 1
    
    # Split on sentence endings
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", processed_text) if s.strip()]
    
    # Restore abbreviations
    for placeholder, original in placeholder_map.items():
        for i, sentence in enumerate(sentences):
            sentences[i] = sentence.replace(placeholder, original)
    
    return sentences

_H1_TAG = "<<<H1>>> "
_H2_TAG = "<<<H2>>> "
_H3_TAG = "<<<H3>>> "
_PBRK   = "<<<PBRK>>>"

# --- Layout Protection ---
def _protect_layout(text: str) -> str:
    lines = text.splitlines()
    out: List[str] = []
    for ln in lines:
        # Skip if already protected
        if ln.startswith(_H1_TAG) or ln.startswith(_H2_TAG) or ln.startswith(_H3_TAG):
            out.append(ln)
        elif ln.startswith("### "):
            out.append(_H3_TAG + ln[4:])
        elif ln.startswith("## "):
            out.append(_H2_TAG + ln[3:])
        elif ln.startswith("# "):
            out.append(_H1_TAG + ln[2:])
        else:
            out.append(ln)
    protected = "\n".join(out)
    protected = re.sub(r"\n\n+", f"\n{_PBRK}\n", protected)
    return protected

def _restore_layout(text: str) -> str:
    t = re.sub(r"\s*%s\s*" % re.escape(_PBRK), "\n\n", text)
    t = t.replace(_H3_TAG, "### ")
    t = t.replace(_H2_TAG, "## ")
    t = t.replace(_H1_TAG, "# ")
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = "\n".join(s.rstrip() for s in t.splitlines())
    return t

# --- Markdown/Code Structure Safeguards (MVP) ---
_CODE_FENCE_TAG = "<<<CODEFENCE>>>"
_TABLE_ROW_TAG = "<<<TABLEROW>>>"
_LIST_ROW_TAG = "<<<LISTROW>>>"
_HEAD_TAG = "<<<HEADLINE>>>"

def protect_markdown_structures(text: str, strict: bool = False) -> tuple[str, dict]:
    """Replace code fences, table lines, and optionally lists/headings with placeholders and return a map for restoration."""
    import re as _re
    t = text or ""
    mapping = {"fences": [], "tables": [], "lists": [], "heads": []}
    # Code fences ```...```
    def repl_fence(m):
        idx = len(mapping["fences"])
        mapping["fences"].append(m.group(0))
        return f"{_CODE_FENCE_TAG}{idx}"
    t = _re.sub(r"```[\s\S]*?```", repl_fence, t)
    # Simple tables: lines with pipes and separators
    lines = t.splitlines()
    new_lines = []
    for ln in lines:
        if '|' in ln and ('---' in ln or ln.strip().startswith('|')):
            idx = len(mapping["tables"])
            mapping["tables"].append(ln)
            new_lines.append(f"{_TABLE_ROW_TAG}{idx}")
        elif strict and _re.match(r"^\s*([\-*+]|\d+[\.)])\s+", ln):
            # bullet/numbered list
            idx = len(mapping["lists"])
            mapping["lists"].append(ln)
            new_lines.append(f"{_LIST_ROW_TAG}{idx}")
        elif strict and _re.match(r"^\s*#{1,6}\s+", ln):
            idx = len(mapping["heads"])
            mapping["heads"].append(ln)
            new_lines.append(f"{_HEAD_TAG}{idx}")
        else:
            new_lines.append(ln)
    return "\n".join(new_lines), mapping

def restore_markdown_structures(text: str, mapping: dict) -> str:
    import re as _re
    t = text or ""
    def rep_f(m):
        try:
            return mapping["fences"][int(m.group(1))]
        except Exception:
            return m.group(0)
    def rep_tr(m):
        try:
            return mapping["tables"][int(m.group(1))]
        except Exception:
            return m.group(0)
    def rep_lr(m):
        try:
            return mapping["lists"][int(m.group(1))]
        except Exception:
            return m.group(0)
    def rep_hd(m):
        try:
            return mapping["heads"][int(m.group(1))]
        except Exception:
            return m.group(0)
    t = _re.sub(rf"{_CODE_FENCE_TAG}(\d+)", rep_f, t)
    t = _re.sub(rf"{_TABLE_ROW_TAG}(\d+)", rep_tr, t)
    t = _re.sub(rf"{_LIST_ROW_TAG}(\d+)", rep_lr, t)
    t = _re.sub(rf"{_HEAD_TAG}(\d+)", rep_hd, t)
    return t

def validate_markdown_structures(text: str, mapping: dict = None) -> dict:
    """Enhanced validators for fences, headers, and placeholder integrity. Returns diagnostics counts."""
    import re as _re
    t = text or ""
    diags = {"open_fences": 0, "header_like": 0, "missing_placeholders": 0}
    
    # Check fence balance
    diags["open_fences"] = (t.count("```") % 2)
    
    # Check empty headers
    for ln in t.splitlines():
        if _re.match(r"^#{1,6}\s*$", ln):
            diags["header_like"] += 1
    
    # If mapping provided, check placeholder integrity
    if mapping:
        for key, items in mapping.items():
            expected_count = len(items)
            tag = {"fences": _CODE_FENCE_TAG, "tables": _TABLE_ROW_TAG, 
                   "lists": _LIST_ROW_TAG, "heads": _HEAD_TAG}.get(key, "")
            if tag:
                actual_count = len(_re.findall(rf"{_re.escape(tag)}\d+", t))
                if actual_count != expected_count:
                    diags["missing_placeholders"] += abs(actual_count - expected_count)
    
    return diags

def _calculate_diff_ratio(before: str, after: str) -> float:
    """Calculate the difference ratio between two texts.
    
    Returns:
        float: 0.0 = identical, 1.0 = completely different
        This represents the magnitude of change (higher = more change)
    """
    return 1.0 - difflib.SequenceMatcher(None, before, after).ratio()

# --- Noise & Transformations ---
def _delete_random_commas(text: str, severity: str = "medium") -> str:
    max_deletions = {"low": 1, "medium": 2, "high": 3}.get(severity, 2)
    lines = text.splitlines()
    out = []
    for line in lines:
        commas = [m.start() for m in re.finditer(r",", line)]
        if commas:
            count = min(max_deletions, len(commas))
            to_remove = _rng.sample(commas, k=count)
            newline = ''.join(c for i, c in enumerate(line) if i not in to_remove)
            out.append(newline)
        else:
            out.append(line)
    return "\n".join(out)

def _random_clause_shuffler(text: str, probability: float = 0.2) -> str:
    paragraphs = text.split("\n\n")
    new_paras = []
    for p in paragraphs:
        if _rng.random() > probability:
            new_paras.append(p)
            continue
        sents = sent_tokenize(p)
        shuffled = []
        for s in sents:
            parts = re.split(r"([,;:])", s)
            if len(parts) > 3:
                idx = _rng.randint(1, len(parts) - 2)
                shuffled.append(''.join(parts[idx:] + parts[:idx]))
            else:
                shuffled.append(s)
        new_paras.append(" ".join(shuffled))
    return "\n\n".join(new_paras)

def _inject_noise_phrases(text: str, probability: float = 0.1) -> str:
    fillers = ["actually", "in a way", "sort of", "almost", "kind of"]
    sents = sent_tokenize(text)
    result = []
    for s in sents:
        if _rng.random() < probability:
            words = s.split()
            if len(words) > 2:
                insert_pos = _rng.randint(1, len(words) - 2)
                words.insert(insert_pos, _rng.choice(fillers))
                s = " ".join(words)
        result.append(s)
    return " ".join(result)

def _modulate_sentence_lengths(text: str) -> str:
    sents = sent_tokenize(text)
    output = []
    i = 0
    while i < len(sents):
        if len(sents[i].split()) < 8 and i+1 < len(sents):
            combined = sents[i] + ", " + sents[i+1]
            output.append(combined)
            i += 2
        else:
            output.append(sents[i])
            i += 1
    return " ".join(output)

def _vary_sentence_starts(text: str) -> str:
    openers = ["Sometimes", "That year", "Often", "Back then", "Even now"]
    sents = sent_tokenize(text)
    recent_starts = []
    result = []
    for s in sents:
        first = s.split()[0].lower() if s else ""
        if first in recent_starts:
            if _rng.random() < 0.5:
                s = _rng.choice(openers) + ", " + s[0].lower() + s[1:]
        recent_starts.append(first)
        if len(recent_starts) > 5:
            recent_starts.pop(0)
        result.append(s)
    return " ".join(result)

def _replace_long_words_with_synonyms(text: str, min_length: int = 8, probability: float = 0.1) -> str:
    try:
        from nltk.corpus import wordnet
    except ImportError:
        # Fallback if WordNet not available
        return text
    
    words = text.split()
    new_words = []
    for w in words:
        if len(w) >= min_length and _rng.random() < probability:
            try:
                syns = wordnet.synsets(w)
                if syns:
                    # Get first synset (most common meaning)
                    first_synset = syns[0]
                    lemmas = [l.name().replace('_', ' ') for l in first_synset.lemmas()]
                    # Filter out the original word and find a suitable replacement
                    alternatives = [l for l in lemmas if l.lower() != w.lower() and len(l.split()) == 1]
                    if alternatives:
                        alt = alternatives[0]
                        new_words.append(alt)
                    else:
                        new_words.append(w)
                else:
                    new_words.append(w)
            except Exception:
                # If anything fails, keep original word
                new_words.append(w)
        else:
            new_words.append(w)
    return ' '.join(new_words)

# --- Anti-detector stylometry shifts ---
_GENERIC_TRANSITIONS = [
    "Additionally,", "Furthermore,", "Moreover,", "In addition,",
    "Therefore,", "Consequently,", "Nonetheless,", "Nevertheless,",
]

def _strip_llm_style_transitions(text: str) -> str:
    pattern = r"\b(?:" + "|".join(map(re.escape, _GENERIC_TRANSITIONS)) + r")\s*"
    return re.sub(pattern, "", text)

def _introduce_contractions(text: str, probability: float = 0.25) -> str:
    repl = {
        " do not ": " don't ",
        " does not ": " doesn't ",
        " did not ": " didn't ",
        " cannot ": " can't ",
        " can not ": " can't ",
        " will not ": " won't ",
        " is not ": " isn't ",
        " are not ": " aren't ",
        " was not ": " wasn't ",
        " were not ": " weren't ",
        " have not ": " haven't ",
        " has not ": " hasn't ",
        " had not ": " hadn't ",
        " I am ": " I'm ",
        " we are ": " we're ",
        " they are ": " they're ",
        " it is ": " it's ",
    }
    out = text
    for k, v in repl.items():
        if _rng.random() < probability:
            out = re.sub(re.escape(k), v, out, flags=re.IGNORECASE)
    return out

def _inject_hedges_and_idioms(text: str, probability: float = 0.15) -> str:
    hedges = [
        "to be fair", "if I'm honest", "in my experience", "to some degree",
        "for what it's worth", "oddly enough", "now and then",
    ]
    sents = sent_tokenize(text)
    out = []
    for s in sents:
        if _rng.random() < probability and len(s.split()) > 6:
            prefix = _rng.choice(hedges)
            s = f"{prefix}, " + s[0].lower() + s[1:]
        out.append(s)
    return " ".join(out)

def _vary_punctuation_and_rhythm(text: str, probability: float = 0.2) -> str:
    # Occasionally replace a comma or period with an em dash or ellipsis
    chars = list(text)
    for i, ch in enumerate(chars):
        if ch in [',', '.'] and _rng.random() < probability:
            chars[i] = '—' if ch == ',' else '…'
    return ''.join(chars)

def _human_typo_variants(text: str, probability: float = 0.05) -> str:
    # Introduce rare, benign human-like variations that won't harm meaning
    variants = [
        (r"\b(OK|Ok|ok)\b", "okay"),
        (r"\b%\b", " percent"),
        (r"\b&\b", " and "),
    ]
    out = text
    for pat, rep in variants:
        if _rng.random() < probability:
            out = re.sub(pat, rep, out)
    return out

def _numeric_style_variation(text: str, probability: float = 0.15) -> str:
    # Toggle between digit and word for small numbers occasionally
    num_map = {
        " one ": " 1 ", " two ": " 2 ", " three ": " 3 ", " four ": " 4 ",
        " five ": " 5 ", " six ": " 6 ", " seven ": " 7 ", " eight ": " 8 ",
        " nine ": " 9 ",
    }
    rev_map = {v: k for k, v in num_map.items()}
    out = text
    if _rng.random() < probability:
        for k, v in (num_map.items() if _rng.random() < 0.5 else rev_map.items()):
            out = re.sub(re.escape(k), v, out)
    return out

def _fragment_some_sentences(text: str, probability: float = 0.12) -> str:
    # Transform some sentences into short fragments to disrupt uniformity
    sents = sent_tokenize(text)
    out = []
    for s in sents:
        if _rng.random() < probability and len(s.split()) > 8:
            parts = s.split()
            cut = max(3, int(len(parts) * 0.4))
            out.append(' '.join(parts[:cut]).rstrip(',.;:') + '.')
        else:
            out.append(s)
    return ' '.join(out)

# --- Optional post-pass helpers ---
def _paragraph_restorer(text: str, sentences_per_para: int = 4) -> str:
    sents = _sentences(text)
    paras = []
    buf = []
    for i, s in enumerate(sents):
        buf.append(s)
        if (i + 1) % max(1, sentences_per_para) == 0:
            paras.append(' '.join(buf))
            buf = []
    if buf:
        paras.append(' '.join(buf))
    return '\n\n'.join(paras)

def _humanizer_filter(text: str) -> str:
    t = re.sub(r"\bThis means that\b", "", text)
    t = re.sub(r"\bIt is important to\b", "", t)
    return re.sub(r"\s+", " ", t).strip()

def _humanize_academic(
    text: str,
    intensity: str = 'light',
    target_avg_len: int = 20,
    max_joins: int = 3,
    max_splits: int = 3,
    idiom_cap: int = 2,
    keywords: list | None = None,
) -> str:
    import re as _re
    if not text:
        return text
    low_intensity = {'light': 0.3, 'medium': 0.6, 'strong': 0.9}.get((intensity or 'light').lower(), 0.3)
    keywords = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]

    # Use the module-level _sentences function for consistent abbreviation handling
    def _words(u: str):
        return _re.findall(r"[A-Za-z0-9']+", (u or ''))
    def _protect_regions(u: str):
        # Mask citations like [12], (Smith, 2020), inline code, and bracketed refs
        masks = []
        def _mask(pattern):
            nonlocal u
            def repl(m):
                idx = len(masks)
                masks.append(m.group(0))
                return f"<<<HUMA_MASK_{idx}>>>"
            u = _re.sub(pattern, repl, u)
        
        # Apply patterns in order of specificity (most specific first)
        _mask(r"`[^`]*`")  # Inline code first (most specific)
        _mask(r"\[[0-9,;\s]+\]")  # Citations
        _mask(r"\([A-Za-z][^)]{0,80}\d{2,4}[^)]*\)")  # Author-year citations
        _mask(r"\b(?:19|20)\d{2}\b")  # Years (more restrictive: 1900-2099)
        
        return u, masks
    def _unprotect(u: str, masks: list[str]):
        for i, val in enumerate(masks):
            u = u.replace(f"<<<HUMA_MASK_{i}>>>", val)
        return u

    t, masks = _protect_regions(text)

    # 1) Sentence joins/splits toward target average
    sents = _sentences(t)
    out_s = []
    joins = 0
    splits = 0
    for s in sents:
        wl = len(_words(s))
        if wl < max(6, target_avg_len - 8) and joins < max_joins:
            # join with next if exists by deferring join; handled after loop by merging short sequences
            out_s.append((s, 'maybe_join'))
        elif wl > target_avg_len + 10 and splits < max_splits:
            parts = _re.split(r"([,;:])", s)
            if len(parts) > 3:
                mid = len(parts)//2
                a = ''.join(parts[:mid]).strip().rstrip(',;:') + '.'
                b = ''.join(parts[mid:]).strip().lstrip(',;:')
                if b and not _re.search(r"[.!?]$", b):
                    b += '.'
                out_s.append((a, 'ok'))
                out_s.append((b, 'ok'))
                splits += 1
            else:
                out_s.append((s, 'ok'))
        else:
            out_s.append((s, 'ok'))
    # realize joins by concatenating short neighbors
    realized = []
    i = 0
    while i < len(out_s):
        s, tag = out_s[i]
        if tag == 'maybe_join' and i+1 < len(out_s) and joins < max_joins:
            s2, _ = out_s[i+1]
            if len(_words(s)) + len(_words(s2)) <= target_avg_len + 8:
                realized.append(s.rstrip(',;:') + ' ' + s2[0].lower() + s2[1:])
                joins += 1
                i += 2
                continue
        realized.append(s)
        i += 1
    t = ' '.join(realized)

    # 2) Connective variety (insert subtle transitions at paragraph or sentence starts)
    transitions = [
        'However', 'Moreover', 'Furthermore', 'In addition', 'By contrast', 'For example', 'As a result'
    ]
    if low_intensity >= 0.3:
        lines = t.split('\n\n')
        new_paras = []
        for idx, para in enumerate(lines):
            sents = _sentences(para)
            enhanced = []
            for j, s in enumerate(sents):
                if j == 0 and idx > 0 and low_intensity >= 0.6 and not _re.match(r"^(?:However|Moreover|Furthermore|In addition|By contrast|For example|As a result)\b", s):
                    enhanced.append(transitions[(idx + j) % len(transitions)] + ', ' + s[0].lower() + s[1:])
                else:
                    enhanced.append(s)
            new_paras.append(' '.join(enhanced))
        t = '\n\n'.join(new_paras)

    # 3) Passive→active conversion removed - was creating sentence fragments
    # Full passive-to-active conversion requires complex syntactic analysis
    # and is beyond the scope of simple text processing

    # 4) Idiom insertion with guards (cap)
    idioms = ["to be fair", "for what it's worth", "oddly enough", "now and then"]
    added = 0
    if low_intensity >= 0.6 and idiom_cap > 0:
        sents = _sentences(t)
        out_s2 = []
        for s in sents:
            if added < idiom_cap and len(_words(s)) > 10 and not _re.search(r"\[[0-9,;\s]+\]", s):
                # avoid headings or code-like lines
                if not _re.match(r"^#{1,6}\s|^\s*```", s):
                    ins = idioms[(added) % len(idioms)]
                    s = f"{ins}, " + s[0].lower() + s[1:]
                    added += 1
            out_s2.append(s)
        t = ' '.join(out_s2)

    # 5) Number/citation handling: ensure spaces around percentages and keep brackets intact
    t = _re.sub(r"(\d)%(\b)", r"\1 percent\2", t)

    t = _unprotect(t, masks)
    return t

def _keyword_integrity_checker(text: str, keywords, max_repeats: int = 2) -> str:
    if not keywords:
        return text
    out = text
    for kw in keywords:
        if not kw:
            continue
        pattern = re.escape(kw)
        hits = re.findall(pattern, out, flags=re.IGNORECASE)
        if len(hits) > max_repeats:
            # remove extras case-insensitively - fix stateful closure
            class _Replacer:
                def __init__(self, max_repeats):
                    self.count = 0
                    self.max_repeats = max_repeats
                
                def __call__(self, match):
                    self.count += 1
                    return match.group(0) if self.count <= self.max_repeats else ''
            
            replacer = _Replacer(max_repeats)
            out = re.sub(pattern, replacer, out, flags=re.IGNORECASE)
    return out

# --- Deterministic schema modules ---
def _macrostructure_det(text: str) -> str:
    """Insert simple headings, merge redundant adjacent paragraphs, ensure separators at topic shifts.
    Conservative and reversible; avoids touching fenced code and tables.
    """
    import re as _re
    paras = [p for p in (text or '').split('\n\n')]
    out = []
    last = ''
    for p in paras:
        # Skip empty chunks
        if not p.strip():
            out.append(p)
            continue
        # Merge with previous if highly similar
        if last and _re.sub(r"\W+", " ", last.lower()).strip() and _re.sub(r"\W+", " ", p.lower()).strip():
            from difflib import SequenceMatcher as _SM
            if _SM(None, last, p).ratio() > 0.92:
                last = last + ' ' + p
                if out:
                    out[-1] = last
                else:
                    out.append(last)
                continue
        # Insert heading when paragraph is long and not headed
        first_line = p.strip().splitlines()[0]
        if not _re.match(r"^#{1,3}\s|^\s*([\-\*\d]+[\.)])\s+", first_line) and len(p.split()) > 120:
            heading = first_line.split('.')[0][:60]
            p = f"## {heading}\n\n" + p
        out.append(p)
        last = p
    return '\n\n'.join(out)

def _microstructure_det(text: str) -> str:
    """Shape rhythm and variety: avoid repeated sentence starters, cap long sentences by soft splits."""
    import re as _re
    # Use the module-level _sentences function for consistent abbreviation handling
    sents = _sentences(text)
    if not sents:
        return text
    openers = ["Sometimes", "Often", "That said", "By contrast", "For example"]
    recent = []
    out = []
    for s in sents:
        first = (s.split()[:1] or [''])[0].lower()
        t = s
        if first in recent:
            t = openers[len(out) % len(openers)] + ", " + s[0].lower() + s[1:]
        if len(t.split()) > 34:
            parts = _re.split(r"([,;:])", t)
            if len(parts) > 3:
                mid = len(parts)//2
                a = ''.join(parts[:mid]).strip().rstrip(',;:') + '.'
                b = ''.join(parts[mid:]).strip().lstrip(',;:')
                if b and not _re.search(r"[.!?]$", b):
                    b += '.'
                out.extend([a, b])
            else:
                out.append(t)
        else:
            out.append(t)
        recent.append(first)
        if len(recent) > 4:
            recent.pop(0)
    return ' '.join(out)

def _strategy_insight_det(text: str) -> str:
    """Light-weight clarity/persuasion/brevity balance: trim filler, promote action verbs, remove clichés."""
    import re as _re
    t = text or ''
    # Remove common filler/clichés
    fillers = [
        'at the end of the day','needless to say','it goes without saying','in today\'s world',
        'the fact of the matter','paradigm shift','think outside the box'
    ]
    for f in fillers:
        t = _re.sub(rf"\b{_re.escape(f)}\b", '', t, flags=_re.IGNORECASE)
    # Prefer concise modals
    t = _re.sub(r"\bwould be able to\b", 'could', t, flags=_re.IGNORECASE)
    t = _re.sub(r"\bit is important to\b", '', t, flags=_re.IGNORECASE)
    # Softly trim trailing verbosity
    t = _re.sub(r"\s+", ' ', t)
    return t.strip()

# --- Transform Profiles ---
TRANSFORM_PROFILES = {
    "low": [
        # Low entropy: only deterministic, non-destructive transforms
        _strip_llm_style_transitions,
        _strategy_insight_det,  # Deterministic, removes clichés
        _humanizer_filter,      # Deterministic, removes filler
    ],
    "medium": [
        _delete_random_commas,
        _random_clause_shuffler,
        _strip_llm_style_transitions,
        _introduce_contractions,
        _inject_hedges_and_idioms,
    ],
    "high": [
        _delete_random_commas,
        _random_clause_shuffler,
        _inject_noise_phrases,
        _vary_sentence_starts,
        _strip_llm_style_transitions,
        _introduce_contractions,
        _inject_hedges_and_idioms,
        _vary_punctuation_and_rhythm,
        _numeric_style_variation,
        _fragment_some_sentences,
    ],
    "very_high": [
        _delete_random_commas,
        _strip_llm_style_transitions,
        _random_clause_shuffler,
        _modulate_sentence_lengths,
        _vary_sentence_starts,
        _introduce_contractions,
        _inject_noise_phrases,
        _inject_hedges_and_idioms,
        _vary_punctuation_and_rhythm,
        _replace_long_words_with_synonyms,  # Only in very_high (needs WordNet)
        _human_typo_variants,
        _numeric_style_variation,
        _fragment_some_sentences,
    ],
}

# --- Core Refinement Pass ---
def stealth_prep_pipeline(text: str, transforms: Iterable[Callable[[str], str]], heuristics: Dict[str, Any], entropy_level: str = "medium") -> Tuple[str, List[str], List[float]]:
    applied: List[str] = []
    weights: List[float] = []
    
    # Protect both layout and markdown structures before transforms
    t = _protect_layout(text)
    
    # Protect markdown structures (code fences, tables, lists, headers)
    markdown_mapping = None  # Initialize to avoid UnboundLocalError
    try:
        fs_cfg = (heuristics.get('formatting_safeguards') or {}) if isinstance(heuristics, dict) else {}
        fs_mode = (fs_cfg.get('mode') if isinstance(fs_cfg, dict) else 'smart') or 'smart'
        if fs_cfg is True or (isinstance(fs_cfg, dict) and fs_cfg.get('enabled', True)):
            t, markdown_mapping = protect_markdown_structures(t, strict=(fs_mode == 'strict'))
    except Exception as e:
        # If markdown protection fails, continue without it
        logger.warning(f"Markdown protection failed: {e}")
        markdown_mapping = None
    
    for fn in TRANSFORM_PROFILES.get(entropy_level, []):
        backup = t  # Backup before each transform
        try:
            t2 = fn(t)
            # Validate transform result
            if not isinstance(t2, str):
                raise ValueError(f"Transform {getattr(fn, '__name__', 'unknown')} returned non-string: {type(t2)}")
            
            # Check for catastrophic corruption (adaptive thresholds based on text size)
            length_change = abs(len(t2) - len(t))
            if len(t) < 1000:
                # Small text: strict percentage check
                if len(t2) < len(t) * 0.5 or len(t2) > len(t) * 2.0:
                    raise ValueError(f"Transform {getattr(fn, '__name__', 'unknown')} caused excessive length change in small text: {len(t)} → {len(t2)} (change: {length_change})")
            else:
                # Large text: require both large absolute AND percentage change
                if length_change > 1000 and (len(t2) < len(t) * 0.3 or len(t2) > len(t) * 3.0):
                    raise ValueError(f"Transform {getattr(fn, '__name__', 'unknown')} caused excessive length change in large text: {len(t)} → {len(t2)} (change: {length_change})")
            
            # Validate markdown structures if protection was applied
            if markdown_mapping:
                diags = validate_markdown_structures(t2, markdown_mapping)
                if any(diags.values()):
                    logger.warning(f"Transform {getattr(fn, '__name__', 'unknown')} corrupted markdown: {diags}, rolling back")
                    t = backup  # Rollback to pre-transform state
                    continue  # Skip this transform and try the next one
            
            if t2 != t:
                applied.append(getattr(fn, "__name__", "transform"))
                weights.append(_calculate_diff_ratio(t, t2))
            t = t2
            
        except Exception as e:
            # Rollback on error
            t = backup
            # Log error but continue with other transforms
            logger.error(f"Transform {getattr(fn, '__name__', 'unknown')} failed: {e}")
    
    # Restore markdown structures if they were protected
    if markdown_mapping:
        try:
            t = restore_markdown_structures(t, markdown_mapping)
            # Validate restoration was successful
            if _CODE_FENCE_TAG in t or _TABLE_ROW_TAG in t or _LIST_ROW_TAG in t or _HEAD_TAG in t:
                raise ValueError("Restoration incomplete: placeholders still present")
        except Exception as e:
            logger.error(f"Markdown restoration failed: {e}, rolling back to original")
            t = _protect_layout(text)  # Start over
    
    t = _restore_layout(t)
    return t, applied, weights

def post_pass_adjustments(text: str, heuristics: Dict[str, Any]) -> str:
    """Apply post-processing adjustments based on heuristics.
    
    Args:
        text: The text to process (output from main pipeline)
        heuristics: Configuration dictionary with the following structure:
            {
                'schema_flags': {
                    'macrostructure_analysis': bool,
                    'microstructure_control': bool,
                    'strategy_insight': bool
                },
                'humanize_academic': {
                    'enabled': bool,
                    'intensity': 'light'|'medium'|'strong',
                    'target_avg_sentence_len': int,
                    'max_joins': int,
                    'max_splits': int,
                    'idiom_cap': int
                },
                'keywords': List[str],
                'post_paragraph_restorer': bool,
                'post_sentences_per_paragraph': int,
                'post_humanizer_filter': bool,
                'post_keyword_cap': bool,
                'post_keyword_max_repeats': int
            }
    
    Returns:
        Processed text with newline at end
    """
    # Validate heuristics structure
    if not isinstance(heuristics, dict):
        heuristics = {}
    
    # Save input (output from main pipeline) for validation baseline
    input_text = text
    text = re.sub(r"\n{3,}", "\n\n", text)
    last_valid = text  # Track last successful state for partial rollback
    
    try:
        # Deterministic schema modules (enabled via heuristics.schema_flags)
        sf = heuristics.get('schema_flags', {})
        if isinstance(sf, dict):
            if sf.get('macrostructure_analysis'):
                text = _macrostructure_det(text)
                last_valid = text  # Update checkpoint
            if sf.get('microstructure_control'):
                text = _microstructure_det(text)
                last_valid = text  # Update checkpoint
            if sf.get('strategy_insight'):
                text = _strategy_insight_det(text)
                last_valid = text  # Update checkpoint
        
        # Humanize Academic (stronger module)
        huma_cfg = heuristics.get('humanize_academic', {})
        huma_enabled = (huma_cfg is True) or (isinstance(huma_cfg, dict) and huma_cfg.get('enabled', False))
        if huma_enabled and isinstance(huma_cfg, dict):
            text = _humanize_academic(
                text,
                intensity=huma_cfg.get('intensity', 'light'),
                target_avg_len=int(huma_cfg.get('target_avg_sentence_len', 20)),
                max_joins=int(huma_cfg.get('max_joins', 3)),
                max_splits=int(huma_cfg.get('max_splits', 3)),
                idiom_cap=int(huma_cfg.get('idiom_cap', 2)),
                keywords=heuristics.get('keywords', []) or [],
            )
            last_valid = text  # Update checkpoint
        
        # Post-processing steps
        if heuristics.get('post_paragraph_restorer'):
            sentences_per_para = int(heuristics.get('post_sentences_per_paragraph', 4))
            text = _paragraph_restorer(text, sentences_per_para=sentences_per_para)
            last_valid = text  # Update checkpoint
        
        if heuristics.get('post_humanizer_filter'):
            text = _humanizer_filter(text)
            last_valid = text  # Update checkpoint
        
        if heuristics.get('post_keyword_cap'):
            keywords = heuristics.get('keywords', []) or []
            max_repeats = int(heuristics.get('post_keyword_max_repeats', 2))
            text = _keyword_integrity_checker(text, keywords, max_repeats=max_repeats)
            last_valid = text  # Update checkpoint
            
    except Exception as e:
        # Log error and rollback to last successful state (preserve partial progress)
        logger.error(f"Post-pass adjustment error: {e}")
        text = last_valid
    
    # Final validation: check for catastrophic corruption (compare to input, not original document)
    if len(text) < len(input_text) * 0.5 or len(text) > len(input_text) * 2.0:
        logger.warning("Post-pass adjustments caused excessive length change, rolling back to pipeline output")
        text = input_text  # Rollback to pipeline output, not start of document
    
    if not text.endswith("\n"):
        text += "\n"
    return text

def adapt_entropy_level(score_delta: float) -> str:
    if score_delta < 1.0:
        return "high"
    elif score_delta < 3.0:
        return "medium"
    return "low"

# --- Annotation helpers (MVP) ---
def generate_sidecar_annotations(before: str, after: str, verbosity: str = "low") -> List[AnnotationSpan]:
    import difflib as _dif
    a = before or ""
    b = after or ""
    sm = _dif.SequenceMatcher(None, a, b)
    anns: List[AnnotationSpan] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        rationale = {
            'replace': 'Rephrased for clarity and style.',
            'insert': 'Added connective/context for flow.',
            'delete': 'Removed redundancy to improve brevity.',
        }.get(tag, 'Adjusted for quality.')
        cat = 'clarity' if tag == 'replace' else 'brevity' if tag == 'delete' else 'structure'
        if verbosity == 'low' and tag == 'replace':
            # coalesce small edits by skipping tiny spans
            if (i2 - i1) + (j2 - j1) < 6:
                continue
        anns.append(AnnotationSpan(start=j1, end=j2, rationale=rationale, category=cat))
    return anns

def inject_inline_annotations(text: str, anns: List[AnnotationSpan], verbosity: str = "low") -> str:
    if not anns:
        return text
    out = []
    last = 0
    for sp in sorted(anns, key=lambda s: s.start):
        out.append(text[last:sp.start])
        segment = text[sp.start:sp.end]
        note = f"[^{sp.category}:{sp.rationale}]" if verbosity != 'high' else f"[^{sp.category}:{sp.rationale}]"
        out.append(segment + note)
        last = sp.end
    out.append(text[last:])
    return ''.join(out)

__all__ = [
    "stealth_prep_pipeline",
    "post_pass_adjustments",
    "adapt_entropy_level",
    "protect_markdown_structures",
    "restore_markdown_structures",
    "validate_markdown_structures",
    "generate_sidecar_annotations",
    "inject_inline_annotations",
    "schema_directives",
    "set_random_seed",  # Added for seed control
    "_sentences",  # Made available as it's used in other modules
]