import 'dart:io';

import 'package:drift/drift.dart';
import 'package:drift/native.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path/path.dart' as paths;
import 'package:path_provider/path_provider.dart';

part 'upload_database.g.dart';

final uploadDatabaseProvider = Provider<UploadDatabase>((ref) {
  final database = UploadDatabase.open();
  ref.onDispose(database.close);
  return database;
});

class UploadItems extends Table {
  TextColumn get localAssetId => text()();
  TextColumn get taskId => text()();
  TextColumn get bindingId => text().nullable()();
  TextColumn get candidateId => text().nullable()();
  TextColumn get localRefJson => text()();
  IntColumn get byteSize => integer()();
  TextColumn get sha256 => text().nullable()();
  TextColumn get backendAssetId => text().nullable()();
  TextColumn get providerUploadId => text().nullable()();
  IntColumn get partSize => integer()();
  IntColumn get retryCount => integer().withDefault(const Constant(0))();
  DateTimeColumn get nextAttemptAt => dateTime().nullable()();
  TextColumn get state => text()();
  TextColumn get lastErrorCode => text().nullable()();
  TextColumn get lastErrorMessage => text().nullable()();
  TextColumn get leaseOwner => text().nullable()();
  DateTimeColumn get leaseExpiresAt => dateTime().nullable()();
  IntColumn get leaseGeneration => integer().withDefault(const Constant(0))();
  IntColumn get policyGeneration => integer().withDefault(const Constant(0))();
  BoolColumn get serverConfirmed =>
      boolean().withDefault(const Constant(false))();
  BoolColumn get terminalSummaryAcknowledged =>
      boolean().withDefault(const Constant(false))();
  BoolColumn get retryRequested =>
      boolean().withDefault(const Constant(false))();
  BoolColumn get localRejected =>
      boolean().withDefault(const Constant(false))();
  TextColumn get pausedFromState => text().nullable()();
  IntColumn get failureReportRetryCount =>
      integer().withDefault(const Constant(0))();
  DateTimeColumn get failureReportNextAttemptAt => dateTime().nullable()();

  @override
  Set<Column<Object>> get primaryKey => <Column<Object>>{localAssetId};
}

class UploadParts extends Table {
  TextColumn get localAssetId => text().references(
    UploadItems,
    #localAssetId,
    onDelete: KeyAction.cascade,
  )();
  IntColumn get partNumber => integer()();
  TextColumn get etag => text().nullable()();
  TextColumn get nativeJobId => text().nullable()();
  TextColumn get nativeLeaseOwner => text().nullable()();
  IntColumn get nativeLeaseGeneration => integer().nullable()();
  IntColumn get nativePolicyGeneration => integer().nullable()();

  @override
  Set<Column<Object>> get primaryKey => <Column<Object>>{
    localAssetId,
    partNumber,
  };
}

class ImportQueueTasks extends Table {
  TextColumn get taskId => text()();
  BoolColumn get sealWhenIdle => boolean().withDefault(const Constant(false))();
  BoolColumn get serverSealed => boolean().withDefault(const Constant(false))();
  DateTimeColumn get sealRequestedAt => dateTime().nullable()();
  DateTimeColumn get sealConfirmedAt => dateTime().nullable()();
  TextColumn get sealLeaseOwner => text().nullable()();
  DateTimeColumn get sealLeaseExpiresAt => dateTime().nullable()();
  IntColumn get sealRetryCount => integer().withDefault(const Constant(0))();
  DateTimeColumn get sealNextAttemptAt => dateTime().nullable()();
  TextColumn get sealLastErrorCode => text().nullable()();

  @override
  Set<Column<Object>> get primaryKey => <Column<Object>>{taskId};
}

class ImportQueueAuthorities extends Table {
  TextColumn get taskId => text()();
  TextColumn get localAssetId => text()();
  TextColumn get disposition => text()();

  @override
  Set<Column<Object>> get primaryKey => <Column<Object>>{taskId, localAssetId};
}

class ImportCleanupRefs extends Table {
  TextColumn get taskId => text()();
  TextColumn get locator => text()();
  TextColumn get localAssetId => text()();
  TextColumn get localRefJson => text()();

  @override
  Set<Column<Object>> get primaryKey => <Column<Object>>{taskId, locator};
}

@DriftDatabase(
  tables: <Type>[
    UploadItems,
    UploadParts,
    ImportQueueTasks,
    ImportQueueAuthorities,
    ImportCleanupRefs,
  ],
)
class UploadDatabase extends _$UploadDatabase {
  UploadDatabase(super.executor);

