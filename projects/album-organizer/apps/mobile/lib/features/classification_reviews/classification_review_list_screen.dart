import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import 'classification_review_error.dart';
import 'classification_review_list_controller.dart';
import 'classification_review_models.dart';

final class ClassificationReviewListScreen extends ConsumerWidget {
  const ClassificationReviewListScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(classificationReviewListControllerProvider);
    final controller = ref.read(
      classificationReviewListControllerProvider.notifier,
    );
    return Scaffold(
      appBar: AppBar(title: const Text('分类审核')),
      body: Column(
        children: <Widget>[
          const Padding(
            padding: EdgeInsets.fromLTRB(16, 8, 16, 8),
            child: Text('仅确认图片分类，不判断真伪、产地、品质或仓验。'),
          ),
          if (state.memberships.isNotEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: DropdownButton<String>(
                key: const Key('opc-selector'),
                value: state.selectedOpcId,
                isExpanded: true,
                items: state.memberships
                    .map(
                      (membership) => DropdownMenuItem<String>(
                        value: membership.id,
                        child: Text(membership.displayName),
                      ),
                    )
                    .toList(growable: false),
                onChanged: (value) {
                  if (value != null) unawaited(controller.selectOpc(value));
                },
              ),
            ),
          if (state.memberships.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
              child: SegmentedButton<ReviewStatus>(
                segments: const <ButtonSegment<ReviewStatus>>[
                  ButtonSegment<ReviewStatus>(
                    value: ReviewStatus.open,
                    label: Text('待审核'),
                  ),
                  ButtonSegment<ReviewStatus>(
                    value: ReviewStatus.resolved,
                    label: Text('已完成'),
                  ),
                ],
                selected: <ReviewStatus>{state.status},
                onSelectionChanged: (selection) =>
                    unawaited(controller.selectStatus(selection.single)),
              ),
            ),
          Expanded(
            child: _ReviewListBody(state: state, controller: controller),
          ),
        ],
      ),
    );
  }
}

final class _ReviewListBody extends StatelessWidget {
  const _ReviewListBody({required this.state, required this.controller});

  final ClassificationReviewListState state;
  final ClassificationReviewListController controller;

  @override
  Widget build(BuildContext context) {
    if (state.loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (state.error != null && state.items.isEmpty) {
      return _MessageList(
        message: classificationReviewSafeMessage(
          state.error!,
          fallback: '加载失败，请稍后重试',
        ),
        action: TextButton(
          onPressed: controller.refresh,
          child: const Text('重试'),
        ),
      );
    }
    if (state.memberships.isEmpty) {
      return const _MessageList(message: '尚未配置 OPC，请联系管理员');
    }
    if (state.items.isEmpty) {
      return _MessageList(
        message: state.status == ReviewStatus.open ? '暂无待审核记录' : '暂无已完成记录',
      );
    }
    final hasMore = state.nextBefore != null;
    return RefreshIndicator(
      onRefresh: controller.refresh,
      child: ListView.separated(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.all(16),
        itemCount: state.items.length + (hasMore ? 1 : 0),
        separatorBuilder: (_, _) => const SizedBox(height: 12),
        itemBuilder: (context, index) {
          if (index == state.items.length) {
            return Center(
              child: state.loadingMore
                  ? const CircularProgressIndicator()
                  : TextButton(
                      onPressed: controller.loadMore,
                      child: const Text('加载更多'),
                    ),
            );
          }
          return _ReviewCard(
            review: state.items[index],
            onTap: () async {
              final review = state.items[index];
              await context.push<void>('/reviews/${review.opcId}/${review.id}');
              if (context.mounted) await controller.refresh();
            },
          );
        },
      ),
    );
  }
}

final class _MessageList extends StatelessWidget {
  const _MessageList({required this.message, this.action});

  final String message;
  final Widget? action;

  @override
  Widget build(BuildContext context) => ListView(
    physics: const AlwaysScrollableScrollPhysics(),
    children: <Widget>[
      const SizedBox(height: 180),
      Center(child: Text(message)),
      if (action != null) Center(child: action),
    ],
  );
}

final class _ReviewCard extends StatelessWidget {
  const _ReviewCard({required this.review, required this.onTap});

  final ClassificationReview review;
  final Future<void> Function() onTap;

  @override
  Widget build(BuildContext context) {
    final first = review.candidates.first;
    final confidence = (first.confidence * 100).round();
    return Card(
      child: InkWell(
        onTap: () => unawaited(onTap()),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Row(
                children: <Widget>[
                  Expanded(
                    child: Text(
                      review.sourceType.label,
                      style: Theme.of(context).textTheme.titleMedium,
                    ),
                  ),
                  Text(review.status == ReviewStatus.open ? '待审核' : '已完成'),
                ],
              ),
              const SizedBox(height: 8),
              Text('Album Product：${review.albumProductId}'),
              Text(
                '快照 v${review.inputSnapshotVersion} · '
                '分类 v${review.classificationVersion}',
              ),
              const SizedBox(height: 8),
              Text('${first.level1} · ${first.level2} · $confidence%'),
              Text('候选 ${review.candidates.length} 个'),
            ],
          ),
        ),
      ),
    );
  }
}
