阶段一：全局架构蓝图与数据库模型设计
一、设计背景与全局对齐
1.1 现有架构 vs 目标架构
维度	旧架构 (dialogue_engine.py)	新架构（双轨校验微场景图谱）
状态管理
线性四阶段 (ICE→CORE→EVENT→WRAP)
有向图节点流转（微场景图谱）
词汇控制
TargetNode 列表匹配
ScenarioConstraint 双轨校验（Intent + Constraints）
场景粒度
大话题 (Topic)
极细粒度 MicroScenario（如"确认咖啡杯型"）
流转规则
固定相位顺序 + 随机事件
70/30 词汇重叠度阈值触发
难度隔离
无严格边界
depth_level 硬隔离（同层内流转）
评估信号
单一 should_advance_phase
intent_met + constraints_satisfied 双轨
1.2 阶段一核心约束
阶段一只做基建与数据结构，不修改对话引擎状态机流转逻辑。新状态机将在阶段二实现。

二、模块 1：数据库模型重构 (database.py)
2.1 废弃与保留
旧表	决策	理由
TargetNode
保留但重新定位
其 node_text + depth_level 是 ScenarioConstraint 的数据来源；旧字段与新约束可共存
Topic
保留
作为微场景的父级大话题
UserProgress
保留
追踪用户对 constraint_id 的掌握度
2.2 新增表设计
表 A：MicroScenario（微场景定义）
class MicroScenario(Base):
    """
    微场景 — 场景图的节点。
    一个 Topic 可包含多个 MicroScenario，形成子图。
    
    设计原则：
    - intent_desc 描述该微场景的学习目标（给 Director LLM 看的教学意图）
    - scene_desc 描述该微场景的自然情境（给 Actor LLM 看的情境描述）
    - depth_level 与 CEFR 对齐：1=Beginner, 2=Intermediate, 3=Advanced
    """
    __tablename__ = 'micro_scenarios'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 归属大话题
    topic_id = Column(Integer, ForeignKey('topics.id'), nullable=False)
    
    # 场景标识
    scenario_code = Column(String, unique=True, nullable=False)  # e.g., "MCD_01_CONFIRM_SIZE"
    scenario_name = Column(String, nullable=False)               # e.g., "Confirm Cup Size"
    
    # 描述层（给 AI 看的）
    intent_desc = Column(Text, nullable=False)  # 教学意图："用户需要确认咖啡杯尺寸（小/中/大）"
    scene_desc = Column(Text, nullable=False)   # 自然情境："咖啡师正在询问您的饮料杯型"
    
    # 难度等级（CEFR 对齐，严格隔离）
    # 注意：此字段决定该微场景的绝对难度，流转时不得跨级
    depth_level = Column(Integer, default=1)    # 1=基础, 2=进阶, 3=高阶
    
    # 元数据
    is_entry_point = Column(Boolean, default=False)  # 是否为话题入口微场景
    max_turns = Column(Integer, default=8)             # 该微场景建议最大轮数（超时强制流转）
    weight = Column(Float, default=1.0)                # 推荐权重（影响图谱构建算法）
    
    # 状态追踪（会话中动态填充，非 DB 存储）
    # 注意：以下字段仅在 session_ctx 内存中存在，不映射为 DB 列
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
表 B：ScenarioConstraint（微场景教学约束）
class ScenarioConstraint(Base):
    """
    微场景的教学约束 — 双轨校验的"Constraints"标尺。
    
    替代旧的 TargetNode 概念，提供更结构化的约束描述。
    
    设计原则：
    - 一个 MicroScenario 可绑定多个 Constraint
    - Director LLM 的双轨校验之一：检查这些 constraint_text 是否被使用
    - constraint_type 用于区分单词/短语/句型，便于 Director 理解检测粒度
    """
    __tablename__ = 'scenario_constraints'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    micro_scenario_id = Column(Integer, ForeignKey('micro_scenarios.id'), nullable=False)
    
    # 约束文本（目标表达）
    constraint_text = Column(String, nullable=False)  # e.g., "medium"
    constraint_type = Column(String, default="word")  # word / phrase / sentence
    
    # 难度等级（必须与父 MicroScenario.depth_level 一致或更低）
    depth_level = Column(Integer, default=1)
    
    # 权重（影响约束被命中的重要性）
    weight = Column(Float, default=1.0)
    
    # 提示（当约束未被命中时，Director 可参考此提示）
    hint_cn = Column(String, nullable=True)  # e.g., "咖啡中杯用 medium"
    
    # 可选：关联旧 TargetNode（用于迁移期兼容）
    legacy_node_id = Column(Integer, ForeignKey('target_nodes.id'), nullable=True)
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
表 C：ScenarioTransition（微场景图谱流转边）
class ScenarioTransition(Base):
    """
    微场景图谱的流转边 — 场景切换的桥梁。
    
    设计原则：
    - overlap_ratio 计算：两场景间 Constraint 集合的 Jaccard IoU
    - overlap_ratio >= 0.6 是建立流转边的阈值（支撑 70/30 平滑原则）
    - 双向独立边：from -> to 和 to -> from 是两条独立记录（支持非对称流转）
    """
    __tablename__ = 'scenario_transitions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 流转边的两个端点
    from_scenario_id = Column(Integer, ForeignKey('micro_scenarios.id'), nullable=False)
    to_scenario_id = Column(Integer, ForeignKey('micro_scenarios.id'), nullable=False)
    
    # 流转条件
    # overlap_ratio: [0.0, 1.0]，两场景间约束词汇的 Jaccard IoU
    # 阈值：0.6 <= ratio <= 0.8 时建立流转边（70/30 平滑原则）
    overlap_ratio = Column(Float, nullable=False)
    
    # 流转触发方式
    trigger_type = Column(String, default="auto")  # auto / manual / intent_driven
    
    # 可选：触发该流转所需的约束命中率阈值（如 0.8 = 80% 约束已命中）
    required_hit_rate = Column(Float, default=0.8)
    
    # 元数据（用于调试和可解释性）
    shared_constraints = Column(JSON, nullable=True)  # 两场景共享的 constraint_id 列表
    created_by = Column(String, default="algorithm")    # "algorithm" / "manual"
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    # 约束：禁止自环
    __table_args__ = (
        CheckConstraint('from_scenario_id != to_scenario_id', name='no_self_loop'),
    )
