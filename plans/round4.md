Flutter 前端适配方案（阶段四 Task 2）
一、现状分析
层次	文件	现状
WebSocket 接收层
chat_provider.dart
✅ 有 _initWebSocketListeners()，已处理 topic_changed、teaching_data 等事件
状态模型
ChatState（chat_provider.dart）
✅ 有 currentTopicTitle、currentRoleName、masteryProgress
状态管理器
ChatNotifier（Riverpod）
✅ Notifier 模式，支持 state.copyWith()
UI 视图层
chat_screen.dart
✅ 有 _buildMasteryTracker() 进度条，AppBar 显示话题标题
现有 ChatState 结构（需扩展的字段已标注）：

class ChatState {
  final ChatStatus status;
  final List<ChatTurn> chatHistory;
  final bool isFlipped;
  final double masteryProgress;
  final String? errorMessage;
  final SessionReport? sessionReport;
  final String currentTopicTitle;        // ✅ 已有
  final String currentTopicTitleZh;      // ✅ 已有
  final String currentRoleName;          // ✅ 已有
  final bool isGeneratingTopic;
  final bool isWaitingForTeachingData;
  // ── 以下为阶段三新增字段 ──────────────────────
  final String currentScenarioName;       // 🆕 新增
  final String currentIntent;            // 🆕 新增
  final double lastRewardDelta;          // 🆕 新增（过关奖励）
  final bool scenarioJustTransitioned;   // 🆕 新增（触发 UI 动效）
}
二、拟修改文件清单
文件	修改类型	修改内容摘要
lib/features/chat/providers/chat_provider.dart
核心修改
新增状态字段、解析 scenario_transition 事件、暴露 triggerScenarioTransitionEffect() 方法
lib/features/chat/presentation/chat_screen.dart
核心修改
新增目标卡片 Widget + AnimatedSwitcher 过渡动画 + SnackBar 闯关庆祝动画
lib/features/chat/widgets/scenario_goal_card.dart
🆕 新增
独立的小组件，封装目标卡片的动画切换逻辑
lib/features/chat/models/scenario_transition_model.dart
🆕 新增
流转事件数据模型（可选，仅用于类型安全）
三、详细实施方案
3.1 数据模型（scenario_transition_model.dart）
/// 阶段三新增：微场景流转事件数据模型
class ScenarioTransitionModel {
  final String previousScenarioCode;
  final String newScenarioCode;
  final String newScenarioName;
  final String newIntent;
  final double progressDelta;
  final String nextTopicSuggestion;
  factory ScenarioTransitionModel.fromJson(Map<String, dynamic> json) {
    return ScenarioTransitionModel(
      previousScenarioCode: json['previous_scenario_code'] ?? '',
      newScenarioCode: json['new_scenario_code'] ?? '',
      newScenarioName: json['new_scenario_name'] ?? '',
      newIntent: json['new_intent'] ?? '',
      progressDelta: (json['progress_delta'] as num?)?.toDouble() ?? 0.0,
      nextTopicSuggestion: json['next_topic_suggestion'] ?? '',
    );
  }
}
3.2 ChatState 新增字段
在 ChatState class 中新增 3 个字段：

// ── 【阶段三新增】微场景流转 UI 状态 ───────────────────────────────
final String currentScenarioName;       // 当前微场景名称（如 "Confirm Cup Size"）
final String currentIntent;              // 当前微场景意图描述
final double lastRewardDelta;           // 最近一次过关奖励分（触发 +分 动效）
final bool scenarioJustTransitioned;     // 本轮是否刚触发流转（触发闯卡通告）
同时新增 copyWith 参数：

ChatState copyWith({
  ...
  String? currentScenarioName,
  String? currentIntent,
  double? lastRewardDelta,
  bool? scenarioJustTransitioned,
})
3.3 WebSocket 事件解析（chat_provider.dart）
在 _initWebSocketListeners() 的 if-else 链中新增处理分支：

// ── 阶段三新增：微场景流转事件 ─────────────────────────────────
else if (data['event'] == 'scenario_transition') {
  final newScenarioName = data['new_scenario_name'] ?? '';
  final newIntent = data['new_intent'] ?? '';
  final rewardDelta = (data['progress_delta'] as num?)?.toDouble() ?? 0.0;
  state = state.copyWith(
    currentScenarioName: newScenarioName,
    currentIntent: newIntent,
    lastRewardDelta: rewardDelta,
    scenarioJustTransitioned: true,   // 通知 UI 播放闯卡通告
  );
}
同时在 ChatNotifier 中暴露一个方法，供 UI 消费完动效后重置标记：

