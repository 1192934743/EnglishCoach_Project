// lib/features/chat/widgets/scenario_goal_card.dart
//
// 【阶段三新增】微场景目标卡片
//
// 功能：
//   - 显示当前微场景名称（scenario_name）和教学意图描述（intent）
//   - 当 upstream ChatState 发生变化时（通过 Consumer 触发 rebuild），
//     使用 AnimatedSwitcher 实现平滑的淡入淡出过渡动画
//   - 降级保护：当 currentScenarioName 为空时，隐藏整张卡片
//
// Riverpod 最佳实践说明：
//   - 本组件是纯展示组件，仅读取 ChatState，不写状态
//   - 不持有任何一次性动作标志（如 scenarioJustTransitioned）
//   - 一次性 UI 副作用（SnackBar 等）统一由 ChatScreen 中的 ref.listen 处理
//

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../providers/chat_provider.dart';

class ScenarioGoalCard extends ConsumerWidget {
  const ScenarioGoalCard({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final chatState = ref.watch(chatProvider);
    final scenarioName = chatState.currentScenarioName;
    final intent = chatState.currentIntent;

    // ── 降级保护：当无微场景数据时隐藏卡片 ───────────────────────────
    // 旧话题或未启用微场景模式时，currentScenarioName 为空字符串，
    // 此时不渲染任何内容（而不是显示降级的 topic title）。
    if (scenarioName.isEmpty) {
      return const SizedBox.shrink();
    }

    return AnimatedSwitcher(
      duration: const Duration(milliseconds: 400),
      switchInCurve: Curves.easeOutCubic,
      switchOutCurve: Curves.easeInCubic,
      child: Container(
        key: ValueKey(scenarioName),
        width: double.infinity,
        margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          gradient: LinearGradient(
            colors: [
              const Color(0xFF667eea).withValues(alpha: 0.12),
              const Color(0xFF764ba2).withValues(alpha: 0.08),
            ],
          ),
          borderRadius: BorderRadius.circular(14),
          border: Border.all(
            color: const Color(0xFF667eea).withValues(alpha: 0.25),
          ),
        ),
        child: Row(
          children: [
            // 旗子图标
            Container(
              width: 28,
              height: 28,
              decoration: BoxDecoration(
                color: const Color(0xFF667eea).withValues(alpha: 0.15),
                borderRadius: BorderRadius.circular(8),
              ),
              child: const Icon(
                Icons.flag_rounded,
                size: 16,
                color: Color(0xFF667eea),
              ),
            ),
            const SizedBox(width: 10),
            // 场景名 + 意图
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    scenarioName,
                    style: const TextStyle(
                      fontSize: 13,
                      fontWeight: FontWeight.w700,
                      color: Color(0xFF333333),
                      letterSpacing: 0.3,
                    ),
                  ),
                  if (intent.isNotEmpty) ...[
                    const SizedBox(height: 2),
                    Text(
                      intent,
                      style: TextStyle(
                        fontSize: 11,
                        color: Colors.grey.shade600,
                        height: 1.3,
                      ),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ],
                ],
              ),
            ),
            // 右上角"进行中"标签
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
              decoration: BoxDecoration(
                color: const Color(0xFF667eea).withValues(alpha: 0.1),
                borderRadius: BorderRadius.circular(6),
              ),
              child: const Text(
                '进行中',
                style: TextStyle(
                  fontSize: 10,
                  fontWeight: FontWeight.w600,
                  color: Color(0xFF667eea),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
