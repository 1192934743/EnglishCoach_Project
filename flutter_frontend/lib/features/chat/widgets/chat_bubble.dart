// 聊天气泡组件。
//
// 包含：用户消息气泡、AI 消息气泡（带可点击句子分割）、
// 翻译、教学提示、纠错等子组件。

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import '../providers/chat_provider.dart';

// ── 提示条目标签 ───────────────────────────────────────────────────────────────

class HintTile extends StatelessWidget {
  final String hint;
  final void Function(String) onTap;
  final double fontSize;

  const HintTile({
    super.key,
    required this.hint,
    required this.onTap,
    required this.fontSize,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: () => onTap(hint),
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

// ── 教学面板 ─────────────────────────────────────────────────────────────────

class TeachingPanel extends StatelessWidget {
  final Map<String, dynamic> teachingData;
  final double fontSize;
  final bool showTranslation;
  final bool showHints;
  final bool showCorrection;
  final void Function(String) onHintTap;

  const TeachingPanel({
    super.key,
    required this.teachingData,
    required this.fontSize,
    required this.showTranslation,
    required this.showHints,
    required this.showCorrection,
    required this.onHintTap,
  });

  @override
  Widget build(BuildContext context) {
    final hasCorrection = showCorrection &&
        teachingData['coach_correction_cn'] != null &&
        teachingData['coach_correction_cn'].toString().isNotEmpty;
    final hasTranslation = showTranslation &&
        teachingData['ai_translation_cn'] != null &&
        teachingData['ai_translation_cn'].toString().isNotEmpty;
    final hasHints = showHints &&
        teachingData['suggested_hints_en'] != null &&
        (teachingData['suggested_hints_en'] as List).isNotEmpty;

    if (!hasCorrection && !hasTranslation && !hasHints) {
      return const SizedBox.shrink();
    }

    return Container(
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
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
              decoration: const BoxDecoration(
                color: Color(0xFFF0F8FF),
              ),
              child: Text(
                teachingData['ai_translation_cn'].toString(),
                style: TextStyle(
                  fontSize: fontSize * 0.9,
                  color: Colors.blue.shade800,
                ),
              ),
            ),
          if (hasHints)
            Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Icon(Icons.lightbulb_rounded, color: Colors.green.shade400, size: 16),
                      const SizedBox(width: 6),
                      const Text(
                        'Try to reply',
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
                    (teachingData['suggested_hints_en'] as List).map(
                      (hint) => HintTile(
                        hint: hint.toString(),
                        onTap: onHintTap,
                        fontSize: fontSize,
                      ),
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

// ── AI 消息气泡（带句子点击分割）────────────────────────────────────────────

class AIChatBubble extends StatefulWidget {
  final String text;
  final double fontSize;
  final void Function(String) onSentenceTap;

  const AIChatBubble({
    super.key,
    required this.text,
    required this.fontSize,
    required this.onSentenceTap,
  });

  @override
  State<AIChatBubble> createState() => _AIChatBubbleState();
}

class _AIChatBubbleState extends State<AIChatBubble> {
  late List<String> _sentences;
  late List<TapGestureRecognizer> _recognizers;

  @override
  void initState() {
    super.initState();
    _parseSentences();
  }

  @override
  void didUpdateWidget(AIChatBubble oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.text != widget.text) {
      _disposeRecognizers();
      _parseSentences();
    }
  }

  void _parseSentences() {
    final regExp = RegExp(r'[^.!?\n]+[.!?\n]*\s*');
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

// ── 完整聊天回合 ─────────────────────────────────────────────────────────────

class ChatBubble extends StatelessWidget {
  final ChatTurn turn;
  final double fontSize;
  final bool showTranslation;
  final bool showHints;
  final bool showCorrection;
  final void Function(String) onHintTap;

  const ChatBubble({
    super.key,
    required this.turn,
    required this.fontSize,
    required this.showTranslation,
    required this.showHints,
    required this.showCorrection,
    required this.onHintTap,
  });

  @override
  Widget build(BuildContext context) {
    final d = turn.rawTeachingData;
    final hasCorrection = showCorrection &&
        d['coach_correction_cn'] != null &&
        d['coach_correction_cn'].toString().isNotEmpty;
    final hasTranslation = showTranslation &&
        d['ai_translation_cn'] != null &&
        d['ai_translation_cn'].toString().isNotEmpty;
    final hasHints = showHints &&
        d['suggested_hints_en'] != null &&
        (d['suggested_hints_en'] as List).isNotEmpty;

    return Padding(
      padding: const EdgeInsets.only(bottom: 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // 用户消息
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
                style: TextStyle(fontSize: fontSize, color: Colors.black87),
              ),
            ),
          ),

          // 纠错（用户消息下方）
          if (hasCorrection)
            Align(
              alignment: Alignment.centerRight,
              child: Container(
                margin: const EdgeInsets.only(bottom: 8, right: 40),
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                decoration: BoxDecoration(
                  color: Colors.orange.shade50,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: Colors.orange.shade200),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.auto_fix_high, size: 14, color: Colors.orange.shade700),
                    const SizedBox(width: 6),
                    Flexible(
                      child: Text(
                        d['coach_correction_cn'].toString(),
                        style: TextStyle(
                          fontSize: fontSize * 0.85,
                          color: Colors.orange.shade900,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),

          // AI 消息
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
                    child: AIChatBubble(
                      text: turn.aiText,
                      fontSize: fontSize,
                      onSentenceTap: onHintTap,
                    ),
                  ),
                  if (hasTranslation || hasHints)
                    TeachingPanel(
                      teachingData: d,
                      fontSize: fontSize,
                      showTranslation: showTranslation,
                      showHints: showHints,
                      showCorrection: showCorrection,
                      onHintTap: onHintTap,
                    ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
