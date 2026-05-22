// lib/features/topics/topic_browser_screen.dart
//
// Topic Browser: shows all available practice topics with mastery progress.
// Tapping a topic sends request_topic to the backend and navigates to chat.

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../core/network/api_client.dart';
import '../../core/network/user_manager.dart';
import '../../core/providers/settings_provider.dart';  // settingsProvider, tr, mainTabIndexProvider, etc.
import '../../core/theme/app_colors.dart';
import '../../core/widgets/mastery_band_widgets.dart';
import '../chat/providers/chat_provider.dart';
import 'models/topic_item.dart';

// ── Provider ──────────────────────────────────────────────────────────────
final topicsProvider = FutureProvider<List<TopicItem>>((ref) async {
  final userId = await UserManager.getOrCreateUuid();
  final data = await ApiClient.getTopics(userId: userId);
  final rawList = (data['topics'] as List).cast<Map<String, dynamic>>();
  if (kDebugMode) {
    final isZh = ref.read(settingsProvider).isChinese;
    debugPrint(
      '[TopicI18n] base=${ApiClient.resolvedBaseUrl} isChinese=$isZh '
      'topics=${rawList.length}',
    );
    for (final j in rawList.take(5)) {
      debugPrint(
        '[TopicI18n]  id=${j['id']} title="${j['title']}" title_zh=${j['title_zh']}',
      );
    }
  }
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
  /// null = all; `open` / `polish` 分界与后端 `MASTERY_AUTO_PICK_SOFT_CAP` 一致（见 kMasteryAutoPickSoftCapPercent）。
  String? _masteryFilter;

  @override
  Widget build(BuildContext context) {
    final topicsAsync = ref.watch(topicsProvider);

    return Scaffold(
      backgroundColor: AppColors.background,
      body: SafeArea(
        child: topicsAsync.when(
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => _buildError(e),
          data: (topics) => _buildContent(topics),
        ),
      ),
    );
  }

  Widget _buildError(Object e) => Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.wifi_off_rounded, size: 48, color: AppColors.textTertiary),
            const SizedBox(height: 12),
            Text(
              '${tr(ref, 'Could not load topics.', '话题列表加载失败。')}\n$e',
              textAlign: TextAlign.center,
              style: TextStyle(color: AppColors.textSecondary),
            ),
            const SizedBox(height: 16),
            ElevatedButton(
              onPressed: () => ref.invalidate(topicsProvider),
              child: Text(tr(ref, 'Retry', '重试')),
            ),
          ],
        ),
      );

  Widget _buildContent(List<TopicItem> all) {
    // Get unique categories
    final allLabel = tr(ref, 'All', '全部');
    final categories = [allLabel, ...{for (final t in all) t.category}.toList()..sort()];

    // Filter
    var filtered = all.where((t) {
      final matchSearch = _search.isEmpty ||
          t.title.toLowerCase().contains(_search.toLowerCase()) ||
          t.category.toLowerCase().contains(_search.toLowerCase());
      final matchCat = _filterCategory == null ||
          _filterCategory == allLabel ||
          t.category == _filterCategory;
      final open = t.avgMastery < kMasteryAutoPickSoftCapPercent;
      final matchMastery = _masteryFilter == null ||
          (_masteryFilter == 'open' && open) ||
          (_masteryFilter == 'polish' && !open);
      return matchSearch && matchCat && matchMastery;
    }).toList();

    // Sort: lowest average mastery first (needs work at top), then title
    filtered.sort((a, b) {
      final c = a.avgMastery.compareTo(b.avgMastery);
      if (c != 0) return c;
      return a.title.compareTo(b.title);
    });

    return Column(
      children: [
        // Search bar
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
          child: TextField(
            onChanged: (v) => setState(() => _search = v),
            decoration: InputDecoration(
              hintText: tr(ref, 'Search topics...', '搜索话题…'),
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
              final selected = (_filterCategory ?? allLabel) == cat;
              return FilterChip(
                label: Text(cat, style: TextStyle(fontSize: 12, color: selected ? Colors.white : Colors.black87)),
                selected: selected,
                onSelected: (_) => setState(() => _filterCategory = cat == allLabel ? null : cat),
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
        _buildMasterySegmentControl(ref),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
          child: Text(
            tr(
              ref,
              'New sessions favor topics where you still have room to grow. Tap a card to practice that topic.',
              '新开对练会优先帮你巩固「待巩固」里的话题；点卡片即可专门练习该话题。',
            ),
            style: TextStyle(fontSize: 11, height: 1.35, color: Colors.grey[600]),
          ),
        ),
        // Stats summary
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
          child: Row(
            children: [
              Text(
                tr(ref, '${filtered.length} topics', '${filtered.length} 个话题'),
                style: TextStyle(fontSize: 12, color: Colors.grey[600]),
              ),
              const SizedBox(width: 12),
              Text(
                tr(
                  ref,
                  '${filtered.where((t) => t.hasPracticed).length} practiced',
                  '已练过 ${filtered.where((t) => t.hasPracticed).length} 个',
                ),
                style: const TextStyle(fontSize: 12, color: Colors.blueAccent),
              ),
            ],
          ),
        ),
        // Topic list（下拉刷新，不再占用顶栏）
        Expanded(
          child: RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(topicsProvider);
              await ref.read(topicsProvider.future);
            },
            child: LayoutBuilder(
              builder: (context, constraints) {
                if (filtered.isEmpty) {
                  return SingleChildScrollView(
                    physics: const AlwaysScrollableScrollPhysics(),
                    child: ConstrainedBox(
                      constraints: BoxConstraints(minHeight: constraints.maxHeight),
                      child: Center(
                        child: Text(
                          tr(ref, 'No topics found', '没有符合条件的话题'),
                          style: const TextStyle(color: Colors.grey),
                        ),
                      ),
                    ),
                  );
                }
                return ListView.separated(
                  physics: const AlwaysScrollableScrollPhysics(),
                  padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
                  itemCount: filtered.length,
                  separatorBuilder: (_, _) => const SizedBox(height: 10),
                  itemBuilder: (_, i) => _TopicCard(
                    topic: filtered[i],
                    onTap: () => _startTopic(filtered[i]),
                  ),
                );
              },
            ),
          ),
        ),
      ],
    );
  }

  Widget _buildMasterySegmentControl(WidgetRef ref) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 0),
      child: Container(
        padding: const EdgeInsets.all(5),
        decoration: BoxDecoration(
          color: const Color(0xFFE8ECF4),
          borderRadius: BorderRadius.circular(18),
        ),
        child: Row(
          children: [
            Expanded(
              child: _masterySegment(
                ref,
                filterKey: null,
                icon: Icons.grid_view_rounded,
                en: 'All',
                cn: '全部',
              ),
            ),
            Expanded(
              child: _masterySegment(
                ref,
                filterKey: 'open',
                icon: Icons.auto_awesome_rounded,
                en: 'Room to grow',
                cn: '待巩固',
              ),
            ),
            Expanded(
              child: _masterySegment(
                ref,
                filterKey: 'polish',
                icon: Icons.emoji_events_rounded,
                en: 'Solid',
                cn: '已扎实',
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _masterySegment(
    WidgetRef ref, {
    required String? filterKey,
    required IconData icon,
    required String en,
    required String cn,
  }) {
    final selected =
        (filterKey == null && _masteryFilter == null) || _masteryFilter == filterKey;
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(14),
        onTap: () => setState(() => _masteryFilter = filterKey),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
          padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 2),
          decoration: BoxDecoration(
            color: selected ? Colors.white : Colors.transparent,
            borderRadius: BorderRadius.circular(14),
            boxShadow: selected
                ? [
                    BoxShadow(
                      color: Colors.black.withValues(alpha: 0.07),
                      blurRadius: 10,
                      offset: const Offset(0, 2),
                    ),
                  ]
                : [],
          ),
          child: Column(
            children: [
              Icon(
                icon,
                size: 20,
                color: selected ? const Color(0xFF2563EB) : Colors.grey.shade600,
              ),
              const SizedBox(height: 3),
              Text(
                tr(ref, en, cn),
                textAlign: TextAlign.center,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: 11,
                  height: 1.15,
                  fontWeight: selected ? FontWeight.w700 : FontWeight.w500,
                  color: selected ? Colors.black87 : Colors.grey.shade600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  void _startTopic(TopicItem topic) {
    // 精准重复检测：使用 topic.id
    final currentTopicId = ref.read(chatProvider).currentTopicId;
    if (currentTopicId == topic.id) {
      // 已是当前话题，切换到对话页面
      ref.read(mainTabIndexProvider.notifier).setTab(0);
      return;
    }

    ref.read(chatProvider.notifier).requestTopic(topic);
  }
}

// ── Topic Card ────────────────────────────────────────────────────────────
class _TopicCard extends ConsumerWidget {
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
  Widget build(BuildContext context, WidgetRef ref) {
    final practiced = topic.hasPracticed;
    final band = masteryUiBandFromPercent(topic.avgMastery);

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
                    chatTopicDisplayTitle(ref, topic.title, titleZh: topic.titleZh),
                    style: const TextStyle(
                      fontSize: 15,
                      fontWeight: FontWeight.bold,
                      color: Colors.black87,
                    ),
                  ),
                ),
                if (practiced) MasteryBandPill(band: band, compact: true),
              ],
            ),
            const SizedBox(height: 6),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                _Chip(topic.category, Colors.grey.shade100, Colors.grey.shade700),
                _Chip(
                  learnerLevelUiLabel(ref, topic.learnerLevel),
                  _levelColor.withValues(alpha: 0.1),
                  _levelColor,
                ),
                _Chip(
                  tr(
                    ref,
                    '${topic.totalNodes} expressions',
                    '${topic.totalNodes} 条表达',
                  ),
                  Colors.orange.shade50,
                  Colors.orange.shade700,
                ),
              ],
            ),
            if (practiced) ...[
              const SizedBox(height: 12),
              ClipRRect(
                borderRadius: BorderRadius.circular(6),
                child: LinearProgressIndicator(
                  value: (topic.avgMastery / 100.0).clamp(0.0, 1.0),
                  minHeight: 7,
                  backgroundColor: Colors.grey.shade100,
                  valueColor: AlwaysStoppedAnimation<Color>(
                    topic.avgMastery >= kMasteryAutoPickSoftCapPercent
                        ? Colors.green
                        : (topic.avgMastery >= kMasteryTierUpThresholdPercent
                            ? Colors.blueAccent
                            : Colors.orange),
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Text(
                    band == MasteryUiBand.fluent
                        ? tr(ref, 'Great — keep polishing!', '很棒，可多练精进！')
                        : tr(ref, 'Keep practicing', '继续加油'),
                    style: TextStyle(
                      fontSize: 11,
                      color: masteryUiBandColor(band).withValues(alpha: 0.95),
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                  Row(
                    children: [
                      Text(
                        '${topic.avgMastery.toInt()}%',
                        style: TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.bold,
                          color: masteryUiBandColor(band),
                        ),
                      ),
                      if (topic.lastPracticed != null) ...[
                        const SizedBox(width: 10),
                        Text(
                          _formatDate(ref, topic.lastPracticed!),
                          style: TextStyle(fontSize: 11, color: Colors.grey[400]),
                        ),
                      ],
                    ],
                  ),
                ],
              ),
            ] else ...[
              const SizedBox(height: 8),
              Text(
                tr(ref, 'Not practiced yet — tap to start', '尚未练习，点按开始'),
                style: TextStyle(fontSize: 12, color: Colors.grey[400]),
              ),
            ],
          ],
        ),
      ),
    );
  }

  String _formatDate(WidgetRef ref, String iso) {
    try {
      final d = DateTime.parse(iso);
      final now = DateTime.now();
      final diff = now.difference(d).inDays;
      if (diff == 0) return tr(ref, 'Today', '今天');
      if (diff == 1) return tr(ref, 'Yesterday', '昨天');
      return tr(ref, '$diff days ago', '$diff 天前');
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
