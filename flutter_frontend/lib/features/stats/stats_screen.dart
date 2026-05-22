// lib/features/stats/stats_screen.dart
//
// Learning Statistics screen.
// Shows: streak, total sessions, expressions mastered, per-topic progress.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../core/providers/settings_provider.dart';
import '../../core/network/api_client.dart';
import '../../core/network/user_manager.dart';
import '../../core/theme/app_colors.dart';
// ── Provider ──────────────────────────────────────────────────────────────
final statsProvider = FutureProvider<Map<String, dynamic>>((ref) async {
  final userId = await UserManager.getOrCreateUuid();
  return ApiClient.getStats(userId: userId);
});

// ── Screen ────────────────────────────────────────────────────────────────
class StatsScreen extends ConsumerWidget {
  const StatsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final statsAsync = ref.watch(statsProvider);

    return Scaffold(
      backgroundColor: AppColors.background,
      body: SafeArea(
        child: statsAsync.when(
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => _buildError(ref, e),
          data: (data) => RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(statsProvider);
              await ref.read(statsProvider.future);
            },
            child: _buildContent(context, ref, data),
          ),
        ),
      ),
    );
  }

  Widget _buildError(WidgetRef ref, Object e) => Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.wifi_off_rounded, size: 48, color: AppColors.textTertiary),
            const SizedBox(height: 12),
            Text(
              '${tr(ref, 'Could not load stats.', '统计数据加载失败。')}\n$e',
              textAlign: TextAlign.center,
              style: TextStyle(color: AppColors.textSecondary),
            ),
            const SizedBox(height: 16),
            ElevatedButton(
              onPressed: () => ref.invalidate(statsProvider),
              child: Text(tr(ref, 'Retry', '重试')),
            ),
          ],
        ),
      );

  Widget _buildContent(BuildContext context, WidgetRef ref, Map<String, dynamic> data) {
    final sessions = data['total_sessions'] as int? ?? 0;
    final practiced = data['total_expressions_practiced'] as int? ?? 0;
    final mastered = data['total_expressions_mastered'] as int? ?? 0;
    final topics = data['topics_touched'] as int? ?? 0;
    final streak = data['current_streak_days'] as int? ?? 0;
    final recentSessions = (data['recent_sessions'] as List?)?.cast<Map<String, dynamic>>() ?? [];
    final topicsSummary = (data['topics_summary'] as List?)?.cast<Map<String, dynamic>>() ?? [];

    final bottomInset = MediaQuery.paddingOf(context).bottom;
    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: EdgeInsets.fromLTRB(16, 16, 16, 20 + bottomInset),
      children: [
        // Streak hero card
        _buildStreakCard(ref, streak),
        const SizedBox(height: 16),
        // 4-stat grid
        _buildStatsGrid(context, ref, sessions, practiced, mastered, topics),
        const SizedBox(height: 20),
        // Topic progress
        if (topicsSummary.isNotEmpty) ...[
          _sectionTitle(tr(ref, 'Topic Mastery', '话题掌握度')),
          const SizedBox(height: 10),
          ...topicsSummary.map((t) => Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: _TopicMasteryBar(
              ref: ref,
              title: chatTopicDisplayTitle(
                ref,
                t['topic_title'] as String,
                titleZh: t['topic_title_zh'] as String?,
              ),
              category: t['category'] as String? ?? '',
              avgMastery: (t['avg_mastery'] as num?)?.toDouble() ?? 0.0,
              nodesPracticed: t['nodes_practiced'] as int? ?? 0,
              totalNodes: t['total_nodes'] as int? ?? 1,
            ),
          )),
          const SizedBox(height: 10),
        ],
        // Recent sessions
        if (recentSessions.isNotEmpty) ...[
          _sectionTitle(tr(ref, 'Recent Sessions', '最近练习')),
          const SizedBox(height: 10),
          ...recentSessions.map((s) => Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: _RecentSessionTile(ref: ref, session: s),
          )),
        ],
        if (sessions == 0) _buildEmptyState(ref),
      ],
    );
  }

  Widget _buildStreakCard(WidgetRef ref, int streak) {
    final hasStreak = streak > 0;
    return LayoutBuilder(
      builder: (context, constraints) {
        final narrow = constraints.maxWidth < 340;
        final titleSize = narrow ? 18.0 : 22.0;
        final subSize = narrow ? 12.0 : 13.0;
        return Container(
          width: double.infinity,
          padding: EdgeInsets.fromLTRB(narrow ? 14 : 20, 16, narrow ? 14 : 20, 16),
          decoration: BoxDecoration(
            gradient: LinearGradient(
              colors: hasStreak
                  ? [const Color(0xFFFF9800), const Color(0xFFFF5722)]
                  : [const Color(0xFF2196F3), const Color(0xFF7C3AED)],
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
            ),
            borderRadius: BorderRadius.circular(20),
          ),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                hasStreak ? '🔥' : '📚',
                style: TextStyle(fontSize: narrow ? 34 : 40),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      hasStreak
                          ? tr(ref, '$streak-Day Streak!', '已连续 $streak 天！')
                          : tr(ref, 'Start Your Streak', '开启连续打卡'),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: titleSize,
                        fontWeight: FontWeight.bold,
                        color: Colors.white,
                        height: 1.2,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      hasStreak
                          ? tr(ref, 'Keep it up — practice again today!', '太棒了，今天也来练一局吧！')
                          : tr(ref, 'Practice every day to build a streak.', '每天坚持练习即可累积打卡天数。'),
                      maxLines: 3,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: subSize,
                        height: 1.25,
                        color: Colors.white.withValues(alpha: 0.9),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        );
      },
    );
  }

  /// 四宫格用固定行高，避免 `childAspectRatio` 在窄屏/系统大字下出现几像素级纵向溢出。
  Widget _buildStatsGrid(
    BuildContext context,
    WidgetRef ref,
    int sessions,
    int practiced,
    int mastered,
    int topics,
  ) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final w = constraints.maxWidth;
        final textScale = MediaQuery.textScalerOf(context).scale(1.0);
        final cellWidth = (w - 10) / 2;
        final baseH = cellWidth / 1.05;
        final mainExtent = (baseH * textScale).clamp(92.0, 152.0);
        return GridView(
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
            crossAxisCount: 2,
            crossAxisSpacing: 10,
            mainAxisSpacing: 10,
            mainAxisExtent: mainExtent,
          ),
          children: [
            _StatCard(
              icon: Icons.play_circle_outline_rounded,
              color: Colors.blueAccent,
              label: tr(ref, 'Sessions', '练习局数'),
              value: '$sessions',
            ),
            _StatCard(
              icon: Icons.record_voice_over_rounded,
              color: Colors.teal,
              label: tr(ref, 'Expressions tried', '已练表达'),
              value: '$practiced',
            ),
            _StatCard(
              icon: Icons.star_rounded,
              color: Colors.orange,
              label: tr(ref, 'Expressions solid', '表达已较熟'),
              value: '$mastered',
            ),
            _StatCard(
              icon: Icons.topic_rounded,
              color: Colors.purple,
              label: tr(ref, 'Topics touched', '涉及话题'),
              value: '$topics',
            ),
          ],
        );
      },
    );
  }

  Widget _buildEmptyState(WidgetRef ref) => Center(
        child: Padding(
          padding: const EdgeInsets.only(top: 40),
          child: Column(
            children: [
              const Text('🎯', style: TextStyle(fontSize: 56)),
              const SizedBox(height: 16),
              Text(
                tr(ref, 'No sessions yet', '还没有练习记录'),
                style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 8),
              Text(
                tr(
                  ref,
                  'Go to the Chat tab and start your first practice session!',
                  '打开「对练」标签，开始你的第一场练习吧！',
                ),
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 14, color: Colors.grey[500]),
              ),
            ],
          ),
        ),
      );

  Widget _sectionTitle(String title) => Text(
        title,
        style: TextStyle(
          fontSize: 16,
          fontWeight: FontWeight.bold,
          color: Colors.grey[800],
        ),
      );
}

