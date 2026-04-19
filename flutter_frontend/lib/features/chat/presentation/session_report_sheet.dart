// lib/features/chat/presentation/session_report_sheet.dart
//
// 学习报告卡底部弹出面板。
// 两阶段刷新：preliminary（立刻显示 L1 数据）→ final（L2 完成后安静更新质量分）。
// 通过 ConsumerWidget 直接 watch chatProvider，final 到达时自动 rebuild。

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../../core/providers/settings_provider.dart';
import '../providers/chat_provider.dart'; // re-exports session_report_model.dart

// ─────────────────────────────────────────────────────────────────────────────
// 品牌色
// ─────────────────────────────────────────────────────────────────────────────
const _kGreen = Color(0xFF4CAF50);
const _kGreenLight = Color(0xFFE8F5E9);
const _kBlue = Color(0xFF2196F3);
const _kOrange = Color(0xFFFF9800);
const _kGrey = Color(0xFFBDBDBD);
const _kBg = Color(0xFFF8F9FA);

// ─────────────────────────────────────────────────────────────────────────────
// 入口：showSessionReportSheet
// ─────────────────────────────────────────────────────────────────────────────
Future<void> showSessionReportSheet(BuildContext context) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.transparent,
    enableDrag: true,
    builder: (_) => const _SessionReportSheetWrapper(),
  );
}

class _SessionReportSheetWrapper extends ConsumerWidget {
  const _SessionReportSheetWrapper();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final report = ref.watch(chatProvider).sessionReport;
    if (report == null) return const SizedBox.shrink();
    return _SessionReportContent(report: report);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 主体内容
// ─────────────────────────────────────────────────────────────────────────────
class _SessionReportContent extends ConsumerWidget {
  final SessionReport report;

  const _SessionReportContent({required this.report});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final mq = MediaQuery.of(context);

    return DraggableScrollableSheet(
      initialChildSize: 0.88,
      maxChildSize: 0.96,
      minChildSize: 0.55,
      snap: true,
      snapSizes: const [0.55, 0.88, 0.96],
      builder: (_, scrollController) {
        return Container(
          decoration: const BoxDecoration(
            color: Colors.white,
            borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
          ),
          child: Column(
            children: [
              const _DragHandle(),
              Expanded(
                child: ListView(
                  controller: scrollController,
                  padding: EdgeInsets.only(
                    left: 20,
                    right: 20,
                    bottom: mq.padding.bottom + 20,
                  ),
                  children: [
                    _buildHeader(context, ref, report),
                    const SizedBox(height: 24),
                    _buildScoreRow(ref, report),
                    const SizedBox(height: 24),
                    _buildTierSection(ref, report),
                    const SizedBox(height: 24),
                    _buildNodesSection(ref, report),
                    if (report.isFinal && report.avgQuality != null) ...[
                      const SizedBox(height: 20),
                      _buildQualityBadge(ref, report),
                    ],
                    const SizedBox(height: 24),
                    _buildEncouragement(ref, report),
                    const SizedBox(height: 24),
                    _buildContinueButton(context, ref),
                  ],
                ),
              ),
            ],
          ),
        );
      },
    );
  }

