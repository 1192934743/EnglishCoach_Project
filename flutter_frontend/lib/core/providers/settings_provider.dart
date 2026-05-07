// lib/core/providers/settings_provider.dart
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// TTS 音色选项配置：{引擎名: {音色ID: 显示名称}}
class VoiceOptions {
  static const Map<String, Map<String, String>> byEngine = {
    'azure': {
      'en-US-AriaNeural': 'Aria (女声, 美音)',
      'en-US-GuyNeural': 'Guy (男声, 美音)',
      'en-US-JennyNeural': 'Jenny (女声, 美音)',
      'en-GB-SoniaNeural': 'Sonia (女声, 英音)',
      'en-GB-RyanNeural': 'Ryan (男声, 英音)',
      'en-AU-NatashaNeural': 'Natasha (女声, 澳音)',
    },
    'volcengine': {
      // 注意：此处的 speaker 必须与 config.env 中 VOLC_RESOURCE_ID_TTS 对应的服务支持的发音人匹配
      'en_male_tim_uranus_bigtts': 'Tim (男声, 美音)',
    },
  };

  /// 获取指定引擎的默认音色 ID
  static String defaultVoice(String engine) {
    final voices = byEngine[engine];
    if (voices == null || voices.isEmpty) return 'en-US-AriaNeural';
    return voices.keys.first;
  }

  /// 检查音色 ID 是否属于指定引擎
  static bool isVoiceValid(String engine, String voiceId) {
    final voices = byEngine[engine];
    return voices != null && voices.containsKey(voiceId);
  }
}

class SettingsState {
  final bool isChinese;
  final bool autoMode;
  final double fontSize;
  final bool showCorrection;
  final bool showTranslation;
  final bool showHints;
  final int vadTimeout;
  final bool showProgressBar;
  final double depthPreference;
  final double newTopicAppetite;
  // TTS 预热开关：启用后服务启动时预热 TTS 连接池，首句延迟降低 200-500ms
  final bool ttsPreWarming;
  // 用户英语熟练度等级
  final String learnerLevel;
  // TTS 引擎选择：azure 或 volcengine
  final String ttsEngine;
  // TTS 音色选择：对应的 voice_id
  final String ttsVoice;

  SettingsState({
    required this.isChinese,
    required this.autoMode,
    required this.fontSize,
    required this.showCorrection,
    required this.showTranslation,
    required this.showHints,
    required this.vadTimeout,
    required this.showProgressBar,
    this.depthPreference = 1.0,
    this.newTopicAppetite = 0.2,
    this.ttsPreWarming = true,
    this.learnerLevel = "Intermediate",
    this.ttsEngine = "azure",
    this.ttsVoice = "en-US-AriaNeural",
  });

  SettingsState copyWith({
    bool? isChinese,
    bool? autoMode,
    double? fontSize,
    bool? showCorrection,
    bool? showTranslation,
    bool? showHints,
    int? vadTimeout,
    bool? showProgressBar,
    double? depthPreference,
    double? newTopicAppetite,
    bool? ttsPreWarming,
    String? learnerLevel,
    String? ttsEngine,
    String? ttsVoice,
  }) {
    return SettingsState(
      isChinese: isChinese ?? this.isChinese,
      autoMode: autoMode ?? this.autoMode,
      fontSize: fontSize ?? this.fontSize,
      showCorrection: showCorrection ?? this.showCorrection,
      showTranslation: showTranslation ?? this.showTranslation,
      showHints: showHints ?? this.showHints,
      vadTimeout: vadTimeout ?? this.vadTimeout,
      showProgressBar: showProgressBar ?? this.showProgressBar,
      depthPreference: depthPreference ?? this.depthPreference,
      newTopicAppetite: newTopicAppetite ?? this.newTopicAppetite,
      ttsPreWarming: ttsPreWarming ?? this.ttsPreWarming,
      learnerLevel: learnerLevel ?? this.learnerLevel,
      ttsEngine: ttsEngine ?? this.ttsEngine,
      ttsVoice: ttsVoice ?? this.ttsVoice,
    );
  }
}

class SettingsNotifier extends Notifier<SettingsState> {
  SharedPreferences? _prefs;
  bool _initialized = false;

