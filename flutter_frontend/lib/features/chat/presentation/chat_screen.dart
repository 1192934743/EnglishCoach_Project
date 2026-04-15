import 'package:flutter/material.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lottie/lottie.dart';
import 'dart:ui' as ui;
import '../providers/chat_provider.dart';
import '../../../core/providers/settings_provider.dart';

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

    ref.listen<SettingsState>(settingsProvider, (prev, next) {
      if (next.autoMode && (prev?.autoMode != true)) {
        if (ref.read(chatProvider).status == ChatStatus.idle)
          notifier.startListening();
      }
    });

    ref.listen<ChatState>(chatProvider, (previous, next) {
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
    });

    return Scaffold(
      backgroundColor: const Color(0xFFF4F6F9),
      appBar: AppBar(
        title: Text(
          'Simulation Practice',
          style: const TextStyle(
            fontWeight: FontWeight.bold,
            color: Colors.black87,
          ),
        ),
        backgroundColor: Colors.white,
        elevation: 1,
        centerTitle: true,
        actions: [
          // 🌟 UI 按钮 1：性格下拉菜单
          PopupMenuButton<int>(
            icon: const Icon(Icons.psychology_alt, color: Colors.black87),
            tooltip: "AI性格礼貌度",
            onSelected: (level) => notifier.updatePoliteness(level),
            itemBuilder: (context) => [
              const PopupMenuItem(value: 0, child: Text("刁钻暴躁 (Rude)")),
              const PopupMenuItem(value: 1, child: Text("正常服务 (Normal)")),
              const PopupMenuItem(value: 2, child: Text("极度客气 (Polite)")),
            ],
          ),
          // 🌟 UI 按钮 2：角色互换按钮（互换后变红）
          IconButton(
            icon: Icon(
              Icons.swap_horiz_rounded,
              color: chatState.isFlipped ? Colors.pinkAccent : Colors.black87,
            ),
            tooltip: "切换角色",
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
                Container(
                  margin: EdgeInsets.only(
                    top: _showHistory
                        ? 12.0
                        : MediaQuery.of(context).size.height * 0.18,
                    bottom: _showHistory ? 12.0 : 0.0,
                  ),
                  child: Center(
                    // 传入 isFlipped 状态给数字人，用于显示当前的身份名称
                    child: _buildAvatar(
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
                          child: chatState.chatHistory.isEmpty
                              ? _buildEmptyState(isListening, settings.fontSize)
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
                                  itemCount: chatState.chatHistory.length,
                                  itemBuilder: (context, index) {
                                    final reversedIndex =
                                        chatState.chatHistory.length -
                                        1 -
                                        index;
                                    return _buildChatTurn(
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
                                  ? "I'm listening..."
                                  : (isSpeaking
                                        ? "AI Speaking..."
                                        : "Tap to Start"),
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

  Widget _buildEmptyState(bool isListening, double fontSize) {
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
                ? "I'm listening... Try saying 'Hello'!"
                : "Tap the pill below to start!",
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

  // 🌟 修改数字人下方显示的身份标签
  Widget _buildAvatar(bool isLis, bool isSpe, double fs, bool isFlipped) {
    final double exactSize = _showHistory ? 90.0 : 160.0;
    // 翻转后，AI 就是顾客
    final String roleName = isFlipped ? "Customer (AI)" : "McDonald's Cashier";

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
                isLis ? "Listening..." : (isSpe ? "Speaking..." : "Ready"),
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
      padding: const EdgeInsets.only(bottom: 30),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Align(
            alignment: Alignment.centerRight,
            child: Container(
              margin: const EdgeInsets.only(left: 40, bottom: 12),
              padding: const EdgeInsets.all(16),
              decoration: const BoxDecoration(
                color: Color(0xFFE8F5E9),
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
          Align(
            alignment: Alignment.centerLeft,
            child: Container(
              margin: const EdgeInsets.only(right: 40),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: const BorderRadius.only(
                  topLeft: Radius.circular(20),
                  topRight: Radius.circular(20),
                  bottomRight: Radius.circular(20),
                ),
                boxShadow: [
                  BoxShadow(
                    color: Colors.black.withOpacity(0.03),
                    blurRadius: 8,
                    offset: const Offset(0, 2),
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
                  if (hasTranslation)
                    _buildAttachmentBox(
                      Icons.translate,
                      Colors.blue,
                      "Translation",
                      d['ai_translation_cn'],
                      settings.fontSize,
                    ),
                  if (hasCorrection)
                    _buildAttachmentBox(
                      Icons.lightbulb_outline,
                      Colors.orange,
                      "Correction",
                      d['coach_correction_cn'],
                      settings.fontSize,
                    ),
                  if (hasHints)
                    Padding(
                      padding: const EdgeInsets.all(16),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(
                            children: const [
                              Icon(
                                Icons.forum_outlined,
                                color: Colors.green,
                                size: 16,
                              ),
                              SizedBox(width: 6),
                              Text(
                                "You can reply:",
                                style: TextStyle(
                                  fontWeight: FontWeight.bold,
                                  color: Colors.grey,
                                  fontSize: 13,
                                ),
                              ),
                            ],
                          ),
                          const SizedBox(height: 10),
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
          ),
        ],
      ),
    );
  }

  Widget _buildAttachmentBox(
    IconData icon,
    Color color,
    String title,
    String content,
    double fontSize,
  ) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: Colors.grey.withOpacity(0.1))),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(icon, color: color, size: 14),
              const SizedBox(width: 6),
              Text(
                title,
                style: TextStyle(
                  fontWeight: FontWeight.bold,
                  color: color,
                  fontSize: 12,
                ),
              ),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            content,
            style: TextStyle(fontSize: fontSize * 0.9, color: Colors.black54),
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
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: Colors.green.withOpacity(0.08),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: Colors.green.withOpacity(0.2)),
        ),
        child: Text(
          hint,
          style: TextStyle(fontSize: fontSize * 0.95, color: Colors.black87),
        ),
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
            ),
          ),
        ),
      ),
    );
  }
}