  Widget _buildHeader(BuildContext context, WidgetRef ref, SessionReport report) {
    return Column(
      children: [
        const SizedBox(height: 8),
        Row(
          children: [
            const Text('\u{1F393}', style: TextStyle(fontSize: 26)),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    tr(ref, 'Session complete', '\u672C\u5C40\u5B8C\u6210'),
                    style: TextStyle(
                      fontSize: 13,
                      color: Colors.grey[500],
                      letterSpacing: 0.8,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    chatTopicDisplayTitle(
                      ref,
                      report.topicTitle,
                      titleZh: report.topicTitleZh,
                    ),
                    style: const TextStyle(
                      fontSize: 20,
                      fontWeight: FontWeight.bold,
                      color: Colors.black87,
                    ),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                  ),
                ],
              ),
            ),
            _TierBadge(tier: report.depthTier),
          ],
        ),
        if (report.isFinal) ...[
          const SizedBox(height: 10),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            decoration: BoxDecoration(
              color: _kBlue.withOpacity(0.1),
              borderRadius: BorderRadius.circular(20),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.verified_rounded, size: 14, color: _kBlue),
                const SizedBox(width: 6),
                Text(
                  tr(ref, 'AI-verified quality analysis ready', 'AI \u5DF2\u5B8C\u6210\u53E3\u8BED\u54C1\u8D28\u5206\u6790'),
                  style: const TextStyle(fontSize: 12, color: _kBlue, fontWeight: FontWeight.w600),
                ),
              ],
            ),
          ),
        ],
      ],
    );
  }

  Widget _buildScoreRow(WidgetRef ref, SessionReport report) {
    return Row(
      children: [
        _ScoreCircle(score: report.sessionScore),
        const SizedBox(width: 20),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              _StatRow(
                icon: Icons.check_circle_rounded,
                color: _kGreen,
                label: tr(ref, 'Expressions hit', '\u547D\u4E2D\u8868\u8FBE'),
                value: '${report.hitCount} / ${report.totalNodes}',
              ),
              const SizedBox(height: 10),
              if (report.newlyMastered.isNotEmpty) ...[
                _StatRow(
                  icon: Icons.star_rounded,
                  color: _kOrange,
                  label: tr(ref, 'Newly mastered', '\u65B0\u638C\u63E1'),
                  value: report.newlyMastered.length.toString(),
                ),
                const SizedBox(height: 10),
              ],
              _StatRow(
                icon: Icons.layers_rounded,
                color: _kBlue,
                label: tr(ref, 'Depth tier', '\u96BE\u5EA6\u5C42\u7EA7'),
                value: tr(ref, 'Tier ${report.depthTier}', '\u7B2C ${report.depthTier} \u5C42'),
              ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildTierSection(WidgetRef ref, SessionReport report) {
    final ts = report.tierStatus;
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: ts.unlockedNextTier ? _kGreenLight : _kBg,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(
          color: ts.unlockedNextTier ? _kGreen.withOpacity(0.3) : Colors.transparent,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                ts.unlockedNextTier
                    ? tr(ref, '\u{1F389} Tier ${ts.tier + 1} Unlocked!', '\u{1F389} \u5DF2\u89E3\u9501\u7B2C ${ts.tier + 1} \u5C42\uFF01')
                    : tr(ref, 'Tier ${ts.tier} progress', '\u7B2C ${ts.tier} \u5C42\u8FDB\u5EA6'),
                style: TextStyle(
                  fontSize: 14,
                  fontWeight: FontWeight.bold,
                  color: ts.unlockedNextTier ? _kGreen : Colors.black87,
                ),
              ),
              Text(
                '${ts.avgEffectiveMastery.toInt()} / ${ts.threshold.toInt()}',
                style: TextStyle(
                  fontSize: 13,
                  color: ts.unlockedNextTier ? _kGreen : Colors.grey[600],
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          ClipRRect(
            borderRadius: BorderRadius.circular(8),
            child: LinearProgressIndicator(
              value: ts.progressRatio,
              minHeight: 10,
              backgroundColor: Colors.grey.shade200,
              valueColor: AlwaysStoppedAnimation(
                ts.unlockedNextTier ? _kGreen : _kBlue,
              ),
            ),
          ),
          if (!ts.unlockedNextTier) ...[
            const SizedBox(height: 8),
            Text(
              ref.watch(settingsProvider).isChinese
                  ? '\u8FD8\u9700 ${(ts.threshold - ts.avgEffectiveMastery).clamp(0, 100).toInt()} \u70B9\u638C\u63E1\u5EA6\u5373\u53EF\u89E3\u9501\u7B2C ${ts.tier + 1} \u5C42'
                  : '${(ts.threshold - ts.avgEffectiveMastery).clamp(0, 100).toInt()} more mastery points to unlock Tier ${ts.tier + 1}',
              style: TextStyle(fontSize: 12, color: Colors.grey[500]),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildNodesSection(WidgetRef ref, SessionReport report) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          tr(ref, 'Expressions practiced', '\u672C\u5C40\u7EC3\u4E60\u7684\u8868\u8FBE'),
          style: TextStyle(
            fontSize: 14,
            fontWeight: FontWeight.bold,
            color: Colors.grey[700],
          ),
        ),
        const SizedBox(height: 12),
        ...report.nodes.map((node) => Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: _NodeCard(node: node),
            )),
      ],
    );
  }

  Widget _buildQualityBadge(WidgetRef ref, SessionReport report) {
    final q = report.avgQuality!;
    final label = report.qualityLabel ?? 'good';
    final (color, icon) = _qualityColorAndIcon(label);
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: color.withOpacity(0.08),
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: color.withOpacity(0.2)),
      ),
      child: Row(
        children: [
          Icon(icon, color: color, size: 24),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  tr(ref, 'Overall speaking quality', '\u53E3\u8BED\u6574\u4F53\u54C1\u8D28'),
                  style: TextStyle(fontSize: 12, color: Colors.grey[600]),
                ),
                const SizedBox(height: 4),
                Text(
                  _qualityLabelText(ref, label),
                  style: TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.bold,
                    color: color,
                  ),
                ),
              ],
            ),
          ),
          Text(
            '${(q * 100).toInt()}%',
            style: TextStyle(
              fontSize: 22,
              fontWeight: FontWeight.bold,
              color: color,
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildEncouragement(WidgetRef ref, SessionReport report) {
    final isZh = ref.watch(settingsProvider).isChinese;
    final zhEnc = report.encouragementZh?.trim();
    final body = (isZh && zhEnc != null && zhEnc.isNotEmpty)
        ? zhEnc
        : report.encouragement;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          colors: [
            const Color(0xFF667eea).withOpacity(0.08),
            const Color(0xFF764ba2).withOpacity(0.06),
          ],
        ),
        borderRadius: BorderRadius.circular(16),
      ),
      child: Text(
        body,
        textAlign: TextAlign.center,
        style: const TextStyle(
          fontSize: 14,
          height: 1.5,
          color: Color(0xFF4A4A6A),
          fontWeight: FontWeight.w500,
        ),
      ),
    );
  }

  Widget _buildContinueButton(BuildContext context, WidgetRef ref) {
    final isZh = ref.watch(settingsProvider).isChinese;
    return SizedBox(
      width: double.infinity,
      height: 52,
      child: ElevatedButton(
        onPressed: () => Navigator.of(context).pop(),
        style: ElevatedButton.styleFrom(
          backgroundColor: _kBlue,
          foregroundColor: Colors.white,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
          elevation: 0,
        ),
        child: Text(
          isZh ? '\u7EE7\u7EED\u7EC3\u4E60' : 'Keep practicing',
          style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
        ),
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 子组件（均使用 ConsumerWidget，避免存储 WidgetRef 字段）
// ─────────────────────────────────────────────────────────────────────────────

class _DragHandle extends StatelessWidget {
  const _DragHandle();

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 12),
      child: Center(
        child: Container(
          width: 40,
          height: 4,
          decoration: BoxDecoration(
            color: Colors.grey.shade300,
            borderRadius: BorderRadius.circular(2),
          ),
        ),
      ),
    );
  }
}

