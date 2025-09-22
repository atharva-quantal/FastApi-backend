
import os
import re
import json
import google.generativeai as genai
from rank_bm25 import BM25Okapi


def simple_tokenize(text):
    return re.findall(r"\b\w+\b", text.lower())


API_KEY = os.getenv("GEMINI_KEY")
genai.configure(api_key=API_KEY)

PRICE_PER_INPUT_TOKEN = 0.35 / 1_000_000
PRICE_PER_OUTPUT_TOKEN = 1.05 / 1_000_000


def build_bm25_index(shopify_data):
    gids = list(shopify_data.keys())
    docs = list(shopify_data.values())
    tokenized_docs = [simple_tokenize(str(doc)) for doc in docs]
    bm25 = BM25Okapi(tokenized_docs)
    return bm25, docs, gids

def search_with_bm25(query, bm25, docs, gids, top_n=5):  
    tokenized_query = simple_tokenize(query)
    scores = bm25.get_scores(tokenized_query)

    max_score = max(scores) if max(scores) > 0 else 1
    norm_scores = [s / max_score for s in scores]

    ranked = sorted(list(enumerate(norm_scores)), key=lambda x: x[1], reverse=True)[:top_n]

    return [{"gid": gids[idx], "text": docs[idx], "score": round(score, 3)} for idx, score in ranked]


