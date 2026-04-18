// lib/features/chat/models/session_report_model.dart
//
// 镜像后端 report_builder.py 产出的 JSON 结构。
// 两阶段：preliminary（L1, 立刻到达）/ final（L2, 异步后到达）

class SessionNodeReport {
  final int id;
  final String text;
  final String type; // "word" / "phrase" / "sentence"
  final int depthLevel;
  final bool hit;
  final double masteryBefore;
  final double masteryNow;
  final double masteryDelta;
  final bool milestoneReached;
  // L2 字段（final 阶段填充）
  final double? quality;
  final String? qualityLabel; // "excellent" / "good" / "fair" / "needs_work" / "not_used"

  const SessionNodeReport({
    required this.id,
    required this.text,
    required this.type,
    required this.depthLevel,
    required this.hit,
    required this.masteryBefore,
    required this.masteryNow,
    required this.masteryDelta,
    required this.milestoneReached,
    this.quality,
    this.qualityLabel,
  });

  factory SessionNodeReport.fromJson(Map<String, dynamic> j) {
    return SessionNodeReport(
      id: (j['id'] as num?)?.toInt() ?? 0,
      text: j['text'] as String? ?? '',
      type: j['type'] as String? ?? 'word',
      depthLevel: (j['depth_level'] as num?)?.toInt() ?? 1,
      hit: j['hit'] as bool? ?? false,
      masteryBefore: (j['mastery_before'] as num?)?.toDouble() ?? 0.0,
      masteryNow: (j['mastery_now'] as num?)?.toDouble() ?? 0.0,
      masteryDelta: (j['mastery_delta'] as num?)?.toDouble() ?? 0.0,
      milestoneReached: j['milestone_reached'] as bool? ?? false,
    );
  }

  SessionNodeReport copyWithQuality({
    required double quality,
    required String qualityLabel,
  }) {
    return SessionNodeReport(
      id: id,
      text: text,
      type: type,
      depthLevel: depthLevel,
      hit: hit,
      masteryBefore: masteryBefore,
      masteryNow: masteryNow,
      masteryDelta: masteryDelta,
      milestoneReached: milestoneReached,
      quality: quality,
      qualityLabel: qualityLabel,
    );
  }
}

class TierStatus {
  final int tier;
  final double avgEffectiveMastery;
  final double threshold;
  final bool unlockedNextTier;

  const TierStatus({
    required this.tier,
    required this.avgEffectiveMastery,
    required this.threshold,
    required this.unlockedNextTier,
  });

  factory TierStatus.fromJson(Map<String, dynamic> j) {
    return TierStatus(
      tier: (j['tier'] as num?)?.toInt() ?? 1,
      avgEffectiveMastery: (j['avg_effective_mastery'] as num?)?.toDouble() ?? 0.0,
      threshold: (j['threshold'] as num?)?.toDouble() ?? 75.0,
      unlockedNextTier: j['unlocked_next_tier'] as bool? ?? false,
    );
  }

  double get progressRatio =>
      (avgEffectiveMastery / threshold).clamp(0.0, 1.0);
}

class SessionReport {
  final String sessionId;
  final String stage; // 'preliminary' | 'final'
  final String topicTitle;
  final String? topicTitleZh;
  final int depthTier;
  final double sessionScore;
  final int hitCount;
  final int totalNodes;
  final List<SessionNodeReport> nodes;
  final List<String> newlyMastered;
  final TierStatus tierStatus;
  final String encouragement;
  final String? encouragementZh;
  // L2 fields
  final double? avgQuality;
  final String? qualityLabel;
  final bool l2Assessed;

  const SessionReport({
    required this.sessionId,
    required this.stage,
    required this.topicTitle,
    this.topicTitleZh,
    required this.depthTier,
    required this.sessionScore,
    required this.hitCount,
    required this.totalNodes,
    required this.nodes,
    required this.newlyMastered,
    required this.tierStatus,
    required this.encouragement,
    this.encouragementZh,
    this.avgQuality,
    this.qualityLabel,
    this.l2Assessed = false,
  });

  factory SessionReport.fromJson(Map<String, dynamic> j) {
    final rawNodes = (j['nodes'] as List?)?.cast<Map<String, dynamic>>() ?? [];
    final rawMastered = (j['newly_mastered'] as List?)?.cast<String>() ?? [];
    final rawTier = j['tier_status'] as Map<String, dynamic>? ?? {};

    return SessionReport(
      sessionId: j['session_id'] as String? ?? '',
      stage: j['stage'] as String? ?? 'preliminary',
      topicTitle: j['topic_title'] as String? ?? '',
      topicTitleZh: j['topic_title_zh'] as String?,
      depthTier: (j['depth_tier'] as num?)?.toInt() ?? 1,
      sessionScore: (j['session_score'] as num?)?.toDouble() ?? 0.0,
      hitCount: (j['hit_count'] as num?)?.toInt() ?? 0,
      totalNodes: (j['total_nodes'] as num?)?.toInt() ?? 0,
      nodes: rawNodes.map(SessionNodeReport.fromJson).toList(),
      newlyMastered: rawMastered,
      tierStatus: TierStatus.fromJson(rawTier),
      encouragement: j['encouragement'] as String? ?? '',
      encouragementZh: j['encouragement_zh'] as String?,
      avgQuality: (j['avg_quality'] as num?)?.toDouble(),
      qualityLabel: j['quality_label'] as String?,
      l2Assessed: j['l2_assessed'] as bool? ?? false,
    );
  }

  /// 收到 final 报告时，将 L2 质量分合并进节点列表
  SessionReport mergeWithFinal(Map<String, dynamic> finalJson) {
    final qualityBreakdown =
        (finalJson['quality_breakdown'] as List?)
            ?.cast<Map<String, dynamic>>() ??
        [];

    // text → quality info
    final qMap = <String, Map<String, dynamic>>{
      for (final q in qualityBreakdown) q['text'] as String: q,
    };

    final updatedNodes = nodes.map((node) {
      final q = qMap[node.text];
      if (q != null) {
        return node.copyWithQuality(
          quality: (q['quality'] as num?)?.toDouble() ?? 0.0,
          qualityLabel: q['quality_label'] as String? ?? 'not_used',
        );
      }
      return node;
    }).toList();

    return SessionReport(
      sessionId: sessionId,
      stage: 'final',
      topicTitle: topicTitle,
      topicTitleZh: topicTitleZh,
      depthTier: depthTier,
      sessionScore: sessionScore,
      hitCount: hitCount,
      totalNodes: totalNodes,
      nodes: updatedNodes,
      newlyMastered: newlyMastered,
      tierStatus: tierStatus,
      encouragement: encouragement,
      encouragementZh: encouragementZh,
      avgQuality: (finalJson['avg_quality'] as num?)?.toDouble(),
      qualityLabel: finalJson['quality_label'] as String?,
      l2Assessed: true,
    );
  }

  // Convenience
  bool get isFinal => stage == 'final';
  bool get hasMilestone => newlyMastered.isNotEmpty || tierStatus.unlockedNextTier;
}