class _TierBadge extends ConsumerWidget {
  final int tier;
  const _TierBadge({required this.tier});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
      decoration: BoxDecoration(
        color: _kBlue.withOpacity(0.1),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Text(
        tr(ref, 'Tier $tier', '\u7B2C $tier \u5C42'),
        style: const TextStyle(
          fontSize: 12,
          fontWeight: FontWeight.bold,
          color: _kBlue,
        ),
      ),
    );
  }
}

class _ScoreCircle extends ConsumerWidget {
  final double score;
  const _ScoreCircle({required this.score});

  Color get _color {
    if (score >= 80) return _kGreen;
    if (score >= 60) return _kBlue;
    if (score >= 40) return _kOrange;
    return _kGrey;
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return SizedBox(
      width: 90,
      height: 90,
      child: Stack(
        alignment: Alignment.center,
        children: [
          CircularProgressIndicator(
            value: score / 100.0,
            strokeWidth: 8,
            backgroundColor: Colors.grey.shade200,
            valueColor: AlwaysStoppedAnimation(_color),
            strokeCap: StrokeCap.round,
          ),
          Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                '${score.toInt()}',
                style: TextStyle(
                  fontSize: 24,
                  fontWeight: FontWeight.bold,
                  color: _color,
                ),
              ),
              Text(
                tr(ref, 'pts', '\u5206'),
                style: TextStyle(fontSize: 10, color: Colors.grey[500]),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _StatRow extends StatelessWidget {
  final IconData icon;
  final Color color;
  final String label;
  final String value;
  const _StatRow({
    required this.icon,
    required this.color,
    required this.label,
    required this.value,
  });

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Icon(icon, size: 16, color: color),
        const SizedBox(width: 8),
        Text(label, style: TextStyle(fontSize: 13, color: Colors.grey[600])),
        const Spacer(),
        Text(
          value,
          style: const TextStyle(fontSize: 13, fontWeight: FontWeight.bold, color: Colors.black87),
        ),
      ],
    );
  }
}

class _NodeCard extends ConsumerWidget {
  final SessionNodeReport node;
  const _NodeCard({required this.node});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final hit = node.hit;
    final bgColor = hit ? _kGreenLight : const Color(0xFFF5F5F5);
    final borderColor = hit ? _kGreen.withOpacity(0.3) : Colors.transparent;
    final (qualityColor, _) = node.qualityLabel != null
        ? _qualityColorAndIcon(node.qualityLabel!)
        : (Colors.transparent, Icons.circle);

    return AnimatedContainer(
      duration: const Duration(milliseconds: 400),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: bgColor,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: borderColor),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // ── 表达文字 + 命中图标 ──────────────────────────────
          Row(
            children: [
              Icon(
                hit ? Icons.check_circle_rounded : Icons.radio_button_unchecked_rounded,
                size: 18,
                color: hit ? _kGreen : _kGrey,
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  '\"${node.text}\"',
                  style: TextStyle(
                    fontSize: 15,
                    fontWeight: FontWeight.bold,
                    color: hit ? Colors.black87 : Colors.grey[500],
                  ),
                ),
              ),
              // Delta badge
              if (hit && node.masteryDelta > 0)
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                  decoration: BoxDecoration(
                    color: _kGreen.withOpacity(0.15),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    '+${node.masteryDelta.toInt()}',
                    style: const TextStyle(
                      fontSize: 12,
                      fontWeight: FontWeight.bold,
                      color: _kGreen,
                    ),
                  ),
                ),
              // Milestone badge
              if (node.milestoneReached) ...[
                const SizedBox(width: 6),
                const Icon(Icons.star_rounded, size: 16, color: _kOrange),
              ],
            ],
          ),
          // ── 掌握度进度条 ────────────────────────────────
          if (hit) ...[
            const SizedBox(height: 10),
            _MasteryBar(before: node.masteryBefore, after: node.masteryNow),
          ],
          // ── L2 质量标签 ───────────────────────────────
          if (node.qualityLabel != null && node.qualityLabel != 'not_used') ...[
            const SizedBox(height: 8),
            Row(
              children: [
                Container(
                  width: 8,
                  height: 8,
                  decoration: BoxDecoration(
                    color: qualityColor,
                    shape: BoxShape.circle,
                  ),
                ),
                const SizedBox(width: 6),
                Text(
                  _qualityLabelText(ref, node.qualityLabel!),
                  style: TextStyle(
                    fontSize: 12,
                    color: qualityColor,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                if (node.quality != null) ...[
                  const SizedBox(width: 4),
                  Text(
                    '(${(node.quality! * 100).toInt()}%)',
                    style: TextStyle(fontSize: 11, color: Colors.grey[500]),
                  ),
                ],
              ],
            ),
          ],
        ],
      ),
    );
  }
}

