"""Phase 2 sanity tests - run with: python test_phase2.py"""
import sys
from core.dialogue_engine import _l1_match_quality
from application.services.mastery_scorer import (
    update_mastery, compute_decayed_mastery, normalize_text, simple_stem
)
import datetime

print("=== mastery_scorer tests ===")
m = 0.0
for i in range(5):
    m = update_mastery(m, was_correct=True, quality=1.0)
print(f"mastery after 5 correct hits: {m:.1f}  (expected ~83)")

decayed = compute_decayed_mastery(m, datetime.datetime.utcnow() - datetime.timedelta(days=7))
print(f"after 7 days decay: {decayed:.1f}  (expected ~41)")

print(f"simple_stem('ordering'): {simple_stem('ordering')}  (expected: order)")
print(f"simple_stem('burgers'): {simple_stem('burgers')}   (expected: burger)")
print(f"simple_stem('tried'): {simple_stem('tried')}     (expected: try)")
print(f"normalize I'd like to order: '{normalize_text(chr(73)+chr(39)+'d like to order')}'")

print("\n=== L1 match quality tests ===")
tests = [
    ("i would like to order", "i would like to order a big mac", 1.0, "exact phrase"),
    ("burger",                "I want two burgers please",        0.8, "stem: burgers->burger"),
    ("combo meal",            "I want a burger",                  0.0, "no match"),
    ("fries",                 "Can I also get some fries",         1.0, "exact word"),
    ("order",                 "I ordered a burger yesterday",      0.8, "stem: ordered->order"),
    ("for here or to go",     "is that for here or to go",        1.0, "exact multi-word"),
]

all_pass = True
for node, user, expected, label in tests:
    result = _l1_match_quality(node, user)
    status = "OK" if result == expected else "FAIL"
    if status == "FAIL":
        all_pass = False
    user_preview = user[:40]
    print(f"  [{status}] {label}: node={repr(node)} user={repr(user_preview)} -> {result} (expected {expected})")

# Contraction test
node = "i would like to order"
user_raw = "id like to order something"  # after normalize_text("I'd like to order")
user_norm = normalize_text("I'd like to order something")
result = _l1_match_quality(node, user_norm)
expected = 1.0
status = "OK" if result == expected else "FAIL"
if status == "FAIL":
    all_pass = False
print(f"  [{status}] contraction I'd->I would: -> {result} (expected {expected})")

print("\n" + ("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED"))
sys.exit(0 if all_pass else 1)
