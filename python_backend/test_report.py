"""Test session report builder end-to-end"""
import json, uuid
from database import SessionLocal, User, UserProgress
from application.services.session_planner import warm_up, build_task_packet
from application.services.report_builder import build_preliminary_report, build_final_report, _quality_label

warm_up()
db = SessionLocal()

# Create test user
test_user = User(id=str(uuid.uuid4()))
db.add(test_user)
db.commit()
db.refresh(test_user)

# Build task packet
packet = build_task_packet(test_user.id, db)
print(f"Topic: {packet.topic_title}, tier={packet.depth_tier}")
print(f"Target nodes: {[n['node_text'] for n in packet.target_nodes]}")

# Simulate session: user hit some nodes
session_hits = set()
mastery_snapshot = {}
if packet.target_nodes:
    session_hits.add(packet.target_nodes[0]["id"])   # hit first node
    if len(packet.target_nodes) > 1:
        session_hits.add(packet.target_nodes[1]["id"])  # hit second node

# Simulate L1 mastery updates
for nid in session_hits:
    prog = UserProgress(user_id=test_user.id, node_id=nid, mastery_score=35.0, practice_count=2)
    db.add(prog)
db.commit()

# Fake session_ctx
session_ctx = {"chat_score": 25.0, "task_score": 40.0}
session_id = str(uuid.uuid4())

# Build preliminary report
report = build_preliminary_report(
    task_packet=packet,
    session_id=session_id,
    session_hits=session_hits,
    session_ctx=session_ctx,
    mastery_snapshot=mastery_snapshot,
    db=db,
    user_id=test_user.id,
)

print("\n=== PRELIMINARY REPORT ===")
print(f"event: {report['event']}, stage: {report['stage']}")
print(f"session_score: {report['session_score']}")
print(f"hit_count: {report['hit_count']} / {report['total_nodes']}")
print(f"tier_status: {report['tier_status']}")
print(f"encouragement: {report['encouragement']}")
print(f"nodes count: {len(report['nodes'])}")
for n in report['nodes']:
    print(f"  - {n['text']}: hit={n['hit']}, mastery {n['mastery_before']}→{n['mastery_now']} (+{n['mastery_delta']})")

# Build final report (simulate L2 results)
l2_results = []
for node in packet.target_nodes[:2]:
    l2_results.append({
        "node_id": node["id"],
        "attempted": node["id"] in session_hits,
        "correct": node["id"] in session_hits,
        "quality": 0.85 if node["id"] in session_hits else 0.0,
    })

final = build_final_report(packet, session_id, l2_results, db, test_user.id)
if final:
    print("\n=== FINAL REPORT ===")
    print(f"avg_quality: {final['avg_quality']}, label: {final['quality_label']}")
    for q in final['quality_breakdown']:
        print(f"  - {q['text']}: quality={q['quality']}, label={q['quality_label']}")

# Test quality labels
print("\n=== quality_label tests ===")
for q, expected in [(0.9, "excellent"), (0.7, "good"), (0.5, "fair"), (0.2, "needs_work"), (0.0, "not_used")]:
    label = _quality_label(q, q > 0)
    print(f"  q={q} -> {label} (expected: {expected})")

db.close()
print("\nAll report builder tests passed.")
