// WebSocket 消息类型定义（对应后端发送的各事件）
//
// 所有服务端 → 前端的消息格式定义，用于 ws_message_parser.dart 解析。

class WsEventType {
  const WsEventType._(this._value);
  final String _value;

  @override String toString() => _value;

  static const WsEventType warmupSuccess = WsEventType._('warmup_success');
  static const WsEventType ttsFinished = WsEventType._('tts_finished');
  static const WsEventType teachingData = WsEventType._('teaching_data');
  static const WsEventType roleSwapped = WsEventType._('role_swapped');
  static const WsEventType topicMasteryReached = WsEventType._('topic_mastery_reached');
  static const WsEventType topicGenerating = WsEventType._('topic_generating');
  static const WsEventType topicChanged = WsEventType._('topic_changed');
  static const WsEventType sessionReport = WsEventType._('session_report');
  static const WsEventType error = WsEventType._('error');
  static const WsEventType asrPartial = WsEventType._('asr_partial');
  static const WsEventType testAiReply = WsEventType._('test_ai_reply');

  static WsEventType? fromString(String v) {
    switch (v) {
      case 'warmup_success': return warmupSuccess;
      case 'tts_finished': return ttsFinished;
      case 'teaching_data': return teachingData;
      case 'role_swapped': return roleSwapped;
      case 'topic_mastery_reached': return topicMasteryReached;
      case 'topic_generating': return topicGenerating;
      case 'topic_changed': return topicChanged;
      case 'session_report': return sessionReport;
      case 'error': return error;
      case 'asr_partial': return asrPartial;
      case 'test_ai_reply': return testAiReply;
      default: return null;
    }
  }
}

class WsWarmupSuccess {
  final UserSettings? userSettings;
  WsWarmupSuccess({this.userSettings});
}

class WsTtsFinished {
  WsTtsFinished();
}

class WsTeachingData {
  final String userText;
  final String aiText;
  final String? aiTranslationCn;
  final List<String>? suggestedHintsEn;
  final String? coachCorrectionCn;
  WsTeachingData({
    required this.userText,
    required this.aiText,
    this.aiTranslationCn,
    this.suggestedHintsEn,
    this.coachCorrectionCn,
  });
}

class WsRoleSwapped {
  final bool isFlipped;
  WsRoleSwapped({required this.isFlipped});
}

class WsTopicMastery {
  final double progress;
  final int level;
  final String nextTopicSuggestion;
  WsTopicMastery({
    required this.progress,
    required this.level,
    required this.nextTopicSuggestion,
  });
}

class WsTopicGenerating {
  final String message;
  final String? messageZh;
  WsTopicGenerating({required this.message, this.messageZh});
}

class WsTopicChanged {
  final int? topicId;
  final String topicTitle;
  final String? topicTitleZh;
  final String roleName;
  final int depthTier;
  final String sessionId;
  WsTopicChanged({
    this.topicId,
    required this.topicTitle,
    this.topicTitleZh,
    required this.roleName,
    required this.depthTier,
    required this.sessionId,
  });
}

class WsSessionReport {
  final String sessionId;
  final String topicTitle;
  final String? topicTitleZh;
  final int depthTier;
  WsSessionReport({
    required this.sessionId,
    required this.topicTitle,
    this.topicTitleZh,
    required this.depthTier,
  });
}

class WsSessionReportPreliminary extends WsSessionReport {
  final double sessionScore;
  final int hitCount;
  final int totalNodes;
  final List<WsNodeProgress> nodes;
  final List<String> newlyMastered;
  final WsTierStatus tierStatus;
  final String encouragement;
  final String? encouragementZh;
  WsSessionReportPreliminary.fromJson(Map<String, dynamic> json)
      : sessionScore = (json['session_score'] as num?)?.toDouble() ?? 0.0,
        hitCount = (json['hit_count'] as num?)?.toInt() ?? 0,
        totalNodes = (json['total_nodes'] as num?)?.toInt() ?? 0,
        nodes = (json['nodes'] as List?)
                ?.map((e) => WsNodeProgress.fromJson(e as Map<String, dynamic>))
                .toList() ??
            [],
        newlyMastered = (json['newly_mastered'] as List?)
                ?.map((e) => e.toString())
                .toList() ??
            [],
        tierStatus = WsTierStatus.fromJson(
            json['tier_status'] as Map<String, dynamic>? ?? {}),
        encouragement = json['encouragement'] as String? ?? '',
        encouragementZh = json['encouragement_zh'] as String?,
        super(
          sessionId: json['session_id'] as String? ?? '',
          topicTitle: json['topic_title'] as String? ?? '',
          topicTitleZh: json['topic_title_zh'] as String?,
          depthTier: (json['depth_tier'] as num?)?.toInt() ?? 1,
        );
}