  /// 确保持久化设置已加载完成
  Future<void> ensureInitialized() async {
    if (_initialized && _prefs != null) return;
    _prefs ??= await SharedPreferences.getInstance();
    final engine = _prefs!.getString('tts_engine');
    final voice = _prefs!.getString('tts_voice');
    if (engine != null && VoiceOptions.byEngine.containsKey(engine)) {
      if (state.ttsEngine == "azure" || !VoiceOptions.byEngine.containsKey(state.ttsEngine)) {
        final validVoice = VoiceOptions.isVoiceValid(engine, voice ?? '')
            ? voice!
            : VoiceOptions.defaultVoice(engine);
        state = state.copyWith(ttsEngine: engine, ttsVoice: validVoice);
      }
    }
    _initialized = true;
  }

  @override
  SettingsState build() {
    // 触发异步初始化（非阻塞）
    ensureInitialized();
    return SettingsState(
      isChinese: true,
      autoMode: true,
      fontSize: 12.0,
      showCorrection: false,
      showTranslation: false,
      showHints: true,
      vadTimeout: 700,
      showProgressBar: true,
    );
  }

  void toggleLanguage(bool val) => state = state.copyWith(isChinese: val);
  void toggleAutoMode(bool val) => state = state.copyWith(autoMode: val);
  void setFontSize(double val) => state = state.copyWith(fontSize: val);
  void toggleCorrection(bool val) =>
      state = state.copyWith(showCorrection: val);
  void toggleTranslation(bool val) =>
      state = state.copyWith(showTranslation: val);
  void toggleHints(bool val) => state = state.copyWith(showHints: val);
  void setVadTimeout(int val) => state = state.copyWith(vadTimeout: val);
  void toggleProgressBar(bool val) =>
      state = state.copyWith(showProgressBar: val);
  void setDepthPreference(double val) =>
      state = state.copyWith(depthPreference: val);
  void setNewTopicAppetite(double val) =>
      state = state.copyWith(newTopicAppetite: val);
  void setTtsPreWarming(bool val) =>
      state = state.copyWith(ttsPreWarming: val);
  // 更新等级的方法
  void setLearnerLevel(String val) => state = state.copyWith(learnerLevel: val);
  // TTS 引擎选择 - 切换引擎时自动重置音色
  void setTtsEngine(String val) {
    final currentVoice = state.ttsVoice;
    // 检查当前音色是否在新引擎的选项中
    String newVoice;
    if (!VoiceOptions.isVoiceValid(val, currentVoice)) {
      // 不在则重置为新引擎的默认音色
      newVoice = VoiceOptions.defaultVoice(val);
    } else {
      newVoice = currentVoice;
    }
    state = state.copyWith(ttsEngine: val, ttsVoice: newVoice);
    _persistTtsSettings(val, newVoice);
  }
  // TTS 音色选择
  void setTtsVoice(String val) {
    state = state.copyWith(ttsVoice: val);
    _persistTtsSettings(state.ttsEngine, val);
  }

  Future<void> _persistTtsSettings(String engine, String voice) async {
    _prefs ??= await SharedPreferences.getInstance();
    await _prefs!.setString('tts_engine', engine);
    await _prefs!.setString('tts_voice', voice);
  }
}

final settingsProvider = NotifierProvider<SettingsNotifier, SettingsState>(
  () => SettingsNotifier(),
);

String tr(WidgetRef ref, String en, String cn) {
  return ref.watch(settingsProvider).isChinese ? cn : en;
}

/// LMS / 设置里使用的 canonical 等级（英文）在界面上的显示名；传给后端的 [canonical] 仍为英文。
String learnerLevelUiLabel(WidgetRef ref, String canonical) {
  switch (canonical.trim()) {
    case 'Beginner':
      return tr(ref, 'Beginner', '入门（零基础）');
    case 'Elementary':
      return tr(ref, 'Elementary', '初级');
    case 'Intermediate':
      return tr(ref, 'Intermediate', '中级');
    case 'Advanced':
      return tr(ref, 'Advanced', '高级');
    default:
      return canonical.trim();
  }
}