2.3 关联关系图
Topic (1) ─────────┬───────── (N) MicroScenario (1)
                  │                       │
                  │                       ├── (N) ScenarioConstraint
                  │                       │         │
                  │                       │         └── legacy_node_id ─── (1) TargetNode
                  │                       │
                  │                       └── (N) ScenarioTransition (as from)
                  │                                         │
                  │                                         └── to_scenario_id ─── MicroScenario (as to)
                  │
                  └── (N) UserProgress ─── node_id ─── (1) TargetNode (兼容)
2.4 迁移策略
为保证向后兼容，采用字段共存 + 渐进迁移策略：

MicroScenario 新增时，legacy_node_id 关联旧 TargetNode
ScenarioConstraint.constraint_text 初始值从 TargetNode.node_text 同步
对话引擎先检查新表，无数据时 fallback 到旧 TargetNode 逻辑
全部迁移完成后，废弃旧 TargetNode 的学习相关逻辑
三、模块 2：离线图谱构建算法 (scripts/build_scenario_graph.py)
3.1 算法目标
遍历同 Topic、同 Depth 级别的所有 MicroScenario，计算任意两场景间的约束词汇 IoU (Intersection over Union)，当 IoU 落在 [MIN_IOU_THRESHOLD, MAX_IOU_THRESHOLD] 区间内时，在 ScenarioTransition 表中建立流转边。