class WsSessionReportFinal extends WsSessionReport {
  final double avgQuality;
  final String qualityLabel;
  final List<WsQualityBreakdown> qualityBreakdown;
  WsSessionReportFinal.fromJson(Map<String, dynamic> json)
      : avgQuality = (json['avg_quality'] as num?)?.toDouble() ?? 0.0,
        qualityLabel = json['quality_label'] as String? ?? '',
        qualityBreakdown = (json['quality_breakdown'] as List?)
                ?.map((e) => WsQualityBreakdown.fromJson(e as Map<String, dynamic>))
                .toList() ??
            [],
        super(
          sessionId: json['session_id'] as String? ?? '',
          topicTitle: json['topic_title'] as String? ?? '',
          topicTitleZh: json['topic_title_zh'] as String?,
          depthTier: (json['depth_tier'] as num?)?.toInt() ?? 1,
        );
}

class WsNodeProgress {
  final int id;
  final String text;
  final String type;
  final int depthLevel;
  final bool hit;
  final double masteryBefore;
  final double masteryNow;
  final double masteryDelta;
  final bool milestoneReached;
  WsNodeProgress.fromJson(Map<String, dynamic> json)
      : id = json['id'] as int? ?? 0,
        text = json['text'] as String? ?? '',
        type = json['type'] as String? ?? 'word',
        depthLevel = (json['depth_level'] as num?)?.toInt() ?? 1,
        hit = json['hit'] as bool? ?? false,
        masteryBefore = (json['mastery_before'] as num?)?.toDouble() ?? 0.0,
        masteryNow = (json['mastery_now'] as num?)?.toDouble() ?? 0.0,
        masteryDelta = (json['mastery_delta'] as num?)?.toDouble() ?? 0.0,
        milestoneReached = json['milestone_reached'] as bool? ?? false;
}

class WsTierStatus {
  final int tier;
  final double avgEffectiveMastery;
  final double threshold;
  final bool unlockedNextTier;
  WsTierStatus.fromJson(Map<String, dynamic> json)
      : tier = (json['tier'] as num?)?.toInt() ?? 1,
        avgEffectiveMastery =
            (json['avg_effective_mastery'] as num?)?.toDouble() ?? 0.0,
        threshold = (json['threshold'] as num?)?.toDouble() ?? 75.0,
        unlockedNextTier = json['unlocked_next_tier'] as bool? ?? false;
}

class WsQualityBreakdown {
  final String text;
  final bool attempted;
  final bool correct;
  final double quality;
  final double masteryAfterL2;
  final String qualityLabel;
  WsQualityBreakdown.fromJson(Map<String, dynamic> json)
      : text = json['text'] as String? ?? '',
        attempted = json['attempted'] as bool? ?? false,
        correct = json['correct'] as bool? ?? false,
        quality = (json['quality'] as num?)?.toDouble() ?? 0.0,
        masteryAfterL2 =
            (json['mastery_after_l2'] as num?)?.toDouble() ?? 0.0,
        qualityLabel = json['quality_label'] as String? ?? '';
}

class WsError {
  final String code;
  final String? message;
  final String? messageZh;
  WsError({required this.code, this.message, this.messageZh});

  bool get isLlmTimeout => code == 'LLM_TIMEOUT';
  bool get isLlmError => code == 'LLM_ERROR';
  bool get isTopicGenerationFailed => code == 'TOPIC_GENERATION_FAILED';
}

class WsAsrPartial {
  final String text;
  WsAsrPartial({required this.text});
}

class WsTestAiReply {
  final String text;
  final String phase;
  WsTestAiReply({required this.text, required this.phase});
}

class UserSettings {
  final double? depthPreference;
  final double? newTopicAppetite;
  final String? learnerLevel;
  UserSettings({this.depthPreference, this.newTopicAppetite, this.learnerLevel});
}