  factory UploadDatabase.open() => UploadDatabase(
    LazyDatabase(() async {
      final directory = await getApplicationSupportDirectory();
      return NativeDatabase.createInBackground(
        File(paths.join(directory.path, 'upload_queue.sqlite')),
      );
    }),
  );

  @override
  int get schemaVersion => 7;

  @override
  MigrationStrategy get migration => MigrationStrategy(
    onCreate: (migrator) async {
      await migrator.createAll();
    },
    onUpgrade: (migrator, from, to) async {
      if (from < 2) {
        await migrator.addColumn(uploadItems, uploadItems.leaseOwner);
        await migrator.addColumn(uploadItems, uploadItems.leaseExpiresAt);
        await migrator.addColumn(uploadItems, uploadItems.serverConfirmed);
        await migrator.addColumn(uploadItems, uploadItems.pausedFromState);
        await migrator.addColumn(
          importQueueTasks,
          importQueueTasks.serverSealed,
        );
        await migrator.addColumn(
          importQueueTasks,
          importQueueTasks.sealLeaseOwner,
        );
        await migrator.addColumn(
          importQueueTasks,
          importQueueTasks.sealLeaseExpiresAt,
        );
        await migrator.addColumn(
          importQueueTasks,
          importQueueTasks.sealRetryCount,
        );
        await migrator.addColumn(
          importQueueTasks,
          importQueueTasks.sealNextAttemptAt,
        );
        await migrator.addColumn(
          importQueueTasks,
          importQueueTasks.sealLastErrorCode,
        );
      }
      if (from < 3) {
        await migrator.addColumn(uploadItems, uploadItems.leaseGeneration);
        await migrator.addColumn(uploadItems, uploadItems.policyGeneration);
        await migrator.addColumn(
          uploadItems,
          uploadItems.terminalSummaryAcknowledged,
        );
        await migrator.addColumn(
          uploadItems,
          uploadItems.failureReportRetryCount,
        );
        await migrator.addColumn(
          uploadItems,
          uploadItems.failureReportNextAttemptAt,
        );
        if (from < 2) {
          await customStatement('''
            DELETE FROM import_queue_authorities
            WHERE disposition = 'accepted'
              AND EXISTS (
                SELECT 1 FROM upload_items
                WHERE upload_items.task_id = import_queue_authorities.task_id
                  AND upload_items.local_asset_id =
                    import_queue_authorities.local_asset_id
              )
          ''');
          await customStatement('''
            UPDATE upload_items
            SET server_confirmed = 1
            WHERE backend_asset_id IS NOT NULL
              AND state IN ('uploaded', 'failedPermanent')
          ''');
          await customStatement('''
            UPDATE upload_items
            SET backend_asset_id = NULL,
                provider_upload_id = NULL,
                state = CASE
                  WHEN sha256 IS NULL THEN 'awaitingHash'
                  ELSE 'awaitingInitiation'
                END
            WHERE backend_asset_id IS NOT NULL
              AND state NOT IN ('uploaded', 'failedPermanent')
          ''');
        }
      }
      if (from < 4) {
        await migrator.addColumn(uploadItems, uploadItems.retryRequested);
        await migrator.addColumn(uploadItems, uploadItems.localRejected);
        await customStatement('''
          UPDATE upload_items
          SET terminal_summary_acknowledged = 0
          WHERE server_confirmed = 1
            AND state IN ('uploaded', 'failedPermanent')
        ''');
      }
      if (from < 5) {
        await migrator.addColumn(uploadParts, uploadParts.nativeJobId);
        await migrator.addColumn(uploadParts, uploadParts.nativeLeaseOwner);
        await migrator.addColumn(
          uploadParts,
          uploadParts.nativeLeaseGeneration,
        );
        await migrator.addColumn(
          uploadParts,
          uploadParts.nativePolicyGeneration,
        );
      }
      if (from < 6) {
        await migrator.addColumn(
          importQueueTasks,
          importQueueTasks.sealRequestedAt,
        );
        await migrator.addColumn(
          importQueueTasks,
          importQueueTasks.sealConfirmedAt,
        );
      }
      if (from < 7) {
        await migrator.addColumn(uploadItems, uploadItems.bindingId);
        await migrator.addColumn(uploadItems, uploadItems.candidateId);
      }
    },
    beforeOpen: (details) async {
      await customStatement('PRAGMA foreign_keys = ON');
    },
  );
}
