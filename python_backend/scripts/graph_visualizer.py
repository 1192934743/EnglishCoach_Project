"""
graph_visualizer.py - 图谱数据导出工具

从数据库导出图谱数据为 JSON，供 D3.js 可视化使用。

使用方法:
    python scripts/graph_visualizer.py
    # 生成 static/graph_data.json 和 static/graph_visualizer.html

或单独运行:
    python -c "from scripts.graph_visualizer import export_graph_data; export_graph_data()"
"""

import json
import os
import sys
from pathlib import Path

# 添加父目录到路径，以便导入 database 等模块
_script_dir = Path(__file__).parent
_backend_dir = _script_dir.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

from dotenv import load_dotenv

# 加载环境变量
load_dotenv(_backend_dir / "config.env")


def get_db_session():
    """获取数据库会话"""
    from database import SessionLocal
    return SessionLocal()


def export_graph_data(output_dir: str = None) -> dict:
    """
    从数据库导出图谱数据为 JSON 格式。

    Returns:
        dict: 包含 nodes 和 links 的图谱数据
    """
    if output_dir is None:
        output_dir = _backend_dir / "static"
    else:
        output_dir = Path(output_dir)

    # 确保输出目录存在
    output_dir.mkdir(parents=True, exist_ok=True)

    db = get_db_session()
    try:
        from database import MicroScenario, ScenarioTransition, Topic, ScenarioConstraint

        # 1. 读取所有话题
        topics = {t.id: {"id": t.id, "title": t.title, "title_zh": t.title_zh or t.title}
                  for t in db.query(Topic).all()}

        # 2. 读取所有节点
        nodes = []
        for s in db.query(MicroScenario).all():
            # 获取约束
            constraints = db.query(ScenarioConstraint).filter(
                ScenarioConstraint.micro_scenario_id == s.id
            ).all()

            topic_info = topics.get(s.topic_id, {"title": "Unknown", "title_zh": "未知"})

            nodes.append({
                "id": s.id,
                "topic_id": s.topic_id,
                "topic_title": topic_info["title"],
                "topic_title_zh": topic_info["title_zh"],
                "scenario_code": s.scenario_code,
                "scenario_name": s.scenario_name,
                "intent_desc": s.intent_desc,
                "scene_desc": s.scene_desc,
                "depth_level": s.depth_level,
                "step_order": s.step_order,
                "is_entry_point": s.is_entry_point,
                "max_turns": s.max_turns,
                "weight": s.weight,
                "constraints": [
                    {
                        "text": c.constraint_text,
                        "type": c.constraint_type,
                        "depth_level": c.depth_level,
                        "weight": c.weight,
                        "hint_cn": c.hint_cn
                    }
                    for c in constraints
                ]
            })

        # 3. 读取所有边
        links = []
        for t in db.query(ScenarioTransition).all():
            links.append({
                "id": t.id,
                "source": t.from_scenario_id,
                "target": t.to_scenario_id,
                "overlap_ratio": t.overlap_ratio,
                "trigger_type": t.trigger_type,
                "required_hit_rate": t.required_hit_rate,
                "edge_type": t.edge_type or "auto",
                "transition_weight": t.transition_weight
            })

        # 4. 组装图谱数据
        graph_data = {
            "nodes": nodes,
            "links": links,
            "metadata": {
                "total_nodes": len(nodes),
                "total_links": len(links),
                "total_topics": len(topics),
                "topics": list(topics.values())
            }
        }

        # 5. 写入 JSON 文件
        json_path = output_dir / "graph_data.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, ensure_ascii=False, indent=2)

        print(f"[OK] 图谱数据已导出到: {json_path}")
        print(f"     - 节点数: {len(nodes)}")
        print(f"     - 边数: {len(links)}")
        print(f"     - 话题数: {len(topics)}")

        return graph_data

    finally:
        db.close()


def get_html_path() -> Path:
    """获取 HTML 文件路径"""
    return _script_dir / "static" / "graph_visualizer.html"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="图谱数据导出工具")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="输出目录 (默认: scripts/static)")
    parser.add_argument("--only-json", action="store_true",
                        help="仅导出 JSON，不生成 HTML")
    args = parser.parse_args()

    # 导出数据
    graph_data = export_graph_data(args.output_dir)

    if not args.only_json:
        html_path = get_html_path()
        html_path.parent.mkdir(parents=True, exist_ok=True)

        if not html_path.exists():
            print(f"[INFO] 请先创建 HTML 文件: {html_path}")
