// lib/features/topics/models/topic_item.dart
//
// TopicItem data model - shared between topic_browser_screen and chat_provider.

class TopicItem {
  final int id;
  final String title;
  final String? titleZh;
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
    this.titleZh,
    required this.category,
    required this.learnerLevel,
    required this.roleName,
    required this.totalNodes,
    required this.depthLevels,
    required this.avgMastery,
    this.lastPracticed,
  });

  factory TopicItem.fromJson(Map<String, dynamic> j) => TopicItem(
        id: (j['id'] as num).toInt(),
        title: j['title'] as String,
        titleZh: j['title_zh'] as String?,
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
