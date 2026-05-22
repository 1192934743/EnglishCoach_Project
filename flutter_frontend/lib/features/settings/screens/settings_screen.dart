import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../../../core/network/backend_config.dart';
import '../../../core/network/server_debug_config.dart';
import '../../../core/network/user_manager.dart';
import '../../../core/network/websocket_client.dart';
import '../../../core/providers/settings_provider.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/config/dev_panel_config.dart';
import '../../chat/providers/chat_provider.dart';
import '../../onboarding/onboarding_screen.dart';

class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});

  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  @override
  Widget build(BuildContext context) {
    final settings = ref.watch(settingsProvider);
    final notifier = ref.read(settingsProvider.notifier);
    final chatNotifier = ref.read(chatProvider.notifier);

    final double baseSize = settings.fontSize;

    return Scaffold(
      backgroundColor: AppColors.background,
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(20),
          children: [
          // ── 开发者模式面板 ─────────────────────────────────────────────────────
          const SizedBox(height: 8),
          _DevModePanel(
            onServerChanged: () async {
              // 切换服务器后强制重连 WebSocket
              final wsClient = ref.read(websocketProvider);
              wsClient.disconnect();
              final userId = await UserManager.getOrCreateUuid();
              wsClient.setUserId(userId);
              wsClient.connect();
            },
          ),
          const SizedBox(height: 20),

          _buildSectionTitle(
            tr(ref, "Language & Interface", "语言与界面"),
            baseSize,
          ),
          _buildSwitchTile(
            tr(ref, "Chinese Interface", "中文界面"),
            tr(ref, "Switch UI text to Chinese", "将应用界面切换为中文"),
            settings.isChinese,
            (v) {
              notifier.toggleLanguage(v);
              SharedPreferences.getInstance().then(
                (p) => p.setBool('ui_is_chinese', v),
              );
            },
            Icons.language,
            baseSize,
          ),

          const SizedBox(height: 20),
          _buildSectionTitle(tr(ref, "Conversation Mode", "对话模式"), baseSize),
          _buildSwitchTile(
            tr(ref, "Auto-Chat (Hands-free)", "全双工免提模式"),
            tr(
              ref,
              "AI replies, you speak automatically.",
              "AI 回答完毕后自动开始聆听，无需点击",
            ),
            settings.autoMode,
            notifier.toggleAutoMode,
            Icons.autorenew,
            baseSize,
          ),
          _buildSwitchTile(
            tr(ref, "TTS Pre-warming", "TTS 预热加速"),
            tr(
              ref,
              "Warm up TTS connections at startup to reduce first-sentence latency by 200-500ms.",
              "服务启动时预热 TTS 连接，使首句合成延迟降低 200-500ms",
            ),
            settings.ttsPreWarming,
            notifier.setTtsPreWarming,
            Icons.bolt_rounded,
            baseSize,
          ),
          // TTS 引擎选择器
          Container(
            margin: const EdgeInsets.only(bottom: 10),
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: AppColors.surface,
              borderRadius: BorderRadius.circular(16),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(Icons.record_voice_over_rounded, color: AppColors.primary, size: 24),
                    const SizedBox(width: 12),
                    Text(
                      tr(ref, "TTS Voice Engine", "TTS 语音引擎"),
                      style: TextStyle(fontWeight: FontWeight.bold, fontSize: baseSize),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Text(
                  tr(
                    ref,
                    "Choose the voice synthesis engine for AI replies.",
                    "选择 AI 语音合成的引擎",
                  ),
                  style: TextStyle(fontSize: baseSize * 0.75, color: AppColors.textSecondary),
                ),
                const SizedBox(height: 12),
                DropdownButton<String>(
                  value: settings.ttsEngine,
                  isExpanded: true,
                  items: [
                    DropdownMenuItem<String>(
                      value: 'azure',
                      child: Row(
                        children: [
                          Icon(Icons.cloud_rounded, size: 18, color: AppColors.primary),
                          const SizedBox(width: 8),
                          Text(tr(ref, 'Azure Voice (Default)', 'Azure 语音 (默认)')),
                        ],
                      ),
                    ),
                    DropdownMenuItem<String>(
                      value: 'volcengine',
                      child: Row(
                        children: [
                          Icon(Icons.local_fire_department_rounded, size: 18, color: AppColors.warning),
                          const SizedBox(width: 8),
                          Text(tr(ref, 'Volcengine Voice', '火山引擎语音')),
                        ],
                      ),
                    ),
                  ],
                  onChanged: (v) async {
                    if (v == null) return;
                    // setTtsEngine 会自动重置音色到新引擎的默认值
                    notifier.setTtsEngine(v);
                    final newVoice = ref.read(settingsProvider).ttsVoice;
                    await chatNotifier.updateLmsSettings(
                      depthPreference: settings.depthPreference,
                      newTopicAppetite: settings.newTopicAppetite,
                      learnerLevel: settings.learnerLevel,
                      ttsEngine: v,
                      ttsVoice: newVoice,
                    );
                  },
                ),
              ],
            ),
          ),
          // TTS 音色选择器
          Container(
            margin: const EdgeInsets.only(bottom: 10),
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: AppColors.surface,
              borderRadius: BorderRadius.circular(16),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(Icons.person_outline_rounded, color: AppColors.accent, size: 24),
                    const SizedBox(width: 12),
                    Text(
                      tr(ref, "Coach Voice", "教练音色"),
                      style: TextStyle(fontWeight: FontWeight.bold, fontSize: baseSize),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Text(
                  tr(
                    ref,
                    "Choose the voice for your AI conversation coach.",
                    "选择 AI 教练的声音",
                  ),
                  style: TextStyle(fontSize: baseSize * 0.75, color: AppColors.textSecondary),
                ),
                const SizedBox(height: 12),
                Builder(
                  builder: (context) {
                    final voices = VoiceOptions.byEngine[settings.ttsEngine] ?? {};
                    return DropdownButton<String>(
                      value: voices.containsKey(settings.ttsVoice) ? settings.ttsVoice : VoiceOptions.defaultVoice(settings.ttsEngine),
                      isExpanded: true,
                      items: voices.entries.map((e) {
                        final isMale = e.key.contains('Guy') || e.key.contains('Ryan') || e.key.contains('BV002');
                        return DropdownMenuItem<String>(
                          value: e.key,
                          child: Row(
                            children: [
                              Icon(
                                isMale ? Icons.face_rounded : Icons.face_2_rounded,
                                size: 18,
                                color: isMale ? Colors.blue : Colors.pink,
                              ),
                              const SizedBox(width: 8),
                              Expanded(child: Text(e.value)),
                            ],
                          ),
                        );
                      }).toList(),
                      onChanged: (v) async {
                        if (v == null) return;
                        notifier.setTtsVoice(v);
                        await chatNotifier.updateLmsSettings(
                          depthPreference: settings.depthPreference,
                          newTopicAppetite: settings.newTopicAppetite,
                          learnerLevel: settings.learnerLevel,
                          ttsEngine: settings.ttsEngine,
                          ttsVoice: v,
                        );
                      },
                    );
                  },
                ),
              ],
            ),
          ),

          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: Colors.white,
              borderRadius: BorderRadius.circular(16),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(
                      tr(ref, "VAD Silence Timeout", "语音停顿检测时长"),
                      style: TextStyle(
                        fontSize: baseSize,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                    Text(
                      "${settings.vadTimeout} ms",
                      style: TextStyle(
                        fontWeight: FontWeight.bold,
                        color: AppColors.primary,
                        fontSize: baseSize,
                      ),
                    ),
                  ],
                ),
                Slider(
                  value: settings.vadTimeout.toDouble(),
                  min: 300,
                  max: 2000,
                  divisions: 17,
                  activeColor: AppColors.primary,
                  onChanged: (val) => notifier.setVadTimeout(val.toInt()),
                ),
                Text(
                  tr(
                    ref,
                    "Lower values make AI respond faster.",
                    "数值越小，AI 回应越快，但也更容易被打断",
                  ),
                  style: TextStyle(
                    fontSize: baseSize * 0.8,
                    color: AppColors.textSecondary,
                  ),
                ),
              ],
            ),
          ),

          const SizedBox(height: 20),
          _buildSectionTitle(tr(ref, "Display Settings", "显示设置"), baseSize),
          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: AppColors.surface,
              borderRadius: BorderRadius.circular(16),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(
                      tr(ref, "Font Size", "字体大小"),
                      style: TextStyle(fontSize: baseSize),
                    ),
                    Text(
                      "${settings.fontSize.toInt()} px",
                      style: const TextStyle(fontWeight: FontWeight.bold),
                    ),
                  ],
                ),
                Slider(
                  value: settings.fontSize,
                  min: 10.0,
                  max: 24.0,
                  divisions: 7,
                  activeColor: AppColors.primary,
                  onChanged: (val) => notifier.setFontSize(val),
                ),
                Text(
                  tr(ref, "Sample text preview", "这是字体大小的预览效果"),
                  style: TextStyle(
                    fontSize: settings.fontSize,
                    color: AppColors.textSecondary,
                  ),
                ),
              ],
            ),
          ),

          const SizedBox(height: 20),
          _buildSectionTitle(tr(ref, "Learning Modules", "教学模块展示"), baseSize),
          // 🌟 新增：进度条开关
          _buildSwitchTile(
            tr(ref, "Show Mastery Tracker", "显示学习进度条"),
            tr(ref, "Display your topic score on top.", "在聊天界面顶部展示打分进度"),
            settings.showProgressBar,
            notifier.toggleProgressBar,
            Icons.moving_rounded,
            baseSize,
          ),
          _buildSwitchTile(
            tr(ref, "Show Correction", "显示语法纠错"),
            tr(ref, "Grammar & pronunciation tips.", "显示老师对你上一句话的纠正"),
            settings.showCorrection,
            notifier.toggleCorrection,
            Icons.lightbulb_outline,
            baseSize,
          ),
          _buildSwitchTile(
            tr(ref, "Show Translation", "显示中文翻译"),
            tr(ref, "Chinese translation of AI replies.", "显示 AI 回复的中文翻译"),
            settings.showTranslation,
            notifier.toggleTranslation,
            Icons.translate,
            baseSize,
          ),
          _buildSwitchTile(
            tr(ref, "Show Response Hints", "显示推荐回复"),
            tr(ref, "Suggested things to say next.", "显示你接下来说什么好的提示"),
            settings.showHints,
            notifier.toggleHints,
            Icons.forum_outlined,
            baseSize,
          ),

          const SizedBox(height: 20),
          _buildSectionTitle(tr(ref, "Learning Parameters", "学习策略"), baseSize),
          // Redo onboarding button
          Container(
            margin: const EdgeInsets.only(bottom: 10),
            decoration: BoxDecoration(color: AppColors.surface, borderRadius: BorderRadius.circular(16)),
            child: ListTile(
              leading: Icon(Icons.restart_alt_rounded, color: AppColors.accent),
              title: Text(
                tr(ref, "Redo Learning Setup", "重新进行学习偏好设置"),
                style: TextStyle(fontWeight: FontWeight.bold, fontSize: baseSize),
              ),
              subtitle: Text(
                tr(ref, "Retake the onboarding to update your preferences.", "重新完成初始化设置，更新你的学习参数"),
                style: TextStyle(fontSize: baseSize * 0.75, color: AppColors.textSecondary),
              ),
              trailing: Icon(Icons.chevron_right, color: AppColors.textTertiary),
              onTap: () async {
                final prefs = await SharedPreferences.getInstance();
                await prefs.remove('onboarding_done');
                if (!context.mounted) return;
                await Navigator.of(context).push(
                  MaterialPageRoute<void>(
                    fullscreenDialog: true,
                    builder: (_) => OnboardingScreen(
                      onComplete: () => Navigator.of(context).pop(),
                    ),
                  ),
                );
              },
            ),
          ),
          Container(
            margin: const EdgeInsets.only(bottom: 10),
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
            decoration: BoxDecoration(
              color: AppColors.surface,
              borderRadius: BorderRadius.circular(16),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  tr(ref, "Your English level", "你的英语水平"),
                  style: TextStyle(fontWeight: FontWeight.bold, fontSize: baseSize),
                ),
                SizedBox(height: baseSize * 0.25),
                Text(
                  tr(
                    ref,
                    "Adjust how challenging and how fast-paced the coach feels.",
                    "调节教练的难度与节奏感。",
                  ),
                  style: TextStyle(fontSize: baseSize * 0.75, color: AppColors.textSecondary),
                ),
                SizedBox(height: baseSize * 0.5),
                DropdownButton<String>(
                  value: settings.learnerLevel,
                  isExpanded: true,
                  items: [
                    'Beginner',
                    'Elementary',
                    'Intermediate',
                    'Advanced',
                  ]
                      .map(
                        (e) => DropdownMenuItem<String>(
                          value: e,
                          child: Text(learnerLevelUiLabel(ref, e)),
                        ),
                      )
                      .toList(),
                  onChanged: (v) async {
                    if (v == null) return;
                    notifier.setLearnerLevel(v);
                    final prefs = await SharedPreferences.getInstance();
                    await prefs.setString('learner_level', v);
                    await chatNotifier.updateLmsSettings(
                      depthPreference: settings.depthPreference,
                      newTopicAppetite: settings.newTopicAppetite,
                      learnerLevel: v,
                      ttsEngine: settings.ttsEngine,
                      ttsVoice: settings.ttsVoice,
                    );
                  },
                ),
              ],
            ),
          ),
          _buildSliderTile(
            title: tr(ref, "Topic Depth", "话题深度"),
            subtitle: tr(ref, "How deep to go before moving on.", "每个话题练多深再切换"),
            valueLabel: _depthLabel(ref, settings.depthPreference),
            value: settings.depthPreference,
            min: 1.0, max: 5.0, divisions: 4,
            color: Colors.deepPurple,
            baseSize: baseSize,
            onChanged: (val) {
              notifier.setDepthPreference(val);
              chatNotifier.updateLmsSettings(
                depthPreference: val,
                newTopicAppetite: settings.newTopicAppetite,
                learnerLevel: settings.learnerLevel,
                ttsEngine: settings.ttsEngine,
                ttsVoice: settings.ttsVoice,
              );
            },
          ),
          const SizedBox(height: 10),
          _buildSliderTile(
            title: tr(ref, "Topic Exploration", "话题探索欲"),
            subtitle: tr(ref, "Balance between new topics and review.", "倾向于探索新话题 vs 反复巩固"),
            valueLabel: _appetiteLabel(ref, settings.newTopicAppetite),
            value: settings.newTopicAppetite,
            min: 0.0, max: 1.0, divisions: 4,
            color: Colors.teal,
            baseSize: baseSize,
            onChanged: (val) {
              notifier.setNewTopicAppetite(val);
              chatNotifier.updateLmsSettings(
                depthPreference: settings.depthPreference,
                newTopicAppetite: val,
                learnerLevel: settings.learnerLevel,
                ttsEngine: settings.ttsEngine,
                ttsVoice: settings.ttsVoice,
              );
            },
          ),
        ],
        ),
      ),
    );
  }

  String _depthLabel(WidgetRef ref, double v) {
    final i = (v.round() - 1).clamp(0, 4);
    final labels = [
      tr(ref, 'Basics only', '仅基础'),
      tr(ref, 'Beginner', '初级'),
      tr(ref, 'Intermediate', '中级'),
      tr(ref, 'Advanced', '高级'),
      tr(ref, 'Expert', '专家'),
    ];
    return labels[i];
  }

  String _appetiteLabel(WidgetRef ref, double v) {
    if (v <= 0.15) return tr(ref, 'Review-focused', '以巩固为主');
    if (v <= 0.35) return tr(ref, 'Mostly review', '偏重复习');
    if (v <= 0.55) return tr(ref, 'Balanced', '均衡');
    if (v <= 0.75) return tr(ref, 'Mostly new', '偏重新话题');
    return tr(ref, 'Explorer', '探索型');
  }

  Widget _buildSectionTitle(String title, double baseSize) => Padding(
    padding: const EdgeInsets.only(bottom: 10, left: 5),
    child: Text(
      title,
      style: TextStyle(
        fontWeight: FontWeight.bold,
        color: AppColors.textSecondary,
        fontSize: baseSize * 0.85,
      ),
    ),
  );

  Widget _buildSliderTile({
    required String title,
    required String subtitle,
    required String valueLabel,
    required double value,
    required double min,
    required double max,
    required int divisions,
    required Color color,
    required double baseSize,
    required ValueChanged<double> onChanged,
  }) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(16),
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
                  style: TextStyle(fontSize: baseSize, fontWeight: FontWeight.bold),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              const SizedBox(width: 8),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 3),
                decoration: BoxDecoration(
                  color: color.withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text(
                  valueLabel,
                  style: TextStyle(fontSize: baseSize * 0.85, color: color, fontWeight: FontWeight.bold),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ],
          ),
          Slider(
            value: value, min: min, max: max, divisions: divisions,
            activeColor: color,
            onChanged: onChanged,
          ),
          Text(subtitle, style: TextStyle(fontSize: baseSize * 0.8, color: AppColors.textSecondary)),
        ],
      ),
    );
  }

  Widget _buildSwitchTile(
    String title,
    String subtitle,
    bool value,
    Function(bool) onChanged,
    IconData icon,
    double baseSize,
  ) {
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(16),
      ),
      child: SwitchListTile(
        secondary: Icon(icon, color: AppColors.primary, size: baseSize + 4),
        title: Text(
          title,
          style: TextStyle(fontWeight: FontWeight.bold, fontSize: baseSize),
        ),
        subtitle: Text(
          subtitle,
          style: TextStyle(fontSize: baseSize * 0.75, color: AppColors.textSecondary),
        ),
        value: value,
        activeTrackColor: AppColors.primary.withValues(alpha: 0.5),
        onChanged: onChanged,
      ),
    );
  }
}

