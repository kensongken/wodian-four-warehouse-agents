import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'classification_review_error.dart';
import 'classification_review_models.dart';
import 'classification_review_repository.dart';

final classificationReviewListControllerProvider =
    StateNotifierProvider.autoDispose<
      ClassificationReviewListController,
      ClassificationReviewListState
    >(
      (ref) => ClassificationReviewListController(
        ref.watch(classificationReviewRepositoryProvider),
      ),
    );

final class ClassificationReviewListState {
  const ClassificationReviewListState({
    this.memberships = const <OpcMembershipSummary>[],
    this.selectedOpcId,
    this.status = ReviewStatus.open,
    this.items = const <ClassificationReview>[],
    this.nextBefore,
    this.loading = true,
    this.loadingMore = false,
    this.error,
  });

  final List<OpcMembershipSummary> memberships;
  final String? selectedOpcId;
  final ReviewStatus status;
  final List<ClassificationReview> items;
  final String? nextBefore;
  final bool loading;
  final bool loadingMore;
  final Object? error;
}

final class ClassificationReviewListController
    extends StateNotifier<ClassificationReviewListState> {
  ClassificationReviewListController(this._repository)
    : super(const ClassificationReviewListState()) {
    Future<void>.microtask(initialize);
  }

  final ClassificationReviewRepository _repository;
  int _requestGeneration = 0;

  Future<void> initialize() async {
    final generation = ++_requestGeneration;
    try {
      final memberships = await _repository.listMyOpcMemberships();
      if (!mounted || generation != _requestGeneration) return;
      if (memberships.isEmpty) {
        state = const ClassificationReviewListState(loading: false);
        return;
      }
      final selectedOpcId = memberships.first.id;
      state = ClassificationReviewListState(
        memberships: memberships,
        selectedOpcId: selectedOpcId,
      );
      await _load(selectedOpcId, ReviewStatus.open);
    } catch (error) {
      if (mounted && generation == _requestGeneration) {
        state = ClassificationReviewListState(loading: false, error: error);
      }
    }
  }

  Future<void> selectOpc(String opcId) async {
    if (!state.memberships.any((membership) => membership.id == opcId)) return;
    state = ClassificationReviewListState(
      memberships: state.memberships,
      selectedOpcId: opcId,
      status: state.status,
    );
    await _load(opcId, state.status);
  }

  Future<void> selectStatus(ReviewStatus status) async {
    final opcId = state.selectedOpcId;
    if (opcId == null || status == state.status) return;
    state = ClassificationReviewListState(
      memberships: state.memberships,
      selectedOpcId: opcId,
      status: status,
    );
    await _load(opcId, status);
  }

  Future<void> refresh() async {
    final opcId = state.selectedOpcId;
    if (opcId == null) {
      await initialize();
      return;
    }
    await _load(opcId, state.status);
  }

  Future<void> loadMore() async {
    final opcId = state.selectedOpcId;
    final cursor = state.nextBefore;
    if (opcId == null || cursor == null || state.loadingMore) return;
    await _load(opcId, state.status, before: cursor, append: true);
  }

  Future<void> _load(
    String opcId,
    ReviewStatus status, {
    String? before,
    bool append = false,
  }) async {
    final generation = ++_requestGeneration;
    final existingItems = append ? state.items : const <ClassificationReview>[];
    state = ClassificationReviewListState(
      memberships: state.memberships,
      selectedOpcId: opcId,
      status: status,
      items: existingItems,
      nextBefore: append ? state.nextBefore : null,
      loading: !append,
      loadingMore: append,
    );
    try {
      final page = await _repository.listReviews(opcId, status, before: before);
      if (!mounted || generation != _requestGeneration) return;
      state = ClassificationReviewListState(
        memberships: state.memberships,
        selectedOpcId: opcId,
        status: status,
        items: <ClassificationReview>[...existingItems, ...page.items],
        nextBefore: page.nextBefore,
        loading: false,
      );
    } catch (error) {
      if (!mounted || generation != _requestGeneration) return;
      final unavailable = isClassificationReviewUnavailable(error);
      state = ClassificationReviewListState(
        memberships: state.memberships,
        selectedOpcId: opcId,
        status: status,
        items: unavailable ? const <ClassificationReview>[] : existingItems,
        nextBefore: unavailable ? null : (append ? state.nextBefore : null),
        loading: false,
        error: error,
      );
    }
  }
}
