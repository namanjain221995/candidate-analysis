"""Overall result computation (day-level and candidate-level).

Method: math combination.
  - overallScore = average of the deliverable scores (rounded)
  - result = PASS only if every deliverable PASSed, else FAIL
  - positives/negatives = collected from the deliverables (deduplicated, capped)

The "is the day complete?" check reads metadata.json at the candidate root to
know how many deliverables a day is expected to have, then counts how many
_result.json files exist for that day.
"""

import json
from typing import List, Optional

import llm_s3


def _candidate_root(any_key: str) -> Optional[str]:
    """The top candidate prefix, e.g. 'Jay Thakkar(001...)/'."""
    parts = any_key.split("/")
    return parts[0] + "/" if parts and parts[0] else None


def read_metadata(s3, bucket, any_key) -> Optional[dict]:
    root = _candidate_root(any_key)
    if not root:
        return None
    try:
        return json.loads(llm_s3.read_text(s3, bucket, root + "metadata.json"))
    except Exception:
        return None


def expected_deliverables_for_day(metadata: dict, day_folder_name: str):
    """From metadata.json, return the set of expected deliverable names for the
    day whose folder is `day_folder_name` (e.g. 'Day 1 - Foundations(a0z...)').
    Combined image/text inputs are excluded — only the scored ones count."""
    if not metadata:
        return None
    # match the day by its title/id appearing in the folder name
    for training in metadata.get("candidateTraining", []):
        for step in training.get("trainingSteps", []):
            for _k, day in step.items():
                title = (day.get("title") or "")
                day_id = (day.get("day Id") or "")
                if (title and title in day_folder_name) or (day_id and day_id in day_folder_name):
                    names = []
                    for d in day.get("deliverables", []):
                        dn = (d.get("name") or "").lower()
                        # skip combined-input-only deliverables (image/text companions)
                        if "diagram" in dn or dn.endswith("text") or dn.endswith("image"):
                            continue
                        names.append(d.get("name"))
                    return [n for n in names if n]
    return None


def combine(results: List[dict], *, label_fields: dict) -> dict:
    """Math-combine per-deliverable result dicts into one overall dict."""
    scored = [r for r in results if isinstance(r.get("score"), (int, float))]
    avg = round(sum(r["score"] for r in scored) / len(scored)) if scored else 0
    all_pass = bool(results) and all((r.get("result") == "PASS") for r in results)

    positives, negatives = [], []
    for r in results:
        for p in r.get("positives", [])[:2]:
            if p not in positives:
                positives.append(p)
        for n in r.get("negatives", [])[:2]:
            if n not in negatives:
                negatives.append(n)

    doc = dict(label_fields)
    doc.update({
        "overallScore": avg,
        "result": "PASS" if all_pass else "FAIL",
        "deliverables": [
            {"deliverable": r.get("deliverable"), "score": r.get("score"), "result": r.get("result")}
            for r in results
        ],
        "reasoning": (
            f"Average score {avg} across {len(results)} deliverable(s); "
            + ("all passed." if all_pass else "one or more did not pass.")
        ),
        "positives": positives[:6],
        "negatives": negatives[:8],
    })
    return doc
