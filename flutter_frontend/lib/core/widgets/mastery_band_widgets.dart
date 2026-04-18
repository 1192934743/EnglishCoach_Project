// Mastery UI helpers: stars + tier pill (used on topic cards). Progress bars use LinearProgressIndicator elsewhere.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/settings_provider.dart';

class MasteryStarsRow extends StatelessWidget {
  final MasteryUiBand band;
  final double iconSize;

  const MasteryStarsRow({
    super.key,
    required this.band,
    this.iconSize = 15,
  });

  @override
  Widget build(BuildContext context) {
    final n = masteryStarCount(band);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: List.generate(3, (i) {
        return Icon(
          i < n ? Icons.star_rounded : Icons.star_outline_rounded,
          size: iconSize,
          color: i < n ? const Color(0xFFFFCA28) : Colors.grey.shade300,
        );
      }),
    );
  }
}

/// 圆角胶囊：星 + 档位文案（用于话题卡片等）。
class MasteryBandPill extends ConsumerWidget {
  final MasteryUiBand band;
  final bool compact;

  const MasteryBandPill({
    super.key,
    required this.band,
    this.compact = false,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final accent = masteryUiBandColor(band);
    return Container(
      padding: EdgeInsets.symmetric(
        horizontal: compact ? 8 : 10,
        vertical: compact ? 3 : 5,
      ),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          colors: [
            accent.withValues(alpha: 0.14),
            accent.withValues(alpha: 0.06),
          ],
        ),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: accent.withValues(alpha: 0.35)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          MasteryStarsRow(band: band, iconSize: compact ? 13 : 15),
          SizedBox(width: compact ? 4 : 6),
          Text(
            masteryUiBandLabel(ref, band),
            style: TextStyle(
              fontSize: compact ? 11 : 12,
              fontWeight: FontWeight.w600,
              color: accent.withValues(alpha: 0.95),
            ),
          ),
        ],
      ),
    );
  }
}
