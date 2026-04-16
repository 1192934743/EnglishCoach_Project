import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../../core/providers/settings_provider.dart';

class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final settings = ref.watch(settingsProvider);
    final notifier = ref.read(settingsProvider.notifier);

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
        ],
      ),
    );
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