// ── Sub-widgets ───────────────────────────────────────────────────────────

class _StatCard extends StatelessWidget {
  final IconData icon;
  final Color color;
  final String label;
  final String value;

  const _StatCard({
    required this.icon,
    required this.color,
    required this.label,
    required this.value,
  });

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(16),
          boxShadow: [
            BoxShadow(
              color: Colors.black.withValues(alpha: 0.04),
              blurRadius: 6,
              offset: const Offset(0, 2),
            ),
          ],
        ),
        child: LayoutBuilder(
          builder: (context, constraints) {
            return FittedBox(
              fit: BoxFit.scaleDown,
              alignment: Alignment.topLeft,
              child: ConstrainedBox(
                constraints: BoxConstraints(maxWidth: constraints.maxWidth),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(icon, color: color, size: 20),
                    const SizedBox(height: 4),
                    Text(
                      value,
                      style: TextStyle(
                        fontSize: 22,
                        fontWeight: FontWeight.bold,
                        color: color,
                      ),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    Text(
                      label,
                      style: TextStyle(fontSize: 10, color: Colors.grey[500], height: 1.2),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ],
                ),
              ),
            );
          },
        ),
      );
}

class _TopicMasteryBar extends StatelessWidget {
  final WidgetRef ref;
  final String title;
  final String category;
  final double avgMastery;
  final int nodesPracticed;
  final int totalNodes;

