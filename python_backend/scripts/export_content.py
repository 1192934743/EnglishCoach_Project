"""
export_content.py — 导出数据库内容为 JSON 快照

用途：将 Topic、MicroScenario、ScenarioConstraint、ScenarioTransition 表导出为
content_seed.json，用于部署时自动同步内容数据到远端服务器。

用法：
    python scripts/export_content.py
    python scripts/export_content.py --output ../content_seed.json
"""

import argparse
import json
import os
import sys
from datetime import datetime

# 添加项目路径
_script_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.dirname(_script_dir)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from database import SessionLocal, Topic, MicroScenario, ScenarioConstraint, ScenarioTransition


def _serialize_value(value):
    """序列化特殊类型（如 datetime）为 JSON 兼容格式"""
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, '__dict__'):
        # 处理 SQLAlchemy 对象
        result = {}
        for k, v in value.__dict__.items():
            if k.startswith('_'):
                continue
            result[k] = _serialize_value(v)
        return result
    return value


def export_content(db, output_path: str) -> dict:
    """
    导出所有内容表为 JSON 快照。

    Returns:
        导出的统计数据
    """
    print(f"[EXPORT] Starting export to {output_path}")

    # 1. 导出 Topics
    topics = db.query(Topic).all()
    topics_data = []
    for t in topics:
        data = {
            "id": t.id,
            "title": t.title,
            "title_zh": t.title_zh,
            "category": t.category,
            "difficulty_base": t.difficulty_base,
            "system_prompt": t.system_prompt,
            "tags": t.tags,
            "role_name": t.role_name,
            "learner_level": t.learner_level,
            "voice": t.voice,
            "vocab_tags": t.vocab_tags,
            "sentence_patterns": t.sentence_patterns,
            "difficulty_tiers": t.difficulty_tiers,
            "scene_specific_rules": t.scene_specific_rules,
            # embedding 字段太长，部署时由 import 重新生成
            # "embedding": t.embedding,
            "domain": t.domain,
            "quality_grade": t.quality_grade,
            "quality_score": t.quality_score,
            "quality_issues": t.quality_issues,
            "generation_attempts": t.generation_attempts,
            "is_published": t.is_published,
        }
        topics_data.append(data)
    print(f"[EXPORT] Topics: {len(topics_data)} records")

    # 2. 导出 MicroScenarios
    scenarios = db.query(MicroScenario).all()
    scenarios_data = []
    for s in scenarios:
        data = {
            "id": s.id,
            "topic_id": s.topic_id,
            "scenario_code": s.scenario_code,
            "scenario_name": s.scenario_name,
            "intent_desc": s.intent_desc,
            "scene_desc": s.scene_desc,
            "depth_level": s.depth_level,
            "step_order": s.step_order,
            "is_entry_point": s.is_entry_point,
            "max_turns": s.max_turns,
            "weight": s.weight,
            # embedding 字段太长，部署时由 import 重新生成
            # "embedding": s.embedding,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        scenarios_data.append(data)
    print(f"[EXPORT] MicroScenarios: {len(scenarios_data)} records")

    # 3. 导出 ScenarioConstraints
    constraints = db.query(ScenarioConstraint).all()
    constraints_data = []
    for c in constraints:
        data = {
            "id": c.id,
            "micro_scenario_id": c.micro_scenario_id,
            "constraint_text": c.constraint_text,
            "constraint_type": c.constraint_type,
            "depth_level": c.depth_level,
            "weight": c.weight,
            "hint_cn": c.hint_cn,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        constraints_data.append(data)
    print(f"[EXPORT] ScenarioConstraints: {len(constraints_data)} records")

    # 4. 导出 ScenarioTransitions
    transitions = db.query(ScenarioTransition).all()
    transitions_data = []
    for t in transitions:
        data = {
            "id": t.id,
            "from_scenario_id": t.from_scenario_id,
            "to_scenario_id": t.to_scenario_id,
            "overlap_ratio": t.overlap_ratio,
            "trigger_type": t.trigger_type,
            "required_hit_rate": t.required_hit_rate,
            "shared_constraints": t.shared_constraints,
            "created_by": t.created_by,
            "edge_type": t.edge_type,
            "transition_weight": t.transition_weight,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        transitions_data.append(data)
    print(f"[EXPORT] ScenarioTransitions: {len(transitions_data)} records")

    # 5. 组装完整快照
    snapshot = {
        "version": "1.0",
        "exported_at": datetime.utcnow().isoformat(),
        "description": "EnglishCoach content seed data - sync to remote server on deployment",
        "data": {
            "topics": topics_data,
            "micro_scenarios": scenarios_data,
            "scenario_constraints": constraints_data,
            "scenario_transitions": transitions_data,
        }
    }

    # 6. 写入文件
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    stats = {
        "topics": len(topics_data),
        "micro_scenarios": len(scenarios_data),
        "scenario_constraints": len(constraints_data),
        "scenario_transitions": len(transitions_data),
        "output_path": output_path,
    }
    print(f"[EXPORT] Done! Total size: {os.path.getsize(output_path)} bytes")
    return stats


def main():
    parser = argparse.ArgumentParser(description="导出数据库内容为 JSON 快照")
    parser.add_argument(
        "--output", "-o",
        default=os.path.join(_backend_dir, "content_seed.json"),
        help="输出文件路径 (默认: ../content_seed.json)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        stats = export_content(db, args.output)
        print(f"\n[SUCCESS] Export completed!")
        print(f"  Topics: {stats['topics']}")
        print(f"  MicroScenarios: {stats['micro_scenarios']}")
        print(f"  Constraints: {stats['scenario_constraints']}")
        print(f"  Transitions: {stats['scenario_transitions']}")
        print(f"  Output: {stats['output_path']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
