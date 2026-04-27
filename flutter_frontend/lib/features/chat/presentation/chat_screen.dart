import 'package:flutter/material.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lottie/lottie.dart';
import 'dart:ui' as ui;
import '../providers/chat_provider.dart';
import '../widgets/scenario_goal_card.dart';
import '../../../core/providers/settings_provider.dart';
import 'session_report_sheet.dart';

// ── Topic-change bottom sheet ─────────────────────────────────────────────
/// Owns [TextEditingController] so dispose order matches the modal route (avoids
/// "used after being disposed" when the sheet closes with IME / focus transitions).
class _TopicRequestSheet extends ConsumerStatefulWidget {
  const _TopicRequestSheet({required this.onSubmit});
  final void Function(String description) onSubmit;

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

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(
        bottom: MediaQuery.of(context).viewInsets.bottom,
      ),
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
                width: 36,
                height: 4,
                decoration: BoxDecoration(
                  color: Colors.grey.shade300,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            ),
            const SizedBox(height: 16),
            Text(
              tr(ref, 'Practice a Custom Topic', '自定义练习话题'),
              style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 6),
            Text(
              tr(
                ref,
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
                hintText: tr(
                  ref,
                  'e.g. "Ordering at Starbucks" or "机场值机"',
                  '例如：星巴克点单、机场值机',
                ),
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: const BorderSide(
                    color: Colors.blueAccent,
                    width: 2,
                  ),
                ),
              ),
            ),
            const SizedBox(height: 14),
            SizedBox(
              width: double.infinity,
              height: 48,
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
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                  elevation: 0,
                ),
                child: Text(
                  tr(ref, 'Generate Practice Session', '生成练习场景'),
                  style: const TextStyle(
                    color: Colors.white,
                    fontWeight: FontWeight.bold,
                    fontSize: 15,
                  ),
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
    builder: (_) =>
        _TopicRequestSheet(onSubmit: (desc) => notifier.requestTopic(desc)),
  );
}

class ChatScreen extends ConsumerStatefulWidget {
  const ChatScreen({super.key});