/// 种子库 / 兜底话题的英文标题 → 中文界面展示名（与 DB `topics.title` 一致）。
/// AI 新生成等未知标题保持英文；请求后端仍用英文 `title`。
const Map<String, String> _kTopicTitleZh = {
  "McDonald's Ordering": '麦当劳点餐',
  'Technical Job Interview': '技术岗位面试',
  'Daily Casual Conversation': '日常闲聊',
  'General English Conversation': '通用英语对话',
  'Daily Conversation': '日常对话',
  'Simulation Practice': '场景模拟对练',
};

String topicTitleUiLabel(WidgetRef ref, String englishTitle) {
  if (!ref.watch(settingsProvider).isChinese) return englishTitle;
  final key = englishTitle.trim();
  if (key.isEmpty) return englishTitle;
  final direct = _kTopicTitleZh[key];
  if (direct != null) return direct;
  for (final e in _kTopicTitleZh.entries) {
    if (e.key.toLowerCase() == key.toLowerCase()) return e.value;
  }
  return englishTitle;
}

/// 聊天/统计等：中文界面优先用后端 `title_zh`，否则用种子映射，再否则英文标题。
String chatTopicDisplayTitle(
  WidgetRef ref,
  String englishTitle, {
  String? titleZh,
}) {
  if (!ref.watch(settingsProvider).isChinese) return englishTitle;
  final z = titleZh?.trim();
  if (z != null && z.isNotEmpty) return z;
  return topicTitleUiLabel(ref, englishTitle);
}

// ── 掌握度三档：仅影响 App 文案与图形，不改变后端算法。阈值与后端常量对齐。──

/// 与 `python_backend/application/services/session_planner.py` 中
/// `MASTERY_AUTO_PICK_SOFT_CAP` 保持数值一致（自动选题软上限、话题列表「已扎实」筛选）。
const double kMasteryAutoPickSoftCapPercent = 88.0;

/// 与同一文件中 `MASTERY_THRESHOLD_FOR_TIER_UP` 保持数值一致（深度晋级均值门槛）。
const double kMasteryTierUpThresholdPercent = 75.0;

/// 平均掌握度 0–100 → 三档**展示**（不向用户展示具体数字）。
enum MasteryUiBand { exploring, advancing, fluent }

MasteryUiBand masteryUiBandFromPercent(double percent0to100) {
  final p = percent0to100.clamp(0.0, 100.0);
  if (p >= kMasteryAutoPickSoftCapPercent) return MasteryUiBand.fluent;
  if (p >= kMasteryTierUpThresholdPercent) return MasteryUiBand.advancing;
  return MasteryUiBand.exploring;
}

String masteryUiBandLabel(WidgetRef ref, MasteryUiBand band) {
  switch (band) {
    case MasteryUiBand.exploring:
      return tr(ref, 'Getting started', '起步阶段');
    case MasteryUiBand.advancing:
      return tr(ref, 'Making progress', '稳步提升');
    case MasteryUiBand.fluent:
      return tr(ref, 'Strong grasp', '掌握扎实');
  }
}

/// 与 [masteryUiBandFromPercent] 对应：1 / 2 / 3 颗星（示意档位，非精确数值）。
int masteryStarCount(MasteryUiBand band) {
  switch (band) {
    case MasteryUiBand.exploring:
      return 1;
    case MasteryUiBand.advancing:
      return 2;
    case MasteryUiBand.fluent:
      return 3;
  }
}

Color masteryUiBandColor(MasteryUiBand band) {
  switch (band) {
    case MasteryUiBand.exploring:
      return const Color(0xFFFF8A65);
    case MasteryUiBand.advancing:
      return const Color(0xFF42A5F5);
    case MasteryUiBand.fluent:
      return const Color(0xFF43A047);
  }
}

// 【阶段四新增】控制底部导航栏的 Tab 索引
// 0 = Chat, 1 = Topics, 2 = Stats, 3 = Settings
class MainTabNotifier extends Notifier<int> {
  @override
  int build() => 0;

  void setTab(int index) => state = index;
}

final mainTabIndexProvider = NotifierProvider<MainTabNotifier, int>(
  () => MainTabNotifier(),
);