// ── 开发者模式面板 Widget ──────────────────────────────────────────────────────
class _DevModePanel extends ConsumerStatefulWidget {
  final VoidCallback onServerChanged;
  const _DevModePanel({required this.onServerChanged});

  @override
  ConsumerState<_DevModePanel> createState() => _DevModePanelState();
}

class _DevModePanelState extends ConsumerState<_DevModePanel> {
  bool _devModeEnabled = false;
  String _selectedModel = 'deepseek-chat';
  bool _initialized = false;

  @override
  void initState() {
    super.initState();
    _loadSettings();
  }

  Future<void> _loadSettings() async {
    final prefs = await SharedPreferences.getInstance();
    if (mounted) {
      setState(() {
        _devModeEnabled = prefs.getBool(DevPanelConfig.devModeKey) ?? false;
        final savedModel = prefs.getString(DevPanelConfig.llmModelKey) ?? 'deepseek-chat';
        final availableModels = DevPanelConfig.panels[0].options ?? [];
        _selectedModel = availableModels.contains(savedModel) ? savedModel : (availableModels.isNotEmpty ? availableModels.first : 'deepseek-chat');
        _initialized = true;
      });
    }
  }

  Future<void> _saveDevMode(bool value) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(DevPanelConfig.devModeKey, value);
    setState(() => _devModeEnabled = value);
  }

  Future<void> _saveModel(String model) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(DevPanelConfig.llmModelKey, model);
    setState(() => _selectedModel = model);
  }

  @override
  Widget build(BuildContext context) {
    if (!_initialized) {
      return const SizedBox.shrink();
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildSectionTitle('开发者模式', 14),
        Container(
          decoration: BoxDecoration(
            color: AppColors.surface,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: AppColors.warning.withValues(alpha: 0.3), width: 1.5),
          ),
          child: Column(
            children: [
              SwitchListTile(
                secondary: Icon(Icons.bug_report_rounded, color: AppColors.warning),
                title: const Text(
                  '开发者模式',
                  style: TextStyle(fontWeight: FontWeight.bold),
                ),
                subtitle: const Text(
                  '开启后可调试 LLM 模型配置',
                  style: TextStyle(fontSize: 12),
                ),
                value: _devModeEnabled,
                activeTrackColor: AppColors.warning.withValues(alpha: 0.5),
                onChanged: _saveDevMode,
              ),
              if (_devModeEnabled) ...[
                const Divider(height: 1),
                Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text(
                        '调试选项',
                        style: TextStyle(
                          fontWeight: FontWeight.w600,
                          fontSize: 13,
                          color: AppColors.textSecondary,
                        ),
                      ),
                      const SizedBox(height: 12),
                      // LLM 模型选择
                      ListTile(
                        contentPadding: EdgeInsets.zero,
                        leading: Icon(
                          DevPanelConfig.panels[0].icon,
                          color: DevPanelConfig.panels[0].iconColor,
                        ),
                        title: Text(
                          DevPanelConfig.panels[0].label,
                          style: const TextStyle(fontSize: 14),
                        ),
                        subtitle: Text(
                          DevPanelConfig.panels[0].subtitle ?? '',
                          style: const TextStyle(fontSize: 11),
                        ),
                        trailing: DropdownButton<String>(
                          value: _selectedModel,
                          underline: const SizedBox(),
                          items: DevPanelConfig.panels[0].options!
                              .map((m) => DropdownMenuItem(
                                    value: m,
                                    child: Text(m, style: const TextStyle(fontSize: 12)),
                                  ))
                              .toList(),
                          onChanged: (v) {
                            if (v != null) _saveModel(v);
                          },
                        ),
                      ),
                      const SizedBox(height: 12),
                      // 服务器配置
                      _ServerSelector(
                        onServerChanged: widget.onServerChanged,
                      ),
                      const SizedBox(height: 8),
                      Container(
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                          color: AppColors.warning.withValues(alpha: 0.1),
                          borderRadius: BorderRadius.circular(8),
                          border: Border.all(color: AppColors.warning.withValues(alpha: 0.3)),
                        ),
                        child: Row(
                          children: [
                            Icon(Icons.info_outline, size: 16, color: AppColors.warning),
                            const SizedBox(width: 8),
                            Expanded(
                              child: Text(
                                '开发者模式不走容灾，直接调用指定模型',
                                style: TextStyle(
                                  fontSize: 11,
                                  color: AppColors.warning,
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildSectionTitle(String title, double baseSize) => Padding(
    padding: const EdgeInsets.only(bottom: 10, left: 5),
    child: Text(
      title,
      style: TextStyle(
        fontWeight: FontWeight.bold,
        color: AppColors.warning,
        fontSize: baseSize,
      ),
    ),
  );
}

// ── 服务器选择器 Widget ──────────────────────────────────────────────────────
class _ServerSelector extends ConsumerStatefulWidget {
  final VoidCallback onServerChanged;
  const _ServerSelector({required this.onServerChanged});

  @override
  ConsumerState<_ServerSelector> createState() => _ServerSelectorState();
}

class _ServerSelectorState extends ConsumerState<_ServerSelector> {
  ServerPreset _currentPreset = ServerPreset.local;
  String _customHost = '';
  String _customPort = '8000';
  bool _initialized = false;

  @override
  void initState() {
    super.initState();
    _loadPreset();
  }

  Future<void> _loadPreset() async {
    final preset = await getServerPreset();
    final customHost = await getCustomHost() ?? '';
    final customPort = await getCustomPort() ?? 8000;
    if (mounted) {
      setState(() {
        _currentPreset = preset;
        _customHost = customHost;
        _customPort = customPort.toString();
        _initialized = true;
      });
    }
  }

  Future<void> _onPresetChanged(ServerPreset preset) async {
    setState(() => _currentPreset = preset);
    await switchServerPreset(preset);
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('${tr(ref, 'Server switched to:', '已切换到服务器：')} ${_presetLabel(preset)}'),
          duration: const Duration(seconds: 2),
        ),
      );
      widget.onServerChanged();
    }
  }

  Future<void> _onCustomApply() async {
    final host = _customHost.trim();
    final port = int.tryParse(_customPort.trim()) ?? 8000;
    if (host.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(tr(ref, 'Host cannot be empty', '服务器地址不能为空'))),
      );
      return;
    }
    final ok = await switchServerPreset(ServerPreset.custom, customHost: host, customPort: port);
    if (ok && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('${tr(ref, 'Server switched to:', '已切换到服务器：')} $host:$port')),
      );
      widget.onServerChanged();
    }
  }

  String _presetLabel(ServerPreset preset) {
    switch (preset) {
      case ServerPreset.local:
        return '$kDefaultLocalHost:8000';
      case ServerPreset.remote:
        return '$kDefaultRemoteHost:8000';
      case ServerPreset.custom:
        return 'Custom';
    }
  }

  @override
  Widget build(BuildContext context) {
    if (!_initialized) {
      return Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(color: AppColors.surface, borderRadius: BorderRadius.circular(16)),
        child: const Center(child: CircularProgressIndicator()),
      );
    }

    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: AppColors.surfaceVariant,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: AppColors.border),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.dns_rounded, color: AppColors.success, size: 22),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  '${tr(ref, 'Current:', '当前：')} $kBackendHost:$kBackendPort',
                  style: const TextStyle(fontWeight: FontWeight.w500, fontSize: 14),
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  color: AppColors.success.withValues(alpha: 0.1),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Text(
                  _currentPreset == ServerPreset.custom ? 'Custom' : _currentPreset.name,
                  style: TextStyle(color: AppColors.success, fontWeight: FontWeight.bold, fontSize: 12),
                ),
              ),
            ],
          ),
          const SizedBox(height: 14),
          const Divider(height: 1),
          const SizedBox(height: 14),

          // 预设选项
          ...[
            (ServerPreset.local, '$kDefaultLocalHost:8000', Icons.home_rounded, '本地服务器'),
            (ServerPreset.remote, '$kDefaultRemoteHost:8000', Icons.cloud_rounded, '远端服务器'),
          ].map((item) {
            final preset = item.$1;
            final address = item.$2;
            final icon = item.$3;
            final label = item.$4;
            final isSelected = _currentPreset == preset;
            return InkWell(
              onTap: () => _onPresetChanged(preset),
              borderRadius: BorderRadius.circular(10),
              child: Container(
                margin: const EdgeInsets.only(bottom: 8),
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                decoration: BoxDecoration(
                  color: isSelected ? AppColors.primary.withValues(alpha: 0.08) : AppColors.surfaceVariant,
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(
                    color: isSelected ? AppColors.primary : AppColors.border,
                    width: isSelected ? 1.5 : 1,
                  ),
                ),
                child: Row(
                  children: [
                    Icon(icon, size: 18, color: isSelected ? AppColors.primary : AppColors.textTertiary),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(label, style: TextStyle(fontWeight: FontWeight.w600, fontSize: 13, color: isSelected ? AppColors.primary : AppColors.textPrimary)),
                          Text(address, style: TextStyle(fontSize: 11, color: AppColors.textSecondary)),
                        ],
                      ),
                    ),
                    if (isSelected)
                      Icon(Icons.check_circle, color: AppColors.primary, size: 18),
                  ],
                ),
              ),
            );
          }),

          // 自定义选项
          Container(
            margin: const EdgeInsets.only(bottom: 8),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            decoration: BoxDecoration(
              color: _currentPreset == ServerPreset.custom ? AppColors.primary.withValues(alpha: 0.08) : AppColors.surfaceVariant,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(
                color: _currentPreset == ServerPreset.custom ? AppColors.primary : AppColors.border,
                width: _currentPreset == ServerPreset.custom ? 1.5 : 1,
              ),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                InkWell(
                  onTap: () => _onPresetChanged(ServerPreset.custom),
                  child: Row(
                    children: [
                      Icon(Icons.edit_rounded, size: 18, color: _currentPreset == ServerPreset.custom ? AppColors.primary : AppColors.textTertiary),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(
                          tr(ref, 'Custom', '自定义'),
                          style: TextStyle(fontWeight: FontWeight.w600, fontSize: 13, color: _currentPreset == ServerPreset.custom ? AppColors.primary : AppColors.textPrimary),
                        ),
                      ),
                      if (_currentPreset == ServerPreset.custom)
                        Icon(Icons.check_circle, color: AppColors.primary, size: 18),
                    ],
                  ),
                ),
                if (_currentPreset == ServerPreset.custom) ...[
                  const SizedBox(height: 10),
                  Row(
                    children: [
                      Expanded(
                        flex: 2,
                        child: TextField(
                          controller: TextEditingController(text: _customHost),
                          decoration: InputDecoration(
                            hintText: 'IP / Host',
                            isDense: true,
                            contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                            border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                          ),
                          style: const TextStyle(fontSize: 13),
                          onChanged: (v) => _customHost = v,
                        ),
                      ),
                      const SizedBox(width: 8),
                      const Text(':', style: TextStyle(fontSize: 16)),
                      const SizedBox(width: 8),
                      Expanded(
                        child: TextField(
                          controller: TextEditingController(text: _customPort),
                          decoration: InputDecoration(
                            hintText: 'Port',
                            isDense: true,
                            contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                            border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                          ),
                          style: const TextStyle(fontSize: 13),
                          keyboardType: TextInputType.number,
                          onChanged: (v) => _customPort = v,
                        ),
                      ),
                      const SizedBox(width: 8),
                      ElevatedButton(
                        onPressed: _onCustomApply,
                        style: ElevatedButton.styleFrom(
                          backgroundColor: AppColors.primary,
                          foregroundColor: Colors.white,
                          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                          minimumSize: Size.zero,
                          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                        ),
                        child: Text(tr(ref, 'Apply', '应用'), style: const TextStyle(fontSize: 13)),
                      ),
                    ],
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}
