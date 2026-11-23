import 'dart:convert';
import 'dart:io';

import 'package:drift/drift.dart'
    show OpeningDetails, QueryExecutor, QueryExecutorUser;
import 'package:drift/native.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:photo_organizer/core/api/models.dart';
import 'package:photo_organizer/core/media/local_asset_ref.dart';
import 'package:photo_organizer/core/upload/upload_database.dart';
import 'package:photo_organizer/core/upload/upload_api.dart';
import 'package:photo_organizer/core/upload/upload_models.dart';
import 'package:photo_organizer/core/upload/upload_queue_repository.dart';
import 'package:photo_organizer/features/tasks/task_repository.dart';
import 'package:photo_organizer/features/tasks/import_task_screen.dart';

const _taskId = '11111111-1111-4111-8111-111111111111';
const _bindingId = '22222222-2222-4222-8222-222222222222';
const _candidateId =
    'opc-controlled-upload-candidate:'
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const _partSize = 8 * 1024 * 1024;

void main() {
  test('pause fences stale owner state and ETag writes', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final repository = UploadQueueRepository(database);
    await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final now = DateTime.utc(2026, 8, 15, 1);
    final first = await repository.nextRunnable(
      _taskId,
      ownerId: 'owner-before-pause',
      now: now,
      leaseDuration: const Duration(minutes: 30),
    );

    await repository.pauseTask(_taskId);
    await repository.resumeTask(_taskId);
    final second = await repository.nextRunnable(
      _taskId,
      ownerId: 'owner-after-pause',
      now: now,
      leaseDuration: const Duration(minutes: 30),
    );

    expect(second, isNotNull);
    await expectLater(
      repository.markPartComplete(first!.claim, 1, 'stale-etag'),
      throwsA(isA<UploadLeaseLostException>()),
    );
    await repository.markPartComplete(second!.claim, 1, 'fresh-etag');
    expect(
      (await repository.find(_asset17MiB.id))!.completedParts,
      <int, String>{1: 'fresh-etag'},
    );
  });

  test('atomically leases one attempt to only one queue owner', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final first = UploadQueueRepository(database);
    final second = UploadQueueRepository(database);
    await first.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final now = DateTime.utc(2026, 8, 15, 2);

    final claims = await Future.wait(<Future<QueuedUploadItem?>>[
      first.nextRunnable(
        _taskId,
        ownerId: 'coordinator-a',
        now: now,
        leaseDuration: const Duration(minutes: 30),
      ),
      second.nextRunnable(
        _taskId,
        ownerId: 'coordinator-b',
        now: now,
        leaseDuration: const Duration(minutes: 30),
      ),
    ]);

    expect(claims.whereType<QueuedUploadItem>(), hasLength(1));
    expect(claims.whereType<QueuedUploadItem>().single.retryCount, 1);
    expect(
      claims.whereType<QueuedUploadItem>().single.leaseOwner,
      anyOf('coordinator-a', 'coordinator-b'),
    );
    expect(
      await first.nextRunnable(
        _taskId,
        ownerId: 'coordinator-c',
        now: now.add(const Duration(minutes: 29)),
        leaseDuration: const Duration(minutes: 30),
      ),
      isNull,
    );
  });

  test('an exhausted lease can never claim a ninth transfer attempt', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final repository = UploadQueueRepository(database);
    await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final now = DateTime.utc(2026, 8, 15, 2);

    for (var attempt = 1; attempt <= 8; attempt += 1) {
      final claimed = await repository.nextRunnable(
        _taskId,
        ownerId: 'attempt-$attempt',
        now: now,
        leaseDuration: const Duration(minutes: 30),
      );
      expect(claimed!.retryCount, attempt);
      await repository.scheduleRetry(
        claimed.claim,
        retryCount: attempt,
        nextAttemptAt: now,
        code: 'network_unavailable',
        safeMessage: '网络不可用，请检查网络后重试',
      );
    }

    expect(
      await repository.nextRunnable(
        _taskId,
        ownerId: 'attempt-9',
        now: now,
        leaseDuration: const Duration(minutes: 30),
      ),
      isNull,
    );
  });

  test('restores incomplete parts after process restart', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final first = UploadQueueRepository(database);
    await first.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final initial = await first.nextRunnable(
      _taskId,
      ownerId: 'crashed-owner',
      now: DateTime.utc(2026, 8, 14),
      leaseDuration: Duration.zero,
    );
    await first.markPartComplete(initial!.claim, 1, 'etag-1');

    final restored = await UploadQueueRepository(database).nextRunnable(
      _taskId,
      ownerId: 'restart-test',
      now: DateTime.utc(2026, 8, 15),
      leaseDuration: const Duration(minutes: 30),
    );

    expect(restored, isNotNull);
    expect(restored!.completedParts, <int, String>{1: 'etag-1'});
    expect(restored.remainingPartNumbers, <int>[2, 3]);
  });

  test('rejects a conflicted batch without taking any ownership', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final repository = UploadQueueRepository(database);
    await repository.recordServerDisposition(
      _taskId,
      const UnsupportedReportReceipt(
        localAssetId: 'ios:known',
        disposition: ServerAssetDisposition.accepted,
      ),
    );

    await expectLater(
      repository.enqueue(_taskId, <LocalAssetRef>[
        _asset17MiB,
        _asset17MiBWith(id: 'ios:known'),
      ]),
      throwsStateError,
    );

    final snapshot = await repository.snapshot(_taskId);
    expect(snapshot.unacknowledgedAssetIds, isEmpty);
    expect(snapshot.queued, 0);
    expect(snapshot.serverAcceptedAssetIds, <String>{'ios:known'});
  });

  test(
    'new queue work is signalled and remains discoverable after restart',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);
      final signals = repository.workAvailable.take(2).toList();

      await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
      await repository.setSealWhenIdle(_taskId);

      expect(await signals, <String>[_taskId, _taskId]);
      expect(await repository.taskIdsWithWork(), <String>{_taskId});
      expect(await UploadQueueRepository(database).taskIdsWithWork(), <String>{
        _taskId,
      });
    },
  );

  test('a terminal local-only failure is not resumed forever', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final repository = UploadQueueRepository(database);
    await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final item = await repository.nextRunnable(
      _taskId,
      ownerId: 'terminal-local-owner',
      now: DateTime.utc(2026, 8, 16),
      leaseDuration: const Duration(minutes: 30),
    );
    await repository.markFailed(
      item!.claim,
      retryCount: 8,
      code: 'media_unavailable',
      safeMessage: '无法读取照片，请重新添加',
    );

    expect(await repository.taskIdsWithWork(), isEmpty);
  });

  test('restores cleanup refs authority and seal intent', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final first = UploadQueueRepository(database);
    final cleanup = _asset17MiBWith(id: 'ios:cleanup');
    await first.retainCleanupRefs(_taskId, <LocalAssetRef>[cleanup]);
    await first.recordServerDisposition(
      _taskId,
      const UnsupportedReportReceipt(
        localAssetId: 'android:raw',
        disposition: ServerAssetDisposition.unsupported,
      ),
    );
    await first.setSealWhenIdle(_taskId);

    final restored = await UploadQueueRepository(database).snapshot(_taskId);

    expect(restored.cleanupRefs.map((asset) => asset.id), <String>[
      'ios:cleanup',
    ]);
    expect(restored.serverUnsupportedAssetIds, <String>{'android:raw'});
    expect(restored.sealWhenIdle, isTrue);
  });

  test(
    'persists one seal request time and the confirmed server time',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);

      await repository.setSealWhenIdle(_taskId);
      final requested = await (database.select(
        database.importQueueTasks,
      )..where((row) => row.taskId.equals(_taskId))).getSingle();
      await repository.setSealWhenIdle(_taskId);
      final repeated = await (database.select(
        database.importQueueTasks,
      )..where((row) => row.taskId.equals(_taskId))).getSingle();

      expect(requested.sealRequestedAt, isNotNull);
      expect(repeated.sealRequestedAt, requested.sealRequestedAt);

      final claimed = await repository.claimSeal(
        _taskId,
        ownerId: 'progress-owner',
        now: DateTime.utc(2026, 8, 16, 12),
        leaseDuration: const Duration(minutes: 5),
      );
      expect(claimed, isTrue);
      await repository.markServerSealed(
        _taskId,
        'progress-owner',
        now: DateTime.utc(2026, 8, 16, 12, 1),
      );
      final confirmed = await (database.select(
        database.importQueueTasks,
      )..where((row) => row.taskId.equals(_taskId))).getSingle();

      expect(
        confirmed.sealConfirmedAt?.toUtc(),
        DateTime.utc(2026, 8, 16, 12, 1),
      );
    },
  );

  test('forgets a cleanup ref only after explicit completion', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final repository = UploadQueueRepository(database);
    final cleanup = _asset17MiBWith(id: 'ios:cleanup');
    await repository.retainCleanupRefs(_taskId, <LocalAssetRef>[cleanup]);

    final completed = await repository.completeCleanupRef(_taskId, cleanup);

    expect(completed.cleanupRefs, isEmpty);
  });

  test(
    'schema version 7 reopens with upload authority and queue state intact',
    () async {
      final directory = await Directory.systemTemp.createTemp(
        'upload-db-test-',
      );
      addTearDown(() => directory.delete(recursive: true));
      final file = File('${directory.path}/upload.sqlite');
      final firstDatabase = UploadDatabase(NativeDatabase(file));
      final first = UploadQueueRepository(firstDatabase);
      await first.enqueue(
        _taskId,
        <LocalAssetRef>[_asset17MiB],
        authority: UploadAuthority(
          bindingId: _bindingId,
          candidateId: _candidateId,
        ),
      );
      final initial = await first.nextRunnable(
        _taskId,
        ownerId: 'disk-crashed-owner',
        now: DateTime.utc(2026, 8, 14),
        leaseDuration: Duration.zero,
      );
      await first.markPartComplete(initial!.claim, 1, 'etag-disk');
      await firstDatabase.close();

      final reopenedDatabase = UploadDatabase(NativeDatabase(file));
      addTearDown(reopenedDatabase.close);
      final version = await reopenedDatabase
          .customSelect('PRAGMA user_version')
          .getSingle();
      final tables = await reopenedDatabase
          .customSelect(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
          )
          .get();
      final restored = await UploadQueueRepository(reopenedDatabase)
          .nextRunnable(
            _taskId,
            ownerId: 'disk-restart-test',
            now: DateTime.utc(2026, 8, 15),
            leaseDuration: const Duration(minutes: 30),
          );

      expect(version.read<int>('user_version'), 7);
      expect(
        tables.map((row) => row.read<String>('name')),
        containsAll(<String>['upload_items', 'upload_parts']),
      );
      expect(restored!.completedParts, <int, String>{1: 'etag-disk'});
      expect(restored.authority!.bindingId, _bindingId);
      expect(restored.authority!.candidateId, _candidateId);
    },
  );

  test('migrates schema v6 rows with no upload authority', () async {
    final directory = await Directory.systemTemp.createTemp(
      'upload-v6-authority-test-',
    );
    addTearDown(() => directory.delete(recursive: true));
    final file = File('${directory.path}/upload.sqlite');
    final seed = UploadDatabase(NativeDatabase(file));
    await UploadQueueRepository(seed)
        .enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    await seed.customStatement(
      'ALTER TABLE upload_items DROP COLUMN binding_id',
    );
    await seed.customStatement(
      'ALTER TABLE upload_items DROP COLUMN candidate_id',
    );
    await seed.customStatement('PRAGMA user_version = 6');
    await seed.close();

    final migrated = UploadDatabase(NativeDatabase(file));
    addTearDown(migrated.close);
    final version = await migrated
        .customSelect('PRAGMA user_version')
        .getSingle();
    final columns = await migrated
        .customSelect('PRAGMA table_info(upload_items)')
        .get();
    final restored = await UploadQueueRepository(migrated).find(_asset17MiB.id);

    expect(version.read<int>('user_version'), 7);
    expect(
      columns.map((row) => row.read<String>('name')),
      containsAll(<String>['binding_id', 'candidate_id']),
    );
    expect(restored, isNotNull);
    expect(restored!.authority, isNull);
  });

  test('rejects malformed upload authority before queue ownership changes', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final repository = UploadQueueRepository(database);

    expect(
      () => UploadAuthority(bindingId: 'not-a-uuid', candidateId: _candidateId),
      throwsFormatException,
    );
    expect(
      () => UploadAuthority(
        bindingId: _bindingId,
        candidateId:
            'opc-controlled-upload-candidate:'
            'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      ),
      throwsFormatException,
    );
    expect((await repository.snapshot(_taskId)).queued, 0);
  });

  test(
    'controlled candidate handoff inserts once and exact replay is idempotent',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);
      final authority = UploadAuthority(
        bindingId: _bindingId,
        candidateId: _candidateId,
      );

      final first = await repository.enqueueControlledCandidate(
        _taskId,
        <LocalAssetRef>[_controlledAssetA, _controlledAssetB],
        authority: authority,
      );
      final replay = await repository.enqueueControlledCandidate(
        _taskId,
        <LocalAssetRef>[_controlledAssetB, _controlledAssetA],
        authority: authority,
      );

      expect(first.queued, 2);
      expect(replay.queued, 2);
      expect(
        (await repository.find(_controlledAssetA.id))!.authority!.candidateId,
        _candidateId,
      );
    },
  );

  test(
    'controlled candidate rejects partial queue ownership atomically',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);
      await repository.enqueue(_taskId, <LocalAssetRef>[_controlledAssetA]);

      await expectLater(
        repository.enqueueControlledCandidate(
          _taskId,
          <LocalAssetRef>[_controlledAssetA, _controlledAssetB],
          authority: UploadAuthority(
            bindingId: _bindingId,
            candidateId: _candidateId,
          ),
        ),
        throwsA(_queueError('candidate_queue_conflict')),
      );

      expect(await repository.find(_controlledAssetB.id), isNull);
      expect((await repository.snapshot(_taskId)).queued, 1);
    },
  );

  test(
    'controlled candidate rejects a different binding on exact IDs',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);
      await repository.enqueueControlledCandidate(
        _taskId,
        <LocalAssetRef>[_controlledAssetA, _controlledAssetB],
        authority: UploadAuthority(
          bindingId: _bindingId,
          candidateId: _candidateId,
        ),
      );

      await expectLater(
        repository.enqueueControlledCandidate(
          _taskId,
          <LocalAssetRef>[_controlledAssetA, _controlledAssetB],
          authority: UploadAuthority(
            bindingId: '33333333-3333-4333-8333-333333333333',
            candidateId: _candidateId,
          ),
        ),
        throwsA(_queueError('candidate_queue_conflict')),
      );
    },
  );

  test(
    'controlled candidate prevents one binding from authorizing another set',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);
      await repository.enqueueControlledCandidate(
        _taskId,
        <LocalAssetRef>[_controlledAssetA, _controlledAssetB],
        authority: UploadAuthority(
          bindingId: _bindingId,
          candidateId: _candidateId,
        ),
      );

      await expectLater(
        repository.enqueueControlledCandidate(
          _taskId,
          <LocalAssetRef>[_controlledAssetC],
          authority: UploadAuthority(
            bindingId: _bindingId,
            candidateId:
                'opc-controlled-upload-candidate:'
                'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
          ),
        ),
        throwsA(_queueError('candidate_queue_conflict')),
      );

      expect(await repository.find(_controlledAssetC.id), isNull);
    },
  );

  for (final damage in <({String name, String? bindingId, String? candidateId})>[
    (name: 'binding only', bindingId: _bindingId, candidateId: null),
    (name: 'candidate only', bindingId: null, candidateId: _candidateId),
    (
      name: 'invalid binding UUID',
      bindingId: 'not-a-uuid',
      candidateId: _candidateId,
    ),
    (
      name: 'invalid candidate ID',
      bindingId: _bindingId,
      candidateId:
          'opc-controlled-upload-candidate:'
          'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    ),
  ]) {
    test(
      'fails closed for stored upload authority damage: ${damage.name}',
      () async {
        final database = UploadDatabase(NativeDatabase.memory());
        addTearDown(database.close);
        final repository = UploadQueueRepository(database);
        await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
        await database.customStatement(
          'UPDATE upload_items SET binding_id = ?, candidate_id = ? '
          'WHERE local_asset_id = ?',
          <Object?>[damage.bindingId, damage.candidateId, _asset17MiB.id],
        );

        await expectLater(
          repository.find(_asset17MiB.id),
          throwsA(
            isA<UploadQueueException>().having(
              (error) => error.code,
              'code',
              'invalid_upload_authority',
            ),
          ),
        );
      },
    );
  }

  test(
    'migrates a real schema v4 file with nullable native job fences',
    () async {
      final directory = await Directory.systemTemp.createTemp(
        'upload-v4-native-job-test-',
      );
      addTearDown(() => directory.delete(recursive: true));
      final file = File('${directory.path}/upload.sqlite');
      final seed = UploadDatabase(NativeDatabase(file));
      final seedRepository = UploadQueueRepository(seed);
      await seedRepository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
      final columns = await seed
          .customSelect('PRAGMA table_info(upload_parts)')
          .get();
      final names = columns.map((row) => row.read<String>('name')).toSet();
      for (final column in <String>[
        'native_job_id',
        'native_lease_owner',
        'native_lease_generation',
        'native_policy_generation',
      ]) {
        if (names.contains(column)) {
          await seed.customStatement(
            'ALTER TABLE upload_parts DROP COLUMN $column',
          );
        }
      }
      for (final column in <String>['seal_requested_at', 'seal_confirmed_at']) {
        await seed.customStatement(
          'ALTER TABLE import_queue_tasks DROP COLUMN $column',
        );
      }
      for (final column in <String>['binding_id', 'candidate_id']) {
        await seed.customStatement(
          'ALTER TABLE upload_items DROP COLUMN $column',
        );
      }
      await seed.customStatement('PRAGMA user_version = 4');
      await seed.close();

      final migrated = UploadDatabase(NativeDatabase(file));
      addTearDown(migrated.close);
      final version = await migrated
          .customSelect('PRAGMA user_version')
          .getSingle();
      final migratedColumns = await migrated
          .customSelect('PRAGMA table_info(upload_parts)')
          .get();

      expect(version.read<int>('user_version'), 7);
      expect(
        migratedColumns.map((row) => row.read<String>('name')),
        containsAll(<String>[
          'native_job_id',
          'native_lease_owner',
          'native_lease_generation',
          'native_policy_generation',
        ]),
      );
      expect(
        (await UploadQueueRepository(migrated).find(_asset17MiB.id)),
        isNotNull,
      );
    },
  );

  test(
    'v3 terminal acknowledgement is revalidated after v7 migration',
    () async {
      final directory = await Directory.systemTemp.createTemp(
        'upload-v3-terminal-receipt-test-',
      );
      addTearDown(() => directory.delete(recursive: true));
      final file = File('${directory.path}/upload.sqlite');
      final seed = UploadDatabase(NativeDatabase(file));
      final seedRepository = UploadQueueRepository(seed);
      await seedRepository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
      final claim = await seedRepository.nextRunnable(
        _taskId,
        ownerId: 'v3-terminal-seed',
        now: DateTime.utc(2026, 8, 15),
        leaseDuration: const Duration(minutes: 30),
      );
      await seedRepository.markAlreadyUploaded(
        claim!.claim,
        const InitiatedAsset(
          assetId: 'backend-v3-terminal',
          alreadyUploaded: true,
          partSize: _partSize,
          completedPartNumbers: <int>[],
        ),
      );
      await seed.customStatement(
        'INSERT INTO import_queue_authorities '
        '(task_id, local_asset_id, disposition) VALUES (?, ?, ?)',
        <Object?>[
          _taskId,
          _asset17MiB.id,
          ServerAssetDisposition.accepted.name,
        ],
      );
      await seed.customStatement(
        'UPDATE upload_items SET terminal_summary_acknowledged = 1 '
        'WHERE local_asset_id = ?',
        <Object?>[_asset17MiB.id],
      );
      await seed.customStatement(
        'ALTER TABLE upload_items DROP COLUMN retry_requested',
      );
      await seed.customStatement(
        'ALTER TABLE upload_items DROP COLUMN local_rejected',
      );
      for (final column in <String>[
        'native_job_id',
        'native_lease_owner',
        'native_lease_generation',
        'native_policy_generation',
      ]) {
        await seed.customStatement(
          'ALTER TABLE upload_parts DROP COLUMN $column',
        );
      }
      for (final column in <String>['seal_requested_at', 'seal_confirmed_at']) {
        await seed.customStatement(
          'ALTER TABLE import_queue_tasks DROP COLUMN $column',
        );
      }
      for (final column in <String>['binding_id', 'candidate_id']) {
        await seed.customStatement(
          'ALTER TABLE upload_items DROP COLUMN $column',
        );
      }
      await seed.customStatement('PRAGMA user_version = 3');
      await seed.close();

      final migrated = UploadDatabase(NativeDatabase(file));
      addTearDown(migrated.close);
      final repository = UploadQueueRepository(migrated);
      final version = await migrated
          .customSelect('PRAGMA user_version')
          .getSingle();
      final columns = await migrated
          .customSelect('PRAGMA table_info(upload_items)')
          .get();

      expect(version.read<int>('user_version'), 7);
      expect(
        columns.map((row) => row.read<String>('name')),
        containsAll(<String>['retry_requested', 'local_rejected']),
      );
      final restored = (await repository.find(_asset17MiB.id))!;
      final beforeFreshSummary = await repository.snapshot(_taskId);
      expect(restored.retryRequested, isFalse);
      expect(restored.serverConfirmed, isTrue);
      expect(restored.terminalSummaryAcknowledged, isFalse);
      expect(beforeFreshSummary.serverAcceptedAssetIds, <String>{
        _asset17MiB.id,
      });
      expect(beforeFreshSummary.uploaded, 1);

      final fence = await repository.captureServerSummaryFence(_taskId);
      await repository.acknowledgeServerSummary(
        fence,
        _summary(totalAssets: 1, uploadedAssets: 1),
      );

      expect(
        (await repository.find(_asset17MiB.id))!.terminalSummaryAcknowledged,
        isTrue,
      );
      expect((await repository.snapshot(_taskId)).uploaded, 0);
    },
  );

  test('migrates a complete schema v1 queue to v7 without losing state', () async {
    final directory = await Directory.systemTemp.createTemp('upload-v1-test-');
    addTearDown(() => directory.delete(recursive: true));
    final file = File('${directory.path}/upload.sqlite');
    final legacy = NativeDatabase(file);
    await legacy.ensureOpen(const _SchemaV1ExecutorUser());
    await legacy.runCustom('''
      CREATE TABLE upload_items (
        local_asset_id TEXT NOT NULL PRIMARY KEY,
        task_id TEXT NOT NULL,
        local_ref_json TEXT NOT NULL,
        byte_size INTEGER NOT NULL,
        sha256 TEXT,
        backend_asset_id TEXT,
        provider_upload_id TEXT,
        part_size INTEGER NOT NULL,
        retry_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER,
        state TEXT NOT NULL,
        last_error_code TEXT,
        last_error_message TEXT
      )
    ''');
    await legacy.runCustom('''
      CREATE TABLE upload_parts (
        local_asset_id TEXT NOT NULL,
        part_number INTEGER NOT NULL,
        etag TEXT,
        PRIMARY KEY (local_asset_id, part_number),
        FOREIGN KEY (local_asset_id) REFERENCES upload_items(local_asset_id)
          ON DELETE CASCADE
      )
    ''');
    await legacy.runCustom('''
      CREATE TABLE import_queue_tasks (
        task_id TEXT NOT NULL PRIMARY KEY,
        seal_when_idle INTEGER NOT NULL DEFAULT 0
      )
    ''');
    await legacy.runCustom('''
      CREATE TABLE import_queue_authorities (
        task_id TEXT NOT NULL,
        local_asset_id TEXT NOT NULL,
        disposition TEXT NOT NULL,
        PRIMARY KEY (task_id, local_asset_id)
      )
    ''');
    await legacy.runCustom('''
      CREATE TABLE import_cleanup_refs (
        task_id TEXT NOT NULL,
        locator TEXT NOT NULL,
        local_asset_id TEXT NOT NULL,
        local_ref_json TEXT NOT NULL,
        PRIMARY KEY (task_id, locator)
      )
    ''');
    await legacy.runCustom(
      'INSERT INTO upload_items '
      '(local_asset_id, task_id, local_ref_json, byte_size, part_size, state) '
      'VALUES (?, ?, ?, ?, ?, ?)',
      <Object?>[
        _asset17MiB.id,
        _taskId,
        '{"id":"legacy"}',
        _asset17MiB.byteSize,
        _partSize,
        LocalUploadState.awaitingHash.name,
      ],
    );
    await legacy.runCustom(
      'INSERT INTO import_queue_tasks (task_id, seal_when_idle) VALUES (?, 1)',
      <Object?>[_taskId],
    );
    await legacy.runCustom('PRAGMA user_version = 1');
    await legacy.close();

    final migrated = UploadDatabase(NativeDatabase(file));
    addTearDown(migrated.close);
    final version = await migrated
        .customSelect('PRAGMA user_version')
        .getSingle();
    final uploadColumns = await migrated
        .customSelect('PRAGMA table_info(upload_items)')
        .get();
    final taskColumns = await migrated
        .customSelect('PRAGMA table_info(import_queue_tasks)')
        .get();

    expect(version.read<int>('user_version'), 7);
    expect(
      uploadColumns.map((row) => row.read<String>('name')),
      containsAll(<String>[
        'lease_owner',
        'lease_expires_at',
        'lease_generation',
        'policy_generation',
        'server_confirmed',
        'terminal_summary_acknowledged',
        'retry_requested',
        'local_rejected',
        'paused_from_state',
        'failure_report_retry_count',
        'failure_report_next_attempt_at',
      ]),
    );
    expect(
      taskColumns.map((row) => row.read<String>('name')),
      containsAll(<String>[
        'server_sealed',
        'seal_requested_at',
        'seal_confirmed_at',
        'seal_lease_owner',
        'seal_lease_expires_at',
        'seal_retry_count',
        'seal_next_attempt_at',
        'seal_last_error_code',
      ]),
    );
    expect(
      await migrated.customSelect('SELECT state FROM upload_items').getSingle(),
      isNotNull,
    );
  });

  test(
    'v2 accepted terminal row stays summary-unacknowledged after migration',
    () async {
      final directory = await Directory.systemTemp.createTemp(
        'upload-v2-terminal-test-',
      );
      addTearDown(() => directory.delete(recursive: true));
      final file = File('${directory.path}/upload.sqlite');
      await _createV2TerminalDatabase(file);

      final migrated = UploadDatabase(NativeDatabase(file));
      addTearDown(migrated.close);
      final repository = UploadQueueRepository(migrated);
      final restored = (await repository.find(_asset17MiB.id))!;
      final snapshot = await repository.snapshot(_taskId);

      expect(restored.serverConfirmed, isTrue);
      expect(restored.terminalSummaryAcknowledged, isFalse);
      expect(snapshot.serverAcceptedAssetIds, <String>{_asset17MiB.id});
      expect(snapshot.uploaded, 1);
    },
  );

  test('application import provider uses the durable repository', () {
    final container = ProviderContainer();
    addTearDown(container.dispose);

    final boundary = container.read(importQueueBoundaryProvider(_taskId));

    expect(boundary, isA<UploadQueueRepository>());
  });

  test(
    'keeps initiated ownership local until summary acknowledgement',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);
      await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
      final claimed = await repository.nextRunnable(
        _taskId,
        ownerId: 'initiation-authority-owner',
        now: DateTime.utc(2026, 8, 15),
        leaseDuration: const Duration(minutes: 30),
      );

      await repository.markInitiated(
        claimed!.claim,
        const InitiatedAsset(
          assetId: 'backend-authority',
          alreadyUploaded: false,
          providerUploadId: 'provider-authority',
          partSize: _partSize,
          completedPartNumbers: <int>[],
        ),
      );

      final snapshot = await repository.snapshot(_taskId);
      expect(snapshot.unacknowledgedAssetIds, <String>{_asset17MiB.id});
      expect(snapshot.serverAcceptedAssetIds, isEmpty);
      expect(snapshot.queued, 1);
    },
  );

  test(
    'summary acknowledgement moves a confirmed upload out of local delta',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);
      await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
      final claimed = await repository.nextRunnable(
        _taskId,
        ownerId: 'terminal-authority-owner',
        now: DateTime.utc(2026, 8, 15),
        leaseDuration: const Duration(minutes: 30),
      );
      await repository.markInitiated(
        claimed!.claim,
        const InitiatedAsset(
          assetId: 'backend-uploaded',
          alreadyUploaded: true,
          partSize: _partSize,
          completedPartNumbers: <int>[],
        ),
      );

      await repository.markUploaded(claimed.claim);

      final pending = await repository.snapshot(_taskId);
      expect(pending.unacknowledgedAssetIds, <String>{_asset17MiB.id});
      expect(pending.serverAcceptedAssetIds, isEmpty);
      expect(pending.uploaded, 1);

      final fence = await repository.captureServerSummaryFence(_taskId);
      await repository.acknowledgeServerSummary(
        fence,
        _summary(totalAssets: 1, uploadedAssets: 1),
      );

      final acknowledged = await repository.snapshot(_taskId);
      expect(acknowledged.unacknowledgedAssetIds, isEmpty);
      expect(acknowledged.serverAcceptedAssetIds, <String>{_asset17MiB.id});
      expect(acknowledged.uploaded, 0);
      expect(acknowledged.failed, 0);
    },
  );

  test(
    'acceptance and terminal summary acknowledgements are independent',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);
      await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
      final claimed = await repository.nextRunnable(
        _taskId,
        ownerId: 'two-phase-authority-owner',
        now: DateTime.utc(2026, 8, 15),
        leaseDuration: const Duration(minutes: 30),
      );
      await repository.markInitiated(
        claimed!.claim,
        const InitiatedAsset(
          assetId: 'backend-two-phase-authority',
          alreadyUploaded: false,
          providerUploadId: 'provider-two-phase-authority',
          partSize: _partSize,
          completedPartNumbers: <int>[],
        ),
      );

      final acceptanceFence = await repository.captureServerSummaryFence(
        _taskId,
      );
      await repository.markUploaded(claimed.claim);
      await repository.acknowledgeServerSummary(
        acceptanceFence,
        _summary(totalAssets: 1, uploadedAssets: 0),
      );

      final acceptanceOnly = await repository.snapshot(_taskId);
      expect(acceptanceOnly.unacknowledgedAssetIds, isEmpty);
      expect(acceptanceOnly.serverAcceptedAssetIds, <String>{_asset17MiB.id});
      expect(acceptanceOnly.uploaded, 1);

      final terminalFence = await repository.captureServerSummaryFence(_taskId);
      await repository.acknowledgeServerSummary(
        terminalFence,
        _summary(totalAssets: 1, uploadedAssets: 1),
      );
      expect((await repository.snapshot(_taskId)).uploaded, 0);
    },
  );

  test(
    'a pre-GET fence never acknowledges a receipt committed after it',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);
      await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
      final claimed = await repository.nextRunnable(
        _taskId,
        ownerId: 'summary-race-owner',
        now: DateTime.utc(2026, 8, 15),
        leaseDuration: const Duration(minutes: 30),
      );

      final beforeReceipt = await repository.captureServerSummaryFence(_taskId);
      await repository.markAlreadyUploaded(
        claimed!.claim,
        const InitiatedAsset(
          assetId: 'backend-after-get-started',
          alreadyUploaded: true,
          partSize: _partSize,
          completedPartNumbers: <int>[],
        ),
      );

      await repository.acknowledgeServerSummary(
        beforeReceipt,
        _summary(totalAssets: 1, uploadedAssets: 1),
      );
      final stale = await repository.snapshot(_taskId);
      expect(stale.unacknowledgedAssetIds, <String>{_asset17MiB.id});
      expect(stale.uploaded, 1);

      final afterReceipt = await repository.captureServerSummaryFence(_taskId);
      await repository.acknowledgeServerSummary(
        afterReceipt,
        _summary(totalAssets: 1, uploadedAssets: 1),
      );
      final fresh = await repository.snapshot(_taskId);
      expect(fresh.unacknowledgedAssetIds, isEmpty);
      expect(fresh.uploaded, 0);
    },
  );

  test('2,999 plus one pending acceptance remains exactly 3,000', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final repository = UploadQueueRepository(database);
    await database.batch((batch) {
      batch.insertAll(
        database.importQueueAuthorities,
        <ImportQueueAuthoritiesCompanion>[
          for (var index = 0; index < 2999; index += 1)
            ImportQueueAuthoritiesCompanion.insert(
              taskId: _taskId,
              localAssetId: 'server:$index',
              disposition: ServerAssetDisposition.accepted.name,
            ),
        ],
      );
    });
    await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final claimed = await repository.nextRunnable(
      _taskId,
      ownerId: 'capacity-boundary-owner',
      now: DateTime.utc(2026, 8, 15),
      leaseDuration: const Duration(minutes: 30),
    );
    await repository.markInitiated(
      claimed!.claim,
      const InitiatedAsset(
        assetId: 'backend-capacity-boundary',
        alreadyUploaded: false,
        providerUploadId: 'provider-capacity-boundary',
        partSize: _partSize,
        completedPartNumbers: <int>[],
      ),
    );

    final staleFence = await repository.captureServerSummaryFence(_taskId);
    await repository.acknowledgeServerSummary(
      staleFence,
      _summary(totalAssets: 2999),
    );
    final stale = await repository.snapshot(_taskId);
    expect(
      stale.serverAcceptedAssetIds.length + stale.unacknowledgedAssetIds.length,
      3000,
    );
    expect(stale.unacknowledgedAssetIds, <String>{_asset17MiB.id});

    final freshFence = await repository.captureServerSummaryFence(_taskId);
    await repository.acknowledgeServerSummary(
      freshFence,
      _summary(totalAssets: 3000),
    );
    final restarted = await UploadQueueRepository(database).snapshot(_taskId);
    expect(restarted.serverAcceptedAssetIds, hasLength(3000));
    expect(restarted.unacknowledgedAssetIds, isEmpty);
  });

  test(
    '2,999 accepted plus a local rejection still leaves one picker slot',
    () async {
      final database = UploadDatabase(NativeDatabase.memory());
      addTearDown(database.close);
      final repository = UploadQueueRepository(database);
      await database.batch((batch) {
        batch.insertAll(
          database.importQueueAuthorities,
          <ImportQueueAuthoritiesCompanion>[
            for (var index = 0; index < 2999; index += 1)
              ImportQueueAuthoritiesCompanion.insert(
                taskId: _taskId,
                localAssetId: 'server:$index',
                disposition: ServerAssetDisposition.accepted.name,
              ),
          ],
        );
      });
      await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
      final claimed = await repository.nextRunnable(
        _taskId,
        ownerId: 'local-rejection-capacity-owner',
        now: DateTime.utc(2026, 8, 15),
        leaseDuration: const Duration(minutes: 30),
      );
      await repository.markLocalRejected(
        claimed!.claim,
        retryCount: 1,
        code: 'image_too_large',
        safeMessage: '无法上传该照片',
      );

      final snapshot = await repository.snapshot(_taskId);
      expect(snapshot.serverAcceptedAssetIds, hasLength(2999));
      expect(snapshot.unacknowledgedAssetIds, isEmpty);
      expect(snapshot.uploaded, 0);
      expect(snapshot.failed, 0);
      expect(
        snapshot.serverAcceptedAssetIds.length +
            snapshot.unacknowledgedAssetIds.length,
        2999,
      );
    },
  );

  test('fails closed when server completed part has no local ETag', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final repository = UploadQueueRepository(database);
    await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final claimed = await repository.nextRunnable(
      _taskId,
      ownerId: 'invalid-etag-owner',
      now: DateTime.utc(2026, 8, 15),
      leaseDuration: const Duration(minutes: 30),
    );

    await expectLater(
      repository.markInitiated(
        claimed!.claim,
        const InitiatedAsset(
          assetId: 'backend-missing-etag',
          alreadyUploaded: false,
          providerUploadId: 'provider-missing-etag',
          partSize: _partSize,
          completedPartNumbers: <int>[2],
        ),
      ),
      throwsA(
        isA<UploadQueueException>().having(
          (error) => error.code,
          'code',
          'server_part_etag_missing',
        ),
      ),
    );

    final restored = await repository.find(_asset17MiB.id);
    expect(restored!.backendAssetId, isNull);
    expect(restored.completedParts, isEmpty);
    expect(
      (await repository.snapshot(_taskId)).serverAcceptedAssetIds,
      isEmpty,
    );
  });

  test('rejects invalid or unknown completed-part ETags', () async {
    final database = UploadDatabase(NativeDatabase.memory());
    addTearDown(database.close);
    final repository = UploadQueueRepository(database);
    await repository.enqueue(_taskId, <LocalAssetRef>[_asset17MiB]);
    final claimed = await repository.nextRunnable(
      _taskId,
      ownerId: 'invalid-etag-owner',
      now: DateTime.utc(2026, 8, 15),
      leaseDuration: const Duration(minutes: 30),
    );

    for (final completion in <({int partNumber, String etag})>[
      (partNumber: 1, etag: ''),
      (partNumber: 99, etag: 'etag-unknown'),
    ]) {
      await expectLater(
        repository.markPartComplete(
          claimed!.claim,
          completion.partNumber,
          completion.etag,
        ),
        throwsA(
          isA<UploadQueueException>().having(
            (error) => error.code,
            'code',
            'invalid_transfer_result',
          ),
        ),
      );
    }

    expect((await repository.find(_asset17MiB.id))!.completedParts, isEmpty);
  });
}

