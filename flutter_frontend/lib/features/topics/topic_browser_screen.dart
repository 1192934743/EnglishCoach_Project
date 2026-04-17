// lib/features/topics/topic_browser_screen.dart
//
// Topic Browser: shows all available practice topics with mastery progress.
// Tapping a topic sends request_topic to the backend and navigates to chat.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../core/network/api_client.dart';
import '../../core/network/user_manager.dart';
import '../chat/providers/chat_provider.dart';

// ── Data model ────────────────────────────────────────────────────────────
class TopicItem {
  final int id;
  final String title;
  final String category;
  final String learnerLevel;
  final String roleName;
  final int totalNodes;
  final List<int> depthLevels;
  final double avgMastery;
  final String? lastPracticed;

  const TopicItem({
    required this.id,
    required this.title,
    required this.category,
    required this.learnerLevel,
    required this.roleName,
    required this.totalNodes,
    required this.depthLevels,
    required this.avgMastery,
    this.lastPracticed,
  });

  factory TopicItem.fromJson(Map<String, dynamic> j) => TopicItem(
        id: j['id'] as int,
        title: j['title'] as String,
        category: j['category'] as String? ?? 'General',
        learnerLevel: j['learner_level'] as String? ?? 'Intermediate',
        roleName: j['role_name'] as String? ?? 'Coach',
        totalNodes: j['total_nodes'] as int? ?? 0,
        depthLevels: (j['depth_levels'] as List?)?.cast<int>() ?? [1],
        avgMastery: (j['avg_mastery'] as num?)?.toDouble() ?? 0.0,
        lastPracticed: j['last_practiced'] as String?,
      );

  bool get hasPracticed => avgMastery > 0;
  int get maxDepth => depthLevels.isEmpty ? 1 : depthLevels.last;
}

// ── Provider ──────────────────────────────────────────────────────────────
final topicsProvider = FutureProvider<List<TopicItem>>((ref) async {
  final userId = await UserManager.getOrCreateUuid();
  final data = await ApiClient.getTopics(userId: userId);
  final rawList = (data['topics'] as List).cast<Map<String, dynamic>>();
  return rawList.map(TopicItem.fromJson).toList();
});

// ── Screen ────────────────────────────────────────────────────────────────
class TopicBrowserScreen extends ConsumerStatefulWidget {
  const TopicBrowserScreen({super.key});
  @override
  ConsumerState<TopicBrowserScreen> createState() => _TopicBrowserScreenState();
}

class _TopicBrowserScreenState extends ConsumerState<TopicBrowserScreen> {
  String _search = '';
  String? _filterCategory;

  @override
  Widget build(BuildContext context) {
    final topicsAsync = ref.watch(topicsProvider);

    return Scaffold(
      backgroundColor: const Color(0xFFF4F6F9),
      appBar: AppBar(
        title: const Text(
          'Practice Topics',
          style: TextStyle(fontWeight: FontWeight.bold, color: Colors.black87),
        ),
        backgroundColor: Colors.white,
        elevation: 1,
        centerTitle: true,
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh_rounded, color: Colors.black54),
            onPressed: () => ref.invalidate(topicsProvider),
          ),
        ],
      ),
      body: topicsAsync.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => _buildError(e),
        data: (topics) => _buildContent(topics),
      ),
    );
  }

  Widget _buildError(Object e) => Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.wifi_off_rounded, size: 48, color: Colors.grey),
            const SizedBox(height: 12),
            Text('Could not load topics.\n$e',
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.grey)),
            const SizedBox(height: 16),
            ElevatedButton(
              onPressed: () => ref.invalidate(topicsProvider),
              child: const Text('Retry'),
            ),
          ],
        ),
      );

  Widget _buildContent(List<TopicItem> all) {
    // Get unique categories
    final categories = ['All', ...{for (final t in all) t.category}.toList()..sort()];

    // Filter
    var filtered = all.where((t) {
      final matchSearch = _search.isEmpty ||
          t.title.toLowerCase().contains(_search.toLowerCase()) ||
          t.category.toLowerCase().contains(_search.toLowerCase());
      final matchCat =
          _filterCategory == null || _filterCategory == 'All' || t.category == _filterCategory;
      return matchSearch && matchCat;
    }).toList();

    // Sort: practiced first (by mastery desc), then unpracticed alphabetically
    filtered.sort((a, b) {
      if (a.hasPracticed && !b.hasPracticed) return -1;
      if (!a.hasPracticed && b.hasPracticed) return 1;
      if (a.hasPracticed && b.hasPracticed) return b.avgMastery.compareTo(a.avgMastery);
      return a.title.compareTo(b.title);
    });

    return Column(
      children: [
        // Search bar
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
          child: TextField(
            onChanged: (v) => setState(() => _search = v),
            decoration: InputDecoration(
              hintText: 'Search topics...',
              prefixIcon: const Icon(Icons.search_rounded, color: Colors.grey),
              filled: true,
              fillColor: Colors.white,
              contentPadding: const EdgeInsets.symmetric(vertical: 0),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(14),
                borderSide: BorderSide.none,
              ),
            ),
          ),
        ),
        // Category filter chips
        SizedBox(
          height: 40,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 16),
            itemCount: categories.length,
            separatorBuilder: (_, __) => const SizedBox(width: 8),
            itemBuilder: (_, i) {
              final cat = categories[i];
              final selected = (_filterCategory ?? 'All') == cat;
              return FilterChip(
                label: Text(cat, style: TextStyle(fontSize: 12, color: selected ? Colors.white : Colors.black87)),
                selected: selected,
                onSelected: (_) => setState(() => _filterCategory = cat == 'All' ? null : cat),
                selectedColor: Colors.blueAccent,
                backgroundColor: Colors.white,
                checkmarkColor: Colors.white,
                side: BorderSide(color: selected ? Colors.blueAccent : Colors.grey.shade200),
                padding: const EdgeInsets.symmetric(horizontal: 4),
                showCheckmark: false,
              );
            },
          ),
        ),
        const SizedBox(height: 8),
        // Stats summary
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
          child: Row(
            children: [
              Text('${filtered.length} topics',
                  style: TextStyle(fontSize: 12, color: Colors.grey[600])),
              const SizedBox(width: 12),
              Text('${filtered.where((t) => t.hasPracticed).length} practiced',
                  style: const TextStyle(fontSize: 12, color: Colors.blueAccent)),
            ],
          ),
        ),
        // Topic list
        Expanded(
          child: filtered.isEmpty
              ? const Center(child: Text('No topics found', style: TextStyle(color: Colors.grey)))
              : ListView.separated(
                  padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
                  itemCount: filtered.length,
                  separatorBuilder: (_, __) => const SizedBox(height: 10),
                  itemBuilder: (_, i) => _TopicCard(
                    topic: filtered[i],
                    onTap: () => _startTopic(filtered[i]),
                  ),
                ),
        ),
      ],
    );
  }

  void _startTopic(TopicItem topic) {
    ref.read(chatProvider.notifier).requestTopic(topic.title);
    // topic_changed event → MainScreen._listenTopicChanged() will auto-switch to chat tab
  }
}

