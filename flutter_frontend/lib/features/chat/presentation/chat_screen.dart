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
        if (ref.read(chatProvider).status == ChatStatus.idle) {
          notifier.startListening();
        }
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
        title: const Text(
          'Simulation Practice',
          style: TextStyle(fontWeight: FontWeight.bold, color: Colors.black87),
        ),
        backgroundColor: Colors.white,
        elevation: 1,
        centerTitle: true,
        actions: [
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
                // 🌟 这里包裹了一层：根据设置判断是否渲染进度条
                if (settings.showProgressBar)
                  _buildMasteryTracker(chatState.masteryProgress),

                Container(
                  margin: EdgeInsets.only(
                    top: _showHistory
                        ? 12.0
                        : MediaQuery.of(context).size.height * 0.15,
                    bottom: _showHistory ? 12.0 : 0.0,
                  ),
                  child: Center(
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

  Widget _buildMasteryTracker(double progress) {
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
              const Text(
                "Topic Mastery",
                style: TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                  color: Colors.black54,
                ),
              ),
              Text(
                "${progress.toInt()}%",
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
                    widthFactor: progress / 100.0,
                    child: Container(
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(10),
                        gradient: LinearGradient(
                          colors: [
                            const Color(0xFF66BB6A),
                            Colors.greenAccent.withOpacity(
                              0.8 + (_glowAnimation.value * 0.2),
                            ),
                            const Color(0xFF4CAF50),
                          ],
                          stops: const [0.0, 0.5, 1.0],
                        ),
                        boxShadow: [
                          BoxShadow(
                            color: Colors.greenAccent.withOpacity(0.4),
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

  Widget _buildAvatar(bool isLis, bool isSpe, double fs, bool isFlipped) {
    final double exactSize = _showHistory ? 90.0 : 160.0;
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
                                      const Text(
                                        "Try to reply",
                                        style: TextStyle(
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
