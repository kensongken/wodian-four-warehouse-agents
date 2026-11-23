import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_error.dart';
import 'classification_review_models.dart';
import 'classification_review_repository.dart';

typedef ClassificationReviewIdentity = ({String opcId, String reviewId});

enum ClassificationConfirmOutcome { confirmed, alreadyResolved, notReviewable }

final classificationReviewDetailControllerProvider = StateNotifierProvider
    .autoDispose
    .family<
      ClassificationReviewDetailController,
      ClassificationReviewDetailState,
      ClassificationReviewIdentity
    >(
      (ref, identity) => ClassificationReviewDetailController(
        ref.watch(classificationReviewRepositoryProvider),
        identity,
      ),
    );

final class ClassificationReviewDetailState {
  const ClassificationReviewDetailState({
    this.review,
    this.selectedLevel1,
    this.selectedLevel2,
    this.loading = true,
    this.submitting = false,
    this.error,
  });

  final ClassificationReview? review;
  final String? selectedLevel1;
  final String? selectedLevel2;
  final bool loading;
  final bool submitting;
  final Object? error;

  bool get canConfirm =>
      review?.status == ReviewStatus.open &&
      selectedLevel1 != null &&
      selectedLevel2 != null &&
      !submitting;
}

final class ClassificationReviewDetailController
    extends StateNotifier<ClassificationReviewDetailState> {
  ClassificationReviewDetailController(this._repository, this._identity)
    : super(const ClassificationReviewDetailState()) {
    Future<void>.microtask(load);
  }

  final ClassificationReviewRepository _repository;
  final ClassificationReviewIdentity _identity;

  Future<void> load() async {
    state = ClassificationReviewDetailState(
      review: state.review,
      selectedLevel1: state.selectedLevel1,
      selectedLevel2: state.selectedLevel2,
    );
    try {
      final review = await _repository.getReview(
        _identity.opcId,
        _identity.reviewId,
      );
      if (!mounted) return;
      state = ClassificationReviewDetailState(
        review: review,
        selectedLevel1: review.confirmedLevel1,
        selectedLevel2: review.confirmedLevel2,
        loading: false,
      );
    } catch (error) {
      if (!mounted) return;
      state = ClassificationReviewDetailState(loading: false, error: error);
    }
  }

  void chooseCandidate(int index) {
    final review = state.review;
    if (review == null ||
        review.status != ReviewStatus.open ||
        index < 0 ||
        index >= review.candidates.length ||
        state.submitting) {
      return;
    }
    final candidate = review.candidates[index];
    state = ClassificationReviewDetailState(
      review: review,
      selectedLevel1: candidate.level1,
      selectedLevel2: candidate.level2,
      loading: false,
    );
  }

  void selectLevel1(String? value) {
    if (value == null || !level1Categories.contains(value)) return;
    _select(level1: value, level2: state.selectedLevel2);
  }

  void selectLevel2(String? value) {
    if (value == null || !level2Categories.contains(value)) return;
    _select(level1: state.selectedLevel1, level2: value);
  }

  void _select({required String? level1, required String? level2}) {
    final review = state.review;
    if (review == null ||
        review.status != ReviewStatus.open ||
        state.submitting) {
      return;
    }
    state = ClassificationReviewDetailState(
      review: review,
      selectedLevel1: level1,
      selectedLevel2: level2,
      loading: false,
    );
  }

  Future<ClassificationConfirmOutcome> confirm() async {
    if (!state.canConfirm) {
      throw StateError('classification selection is incomplete');
    }
    final level1 = state.selectedLevel1!;
    final level2 = state.selectedLevel2!;
    state = ClassificationReviewDetailState(
      review: state.review,
      selectedLevel1: level1,
      selectedLevel2: level2,
      loading: false,
      submitting: true,
    );
    try {
      final review = await _repository.confirm(
        _identity.opcId,
        _identity.reviewId,
        level1: level1,
        level2: level2,
      );
      if (mounted) {
        state = ClassificationReviewDetailState(
          review: review,
          selectedLevel1: review.confirmedLevel1,
          selectedLevel2: review.confirmedLevel2,
          loading: false,
        );
      }
      return ClassificationConfirmOutcome.confirmed;
    } on ApiException catch (error) {
      final outcome = switch (error.code) {
        'review_already_resolved' =>
          ClassificationConfirmOutcome.alreadyResolved,
        'classification_not_reviewable' =>
          ClassificationConfirmOutcome.notReviewable,
        _ => null,
      };
      if (outcome == null) {
        if (mounted) {
          state = ClassificationReviewDetailState(
            review: state.review,
            selectedLevel1: level1,
            selectedLevel2: level2,
            loading: false,
          );
        }
        rethrow;
      }
      await load();
      return outcome;
    } catch (_) {
      if (mounted) {
        state = ClassificationReviewDetailState(
          review: state.review,
          selectedLevel1: level1,
          selectedLevel2: level2,
          loading: false,
        );
      }
      rethrow;
    }
  }
}