  @override
  ConsumerState<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends ConsumerState<ChatScreen>
    with SingleTickerProviderStateMixin {
  final ScrollController _scrollController = ScrollController();
  late AnimationController _glowController;
  late Animation<double> _glowAnimation;

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

  @override
  Widget build(BuildContext context) {
    final chatState = ref.watch(chatProvider);
    final settings = ref.watch(settingsProvider);
    final notifier = ref.read(chatProvider.notifier);

    final isListening = chatState.status == ChatStatus.listening;
    final isSpeaking = chatState.status == ChatStatus.speaking;

    final bool isFirstStart =
        chatState.chatHistory.isEmpty && chatState.status == ChatStatus.idle;
    final bool shouldShowButton = !settings.autoMode || isFirstStart;

    // Fix B1: Guard with prev != null — prevents auto-firing on first render
    ref.listen<SettingsState>(settingsProvider, (prev, next) {
      if (prev != null && next.autoMode && !prev.autoMode) {
        if (ref.read(chatProvider).status == ChatStatus.idle) {
          notifier.startListening();
        }
      }
    });

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

      // LLM 超时 / 错误：弹出 Snackbar，展示后立刻清空 errorMessage
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

      // 🌟 学习报告卡：preliminary 到达时弹出底部面板
      if (next.sessionReport != null && previous?.sessionReport == null) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          showSessionReportSheet(context).then((_) {
            // 面板 dismiss 后清空状态，防止重复弹出
            ref.read(chatProvider.notifier).clearSessionReport();
          });
        });
      }

      // 🌟 【阶段三新增】微场景流转 SnackBar：
      //   当 currentScenarioName 发生变化时（说明后端刚下发 scenario_transition 事件），
      //   触发一个 Floating SnackBar 闯关庆祝提示。
      //
      //   Riverpod 最佳实践：
      //     - 纯状态（ChatState）只存储数据，不含任何一次性动作标记
      //     - 一次性 UI 副作用（SnackBar）统一在 ref.listen 中处理，
      //       不污染 State，也不引入不必要的 Widget 重建
      //
      //   修正意见 1：使用 ref.listen 而非状态 boolean 标志
      //   修正意见 2：每次 showSnackBar 前先 clearSnackBars()，防止排队堆积
      //   修正意见 3： SnackBar behavior=floating + margin 抬高，不遮挡聊天记录
      final prevScenario = previous?.currentScenarioName ?? '';
      final nextScenario = next.currentScenarioName;
      if (prevScenario.isNotEmpty && prevScenario != nextScenario) {
        // 场景名称发生变化（previous 非空说明不是初始化，而是真正的流转）
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!context.mounted) return;
          final messenger = ScaffoldMessenger.of(context);
          messenger.clearSnackBars();
          messenger.showSnackBar(
            SnackBar(
              content: Row(
                children: [
                  const Text('🎯 ', style: TextStyle(fontSize: 20)),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text(
                          '场景通关！',
                          style: TextStyle(
                            fontWeight: FontWeight.bold,
                            fontSize: 14,
                          ),
                        ),
                        Text(
                          '即将进入: $nextScenario',
                          style: const TextStyle(
                            fontSize: 12,
                            color: Colors.white70,
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
              behavior: SnackBarBehavior.floating,
              duration: const Duration(seconds: 3),
              margin: const EdgeInsets.only(
                left: 16,
                right: 16,
                bottom: 80,
              ),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(12),
              ),
              backgroundColor: const Color(0xFF1A1A2E),
            ),
          );
        });
      }
    });

    // ── 控制显示进行中状态的标识 ──
    final hasActiveTurn = isListening || chatState.isWaitingForTeachingData;
    final showEmptyState = chatState.chatHistory.isEmpty && !hasActiveTurn;

    return Scaffold(
      backgroundColor: const Color(0xFFF4F6F9),
      appBar: AppBar(
        title: Text(
          chatTopicDisplayTitle(
            ref,
            chatState.currentTopicTitle,
            titleZh: chatState.currentTopicTitleZh.isEmpty
                ? null
                : chatState.currentTopicTitleZh,
          ),
          style: const TextStyle(
            fontWeight: FontWeight.bold,
            color: Colors.black87,
          ),
          overflow: TextOverflow.ellipsis,
        ),
        backgroundColor: Colors.white,
        elevation: 1,
        centerTitle: true,
        actions: [
          IconButton(
            icon: const Icon(Icons.add_comment_outlined, color: Colors.black87),
            tooltip: tr(ref, 'Change practice topic', '切换练习话题'),
            onPressed: () => _showTopicRequestSheet(context, ref),
          ),
          PopupMenuButton<int>(
            icon: const Icon(Icons.psychology_alt, color: Colors.black87),
            tooltip: tr(ref, 'Coach politeness', '教练礼貌程度'),
            onSelected: (level) => notifier.updatePoliteness(level),
            itemBuilder: (context) => [
              PopupMenuItem(
                value: 0,
                child: Text(tr(ref, 'Difficult & rude', '刁钻暴躁')),
              ),
              PopupMenuItem(
                value: 1,
                child: Text(tr(ref, 'Normal & professional', '正常礼貌')),
              ),
              PopupMenuItem(
                value: 2,
                child: Text(tr(ref, 'Very warm & polite', '极度客气')),
              ),
            ],
          ),
          IconButton(
            icon: Icon(
              Icons.swap_horiz_rounded,
              color: chatState.isFlipped ? Colors.pinkAccent : Colors.black87,
            ),
            tooltip: tr(ref, 'Swap roles', '切换角色'),
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
      ),
      body: SafeArea(
        bottom: false,
        child: Stack(
          children: [
            if (settings.autoMode && isListening)
              Positioned.fill(
                child: IgnorePointer(
                  child: AnimatedBuilder(
                    animation: _glowAnimation,
                    builder: (context, child) {
                      return Stack(
                        children: [
                          Positioned(
                            left: 0,
                            top: 0,
                            bottom: 0,
                            width: 6,
                            child: _buildColorfulGlowingLine(),
                          ),
                          Positioned(
                            right: 0,
                            top: 0,
                            bottom: 0,
                            width: 6,
                            child: _buildColorfulGlowingLine(),
                          ),
                        ],
                      );
                    },
                  ),
                ),
              ),

            Column(
              children: [
                if (chatState.isGeneratingTopic) _TopicGeneratingBanner(),

                // ── 【阶段三新增】微场景目标卡片 ──────────────────────────────
                // 当 currentScenarioName 非空时显示，通过 AnimatedSwitcher 实现平滑过渡
                const ScenarioGoalCard(),

                if (settings.showProgressBar)
                  _buildMasteryTracker(ref, chatState.masteryProgress),

                Container(
                  margin: EdgeInsets.only(
                    top: _showHistory
                        ? 12.0
                        : MediaQuery.of(context).size.height * 0.15,
                    bottom: _showHistory ? 12.0 : 0.0,
                  ),
                  child: Center(
                    child: _buildAvatar(
                      ref,
                      isListening,
                      isSpeaking,
                      settings.fontSize,
                      chatState.isFlipped,
                    ),
                  ),
                ),

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
                                color: Colors.black.withOpacity(0.04),
                                blurRadius: 15,
                                offset: const Offset(0, -5),
                              ),
                            ],
                          ),
                          child: showEmptyState
                              ? _buildEmptyState(
                                  ref,
                                  isListening,
                                  settings.fontSize,
                                )
                              : ListView.builder(
                                  controller: _scrollController,
                                  reverse: true,
                                  padding: EdgeInsets.only(
                                    left: 16,
                                    right: 16,
                                    top: 20,
                                    bottom: shouldShowButton ? 120.0 : 20.0,
                                  ),
                                  physics: const BouncingScrollPhysics(),
                                  itemCount:
                                      chatState.chatHistory.length +
                                      (hasActiveTurn ? 1 : 0),
                                  itemBuilder: (context, index) {
                                    if (hasActiveTurn && index == 0) {
                                      // 👉 渲染最新的动态构建气泡 (打字机效果+骨架屏)
                                      return _buildActiveTurn(
                                        ref,
                                        chatState,
                                        settings,
                                        notifier,
                                      );
                                    }
                                    final historyIndex = hasActiveTurn
                                        ? index - 1
                                        : index;
                                    final reversedIndex =
                                        chatState.chatHistory.length -
                                        1 -
                                        historyIndex;
                                    return _buildChatTurn(
                                      ref,
                                      chatState.chatHistory[reversedIndex],
                                      settings,
                                      notifier,
                                    );
                                  },
                                ),
                        )
                      : const SizedBox.shrink(),
                ),
              ],
            ),

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
                                  .withOpacity(0.2),
                              blurRadius: 10,
                              offset: const Offset(0, 4),
                            ),
                          ],
                        ),
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          mainAxisAlignment: MainAxisAlignment.center,
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
                              isListening
                                  ? tr(ref, "I'm listening...", '正在聆听…')
                                  : (isSpeaking
                                        ? tr(ref, 'AI speaking...', 'AI 回复中…')
                                        : tr(ref, 'Tap to start', '点击开始')),
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

  // ── 动态构建的当前轮次 (局部刷新) ──────────────────────────────────
  Widget _buildActiveTurn(
    WidgetRef ref,
    ChatState state,
    SettingsState settings,
    ChatNotifier notifier,
  ) {
    final isListening = state.status == ChatStatus.listening;
    final isWaiting = state.isWaitingForTeachingData;

    return Padding(
      padding: const EdgeInsets.only(bottom: 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // User Bubble 局部监听
          ValueListenableBuilder<String>(
            valueListenable: notifier.activeUserTextNotifier,
            builder: (context, userText, child) {
              if (userText.isEmpty && !isListening)
                return const SizedBox.shrink();
              return Align(
                alignment: Alignment.centerRight,
                child: Container(
                  margin: const EdgeInsets.only(left: 40, bottom: 12),
                  padding: const EdgeInsets.all(16),
                  decoration: const BoxDecoration(
                    color: Color(0xFFF0F4F8),
                    borderRadius: BorderRadius.only(
                      topLeft: Radius.circular(20),
                      topRight: Radius.circular(20),
                      bottomLeft: Radius.circular(20),
                    ),
                  ),
                  child: Text(
                    userText.isEmpty ? "..." : userText,
                    style: TextStyle(
                      fontSize: settings.fontSize,
                      color: userText.isEmpty ? Colors.black38 : Colors.black87,
                    ),
                  ),
                ),
              );
            },
          ),

          // AI Bubble & 骨架屏 局部监听
          if (isWaiting)
            Align(
              alignment: Alignment.centerLeft,
              child: Container(
                margin: const EdgeInsets.only(right: 30),
                decoration: BoxDecoration(
                  color: Colors.white,
                  borderRadius: const BorderRadius.only(
                    topLeft: Radius.circular(20),
                    topRight: Radius.circular(20),
                    bottomRight: Radius.circular(20),
                  ),
                  boxShadow: [
                    BoxShadow(
                      color: Colors.black.withOpacity(0.04),
                      blurRadius: 10,
                      offset: const Offset(0, 4),
                    ),
                  ],
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Padding(
                      padding: const EdgeInsets.all(16),
                      child: ValueListenableBuilder<String>(
                        valueListenable: notifier.activeAiTextNotifier,
                        builder: (context, aiText, child) {
                          if (aiText.isNotEmpty) {
                            return _AIChatBubble(
                              text: aiText,
                              fontSize: settings.fontSize,
                              onSentenceTap: (_) {}, // 流式输出时禁用点击复读，防止打断
                            );
                          }
                          // AI 还没发声时的思考态
                          return AnimatedBuilder(
                            animation: _glowAnimation,
                            builder: (context, _) => Opacity(
                              opacity: 0.4 + (_glowAnimation.value * 0.6),
                              child: Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  const SizedBox(
                                    width: 16,
                                    height: 16,
                                    child: CircularProgressIndicator(
                                      strokeWidth: 2,
                                      color: Colors.blueAccent,
                                    ),
                                  ),
                                  const SizedBox(width: 8),
                                  Text(
                                    tr(ref, 'Thinking...', '思考中...'),
                                    style: TextStyle(
                                      color: Colors.blueAccent,
                                      fontSize: settings.fontSize,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          );
                        },
                      ),
                    ),
                    if (settings.showTranslation || settings.showHints)
                      _buildSkeletonTeachingData(ref, settings),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }

  // ── 副模型教辅数据生成期间的骨架屏掩护 (Skeleton Loader) ──
  Widget _buildSkeletonTeachingData(WidgetRef ref, SettingsState settings) {
    return Container(
      decoration: BoxDecoration(
        color: const Color(0xFFFAFAFA),
        borderRadius: const BorderRadius.only(bottomRight: Radius.circular(20)),
        border: Border(top: BorderSide(color: Colors.grey.shade100)),
      ),
      child: AnimatedBuilder(
        animation: _glowAnimation,
        builder: (context, _) {
          return Opacity(
            opacity: 0.4 + (_glowAnimation.value * 0.6),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (settings.showTranslation)
                  Container(
                    width: double.infinity,
                    padding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 12,
                    ),
                    decoration: const BoxDecoration(color: Color(0xFFF0F8FF)),
                    child: Container(
                      height: settings.fontSize,
                      width: 180,
                      decoration: BoxDecoration(
                        color: Colors.blue.withOpacity(0.15),
                        borderRadius: BorderRadius.circular(4),
                      ),
                    ),
                  ),
                if (settings.showHints)
                  Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          children: [
                            Icon(
                              Icons.lightbulb_rounded,
                              color: Colors.green.shade300,
                              size: 16,
                            ),
                            const SizedBox(width: 6),
                            Text(
                              tr(ref, 'Generating feedback...', '教练正在生成反馈...'),
                              style: const TextStyle(
                                fontSize: 12,
                                fontWeight: FontWeight.bold,
                                color: Colors.black45,
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 12),
                        Container(
                          height: settings.fontSize * 1.2,
                          width: double.infinity,
                          decoration: BoxDecoration(
                            color: Colors.green.withOpacity(0.06),
                            borderRadius: BorderRadius.circular(8),
                          ),
                        ),
                        const SizedBox(height: 8),
                        Container(
                          height: settings.fontSize * 1.2,
                          width: MediaQuery.of(context).size.width * 0.5,
                          decoration: BoxDecoration(
                            color: Colors.green.withOpacity(0.06),
                            borderRadius: BorderRadius.circular(8),
                          ),
                        ),
                      ],
                    ),
                  ),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _buildMasteryTracker(WidgetRef ref, double progress) {
    final p = progress.clamp(0.0, 100.0);
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
                tr(ref, 'Topic Mastery', '话题掌握度'),
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
            child: AnimatedContainer(
              duration: const Duration(milliseconds: 500),
              height: 8,
              width: double.infinity,
              alignment: Alignment.centerLeft,
              decoration: BoxDecoration(color: Colors.grey.shade200),
              child: AnimatedBuilder(
                animation: _glowAnimation,
                builder: (context, child) {
                  return FractionallySizedBox(
                    widthFactor: (p / 100.0).clamp(0.0, 1.0),
                    child: Container(
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(10),
                        gradient: LinearGradient(
                          colors: [
                            const Color(0xFF66BB6A),
                            Colors.greenAccent.withValues(
                              alpha: 0.8 + (_glowAnimation.value * 0.2),
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
          ),
        ],
      ),
    );
  }

  Widget _buildEmptyState(WidgetRef ref, bool isListening, double fontSize) {
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
                ? tr(
                    ref,
                    "I'm listening... Try saying 'Hello'!",
                    '正在聆听…试试说 Hello！',
                  )
                : tr(ref, 'Tap the pill below to start!', '点击下方按钮开始对话！'),
            textAlign: TextAlign.center,
            style: TextStyle(color: Colors.grey[600], fontSize: fontSize),
          ),
        ],
      ),
    );
  }

  Widget _buildLottieImage(bool isSpe) {
    return Lottie.asset(
      'assets/avatar.json',
      fit: BoxFit.contain,
      animate: isSpe,
      errorBuilder: (context, error, stackTrace) => const Center(
        child: Text(
          "Json Error",
          style: TextStyle(color: Colors.red, fontSize: 12),
        ),
      ),
    );
  }

  Widget _buildAvatar(
    WidgetRef ref,
    bool isLis,
    bool isSpe,
    double fs,
    bool isFlipped,
  ) {
    final double exactSize = _showHistory ? 90.0 : 160.0;
    // 动态角色名：来自 session_report 的 topic 信息；翻转时显示固定文字
    final chatState = ref.read(chatProvider);
    final String roleName = isFlipped
        ? tr(ref, "Customer (you're the coach)", '顾客（由你来当教练）')
        : chatState.currentRoleName;

    return AnimatedBuilder(
      animation: _glowAnimation,
      builder: (context, _) {
        final double visualScale =
            1.0 + (isLis || isSpe ? _glowAnimation.value * 0.05 : 0.0);

        return Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            SizedBox(
              width: exactSize,
              height: exactSize,
              child: Stack(
                alignment: Alignment.center,
                children: [
                  if (isLis || isSpe)
                    Transform.scale(
                      scale: visualScale * 1.15,
                      child: ImageFiltered(
                        imageFilter: ui.ImageFilter.blur(
                          sigmaX: 12.0,
                          sigmaY: 12.0,
                        ),
                        child: ColorFiltered(
                          colorFilter: ColorFilter.mode(
                            (isLis ? Colors.pinkAccent : Colors.cyanAccent)
                                .withOpacity(0.7),
                            BlendMode.srcATop,
                          ),
                          child: _buildLottieImage(isSpe),
                        ),
                      ),
                    ),
                  Transform.scale(
                    scale: visualScale,
                    child: _buildLottieImage(isSpe),
                  ),
                ],
              ),
            ),
            if (!_showHistory) ...[
              const SizedBox(height: 16),
              Text(
                isLis
                    ? tr(ref, 'Listening...', '聆听中…')
                    : (isSpe
                          ? tr(ref, 'Speaking...', '说话中…')
                          : tr(ref, 'Ready', '就绪')),
                style: TextStyle(
                  fontSize: fs + 2,
                  fontWeight: FontWeight.bold,
                  color: isLis
                      ? Colors.pinkAccent
                      : (isSpe ? Colors.blueAccent : Colors.grey),
                ),
              ),
              const SizedBox(height: 6),
              Text(
                roleName,
                style: TextStyle(
                  fontSize: fs - 1,
                  color: isFlipped ? Colors.pinkAccent : Colors.grey.shade500,
                  letterSpacing: 1.2,
                ),
              ),
            ],
          ],
        );
      },
    );
  }

  Widget _buildColorfulGlowingLine() {
    return Container(
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [
            Colors.transparent,
            Colors.pinkAccent.withOpacity(_glowAnimation.value),
            Colors.deepPurpleAccent.withOpacity(_glowAnimation.value),
            Colors.cyanAccent.withOpacity(_glowAnimation.value),
            Colors.transparent,
          ],
          stops: const [0.0, 0.2, 0.5, 0.8, 1.0],
        ),
        boxShadow: [
          BoxShadow(
            color: Colors.deepPurpleAccent.withOpacity(
              _glowAnimation.value * 0.6,
            ),
            blurRadius: 18,
            spreadRadius: 2,
          ),
        ],
      ),
    );
  }

  Widget _buildChatTurn(
    WidgetRef ref,
    ChatTurn turn,
    SettingsState settings,
    ChatNotifier notifier,
  ) {
    final d = turn.rawTeachingData;
    final hasCorrection =
        settings.showCorrection &&
        d['coach_correction_cn'] != null &&
        d['coach_correction_cn'].toString().isNotEmpty;
    final hasTranslation =
        settings.showTranslation &&
        d['ai_translation_cn'] != null &&
        d['ai_translation_cn'].toString().isNotEmpty;
    final hasHints =
        settings.showHints &&
        d['suggested_hints_en'] != null &&
        (d['suggested_hints_en'] as List).isNotEmpty;

    return Padding(
      padding: const EdgeInsets.only(bottom: 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Align(
            alignment: Alignment.centerRight,
            child: Container(
              margin: const EdgeInsets.only(left: 40, bottom: 12),
              padding: const EdgeInsets.all(16),
              decoration: const BoxDecoration(
                color: Color(0xFFF0F4F8),
                borderRadius: BorderRadius.only(
                  topLeft: Radius.circular(20),
                  topRight: Radius.circular(20),
                  bottomLeft: Radius.circular(20),
                ),
              ),
              child: Text(
                turn.userText,
                style: TextStyle(
                  fontSize: settings.fontSize,
                  color: Colors.black87,
                ),
              ),
            ),
          ),

          if (hasCorrection)
            Align(
              alignment: Alignment.centerRight,
              child: Container(
                margin: const EdgeInsets.only(bottom: 8, right: 40),
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 6,
                ),
                decoration: BoxDecoration(
                  color: Colors.orange.shade50,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: Colors.orange.shade200),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(
                      Icons.auto_fix_high,
                      size: 14,
                      color: Colors.orange.shade700,
                    ),
                    const SizedBox(width: 6),
                    Flexible(
                      child: Text(
                        d['coach_correction_cn'],
                        style: TextStyle(
                          fontSize: settings.fontSize * 0.85,
                          color: Colors.orange.shade900,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),

          Align(
            alignment: Alignment.centerLeft,
            child: Container(
              margin: const EdgeInsets.only(right: 30),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: const BorderRadius.only(
                  topLeft: Radius.circular(20),
                  topRight: Radius.circular(20),
                  bottomRight: Radius.circular(20),
                ),
                boxShadow: [
                  BoxShadow(
                    color: Colors.black.withOpacity(0.04),
                    blurRadius: 10,
                    offset: const Offset(0, 4),
                  ),
                ],
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Padding(
                    padding: const EdgeInsets.all(16),
                    child: _AIChatBubble(
                      text: turn.aiText,
                      fontSize: settings.fontSize,
                      onSentenceTap: (s) => notifier.speakText(s),
                    ),
                  ),

                  if (hasTranslation || hasHints)
                    Container(
                      decoration: BoxDecoration(
                        color: const Color(0xFFFAFAFA),
                        borderRadius: const BorderRadius.only(
                          bottomRight: Radius.circular(20),
                        ),
                        border: Border(
                          top: BorderSide(color: Colors.grey.shade100),
                        ),
                      ),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          if (hasTranslation)
                            Container(
                              width: double.infinity,
                              padding: const EdgeInsets.symmetric(
                                horizontal: 16,
                                vertical: 10,
                              ),
                              decoration: const BoxDecoration(
                                color: Color(0xFFF0F8FF),
                              ),
                              child: Text(
                                d['ai_translation_cn'],
                                style: TextStyle(
                                  fontSize: settings.fontSize * 0.9,
                                  color: Colors.blue.shade800,
                                ),
                              ),
                            ),

                          if (hasHints)
                            Container(
                              padding: const EdgeInsets.all(16),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Row(
                                    children: [
                                      Icon(
                                        Icons.lightbulb_rounded,
                                        color: Colors.green.shade400,
                                        size: 16,
                                      ),
                                      const SizedBox(width: 6),
                                      Text(
                                        tr(ref, 'Try to reply', '试着这样回'),
                                        style: const TextStyle(
                                          fontSize: 12,
                                          fontWeight: FontWeight.bold,
                                          color: Colors.black45,
                                        ),
                                      ),
                                    ],
                                  ),
                                  const SizedBox(height: 8),
                                  ...List<Widget>.from(
                                    (d['suggested_hints_en'] as List).map(
                                      (hint) => _buildHintTile(
                                        hint,
                                        notifier,
                                        settings.fontSize,
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
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildHintTile(String hint, ChatNotifier notifier, double fontSize) {
    return GestureDetector(
      onTap: () => notifier.speakText(hint),
      child: Container(
        margin: const EdgeInsets.only(bottom: 8),
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          color: const Color(0xFFF1F8E9),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: Colors.green.shade100),
        ),
        child: Text(
          hint,
          style: TextStyle(
            fontSize: fontSize * 0.95,
            color: Colors.green.shade800,
            fontWeight: FontWeight.w500,
          ),
        ),
      ),
    );
  }
}

class _TopicGeneratingBanner extends ConsumerWidget {
  const _TopicGeneratingBanner();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
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
            width: 16,
            height: 16,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              color: Colors.blueAccent,
            ),
          ),
          const SizedBox(width: 10),
          Text(
            tr(ref, 'Generating your practice topic...', '正在生成练习场景…'),
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

class _AIChatBubble extends StatefulWidget {
  final String text;
  final double fontSize;
  final Function(String) onSentenceTap;
  const _AIChatBubble({
    required this.text,
    required this.fontSize,
    required this.onSentenceTap,
  });
  @override
  __AIChatBubbleState createState() => __AIChatBubbleState();
}

class __AIChatBubbleState extends State<_AIChatBubble> {
  late List<String> _sentences;
  late List<TapGestureRecognizer> _recognizers;

  @override
  void initState() {
    super.initState();
    _parseSentences();
  }

  @override
  void didUpdateWidget(_AIChatBubble oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.text != widget.text) {
      _disposeRecognizers();
      _parseSentences();
    }
  }

  void _parseSentences() {
    final RegExp regExp = RegExp(r'[^.!?\n]+[.!?\n]*\s*');
    _sentences = regExp
        .allMatches(widget.text)
        .map((m) => m.group(0)!)
        .toList();
    if (_sentences.isEmpty) _sentences = [widget.text];
    _recognizers = _sentences
        .map(
          (s) => TapGestureRecognizer()
            ..onTap = () {
              if (s.trim().isNotEmpty) widget.onSentenceTap(s.trim());
            },
        )
        .toList();
  }

  void _disposeRecognizers() {
    for (var r in _recognizers) {
      r.dispose();
    }
  }

  @override
  void dispose() {
    _disposeRecognizers();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Text.rich(
      TextSpan(
        children: List.generate(
          _sentences.length,
          (i) => TextSpan(
            text: _sentences[i],
            recognizer: _recognizers[i],
            style: TextStyle(
              fontSize: widget.fontSize,
              color: Colors.black87,
              fontWeight: FontWeight.w500,
              height: 1.4,
            ),
          ),
        ),
      ),
    );
  }
}
