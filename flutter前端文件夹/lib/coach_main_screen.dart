import 'package:flutter/material.dart';
import 'coach_view_model.dart';
import 'teaching_panel.dart';

class CoachMainScreen extends StatefulWidget {
  const CoachMainScreen({super.key});

  @override
  State<CoachMainScreen> createState() => _CoachMainScreenState();
}

class _CoachMainScreenState extends State<CoachMainScreen> {
  final CoachViewModel _viewModel = CoachViewModel();

  @override
  void initState() {
    super.initState();
    // 这里先填本机的后端地址，等会儿跑起来再调
    _viewModel.connect('ws://127.0.0.1:8000/ws/coach');
  }

  @override
  void dispose() {
    _viewModel.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.grey.shade100,
      appBar: AppBar(
        title: const Text('AI 私教'),
        elevation: 0,
        backgroundColor: Colors.white,
        foregroundColor: Colors.black,
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(4.0),
          child: AnimatedBuilder(
            animation: _viewModel,
            builder: (context, child) {
              return TweenAnimationBuilder<double>(
                tween: Tween<double>(begin: 0.0, end: _viewModel.topicProgress),
                duration: const Duration(milliseconds: 800),
                curve: Curves.easeOutCubic,
                builder: (context, value, _) {
                  return LinearProgressIndicator(
                    value: value,
                    backgroundColor: Colors.grey.shade200,
                    valueColor: const AlwaysStoppedAnimation<Color>(
                      Colors.amber,
                    ),
                    minHeight: 4,
                  );
                },
              );
            },
          ),
        ),
      ),
      body: AnimatedBuilder(
        animation: _viewModel,
        builder: (context, child) {
          return Column(
            children: [
              Expanded(
                child: Center(
                  child: Text(
                    _viewModel.isConnected ? '正在监听对话...' : '连接中...',
                    style: TextStyle(color: Colors.grey.shade500),
                  ),
                ),
              ),
              TeachingPanel(
                teachingData: _viewModel.currentTeachingData,
                onHintSelected: (selectedHint) {
                  debugPrint("用户点击了提示词: $selectedHint");
                },
              ),
              Container(
                padding: const EdgeInsets.all(24),
                child: FloatingActionButton(
                  onPressed: () {
                    _viewModel.clearTeachingData();
                  },
                  backgroundColor: Colors.blueAccent,
                  child: const Icon(Icons.mic, size: 32),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}