3.2 核心参数
# 图谱构建参数（可配置化）
MIN_IOU_THRESHOLD = 0.60   # 最低共享比例（低于此值=场景跳跃过大）
MAX_IOU_THRESHOLD = 0.80   # 最高共享比例（高于此值=两场景实质相同，冗余）
ENTRY_POINT_BOOST = 0.05   # 若 to_scenario.is_entry_point，则 overlap_ratio 门槛降低 5%
3.3 伪代码
"""
scripts/build_scenario_graph.py
Usage:
    python scripts/build_scenario_graph.py [--topic-id TOPIC_ID] [--depth DEPTH] [--dry-run]
"""
def compute_constraint_iou(constraints_a: list[str], constraints_b: list[str]) -> float:
    """
    计算两场景约束集合的 Jaccard IoU。
    返回值 ∈ [0.0, 1.0]
    """
    set_a = set(normalize_text(c) for c in constraints_a)
    set_b = set(normalize_text(c) for c in constraints_b)
    
    if not set_a and not set_b:
        return 0.0
    
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0
def build_scenario_graph(db: Session, topic_id: int, depth_level: int, dry_run: bool = False):
    """
    主流程：
    1. 加载该 Topic + Depth 下所有 MicroScenario
    2. 加载每个 MicroScenario 绑定的 ScenarioConstraint
    3. 两两计算 IoU
    4. 阈值过滤后，批量写入 ScenarioTransition
    """
    # Step 1: 获取所有微场景
    scenarios = db.query(MicroScenario).filter(
        MicroScenario.topic_id == topic_id,
        MicroScenario.depth_level == depth_level
    ).all()
    
    # Step 2: 构建场景ID → 约束集合的映射
    scenario_constraints = {}
    for sc in scenarios:
        constraints = db.query(ScenarioConstraint).filter(
            ScenarioConstraint.micro_scenario_id == sc.id
        ).all()
        scenario_constraints[sc.id] = [c.constraint_text for c in constraints]
    
    # Step 3: 两两遍历，建立流转候选
    transitions_to_create = []
    
    for i, sc_a in enumerate(scenarios):
        for sc_b in scenarios[i + 1:]:
            constraint_set_a = scenario_constraints[sc_a.id]
            constraint_set_b = scenario_constraints[sc_b.id]
            
            iou = compute_constraint_iou(constraint_set_a, constraint_set_b)
            
            # 阈值判断
            effective_min = MIN_IOU_THRESHOLD
            if sc_b.is_entry_point:
                effective_min -= ENTRY_POINT_BOOST
            
            if MIN_IQR_THRESHOLD <= iou <= MAX_IQR_THRESHOLD:
                shared_ids = list(
                    db.query(ScenarioConstraint.id).filter(
                        ScenarioConstraint.micro_scenario_id.in_([sc_a.id, sc_b.id]),
                        ScenarioConstraint.constraint_text.in_(
                            set(scenario_constraints[sc_a.id]) & set(scenario_constraints[sc_b.id])
                        )
                    ).all()
                )
                
                transitions_to_create.append({
                    "from_scenario_id": sc_a.id,
                    "to_scenario_id": sc_b.id,
                    "overlap_ratio": round(iou, 4),
                    "trigger_type": "auto",
                    "shared_constraints": [c[0] for c in shared_ids],
                    "created_by": "algorithm",
                })
                # 双向边
                transitions_to_create.append({
                    "from_scenario_id": sc_b.id,
                    "to_scenario_id": sc_a.id,
                    "overlap_ratio": round(iou, 4),
                    "trigger_type": "auto",
                    "shared_constraints": [c[0] for c in shared_ids],
                    "created_by": "algorithm",
                })
    
    if dry_run:
        print(f"[DRY RUN] Would create {len(transitions_to_create)} transitions")
        for t in transitions_to_create:
            print(f"  {t['from_scenario_id']} -> {t['to_scenario_id']} (IoU={t['overlap_ratio']})")
        return
    
    # Step 4: 批量写入（去重：先删后插）
    db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id.in_([t["from_scenario_id"] for t in transitions_to_create]),
        ScenarioTransition.created_by == "algorithm"
    ).delete(synchronize_session=False)
    
    db.bulk_insert_mappings(ScenarioTransition, transitions_to_create)
    db.commit()
    
    print(f"[OK] Created {len(transitions_to_create)} transitions for topic_id={topic_id}, depth={depth_level}")
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic-id", type=int, required=True)
    parser.add_argument("--depth", type=int, default=1, choices=[1, 2, 3])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    
    from database import SessionLocal
    db = SessionLocal()
    try:
        build_scenario_graph(db, args.topic_id, args.depth, args.dry_run)
    finally:
        db.close()
3.4 手动干预接口
为防止纯算法生成的图谱不符合教学直觉，提供手动增删边的接口：

def add_manual_transition(db: Session, from_id: int, to_id: int, reason: str = ""):
    """手动添加一条流转边（教学专家干预）"""
    existing = db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id == from_id,
        ScenarioTransition.to_scenario_id == to_id
    ).first()
    if existing:
        return
    t = ScenarioTransition(
        from_scenario_id=from_id,
        to_scenario_id=to_id,
        overlap_ratio=0.0,  # 手动边不计算 IoU
        trigger_type="manual",
        created_by="manual"
    )
    db.add(t)
    db.commit()
def remove_transition(db: Session, from_id: int, to_id: int):
    """删除一条流转边（移除算法误判的边）"""
    db.query(ScenarioTransition).filter(
        ScenarioTransition.from_scenario_id == from_id,
        ScenarioTransition.to_scenario_id == to_id
    ).delete()
    db.commit()
