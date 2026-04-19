// WebSocket 入站消息解析器（纯函数，无副作用）
//
// 每个解析函数接收原始 Map<String, dynamic>，返回强类型结果或 null（解析失败时）。
// 用途：chat_provider.dart 中的 WS 消息处理委托给此模块。

import '../models/ws_message_models.dart';

/// 解析服务端下发的事件类型
WsEventType? parseEventType(Map<String, dynamic> data) {
  final event = data['event'] as String?;
  if (event == null) return null;
  return WsEventType.fromString(event);
}

/// 解析 warmup_success 事件
WsWarmupSuccess? parseWarmupSuccess(Map<String, dynamic> data) {
  if (data['event'] != 'warmup_success') return null;
  final settings = data['user_settings'];
  return WsWarmupSuccess(
    userSettings: settings is Map<String, dynamic>
        ? parseUserSettings(settings)
        : null,
  );
}

/// 解析 tts_finished 事件
WsTtsFinished? parseTtsFinished(Map<String, dynamic> data) {
  if (data['event'] != 'tts_finished') return null;
  return WsTtsFinished();
}

/// 解析 teaching_data 事件
WsTeachingData? parseTeachingData(Map<String, dynamic> data) {
  if (data['event'] != 'teaching_data') return null;
  final rawData = data['data'];
  if (rawData is! Map) return null;
  final d = Map<String, dynamic>.from(rawData);
  return WsTeachingData(
    userText: d['user_text'] as String? ?? '',
    aiText: d['ai_text'] as String? ?? '',
    aiTranslationCn: d['ai_translation_cn'] as String?,
    suggestedHintsEn: (d['suggested_hints_en'] as List?)
        ?.map((e) => e.toString())
        .toList(),
    coachCorrectionCn: d['coach_correction_cn'] as String?,
  );
}

/// 解析 role_swapped 事件
WsRoleSwapped? parseRoleSwapped(Map<String, dynamic> data) {
  if (data['event'] != 'role_swapped') return null;
  return WsRoleSwapped(
    isFlipped: data['is_flipped'] as bool? ?? false,
  );
}

/// 解析 topic_mastery_reached 事件
WsTopicMastery? parseTopicMastery(Map<String, dynamic> data) {
  if (data['event'] != 'topic_mastery_reached') return null;
  return WsTopicMastery(
    progress: (data['progress'] as num?)?.toDouble() ?? 0.0,
    level: (data['level'] as num?)?.toDouble().toInt() ?? 1,
    nextTopicSuggestion: data['next_topic_suggestion'] as String? ?? '',
  );
}

/// 解析 topic_generating 事件
WsTopicGenerating? parseTopicGenerating(Map<String, dynamic> data) {
  if (data['event'] != 'topic_generating') return null;
  return WsTopicGenerating(
    message: data['message'] as String? ?? '',
    messageZh: data['message_zh'] as String?,
  );
}

/// 解析 topic_changed 事件
WsTopicChanged? parseTopicChanged(Map<String, dynamic> data) {
  if (data['event'] != 'topic_changed') return null;
  return WsTopicChanged(
    topicId: data['topic_id'] as int?,
    topicTitle: data['topic_title'] as String? ?? '',
    topicTitleZh: (data['topic_title_zh'] as String?)?.trim(),
    roleName: data['role_name'] as String? ?? '',
    depthTier: data['depth_tier'] as int? ?? 1,
    sessionId: data['session_id'] as String? ?? '',
  );
}

/// 解析 session_report 事件
WsSessionReport? parseSessionReport(Map<String, dynamic> data) {
  if (data['event'] != 'session_report') return null;
  final stage = data['stage'] as String? ?? 'preliminary';
  if (stage == 'preliminary') {
    return WsSessionReportPreliminary.fromJson(data);
  } else {
    return WsSessionReportFinal.fromJson(data);
  }
}

/// 解析 error 事件
WsError? parseError(Map<String, dynamic> data) {
  if (data['event'] != 'error') return null;
  return WsError(
    code: data['code'] as String? ?? 'UNKNOWN',
    message: data['message'] as String?,
    messageZh: data['message_zh'] as String?,
  );
}

/// 解析 asr_partial 事件
WsAsrPartial? parseAsrPartial(Map<String, dynamic> data) {
  if (data['event'] != 'asr_partial') return null;
  return WsAsrPartial(
    text: data['text'] as String? ?? '',
  );
}

/// 解析 test_ai_reply 事件
WsTestAiReply? parseTestAiReply(Map<String, dynamic> data) {
  if (data['event'] != 'test_ai_reply') return null;
  return WsTestAiReply(
    text: data['text'] as String? ?? '',
    phase: data['phase'] as String? ?? '',
  );
}

/// 解析 user_settings 辅助
UserSettings parseUserSettings(Map<String, dynamic> data) {
  return UserSettings(
    depthPreference: (data['depth_preference'] as num?)?.toDouble(),
    newTopicAppetite: (data['new_topic_appetite'] as num?)?.toDouble(),
    learnerLevel: data['learner_level'] as String?,
  );
}
