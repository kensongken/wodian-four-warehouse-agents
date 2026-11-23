import 'dart:async';

import 'package:flutter/services.dart';

import 'background_transfer_bridge.dart';
import 'part_transfer_client.dart';

final class MethodChannelBackgroundTransferBridge
    implements BackgroundTransferBridge {
  MethodChannelBackgroundTransferBridge({
    this._methodChannel = const MethodChannel(
      'photo_organizer/background_transfer',
    ),
    EventChannel eventChannel = const EventChannel(
      'photo_organizer/background_transfer_events',
    ),
  }) {
    _events = eventChannel.receiveBroadcastStream().map((value) {
      final job = _decodeJob(value);
      if (job.isTerminal && _locallyEnqueuedJobs.contains(job.jobId)) {
        _terminalReplay[job.jobId] = job;
      }
      return job;
    }).asBroadcastStream();
  }

  final MethodChannel _methodChannel;
  late final Stream<NativeTransferJob> _events;
  final Set<String> _locallyEnqueuedJobs = <String>{};
  final Set<String> _cancelledLocalJobs = <String>{};
  final Map<String, NativeTransferJob> _terminalReplay =
      <String, NativeTransferJob>{};
  final Map<String, Completer<void>> _cancellationWaiters =
      <String, Completer<void>>{};

  @override
  Stream<NativeTransferJob> get events => _events;

  @override
  Future<String> enqueuePart(
    TransferPartRequest request, {
    required bool allowMobileData,
  }) async {
    _locallyEnqueuedJobs.add(request.jobId);
    try {
      final result = await _methodChannel.invokeMethod<Object?>(
        'enqueuePart',
        <String, Object?>{
          'jobId': request.jobId,
          'asset': request.asset.toMap(),
          'partNumber': request.partNumber,
          'byteOffset': request.byteOffset,
          'byteLength': request.byteLength,
          // This URL crosses the platform channel once and is never included
          // in the durable Drift linkage or any diagnostic text.
          'signedUrl': request.signedUrl.toString(),
          'allowMobileData': allowMobileData,
        },
      );
      if (result is! String || result != request.jobId) {
        throw const FormatException('invalid native job identifier');
      }
      return result;
    } catch (_) {
      _locallyEnqueuedJobs.remove(request.jobId);
      rethrow;
    }
  }

  @override
  Future<Map<String, NativeTransferJob>> queryJobs(
    Iterable<String> nativeJobIds,
  ) async {
    final requested = nativeJobIds.toSet();
    if (requested.isEmpty) return const <String, NativeTransferJob>{};
    final response = await _methodChannel.invokeMethod<Object?>(
      'queryJobs',
      <String, Object>{'jobIds': requested.toList(growable: false)},
    );
    if (response is! List<Object?>) {
      throw const FormatException('native jobs must be a list');
    }
    final jobs = <String, NativeTransferJob>{};
    for (final raw in response) {
      final job = _decodeJob(raw);
      if (!requested.contains(job.jobId) || jobs.containsKey(job.jobId)) {
        throw const FormatException('native job response is not requested');
      }
      jobs[job.jobId] = job;
    }
    return Map<String, NativeTransferJob>.unmodifiable(jobs);
  }

  @override
  Future<NativeTransferJob> waitForTerminal(String nativeJobId) async {
    if (_cancelledLocalJobs.remove(nativeJobId)) {
      _locallyEnqueuedJobs.remove(nativeJobId);
      _terminalReplay.remove(nativeJobId);
      throw StateError('native transfer cancelled');
    }
    final replay = _terminalReplay.remove(nativeJobId);
    if (replay != null) {
      _locallyEnqueuedJobs.remove(nativeJobId);
      return replay;
    }
    final event = Completer<NativeTransferJob>();
    final cancellation = Completer<void>();
    _cancellationWaiters[nativeJobId] = cancellation;
    late final StreamSubscription<NativeTransferJob> subscription;
    subscription = events.listen(
      (job) {
        if (job.jobId == nativeJobId && job.isTerminal && !event.isCompleted) {
          event.complete(job);
        }
      },
      onError: (Object error, StackTrace stackTrace) {
        if (!event.isCompleted) event.completeError(error, stackTrace);
      },
    );
    try {
      final known = await queryJobs(<String>[nativeJobId]);
      final current = known[nativeJobId];
      if (current != null && current.isTerminal) return current;
      // The process-wide event listener may observe and persist/ack a very
      // fast terminal result between the initial replay check and this
      // direct subscription. Re-check after the subscription and native
      // query are established so that result cannot fall through both paths.
      final replayAfterQuery = _terminalReplay.remove(nativeJobId);
      if (replayAfterQuery != null) return replayAfterQuery;
      if (_cancelledLocalJobs.remove(nativeJobId)) {
        throw StateError('native transfer cancelled');
      }
      return await Future.any(<Future<NativeTransferJob>>[
        event.future,
        cancellation.future.then<NativeTransferJob>(
          (_) => throw StateError('native transfer cancelled'),
        ),
      ]);
    } finally {
      await subscription.cancel();
      if (identical(_cancellationWaiters[nativeJobId], cancellation)) {
        _cancellationWaiters.remove(nativeJobId);
      }
      _cancelledLocalJobs.remove(nativeJobId);
      _terminalReplay.remove(nativeJobId);
      _locallyEnqueuedJobs.remove(nativeJobId);
    }
  }

  @override
  Future<void> acknowledge(String nativeJobId) =>
      _methodChannel.invokeMethod<void>('acknowledge', <String, Object>{
        'jobId': nativeJobId,
      });

  @override
  Future<void> cancel(String nativeJobId) async {
    await _methodChannel.invokeMethod<void>('cancel', <String, Object>{
      'jobId': nativeJobId,
    });
    if (!_locallyEnqueuedJobs.contains(nativeJobId)) return;
    _cancelledLocalJobs.add(nativeJobId);
    _terminalReplay.remove(nativeJobId);
    final waiter = _cancellationWaiters[nativeJobId];
    if (waiter != null && !waiter.isCompleted) waiter.complete();
  }
}

NativeTransferJob _decodeJob(Object? value) {
  if (value is! Map<Object?, Object?>) {
    throw const FormatException('native job must be an object');
  }
  return NativeTransferJob.fromMap(<String, Object?>{
    for (final entry in value.entries)
      if (entry.key is String) entry.key as String: entry.value,
  });
}