四、模块 3：数据契约升级 (domain/entities/task_packet.py)
4.1 设计原则
保留旧字段兼容性：target_nodes、bonus_nodes、review_nodes 保留，标记 @deprecated
引入新结构化字段：_constraints 替代 target_nodes，提供更丰富语义
新增流转支撑字段：current_scenario_id、available_transitions
前后台一致：Flutter WebSocket 协议同步升级
4.2 新增数据类
@dataclass
class ScenarioConstraintItem:
    """
    场景约束项 — 替代原有的 node dict，提供更结构化的约束描述。
    
    结构化设计支撑 Director LLM 的双轨校验：
    - constraint_text: 目标表达原文
    - constraint_type: 检测粒度 (word/phrase/sentence)
    - depth_level: 绝对难度（支撑难度隔离）
    - is_satisfied: 当前轮是否已命中（运行时填充，不序列化）
    """
    constraint_id: int = 0           # 对应 ScenarioConstraint.id
    constraint_text: str = ""         # e.g., "medium"
    constraint_type: str = "word"      # word / phrase / sentence
    depth_level: int = 1              # CEFR 对齐
    weight: float = 1.0               # 命中权重
    hint_cn: Optional[str] = None    # 未命中时的中文提示
    
    # 以下字段在运行时由对话引擎填充，不参与序列化
    is_satisfied: bool = False        # 是否已在本次对话中命中
    hit_quality: float = 0.0         # 命中质量 (0.0~1.0)
    
    def to_dict(self) -> dict:
        return {
            "constraint_id": self.constraint_id,
            "constraint_text": self.constraint_text,
            "constraint_type": self.constraint_type,
            "depth_level": self.depth_level,
            "weight": self.weight,
            "hint_cn": self.hint_cn,
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> "ScenarioConstraintItem":
        return cls(
            constraint_id=d.get("constraint_id", 0),
            constraint_text=d.get("constraint_text", ""),
            constraint_type=d.get("constraint_type", "word"),
            depth_level=d.get("depth_level", 1),
            weight=d.get("weight", 1.0),
            hint_cn=d.get("hint_cn"),
        )
@dataclass
class MicroScenarioInfo:
    """
    当前微场景信息 — 支撑图谱流转。
    """
    scenario_id: int = 0
    scenario_code: str = ""
    scenario_name: str = ""
    intent_desc: str = ""
    depth_level: int = 1
    max_turns: int = 8
    
    # 可选的下一个微场景（来自 ScenarioTransition 表）
    available_transitions: list[dict] = field(default_factory=list)
    # 格式: [{"scenario_id": int, "scenario_name": str, "overlap_ratio": float}, ...]
    
    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "scenario_code": self.scenario_code,
            "scenario_name": self.scenario_name,
            "intent_desc": self.intent_desc,
            "depth_level": self.depth_level,
            "max_turns": self.max_turns,
            "available_transitions": self.available_transitions,
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> "MicroScenarioInfo":
        return cls(
            scenario_id=d.get("scenario_id", 0),
            scenario_code=d.get("scenario_code", ""),
            scenario_name=d.get("scenario_name", ""),
            intent_desc=d.get("intent_desc", ""),
            depth_level=d.get("depth_level", 1),
            max_turns=d.get("max_turns", 8),
            available_transitions=d.get("available_transitions", []),
        )
4.3 TaskPacket 升级
@dataclass
class TaskPacket:
    """
    LMS 下发给对话引擎的「作业单」— 阶段一升级版。
    
    新增字段说明：
    - current_scenario: 当前微场景信息（来自 MicroScenario 表）
    - constraints: 结构化约束列表（来自 ScenarioConstraint 表）
    - current_intent: 当前微场景的教学意图描述（给 Director LLM 看的核心目标）
    
    废弃字段说明（标记 @deprecated，阶段二完全移除）：
    - target_nodes / bonus_nodes / review_nodes
      → 替换为 constraints + bonus_constraints + review_constraints
    """
    
    # ── 话题基础信息（保留）───────────────────────────────────────────────
    topic_id: int = 0
    topic_title: str = "Daily Conversation"
    topic_title_zh: Optional[str] = None
    scene_prompt: str = "A casual daily conversation"
    role_name: str = "English Coach"
    learner_level: str = "Intermediate"
    voice: str = "Stanley"
    
    # ── 深度控制（保留）───────────────────────────────────────────────────
    depth_tier: int = 1
    max_reply_sentences: int = 3
    
    # ── 【新增】微场景信息 ─────────────────────────────────────────────────
    current_scenario: MicroScenarioInfo = field(default_factory=MicroScenarioInfo)
    
    # ── 【新增】结构化约束列表（替代 target_nodes） ─────────────────────────
    constraints: list[ScenarioConstraintItem] = field(default_factory=list)
    bonus_constraints: list[ScenarioConstraintItem] = field(default_factory=list)
    review_constraints: list[ScenarioConstraintItem] = field(default_factory=list)
    
    # ── 【新增】当前教学意图 ───────────────────────────────────────────────
    # 给 Director LLM 的双轨校验之一：判断此 intent 是否达成
    current_intent: str = ""
    
    # ── 【废弃 @deprecated】旧节点列表（向后兼容） ─────────────────────────
    # 阶段一：引擎优先读取 constraints，无则 fallback 到 target_nodes
    target_nodes: list = field(default_factory=list)
    bonus_nodes: list = field(default_factory=list)
    review_nodes: list = field(default_factory=list)
    
    # ── 难度开关（保留）───────────────────────────────────────────────────
    difficulty_config: DifficultyConfig = field(default_factory=DifficultyConfig)
    
    # ── 场景专属护栏（保留）───────────────────────────────────────────────
    scene_specific_rules: list = field(default_factory=list)
    vocab_tags: list = field(default_factory=list)
    sentence_patterns: list = field(default_factory=list)
    session_goal: str = ""
    
    # ── 序列化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = asdict(self)
        # MicroScenarioInfo 和 ScenarioConstraintItem 已在 asdict 中自动序列化
        return d
    
    @classmethod
    def from_dict(cls, d: dict) -> "TaskPacket":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        data = {k: v for k, v in d.items() if k in known}
        
        if "difficulty_config" in data and isinstance(data["difficulty_config"], dict):
            data["difficulty_config"] = DifficultyConfig.from_dict(data["difficulty_config"])
        
        if "current_scenario" in data and isinstance(data["current_scenario"], dict):
            data["current_scenario"] = MicroScenarioInfo.from_dict(data["current_scenario"])
        
        # 解析约束列表
        for field_name in ["constraints", "bonus_constraints", "review_constraints"]:
            if field_name in data and isinstance(data[field_name], list):
                data[field_name] = [
                    ScenarioConstraintItem.from_dict(c) if isinstance(c, dict) else c
                    for c in data[field_name]
                ]
        
        return cls(**data)
    
    # ── 辅助查询 ──────────────────────────────────────────────────────────
    @property
    def all_active_constraints(self) -> list[ScenarioConstraintItem]:
        """当前轮次活跃的所有约束（用于双轨校验）"""
        return self.constraints + self.review_constraints
    
    @property
    def all_practice_nodes(self) -> list:
        """【废弃兼容】旧版 target_nodes 兜底"""
        if self.constraints:
            return [c.to_dict() for c in self.constraints]
        return self.target_nodes + self.review_nodes
    
    def has_constraints(self) -> bool:
        return bool(self.constraints or self.review_constraints)
五、阶段一文件变更清单
文件路径	操作	变更说明
python_backend/database.py
修改
新增 MicroScenario、ScenarioConstraint、ScenarioTransition 三个 ORM 模型
python_backend/domain/entities/task_packet.py
修改
新增 ScenarioConstraintItem、MicroScenarioInfo 数据类；升级 TaskPacket
python_backend/scripts/build_scenario_graph.py
新增
离线图谱构建脚本
python_backend/scripts/add_manual_transition.py
新增
手动干预流转边的脚本（可选）
六、风险与依赖
风险	缓解措施
ScenarioTransition 数据量爆炸（两两遍历）
限制同 Topic + 同 Depth 内遍历；N ≤ 50 微场景/话题时 O(N²) 可接受
IoU 计算依赖约束文本质量
提供 hint_cn 和手动干预接口；算法结果需教学专家评审
旧 target_nodes fallback 逻辑复杂
阶段二统一废弃；阶段一只做兼容不减复杂度
Flutter 前端 WebSocket 协议需同步
前端新增字段采用可选（Optional），无则降级处理
七、验收标准

 database.py 可成功 Base.metadata.create_all() 不报错

 MicroScenario 可通过 db.query(MicroScenario).filter(...) 正确查询

 ScenarioTransition.overlap_ratio 可正确去重（无自环、无重复边）

 TaskPacket.from_dict() 可正确解析新旧两种数据格式

 build_scenario_graph.py --dry-run 输出符合预期的流转边候选
是否同意该方案并开始阶段一的编码？