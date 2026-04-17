// lib/features/stats/stats_screen.dart
//
// Learning Statistics screen.
// Shows: streak, total sessions, expressions mastered, per-topic progress.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../core/network/api_client.dart';
import '../../core/network/user_manager.dart';
import '../topics/topic_browser_screen.dart' show TopicItem;

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
      backgroundColor: const Color(0xFFF4F6F9),
      appBar: AppBar(
        title: const Text(
          'My Progress',
          style: TextStyle(fontWeight: FontWeight.bold, color: Colors.black87),
        ),
        backgroundColor: Colors.white,
        elevation: 1,
        centerTitle: true,
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh_rounded, color: Colors.black54),
            onPressed: () => ref.invalidate(statsProvider),
          ),
        ],
      ),
      body: statsAsync.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => _buildError(context, ref, e),
        data: (data) => _buildContent(context, data),
      ),
    );
  }

  Widget _buildError(BuildContext context, WidgetRef ref, Object e) => Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.wifi_off_rounded, size: 48, color: Colors.grey),
            const SizedBox(height: 12),
            Text('Could not load stats.\n$e',
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.grey)),
            const SizedBox(height: 16),
            ElevatedButton(
              onPressed: () => ref.invalidate(statsProvider),
              child: const Text('Retry'),
            ),
          ],
        ),
      );

  Widget _buildContent(BuildContext context, Map<String, dynamic> data) {
    final sessions = data['total_sessions'] as int? ?? 0;
    final practiced = data['total_expressions_practiced'] as int? ?? 0;
    final mastered = data['total_expressions_mastered'] as int? ?? 0;
    final topics = data['topics_touched'] as int? ?? 0;
    final streak = data['current_streak_days'] as int? ?? 0;
    final recentSessions = (data['recent_sessions'] as List?)?.cast<Map<String, dynamic>>() ?? [];
    final topicsSummary = (data['topics_summary'] as List?)?.cast<Map<String, dynamic>>() ?? [];

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        // Streak hero card
        _buildStreakCard(streak),
        const SizedBox(height: 16),
        // 4-stat grid
        _buildStatsGrid(sessions, practiced, mastered, topics),
        const SizedBox(height: 20),
        // Topic progress
        if (topicsSummary.isNotEmpty) ...[
          _sectionTitle('Topic Mastery'),
          const SizedBox(height: 10),
          ...topicsSummary.map((t) => Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: _TopicMasteryBar(
              title: t['topic_title'] as String,
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
          _sectionTitle('Recent Sessions'),
          const SizedBox(height: 10),
          ...recentSessions.map((s) => Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: _RecentSessionTile(session: s),
          )),
        ],
        if (sessions == 0)
          _buildEmptyState(),
      ],
    );
  }

  Widget _buildStreakCard(int streak) {
    final hasStreak = streak > 0;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(20),
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
        children: [
          Text(
            hasStreak ? '🔥' : '📚',
            style: const TextStyle(fontSize: 40),
          ),
          const SizedBox(width: 16),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  hasStreak ? '$streak-Day Streak!' : 'Start Your Streak',
                  style: const TextStyle(
                    fontSize: 22,
                    fontWeight: FontWeight.bold,
                    color: Colors.white,
                  ),
                ),
                Text(
                  hasStreak
                      ? 'Keep it up — practice again today!'
                      : 'Practice every day to build a streak.',
                  style: TextStyle(
                    fontSize: 13,
                    color: Colors.white.withValues(alpha: 0.85),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildStatsGrid(int sessions, int practiced, int mastered, int topics) {
    return GridView.count(
      crossAxisCount: 2,
      crossAxisSpacing: 10,
      mainAxisSpacing: 10,
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      childAspectRatio: 1.6,
      children: [
        _StatCard(icon: Icons.play_circle_outline_rounded, color: Colors.blueAccent,
            label: 'Sessions', value: '$sessions'),
        _StatCard(icon: Icons.record_voice_over_rounded, color: Colors.teal,
            label: 'Expressions tried', value: '$practiced'),
        _StatCard(icon: Icons.star_rounded, color: Colors.orange,
            label: 'Mastered (≥60%)', value: '$mastered'),
        _StatCard(icon: Icons.topic_rounded, color: Colors.purple,
            label: 'Topics touched', value: '$topics'),
      ],
    );
  }

  Widget _buildEmptyState() => Center(
        child: Padding(
          padding: const EdgeInsets.only(top: 40),
          child: Column(
            children: [
              const Text('🎯', style: TextStyle(fontSize: 56)),
              const SizedBox(height: 16),
              const Text(
                'No sessions yet',
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 8),
              Text(
                'Go to the Chat tab and start your first practice session!',
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
        padding: const EdgeInsets.all(14),
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
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Icon(icon, color: color, size: 22),
            Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  value,
                  style: TextStyle(
                    fontSize: 24,
                    fontWeight: FontWeight.bold,
                    color: color,
                  ),
                ),
                Text(
                  label,
                  style: TextStyle(fontSize: 11, color: Colors.grey[500]),
                ),
              ],
            ),
          ],
        ),
      );
}

class _TopicMasteryBar extends StatelessWidget {
  final String title;
  final String category;
  final double avgMastery;
  final int nodesPracticed;
  final int totalNodes;

  const _TopicMasteryBar({
    required this.title,
    required this.category,
    required this.avgMastery,
    required this.nodesPracticed,
    required this.totalNodes,
  });

  @override
  Widget build(BuildContext context) {
    final ratio = (avgMastery / 100.0).clamp(0.0, 1.0);
    final color = ratio >= 0.75 ? Colors.green : (ratio >= 0.4 ? Colors.blueAccent : Colors.orange);

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
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Expanded(
                child: Text(
                  title,
                  style: const TextStyle(fontSize: 14, fontWeight: FontWeight.bold),
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
            '$category · $nodesPracticed/$totalNodes expressions',
            style: TextStyle(fontSize: 11, color: Colors.grey[500]),
          ),
          const SizedBox(height: 8),
          ClipRRect(
            borderRadius: BorderRadius.circular(4),
            child: LinearProgressIndicator(
              value: ratio,
              minHeight: 6,
              backgroundColor: Colors.grey.shade100,
              valueColor: AlwaysStoppedAnimation(color),
            ),
          ),
        ],
      ),
    );
  }
}

class _RecentSessionTile extends StatelessWidget {
  final Map<String, dynamic> session;

  const _RecentSessionTile({required this.session});

  @override
  Widget build(BuildContext context) {
    final title = session['topic_title'] as String? ?? 'Unknown';
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
              child: Text('T$tier', style: const TextStyle(
                fontSize: 12, fontWeight: FontWeight.bold, color: Colors.blueAccent,
              )),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.bold),
                    overflow: TextOverflow.ellipsis),
                Text(
                  '$mastered expressions mastered${quality != null ? ' · Quality: ${(quality * 100).toInt()}%' : ''}',
                  style: TextStyle(fontSize: 11, color: Colors.grey[500]),
                ),
              ],
            ),
          ),
          if (date != null)
            Text(_formatDate(date), style: TextStyle(fontSize: 11, color: Colors.grey[400])),
        ],
      ),
    );
  }

  String _formatDate(String iso) {
    try {
      final d = DateTime.parse(iso);
      final diff = DateTime.now().difference(d).inDays;
      if (diff == 0) return 'Today';
      if (diff == 1) return 'Yesterday';
      return '$diff days ago';
    } catch (_) {
      return '';
    }
  }
}
