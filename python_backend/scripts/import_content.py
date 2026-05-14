"""
import_content.py — 从 JSON 快照导入数据库内容（Upsert 模式）

用途：在服务器部署时，从 content_seed.json 自动同步内容数据。
使用 Upsert（有则更新，无则插入）策略，保证：
- 用户数据（users, learning_sessions 等）完全不受影响
- 内容数据（topics, micro_scenarios 等）与本地完全一致

用法：
    python scripts/import_content.py
    python scripts/import_content.py --input content_seed.json --dry-run
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

from database import SessionLocal, Base, ensure_schema_upgrades


def ensure_table_schema(db) -> None:
    """
    确保数据库表结构与当前代码一致。

    自动创建缺失的表、添加缺失的列，不影响已有数据。
    这解决了服务器数据库结构较旧的问题。
    """
    from sqlalchemy import text

    print("[MIGRATION] Checking database schema...")

    # 复用 database.py 中的 ensure_schema_upgrades 函数（处理列的添加）
    ensure_schema_upgrades()

    # 检查并创建缺失的表
    tables_to_create = {
        "topics": _get_topics_create_sql(),
        "micro_scenarios": _get_micro_scenarios_create_sql(),
        "scenario_constraints": _get_scenario_constraints_create_sql(),
        "scenario_transitions": _get_scenario_transitions_create_sql(),
    }

    for table_name, create_sql in tables_to_create.items():
        result = db.execute(text(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'"
        )).fetchone()
        if not result:
            print(f"[CREATE] Table '{table_name}' not found, creating...")
            db.execute(text(create_sql))
            db.commit()
            print(f"[CREATE] Table '{table_name}' created successfully")
        else:
            print(f"[OK] Table '{table_name}' exists")

    print("[MIGRATION] Schema check complete.")


def _get_topics_create_sql() -> str:
    """返回 topics 表的 CREATE SQL（用于动态创建）"""
    return """
    CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title VARCHAR NOT NULL,
        title_zh VARCHAR,
        category VARCHAR,
        difficulty_base INTEGER DEFAULT 1,
        system_prompt VARCHAR,
        tags VARCHAR,
        role_name VARCHAR,
        learner_level VARCHAR,
        voice VARCHAR,
        vocab_tags JSON,
        sentence_patterns JSON,
        difficulty_tiers JSON,
        scene_specific_rules JSON,
        embedding JSON,
        domain VARCHAR(64),
        quality_grade VARCHAR(8),
        quality_score INTEGER,
        quality_issues JSON,
        generation_attempts INTEGER DEFAULT 1,
        is_published BOOLEAN DEFAULT 1
    )
    """


def _get_micro_scenarios_create_sql() -> str:
    """返回 micro_scenarios 表的 CREATE SQL"""
    return """
    CREATE TABLE IF NOT EXISTS micro_scenarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER NOT NULL,
        scenario_code VARCHAR UNIQUE NOT NULL,
        scenario_name VARCHAR NOT NULL,
        intent_desc TEXT NOT NULL,
        scene_desc TEXT NOT NULL,
        depth_level INTEGER DEFAULT 1,
        step_order INTEGER DEFAULT 0,
        is_entry_point BOOLEAN DEFAULT 0,
        max_turns INTEGER DEFAULT 8,
        weight REAL DEFAULT 1.0,
        embedding JSON,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        FOREIGN KEY (topic_id) REFERENCES topics(id)
    )
    """


def _get_scenario_constraints_create_sql() -> str:
    """返回 scenario_constraints 表的 CREATE SQL"""
    return """
    CREATE TABLE IF NOT EXISTS scenario_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        micro_scenario_id INTEGER NOT NULL,
        constraint_text VARCHAR NOT NULL,
        constraint_type VARCHAR DEFAULT 'word',
        depth_level INTEGER DEFAULT 1,
        weight REAL DEFAULT 1.0,
        hint_cn VARCHAR,
        created_at TIMESTAMP,
        FOREIGN KEY (micro_scenario_id) REFERENCES micro_scenarios(id)
    )
    """


def _get_scenario_transitions_create_sql() -> str:
    """返回 scenario_transitions 表的 CREATE SQL"""
    return """
    CREATE TABLE IF NOT EXISTS scenario_transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_scenario_id INTEGER NOT NULL,
        to_scenario_id INTEGER NOT NULL,
        overlap_ratio REAL NOT NULL,
        trigger_type VARCHAR DEFAULT 'auto',
        required_hit_rate REAL DEFAULT 0.8,
        shared_constraints JSON,
        created_by VARCHAR DEFAULT 'algorithm',
        edge_type VARCHAR(32),
        transition_weight REAL,
        created_at TIMESTAMP,
        CHECK (from_scenario_id != to_scenario_id),
        FOREIGN KEY (from_scenario_id) REFERENCES micro_scenarios(id),
        FOREIGN KEY (to_scenario_id) REFERENCES micro_scenarios(id)
    )
    """


def upsert_topic(db, data: dict, available_columns: set) -> dict:
    """Upsert Topic"""
    from database import Topic

    existing = db.query(Topic).filter(Topic.id == data["id"]).first()
    if existing:
        # 只更新存在的字段
        topic_fields = ["title", "title_zh", "category", "difficulty_base", "system_prompt",
                       "tags", "role_name", "learner_level", "voice", "vocab_tags",
                       "sentence_patterns", "difficulty_tiers", "scene_specific_rules",
                       "embedding", "domain", "quality_grade", "quality_score",
                       "quality_issues", "generation_attempts", "is_published"]
        for key in topic_fields:
            if key in available_columns and key in data and data[key] is not None:
                setattr(existing, key, data[key])
        return {"action": "updated", "id": existing.id}
    else:
        # 插入新记录，只包含存在的字段
        insert_fields = ["id", "title", "title_zh", "category", "difficulty_base", "system_prompt",
                        "tags", "role_name", "learner_level", "voice", "vocab_tags",
                        "sentence_patterns", "difficulty_tiers", "scene_specific_rules",
                        "embedding", "domain", "quality_grade", "quality_score",
                        "quality_issues", "generation_attempts", "is_published"]
        insert_data = {k: v for k, v in data.items() if k in insert_fields and k in available_columns}
        topic = Topic(**insert_data)
        db.add(topic)
        return {"action": "inserted", "id": data["id"]}


def upsert_micro_scenario(db, data: dict, available_columns: set) -> dict:
    """Upsert MicroScenario"""
    from database import MicroScenario

    existing = db.query(MicroScenario).filter(MicroScenario.id == data["id"]).first()
    if existing:
        scenario_fields = ["topic_id", "scenario_code", "scenario_name", "intent_desc",
                         "scene_desc", "depth_level", "step_order", "is_entry_point",
                         "max_turns", "weight", "embedding"]
        for key in scenario_fields:
            if key in available_columns and key in data and data[key] is not None:
                setattr(existing, key, data[key])
        return {"action": "updated", "id": existing.id}
    else:
        insert_fields = ["id", "topic_id", "scenario_code", "scenario_name", "intent_desc",
                        "scene_desc", "depth_level", "step_order", "is_entry_point",
                        "max_turns", "weight", "embedding"]
        insert_data = {k: v for k, v in data.items() if k in insert_fields and k in available_columns}
        scenario = MicroScenario(**insert_data)
        db.add(scenario)
        return {"action": "inserted", "id": data["id"]}


def upsert_constraint(db, data: dict, available_columns: set) -> dict:
    """Upsert ScenarioConstraint"""
    from database import ScenarioConstraint

    existing = db.query(ScenarioConstraint).filter(ScenarioConstraint.id == data["id"]).first()
    if existing:
        constraint_fields = ["micro_scenario_id", "constraint_text", "constraint_type",
                           "depth_level", "weight", "hint_cn"]
        for key in constraint_fields:
            if key in available_columns and key in data and data[key] is not None:
                setattr(existing, key, data[key])
        return {"action": "updated", "id": existing.id}
    else:
        insert_fields = ["id", "micro_scenario_id", "constraint_text", "constraint_type",
                       "depth_level", "weight", "hint_cn"]
        insert_data = {k: v for k, v in data.items() if k in insert_fields and k in available_columns}
        constraint = ScenarioConstraint(**insert_data)
        db.add(constraint)
        return {"action": "inserted", "id": data["id"]}


def upsert_transition(db, data: dict, available_columns: set) -> dict:
    """Upsert ScenarioTransition"""
    from database import ScenarioTransition

    existing = db.query(ScenarioTransition).filter(ScenarioTransition.id == data["id"]).first()
    if existing:
        transition_fields = ["from_scenario_id", "to_scenario_id", "overlap_ratio",
                            "trigger_type", "required_hit_rate", "shared_constraints",
                            "created_by", "edge_type", "transition_weight"]
        for key in transition_fields:
            if key in available_columns and key in data and data[key] is not None:
                setattr(existing, key, data[key])
        return {"action": "updated", "id": existing.id}
    else:
        insert_fields = ["id", "from_scenario_id", "to_scenario_id", "overlap_ratio",
                       "trigger_type", "required_hit_rate", "shared_constraints",
                       "created_by", "edge_type", "transition_weight"]
        insert_data = {k: v for k, v in data.items() if k in insert_fields and k in available_columns}
        transition = ScenarioTransition(**insert_data)
        db.add(transition)
        return {"action": "inserted", "id": data["id"]}


def import_content(db, input_path: str, dry_run: bool = False) -> dict:
    """
    从 JSON 快照导入数据库内容（Upsert 模式）。

    Args:
        db: SQLAlchemy Session
        input_path: JSON 文件路径
        dry_run: 如果为 True，只打印操作而不实际执行

    Returns:
        导入统计
    """
    print(f"[IMPORT] Loading {input_path}")
    print(f"[IMPORT] Dry-run mode: {dry_run}")

    if not os.path.exists(input_path):
        print(f"[ERROR] File not found: {input_path}")
        return {"error": "File not found"}

    # 1. 确保数据库结构是最新的
    ensure_table_schema(db)

    # 2. 获取每张表的可用列（用于处理新旧数据库结构差异）
    available_columns = {}
    for table_name in ["topics", "micro_scenarios", "scenario_constraints", "scenario_transitions"]:
        from sqlalchemy import text
        result = db.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        available_columns[table_name] = {row[1] for row in result}
    print(f"[IMPORT] Available columns: { {k: len(v) for k, v in available_columns.items()} }")

    with open(input_path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    version = snapshot.get("version", "unknown")
    exported_at = snapshot.get("exported_at", "unknown")
    print(f"[IMPORT] Snapshot version: {version}, exported at: {exported_at}")

    data = snapshot.get("data", {})
    stats = {
        "topics": {"inserted": 0, "updated": 0, "skipped": 0},
        "micro_scenarios": {"inserted": 0, "updated": 0, "skipped": 0},
        "scenario_constraints": {"inserted": 0, "updated": 0, "skipped": 0},
        "scenario_transitions": {"inserted": 0, "updated": 0, "skipped": 0},
    }

    if dry_run:
        print("\n[DRY RUN] Would perform the following operations:")

    # 3. Import Topics
    topics = data.get("topics", [])
    topic_cols = available_columns.get("topics", set())
    print(f"\n[IMPORT] Processing {len(topics)} Topics...")
    for item in topics:
        result = upsert_topic(db, item, topic_cols)
        key = "updated" if result["action"] == "updated" else "inserted"
        stats["topics"][key] += 1
        if dry_run and stats["topics"]["inserted"] <= 5:
            print(f"  [DRY] Topic {item['id']}: {item['title'][:40]}...")

    # 4. Import MicroScenarios
    scenarios = data.get("micro_scenarios", [])
    scenario_cols = available_columns.get("micro_scenarios", set())
    print(f"[IMPORT] Processing {len(scenarios)} MicroScenarios...")
    for item in scenarios:
        result = upsert_micro_scenario(db, item, scenario_cols)
        key = "updated" if result["action"] == "updated" else "inserted"
        stats["micro_scenarios"][key] += 1
        if dry_run and stats["micro_scenarios"]["inserted"] <= 3:
            print(f"  [DRY] MicroScenario {item['id']}: {item['scenario_code']}")

    # 5. Import Constraints
    constraints = data.get("scenario_constraints", [])
    constraint_cols = available_columns.get("scenario_constraints", set())
    print(f"[IMPORT] Processing {len(constraints)} Constraints...")
    for item in constraints:
        result = upsert_constraint(db, item, constraint_cols)
        key = "updated" if result["action"] == "updated" else "inserted"
        stats["scenario_constraints"][key] += 1

    # 6. Import Transitions
    transitions = data.get("scenario_transitions", [])
    transition_cols = available_columns.get("scenario_transitions", set())
    print(f"[IMPORT] Processing {len(transitions)} Transitions...")
    for item in transitions:
        result = upsert_transition(db, item, transition_cols)
        key = "updated" if result["action"] == "updated" else "inserted"
        stats["scenario_transitions"][key] += 1

    # 提交或回滚
    if dry_run:
        print("\n[DRY RUN] No changes committed (--dry-run mode)")
        db.rollback()
    else:
        try:
            db.commit()
            print("\n[IMPORT] Changes committed successfully!")
        except Exception as e:
            print(f"\n[ERROR] Commit failed: {e}")
            db.rollback()
            return {"error": str(e)}

    # 打印汇总
    print("\n" + "=" * 50)
    print("[IMPORT] Summary")
    print("=" * 50)
    for table, s in stats.items():
        total = s["inserted"] + s["updated"]
        print(f"  {table}: {total} total ({s['inserted']} inserted, {s['updated']} updated)")

    return stats


def main():
    parser = argparse.ArgumentParser(description="从 JSON 快照导入数据库内容")
    parser.add_argument(
        "--input", "-i",
        default=os.path.join(_backend_dir, "content_seed.json"),
        help="输入 JSON 文件路径 (默认: ../content_seed.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印操作，不实际执行",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = import_content(db, args.input, dry_run=args.dry_run)
        if "error" not in result:
            print("\n[SUCCESS] Import completed!")
        else:
            print(f"\n[FAILED] Import failed: {result['error']}")
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
