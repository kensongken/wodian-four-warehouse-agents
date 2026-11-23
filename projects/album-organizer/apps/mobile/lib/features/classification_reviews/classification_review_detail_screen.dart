import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_error.dart';
import 'classification_review_detail_controller.dart';
import 'classification_review_error.dart';
import 'classification_review_models.dart';

final class ClassificationReviewDetailScreen extends ConsumerWidget {
  const ClassificationReviewDetailScreen({
    required this.opcId,
    required this.reviewId,
    super.key,
  });

  final String opcId;
  final String reviewId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final identity = (opcId: opcId, reviewId: reviewId);
    final state = ref.watch(
      classificationReviewDetailControllerProvider(identity),
    );
    final controller = ref.read(
      classificationReviewDetailControllerProvider(identity).notifier,
    );
    return Scaffold(
      appBar: AppBar(title: const Text('审核详情')),
      body: state.loading
          ? const Center(child: CircularProgressIndicator())
          : state.error != null
          ? _DetailError(error: state.error!, retry: controller.load)
          : _DetailBody(
              state: state,
              controller: controller,
              confirm: () => _confirm(context, controller),
            ),
    );
  }

  Future<void> _confirm(
    BuildContext context,
    ClassificationReviewDetailController controller,
  ) async {
    final approved = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('确认分类'),
        content: const Text('确认后当前流程不可再次修改。'),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('确认'),
          ),
        ],
      ),
    );
    if (approved != true || !context.mounted) return;
    try {
      final outcome = await controller.confirm();
      if (!context.mounted) return;
      final message = switch (outcome) {
        ClassificationConfirmOutcome.confirmed => '分类已确认',
        ClassificationConfirmOutcome.alreadyResolved => '该记录已处理，已刷新',
        ClassificationConfirmOutcome.notReviewable => '该分类当前不可确认，已刷新',
      };
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(message)));
    } on ApiException catch (error) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              classificationReviewSafeMessage(
                error,
                fallback: error.userMessage,
              ),
            ),
          ),
        );
      }
    }
  }
}

final class _DetailError extends StatelessWidget {
  const _DetailError({required this.error, required this.retry});

  final Object error;
  final Future<void> Function() retry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Text(classificationReviewSafeMessage(error, fallback: '加载失败，请稍后重试')),
          const SizedBox(height: 8),
          TextButton(onPressed: retry, child: const Text('重试')),
        ],
      ),
    );
  }
}

final class _DetailBody extends StatelessWidget {
  const _DetailBody({
    required this.state,
    required this.controller,
    required this.confirm,
  });

  final ClassificationReviewDetailState state;
  final ClassificationReviewDetailController controller;
  final VoidCallback confirm;

  @override
  Widget build(BuildContext context) {
    final review = state.review!;
    return ListView(
      padding: const EdgeInsets.all(16),
      children: <Widget>[
        const Text('仅确认图片分类，不判断真伪、产地、品质或仓验。'),
        const SizedBox(height: 16),
        Text(
          review.sourceType.label,
          style: Theme.of(context).textTheme.titleLarge,
        ),
        Text('Album Product：${review.albumProductId}'),
        Text(
          '快照 v${review.inputSnapshotVersion} · '
          '分类 v${review.classificationVersion}',
        ),
        const SizedBox(height: 16),
        Text('AI 候选', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        for (var index = 0; index < review.candidates.length; index += 1)
          _CandidateCard(
            index: index,
            candidate: review.candidates[index],
            enabled: review.status == ReviewStatus.open && !state.submitting,
            selected:
                state.selectedLevel1 == review.candidates[index].level1 &&
                state.selectedLevel2 == review.candidates[index].level2,
            onTap: () => controller.chooseCandidate(index),
          ),
        const SizedBox(height: 16),
        if (review.status == ReviewStatus.resolved)
          Text(
            '已确认：${review.confirmedLevel1} · ${review.confirmedLevel2}',
            style: Theme.of(context).textTheme.titleMedium,
          )
        else ...<Widget>[
          DropdownButtonFormField<String>(
            key: const Key('level-1-selector'),
            initialValue: state.selectedLevel1,
            decoration: const InputDecoration(labelText: '一级分类'),
            items: level1Categories
                .map(
                  (value) => DropdownMenuItem(value: value, child: Text(value)),
                )
                .toList(growable: false),
            onChanged: state.submitting ? null : controller.selectLevel1,
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            key: const Key('level-2-selector'),
            initialValue: state.selectedLevel2,
            decoration: const InputDecoration(labelText: '二级分类'),
            items: level2Categories
                .map(
                  (value) => DropdownMenuItem(value: value, child: Text(value)),
                )
                .toList(growable: false),
            onChanged: state.submitting ? null : controller.selectLevel2,
          ),
          const SizedBox(height: 20),
          FilledButton(
            onPressed: state.canConfirm ? confirm : null,
            child: Text(state.submitting ? '确认中…' : '确认分类'),
          ),
        ],
      ],
    );
  }
}

final class _CandidateCard extends StatelessWidget {
  const _CandidateCard({
    required this.index,
    required this.candidate,
    required this.enabled,
    required this.selected,
    required this.onTap,
  });

  final int index;
  final ClassificationCandidate candidate;
  final bool enabled;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final confidence = (candidate.confidence * 100).round();
    return Card(
      color: selected ? Theme.of(context).colorScheme.secondaryContainer : null,
      child: InkWell(
        key: Key('candidate-$index'),
        onTap: enabled ? onTap : null,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Text(
            '${candidate.level1} · ${candidate.level2} · $confidence%',
          ),
        ),
      ),
    );
  }
}