# ---------------------------
# Gemini Validation
# ----------------------------
def validate_with_gemini(filename, bm25_candidates, model_name="gemini-1.5-flash"):
    """
    Validates BM25 candidates using Gemini LLM.
    Returns top-3 candidates with 'gid', 'text', 'score', 'reason'.
    If all scores <= 5, still returns top-3 but flags NHR.
    """
    if not bm25_candidates:
        print("No BM25 candidates to validate.")
        return []

    model = genai.GenerativeModel(model_name=model_name)

    cand_lines = []
    for c in bm25_candidates:
        cand_lines.append(f"- GID: {c['gid']}\n  Title: {c['text']}\n  BM25 Score: {c['score']:.3f}")

    aoc_rules = """
AOC Naming Rules – Memory Sheet (scoring + matching rules)

Normalization & Matching:
- Remove accents/diacritics; collapse multiple spaces; ignore leading/trailing spaces.
- Matching is TEXT-BASED ONLY: do NOT use internal wine-encyclopedic knowledge (no sub-appellation, no region inference).
- Allow minor spelling: treat a token as matched if it is identical after normalization or differs by at most one character (single-character typo).
- Field tokens to consider: Vintage, Producer, Appellation, Classification, Cru, Vineyard, Style/Variety, Format.

Scoring system (deterministic):
- Initialize score = 10 for each candidate.
- For each of the following fields that matches → add -2 points:
    1) Vintage (counts only if both extracted label and candidate contain a vintage and they match; see vintage rules)
    2) Producer
    3) Appellation
    4) Classification
    5) Cru
    6) Vineyard
    7) Style/Variety
    8) Format

** Vintage Rules ** 
- Vintage comparison rule (strict):
- If both extracted vintage and database vintage have 4 digits , compare them literally as 4-digit numbers.If Vintage has 2 digits match them logically ,Example 21 matches 2021 and 78 matches 1978    
- If they are identical, mark as "Vintage match" and DO NOT subtract points.  
- Do not state "vintage mismatch" if the values are the same.
- If one side has no vintage, apply the missing-vintage rule instead.
- If the candidate does NOT contain a vintage, vintage contributes +0 (no penalty).
- If BOTH extracted label AND candidate contain a vintage but the vintages DO NOT match (account for 2-digit ↔ 4-digit equivalence, e.g., "21" ↔ "2021"), then apply a vintage penalty of -5 **to the computed score** (i.e., after adding +2's).
- After computing, clamp the final score to the integer range 0..10. **Scores must be whole integers only — do not return decimals.**
- The caller will use a selection threshold (e.g., 7); still return the computed integer score even if below the threshold.

Strict rules to avoid false positives:
- If a candidate shares only generic terms (e.g., "Chateau", "Bordeaux", "Red Wine", "Grand Cru Classe") but **does not** match Producer AND Appellation (subject to minor spelling allowance), that candidate must not achieve a high score via partial overlap alone — it will only gain points for the actual fields matched according to the +2 rule. Do not award points for implied or hierarchical relationships.
- If the candidate lacks vintage, it may still reach a high score, but only by matching all other relevant fields precisely (subject to minor spelling tolerance).

Output & reasons:
- For each candidate, produce a compact one-line reason that:
    * lists which fields matched (e.g., "Matched: Producer, Appellation, Classification")
    * notes if vintage was missing or if a -5 vintage penalty was applied (e.g., "Vintage mismatch: 2021 vs 2007 → -5")
    * ends with the final integer score summary.
"""




    prompt = f"""
You are a wine-label matching assistant.

Filename (extracted): "{filename}"
BM25 top candidates (up to 5) follow:
{chr(10).join(cand_lines)}

Rules (AOC + scoring): {aoc_rules}

Task:
For each candidate:
1. Normalize both texts (remove diacritics, collapse spaces).
2. Determine whether each of these fields matches (allow 1-character typo):
   Vintage, Producer, Appellation, Classification, Cru, Vineyard, Style/Variety, Format.
   - Vintage matching may consider 2-digit vs 4-digit equivalence (e.g., '21' ↔ '2021').
3. Compute score:
   - Start at 0.
   - For each matched field add +2.
   - If both sides have vintages and vintages do NOT match, subtract 5 from the computed score.
   - Clamp the final score to the integer range 0..10. Only return whole integers (0,1,...,10). No decimals.
4. Build a one-line reason listing matched fields and any vintage penalty.
5. Return a JSON array (top-3 candidates by this final integer score, highest first) where each element is:
   {{
     "gid": "<candidate gid>",
     "text": "<candidate product title>",
     "score": <integer 0-10>,
     "reason": "<one-line reason describing which fields matched and any penalty>"
   }}

Important:
- Do NOT use outside knowledge (no sub-appellation inference). Base all matches on direct text comparisons only.
- Do not output anything other than the JSON array.

Example Scoring Walkthroughs:
- Extracted = "2019 Chateau Test Pauillac Grand Cru Classe"
- Candidate = "2019 Chateau Test Pauillac Grand Cru Classe"
  * Vintage +2, Producer +2, Appellation +2, Classification +2 → total 8
  * No mismatch → final score = 8
- Extracted = "2019 Chateau Test Pauillac Grand Cru Classe"
- Candidate = "2007 Chateau Test Pauillac Grand Cru Classe"
  * Producer +2, Appellation +2, Classification +2 = 6
  * Vintage mismatch (2019 vs 2007) = -5
  * Final = 1
- Extracted = "2021 Domaine Example Chardonnay"
- Candidate = "Domaine Example Chardonnay"
  * Producer +2, Style/Variety +2 = 4
  * Candidate has no vintage → +0 (no penalty)
  * Final = 4
"""




    response = model.generate_content(prompt)

    try:
        input_tokens = response.usage_metadata.prompt_token_count
        output_tokens = response.usage_metadata.candidates_token_count
        total_tokens = response.usage_metadata.total_token_count
        cost = (input_tokens * PRICE_PER_INPUT_TOKEN) + (output_tokens * PRICE_PER_OUTPUT_TOKEN)
        print(f"    - Gemini Usage: {total_tokens} tokens | Cost: ${cost:,.6f}")
    except (AttributeError, KeyError):
        pass

    text = response.text.strip()

    try:
        gemini_candidates = json.loads(text)
    except Exception:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            try:
                gemini_candidates = json.loads(text[start:end+1])
            except Exception:
                gemini_candidates = []
        else:
            gemini_candidates = []

    if not gemini_candidates:
        print("Gemini returned no valid results.")
        return []

    
    for c in gemini_candidates:
        if "reason" in c:
            c["reason"] = " ".join(c["reason"].split())

    top3 = sorted(gemini_candidates, key=lambda x: x.get("score", 0), reverse=True)[:3]

    print("\n[Gemini Top 3]")
    for c in top3:
        print(f"- {c['gid']} | {c['text']} | Score: {c['score']} | Reason: {c['reason']}")

    return top3


# ----------------------------
# Main Compare Function
# ----------------------------
def compare(filename, shopify_data):

    print(f"\nComparing: '{filename}'")

    
    bm25, docs, gids = build_bm25_index(shopify_data)
    bm25_candidates = search_with_bm25(filename, bm25, docs, gids, top_n=5)

    print("\n[BM25 Candidates]")
    for c in bm25_candidates:
        print(f"- {c['gid']} | {c['text']} | Score: {c['score']:.3f}")

    
    gemini_candidates = validate_with_gemini(filename, bm25_candidates)

   
    if all(c.get("score", 0) <= 7 for c in gemini_candidates):
        validated_gid = "0"
        nhr_reason_auto = "All Gemini scores below threshold. Showing top-3 anyway."
        need_human_review = True
    else:
        validated_gid = gemini_candidates[0]["gid"]
        nhr_reason_auto = ""
        need_human_review = False

    return {
        "orig": filename,
        "final": filename,
        "candidates": gemini_candidates,
        "validated_gid": validated_gid,
        "need_human_review": need_human_review,
        "nhr_reason_auto": nhr_reason_auto
    }