const _asset17MiB = LocalAssetRef(
  id: 'ios:asset-17mib',
  displayName: 'IMG_0017.JPG',
  mediaType: 'image/jpeg',
  byteSize: 17 * 1024 * 1024,
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/app-support/asset-17mib/original',
);

final _controlledAssetA = LocalAssetRef(
  id: '11111111-1111-4111-8111-111111111111',
  displayName: '11111111-1111-4111-8111-111111111111.jpg',
  mediaType: 'image/jpeg',
  byteSize: 123,
  capturedAt: DateTime.utc(2026, 8, 19, 7),
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/synthetic/a',
);

final _controlledAssetB = LocalAssetRef(
  id: '22222222-2222-4222-8222-222222222222',
  displayName: '22222222-2222-4222-8222-222222222222.png',
  mediaType: 'image/png',
  byteSize: 456,
  capturedAt: DateTime.utc(2026, 8, 19, 7, 5),
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/synthetic/b',
);

final _controlledAssetC = LocalAssetRef(
  id: '33333333-3333-4333-8333-333333333333',
  displayName: '33333333-3333-4333-8333-333333333333.heic',
  mediaType: 'image/heic',
  byteSize: 789,
  capturedAt: DateTime.utc(2026, 8, 19, 7, 10),
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/synthetic/c',
);

