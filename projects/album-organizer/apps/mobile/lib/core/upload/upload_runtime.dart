import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../features/tasks/task_repository.dart';
import '../auth/session_repository.dart';
import '../media/method_channel_media_library.dart';
import 'method_channel_background_transfer.dart';
import 'part_transfer_client.dart';
import 'upload_api.dart';
import 'upload_coordinator.dart';
import 'upload_queue_repository.dart';

abstract interface class BackgroundUploadRuntime {
  Future<void> resume();
}

final backgroundUploadRuntimeProvider = Provider<BackgroundUploadRuntime>((
  ref,
) {
  final session = ref.watch(sessionRepositoryProvider);
  final queue = ref.watch(uploadQueueRepositoryProvider);
  final coordinator = UploadCoordinator(
    queue: queue,
    mediaLibrary: MethodChannelMediaLibrary(),
    api: SessionUploadApi(session),
    transferClient: const _BackgroundOnlyPartTransferClient(),
    backgroundTransfers: MethodChannelBackgroundTransferBridge(),
    network: ConnectivityNetworkMonitor(),
    tasks: ref.watch(taskRepositoryProvider),
    refreshSession: () async {
      await session.refresh();
    },
  );
  final runtime = _CoordinatorBackgroundUploadRuntime(
    coordinator,
    queue,
    session,
  );
  ref.onDispose(() => unawaited(runtime.dispose()));
  return runtime;
});

final class ConnectivityNetworkMonitor implements NetworkMonitor {
  ConnectivityNetworkMonitor([Connectivity? connectivity])
    : _connectivity = connectivity ?? Connectivity();

  final Connectivity _connectivity;

  @override
  Future<NetworkTransport> current() async =>
      _transport(await _connectivity.checkConnectivity());

  @override
  Stream<NetworkTransport> get changes =>
      _connectivity.onConnectivityChanged.map(_transport).distinct();

  static NetworkTransport _transport(List<ConnectivityResult> results) {
    if (results.contains(ConnectivityResult.none)) {
      return NetworkTransport.offline;
    }
    if (results.contains(ConnectivityResult.wifi) ||
        results.contains(ConnectivityResult.ethernet)) {
      return NetworkTransport.wifi;
    }
    return NetworkTransport.mobile;
  }
}

final class _CoordinatorBackgroundUploadRuntime
    implements BackgroundUploadRuntime {
  _CoordinatorBackgroundUploadRuntime(
    this._coordinator,
    this._queue,
    this._session,
  ) {
    _workSubscription = _queue.workAvailable.listen(
      (taskId) => unawaited(
        _runTaskIfSignedIn(taskId).catchError((Object _) {
          // Durable queue state is retried by the next signal or app resume.
        }),
      ),
    );
    _sessionSubscription = _session.sessionChanges.listen((signedIn) {
      if (signedIn) unawaited(resume().catchError((Object _) {}));
    });
  }

  final UploadCoordinator _coordinator;
  final UploadQueueRepository _queue;
  final SessionRepository _session;
  late final StreamSubscription<String> _workSubscription;
  late final StreamSubscription<bool> _sessionSubscription;

  @override
  Future<void> resume() async {
    if (!await _session.isSignedIn) return;
    await _coordinator.resumeBackgroundWork();
  }

  Future<void> _runTaskIfSignedIn(String taskId) async {
    if (!await _session.isSignedIn) return;
    await _coordinator.runUntilIdle(taskId);
  }

  Future<void> dispose() async {
    await _workSubscription.cancel();
    await _sessionSubscription.cancel();
    await _coordinator.dispose();
  }
}

final class _BackgroundOnlyPartTransferClient implements PartTransferClient {
  const _BackgroundOnlyPartTransferClient();

  @override
  Future<TransferPartResult> transfer(TransferPartRequest request) =>
      throw StateError('Dart byte transfer is disabled');
}
