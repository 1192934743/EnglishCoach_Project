import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../../../core/providers/settings_provider.dart';
import '../../chat/providers/chat_provider.dart';
import '../../onboarding/onboarding_screen.dart';

class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final settings = ref.watch(settingsProvider);
    final notifier = ref.read(settingsProvider.notifier);
    final chatNotifier = ref.read(chatProvider.notifier);

    final double baseSize = settings.fontSize;

    return Scaffold(
      backgroundColor: const Color(0xFFF4F6F9),
      appBar: AppBar(
        title: Text(
          tr(ref, 'Settings', '应用设置'),
          style: TextStyle(
            fontWeight: FontWeight.bold,
            color: Colors.black87,
            fontSize: baseSize + 2,
          ),
        ),
        backgroundColor: Colors.white,
        elevation: 1,
        centerTitle: true,
      ),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          _buildSectionTitle(
            tr(ref, "Language & Interface", "语言与界面"),
            baseSize,
          ),
          _buildSwitchTile(
            tr(ref, "Chinese Interface", "中文界面"),
            tr(ref, "Switch UI text to Chinese", "将应用界面切换为中文"),
            settings.isChinese,
            notifier.toggleLanguage,
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
                        color: Colors.blueAccent,
                        fontSize: baseSize,
                      ),
                    ),
                  ],
                ),
                Slider(
                  value: settings.vadTimeout.toDouble(),
                  min: 500,
                  max: 2500,
                  divisions: 20,
                  activeColor: Colors.blueAccent,
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
                    color: Colors.grey,
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
                  activeColor: Colors.blueAccent,
                  onChanged: (val) => notifier.setFontSize(val),
                ),
                Text(
                  tr(ref, "Sample text preview", "这是字体大小的预览效果"),
                  style: TextStyle(
                    fontSize: settings.fontSize,
                    color: Colors.black54,
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
            decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(16)),
            child: ListTile(
              leading: const Icon(Icons.restart_alt_rounded, color: Colors.deepPurple),
              title: Text(
                tr(ref, "Redo Learning Setup", "重新进行学习偏好设置"),
                style: TextStyle(fontWeight: FontWeight.bold, fontSize: baseSize),
              ),
              subtitle: Text(
                tr(ref, "Retake the onboarding to update your preferences.", "重新完成初始化设置，更新你的学习参数"),
                style: TextStyle(fontSize: baseSize * 0.75, color: Colors.grey),
              ),
              trailing: const Icon(Icons.chevron_right, color: Colors.grey),
              onTap: () async {
                final prefs = await SharedPreferences.getInstance();
                await prefs.remove('onboarding_done');
                if (!context.mounted) return;
                // Navigate to OnboardingScreen as a full-screen modal
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
          _buildSliderTile(
            title: tr(ref, "Topic Depth", "话题深度"),
            subtitle: tr(ref, "How deep to go before moving on.", "每个话题练多深再切换"),
            valueLabel: _depthLabel(settings.depthPreference),
            value: settings.depthPreference,
            min: 1.0, max: 5.0, divisions: 4,
            color: Colors.deepPurple,
            baseSize: baseSize,
            onChanged: (val) {
              notifier.setDepthPreference(val);
              chatNotifier.updateLmsSettings(
                depthPreference: val,
                newTopicAppetite: settings.newTopicAppetite,
              );
            },
          ),
          const SizedBox(height: 10),
          _buildSliderTile(
            title: tr(ref, "Topic Exploration", "话题探索欲"),
            subtitle: tr(ref, "Balance between new topics and review.", "倾向于探索新话题 vs 反复巩固"),
            valueLabel: _appetiteLabel(settings.newTopicAppetite),
            value: settings.newTopicAppetite,
            min: 0.0, max: 1.0, divisions: 4,
            color: Colors.teal,
            baseSize: baseSize,
            onChanged: (val) {
              notifier.setNewTopicAppetite(val);
              chatNotifier.updateLmsSettings(
                depthPreference: settings.depthPreference,
                newTopicAppetite: val,
              );
            },
          ),
        ],
      ),
    );
  }

  String _depthLabel(double v) {
    const labels = ['Basics only', 'Beginner', 'Intermediate', 'Advanced', 'Expert'];
    return labels[(v.round() - 1).clamp(0, 4)];
  }

  String _appetiteLabel(double v) {
    if (v <= 0.15) return 'Review-focused';
    if (v <= 0.35) return 'Mostly review';
    if (v <= 0.55) return 'Balanced';
    if (v <= 0.75) return 'Mostly new';
    return 'Explorer';
  }

  Widget _buildSectionTitle(String title, double baseSize) => Padding(
    padding: const EdgeInsets.only(bottom: 10, left: 5),
    child: Text(
      title,
      style: TextStyle(
        fontWeight: FontWeight.bold,
        color: Colors.grey,
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
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(title, style: TextStyle(fontSize: baseSize, fontWeight: FontWeight.bold)),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 3),
                decoration: BoxDecoration(
                  color: color.withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text(
                  valueLabel,
                  style: TextStyle(fontSize: baseSize * 0.85, color: color, fontWeight: FontWeight.bold),
                ),
              ),
            ],
          ),
          Slider(
            value: value, min: min, max: max, divisions: divisions,
            activeColor: color,
            onChanged: onChanged,
          ),
          Text(subtitle, style: TextStyle(fontSize: baseSize * 0.8, color: Colors.grey)),
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
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
      ),
      child: SwitchListTile(
        secondary: Icon(icon, color: Colors.blueAccent, size: baseSize + 4),
        title: Text(
          title,
          style: TextStyle(fontWeight: FontWeight.bold, fontSize: baseSize),
        ),
        subtitle: Text(
          subtitle,
          style: TextStyle(fontSize: baseSize * 0.75, color: Colors.grey),
        ),
        value: value,
        activeColor: Colors.blueAccent,
        onChanged: onChanged,
      ),
    );
  }
}
