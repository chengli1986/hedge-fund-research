"""Guard: hand-audited static facts must never be silently regressed by the
monthly refresh (or any future edit). Asserts known-correct tokens are present
and known-wrong tokens never reappear in production publish._FUND_PROFILES."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("publish_real", REPO / "publish.py")
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)
PROFILES = pub._FUND_PROFILES

MUST_CONTAIN = {
    "gmo": ("desc_zh", "Eyk van Otterloo"),
}
MUST_NOT_CONTAIN = [
    "Nicholas Otis",          # GMO wrong founder
    "Hudson River Trading",   # D.E. Shaw wrong alumnus claim
    "英国最大独立",            # Aberdeen overclaim
    "全球最大另类信贷",        # Oaktree overclaim
]


def test_required_static_facts_present():
    for fund_id, (field, token) in MUST_CONTAIN.items():
        assert token in PROFILES[fund_id][field], f"{fund_id}.{field} lost {token!r}"


def test_regressed_facts_never_reappear():
    blob = "\n".join(
        f"{v.get('desc_zh','')} {v.get('notable_en','')} {v.get('notable_zh','')}"
        for v in PROFILES.values())
    for bad in MUST_NOT_CONTAIN:
        assert bad not in blob, f"regressed fact reappeared: {bad!r}"
