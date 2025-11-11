import ssl
import random
import warnings

import nltk
import spacy
from nltk.tokenize import word_tokenize
from nltk.corpus import wordnet
from sentence_transformers import SentenceTransformer, util

warnings.filterwarnings("ignore", category=FutureWarning)


def download_nltk_resources() -> None:
    """
    Download required NLTK resources if not already installed.
    Safe to call multiple times.
    """
    try:
        _create_unverified_https_context = ssl._create_unverified_context
    except AttributeError:
        pass
    else:
        ssl._create_default_https_context = _create_unverified_https_context

    resources = [
        "punkt",
        "averaged_perceptron_tagger",
        "punkt_tab",
        "wordnet",
        "averaged_perceptron_tagger_eng",
    ]
    for resource in resources:
        try:
            nltk.download(resource, quiet=True)
        except Exception:
            # Best-effort; downstream calls should still be guarded
            pass


class AcademicTextHumanizer:
    """
    Transforms text to feel more human/academic by:
      - Expanding contractions
      - Adding academic transitions
      - Optional passive-voice conversions
      - Optional synonym substitutions guided by embeddings similarity
    """

    def __init__(
        self,
        model_name: str = "paraphrase-MiniLM-L6-v2",
        p_passive: float = 0.2,
        p_synonym_replacement: float = 0.3,
        p_academic_transition: float = 0.3,
        seed: int | None = None,
    ) -> None:
        if seed is not None:
            random.seed(seed)

        # Load NLP pipeline and embedding model lazily/safel
        
        self.nlp = spacy.load("en_core_web_sm")
        self.model = SentenceTransformer(model_name)

        self.p_passive = p_passive
        self.p_synonym_replacement = p_synonym_replacement
        self.p_academic_transition = p_academic_transition

        self.academic_transitions = [
            "Moreover,",
            "Additionally,",
            "Furthermore,",
            "Hence,",
            "Therefore,",
            "Consequently,",
            "Nonetheless,",
            "Nevertheless,",
        ]

    def humanize_text(self, text: str, use_passive: bool = False, use_synonyms: bool = False) -> str:
        doc = self.nlp(text)
        transformed_sentences: list[str] = []

        for sent in doc.sents:
            sentence_str = sent.text.strip()

            sentence_str = self.expand_contractions(sentence_str)

            if random.random() < self.p_academic_transition:
                sentence_str = self.add_academic_transitions(sentence_str)

            if use_passive and random.random() < self.p_passive:
                sentence_str = self.convert_to_passive(sentence_str)

            if use_synonyms and random.random() < self.p_synonym_replacement:
                sentence_str = self.replace_with_synonyms(sentence_str)

            transformed_sentences.append(sentence_str)

        return " ".join(transformed_sentences)

    def expand_contractions(self, sentence: str) -> str:
        contraction_map = {
            "n't": " not",
            "'re": " are",
            "'s": " is",
            "'ll": " will",
            "'ve": " have",
            "'d": " would",
            "'m": " am",
        }
        tokens = word_tokenize(sentence)
        expanded_tokens: list[str] = []
        for token in tokens:
            lower_token = token.lower()
            replaced = False
            for contraction, expansion in contraction_map.items():
                if contraction in lower_token and lower_token.endswith(contraction):
                    new_token = lower_token.replace(contraction, expansion)
                    if token and token[0].isupper():
                        new_token = new_token.capitalize()
                    expanded_tokens.append(new_token)
                    replaced = True
                    break
            if not replaced:
                expanded_tokens.append(token)

        return " ".join(expanded_tokens)

    def add_academic_transitions(self, sentence: str) -> str:
        transition = random.choice(self.academic_transitions)
        return f"{transition} {sentence}"

    def convert_to_passive(self, sentence: str) -> str:
        doc = self.nlp(sentence)
        subj_tokens = [t for t in doc if t.dep_ == "nsubj" and t.head.dep_ == "ROOT"]
        dobj_tokens = [t for t in doc if t.dep_ == "dobj"]

        if subj_tokens and dobj_tokens:
            subject = subj_tokens[0]
            dobj = dobj_tokens[0]
            verb = subject.head
            if subject.i < verb.i < dobj.i:
                passive_str = f"{dobj.text} {verb.lemma_} by {subject.text}"
                original_str = " ".join(token.text for token in doc)
                chunk = f"{subject.text} {verb.text} {dobj.text}"
                if chunk in original_str:
                    sentence = original_str.replace(chunk, passive_str)
        return sentence

    def replace_with_synonyms(self, sentence: str) -> str:
        tokens = word_tokenize(sentence)
        pos_tags = nltk.pos_tag(tokens)

        new_tokens: list[str] = []
        for (word, pos) in pos_tags:
            if pos.startswith(("J", "N", "V", "R")) and wordnet.synsets(word):
                if random.random() < 0.5:
                    synonyms = self._get_synonyms(word, pos)
                    if synonyms:
                        best_synonym = self._select_closest_synonym(word, synonyms)
                        new_tokens.append(best_synonym if best_synonym else word)
                    else:
                        new_tokens.append(word)
                else:
                    new_tokens.append(word)
            else:
                new_tokens.append(word)

        return " ".join(new_tokens)

    def _get_synonyms(self, word: str, pos: str) -> list[str]:
        wn_pos = None
        if pos.startswith("J"):
            wn_pos = wordnet.ADJ
        elif pos.startswith("N"):
            wn_pos = wordnet.NOUN
        elif pos.startswith("R"):
            wn_pos = wordnet.ADV
        elif pos.startswith("V"):
            wn_pos = wordnet.VERB

        synonyms: set[str] = set()
        for syn in wordnet.synsets(word, pos=wn_pos):
            for lemma in syn.lemmas():
                lemma_name = lemma.name().replace("_", " ")
                if lemma_name.lower() != word.lower():
                    synonyms.add(lemma_name)
        return list(synonyms)

    def _select_closest_synonym(self, original_word: str, synonyms: list[str]) -> str | None:
        if not synonyms:
            return None
        original_emb = self.model.encode(original_word, convert_to_tensor=True)
        synonym_embs = self.model.encode(synonyms, convert_to_tensor=True)
        cos_scores = util.cos_sim(original_emb, synonym_embs)[0]
        max_score_index = cos_scores.argmax().item()
        max_score = cos_scores[max_score_index].item()
        if max_score >= 0.5:
            return synonyms[max_score_index]
        return None







