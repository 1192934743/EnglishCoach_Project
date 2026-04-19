// 头像动画组件。
//
// 包含：头像 Lottie 动画、呼吸光效动画、角色名标签。

import 'package:flutter/material.dart';
import 'package:lottie/lottie.dart';
import 'dart:ui' as ui;

// ── 发光边条（AutoMode 激活时显示）──────────────────────────────────────────

class GlowingLine extends StatelessWidget {
  final Animation<double> glowAnimation;

  const GlowingLine({super.key, required this.glowAnimation});

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [
            Colors.transparent,
            Colors.pinkAccent.withOpacity(glowAnimation.value),
            Colors.deepPurpleAccent.withOpacity(glowAnimation.value),
            Colors.cyanAccent.withOpacity(glowAnimation.value),
            Colors.transparent,
          ],
          stops: const [0.0, 0.2, 0.5, 0.8, 1.0],
        ),
        boxShadow: [
          BoxShadow(
            color: Colors.deepPurpleAccent.withOpacity(glowAnimation.value * 0.6),
            blurRadius: 18,
            spreadRadius: 2,
          ),
        ],
      ),
    );
  }
}

// ── Lottie 头像 ─────────────────────────────────────────────────────────────

class AvatarLottie extends StatelessWidget {
  final bool animate;

  const AvatarLottie({super.key, required this.animate});

  @override
  Widget build(BuildContext context) {
    return Lottie.asset(
      'assets/avatar.json',
      fit: BoxFit.contain,
      animate: animate,
      errorBuilder: (context, error, stackTrace) => const Center(
        child: Text(
          "Json Error",
          style: TextStyle(color: Colors.red, fontSize: 12),
        ),
      ),
    );
  }
}

// ── 头像完整组件 ───────────────────────────────────────────────────────────

class AvatarAnimated extends StatelessWidget {
  final bool isListening;
  final bool isSpeaking;
  final bool isFlipped;
  final bool showHistory;
  final Animation<double> glowAnimation;
  final String roleName;
  final String statusText;

  const AvatarAnimated({
    super.key,
    required this.isListening,
    required this.isSpeaking,
    required this.isFlipped,
    required this.showHistory,
    required this.glowAnimation,
    required this.roleName,
    required this.statusText,
  });

  @override
  Widget build(BuildContext context) {
    final double exactSize = showHistory ? 90.0 : 160.0;
    final bool isActive = isListening || isSpeaking;

    return AnimatedBuilder(
      animation: glowAnimation,
      builder: (context, _) {
        final double visualScale =
            1.0 + (isActive ? glowAnimation.value * 0.05 : 0.0);

        return Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            SizedBox(
              width: exactSize,
              height: exactSize,
              child: Stack(
                alignment: Alignment.center,
                children: [
                  // 光晕层
                  if (isActive)
                    Transform.scale(
                      scale: visualScale * 1.15,
                      child: ImageFiltered(
                        imageFilter: ui.ImageFilter.blur(sigmaX: 12.0, sigmaY: 12.0),
                        child: ColorFiltered(
                          colorFilter: ColorFilter.mode(
                            (isListening ? Colors.pinkAccent : Colors.cyanAccent)
                                .withOpacity(0.7),
                            BlendMode.srcATop,
                          ),
                          child: AvatarLottie(animate: isSpeaking),
                        ),
                      ),
                    ),
                  // 正常层
                  Transform.scale(
                    scale: visualScale,
                    child: AvatarLottie(animate: isSpeaking),
                  ),
                ],
              ),
            ),
            // 标签（历史模式隐藏）
            if (!showHistory) ...[
              const SizedBox(height: 16),
              Text(
                statusText,
                style: TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.bold,
                  color: isListening
                      ? Colors.pinkAccent
                      : (isSpeaking ? Colors.blueAccent : Colors.grey),
                ),
              ),
              const SizedBox(height: 6),
              Text(
                roleName,
                style: TextStyle(
                  fontSize: 14,
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
}
