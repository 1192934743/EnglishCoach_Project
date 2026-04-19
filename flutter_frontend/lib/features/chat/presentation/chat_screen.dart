// lib/features/chat/presentation/chat_screen.dart
//
// 职责：
// 1. 主布局（AppBar、消息历史列表、底部操作按钮）
// 2. 学习进度条、话题切换等辅助 UI
//
// 已委托给独立组件：
// - ChatBubble         — 聊天回合气泡（含 AI 句子点击，教学面板）
// - AvatarAnimated     — 头像动画（含 Lottie，光效、角色标签）
// - GlowingLine       — AutoMode 呼吸光效

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/chat_provider.dart';
import '../../../core/providers/settings_provider.dart';
import '../widgets/chat_bubble.dart';
import '../widgets/avatar_animated.dart';
import 'session_report_sheet.dart';

// ── 国际化辅助（供 Widget 内部使用）────────────────────────────────────

String _tr(BuildContext ctx, String en, String zh) {
  // 简单中英文切换：检测 zh 是否含中文
  return RegExp(r'[\u4e00-\u9fff]').hasMatch(zh) ? zh : en;
}

// ── 话题请求底部表单 ─────────────────────────────────────────────────────

class _TopicRequestSheet extends ConsumerStatefulWidget {
  const _TopicRequestSheet({required this.onSubmit});
  final void Function(String) onSubmit;

  @override
  ConsumerState<_TopicRequestSheet> createState() => _TopicRequestSheetState();
}