  const _TopicMasteryBar({
    required this.ref,
    required this.title,
    required this.category,
    required this.avgMastery,
    required this.nodesPracticed,
    required this.totalNodes,
  });

  @override
  Widget build(BuildContext context) {
    final ratio = (avgMastery / 100.0).clamp(0.0, 1.0);
    final color = ratio >= (kMasteryAutoPickSoftCapPercent / 100.0)
        ? Colors.green
        : (ratio >= (kMasteryTierUpThresholdPercent / 100.0)
            ? Colors.blueAccent
            : Colors.orange);

    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Text(
                  title,
                  style: const TextStyle(fontSize: 14, fontWeight: FontWeight.bold),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              Text(
                '${avgMastery.toInt()}%',
                style: TextStyle(fontSize: 14, fontWeight: FontWeight.bold, color: color),
              ),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            '$category · $nodesPracticed/$totalNodes ${tr(ref, 'expressions', '条表达')}',
            style: TextStyle(fontSize: 11, color: Colors.grey[500], height: 1.2),
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
          ),
          const SizedBox(height: 8),
          ClipRRect(
            borderRadius: BorderRadius.circular(4),
            child: LinearProgressIndicator(
              value: ratio,
              minHeight: 6,
              backgroundColor: Colors.grey.shade100,
              valueColor: AlwaysStoppedAnimation<Color>(color),
            ),
          ),
        ],
      ),
    );
  }
}

class _RecentSessionTile extends StatelessWidget {
  final WidgetRef ref;
  final Map<String, dynamic> session;

  const _RecentSessionTile({required this.ref, required this.session});

  @override
  Widget build(BuildContext context) {
    final title = chatTopicDisplayTitle(
      ref,
      session['topic_title'] as String? ?? tr(ref, 'Unknown', '未知'),
      titleZh: session['topic_title_zh'] as String?,
    );
    final tier = session['depth_tier'] as int? ?? 1;
    final mastered = session['nodes_mastered'] as int? ?? 0;
    final date = session['date'] as String?;
    final quality = (session['quality'] as num?)?.toDouble();

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        children: [
          Container(
            width: 36, height: 36,
            decoration: BoxDecoration(
              color: Colors.blueAccent.withValues(alpha: 0.1),
              shape: BoxShape.circle,
            ),
            child: Center(
              child: Text(
                tr(ref, 'T$tier', '第$tier层'),
                style: const TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.bold,
                  color: Colors.blueAccent,
                ),
              ),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  title,
                  style: const TextStyle(fontSize: 13, fontWeight: FontWeight.bold),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                Text(
                  '$mastered ${tr(ref, 'expressions mastered', '条表达已掌握')}'
                  '${quality != null ? ' · ${tr(ref, 'Quality', '质量')}: ${(quality * 100).toInt()}%' : ''}',
                  style: TextStyle(fontSize: 11, color: Colors.grey[500], height: 1.2),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
              ],
            ),
          ),
          if (date != null)
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 76),
              child: Text(
                _formatDate(ref, date),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                textAlign: TextAlign.end,
                style: TextStyle(fontSize: 11, color: Colors.grey[400]),
              ),
            ),
        ],
      ),
    );
  }

  String _formatDate(WidgetRef ref, String iso) {
    try {
      final d = DateTime.parse(iso);
      final diff = DateTime.now().difference(d).inDays;
      if (diff == 0) return tr(ref, 'Today', '今天');
      if (diff == 1) return tr(ref, 'Yesterday', '昨天');
      return tr(ref, '$diff days ago', '$diff 天前');
    } catch (_) {
      return '';
    }
  }
}