LocalAssetRef _asset17MiBWith({required String id}) => LocalAssetRef(
  id: id,
  displayName: '$id.JPG',
  mediaType: 'image/jpeg',
  byteSize: 17 * 1024 * 1024,
  accessKind: AssetAccessKind.stagedFile,
  locator: '/private/app-support/$id/original',
);

final class _SchemaV1ExecutorUser implements QueryExecutorUser {
  const _SchemaV1ExecutorUser();

  @override
  int get schemaVersion => 1;

  @override
  Future<void> beforeOpen(
    QueryExecutor executor,
    OpeningDetails details,
  ) async {}
}

Matcher _queueError(String code) =>
    isA<UploadQueueException>().having((error) => error.code, 'code', code);

final class _SchemaV2ExecutorUser implements QueryExecutorUser {
  const _SchemaV2ExecutorUser();

  @override
  int get schemaVersion => 2;

  @override
  Future<void> beforeOpen(
    QueryExecutor executor,
    OpeningDetails details,
  ) async {}
}

Future<void> _createV2TerminalDatabase(File file) async {
  final legacy = NativeDatabase(file);
  await legacy.ensureOpen(const _SchemaV2ExecutorUser());
  await legacy.runCustom('''
    CREATE TABLE upload_items (
      local_asset_id TEXT NOT NULL PRIMARY KEY,
      task_id TEXT NOT NULL,
      local_ref_json TEXT NOT NULL,
      byte_size INTEGER NOT NULL,
      sha256 TEXT,
      backend_asset_id TEXT,
      provider_upload_id TEXT,
      part_size INTEGER NOT NULL,
      retry_count INTEGER NOT NULL DEFAULT 0,
      next_attempt_at INTEGER,
      state TEXT NOT NULL,
      last_error_code TEXT,
      last_error_message TEXT,
      lease_owner TEXT,
      lease_expires_at INTEGER,
      server_confirmed INTEGER NOT NULL DEFAULT 0,
      paused_from_state TEXT
    )
  ''');
  await legacy.runCustom('''
    CREATE TABLE upload_parts (
      local_asset_id TEXT NOT NULL,
      part_number INTEGER NOT NULL,
      etag TEXT,
      PRIMARY KEY (local_asset_id, part_number),
      FOREIGN KEY (local_asset_id) REFERENCES upload_items(local_asset_id)
        ON DELETE CASCADE
    )
  ''');
  await legacy.runCustom('''
    CREATE TABLE import_queue_tasks (
      task_id TEXT NOT NULL PRIMARY KEY,
      seal_when_idle INTEGER NOT NULL DEFAULT 0,
      server_sealed INTEGER NOT NULL DEFAULT 0,
      seal_lease_owner TEXT,
      seal_lease_expires_at INTEGER,
      seal_retry_count INTEGER NOT NULL DEFAULT 0,
      seal_next_attempt_at INTEGER,
      seal_last_error_code TEXT
    )
  ''');
  await legacy.runCustom('''
    CREATE TABLE import_queue_authorities (
      task_id TEXT NOT NULL,
      local_asset_id TEXT NOT NULL,
      disposition TEXT NOT NULL,
      PRIMARY KEY (task_id, local_asset_id)
    )
  ''');
  await legacy.runCustom('''
    CREATE TABLE import_cleanup_refs (
      task_id TEXT NOT NULL,
      locator TEXT NOT NULL,
      local_asset_id TEXT NOT NULL,
      local_ref_json TEXT NOT NULL,
      PRIMARY KEY (task_id, locator)
    )
  ''');
  await legacy.runCustom(
    'INSERT INTO upload_items '
    '(local_asset_id, task_id, local_ref_json, byte_size, sha256, '
    'backend_asset_id, part_size, state, server_confirmed) '
    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)',
    <Object?>[
      _asset17MiB.id,
      _taskId,
      jsonEncode(_asset17MiB.toMap()),
      _asset17MiB.byteSize,
      'a' * 64,
      'backend-v2-terminal',
      _partSize,
      LocalUploadState.uploaded.name,
    ],
  );
  await legacy.runCustom(
    'INSERT INTO import_queue_authorities '
    '(task_id, local_asset_id, disposition) VALUES (?, ?, ?)',
    <Object?>[_taskId, _asset17MiB.id, ServerAssetDisposition.accepted.name],
  );
  await legacy.runCustom('PRAGMA user_version = 2');
  await legacy.close();
}

TaskSummary _summary({
  required int totalAssets,
  int uploadedAssets = 0,
  int failedAssets = 0,
}) {
  final now = DateTime.utc(2026, 8, 15);
  return TaskSummary(
    id: _taskId,
    name: 'Import',
    status: 'waiting_upload',
    totalAssets: totalAssets,
    uploadedAssets: uploadedAssets,
    failedAssets: failedAssets,
    unsupportedAssets: 0,
    createdAt: now,
    updatedAt: now,
  );
}