/// 闯关动画播放完成后调用，关闭闯关动效标记
void clearScenarioTransitionFlag() {
  state = state.copyWith(scenarioJustTransitioned: false);
}
3.4 目标卡片组件（scenario_goal_card.dart）
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../providers/chat_provider.dart';
/// 微场景目标卡片
/// 显示当前微场景名称 + 意图描述
/// 当 scenarioJustTransitioned=True 时，使用 AnimatedSwitcher 淡入新内容
class ScenarioGoalCard extends ConsumerWidget {
  const ScenarioGoalCard({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final chatState = ref.watch(chatNotifierProvider);
    final scenarioName = chatState.currentScenarioName;
    final intent = chatState.currentIntent;
    return AnimatedSwitcher(
      duration: const Duration(milliseconds: 400),
      switchInCurve: Curves.easeOutCubic,
      switchOutCurve: Curves.easeInCubic,
      child: Container(
        key: ValueKey(scenarioName),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        decoration: BoxDecoration(
          color: Theme.of(context).colorScheme.primaryContainer,
          borderRadius: BorderRadius.circular(12),
        ),
        child: Row(
          children: [
            Icon(
              Icons.flag_rounded,
              size: 18,
              color: Theme.of(context).colorScheme.onPrimaryContainer,
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    scenarioName,
                    style: TextStyle(
                      fontWeight: FontWeight.w600,
                      color: Theme.of(context).colorScheme.onPrimaryContainer,
                    ),
                  ),
                  if (intent.isNotEmpty)
                    Text(
                      intent,
                      style: TextStyle(
                        fontSize: 12,
                        color: Theme.of(context).colorScheme.onPrimaryContainer.withOpacity(0.8),
                      ),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
3.5 ChatScreen 集成目标卡片
在 ChatScreen 的 Scaffold body 中，找一个合适位置插入目标卡片。建议放在 AppBar 下方、_buildMasteryTracker() 下方、或作为 chatHistory ListView 的 header：

Scaffold(
  appBar: AppBar(...),
  body: Column(
    children: [
      // ── 微场景目标卡片 ────────────────────────────────
      if (state.currentScenarioName.isNotEmpty)
        const Padding(
          padding: EdgeInsets.fromLTRB(12, 8, 12, 0),
          child: ScenarioGoalCard(),
        ),
      // ── 进度条 ──────────────────────────────────────
      _buildMasteryTracker(state),
      // ── 聊天历史 / 活跃回合 ─────────────────────────
      Expanded(child: ...),
    ],
  ),
)
3.6 闯关庆祝动画（ChatScreen 中集成）
在 ChatScreen 中，监听 scenarioJustTransitioned 状态，触发 SnackBar + 奖励分动画：

// 在 ChatScreen widget 顶层添加：
@override
void initState() {
  super.initState();
  WidgetsBinding.instance.addPostFrameCallback((_) {
    _watchScenarioTransition();
  });
}
void _watchScenarioTransition() {
  // 监听状态变化（用 ref.read 获取 notifier）
  ref.read(chatNotifierProvider.notifier).stream.listen((prev, next) {
    if (prev.scenarioJustTransitioned == false &&
        next.scenarioJustTransitioned == true) {
      _showScenarioTransitionCelebration(next);
      // 动画播放完成后清除标记
      Future.delayed(const Duration(seconds: 3), () {
        ref.read(chatNotifierProvider.notifier).clearScenarioTransitionFlag();
      });
    }
  });
}
void _showScenarioTransitionCelebration(ChatState state) {
  // ── 顶部 SnackBar 闯关庆祝（不打断对话流）──────────
  ScaffoldMessenger.of(context).showSnackBar(
    SnackBar(
      content: Row(
        children: [
          const Text('🎯 ', style: TextStyle(fontSize: 20)),
          Expanded(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  '场景通关！',
                  style: TextStyle(fontWeight: FontWeight.bold),
                ),
                Text(
                  '即将进入: ${state.currentScenarioName}',
                  style: const TextStyle(fontSize: 12),
                ),
              ],
            ),
          ),
          if (state.lastRewardDelta > 0)
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              decoration: BoxDecoration(
                color: Colors.amber,
                borderRadius: BorderRadius.circular(12),
              ),
              child: Text(
                '+${state.lastRewardDelta.toInt()}',
                style: const TextStyle(
                  fontWeight: FontWeight.bold,
                  color: Colors.black87,
                ),
              ),
            ),
        ],
      ),
      behavior: SnackBarBehavior.floating,
      duration: const Duration(seconds: 3),
      margin: const EdgeInsets.all(16),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      backgroundColor: const Color(0xFF1A1A2E),
    ),
  );
}
关键设计原则：所有动画均为非阻塞的 Floating SnackBar + AnimatedSwitcher，绝不打断聊天记录流，用户感知极度顺滑。

3.7 topic_changed 事件的向后兼容
原有 topic_changed 事件继续保留（处理话题整体切换场景），新增 scenario_transition 专门处理微场景内部流转。两者互不干扰。

四、完整代码变更速查
文件	改动行数	改动摘要
scenario_transition_model.dart
🆕 新增 ~40行
数据模型
scenario_goal_card.dart
🆕 新增 ~60行
目标卡片 Widget
chat_provider.dart
~5处改动
新增 3 字段 + scenario_transition 事件分支 + clearScenarioTransitionFlag()
chat_screen.dart
~15行改动
插入 ScenarioGoalCard + _showScenarioTransitionCelebration() 集成
五、动画效果说明
用户说 "Americano please" → 命中所有约束 → 后端下发 scenario_transition
                                              ↓
                            聊天记录继续显示，不中断
                                              ↓
                            SnackBar 从顶部滑入:
                            ┌────────────────────────────────────┐
                            │ 🎯 场景通关！                     │
                            │ 即将进入: Confirm Drink Temperature │
                            │                              [+25] │
                            └────────────────────────────────────┘
                                              ↓
                            ScenarioGoalCard 淡出旧内容 → 淡入新内容（400ms）
                                              ↓
                            3秒后 SnackBar 自动消失 → 正常继续对话
是否同意该前端方案并开始编码？