class _MasteryBar extends StatelessWidget {
  final double before;
  final double after;
  const _MasteryBar({required this.before, required this.after});

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(
              'Mastery',
              style: TextStyle(fontSize: 11, color: Colors.grey[500]),
            ),
            Text(
              '${after.toInt()}%',
              style: const TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: _kGreen),
            ),
          ],
        ),
        const SizedBox(height: 5),
        Stack(
          children: [
            // Background
            ClipRRect(
              borderRadius: BorderRadius.circular(6),
              child: Container(height: 7, color: Colors.grey.shade200),
            ),
            // Before (lighter)
            FractionallySizedBox(
              widthFactor: (before / 100).clamp(0.0, 1.0),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(6),
                child: Container(
                  height: 7,
                  color: _kGreen.withOpacity(0.35),
                ),
              ),
            ),
            // After (full)
            FractionallySizedBox(
              widthFactor: (after / 100).clamp(0.0, 1.0),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(6),
                child: Container(
                  height: 7,
                  decoration: const BoxDecoration(
                    gradient: LinearGradient(
                      colors: [Color(0xFF66BB6A), _kGreen],
                    ),
                    borderRadius: BorderRadius.all(Radius.circular(6)),
                  ),
                ),
              ),
            ),
          ],
        ),
      ],
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 质量标签工具函数
// ─────────────────────────────────────────────────────────────────────────────
(Color, IconData) _qualityColorAndIcon(String label) {
  return switch (label) {
    'excellent' => (const Color(0xFF2E7D32), Icons.star_rounded),
    'good'      => (_kGreen, Icons.thumb_up_rounded),
    'fair'      => (_kOrange, Icons.thumbs_up_down_rounded),
    'needs_work' => (Colors.redAccent, Icons.refresh_rounded),
    _           => (_kGrey, Icons.remove_circle_outline_rounded),
  };
}

String _qualityLabelText(WidgetRef ref, String label) {
  return switch (label) {
    'excellent' => tr(ref, 'Excellent \u2014 natural & correct', '\u4F18\u79C0\uFF1A\u81EA\u7136\u4E14\u51C6\u786E'),
    'good' => tr(ref, 'Good \u2014 correct', '\u826F\u597D\uFF1A\u8868\u8FBE\u6B63\u786E'),
    'fair' => tr(ref, 'Fair \u2014 needs polish', '\u4E00\u822C\uFF1A\u8FD8\u53EF\u6253\u78E8'),
    'needs_work' => tr(ref, 'Needs more practice', '\u9700\u52A0\u5F3A\u7EC3\u4E60'),
    _ => tr(ref, 'Not attempted', '\u672A\u8BC4\u4F30'),
  };
}