// ── Topic Card ────────────────────────────────────────────────────────────
class _TopicCard extends StatelessWidget {
  final TopicItem topic;
  final VoidCallback onTap;

  const _TopicCard({required this.topic, required this.onTap});

  Color get _levelColor {
    return switch (topic.learnerLevel.toLowerCase()) {
      'beginner' => const Color(0xFF16A34A),
      'professional' => const Color(0xFF7C3AED),
      _ => const Color(0xFF2196F3),
    };
  }

  @override
  Widget build(BuildContext context) {
    final mastery = topic.avgMastery / 100.0;
    final practiced = topic.hasPracticed;

    return GestureDetector(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(16),
          border: practiced
              ? Border.all(color: Colors.blueAccent.withValues(alpha: 0.2))
              : null,
          boxShadow: [
            BoxShadow(
              color: Colors.black.withValues(alpha: 0.04),
              blurRadius: 8,
              offset: const Offset(0, 2),
            ),
          ],
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    topic.title,
                    style: const TextStyle(
                      fontSize: 15,
                      fontWeight: FontWeight.bold,
                      color: Colors.black87,
                    ),
                  ),
                ),
                if (practiced)
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                    decoration: BoxDecoration(
                      color: Colors.blueAccent.withValues(alpha: 0.1),
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Text(
                      '${topic.avgMastery.toInt()}%',
                      style: const TextStyle(
                        fontSize: 12,
                        fontWeight: FontWeight.bold,
                        color: Colors.blueAccent,
                      ),
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 6),
            Row(
              children: [
                _Chip(topic.category, Colors.grey.shade100, Colors.grey.shade700),
                const SizedBox(width: 6),
                _Chip(topic.learnerLevel, _levelColor.withValues(alpha: 0.1), _levelColor),
                const SizedBox(width: 6),
                _Chip(
                  '${topic.totalNodes} expressions',
                  Colors.orange.shade50,
                  Colors.orange.shade700,
                ),
              ],
            ),
            if (practiced) ...[
              const SizedBox(height: 10),
              ClipRRect(
                borderRadius: BorderRadius.circular(4),
                child: LinearProgressIndicator(
                  value: mastery.clamp(0.0, 1.0),
                  minHeight: 5,
                  backgroundColor: Colors.grey.shade100,
                  valueColor: AlwaysStoppedAnimation(
                    mastery >= 0.75 ? Colors.green : Colors.blueAccent,
                  ),
                ),
              ),
              const SizedBox(height: 6),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Text(
                    mastery >= 0.75 ? 'Ready for next tier!' : 'Keep practicing',
                    style: TextStyle(
                      fontSize: 11,
                      color: mastery >= 0.75 ? Colors.green : Colors.grey[500],
                    ),
                  ),
                  if (topic.lastPracticed != null)
                    Text(
                      _formatDate(topic.lastPracticed!),
                      style: TextStyle(fontSize: 11, color: Colors.grey[400]),
                    ),
                ],
              ),
            ] else ...[
              const SizedBox(height: 8),
              Text(
                'Not practiced yet — tap to start',
                style: TextStyle(fontSize: 12, color: Colors.grey[400]),
              ),
            ],
          ],
        ),
      ),
    );
  }

  String _formatDate(String iso) {
    try {
      final d = DateTime.parse(iso);
      final now = DateTime.now();
      final diff = now.difference(d).inDays;
      if (diff == 0) return 'Today';
      if (diff == 1) return 'Yesterday';
      return '$diff days ago';
    } catch (_) {
      return '';
    }
  }
}

class _Chip extends StatelessWidget {
  final String label;
  final Color bg;
  final Color fg;
  const _Chip(this.label, this.bg, this.fg);
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
        decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(6)),
        child: Text(label, style: TextStyle(fontSize: 11, color: fg, fontWeight: FontWeight.w500)),
      );
}