class _TopicRequestSheetState extends ConsumerState<_TopicRequestSheet> {
  late final TextEditingController _controller;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  String _t(String en, String zh) => _tr(context, en, zh);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.of(context).viewInsets.bottom),
      child: Container(
        decoration: const BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
        ),
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Center(
              child: Container(
                width: 36, height: 4,
                decoration: BoxDecoration(
                  color: Colors.grey.shade300,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            ),
            const SizedBox(height: 16),
            Text(
              _t('Practice a Custom Topic', '自定义练习话题'),
              style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 6),
            Text(
              _t(
                'Describe any topic in English or Chinese — we\'ll create the perfect practice session.',
                '用中文或英文描述任意场景，我们会为你生成合适的对练内容。',
              ),
              style: TextStyle(fontSize: 13, color: Colors.grey[600]),
            ),
            const SizedBox(height: 14),
            TextField(
              controller: _controller,
              autofocus: true,
              maxLines: 2,
              decoration: InputDecoration(
                hintText: _t('e.g. "Ordering at Starbucks" or "机场值机"', '例如：星巴克点单、机场值机'),
                border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: const BorderSide(color: Colors.blueAccent, width: 2),
                ),
              ),
            ),
            const SizedBox(height: 14),
            SizedBox(
              width: double.infinity, height: 48,
              child: ElevatedButton(
                onPressed: () {
                  final desc = _controller.text.trim();
                  if (desc.isNotEmpty) {
                    widget.onSubmit(desc);
                    Navigator.of(context).pop();
                  }
                },
                style: ElevatedButton.styleFrom(
                  backgroundColor: Colors.blueAccent,
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                  elevation: 0,
                ),
                child: Text(
                  _t('Generate Practice Session', '生成练习场景'),
                  style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 15),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

Future<void> _showTopicRequestSheet(BuildContext context, WidgetRef ref) async {
  final notifier = ref.read(chatProvider.notifier);
  await showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.transparent,
    builder: (_) => _TopicRequestSheet(
      onSubmit: (desc) => notifier.requestTopic(desc),
    ),
  );
}

// ── 话题生成中横幅 ───────────────────────────────────────────────────────

class _TopicGeneratingBanner extends ConsumerWidget {
  const _TopicGeneratingBanner();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final isZh = ref.watch(settingsProvider).isChinese;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          colors: [
            Colors.blueAccent.withValues(alpha: 0.12),
            Colors.purpleAccent.withValues(alpha: 0.08),
          ],
        ),
        border: Border(
          bottom: BorderSide(color: Colors.blueAccent.withValues(alpha: 0.2)),
        ),
      ),
      child: Row(
        children: [
          const SizedBox(
            width: 16, height: 16,
            child: CircularProgressIndicator(strokeWidth: 2, color: Colors.blueAccent),
          ),
          const SizedBox(width: 10),
          Text(
            isZh ? '正在生成练习场景…' : 'Generating your practice topic...',
            style: TextStyle(
              fontSize: 12,
              color: Colors.blueAccent.shade700,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }
}

// ── 话题标题显示（来自 settings_provider）───────────────────────────────────
// chatTopicDisplayTitle 从 core/providers/settings_provider.dart 导入

// ── 主屏幕 ───────────────────────────────────────────────────────────────

class ChatScreen extends ConsumerStatefulWidget {
  const ChatScreen({super.key});
  @override
  ConsumerState<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends ConsumerState<ChatScreen>
    with SingleTickerProviderStateMixin {
  final ScrollController _scrollController = ScrollController();
  late final AnimationController _glowController;
  late final Animation<double> _glowAnimation;
  bool _showHistory = false;

  @override
  void initState() {
    super.initState();
    _glowController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    )..repeat(reverse: true);
    _glowAnimation = Tween<double>(begin: 0.1, end: 0.9).animate(
      CurvedAnimation(parent: _glowController, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _scrollController.dispose();
    _glowController.dispose();
    super.dispose();
  }

  String _t(String en, String zh) => _tr(context, en, zh);

  @override
  Widget build(BuildContext context) {
    final chatState = ref.watch(chatProvider);
    final settings = ref.watch(settingsProvider);
    final notifier = ref.read(chatProvider.notifier);

    final bool isListening = chatState.status == ChatStatus.listening;
    final bool isSpeaking = chatState.status == ChatStatus.speaking;
    final bool isFirstStart =
        chatState.chatHistory.isEmpty && chatState.status == ChatStatus.idle;
    final bool shouldShowButton = !settings.autoMode || isFirstStart;

    // Fix B1: autoMode 开启时自动开始
    ref.listen<SettingsState>(settingsProvider, (prev, next) {
      if (prev != null && next.autoMode && !prev.autoMode) {
        if (ref.read(chatProvider).status == ChatStatus.idle) {
          notifier.startListening();
        }
      }
    });

    // 消息监听
    ref.listen<ChatState>(chatProvider, (previous, next) {
      // 滚动到最新消息
      if (previous != null &&
          previous.chatHistory.length != next.chatHistory.length) {
        if (_scrollController.hasClients && _scrollController.offset > 50) {
          _scrollController.animateTo(
            0.0,
            duration: const Duration(milliseconds: 300),
            curve: Curves.easeOutCubic,
          );
        }
      }
      // 错误提示
      if (next.errorMessage != null &&
          next.errorMessage != previous?.errorMessage) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(next.errorMessage!),
            backgroundColor: Colors.redAccent,
            behavior: SnackBarBehavior.floating,
            duration: const Duration(seconds: 4),
          ),
        );
        WidgetsBinding.instance.addPostFrameCallback((_) {
          ref.read(chatProvider.notifier).clearError();
        });
      }
      // 报告卡弹出
      if (next.sessionReport != null && previous?.sessionReport == null) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          showSessionReportSheet(context).then((_) {
            ref.read(chatProvider.notifier).clearSessionReport();
          });
        });
      }
    });

    final String roleName = chatState.isFlipped
        ? _t("Customer (you're the coach)", '顾客（由你来当教练）')
        : chatState.currentRoleName;

    final String statusText = isListening
        ? _t('Listening...', '聆听中…')
        : (isSpeaking
            ? _t('Speaking...', '说话中…')
            : _t('Ready', '就绪'));

    final String buttonLabel = isListening
        ? _t("I'm listening...", '正在聆听…')
        : (isSpeaking
            ? _t('AI speaking...', 'AI 回复中…')
            : _t('Tap to start', '点击开始'));

    return Scaffold(
      backgroundColor: const Color(0xFFF4F6F9),
      appBar: _buildAppBar(chatState, notifier, settings),
      body: SafeArea(
        bottom: false,
        child: Stack(
          children: [
            // AutoMode 发光边条
            if (settings.autoMode && isListening)
              Positioned.fill(
                child: IgnorePointer(
                  child: AnimatedBuilder(
                    animation: _glowAnimation,
                    builder: (context, _) {
                      return Row(
                        children: [
                          SizedBox(
                            width: 6,
                            child: GlowingLine(glowAnimation: _glowAnimation),
                          ),
                          const Spacer(),
                          SizedBox(
                            width: 6,
                            child: GlowingLine(glowAnimation: _glowAnimation),
                          ),
                        ],
                      );
                    },
                  ),
                ),
              ),
            Column(
              children: [
                if (chatState.isGeneratingTopic) const _TopicGeneratingBanner(),
                if (settings.showProgressBar)
                  _buildMasteryTracker(chatState.masteryProgress, settings),
                // 头像区
                Container(
                  margin: EdgeInsets.only(
                    top: _showHistory
                        ? 12.0
                        : MediaQuery.of(context).size.height * 0.15,
                    bottom: _showHistory ? 12.0 : 0.0,
                  ),
                  child: Center(
                    child: AvatarAnimated(
                      isListening: isListening,
                      isSpeaking: isSpeaking,
                      isFlipped: chatState.isFlipped,
                      showHistory: _showHistory,
                      glowAnimation: _glowAnimation,
                      roleName: roleName,
                      statusText: statusText,
                    ),
                  ),
                ),
                // 消息历史
                Expanded(
                  child: _showHistory
                      ? Container(
                          width: double.infinity,
                          decoration: BoxDecoration(
                            color: Colors.white,
                            borderRadius: const BorderRadius.vertical(
                              top: Radius.circular(30),
                            ),
                            boxShadow: [
                              BoxShadow(
                                color: Colors.black.withValues(alpha: 0.04),
                                blurRadius: 15,
                                offset: const Offset(0, -5),
                              ),
                            ],
                          ),
                          child: chatState.chatHistory.isEmpty
                              ? _buildEmptyState(isListening, settings)
                              : ListView.builder(
                                  controller: _scrollController,
                                  reverse: true,
                                  padding: EdgeInsets.only(
                                    left: 16, right: 16, top: 20,
                                    bottom: shouldShowButton ? 120.0 : 20.0,
                                  ),
                                  physics: const BouncingScrollPhysics(),
                                  itemCount: chatState.chatHistory.length,
                                  itemBuilder: (context, index) {
                                    final reversedIndex =
                                        chatState.chatHistory.length - 1 - index;
                                    return ChatBubble(
                                      turn: chatState.chatHistory[reversedIndex],
                                      fontSize: settings.fontSize,
                                      showTranslation: settings.showTranslation,
                                      showHints: settings.showHints,
                                      showCorrection: settings.showCorrection,
                                      onHintTap: (s) => notifier.speakText(s),
                                    );
                                  },
                                ),
                        )
                      : const SizedBox.shrink(),
                ),
              ],
            ),
            // 底部操作按钮
            Align(
              alignment: Alignment.bottomCenter,
              child: Padding(
                padding: const EdgeInsets.only(bottom: 20),
                child: AnimatedOpacity(
                  opacity: shouldShowButton ? 1.0 : 0.0,
                  duration: const Duration(milliseconds: 400),
                  child: IgnorePointer(
                    ignoring: !shouldShowButton,
                    child: GestureDetector(
                    onTap: () => notifier.toggleButton(),
                    child: AnimatedContainer(
                      duration: const Duration(milliseconds: 300),
                      height: 42,
                      padding: const EdgeInsets.symmetric(horizontal: 24),
                      decoration: BoxDecoration(
                        color: isListening
                            ? Colors.pink[300]
                            : (isSpeaking
                                  ? Colors.purpleAccent
                                  : Colors.blueAccent),
                        borderRadius: BorderRadius.circular(21),
                        boxShadow: [
                          BoxShadow(
                            color: (isListening ? Colors.pink : Colors.blue)
                                .withValues(alpha: 0.2),
                            blurRadius: 10,
                            offset: const Offset(0, 4),
                          ),
                        ],
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(
                            isListening
                                ? Icons.graphic_eq_rounded
                                : (isSpeaking
                                      ? Icons.volume_up_rounded
                                      : Icons.mic_rounded),
                            color: Colors.white,
                            size: 18,
                          ),
                          const SizedBox(width: 8),
                          Text(
                            buttonLabel,
                            style: const TextStyle(
                              color: Colors.white,
                              fontWeight: FontWeight.bold,
                              fontSize: 13,
                            ),
                          ),
                        ],
                      ),
                    ),
                    ),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  AppBar _buildAppBar(
      ChatState chatState, ChatNotifier notifier, SettingsState settings) {
    final isZh = settings.isChinese;
    return AppBar(
      title: Text(
        chatTopicDisplayTitle(
          ref,
          chatState.currentTopicTitle,
          titleZh: chatState.currentTopicTitleZh.isEmpty
              ? null
              : chatState.currentTopicTitleZh,
        ),
        style:
            const TextStyle(fontWeight: FontWeight.bold, color: Colors.black87),
        overflow: TextOverflow.ellipsis,
      ),
      backgroundColor: Colors.white,
      elevation: 1,
      centerTitle: true,
      actions: [
        IconButton(
          icon: const Icon(Icons.add_comment_outlined, color: Colors.black87),
          tooltip: isZh ? '切换练习话题' : 'Change practice topic',
          onPressed: () => _showTopicRequestSheet(context, ref),
        ),
        PopupMenuButton<int>(
          icon: const Icon(Icons.psychology_alt, color: Colors.black87),
          tooltip: isZh ? '教练礼貌程度' : 'Coach politeness',
          onSelected: (level) => notifier.updatePoliteness(level),
          itemBuilder: (context) => [
            PopupMenuItem(
              value: 0,
              child: Text(isZh ? '刁钻暴躁' : 'Difficult & rude'),
            ),
            PopupMenuItem(
              value: 1,
              child:
                  Text(isZh ? '正常礼貌' : 'Normal & professional'),
            ),
            PopupMenuItem(
              value: 2,
              child: Text(isZh ? '极度客气' : 'Very warm & polite'),
            ),
          ],
        ),
        IconButton(
          icon: Icon(
            Icons.swap_horiz_rounded,
            color: chatState.isFlipped ? Colors.pinkAccent : Colors.black87,
          ),
          tooltip: isZh ? '切换角色' : 'Swap roles',
          onPressed: () => notifier.swapRole(),
        ),
        IconButton(
          icon: Icon(
            _showHistory
                ? Icons.speaker_notes_off_rounded
                : Icons.history_rounded,
            color: Colors.black87,
          ),
          onPressed: () => setState(() => _showHistory = !_showHistory),
        ),
        const SizedBox(width: 8),
      ],
    );
  }

  Widget _buildMasteryTracker(double progress, SettingsState settings) {
    final p = progress.clamp(0.0, 100.0);
    final isZh = settings.isChinese;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
      decoration: BoxDecoration(
        color: Colors.white,
        border: Border(bottom: BorderSide(color: Colors.grey.shade200)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                isZh ? '话题掌握度' : 'Topic Mastery',
                style: const TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                  color: Colors.black54,
                ),
              ),
              Text(
                '${p.toInt()}%',
                style: const TextStyle(
                  fontSize: 14,
                  fontWeight: FontWeight.bold,
                  color: Color(0xFF4CAF50),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          ClipRRect(
            borderRadius: BorderRadius.circular(10),
            child: AnimatedBuilder(
              animation: _glowAnimation,
              builder: (context, _) {
                return FractionallySizedBox(
                  widthFactor: (p / 100.0).clamp(0.0, 1.0),
                  child: Container(
                    height: 8,
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(10),
                      gradient: LinearGradient(
                        colors: [
                          const Color(0xFF66BB6A),
                          Colors.greenAccent.withValues(
                            alpha: 0.8 + _glowAnimation.value * 0.2,
                          ),
                          const Color(0xFF4CAF50),
                        ],
                        stops: const [0.0, 0.5, 1.0],
                      ),
                      boxShadow: [
                        BoxShadow(
                          color: Colors.greenAccent.withValues(
                            alpha: 0.4 * _glowAnimation.value,
                          ),
                          blurRadius: 6 * _glowAnimation.value,
                          spreadRadius: 1,
                        ),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildEmptyState(bool isListening, SettingsState settings) {
    final isZh = settings.isChinese;
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            isListening ? Icons.mic_rounded : Icons.waving_hand_rounded,
            color: isListening ? Colors.pinkAccent : Colors.orangeAccent,
            size: 56,
          ),
          const SizedBox(height: 20),
          Text(
            isListening
                ? (isZh ? '正在聆听…试试说 Hello！' : "I'm listening... Try saying 'Hello'!")
                : (isZh ? '点击下方按钮开始对话！' : 'Tap the pill below to start!'),
            textAlign: TextAlign.center,
            style: TextStyle(color: Colors.grey[600], fontSize: settings.fontSize),
          ),
        ],
      ),
    );
  }
}
